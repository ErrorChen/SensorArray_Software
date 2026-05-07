from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any

from .config import DEFAULT_SERIAL_READ_SIZE, DEFAULT_SERIAL_TIMEOUT_SECONDS

try:
    import serial
except ImportError:  # pragma: no cover - depends on local environment.
    serial = None

LOGGER = logging.getLogger(__name__)


class BaseReaderThread(threading.Thread):
    def __init__(self, name: str, mode: str, inputQueue: "queue.Queue[bytes]"):
        super().__init__(name=name, daemon=True)
        self.inputQueue = inputQueue
        self.mode = mode
        self._stopEvent = threading.Event()
        self._statusLock = threading.Lock()
        self._status = {
            "mode": mode,
            "active": False,
            "serialPort": "",
            "baud": None,
            "serialConnected": False,
            "bytesReceived": 0,
            "chunksReceived": 0,
            "rawLinesReceived": 0,
            "droppedInputBytes": 0,
            "droppedInputChunks": 0,
            "lastDataTime": None,
            "lastLineTime": None,
            "lastError": "",
            "reconnectAttempts": 0,
            "autoReconnect": False,
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

    def _put_bytes(self, data: bytes) -> None:
        if not data:
            return
        chunk = bytes(data)
        dropped_bytes = 0
        dropped_chunks = 0
        try:
            self.inputQueue.put_nowait(chunk)
        except queue.Full:
            try:
                dropped = self.inputQueue.get_nowait()
            except queue.Empty:
                dropped = None
            if dropped is not None:
                dropped_chunks += 1
                try:
                    dropped_bytes += len(dropped)
                except TypeError:
                    dropped_bytes += 0
            try:
                self.inputQueue.put_nowait(chunk)
            except queue.Full:
                dropped_chunks += 1
                dropped_bytes += len(chunk)
        now = time.time()
        with self._statusLock:
            self._status["bytesReceived"] += len(chunk)
            self._status["chunksReceived"] += 1
            self._status["rawLinesReceived"] += chunk.count(b"\n")
            self._status["droppedInputBytes"] += dropped_bytes
            self._status["droppedInputChunks"] += dropped_chunks
            self._status["lastDataTime"] = now
            self._status["lastLineTime"] = now

    def _sleep_stoppable(self, seconds: float) -> bool:
        return self._stopEvent.wait(max(0.0, seconds))


class SerialReaderThread(BaseReaderThread):
    def __init__(
        self,
        port: str,
        baud: int,
        inputQueue: "queue.Queue[bytes]",
        autoReconnect: bool = False,
        reconnectIntervalSeconds: float = 1.0,
        readSize: int = DEFAULT_SERIAL_READ_SIZE,
        timeoutSeconds: float = DEFAULT_SERIAL_TIMEOUT_SECONDS,
    ):
        super().__init__("SerialReaderThread", "serial", inputQueue)
        self.port = str(port)
        self.baud = int(baud)
        self.autoReconnect = bool(autoReconnect)
        self.reconnectIntervalSeconds = reconnectIntervalSeconds
        self.readSize = max(4096, int(readSize))
        self.timeoutSeconds = max(0.02, min(0.1, float(timeoutSeconds)))
        self._serialLock = threading.Lock()
        self._serialHandle: Any | None = None
        self._set_status(
            serialPort=self.port,
            port=self.port,
            baud=self.baud,
            autoReconnect=self.autoReconnect,
            readSize=self.readSize,
        )

    def run(self) -> None:
        self._set_status(active=True)
        LOGGER.info("Serial reader starting on %s at %d baud", self.port, self.baud)

        try:
            while not self._stopEvent.is_set():
                serial_handle = self._open_serial()
                if serial_handle is None:
                    if not self.autoReconnect:
                        break
                    self._increment_reconnect_attempts()
                    if self._sleep_stoppable(self.reconnectIntervalSeconds):
                        break
                    continue

                while not self._stopEvent.is_set():
                    try:
                        data = serial_handle.read(self.readSize)
                        if data:
                            self._put_bytes(data)
                    except Exception as exc:
                        self._set_status(serialConnected=False, lastError=str(exc))
                        LOGGER.warning("Serial disconnected: %s", exc)
                        self._close_serial()
                        break

                if not self.autoReconnect:
                    break
                self._increment_reconnect_attempts()
                if self._sleep_stoppable(self.reconnectIntervalSeconds):
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
            serial_handle = serial.Serial(self.port, baudrate=self.baud, timeout=self.timeoutSeconds)
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

    def _increment_reconnect_attempts(self) -> None:
        with self._statusLock:
            self._status["reconnectAttempts"] += 1


class ReplayReaderThread(BaseReaderThread):
    def __init__(
        self,
        replayFile: str | Path,
        replaySpeed: float,
        inputQueue: "queue.Queue[bytes]",
        chunkSize: int = DEFAULT_SERIAL_READ_SIZE,
    ):
        super().__init__("ReplayReaderThread", "replay", inputQueue)
        self.replayFile = Path(replayFile)
        self.replaySpeed = replaySpeed if replaySpeed and replaySpeed > 0 else 1.0
        self.chunkSize = max(1, int(chunkSize))
        self._set_status(replayFile=str(self.replayFile), replaySpeed=self.replaySpeed)

    def run(self) -> None:
        self._set_status(active=True, replayFinished=False, lastError="")
        LOGGER.info("Replay reader starting: %s at %.3gx", self.replayFile, self.replaySpeed)

        try:
            with self.replayFile.open("rb") as replay_file:
                while not self._stopEvent.is_set():
                    chunk = replay_file.read(self.chunkSize)
                    if not chunk:
                        break
                    self._put_bytes(chunk)
                    sleep_seconds = min(0.02 / self.replaySpeed, 0.25)
                    if self._sleep_stoppable(sleep_seconds):
                        break
        except Exception as exc:
            self._set_status(lastError=str(exc))
            LOGGER.error("Replay reader failed: %s", exc)
        finally:
            self._set_status(active=False, replayFinished=True)
            LOGGER.info("Replay reader stopped")
