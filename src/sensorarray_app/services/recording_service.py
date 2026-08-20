from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import queue
import threading
from pathlib import Path
import time
from typing import Any
import uuid

import numpy as np

from sensorarray_app.domain.models import CapacitanceFrame, MeasurementFrame, MixedMeasurementFrame


SESSION_SCHEMA_VERSION = 3


class RawRecordingWriter:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.queue: queue.Queue[bytes | None] = queue.Queue(maxsize=4096)
        self._thread = threading.Thread(target=self._run, name="SensorArrayRawRecorder", daemon=True)
        self._thread.start()

    def write(self, payload: bytes) -> None:
        try:
            self.queue.put_nowait(bytes(payload))
        except queue.Full:
            pass

    def close(self) -> None:
        self.queue.put(None)
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as handle:
            while True:
                item = self.queue.get()
                if item is None:
                    return
                handle.write(item)


class ScientificRecorder:
    """Loss-accounted, UI-independent writer for accepted logical frames.

    One recording remains open across temporary transport reconnects. Device
    reboot and transport gap records are written to the event stream so a
    repeated sequence number can never be mistaken for continuous acquisition.
    """

    def __init__(self, queue_size: int = 8192):
        self.queueSize = max(64, int(queue_size))
        self._queue: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue(maxsize=self.queueSize)
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self.state = "NOT_RECORDING"
        self.sessionId: str | None = None
        self.directory: Path | None = None
        self.receivedFrames = 0
        self.writtenFrames = 0
        self.writtenEvents = 0
        self.droppedFrames = 0
        self._pendingGapCount = 0
        self._pendingGapFirstSeq: int | None = None
        self._pendingGapLastSeq: int | None = None
        self._pendingGapBootId: int | None = None
        self._pendingGapConnectionGeneration: int | None = None
        self.error = ""
        self.startedAt: str | None = None
        self.finishedAt: str | None = None

    def start(self, directory: str | Path, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if self.state in {"RECORDING", "FINALIZING"}:
                raise RuntimeError("scientific recording is already active")
            target = Path(directory).expanduser()
            session_id = str(uuid.uuid4())
            if target.suffix:
                target = target.parent / f"{target.stem}-{session_id}"
            else:
                target = target / f"sensorarray-recording-{session_id}"
            target.mkdir(parents=True, exist_ok=False)
            self._queue = queue.Queue(maxsize=self.queueSize)
            self.sessionId = session_id
            self.directory = target
            self.receivedFrames = 0
            self.writtenFrames = 0
            self.writtenEvents = 0
            self.droppedFrames = 0
            self._clear_pending_gap()
            self.error = ""
            self.startedAt = datetime.now(timezone.utc).isoformat()
            self.finishedAt = None
            _write_json(
                target / "metadata.json",
                {
                    "schemaVersion": SESSION_SCHEMA_VERSION,
                    "sessionId": session_id,
                    "startedAt": self.startedAt,
                    "clean": False,
                    **dict(metadata or {}),
                },
            )
            self.state = "RECORDING"
            self._thread = threading.Thread(target=self._run, name="SensorArrayScientificRecorder", daemon=True)
            self._thread.start()
            return self.snapshot()

    def record_frame(
        self,
        frame: CapacitanceFrame | MeasurementFrame | MixedMeasurementFrame,
        *,
        configured_row_profile: tuple[str, ...] | None = None,
        rail: dict[str, Any] | None = None,
    ) -> bool:
        with self._lock:
            if self.state != "RECORDING" or self.sessionId is None:
                return False
            self.receivedFrames += 1
            payload = frame_to_v3_record(
                frame,
                session_id=self.sessionId,
                configured_row_profile=configured_row_profile,
                rail=rail,
            )
            if self._pendingGapCount and not self._flush_pending_gap_nowait():
                self._note_frame_drop(payload)
                return False
            try:
                self._queue.put_nowait(("frame", payload))
                return True
            except queue.Full:
                self._note_frame_drop(payload)
                return False

    def record_event(self, kind: str, details: dict[str, Any] | None = None) -> bool:
        with self._lock:
            if self.state != "RECORDING" or self.sessionId is None:
                return False
            if self._pendingGapCount and not self._flush_pending_gap_nowait():
                self.error = "recording queue full while writing lifecycle event after a frame gap"
                self.state = "ERROR"
                return False
            payload = {
                "schemaVersion": SESSION_SCHEMA_VERSION,
                "sessionId": self.sessionId,
                "kind": str(kind),
                "hostTimestampUtc": datetime.now(timezone.utc).isoformat(),
                "hostMonotonicNs": time.monotonic_ns(),
                **dict(details or {}),
            }
            try:
                self._queue.put_nowait(("event", payload))
                return True
            except queue.Full:
                # Event loss makes the scientific timeline ambiguous. Surface
                # it as a recorder error rather than hiding it as a frame drop.
                self.error = "recording queue full while writing lifecycle event"
                self.state = "ERROR"
                return False

    def stop(self, timeout: float = 10.0) -> dict[str, Any]:
        with self._lock:
            if self.state == "NOT_RECORDING":
                return self.snapshot()
            if self.state not in {"RECORDING", "ERROR"}:
                raise RuntimeError(f"cannot stop recorder from state {self.state}")
            self.state = "FINALIZING"
            pending_gap = self._pending_gap_payload()
        if pending_gap is not None:
            try:
                self._queue.put(("event", pending_gap), timeout=2.0)
                with self._lock:
                    self._clear_pending_gap()
            except queue.Full:
                with self._lock:
                    self.state = "ERROR"
                    self.error = "recording gap event could not be finalized"
                return self.snapshot()
        try:
            self._queue.put(None, timeout=2.0)
        except queue.Full:
            with self._lock:
                self.state = "ERROR"
                self.error = "recording queue could not be finalized"
            return self.snapshot()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.1, float(timeout)))
        with self._lock:
            if thread is not None and thread.is_alive():
                self.state = "ERROR"
                self.error = "recording writer did not stop before timeout"
            elif self.error:
                self.state = "ERROR"
            else:
                self.state = "NOT_RECORDING"
            self.finishedAt = datetime.now(timezone.utc).isoformat()
            self._finalize_metadata(clean=self.state == "NOT_RECORDING")
            return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.state,
                "schemaVersion": SESSION_SCHEMA_VERSION,
                "sessionId": self.sessionId,
                "directory": str(self.directory) if self.directory is not None else None,
                "receivedFrames": self.receivedFrames,
                "writtenFrames": self.writtenFrames,
                "writtenEvents": self.writtenEvents,
                "queueDepth": self._queue.qsize(),
                "queueCapacity": self.queueSize,
                "droppedFrames": self.droppedFrames,
                "pendingGapFrames": self._pendingGapCount,
                "error": self.error,
                "startedAt": self.startedAt,
                "finishedAt": self.finishedAt,
            }

    def _run(self) -> None:
        assert self.directory is not None
        try:
            with (self.directory / "frames.jsonl").open("a", encoding="utf-8", newline="\n") as frames_handle, (
                self.directory / "events.jsonl"
            ).open("a", encoding="utf-8", newline="\n") as events_handle:
                while True:
                    item = self._queue.get()
                    try:
                        if item is None:
                            frames_handle.flush()
                            events_handle.flush()
                            return
                        kind, payload = item
                        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
                        if kind == "frame":
                            frames_handle.write(encoded)
                            self.writtenFrames += 1
                        else:
                            events_handle.write(encoded)
                            self.writtenEvents += 1
                    finally:
                        self._queue.task_done()
        except Exception as exc:  # pragma: no cover - exercised with I/O fault injection
            with self._lock:
                self.error = str(exc)
                self.state = "ERROR"

    def _finalize_metadata(self, *, clean: bool) -> None:
        if self.directory is None or self.sessionId is None:
            return
        _write_json(
            self.directory / "metadata.json",
            {
                "schemaVersion": SESSION_SCHEMA_VERSION,
                "sessionId": self.sessionId,
                "startedAt": self.startedAt,
                "finishedAt": self.finishedAt,
                "clean": bool(clean),
                "receivedFrames": self.receivedFrames,
                "writtenFrames": self.writtenFrames,
                "writtenEvents": self.writtenEvents,
                "droppedFrames": self.droppedFrames,
                "pendingGapFrames": self._pendingGapCount,
                "error": self.error,
            },
        )

    def _note_frame_drop(self, payload: dict[str, Any]) -> None:
        self.droppedFrames += 1
        self._pendingGapCount += 1
        sequence = _optional_int(payload.get("seq"))
        if self._pendingGapFirstSeq is None:
            self._pendingGapFirstSeq = sequence
            self._pendingGapBootId = _optional_int(payload.get("bootId"))
            self._pendingGapConnectionGeneration = _optional_int(payload.get("connectionGeneration"))
        self._pendingGapLastSeq = sequence

    def _pending_gap_payload(self) -> dict[str, Any] | None:
        if self._pendingGapCount <= 0 or self.sessionId is None:
            return None
        return {
            "schemaVersion": SESSION_SCHEMA_VERSION,
            "sessionId": self.sessionId,
            "kind": "RECORDING_GAP",
            "hostTimestampUtc": datetime.now(timezone.utc).isoformat(),
            "hostMonotonicNs": time.monotonic_ns(),
            "droppedFrames": self._pendingGapCount,
            "firstSeq": self._pendingGapFirstSeq,
            "lastSeq": self._pendingGapLastSeq,
            "bootId": self._pendingGapBootId,
            "connectionGeneration": self._pendingGapConnectionGeneration,
        }

    def _flush_pending_gap_nowait(self) -> bool:
        payload = self._pending_gap_payload()
        if payload is None:
            return True
        try:
            self._queue.put_nowait(("event", payload))
        except queue.Full:
            return False
        self._clear_pending_gap()
        return True

    def _clear_pending_gap(self) -> None:
        self._pendingGapCount = 0
        self._pendingGapFirstSeq = None
        self._pendingGapLastSeq = None
        self._pendingGapBootId = None
        self._pendingGapConnectionGeneration = None


