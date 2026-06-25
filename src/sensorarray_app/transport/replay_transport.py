from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from sensorarray_app.domain.models import TransportEnvelope, TransportStateEvent


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
            with self.path.open("rb") as handle:
                while not self._stop.is_set():
                    chunk = handle.read(self.chunkSize)
                    if not chunk:
                        break
                    envelope = TransportEnvelope(
                        source="replay",
                        channel="data",
                        deviceId=str(self.path),
                        sessionGeneration=self.sessionGeneration,
                        receivedMonotonicNs=time.monotonic_ns(),
                        receivedWallTime=time.time(),
                        rawPayload=chunk,
                    )
                    self.outputQueue.put(envelope, timeout=0.1)
                    time.sleep(min(0.02 / self.speed, 0.25))
        except Exception as exc:
            self._put_state("ERROR", str(exc))
        self._put_state("DISCONNECTED", "")

    def _put_state(self, state: str, message: str) -> None:
        try:
            self.outputQueue.put_nowait(TransportStateEvent("replay", state, self.sessionGeneration, message))
        except queue.Full:
            pass
