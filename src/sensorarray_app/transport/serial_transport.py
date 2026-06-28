from __future__ import annotations

import queue
import threading
import time
from typing import Any

from sensorarray_app.constants import DEFAULT_SERIAL_BAUD
from sensorarray_app.domain.models import TransportEnvelope, TransportStateEvent

try:
    import serial
    from serial.tools import list_ports
except Exception:  # pragma: no cover
    serial = None
    list_ports = None


class SerialTransport:
    source = "serial"

    def __init__(
        self,
        output_queue: "queue.Queue[TransportEnvelope | TransportStateEvent]",
        session_generation: int,
        port: str,
        baud: int = DEFAULT_SERIAL_BAUD,
        read_size: int = 4096,
        auto_reconnect: bool = False,
    ):
        self.outputQueue = output_queue
        self.sessionGeneration = int(session_generation)
        if not str(port or "").strip():
            raise ValueError("serial port is required")
        self.port = str(port).strip()
        self.baud = int(baud or DEFAULT_SERIAL_BAUD)
        self.readSize = max(256, int(read_size))
        self.autoReconnect = bool(auto_reconnect)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle: Any | None = None
        self._write_lock = threading.Lock()
        self.bytesReceived = 0
        self.packetsReceived = 0
        self.lastError = ""

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="SensorArraySerialTransport", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def send_command(self, command: str) -> None:
        self.write((command.rstrip() + "\n").encode("ascii", errors="strict"))

    def write(self, data: bytes) -> int:
        handle = self._handle
        if handle is None:
            raise RuntimeError("serial is not connected")
        payload = bytes(data)
        with self._write_lock:
            written = handle.write(payload)
        return int(written if written is not None else len(payload))

    @staticmethod
    def list_ports() -> list[dict[str, str]]:
        if list_ports is None:
            raise RuntimeError("pyserial is not installed or serial.tools.list_ports is unavailable")
        ports: list[dict[str, str]] = []
        for item in list_ports.comports():
            ports.append(
                {
                    "device": item.device,
                    "name": item.name or item.device,
                    "description": item.description or "",
                    "hwid": item.hwid or "",
                    "label": f"{item.device} - {item.description}" if item.description else item.device,
                    "value": item.device,
                }
            )
        return ports

    def _run(self) -> None:
        self._put_state("CONNECTING", f"opening {self.port} at {self.baud}")
        while not self._stop.is_set():
            try:
                if serial is None:
                    raise RuntimeError("pyserial is not installed")
                self._handle = serial.Serial(self.port, self.baud, timeout=0.05)
                self._put_state("CONNECTED", f"connected {self.port}")
                while not self._stop.is_set():
                    data = self._handle.read(self.readSize)
                    if not data:
                        continue
                    self.bytesReceived += len(data)
                    self.packetsReceived += 1
                    self._put_envelope(data)
            except Exception as exc:
                self.lastError = str(exc)
                self._put_state("ERROR", str(exc))
                self._close()
                if not self.autoReconnect:
                    break
                self._put_state("RECONNECTING", str(exc))
                time.sleep(1.0)
        self._close()
        self._put_state("DISCONNECTED", "")

    def _put_envelope(self, data: bytes) -> None:
        envelope = TransportEnvelope(
            source="serial",
            channel="data",
            deviceId=self.port,
            sessionGeneration=self.sessionGeneration,
            receivedMonotonicNs=time.monotonic_ns(),
            receivedWallTime=time.time(),
            rawPayload=bytes(data),
        )
        try:
            self.outputQueue.put_nowait(envelope)
        except queue.Full:
            try:
                self.outputQueue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.outputQueue.put_nowait(envelope)
            except queue.Full:
                pass

    def _put_state(self, state: str, message: str) -> None:
        event = TransportStateEvent("serial", state, self.sessionGeneration, message, {"port": self.port, "baud": self.baud})
        try:
            self.outputQueue.put_nowait(event)
        except queue.Full:
            pass

    def _close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
