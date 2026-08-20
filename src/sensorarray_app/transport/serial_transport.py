from __future__ import annotations

import queue
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from sensorarray_app.constants import DEFAULT_SERIAL_BAUD
from sensorarray_app.domain.models import TransportEnvelope, TransportStateEvent
from sensorarray_app.transport.base import TransportNotSent, TransportShutdownTimeout, TransportWriteOutcomeUnknown

try:
    import serial
    from serial.tools import list_ports
except Exception:  # pragma: no cover
    serial = None
    list_ports = None


@dataclass(frozen=True)
class SerialDeviceIdentity:
    device: str
    name: str = ""
    description: str = ""
    hwid: str = ""
    vid: int | None = None
    pid: int | None = None
    serialNumber: str = ""
    location: str = ""
    manufacturer: str = ""
    product: str = ""


class SerialTransport:
    source = "serial"

    def __init__(
        self,
        output_queue: "queue.Queue[TransportEnvelope | TransportStateEvent]",
        session_generation: int,
        port: str,
        baud: int = DEFAULT_SERIAL_BAUD,
        read_size: int = 4096,
        auto_reconnect: bool = True,
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
        self._reconnect_requested = threading.Event()
        self._reconnect_reason = "expected device restart"
        self.bytesReceived = 0
        self.packetsReceived = 0
        self.lastError = ""
        self.connectionGeneration = 0
        self.reconnectAttempt = 0
        self.reconnectBackoff = 0.0
        self.rawQueueOverflow = 0
        self.lifecycleDrops = 0
        self._overflow_pending = False
        self._identity = self._identity_for_device(self.port)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("serial transport is already running")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="SensorArraySerialTransport", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                raise TransportShutdownTimeout("serial worker did not stop within 5 seconds")
        self._thread = None

    def send_command(self, command: str) -> None:
        self.write((command.rstrip() + "\n").encode("ascii", errors="strict"))

    def request_reconnect(self, reason: str = "expected device restart") -> None:
        """Reacquire a CDC endpoint after firmware announced a restart.

        ESP USB Serial/JTAG can keep the device node and an apparently valid
        pyserial handle across ``esp_restart()`` while that old handle never
        delivers bytes from the new boot.  Wake the read loop and deliberately
        reopen the stable physical identity without creating a new Host
        session.
        """

        self._reconnect_reason = str(reason or "expected device restart")
        self._reconnect_requested.set()

    def write(self, data: bytes) -> int:
        handle = self._handle
        if handle is None:
            raise TransportNotSent("serial is not connected; command was not sent")
        payload = bytes(data)
        try:
            with self._write_lock:
                written = handle.write(payload)
        except Exception as exc:
            raise TransportWriteOutcomeUnknown("serial write failed after submission; firmware outcome is unknown") from exc
        count = int(written if written is not None else len(payload))
        if count != len(payload):
            raise TransportWriteOutcomeUnknown(f"serial accepted {count}/{len(payload)} bytes; firmware outcome is unknown")
        return count

    @staticmethod
    def list_ports() -> list[dict[str, Any]]:
        if list_ports is None:
            raise RuntimeError("pyserial is not installed or serial.tools.list_ports is unavailable")
        ports: list[dict[str, str]] = []
        for item in list_ports.comports():
            identity = _identity_from_port(item)
            payload = asdict(identity)
            payload.update(
                {
                    "serial_number": identity.serialNumber,
                    "label": f"{identity.device} - {identity.description}" if identity.description else identity.device,
                    "value": identity.device,
                }
            )
            ports.append(payload)
        return ports

    def _run(self) -> None:
        self._put_state("CONNECTING", f"opening {self.port} at {self.baud}")
        while not self._stop.is_set():
            try:
                if serial is None:
                    raise RuntimeError("pyserial is not installed")
                self._handle = self._open_handle_without_reset()
                self.connectionGeneration += 1
                self.reconnectAttempt = 0
                self.reconnectBackoff = 0.0
                self._put_state("CONNECTED", f"connected {self.port}")
                self._put_state("STREAMING", self.port)
                while not self._stop.is_set():
                    self._raise_if_reconnect_requested()
                    data = self._handle.read(self.readSize)
                    self._raise_if_reconnect_requested()
                    if not data:
                        continue
                    self.bytesReceived += len(data)
                    self.packetsReceived += 1
                    self._put_envelope(data)
            except _ExpectedSerialReconnect as exc:
                if self._stop.is_set():
                    break
                self._close()
                self._put_state("CONNECTION_RESET", "discarding partial serial frame state")
                self.reconnectAttempt += 1
                replacement = self._resolve_reconnect_port()
                if replacement is None:
                    self._put_state("RECONNECT_AMBIGUOUS", "physical serial device could not be uniquely resolved")
                elif replacement != self.port:
                    old_port = self.port
                    self.port = replacement
                    self._put_state("REENUMERATED", f"{old_port} -> {self.port}")
                self._put_state("RECONNECTING", str(exc))
                self.reconnectBackoff = 0.5
                self._stop.wait(self.reconnectBackoff)
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.lastError = str(exc)
                self._put_state("ERROR", str(exc))
                self._close()
                if not self.autoReconnect:
                    break
                self._put_state("CONNECTION_RESET", "discarding partial serial frame state")
                self.reconnectAttempt += 1
                replacement = self._resolve_reconnect_port()
                if replacement is None:
                    self._put_state("RECONNECT_AMBIGUOUS", "physical serial device could not be uniquely resolved")
                elif replacement != self.port:
                    old_port = self.port
                    self.port = replacement
                    self._put_state("REENUMERATED", f"{old_port} -> {self.port}")
                self._put_state("RECONNECTING", str(exc))
                self.reconnectBackoff = min(5.0, max(0.5, float(self.reconnectAttempt)))
                self._stop.wait(self.reconnectBackoff)
        self._close()
        self._put_state("DISCONNECTED", "")

    def _raise_if_reconnect_requested(self) -> None:
        if self._reconnect_requested.is_set():
            self._reconnect_requested.clear()
            raise _ExpectedSerialReconnect(self._reconnect_reason)

    def _open_handle_without_reset(self) -> Any:
        """Open the CDC port without pulsing the board's reset control lines.

        Passing ``port`` to ``serial.Serial`` opens immediately with pyserial's
        default DTR/RTS state.  On the ESP USB Serial/JTAG endpoint that state
        transition can reset a board merely because the Host starts, stops, or
        reconnects.  Configure both inactive while the handle is closed, just
        like the authoritative firmware HIL tooling, and only then open it.
        """

        if serial is None:  # pragma: no cover - guarded by the caller
            raise RuntimeError("pyserial is not installed")
        handle = serial.Serial()
        handle.port = self.port
        handle.baudrate = self.baud
        handle.timeout = 0.05
        handle.write_timeout = 1.0
        handle.dtr = False
        handle.rts = False
        handle.open()
        return handle

    def _put_envelope(self, data: bytes) -> None:
        if self._overflow_pending:
            if not self._put_state("SERIAL_RX_OVERFLOW", f"raw chunks dropped={self.rawQueueOverflow}"):
                return
            self._overflow_pending = False
        envelope = TransportEnvelope(
            source="serial",
            channel="data",
            deviceId=self._stable_device_id(),
            sessionGeneration=self.sessionGeneration,
            receivedMonotonicNs=time.monotonic_ns(),
            receivedWallTime=time.time(),
            rawPayload=bytes(data),
            connectionGeneration=self.connectionGeneration,
        )
        try:
            self.outputQueue.put(envelope, timeout=0.05)
        except queue.Full:
            # A raw byte stream cannot be repaired by evicting an arbitrary
            # older chunk. Drop this complete chunk, flag the discontinuity,
            # and make the parser reset before accepting the next bytes.
            self.rawQueueOverflow += 1
            self._overflow_pending = True

    def _put_state(self, state: str, message: str) -> bool:
        event = TransportStateEvent(
            "serial",
            state,
            self.sessionGeneration,
            message,
            {
                "port": self.port,
                "baud": self.baud,
                "connectionGeneration": self.connectionGeneration,
                "reconnectAttempt": self.reconnectAttempt,
                "reconnectBackoff": self.reconnectBackoff,
                "identity": asdict(self._identity) if self._identity else None,
                "hostRawQueueOverflow": self.rawQueueOverflow,
            },
        )
        try:
            self.outputQueue.put(event, timeout=0.2)
            return True
        except queue.Full:
            self.lifecycleDrops += 1
            return False

    def _identity_for_device(self, device: str) -> SerialDeviceIdentity | None:
        if list_ports is None:
            return None
        try:
            matches = [_identity_from_port(item) for item in list_ports.comports() if str(item.device) == device]
        except Exception:
            return None
        return matches[0] if len(matches) == 1 else None

    def _resolve_reconnect_port(self) -> str | None:
        if list_ports is None:
            return self.port
        try:
            candidates = [_identity_from_port(item) for item in list_ports.comports()]
        except Exception:
            return None
        identity = self._identity
        if identity is None:
            same_path = [candidate for candidate in candidates if candidate.device == self.port]
            return same_path[0].device if len(same_path) == 1 else None
        if identity.serialNumber and identity.vid is not None and identity.pid is not None:
            matches = [
                item
                for item in candidates
                if item.serialNumber == identity.serialNumber and item.vid == identity.vid and item.pid == identity.pid
            ]
            if len(matches) == 1:
                return matches[0].device
            if len(matches) > 1:
                return None
        stable_matches = [
            item
            for item in candidates
            if (
                identity.location
                and item.location == identity.location
                and (identity.vid is None or item.vid == identity.vid)
                and (identity.pid is None or item.pid == identity.pid)
            )
            or (
                identity.hwid
                and item.hwid == identity.hwid
                and (identity.vid is None or item.vid == identity.vid)
                and (identity.pid is None or item.pid == identity.pid)
            )
        ]
        if len(stable_matches) == 1:
            return stable_matches[0].device
        if identity.vid is None or identity.pid is None:
            return None
        vid_pid = [item for item in candidates if item.vid == identity.vid and item.pid == identity.pid]
        return vid_pid[0].device if len(vid_pid) == 1 else None

    def _stable_device_id(self) -> str:
        identity = self._identity
        if identity is None:
            return self.port
        if identity.serialNumber:
            return f"serial:{identity.vid or 0:04X}:{identity.pid or 0:04X}:{identity.serialNumber}"
        return f"serial:{identity.location or identity.hwid or identity.device}"

    def _close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


def _identity_from_port(item: Any) -> SerialDeviceIdentity:
    return SerialDeviceIdentity(
        device=str(getattr(item, "device", "")),
        name=str(getattr(item, "name", "") or getattr(item, "device", "")),
        description=str(getattr(item, "description", "") or ""),
        hwid=str(getattr(item, "hwid", "") or ""),
        vid=getattr(item, "vid", None),
        pid=getattr(item, "pid", None),
        serialNumber=str(getattr(item, "serial_number", "") or ""),
        location=str(getattr(item, "location", "") or ""),
        manufacturer=str(getattr(item, "manufacturer", "") or ""),
        product=str(getattr(item, "product", "") or ""),
    )


class _ExpectedSerialReconnect(RuntimeError):
    """Internal control flow for an expected same-session CDC reacquire."""
