from __future__ import annotations

import csv
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any

try:
    import serial
except ImportError:  # pragma: no cover - depends on local environment.
    serial = None

LOGGER = logging.getLogger(__name__)


class BaseReaderThread(threading.Thread):
    def __init__(self, name: str, mode: str, lineQueue: "queue.Queue[str]"):
        super().__init__(name=name, daemon=True)
        self.lineQueue = lineQueue
        self.mode = mode
        self._stopEvent = threading.Event()
        self._statusLock = threading.Lock()
        self._status = {
            "mode": mode,
            "active": False,
            "serialConnected": False,
            "rawLinesReceived": 0,
            "lastLineTime": None,
            "lastError": "",
            "replayFinished": False,
        }

    def stop(self) -> None:
        self._stopEvent.set()

    def getStatus(self) -> dict:
        with self._statusLock:
            return dict(self._status)

    def _set_status(self, **kwargs: Any) -> None:
        with self._statusLock:
            self._status.update(kwargs)

    def _put_line(self, line: str) -> None:
        self.lineQueue.put(line)
        with self._statusLock:
            self._status["rawLinesReceived"] += 1
            self._status["lastLineTime"] = time.time()

    def _sleep_stoppable(self, seconds: float) -> bool:
        return self._stopEvent.wait(max(0.0, seconds))


class SerialReaderThread(BaseReaderThread):
    def __init__(
        self,
        port: str,
        baud: int,
        lineQueue: "queue.Queue[str]",
        reconnectIntervalSeconds: float = 1.0,
    ):
        super().__init__("SerialReaderThread", "serial", lineQueue)
        self.port = port
        self.baud = int(baud)
        self.reconnectIntervalSeconds = reconnectIntervalSeconds
        self._serialLock = threading.Lock()
        self._serialHandle: Any | None = None
        self._set_status(port=port, baud=self.baud)

    def run(self) -> None:
        self._set_status(active=True)
        LOGGER.info("Serial reader starting on %s at %d baud", self.port, self.baud)

        try:
            while not self._stopEvent.is_set():
                serial_handle = self._open_serial()
                if serial_handle is None:
                    if self._sleep_stoppable(self.reconnectIntervalSeconds):
                        break
                    continue

                while not self._stopEvent.is_set():
                    try:
                        raw_line = serial_handle.readline()
                        if not raw_line:
                            continue
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        self._put_line(line)
                    except Exception as exc:
                        self._set_status(serialConnected=False, lastError=str(exc))
                        LOGGER.warning("Serial disconnected: %s", exc)
                        self._close_serial()
                        break
        finally:
            self._close_serial()
            self._set_status(active=False, serialConnected=False)
            LOGGER.info("Serial reader stopped")

    def stop(self) -> None:
        super().stop()
        self._close_serial()

    def _open_serial(self) -> Any | None:
        if serial is None:
            self._set_status(
                serialConnected=False,
                lastError="pyserial is not installed; run pip install -r requirements.txt",
            )
            return None

        try:
            serial_handle = serial.Serial(self.port, baudrate=self.baud, timeout=0.1)
        except Exception as exc:
            self._set_status(serialConnected=False, lastError=str(exc))
            LOGGER.debug("Serial open failed: %s", exc)
            return None

        with self._serialLock:
            self._serialHandle = serial_handle
        self._set_status(serialConnected=True, lastError="")
        LOGGER.info("Serial connected on %s", self.port)
        return serial_handle

    def _close_serial(self) -> None:
        with self._serialLock:
            serial_handle = self._serialHandle
            self._serialHandle = None

        if serial_handle is not None:
            try:
                serial_handle.close()
            except Exception:
                LOGGER.debug("Serial close failed", exc_info=True)


class ReplayReaderThread(BaseReaderThread):
    def __init__(
        self,
        replayFile: str | Path,
        replaySpeed: float,
        lineQueue: "queue.Queue[str]",
    ):
        super().__init__("ReplayReaderThread", "replay", lineQueue)
        self.replayFile = Path(replayFile)
        self.replaySpeed = replaySpeed if replaySpeed and replaySpeed > 0 else 1.0
        self._set_status(replayFile=str(self.replayFile), replaySpeed=self.replaySpeed)

    def run(self) -> None:
        self._set_status(active=True, replayFinished=False, lastError="")
        LOGGER.info("Replay reader starting: %s at %.3gx", self.replayFile, self.replaySpeed)

        previous_timestamp_us: int | None = None
        try:
            with self.replayFile.open("r", encoding="utf-8", errors="replace") as log_file:
                for raw_line in log_file:
                    if self._stopEvent.is_set():
                        break

                    line = raw_line.strip()
                    if not (line.startswith("MATV_HEADER,") or line.startswith("MATV,")):
                        continue

                    timestamp_us = self._extract_timestamp_us(line)
                    if timestamp_us is not None and previous_timestamp_us is not None:
                        delta_us = timestamp_us - previous_timestamp_us
                        if delta_us > 0:
                            sleep_seconds = min(delta_us / 1_000_000.0 / self.replaySpeed, 1.0)
                            if self._sleep_stoppable(sleep_seconds):
                                break

                    self._put_line(line)
                    if timestamp_us is not None:
                        previous_timestamp_us = timestamp_us
        except Exception as exc:
            self._set_status(lastError=str(exc))
            LOGGER.error("Replay reader failed: %s", exc)
        finally:
            self._set_status(active=False, replayFinished=True)
            LOGGER.info("Replay reader stopped")

    @staticmethod
    def _extract_timestamp_us(line: str) -> int | None:
        if not line.startswith("MATV,"):
            return None

        try:
            fields = next(csv.reader([line]))
            if len(fields) < 3:
                return None
            return int(fields[2].strip())
        except Exception:
            return None

