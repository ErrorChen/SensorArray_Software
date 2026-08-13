from __future__ import annotations

import itertools
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from sensorarray_app.domain.models import CommandAccepted, CommandApplied, CommandTransactionEvent


@dataclass
class CommandRecord:
    localId: int
    commandType: str
    command: str
    requestedValue: Any
    state: str
    sentTime: float
    firmwareId: int | None = None
    acceptedTime: float | None = None
    appliedTime: float | None = None
    appliedValue: Any = None
    generation: int | None = None
    frameSeq: int | None = None
    error: str = ""
    message: str = ""
    rawFields: dict[str, str] = field(default_factory=dict)

    @property
    def requestedRows(self) -> int | None:
        return int(self.requestedValue) if self.commandType == "rows" and self.requestedValue is not None else None


class CommandService:
    """Correlates accepted/applied firmware transactions by request ID.

    ROWS compatibility fields remain available, while mode, external rail,
    ADSCHK and battery actions share one transaction record format. In
    particular, an accepted MACK never changes ``appliedMode``.
    """

    def __init__(self):
        self._ids = itertools.count(1)
        self._commands: dict[int, CommandRecord] = {}
        self.requestedRows: int | None = None
        self.activeRows = 8
        self.pendingRows: int | None = None
        self.rowsRequestId: int | None = None
        self.appliedMode = "CAP"
        self.pendingMode: str | None = None
        self.transitionState = "applied"
        self.modeRequestId: int | None = None
        self.modeGeneration: int | None = None
        self.modeFrameSeq: int | None = None
        self.modeError = ""
        self.modePendingSince: float | None = None
        self.appliedRowModes: tuple[str, ...] = ("CAP",) * 8
        self.pendingRowModes: tuple[str, ...] | None = None
        self.rowModeRequestId: int | None = None
        self.rowModeGeneration: int | None = None
        self.rowModeFrameSeq: int | None = None
        self.rowModeTransitionState = "applied"
        self.rowModeError = ""
        self.rowModePendingSince: float | None = None
        self.deviceState = "CAPACITANCE"
        self.railConfigured = False
        self.railState = "unconfigured"
        self.railRequestId: int | None = None
        self.measuredAvddV: float | None = None
        self.measuredAvssV: float | None = None
        self.desiredModeAfterRail: str | None = None
        self.adsDiagnostics: dict[str, Any] = {
            "state": "idle",
            "requestId": None,
            "identity": {},
            "check": {},
            "statistics": {},
            "error": "",
        }
        self.sessionGeneration = 0

    def reset_session(self, session_generation: int) -> None:
        self.sessionGeneration = int(session_generation)
        # A transport generation identifies a new device session.  Firmware
        # boots in CAP, so carrying an applied VOLT/RES state or its generation
        # gate into the next connection would let stale UI state disagree with
        # the newly connected device.
        self.appliedMode = "CAP"
        self._commands.clear()
        self.requestedRows = None
        self.activeRows = 8
        self.pendingRows = None
        self.pendingMode = None
        self.transitionState = "applied"
        self.modeRequestId = None
        self.modeGeneration = None
        self.modeFrameSeq = None
        self.modeError = ""
        self.modePendingSince = None
        self.appliedRowModes = ("CAP",) * 8
        self.pendingRowModes = None
        self.rowModeRequestId = None
        self.rowModeGeneration = None
        self.rowModeFrameSeq = None
        self.rowModeTransitionState = "applied"
        self.rowModeError = ""
        self.rowModePendingSince = None
        self.deviceState = "CAPACITANCE"
        self.railConfigured = False
        self.railState = "unconfigured"
        self.railRequestId = None
        self.desiredModeAfterRail = None
        self.rowsRequestId = None
        self.adsDiagnostics = {
            "state": "idle",
            "requestId": None,
            "identity": {},
            "check": {},
            "statistics": {},
            "error": "",
        }

    def request_rows(self, rows: int, sender: Callable[[str], None]) -> CommandRecord:
        if not (1 <= int(rows) <= 8):
            raise ValueError("ROWS must be 1..8")
        record = self._new_record("rows", f"ROWS={int(rows)}", int(rows))
        self.requestedRows = int(rows)
        self.pendingRows = int(rows)
        self.rowsRequestId = None
        self._send(record, sender)
        return record

    def request_mode(self, mode: str, sender: Callable[[str], None]) -> CommandRecord:
        normalized = _normalize_mode(mode)
        if self.pendingMode is not None:
            raise RuntimeError("a measurement mode transaction is already pending")
        if self.pendingRowModes is not None:
            raise RuntimeError("a row-mode profile transaction is already pending")
        record = self._new_record("mode", f"MODE={normalized}", normalized)
        self.pendingMode = normalized
        self.transitionState = "requested"
        self.modeRequestId = None
        self.modeError = ""
        self.modePendingSince = time.time()
        self._send(record, sender)
        return record

    def request_row_modes(self, modes: Any, sender: Callable[[str], None]) -> CommandRecord:
        """Request one atomic eight-row measurement profile transaction.

        Draft editing belongs to the caller. This method emits exactly one
        ``ROWMODES=`` command and does not change ``appliedRowModes`` until a
        matching RMAPP is observed.
        """

        normalized = _normalize_row_modes(modes)
        if self.pendingRowModes is not None:
            raise RuntimeError("a row-mode profile transaction is already pending")
        if self.pendingMode is not None:
            raise RuntimeError("a measurement mode transaction is already pending")
        encoded = "".join({"CAP": "C", "VOLT": "V", "RES": "R"}[mode] for mode in normalized)
        record = self._new_record("row_modes", f"ROWMODES={encoded}", normalized)
        self.pendingRowModes = normalized
        self.rowModeRequestId = None
        self.rowModeTransitionState = "requested"
        self.rowModeError = ""
        self.rowModePendingSince = time.time()
        self._send(record, sender)
        return record

    def request_rail(
        self,
        avdd_uv: int,
        avss_uv: int,
        sender: Callable[[str], None],
        desired_mode: str | None = None,
    ) -> CommandRecord:
        if int(avdd_uv) <= 0 or int(avss_uv) >= 0:
            raise ValueError("RAILCFG requires positive AVDD and negative AVSS in integer microvolts")
        rail_span_uv = int(avdd_uv) - int(avss_uv)
        if not 3_500_000 <= rail_span_uv <= 6_000_000:
            raise ValueError("RAILCFG AVDD-AVSS span must be between 3.5 V and 6.0 V")
        requested = {"avddUv": int(avdd_uv), "avssUv": int(avss_uv)}
        record = self._new_record("rail", f"RAILCFG={int(avdd_uv)},{int(avss_uv)}", requested)
        self.railConfigured = False
        self.railState = "requested"
        self.railRequestId = None
        self.measuredAvddV = int(avdd_uv) / 1_000_000.0
        self.measuredAvssV = int(avss_uv) / 1_000_000.0
        self.desiredModeAfterRail = _normalize_mode(desired_mode) if desired_mode else None
        if self.desiredModeAfterRail:
            self.pendingMode = self.desiredModeAfterRail
            self.transitionState = "configuring_rail"
        self._send(record, sender)
        return record

    def record_action(self, command_type: str, command: str, requested_value: Any, sender: Callable[[str], None]) -> CommandRecord:
        record = self._new_record(command_type, command, requested_value)
        self._send(record, sender)
        return record

    def accept(self, event: CommandAccepted) -> None:
        generic = CommandTransactionEvent(
            commandType="rows",
            phase="accepted",
            requestId=event.commandId,
            state="accepted",
            oldValue=event.oldRows,
            requestedValue=event.requestedRows,
            appliedValue=None,
            generation=event.generation,
            frameSeq=None,
            error=None,
            rawFields={},
            sessionGeneration=event.sessionGeneration,
            rawText=event.rawText,
        )
        self.handle(generic)

    def apply(self, event: CommandApplied) -> None:
        generic = CommandTransactionEvent(
            commandType="rows",
            phase="applied",
            requestId=event.commandId,
            state="applied",
            oldValue=event.oldRows,
            requestedValue=event.newRows,
            appliedValue=event.newRows,
            generation=event.generation,
            frameSeq=event.seq,
            error=None,
            rawFields={},
            sessionGeneration=event.sessionGeneration,
            rawText=event.rawText,
        )
        self.handle(generic)

    def handle(self, event: CommandTransactionEvent) -> dict[str, Any]:
        if int(event.sessionGeneration) != self.sessionGeneration:
            return {"modeApplied": False, "railApplied": False, "ignoredOldSession": True}
        command_type = str(event.commandType).lower()
        phase = str(event.phase).lower()

        # MACK/MAPP and RMACK/RMAPP are correlated transactions, not general
        # state announcements.  In particular, ``None == None`` must never
        # allow a malformed event without a firmware request ID to accept or
        # commit a locally pending request.
        if (
            command_type in {"mode", "row_modes"}
            and phase in {"accepted", "applied"}
            and event.requestId is None
        ):
            return {
                "modeApplied": False,
                "rowModesApplied": False,
                "railApplied": False,
                "rejectedMissingRequestId": True,
            }
        if (
            command_type in {"mode", "row_modes"}
            and phase == "applied"
            and (
                event.generation is None
                or event.frameSeq is None
                or int(event.generation) < 0
                or int(event.frameSeq) < 0
            )
        ):
            return {
                "modeApplied": False,
                "rowModesApplied": False,
                "railApplied": False,
                "rejectedMissingBoundary": True,
            }

        # Validate the transaction identity and requested value before the
        # generic CommandRecord is mutated.  Otherwise an RMAPP carrying the
        # right ID but the wrong profile could make the audit record say
        # APPLIED even though the strict row-profile state machine rejected it.
        if command_type == "mode" and phase in {"accepted", "applied", "failed", "rejected", "error"}:
            event_mode_value = event.appliedValue or event.requestedValue
            if event_mode_value is not None:
                try:
                    event_mode = _normalize_mode(str(event_mode_value))
                except ValueError:
                    return _rejected_transaction_result("rejectedInvalidValue")
                if self.pendingMode is not None and event_mode != self.pendingMode:
                    return _rejected_transaction_result("rejectedMismatchedValue")
            if self.modeRequestId is not None and self.pendingMode is not None and event.requestId != self.modeRequestId:
                return _rejected_transaction_result("rejectedMismatchedRequestId")
        if command_type == "row_modes" and phase in {"accepted", "applied", "failed", "rejected", "error"}:
            event_profile_value = event.appliedValue or event.requestedValue
            if event_profile_value is not None:
                try:
                    event_profile = _normalize_row_modes(event_profile_value)
                except ValueError:
                    return _rejected_transaction_result("rejectedInvalidValue")
                if self.pendingRowModes is not None and event_profile != self.pendingRowModes:
                    return _rejected_transaction_result("rejectedMismatchedValue")
            if (
                self.rowModeRequestId is not None
                and self.pendingRowModes is not None
                and event.requestId != self.rowModeRequestId
            ):
                return _rejected_transaction_result("rejectedMismatchedRequestId")
        record = self._match_record(command_type, event)
        now = time.time()
        if record is not None:
            record.firmwareId = event.requestId if event.requestId is not None else record.firmwareId
            record.rawFields = dict(event.rawFields)
            record.message = event.rawText
            record.generation = event.generation
            record.frameSeq = event.frameSeq
            if phase in {"accepted", "checking"}:
                record.state = "ACCEPTED"
                record.acceptedTime = now
            elif phase in {"applied", "complete", "completed"}:
                record.state = "APPLIED" if phase == "applied" else "COMPLETED"
                record.appliedTime = now
                record.appliedValue = event.appliedValue
            elif phase in {"error", "failed", "rejected"}:
                record.state = "ERROR"
                record.error = str(event.error or event.state or "firmware rejected command")

        result: dict[str, Any] = {"modeApplied": False, "rowModesApplied": False, "railApplied": False}
        if command_type == "rows":
            self._handle_rows(event)
        elif command_type == "mode":
            result["modeApplied"] = self._handle_mode(event)
        elif command_type == "row_modes":
            result["rowModesApplied"] = self._handle_row_modes(event)
        elif command_type == "rail":
            result["railApplied"] = self._handle_rail(event)
        elif command_type == "ads_check":
            self._handle_ads_check(event)
        elif command_type == "ads_identity":
            self.adsDiagnostics["identity"] = dict(event.rawFields)
        return result

    def observe_mode_frame(self, mode: str, generation: int | None, request_id: int | None, seq: int) -> bool:
        normalized = _normalize_mode(mode)
        if self.pendingMode is not None:
            return False
        changed = normalized != self.appliedMode
        if changed or self.modeGeneration is None:
            self.appliedMode = normalized
            self.modeGeneration = generation
            self.modeRequestId = request_id
            self.modeFrameSeq = int(seq)
            self.transitionState = "synced" if changed else "applied"
            self.modeError = ""
            self.modePendingSince = None
        return changed

    def observe_row_modes_frame(
        self,
        modes: Any,
        generation: int | None,
        request_id: int | None,
        seq: int,
    ) -> bool:
        """Synchronise a first-attach profile without bypassing RMAPP.

        This is allowed only when no ROWMODES transaction is pending. A mixed
        data frame can therefore establish initial attach state, but can never
        complete a requested transaction or replace a mismatched RMAPP.
        """

        normalized = _normalize_row_modes(modes)
        if self.pendingRowModes is not None:
            return False
        changed = normalized != self.appliedRowModes
        if changed or self.rowModeGeneration is None:
            self.appliedRowModes = normalized
            self.rowModeGeneration = generation
            self.rowModeRequestId = request_id
            self.rowModeFrameSeq = int(seq)
            self.rowModeTransitionState = "synced" if changed else "applied"
            self.rowModeError = ""
            self.rowModePendingSince = None
        return changed

    def timeout_old(self, seconds: float = 5.0) -> None:
        now = time.time()
        for record in self._commands.values():
            if record.state not in {"REQUESTED", "ACCEPTED"} or now - record.sentTime <= seconds:
                continue
            record.state = "TIMEOUT"
            record.error = "Timed out waiting for firmware apply"
            if record.commandType == "mode" and self.pendingMode == record.requestedValue:
                self.transitionState = "timeout"
                self.modeError = record.error
            if record.commandType == "row_modes" and self.pendingRowModes == record.requestedValue:
                self.rowModeTransitionState = "timeout"
                self.rowModeError = "Timed out waiting for firmware apply (RMAPP)"
            if record.commandType == "rail" and self.railState in {"requested", "accepted"}:
                self.railState = "timeout"
                if self.desiredModeAfterRail:
                    self.transitionState = "timeout"
                    self.modeError = "Timed out waiting for firmware rail apply (RAPP)"
                    self.desiredModeAfterRail = None
        if (
            self.pendingMode is not None
            and self.modePendingSince is not None
            and self.transitionState in {"requested", "accepted"}
            and now - self.modePendingSince > seconds
        ):
            self.transitionState = "timeout"
            self.modeError = "Timed out waiting for firmware apply (MAPP)"
        if (
            self.pendingRowModes is not None
            and self.rowModePendingSince is not None
            and self.rowModeTransitionState in {"requested", "accepted"}
            and now - self.rowModePendingSince > seconds
        ):
            self.rowModeTransitionState = "timeout"
            self.rowModeError = "Timed out waiting for firmware apply (RMAPP)"

    def measurement_snapshot(self) -> dict[str, Any]:
        self.timeout_old()
        return {
            "appliedMode": self.appliedMode,
            "pendingMode": self.pendingMode,
            "transitionState": self.transitionState,
            "requestId": self.modeRequestId,
            "generation": self.modeGeneration,
            "frameSeq": self.modeFrameSeq,
            "error": self.modeError,
            "deviceState": self.deviceState,
            "rowProfile": {
                "appliedModes": list(self.appliedRowModes),
                "pendingModes": list(self.pendingRowModes) if self.pendingRowModes is not None else None,
                "transitionState": self.rowModeTransitionState,
                "requestId": self.rowModeRequestId,
                "generation": self.rowModeGeneration,
                "frameSeq": self.rowModeFrameSeq,
                "error": self.rowModeError,
            },
            "rail": {
                "configured": self.railConfigured,
                "state": self.railState,
                "requestId": self.railRequestId,
                "measuredAvddV": self.measuredAvddV,
                "measuredAvssV": self.measuredAvssV,
            },
        }

    def snapshot(self) -> dict[str, Any]:
        self.timeout_old()
        latest = max(self._commands.values(), key=lambda item: item.localId, default=None)
        return {
            "requestedRows": self.requestedRows,
            "activeRows": self.activeRows,
            "pendingRows": self.pendingRows,
            "rowsRequestId": self.rowsRequestId,
            "latestCommand": _record_payload(latest) if latest else None,
            "transactions": [_record_payload(item) for item in sorted(self._commands.values(), key=lambda item: item.localId)[-20:]],
        }

    def _new_record(self, command_type: str, command: str, requested_value: Any) -> CommandRecord:
        local_id = next(self._ids)
        record = CommandRecord(local_id, command_type, command, requested_value, "REQUESTED", time.time())
        self._commands[local_id] = record
        return record

    def _send(self, record: CommandRecord, sender: Callable[[str], None]) -> None:
        try:
            sender(record.command)
        except Exception as exc:
            record.state = "ERROR"
            record.error = str(exc)
            if record.commandType == "mode":
                self.transitionState = "error"
                self.modeError = str(exc)
                self.pendingMode = None
                self.modePendingSince = None
            if record.commandType == "row_modes":
                self.rowModeTransitionState = "error"
                self.rowModeError = str(exc)
                self.pendingRowModes = None
                self.rowModePendingSince = None
            if record.commandType == "rail":
                self.railState = "error"
                if self.desiredModeAfterRail:
                    self.transitionState = "error"
                    self.modeError = str(exc)
                    self.pendingMode = None
                    self.modePendingSince = None
                    self.desiredModeAfterRail = None
            if record.commandType == "rows":
                self.pendingRows = None
                self.rowsRequestId = None
            raise

    def _match_record(self, command_type: str, event: CommandTransactionEvent) -> CommandRecord | None:
        if event.requestId is not None:
            for record in reversed(list(self._commands.values())):
                if record.commandType == command_type and record.firmwareId == event.requestId:
                    return record
            # The first accepted response assigns the firmware-generated ID to
            # a locally requested command.  A later event carrying a different
            # ID must never be allowed to mutate that record through the value
            # fallback below.
            if str(event.phase).lower() != "accepted":
                return None
        for record in reversed(list(self._commands.values())):
            if record.commandType != command_type or record.state not in {"REQUESTED", "ACCEPTED"}:
                continue
            if event.requestId is not None and record.firmwareId is not None and record.firmwareId != event.requestId:
                continue
            if event.requestedValue is None or _values_match(record.requestedValue, event.requestedValue):
                return record
        return None

    def _handle_rows(self, event: CommandTransactionEvent) -> None:
        if event.phase == "accepted":
            if self.rowsRequestId is not None and event.requestId != self.rowsRequestId:
                return
            if event.requestedValue is not None:
                self.requestedRows = int(event.requestedValue)
                self.pendingRows = int(event.requestedValue)
                self.rowsRequestId = event.requestId
        elif event.phase == "applied" and event.appliedValue is not None:
            if self.rowsRequestId is None or event.requestId != self.rowsRequestId:
                return
            self.activeRows = int(event.appliedValue)
            self.pendingRows = None
            self.rowsRequestId = None

    def _handle_mode(self, event: CommandTransactionEvent) -> bool:
        if event.phase == "accepted":
            requested = _normalize_mode(str(event.requestedValue))
            if self.pendingMode is not None and requested != self.pendingMode:
                return False
            if self.modeRequestId is not None and self.pendingMode is not None and event.requestId != self.modeRequestId:
                return False
            # Once a mode request has completed, a duplicated/late MACK from
            # that transaction must not reopen it. A different request ID
            # whose authoritative old mode matches the currently applied mode
            # is, however, a real subsequent transaction (for example a
            # multi-mode Replay or another host controller).
            if self.pendingMode is None and self.modeRequestId is not None:
                old_mode = _normalize_mode(str(event.oldValue)) if event.oldValue is not None else None
                if event.requestId == self.modeRequestId or old_mode != self.appliedMode:
                    return False
            self.pendingMode = requested
            self.modeRequestId = event.requestId
            self.transitionState = "accepted"
            self.modeError = ""
            self.modePendingSince = time.time()
            return False
        if event.phase == "applied":
            applied = _normalize_mode(str(event.appliedValue or event.requestedValue))
            if self.pendingMode != applied or self.modeRequestId != event.requestId:
                return False
            self.appliedMode = applied
            self.pendingMode = None
            self.transitionState = "applied"
            self.modeGeneration = event.generation
            self.modeFrameSeq = event.frameSeq
            self.modeError = ""
            self.modePendingSince = None
            self.deviceState = {"CAP": "CAPACITANCE", "VOLT": "VOLTAGE", "RES": "RESISTANCE"}[applied]
            # Legacy MODE is the firmware's backwards-compatible "set all
            # rows" action, so its authoritative MAPP also commits the row
            # profile. Cross-kind requests are rejected at send time, but a
            # remote controller may still create an overlapping transaction;
            # never let its MAPP steal the identity of a pending ROWMODES.
            self.appliedRowModes = (applied,) * 8
            if self.pendingRowModes is None:
                self.rowModeRequestId = event.requestId
                # Firmware 331c445 completes its independent RowModeProfile
                # state after MODE succeeds, but MAPP.gen belongs only to the
                # MeasurementMode context.  The two counters can already have
                # diverged after prior RMAPP transactions, so never forge a
                # row-profile generation from MAPP.
                self.rowModeGeneration = None
                self.rowModeFrameSeq = event.frameSeq
                self.rowModeTransitionState = "applied"
                self.rowModeError = ""
                self.rowModePendingSince = None
            return True
        if event.phase in {"error", "failed", "rejected"}:
            if self.modeRequestId is None:
                matches_error = event.requestId is None
            else:
                matches_error = event.requestId == self.modeRequestId
            if matches_error:
                self.transitionState = "error"
                self.pendingMode = None
                self.modePendingSince = None
                device_state = str(event.state or "").upper()
                detail = str(event.error or event.state or "mode transition failed")
                if device_state in {"SAFE", "DEGRADED", "FAULT"}:
                    self.deviceState = device_state
                    self.modeError = f"Firmware entered {device_state}: {detail}"
                else:
                    self.modeError = detail
            return False
        return False

    def _handle_row_modes(self, event: CommandTransactionEvent) -> bool:
        phase = str(event.phase).lower()
        if phase == "accepted":
            requested = _normalize_row_modes(event.requestedValue)
            if self.pendingRowModes is not None and requested != self.pendingRowModes:
                return False
            if self.rowModeRequestId is not None and self.pendingRowModes is not None and event.requestId != self.rowModeRequestId:
                return False
            if self.pendingRowModes is None and self.rowModeRequestId is not None:
                old_modes = _normalize_row_modes(event.oldValue) if event.oldValue is not None else None
                if event.requestId == self.rowModeRequestId or old_modes != self.appliedRowModes:
                    return False
            self.pendingRowModes = requested
            self.rowModeRequestId = event.requestId
            self.rowModeTransitionState = "accepted"
            self.rowModeError = ""
            self.rowModePendingSince = time.time()
            return False
        if phase == "applied":
            applied = _normalize_row_modes(event.appliedValue or event.requestedValue)
            if self.pendingRowModes != applied or self.rowModeRequestId != event.requestId:
                return False
            self.appliedRowModes = applied
            self.pendingRowModes = None
            self.rowModeTransitionState = "applied"
            self.rowModeGeneration = event.generation
            self.rowModeFrameSeq = event.frameSeq
            self.rowModeError = ""
            self.rowModePendingSince = None
            return True
        if phase in {"error", "failed", "rejected"}:
            if self.rowModeRequestId is None:
                matches_error = event.requestId is None
            else:
                matches_error = event.requestId == self.rowModeRequestId
            if matches_error:
                self.rowModeTransitionState = "error"
                self.pendingRowModes = None
                self.rowModePendingSince = None
                self.rowModeError = str(event.error or event.state or "row mode transition failed")
            return False
        return False

    def _handle_rail(self, event: CommandTransactionEvent) -> bool:
        if event.phase == "accepted":
            if self.railRequestId is not None and event.requestId != self.railRequestId:
                return False
            self.railState = "accepted"
            self.railRequestId = event.requestId
            return False
        if event.phase == "applied":
            if self.railRequestId != event.requestId:
                return False
            self.railConfigured = True
            self.railState = "applied"
            fields = event.rawFields
            if "avdd" in fields:
                self.measuredAvddV = int(fields["avdd"], 0) / 1_000_000.0
            if "avss" in fields:
                self.measuredAvssV = int(fields["avss"], 0) / 1_000_000.0
            return True
        if event.phase in {"error", "failed", "rejected"}:
            if self.railRequestId is not None and event.requestId not in {None, self.railRequestId}:
                return False
            self.railConfigured = False
            self.railState = "error"
            if self.desiredModeAfterRail:
                self.transitionState = "error"
                self.modeError = str(event.error or "rail configuration failed")
                self.pendingMode = None
                self.desiredModeAfterRail = None
            return False
        return False

    def _handle_ads_check(self, event: CommandTransactionEvent) -> None:
        if event.phase == "accepted":
            active_request_id = self.adsDiagnostics.get("requestId")
            if (
                active_request_id is not None
                and event.requestId != active_request_id
                and self.adsDiagnostics.get("state") == "checking"
            ):
                return
            self.adsDiagnostics["requestId"] = event.requestId
            self.adsDiagnostics["state"] = "checking"
            self.adsDiagnostics["error"] = ""
            return
        active_request_id = self.adsDiagnostics.get("requestId")
        if active_request_id is not None and event.requestId != active_request_id:
            return
        self.adsDiagnostics["requestId"] = event.requestId
        if event.phase in {"checking", "progress"}:
            self.adsDiagnostics["state"] = "checking"
            self.adsDiagnostics["check"] = dict(event.rawFields)
        elif event.phase in {"complete", "completed", "applied"}:
            self.adsDiagnostics["statistics"] = dict(event.rawFields)
            restore = str(event.rawFields.get("restore", "")).lower()
            self.adsDiagnostics["state"] = "completed" if restore in {"", "ok"} else "failed"
            if restore not in {"", "ok"}:
                self.adsDiagnostics["error"] = f"ADS restore result: {restore}"
        elif event.phase in {"error", "failed", "rejected"}:
            self.adsDiagnostics["state"] = "failed"
            self.adsDiagnostics["error"] = str(event.error or event.state or "ADS check failed")


