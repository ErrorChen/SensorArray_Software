from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from sensorarray_app.domain.models import (
    BootInfo,
    BuildInfo,
    CalibrationInfo,
    FdcIsolationInfo,
    LogRecord,
    ProtocolInfo,
    ReadyInfo,
    UsbStreamInfo,
)
from sensorarray_app.transport.base import TransportWriteOutcomeUnknown


@dataclass
class BootstrapStatus:
    state: str = "IDLE"
    phase: str = ""
    source: str = "none"
    sessionGeneration: int = 0
    connectionGeneration: int = 0
    readyPolls: int = 0
    error: str = ""
    commandsSent: list[str] = field(default_factory=list)
    startedMonotonic: float = 0.0
    completedMonotonic: float | None = None


class DeviceSynchronizer:
    """Event-driven 8045 bootstrap and reconnect resynchronisation.

    Exactly one query is outstanding at a time.  This keeps first attach and
    reconnect deterministic and prevents a burst of configuration commands
    while firmware is still reporting READY=0.
    """

    _RESPONSE_TAGS = {
        "STATE?": {"MODE"},
        "ROWS?": {"ROWS"},
        "ROWMODES?": {"ROWMODES"},
        "BAT?": {"ABAT", "BATD", "BATERR"},
        "RAIL?": {"ARL", "RAIL"},
        "ST=AUTO": {"ACK"},
        "BTX?": {"ACK"},
    }

    def __init__(
        self,
        sender: Callable[[str], None],
        *,
        ready_poll_interval: float = 0.5,
        maximum_ready_polls: int = 120,
        query_timeout: float = 2.0,
        maximum_query_retries: int = 2,
        on_complete: Callable[[BootstrapStatus], None] | None = None,
    ):
        self.sender = sender
        self.readyPollInterval = max(0.05, float(ready_poll_interval))
        self.maximumReadyPolls = max(1, int(maximum_ready_polls))
        self.queryTimeout = max(0.2, float(query_timeout))
        self.maximumQueryRetries = max(0, int(maximum_query_retries))
        self.onComplete = on_complete
        self.status = BootstrapStatus()
        self.protocol: ProtocolInfo | None = None
        self.build: BuildInfo | None = None
        self.boot: BootInfo | None = None
        self.ready: ReadyInfo | None = None
        self.calibration: CalibrationInfo | None = None
        self.fdcIsolation: FdcIsolationInfo | None = None
        self.usbStream: UsbStreamInfo | None = None
        self._outstanding = ""
        self._nextReadyPoll = 0.0
        self._queryDeadline = 0.0
        self._queryRetries = 0

    def start(self, source: str, session_generation: int, connection_generation: int) -> None:
        self.status = BootstrapStatus(
            state="BOOTSTRAPPING",
            phase="PROTO",
            source=str(source),
            sessionGeneration=int(session_generation),
            connectionGeneration=int(connection_generation),
            startedMonotonic=time.monotonic(),
        )
        self.protocol = None
        self.build = None
        self.boot = None
        self.ready = None
        self.calibration = None
        self.fdcIsolation = None
        self.usbStream = None
        self._outstanding = ""
        self._queryDeadline = 0.0
        self._queryRetries = 0
        self._send("PROTO?")

    def stop(self) -> None:
        self._outstanding = ""
        if self.status.state == "BOOTSTRAPPING":
            self.status.state = "INTERRUPTED"

    def handle(self, event: Any) -> None:
        if self.status.state != "BOOTSTRAPPING":
            return
        if isinstance(event, ProtocolInfo) and self._outstanding == "PROTO?":
            self.protocol = event
            if not event.compatible:
                self._fail(f"INCOMPATIBLE_FIRMWARE: {event.incompatibility}", state="INCOMPATIBLE_FIRMWARE")
                return
            self._send("BUILD?")
            return
        if isinstance(event, BuildInfo) and self._outstanding == "BUILD?":
            self.build = event
            self._send("BOOT?")
            return
        if isinstance(event, BootInfo) and self._outstanding == "BOOT?":
            self.boot = event
            self._send("READY?")
            return
        if isinstance(event, ReadyInfo) and self._outstanding == "READY?":
            self.ready = event
            if event.ready:
                self._send("STATE?")
            else:
                self.status.phase = "WAITING_READY"
                self.status.readyPolls += 1
                self._outstanding = ""
                self._nextReadyPoll = time.monotonic() + self.readyPollInterval
            return
        if isinstance(event, CalibrationInfo) and self._outstanding == "CAL?":
            self.calibration = event
            if self.status.source in {"serial", "ble", "wifi"}:
                # Sink selection is volatile firmware state. A prior client or
                # interrupted HIL may have left ST=SER/BLE/WIFI; select AUTO in
                # the same ordered bootstrap so the transport just connected
                # is eligible without issuing an uncorrelated command burst.
                self._send("ST=AUTO")
            else:
                self._complete()
            return
        if isinstance(event, FdcIsolationInfo) and self._outstanding == "FDCISO?":
            self.fdcIsolation = event
            self._send("BAT?")
            return
        if isinstance(event, UsbStreamInfo) and self._outstanding == "USBSTREAM?":
            self.usbStream = event
            self._complete()
            return
        if isinstance(event, LogRecord):
            self._handle_log(event)

    def tick(self, now: float | None = None) -> None:
        if self.status.state != "BOOTSTRAPPING":
            return
        current = time.monotonic() if now is None else float(now)
        if self._outstanding and current >= self._queryDeadline:
            command = self._outstanding
            if self._queryRetries >= self.maximumQueryRetries:
                self._fail(f"bootstrap {command} timed out after bounded retries")
                return
            self._queryRetries += 1
            self._outstanding = ""
            self._send(command, retry=True)
            return
        if self.status.phase != "WAITING_READY":
            return
        if current < self._nextReadyPoll:
            return
        if self.status.readyPolls >= self.maximumReadyPolls:
            self._fail("READY remained false after bounded polling")
            return
        self._send("READY?")

    def snapshot(self) -> dict[str, Any]:
        return {
            **asdict(self.status),
            "protocol": asdict(self.protocol) if self.protocol else None,
            "build": asdict(self.build) if self.build else None,
            "boot": asdict(self.boot) if self.boot else None,
            "ready": asdict(self.ready) if self.ready else None,
            "calibration": asdict(self.calibration) if self.calibration else None,
            "fdcIsolation": asdict(self.fdcIsolation) if self.fdcIsolation else None,
            "usbStream": asdict(self.usbStream) if self.usbStream else None,
        }

    def _handle_log(self, record: LogRecord) -> None:
        tags = self._RESPONSE_TAGS.get(self._outstanding)
        if not tags or record.tag not in tags:
            return
        if self._outstanding == "BTX?" and record.parsedFields.get("cmd", "").upper() != "BTX":
            return
        if self._outstanding == "ST=AUTO":
            if (
                record.parsedFields.get("cmd", "").upper() != "ST"
                or record.parsedFields.get("v", "").lower() != "auto"
            ):
                return
            if self.status.source == "serial":
                self._send("USBSTREAM?")
            elif self.status.source == "ble":
                self._send("BTX?")
            else:
                self._complete()
            return
        next_query = {
            "STATE?": "ROWS?",
            "ROWS?": "ROWMODES?",
            "ROWMODES?": "FDCISO?",
            "BAT?": "RAIL?",
            "RAIL?": "CAL?",
        }.get(self._outstanding)
        if next_query:
            self._send(next_query)
        elif self._outstanding == "BTX?":
            self._complete()

    def _send(self, command: str, *, retry: bool = False) -> None:
        if not retry:
            self._queryRetries = 0
        try:
            self.sender(command)
        except TransportWriteOutcomeUnknown:
            # A bootstrap query is idempotent.  Wait for the possibly-delivered
            # response before the bounded retry timer is allowed to fire.
            self._outstanding = command
            self.status.phase = f"{command.removesuffix('?')}_OUTCOME_UNKNOWN"
            self._queryDeadline = time.monotonic() + self.queryTimeout
            self.status.commandsSent.append(command)
            return
        except Exception as exc:
            self._fail(f"bootstrap {command} failed: {exc}")
            return
        self._outstanding = command
        self.status.phase = command.removesuffix("?")
        self._queryDeadline = time.monotonic() + self.queryTimeout
        self.status.commandsSent.append(command)

    def _complete(self) -> None:
        self._outstanding = ""
        self._queryDeadline = 0.0
        self.status.state = "SYNCED"
        self.status.phase = "COMPLETE"
        self.status.completedMonotonic = time.monotonic()
        if self.onComplete is not None:
            self.onComplete(self.status)

    def _fail(self, message: str, *, state: str = "FAILED") -> None:
        self._outstanding = ""
        self._queryDeadline = 0.0
        self.status.state = state
        self.status.error = str(message)
        self.status.completedMonotonic = time.monotonic()


__all__ = ["BootstrapStatus", "DeviceSynchronizer"]
