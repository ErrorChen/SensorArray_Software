from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any

from .config import DEFAULT_SERIAL_READ_SIZE

try:
    from serial.tools import list_ports
except Exception:  # pragma: no cover - pyserial may not be installed yet.
    list_ports = None

from .serial_reader import ReplayReaderThread, SerialReaderThread


class ConnectionManager:
    """Own serial/replay reader lifecycle for CLI startup and Dash controls."""

    def __init__(self, inputQueue: "queue.Queue[bytes]"):
        self._lock = threading.RLock()
        self.inputQueue = inputQueue
        self.reader: SerialReaderThread | ReplayReaderThread | None = None
        self._lastSerialConfig: dict[str, Any] = {}
        self._generation = 0
        self._status = {
            "mode": "disconnected",
            "serialPort": "",
            "baud": None,
            "serialConnected": False,
            "bytesReceived": 0,
            "chunksReceived": 0,
            "rawLinesReceived": 0,
            "droppedInputBytes": 0,
            "droppedInputChunks": 0,
            "readSize": DEFAULT_SERIAL_READ_SIZE,
            "lastDataTime": None,
            "lastError": "",
            "reconnectAttempts": 0,
            "autoReconnect": False,
            "dependencyMissing": "",
            "generation": 0,
        }

    def init(self, inputQueue: "queue.Queue[bytes]") -> None:
        with self._lock:
            self.inputQueue = inputQueue

    def listPorts(self) -> list[dict]:
        if list_ports is None:
            with self._lock:
                self._status["dependencyMissing"] = "pyserial is not installed"
                self._status["lastError"] = "pyserial is not installed; run pip install -r requirements.txt"
            return []

        ports = []
        for port in list_ports.comports():
            vid_pid = ""
            if getattr(port, "vid", None) is not None and getattr(port, "pid", None) is not None:
                vid_pid = f" - VID:PID {int(port.vid):04X}:{int(port.pid):04X}"
            description = getattr(port, "description", "") or getattr(port, "name", "") or "Serial Port"
            ports.append(
                {
                    "label": f"{port.device} - {description}{vid_pid}",
                    "value": port.device,
                    "device": port.device,
                    "description": description,
                }
            )
        with self._lock:
            self._status["dependencyMissing"] = ""
        return ports

    def connectSerial(
        self,
        port: str,
        baud: int,
        autoReconnect: bool,
        readSize: int = DEFAULT_SERIAL_READ_SIZE,
    ) -> None:
        port = (port or "").strip()
        if not port:
            raise ValueError("Serial port is required.")
        baud = int(baud)
        if baud <= 0:
            raise ValueError("Baudrate must be greater than 0.")
        read_size = max(4096, int(readSize or DEFAULT_SERIAL_READ_SIZE))

        with self._lock:
            self._stop_reader_locked()
            self._generation += 1
            self._lastSerialConfig = {
                "port": port,
                "baud": baud,
                "autoReconnect": bool(autoReconnect),
                "readSize": read_size,
            }
            self.reader = SerialReaderThread(
                port,
                baud,
                self.inputQueue,
                autoReconnect=bool(autoReconnect),
                readSize=read_size,
            )
            self.reader.start()
            self._status.update(
                {
                    "mode": "serial",
                    "serialPort": port,
                    "baud": baud,
                    "autoReconnect": bool(autoReconnect),
                    "readSize": read_size,
                    "lastError": "",
                    "generation": self._generation,
                }
            )

    def disconnect(self) -> None:
        with self._lock:
            self._stop_reader_locked()
            self._status.update(
                {
                    "mode": "disconnected",
                    "serialConnected": False,
                    "lastError": "",
                }
            )

    def reconnect(self) -> None:
        with self._lock:
            config = dict(self._lastSerialConfig)
        if not config:
            with self._lock:
                self._status["lastError"] = "No previous serial connection to reconnect."
            raise RuntimeError("No previous serial connection to reconnect.")
        self.connectSerial(
            config["port"],
            config["baud"],
            config.get("autoReconnect", False),
            config.get("readSize", DEFAULT_SERIAL_READ_SIZE),
        )

    def startReplay(
        self,
        replayFile: str,
        replaySpeed: float,
        readSize: int = DEFAULT_SERIAL_READ_SIZE,
    ) -> None:
        replay_path = Path(replayFile)
        if not replay_path.exists():
            raise FileNotFoundError(f"Replay file does not exist: {replay_path}")
        speed = float(replaySpeed)
        if speed <= 0:
            raise ValueError("Replay speed must be greater than 0.")
        read_size = max(4096, int(readSize or DEFAULT_SERIAL_READ_SIZE))

        with self._lock:
            self._stop_reader_locked()
            self._generation += 1
            self.reader = ReplayReaderThread(replay_path, speed, self.inputQueue, chunkSize=read_size)
            self.reader.start()
            self._status.update(
                {
                    "mode": "replay",
                    "serialPort": "",
                    "baud": None,
                    "serialConnected": False,
                    "replayFile": str(replay_path),
                    "replaySpeed": speed,
                    "readSize": read_size,
                    "lastError": "",
                    "generation": self._generation,
                }
            )

    def getStatus(self) -> dict:
        with self._lock:
            status = dict(self._status)
            reader = self.reader
        if reader is not None:
            status.update(reader.getStatus())
        return status

    def stop(self) -> None:
        self.disconnect()

    def _stop_reader_locked(self) -> None:
        reader = self.reader
        self.reader = None
        if reader is None:
            return
        reader.stop()
        reader.join(timeout=2.0)