def _normalize_mode(mode: str) -> str:
    normalized = str(mode).strip().upper()
    normalized = {"CAPACITANCE": "CAP", "VOLTAGE": "VOLT", "RESISTANCE": "RES"}.get(normalized, normalized)
    if normalized not in {"CAP", "VOLT", "RES"}:
        raise ValueError("measurement mode must be CAP, VOLT, or RES")
    return normalized


def _normalize_row_modes(modes: Any) -> tuple[str, ...]:
    if isinstance(modes, str):
        compact = modes.strip().upper()
        if len(compact) == 8 and set(compact) <= {"C", "V", "R"}:
            modes = tuple({"C": "CAP", "V": "VOLT", "R": "RES"}[item] for item in compact)
        else:
            raise ValueError("row modes must contain exactly 8 CAP, VOLT, or RES entries")
    try:
        normalized = tuple(_normalize_mode(str(mode)) for mode in modes)
    except TypeError as exc:
        raise ValueError("row modes must contain exactly 8 CAP, VOLT, or RES entries") from exc
    if len(normalized) != 8:
        raise ValueError("row modes must contain exactly 8 CAP, VOLT, or RES entries")
    return normalized


def _values_match(left: Any, right: Any) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.upper() == right.upper()
    if isinstance(left, dict) and isinstance(right, dict):
        # Firmware acknowledgements may add descriptive fields such as
        # ``source=external`` that were not part of the host request.  Match
        # the shared command value fields without requiring those extras.
        shared_keys = set(left).intersection(right)
        return bool(shared_keys) and all(_values_match(left[key], right[key]) for key in shared_keys)
    return left == right


def _record_payload(record: CommandRecord) -> dict[str, Any]:
    payload = asdict(record)
    payload["requestedRows"] = record.requestedRows
    return payload


def _rejected_transaction_result(reason: str) -> dict[str, Any]:
    return {
        "modeApplied": False,
        "rowModesApplied": False,
        "railApplied": False,
        reason: True,
    }
