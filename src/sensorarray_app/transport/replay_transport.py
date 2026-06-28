from __future__ import annotations

import queue
import json
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from sensorarray_app.constants import CAP_FIXED_SCALE, CAP_INVALID_SENTINEL, FDC_CIRCUIT_OFFSET_PF
from sensorarray_app.domain.models import TransportEnvelope, TransportStateEvent
from sensorarray_app.protocol.crc import crc32_reflected
from sensorarray_backend.core.session_data import frames_to_cap_ascii_bytes, load_session_frames


class ReplayTransport:
    source = "replay"

    def __init__(
        self,
        output_queue: "queue.Queue[TransportEnvelope | TransportStateEvent]",
        session_generation: int,
        path: str | Path,
        speed: float = 1.0,
        chunk_size: int = 4096,
    ):
        self.outputQueue = output_queue
        self.sessionGeneration = int(session_generation)
        self.path = Path(path)
        self.speed = max(0.001, float(speed or 1.0))
        self.chunkSize = max(1, int(chunk_size))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="SensorArrayReplayTransport", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def send_command(self, command: str) -> None:
        raise NotImplementedError("replay transport does not support write")

    def write(self, data: bytes) -> int:
        raise NotImplementedError("replay transport does not support write")

    def _run(self) -> None:
        self._put_state("STREAMING", str(self.path))
        try:
            payload = _session_file_to_replay_bytes(self.path)
            if payload is not None:
                self._stream_bytes(payload)
            else:
                with self.path.open("rb") as handle:
                    while not self._stop.is_set():
                        chunk = handle.read(self.chunkSize)
                        if not chunk:
                            break
                        self._put_payload(chunk)
                        time.sleep(min(0.02 / self.speed, 0.25))
        except Exception as exc:
            self._put_state("ERROR", str(exc))
        self._put_state("DISCONNECTED", "")

    def _stream_bytes(self, payload: bytes) -> None:
        offset = 0
        while not self._stop.is_set() and offset < len(payload):
            chunk = payload[offset : offset + self.chunkSize]
            offset += len(chunk)
            self._put_payload(chunk)
            time.sleep(min(0.02 / self.speed, 0.25))

    def _put_payload(self, payload: bytes) -> None:
        envelope = TransportEnvelope(
            source="replay",
            channel="data",
            deviceId=str(self.path),
            sessionGeneration=self.sessionGeneration,
            receivedMonotonicNs=time.monotonic_ns(),
            receivedWallTime=time.time(),
            rawPayload=payload,
        )
        self.outputQueue.put(envelope, timeout=0.1)

    def _put_state(self, state: str, message: str) -> None:
        try:
            self.outputQueue.put_nowait(TransportStateEvent("replay", state, self.sessionGeneration, message))
        except queue.Full:
            pass


def _session_file_to_replay_bytes(path: Path) -> bytes | None:
    if path.suffix.lower() in {".csv", ".xlsx", ".mat", ".h5", ".hdf5"}:
        return frames_to_cap_ascii_bytes(load_session_frames(path))
    if path.suffix.lower() != ".json":
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or "metadata" not in payload or "currentMatrix" not in payload:
        return None
    frames = _session_frames(payload)
    raw_logs = payload.get("rawLogs") if isinstance(payload.get("rawLogs"), list) else []
    out = bytearray()
    for frame in frames:
        out.extend(_frame_to_cap_ascii(frame))
    for row in raw_logs:
        raw_text = row.get("rawText") if isinstance(row, dict) else None
        if isinstance(raw_text, str) and raw_text.strip():
            text = raw_text.strip()
            if not text.startswith(("C,", "D", "K,")):
                out.extend(text.encode("ascii", errors="ignore") + b"\n")
    return bytes(out)


def _session_frames(payload: dict[str, Any]) -> list[dict[str, Any]]:
    history_frames = payload.get("historyFrames")
    if isinstance(history_frames, list) and history_frames:
        return [frame for frame in history_frames if isinstance(frame, dict)]
    matrix = payload.get("currentMatrix") if isinstance(payload.get("currentMatrix"), dict) else {}
    corrected = _flatten_matrix(matrix.get("correctedPf"))
    valid = _flatten_bool_matrix(matrix.get("validMask"))
    rows = int(payload.get("metadata", {}).get("rows") or 8)
    return [
        {
            "seq": int(payload.get("metadata", {}).get("frameSeq") or 1),
            "timeSeconds": 0.0,
            "rows": rows,
            "valuesPf": corrected,
            "valid": valid,
        }
    ]


def _frame_to_cap_ascii(frame: dict[str, Any]) -> bytes:
    rows = max(1, min(8, int(frame.get("rows") or 8)))
    cells = rows * 8
    seq = max(0, int(frame.get("seq") or 0))
    time_seconds = _finite_number(frame.get("timeSeconds"), 0.0)
    values = list(frame.get("valuesPf") or [])
    valid = list(frame.get("valid") or [])
    raw_fixed: list[int] = []
    for index in range(cells):
        value = _finite_number(values[index] if index < len(values) else None, np.nan)
        is_valid = bool(valid[index]) if index < len(valid) else np.isfinite(value)
        if not is_valid or not np.isfinite(value):
            raw_fixed.append(CAP_INVALID_SENTINEL)
        else:
            raw_fixed.append(int(round((float(value) + FDC_CIRCUIT_OFFSET_PF) * CAP_FIXED_SCALE)))
    header = (
        f"C,seq={seq},ts={int(time_seconds * 1_000_000)},rows={rows},cells={cells},gen=1,rid=1,"
        f"rf=FF,pf=FF,sf=FF,bad=0/0/0,fmt=pf6,n={cells}\n"
    ).encode("ascii")
    body = bytearray(header)
    for line_index, start in enumerate(range(0, cells, 16)):
        values_text = ",".join(str(value) for value in raw_fixed[start : start + 16])
        body.extend(f"D{line_index},{values_text}\n".encode("ascii"))
    crc = crc32_reflected(bytes(body))
    body.extend(f"K,seq={seq},gen=1,rid=1,crc={crc:08X}\n".encode("ascii"))
    return bytes(body)


def _flatten_matrix(value: Any) -> list[float | None]:
    if not isinstance(value, list):
        return [None for _ in range(64)]
    out: list[float | None] = []
    for row in value[:8]:
        if not isinstance(row, list):
            out.extend([None for _ in range(8)])
        else:
            out.extend([_finite_number(item, None) for item in row[:8]])
            out.extend([None for _ in range(max(0, 8 - len(row)))])
    out.extend([None for _ in range(max(0, 64 - len(out)))])
    return out[:64]


def _flatten_bool_matrix(value: Any) -> list[bool]:
    if not isinstance(value, list):
        return [False for _ in range(64)]
    out: list[bool] = []
    for row in value[:8]:
        if not isinstance(row, list):
            out.extend([False for _ in range(8)])
        else:
            out.extend([bool(item) for item in row[:8]])
            out.extend([False for _ in range(max(0, 8 - len(row)))])
    out.extend([False for _ in range(max(0, 64 - len(out)))])
    return out[:64]


def _finite_number(value: Any, default: float | None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(number):
        return default
    return number
