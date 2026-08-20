from __future__ import annotations

import queue
from dataclasses import asdict
from pathlib import Path

from sensorarray_app.constants import DEFAULT_SERIAL_BAUD
from sensorarray_app.domain.models import TransportEnvelope, TransportStateEvent
from sensorarray_app.transport.ble_transport import BleTransport
from sensorarray_app.transport.replay_transport import ReplayTransport
from sensorarray_app.transport.serial_transport import SerialTransport
from sensorarray_app.transport.base import TransportNotSent
from sensorarray_app.transport.session import SessionManager
from sensorarray_app.transport.wifi_udp_transport import WifiUdpTransport


class TransportManager:
    def __init__(self, output_queue: "queue.Queue[TransportEnvelope | TransportStateEvent]"):
        self.outputQueue = output_queue
        self.sessions = SessionManager()
        self.current = None
        self.status = {
            "transport": "none",
            "state": "DISCONNECTED",
            "device": "",
            "sessionGeneration": 0,
            "connectionGeneration": 0,
            "reconnectAttempt": 0,
            "reconnectBackoff": 0.0,
            "deviceIdentity": None,
            "error": "",
        }

    def disconnect(self) -> None:
        current = self.current
        if current is not None:
            # stop() is required to confirm its worker/client has exited.  A
            # failure is surfaced and a replacement transport is not created.
            current.stop()
        self.current = None
        generation = self.sessions.next_generation()
        self.status.update(
            {
                "transport": "none",
                "state": "DISCONNECTED",
                "device": "",
                "sessionGeneration": generation,
                "connectionGeneration": 0,
                "deviceIdentity": None,
                "queueCounters": {},
                "priorityBacklog": 0,
                "notificationCounts": {},
                "notificationBytes": {},
                "hostRawQueueOverflow": 0,
                "message": "",
                "error": "",
            }
        )

    def connect_serial(self, port: str, baud: int = DEFAULT_SERIAL_BAUD, auto_reconnect: bool = True) -> int:
        if not str(port or "").strip():
            raise ValueError("serial port is required")
        self.disconnect()
        generation = self.sessions.generation
        self.current = SerialTransport(self.outputQueue, generation, port, baud, auto_reconnect=auto_reconnect)
        self.current.start()
        self.status.update(
            {
                "transport": "serial",
                "state": "CONNECTING",
                "device": port,
                "sessionGeneration": generation,
                "connectionGeneration": 0,
                "message": "",
                "error": "",
            }
        )
        return generation

    def connect_replay(self, path: str | Path, speed: float = 1.0) -> int:
        self.disconnect()
        generation = self.sessions.generation
        self.current = ReplayTransport(self.outputQueue, generation, path, speed=speed)
        self.current.start()
        self.status.update(
            {
                "transport": "replay",
                "state": "STREAMING",
                "device": str(path),
                "sessionGeneration": generation,
                "message": "",
                "error": "",
            }
        )
        return generation

    def connect_ble(
        self,
        address: str,
        device_id: str = "",
        *,
        ble_device=None,
        auto_reconnect: bool = True,
    ) -> int:
        self.disconnect()
        generation = self.sessions.generation
        self.current = BleTransport(
            self.outputQueue,
            generation,
            address,
            device_id=device_id,
            ble_device=ble_device,
            auto_reconnect=auto_reconnect,
        )
        self.current.start()
        self.status.update(
            {
                "transport": "ble",
                "state": "CONNECTING",
                "device": address,
                "sessionGeneration": generation,
                "connectionGeneration": 0,
                "deviceIdentity": {
                    "transport": "ble",
                    "address": str(address),
                    "deviceId": str(device_id),
                },
                "message": "",
                "error": "",
            }
        )
        return generation

    def connect_wifi(self, host: str) -> int:
        self.disconnect()
        generation = self.sessions.generation
        self.current = WifiUdpTransport(self.outputQueue, generation, host)
        self.current.start()
        self.status.update(
            {
                "transport": "wifi",
                "state": "STREAMING",
                "device": host,
                "sessionGeneration": generation,
                "message": "",
                "error": "",
            }
        )
        return generation

    def send_command(self, command: str) -> None:
        if self.current is None:
            raise TransportNotSent("no transport connected; command was not sent")
        self.current.send_command(command)

    def request_expected_restart_reconnect(self) -> bool:
        current = self.current
        if not isinstance(current, SerialTransport):
            return False
        current.request_reconnect("firmware acknowledged expected RESTART")
        return True

    def write(self, data: bytes) -> dict[str, int | str | bool]:
        if self.current is None:
            raise RuntimeError("not connected")
        transport = str(self.status.get("transport") or getattr(self.current, "source", "unknown"))
        if transport in {"none", ""}:
            raise RuntimeError("not connected")
        if not hasattr(self.current, "write"):
            raise NotImplementedError(f"{transport} transport does not support write")
        bytes_written = int(self.current.write(bytes(data)))
        return {"ok": True, "transport": transport, "bytesWritten": bytes_written}

    def apply_state_event(self, event: TransportStateEvent) -> None:
        if event.sessionGeneration != self.status.get("sessionGeneration"):
            return
        # State events carry both informational messages (for example the
        # replay path in STREAMING, or a serial port in CONNECTED) and actual
        # failures.  Keeping those concepts separate prevents a healthy
        # connection from rendering its device/path as a red GUI error.
        normalizedState = str(event.state).upper()
        errorMessage = event.message if normalizedState in {"ERROR", "FAILED"} else ""
        self.status.update(
            {
                "transport": event.source,
                "state": event.state,
                "message": event.message,
                "error": errorMessage,
                "connectionGeneration": int(event.metadata.get("connectionGeneration", 0) or 0),
                "reconnectAttempt": int(event.metadata.get("reconnectAttempt", 0) or 0),
                "reconnectBackoff": float(event.metadata.get("reconnectBackoff", 0.0) or 0.0),
                "deviceIdentity": event.metadata.get("identity") or self.status.get("deviceIdentity"),
                "queueCounters": event.metadata.get("queueCounters", self.status.get("queueCounters", {})),
                "priorityBacklog": int(event.metadata.get("priorityBacklog", self.status.get("priorityBacklog", 0)) or 0),
                "notificationCounts": event.metadata.get("notificationCounts", self.status.get("notificationCounts", {})),
                "notificationBytes": event.metadata.get("notificationBytes", self.status.get("notificationBytes", {})),
                "hostRawQueueOverflow": int(event.metadata.get("hostRawQueueOverflow", self.status.get("hostRawQueueOverflow", 0)) or 0),
            }
        )

    def diagnostics(self) -> dict:
        current = self.current
        if isinstance(current, BleTransport):
            fragment_stats = getattr(getattr(current, "_fragmenter", None), "stats", None)
            return {
                "source": "ble",
                "queueCounters": dict(current.queueCounters),
                "priorityBacklog": current.priorityBacklog,
                "notificationCounts": dict(current._notify_counts),
                "notificationBytes": dict(current._notify_bytes),
                "fragment": asdict(fragment_stats) if fragment_stats is not None else {},
            }
        if isinstance(current, SerialTransport):
            return {
                "source": "serial",
                "rawQueueOverflow": int(current.rawQueueOverflow),
                "lifecycleDrops": int(current.lifecycleDrops),
                "bytesReceived": int(current.bytesReceived),
                "packetsReceived": int(current.packetsReceived),
            }
        return {"source": str(self.status.get("transport", "none"))}
