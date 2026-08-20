from __future__ import annotations

import itertools
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from sensorarray_app.domain.models import CommandAccepted, CommandApplied, CommandTransactionEvent
from sensorarray_app.transport.base import TransportNotSent, TransportWriteOutcomeUnknown


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
    acceptedMessage: str = ""
    acceptedRawFields: dict[str, str] = field(default_factory=dict)
    terminalMessage: str = ""
    terminalRawFields: dict[str, str] = field(default_factory=dict)
    timeoutObserved: bool = False
    outcomeSource: str = "wire"
    bootId: int | None = None
    terminalCount: int = 0

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
        self.rowsGeneration: int | None = None
        self.rowsAppliedRequestId: int | None = None
        self.rowsFrameSeq: int | None = None
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
        self.connectionGeneration = 0
        self.bootId: int | None = None
        self.previousBootId: int | None = None
        self.authoritativeStateKnown = False
        self.syncState = "not_attached"
        self.resyncRequired = False
        self.expectedRestart = False
        self.expectedRestartRequestId: int | None = None
        self.expectedRestartCommandType: str | None = None
        self.protocolWarnings: list[str] = []
        self._syncSeen = {"mode": False, "rows": False, "row_modes": False}

    def reset_session(self, session_generation: int) -> None:
        self.sessionGeneration = int(session_generation)
        # A host transport epoch is not an MCU boot. Preserve the last known
        # applied state as stale until BOOT?/STATE?/ROWS?/ROWMODES? establish
        # authoritative truth. In-flight writes are ambiguous, not erased.
        self.authoritativeStateKnown = False
        self.syncState = "awaiting_bootstrap"
        self.resyncRequired = True
        self._syncSeen = {"mode": False, "rows": False, "row_modes": False}
        # ADS identity/check output is connection-observed diagnostic state.
        # Keep command audit records below, but do not present an old link's
        # identity as current before the new bootstrap queries complete.
        self.adsDiagnostics = {
            "state": "awaiting_resync",
            "requestId": None,
            "identity": {},
            "check": {},
            "statistics": {},
            "error": "",
        }
        self._mark_inflight_outcome_unknown()
        if self.pendingMode is not None:
            self.transitionState = "outcome_unknown"
        if self.pendingRowModes is not None:
            self.rowModeTransitionState = "outcome_unknown"
        if self.pendingRows is not None:
            self.protocolWarnings.append("ROWS outcome unknown after connection change")

    def begin_connection(self, connection_generation: int) -> None:
        self.connectionGeneration = int(connection_generation)
        self.authoritativeStateKnown = False
        self.syncState = "bootstrapping"
        self.resyncRequired = True
        self._syncSeen = {"mode": False, "rows": False, "row_modes": False}
        # Auto-reconnect deliberately keeps the host session generation.  Any
        # write accepted by the old link is nevertheless ambiguous until the
        # authoritative bootstrap queries prove whether it applied.
        self._mark_inflight_outcome_unknown()
        if self.pendingMode is not None:
            self.transitionState = "outcome_unknown"
        if self.pendingRowModes is not None:
            self.rowModeTransitionState = "outcome_unknown"

    def _mark_inflight_outcome_unknown(self) -> None:
        for record in self._commands.values():
            if record.state in {"REQUESTED", "ACCEPTED"}:
                record.state = "OUTCOME_UNKNOWN"
                record.error = "Connection changed before a terminal firmware event; resync required"

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

    def observe_action_state(self, command_type: str, applied_value: Any) -> bool:
        """Complete an id-less setter whose authoritative reply is a state record."""

        for record in reversed(list(self._commands.values())):
            if record.commandType != command_type or record.state not in {"REQUESTED", "OUTCOME_UNKNOWN"}:
                continue
            if not _values_match(record.requestedValue, applied_value):
                continue
            record.state = "APPLIED"
            record.appliedValue = applied_value
            record.appliedTime = time.time()
            record.outcomeSource = "state_readback"
            record.error = ""
            record.terminalCount += 1
            return True
        return False

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
                record.error = ""
                record.outcomeSource = "wire"
                record.acceptedMessage = event.rawText
                record.acceptedRawFields = dict(event.rawFields)
            elif phase in {"applied", "complete", "completed"}:
                record.state = "APPLIED" if phase == "applied" else "COMPLETED"
                record.appliedTime = now
                record.appliedValue = event.appliedValue
                record.error = ""
                record.outcomeSource = "wire"
                record.terminalMessage = event.rawText
                record.terminalRawFields = dict(event.rawFields)
            elif phase == "restarting":
                record.state = "EXPECTED_RESTART"
                record.acceptedTime = record.acceptedTime or now
                record.error = ""
                record.outcomeSource = "wire"
                record.terminalMessage = event.rawText
                record.terminalRawFields = dict(event.rawFields)
                self.mark_expected_restart(event.requestId, command_type=command_type)
            elif phase in {"error", "failed", "rejected"}:
                record.state = "ERROR"
                record.error = str(event.error or event.state or "firmware rejected command")
                record.terminalMessage = event.rawText
                record.terminalRawFields = dict(event.rawFields)

        result: dict[str, Any] = {"modeApplied": False, "rowModesApplied": False, "railApplied": False}
        if record is None and phase in {"applied", "complete", "completed", "failed", "rejected", "error"}:
            if command_type in {"mode", "row_modes", "rows", "rail", "fdc_isolation", "restart", "recover"}:
                self.protocolWarnings.append(f"terminal without local acceptance: {command_type} id={event.requestId}")
                result["unsolicitedTerminal"] = True
        if record is not None and phase in {"applied", "complete", "completed", "failed", "rejected", "error"}:
            record.terminalCount += 1
            if record.terminalCount > 1:
                warning = f"duplicate terminal for {command_type} id={event.requestId}"
                self.protocolWarnings.append(warning)
                result["duplicateTerminal"] = True
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
        elif command_type == "device_state" and phase == "snapshot":
            self._handle_state_snapshot(event.rawFields)
        return result

    def observe_boot(self, boot_id: int) -> dict[str, Any]:
        new_boot_id = int(boot_id)
        old_boot_id = self.bootId
        changed = old_boot_id is not None and old_boot_id != new_boot_id
        first_attach = old_boot_id is None
        self.previousBootId = old_boot_id if changed else self.previousBootId
        self.bootId = new_boot_id
        if changed:
            for record in self._commands.values():
                if record.state not in {"REQUESTED", "ACCEPTED", "OUTCOME_UNKNOWN", "EXPECTED_RESTART"}:
                    continue
                if self.expectedRestart and (
                    self.expectedRestartRequestId is None or record.firmwareId == self.expectedRestartRequestId
                ):
                    record.state = "COMPLETED_AFTER_REBOOT"
                    record.appliedTime = time.time()
                    record.outcomeSource = "boot_resync"
                else:
                    record.state = "ABORTED_BY_REBOOT"
                    record.error = f"Device rebooted ({old_boot_id} -> {new_boot_id})"
            self.pendingMode = None
            self.pendingRowModes = None
            self.pendingRows = None
            self.transitionState = "aborted_by_reboot"
            self.rowModeTransitionState = "aborted_by_reboot"
            self.modeRequestId = None
            self.rowModeRequestId = None
            self.rowsRequestId = None
            self.rowsGeneration = None
            self.rowsAppliedRequestId = None
            self.rowsFrameSeq = None
            self.modeGeneration = None
            self.rowModeGeneration = None
            self.railConfigured = False
            self.railState = "unknown_after_reboot"
            self.expectedRestart = False
            self.expectedRestartRequestId = None
            self.expectedRestartCommandType = None
        self.authoritativeStateKnown = False
        self.syncState = "querying_authoritative_state"
        return {"firstAttach": first_attach, "bootChanged": changed, "oldBootId": old_boot_id, "newBootId": new_boot_id}

    def mark_expected_restart(self, request_id: int | None, *, command_type: str = "restart") -> None:
        self.expectedRestart = True
        self.expectedRestartRequestId = request_id
        self.expectedRestartCommandType = str(command_type).lower()
        record = self._match_record(
            command_type,
            CommandTransactionEvent(commandType=command_type, phase="accepted", requestId=request_id),
        )
        if record is not None:
            record.state = "EXPECTED_RESTART"

    def resync_authoritative(
        self,
        *,
        mode: str | None = None,
        rows: int | None = None,
        row_modes: Any | None = None,
        mode_generation: int | None = None,
        mode_request_id: int | None = None,
        profile_generation: int | None = None,
        profile_request_id: int | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if mode is not None:
            normalized_mode = _normalize_mode(mode)
            if self.pendingMode is not None:
                matching = normalized_mode == self.pendingMode
                self._resolve_unknown_records("mode", matching)
                result["modeOutcome"] = "RESYNC_CONFIRMED_APPLIED" if matching else "RESYNC_CONFIRMED_NOT_APPLIED"
                self.pendingMode = None
            self.appliedMode = normalized_mode
            self.modeGeneration = mode_generation
            self.modeRequestId = mode_request_id
            self.transitionState = "resync_confirmed"
            self.deviceState = {"CAP": "CAPACITANCE", "VOLT": "VOLTAGE", "RES": "RESISTANCE"}[normalized_mode]
            self._syncSeen["mode"] = True
        if rows is not None:
            active_rows = int(rows)
            if not 1 <= active_rows <= 8:
                raise ValueError("authoritative ROWS must be 1..8")
            if self.pendingRows is not None:
                self._resolve_unknown_records("rows", active_rows == self.pendingRows)
                self.pendingRows = None
            self.activeRows = active_rows
            self._syncSeen["rows"] = True
        if row_modes is not None:
            normalized_profile = _normalize_row_modes(row_modes)
            if self.pendingRowModes is not None:
                matching = normalized_profile == self.pendingRowModes
                self._resolve_unknown_records("row_modes", matching)
                result["rowModesOutcome"] = "RESYNC_CONFIRMED_APPLIED" if matching else "RESYNC_CONFIRMED_NOT_APPLIED"
                self.pendingRowModes = None
            self.appliedRowModes = normalized_profile
            self.rowModeGeneration = profile_generation
            self.rowModeRequestId = profile_request_id
            self.rowModeTransitionState = "resync_confirmed"
            self._syncSeen["row_modes"] = True
        self.authoritativeStateKnown = all(self._syncSeen.values())
        if self.authoritativeStateKnown:
            self.syncState = "synced"
            self.resyncRequired = False
        return result

    def _resolve_unknown_records(self, command_type: str, applied: bool) -> None:
        for record in reversed(list(self._commands.values())):
            if record.commandType != command_type or record.state not in {"REQUESTED", "ACCEPTED", "OUTCOME_UNKNOWN"}:
                continue
            record.state = "RESYNC_CONFIRMED_APPLIED" if applied else "RESYNC_CONFIRMED_NOT_APPLIED"
            record.appliedTime = time.time()
            record.outcomeSource = "state_resync"
            return

    def _handle_state_snapshot(self, fields: dict[str, str]) -> None:
        active = fields.get("active")
        normalized_active = str(active or "").strip().upper()
        if normalized_active in {"CAP", "VOLT", "RES"}:
            self.resync_authoritative(
                mode=normalized_active,
                mode_generation=_optional_int(fields.get("gen")),
                mode_request_id=_optional_int(fields.get("rid")),
            )
            return
        if normalized_active:
            # Firmware explicitly uses active=NONE while its matrix engine is
            # SAFE/DEGRADED. This is authoritative lifecycle information, not
            # an unknown measurement mode to pass through the C/V/R parser.
            if self.pendingMode is not None:
                self._resolve_unknown_records("mode", False)
                self.pendingMode = None
                self.modePendingSince = None
            state = str(fields.get("state") or normalized_active).strip().upper()
            self.deviceState = state
            self.authoritativeStateKnown = False
            self.syncState = "device_degraded"
            self.resyncRequired = True
            self.transitionState = "error"
            self.modeError = f"Device reported state={state}, active={normalized_active}"

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
            record.state = "OUTCOME_UNKNOWN"
            record.timeoutObserved = True
            record.error = "Timed out observing a terminal event; firmware outcome is unknown"
            if record.commandType == "mode" and self.pendingMode == record.requestedValue:
                self.transitionState = "outcome_unknown"
                self.modeError = record.error
            if record.commandType == "row_modes" and self.pendingRowModes == record.requestedValue:
                self.rowModeTransitionState = "outcome_unknown"
                self.rowModeError = "Timed out observing RMAPP/RMERR; firmware outcome is unknown"
            if record.commandType == "rail" and self.railState in {"requested", "accepted"}:
                self.railState = "outcome_unknown"
                if self.desiredModeAfterRail:
                    self.transitionState = "outcome_unknown"
                    self.modeError = "Timed out observing RAPP; firmware outcome is unknown"
        if (
            self.pendingMode is not None
            and self.modePendingSince is not None
            and self.transitionState in {"requested", "accepted"}
            and now - self.modePendingSince > seconds
        ):
            self.transitionState = "outcome_unknown"
            self.modeError = "Timed out observing MAPP/MERR; firmware outcome is unknown"
        if (
            self.pendingRowModes is not None
            and self.rowModePendingSince is not None
            and self.rowModeTransitionState in {"requested", "accepted"}
            and now - self.rowModePendingSince > seconds
        ):
            self.rowModeTransitionState = "outcome_unknown"
            self.rowModeError = "Timed out observing RMAPP/RMERR; firmware outcome is unknown"

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
            "bootId": self.bootId,
            "connectionGeneration": self.connectionGeneration,
            "authoritativeStateKnown": self.authoritativeStateKnown,
            "syncState": self.syncState,
            "resyncRequired": self.resyncRequired,
            "expectedRestart": self.expectedRestart,
            "protocolWarnings": list(self.protocolWarnings[-20:]),
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
            "rowsGeneration": self.rowsGeneration,
            "rowsAppliedRequestId": self.rowsAppliedRequestId,
            "rowsFrameSeq": self.rowsFrameSeq,
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
        except TransportWriteOutcomeUnknown as exc:
            record.state = "OUTCOME_UNKNOWN"
            record.error = str(exc)
            record.outcomeSource = "ambiguous_write"
            self.resyncRequired = True
            self.syncState = "resync_required"
            if record.commandType == "mode":
                self.transitionState = "outcome_unknown"
                self.modeError = str(exc)
            elif record.commandType == "row_modes":
                self.rowModeTransitionState = "outcome_unknown"
                self.rowModeError = str(exc)
            elif record.commandType == "rail":
                self.railState = "outcome_unknown"
            raise
        except TransportNotSent as exc:
            self._mark_not_sent(record, exc)
            raise
        except Exception as exc:
            self._mark_not_sent(record, exc)
            raise

    def _mark_not_sent(self, record: CommandRecord, exc: Exception) -> None:
        record.state = "NOT_SENT"
        record.error = str(exc)
        record.outcomeSource = "host_pre_submit"
        if record.commandType == "mode":
            self.transitionState = "not_sent"
            self.modeError = str(exc)
            self.pendingMode = None
            self.modePendingSince = None
        if record.commandType == "row_modes":
            self.rowModeTransitionState = "not_sent"
            self.rowModeError = str(exc)
            self.pendingRowModes = None
            self.rowModePendingSince = None
        if record.commandType == "rail":
            self.railState = "not_sent"
            if self.desiredModeAfterRail:
                self.transitionState = "error"
                self.modeError = str(exc)
                self.pendingMode = None
                self.modePendingSince = None
                self.desiredModeAfterRail = None
        if record.commandType == "rows":
            self.pendingRows = None
            self.rowsRequestId = None

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
            if record.commandType != command_type or record.state not in {"REQUESTED", "ACCEPTED", "OUTCOME_UNKNOWN"}:
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
            self.rowsGeneration = event.generation
            self.rowsAppliedRequestId = event.requestId
            self.rowsFrameSeq = event.frameSeq
            self.pendingRows = None
            self.rowsRequestId = None
        elif event.phase == "snapshot" and event.appliedValue is not None:
            self.resync_authoritative(rows=int(event.appliedValue))
            self.rowsGeneration = event.generation
            self.rowsAppliedRequestId = event.requestId
            self.rowsFrameSeq = event.frameSeq

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
                # Firmware completes its independent RowModeProfile
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
        if phase == "snapshot" and event.appliedValue is not None:
            self.resync_authoritative(
                row_modes=event.appliedValue,
                profile_generation=event.generation,
                profile_request_id=event.requestId,
            )
            return False
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
            if len(set(applied)) == 1:
                # Homogeneous ROWMODES uses the fast C/V/R frame family and
                # updates MeasurementMode internally, but RMAPP.gen belongs to
                # RowModeProfile.  Keep mode generation explicitly unknown
                # until STATE? or the first authoritative frame supplies it.
                self.appliedMode = applied[0]
                self.modeGeneration = None
                self.modeRequestId = event.requestId
                self.modeFrameSeq = event.frameSeq
                self.transitionState = "applied_by_row_profile"
                self.deviceState = {
                    "CAP": "CAPACITANCE",
                    "VOLT": "VOLTAGE",
                    "RES": "RESISTANCE",
                }[applied[0]]
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


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return None


def _rejected_transaction_result(reason: str) -> dict[str, Any]:
    return {
        "modeApplied": False,
        "rowModesApplied": False,
        "railApplied": False,
        reason: True,
    }
