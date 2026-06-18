from __future__ import annotations

import queue
import threading
from pathlib import Path


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