def frame_to_v3_record(
    frame: CapacitanceFrame | MeasurementFrame | MixedMeasurementFrame,
    *,
    session_id: str,
    configured_row_profile: tuple[str, ...] | None = None,
    rail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the lossless Session V3 frame record used by recorder/export."""

    rows = int(frame.rows)
    cells = rows * 8
    physical = np.full(64, np.nan, dtype=np.float64)
    raw_fixed = np.full(64, np.nan, dtype=np.float64)
    expected = np.zeros(64, dtype=bool)
    acquired = np.zeros(64, dtype=bool)
    fresh = np.zeros(64, dtype=bool)
    valid = np.zeros(64, dtype=bool)
    error = np.zeros(64, dtype=bool)
    error_codes = np.full(64, -1, dtype=np.int16)
    error_reasons = np.full(64, "", dtype=object)
    pga = np.full(64, -1, dtype=np.int16)
    pga_bypass = np.zeros(64, dtype=bool)
    row_modes = ["NONE"] * 8
    row_units = [""] * 8
    row_scales: list[int | None] = [None] * 8
    rows_generation = rows_request_id = mode_generation = mode_request_id = None
    profile_generation = profile_request_id = None
    wire_profile = None

    if isinstance(frame, CapacitanceFrame):
        frame_kind = "CAP"
        row_modes[:rows] = ["CAP"] * rows
        row_units[:rows] = ["pF"] * rows
        row_scales[:rows] = [-6] * rows
        physical[:cells] = np.asarray(frame.correctedPfValues, dtype=np.float64)
        raw_fixed[:cells] = np.asarray(frame.rawFixedValues, dtype=np.float64)
        valid[:cells] = np.asarray(frame.validMask, dtype=bool)
        for row_index in range(rows):
            row_bit = 1 << row_index
            for col_index in range(8):
                device_mask = frame.primaryFreshMask if col_index < 4 else frame.secondaryFreshMask
                fresh[row_index * 8 + col_index] = bool(frame.rowFreshMask & row_bit and device_mask & row_bit)
        error[:cells] = ~valid[:cells]
        error_codes[:cells] = np.where(valid[:cells], -1, 0x14)
        error_reasons[:cells] = np.where(valid[:cells], "", "Invalid capacitance value")
        rows_generation = frame.generation
        rows_request_id = frame.requestId
        masks_known = bool(frame.acquisitionMasksKnown)
    elif isinstance(frame, MeasurementFrame):
        frame_kind = frame.mode
        row_modes[:rows] = [frame.mode] * rows
        row_units[:rows] = [frame.unit] * rows
        row_scales[:rows] = [frame.scale] * rows
        physical[:cells] = np.asarray(frame.physicalValues, dtype=np.float64)
        raw_fixed[:cells] = np.asarray(frame.rawFixedValues, dtype=np.float64)
        valid[:cells] = np.asarray(frame.validMask, dtype=bool)
        fresh[:cells] = np.asarray(frame.freshMask, dtype=bool)
        error[:cells] = np.asarray(frame.errorMask, dtype=bool)
        codes = np.asarray(frame.errorCodes, dtype=np.int16)
        error_codes[:cells] = np.where(error[:cells], codes, -1)
        error_reasons[:cells] = np.asarray(frame.errorReasons, dtype=object)
        pga[:cells] = np.asarray(frame.pgaValues, dtype=np.int16)
        pga_bypass[:cells] = np.asarray(frame.pgaBypassMask, dtype=bool)
        mode_generation = frame.generation
        mode_request_id = frame.requestId
        masks_known = bool(frame.acquisitionMasksKnown)
        # A V/R frame carries the exact rail sample used for that conversion.
        # It is more authoritative than a later global telemetry snapshot and
        # must win when both dictionaries expose the same canonical keys.
        rail = {
            **dict(rail or {}),
            "railValid": frame.railValid,
            "railFresh": frame.railValid,
            "railAge": frame.railAgeFrames,
            "avddUv": frame.avddUv,
            "avssUv": frame.avssUv,
            "railSpanUv": frame.avddUv - frame.avssUv,
            "railSource": "frame",
            "railReason": "ok" if frame.railValid else "rail_invalid",
            "bootId": frame.bootId,
        }
    else:
        frame_kind = "MIXED"
        wire_profile = frame.wireProfile
        profile_generation = frame.profileGeneration
        profile_request_id = frame.profileRequestId
        rows_generation = frame.rowsGeneration
        rows_request_id = frame.rowsRequestId
        masks_known = True
        for row_frame in frame.rowFrames:
            row_index = int(row_frame.row) - 1
            start = row_index * 8
            stop = start + 8
            row_modes[row_index] = row_frame.mode
            row_units[row_index] = row_frame.unit
            row_scales[row_index] = row_frame.scale
            physical[start:stop] = np.asarray(row_frame.physicalValues, dtype=np.float64)
            raw_fixed[start:stop] = np.asarray(row_frame.rawFixedValues, dtype=np.float64)
            valid[start:stop] = np.asarray(row_frame.validMask, dtype=bool)
            fresh[start:stop] = np.asarray(row_frame.freshMask, dtype=bool)
            error[start:stop] = np.asarray(row_frame.errorMask, dtype=bool)
            codes = np.asarray(row_frame.errorCodes, dtype=np.int16)
            error_codes[start:stop] = np.where(error[start:stop], codes, -1)
            error_reasons[start:stop] = np.asarray(row_frame.errorReasons, dtype=object)
            if row_frame.pgaValues is not None:
                pga[start:stop] = np.asarray(row_frame.pgaValues, dtype=np.int16)
                pga_bypass[start:stop] = np.asarray(row_frame.pgaBypassMask, dtype=bool)

    expected[:cells] = np.asarray(frame.expectedMask, dtype=bool).reshape(cells)
    acquired[:cells] = np.asarray(frame.acquiredMask, dtype=bool).reshape(cells)
    configured = list(configured_row_profile) if configured_row_profile is not None else [
        mode if mode != "NONE" else "CAP" for mode in row_modes
    ]
    host_utc = datetime.fromtimestamp(float(frame.receivedTime), timezone.utc).isoformat()
    supplied_rail = dict(rail or {})
    rail_payload = {
        **supplied_rail,
        "railValid": bool(supplied_rail.get("railValid", supplied_rail.get("valid", False))),
        "railFresh": bool(supplied_rail.get("railFresh", supplied_rail.get("fresh", False))),
        "railAge": supplied_rail.get("railAge", supplied_rail.get("age")),
        "railSource": supplied_rail.get("railSource", supplied_rail.get("source", "")),
        "railReason": supplied_rail.get("railReason", supplied_rail.get("reason", "")),
    }
    return _json_safe(
        {
            "schemaVersion": SESSION_SCHEMA_VERSION,
            "sessionId": session_id,
            "connectionGeneration": int(frame.connectionGeneration),
            "bootId": frame.bootId,
            "deviceTimestampUs": int(frame.timestampUs),
            "hostReceivedUtc": host_utc,
            "hostWallTime": float(frame.receivedTime),
            "hostReceivedMonotonicNs": int(frame.receivedMonotonicNs),
            "seq": int(frame.seq),
            "rows": rows,
            "frameKind": frame_kind,
            "source": str(frame.sourceTransport),
            "rowsGeneration": rows_generation,
            "rowsRequestId": rows_request_id,
            "modeGeneration": mode_generation,
            "modeRequestId": mode_request_id,
            "profileGeneration": profile_generation,
            "profileRequestId": profile_request_id,
            "configuredRowProfile": configured,
            "wireRowProfile": wire_profile,
            "rowModes": row_modes,
            "rowUnits": row_units,
            "rowScales": row_scales,
            "acquisitionMasksKnown": masks_known,
            "expectedKnown": [masks_known] * 64,
            "acquiredKnown": [masks_known] * 64,
            "freshKnown": [True] * 64,
            "expected": expected.tolist(),
            "acquired": acquired.tolist(),
            "fresh": fresh.tolist(),
            "valid": valid.tolist(),
            "error": error.tolist(),
            "errorCodes": error_codes.tolist(),
            "errorReasons": error_reasons.tolist(),
            "pga": pga.tolist(),
            "pgaBypass": pga_bypass.tolist(),
            "rawFixed": raw_fixed.tolist(),
            "physicalValues": physical.tolist(),
            "rail": rail_payload,
        }
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_json_safe(item) for item in list(value)]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def _optional_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


__all__ = ["RawRecordingWriter", "ScientificRecorder", "frame_to_v3_record"]
