from __future__ import annotations

import queue
import threading
import time
from dataclasses import asdict, replace
from typing import Any

import numpy as np

from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_app.app.state import UiState as UiModel
from sensorarray_app.constants import RAW_INPUT_QUEUE_SIZE
from sensorarray_app.domain.baseline import BaselineSession
from sensorarray_app.domain.lifecycle import classify_reset_reason
from sensorarray_app.domain.models import (
    BatteryTelemetry,
    BootInfo,
    BuildInfo,
    CalibrationInfo,
    CapacitanceFrame,
    CommandAccepted,
    CommandApplied,
    CommandTransactionEvent,
    DisplayMode,
    FdcIsolationInfo,
    LogRecord,
    MeasurementFrame,
    MixedMeasurementFrame,
    ParserErrorEvent,
    PerformanceInfo,
    ProtocolInfo,
    RailTelemetry,
    ReadyInfo,
    RecoveryEvent,
    ResistanceFrame,
    RestartEvent,
    TransportEnvelope,
    TransportStateEvent,
    UsbStreamInfo,
    VoltageFrame,
)
from sensorarray_app.domain.selection import correct_selection, select_group
from sensorarray_app.protocol.registry import ProtocolRegistry
from sensorarray_app.services.command_service import CommandService
from sensorarray_app.services.device_synchronizer import DeviceSynchronizer
from sensorarray_app.services.discovery_service import scan_ble, scan_wifi
from sensorarray_app.services.recording_service import ScientificRecorder
from sensorarray_app.store.matrix_store import MatrixStore
from sensorarray_app.store.raw_log_store import RawLogStore
from sensorarray_app.store.statistics_store import StatisticsStore
from sensorarray_app.store.telemetry_store import TelemetryStore
from sensorarray_app.transport.manager import TransportManager


def _optional_nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(str(value), 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


class SensorArrayRuntime:
    def __init__(self, config: AppConfiguration):
        self.config = config
        self.inputQueue: queue.Queue[TransportEnvelope | TransportStateEvent] = queue.Queue(maxsize=RAW_INPUT_QUEUE_SIZE)
        self.registry = ProtocolRegistry()
        self.matrixStore = MatrixStore(config.historyFrames)
        self.rawLogs = RawLogStore(config.maxLogLines)
        self.telemetry = TelemetryStore()
        self.stats = StatisticsStore()
        self.transport = TransportManager(self.inputQueue)
        self.commands = CommandService()
        self.synchronizer = DeviceSynchronizer(self.transport.send_command)
        self.recorder = ScientificRecorder()
        self.deviceInfo: dict[str, Any] = {
            "boot": None,
            "ready": None,
            "protocol": None,
            "build": None,
            "performance": {},
            "fdcIsolation": None,
            "usbStream": None,
            "calibration": None,
            "lifecycleEvents": [],
        }
        self._lastConnectionGeneration = 0
        self.ui = UiModel()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run_parser, name="SensorArrayParserWorker", daemon=True)
        self._baseline_session: BaselineSession | None = None
        self._ble_scan_results: list[dict[str, Any]] = []
        self._wifi_scan_results: list[dict[str, Any]] = []
        self._discovery_state = {"ble": "idle", "wifi": "idle"}
        self.lastBootTransition: dict[str, Any] = {
            "firstAttach": False,
            "bootChanged": False,
            "oldBootId": None,
            "newBootId": None,
        }
        self._legacyRowsTransaction: tuple[str, str, int] | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.transport.disconnect()
        self._stop.set()
        self._thread.join(timeout=2.0)
        if self.recorder.state in {"RECORDING", "ERROR"}:
            self.recorder.stop()
        self.rawLogs.close(clean=True)

    def connect_serial(self, port: str, baud: int, auto_reconnect: bool) -> None:
        self.invalidate_baseline("session changed")
        self.telemetry.begin_device(f"serial:{str(port).strip().lower()}")
        self.transport.connect_serial(port, baud, auto_reconnect)

    def connect_replay(self, path: str, speed: float = 1.0) -> None:
        self.invalidate_baseline("replay restart")
        self.telemetry.begin_device(f"replay:{str(path)}")
        self.transport.connect_replay(path, speed)

    def connect_ble(self, address: str, device_id: str = "", auto_reconnect: bool = True) -> None:
        self.invalidate_baseline("session changed")
        self.telemetry.begin_device(f"ble:{str(device_id or address).strip().lower()}")
        self.transport.connect_ble(address, device_id, auto_reconnect=auto_reconnect)

    def connect_wifi(self, host: str) -> None:
        self.invalidate_baseline("session changed")
        self.telemetry.begin_device(f"wifi:{str(host).strip().lower()}")
        self.transport.connect_wifi(host)

    def disconnect(self) -> None:
        self.invalidate_baseline("disconnect")
        self.telemetry.mark_connection_stale()
        self.transport.disconnect()

    def request_rows(self, rows: int) -> None:
        self.commands.request_rows(rows, self.transport.send_command)
        self._host_log("Commands", "info", f"ROWS={rows} requested; waiting for RCMD/RAPP")

    def request_measurement_mode(
        self,
        mode: str,
        measured_avdd_v: float | None = None,
        measured_avss_v: float | None = None,
    ) -> None:
        normalized = str(mode).strip().upper()
        normalized = {"CAPACITANCE": "CAP", "VOLTAGE": "VOLT", "RESISTANCE": "RES"}.get(normalized, normalized)
        if normalized not in {"CAP", "VOLT", "RES"}:
            raise ValueError("measurement mode must be CAP, VOLT, or RES")
        # Firmware now owns its ADS analogue-rail monitor. Keep the optional
        # arguments only as a source-compatible API shim for older callers;
        # MODE must never be sequenced behind a production RAILCFG transaction.
        self.commands.request_mode(normalized, self.transport.send_command)
        self._host_log("Commands", "info", f"MODE={normalized} requested; waiting for MACK/MAPP")

    def request_row_modes(self, modes: Any) -> None:
        record = self.commands.request_row_modes(modes, self.transport.send_command)
        self._host_log("Commands", "info", f"{record.command} requested; waiting for RMACK/RMAPP")

    def send_command(self, command: str) -> None:
        self.transport.send_command(command)
        self._host_log("Commands", "info", command)

    def capture_baseline(self) -> None:
        snap = self.matrixStore.snapshot()
        has_cap_rows = any(mode == "CAP" for mode in snap.rowModes[: snap.activeRows])
        baseline_generation = snap.profileGeneration if snap.layout == "MIXED" else snap.firmwareGeneration
        baseline_request_id = snap.profileRequestId if snap.layout == "MIXED" else snap.requestId
        if not has_cap_rows or snap.seq is None or baseline_generation is None or baseline_request_id is None:
            self._baseline_session = None
            self.ui.pendingDisplayMode = None
            self.ui.baseline = None
            self.ui.displayMode = DisplayMode.ABSOLUTE_C
            self.ui.baselineStatus = "No data"
            self.ui.baselineInvalidReason = "Available when an active row uses CAP" if not has_cap_rows else "No capacitance frame yet"
            return
        if self.ui.displayMode == DisplayMode.DELTA_PERCENT:
            self.ui.pendingDisplayMode = DisplayMode.DELTA_PERCENT
            self.ui.displayMode = DisplayMode.ABSOLUTE_C
        self.ui.baseline = None
        self._baseline_session = BaselineSession(
            sessionGeneration=snap.sessionGeneration,
            transport=self.transport.status.get("transport", "none"),
            deviceId=self.transport.status.get("device", ""),
            activeRows=snap.activeRows,
            firmwareGeneration=baseline_generation,
            requestId=baseline_request_id,
            measurementDomain="capacitance",
            circuitOffsetPf=self.registry.cap.circuit_offset_pf,
            startMonotonicNs=time.monotonic_ns(),
            userOffsetsPf=self.user_offsets_array().reshape(64),
        )
        self.ui.baselineStatus = "Capturing baseline..."
        self.ui.baselineInvalidReason = ""

    def reset_baseline(self) -> None:
        with self._lock:
            self._baseline_session = None
            self.ui.baseline = None
            self.ui.displayMode = DisplayMode.ABSOLUTE_C
            self.ui.pendingDisplayMode = None
            self.ui.baselineStatus = "Reset"
            self.ui.baselineInvalidReason = ""

    def cancel_baseline(self) -> None:
        if self._baseline_session is not None:
            self._baseline_session.cancelled = True
        self._baseline_session = None
        self.ui.baselineStatus = "Cancelled"

    def invalidate_baseline(self, reason: str) -> None:
        with self._lock:
            self._baseline_session = None
            self.ui.baseline = None
            self.ui.displayMode = DisplayMode.ABSOLUTE_C
            self.ui.pendingDisplayMode = None
            self.ui.baselineStatus = "Invalid"
            self.ui.baselineInvalidReason = reason

    def set_display_mode(self, mode: str) -> None:
        selected = DisplayMode(mode)
        if selected == DisplayMode.DELTA_PERCENT and self.ui.baseline is None:
            self.ui.pendingDisplayMode = DisplayMode.DELTA_PERCENT
            self.capture_baseline()
            return
        self.ui.pendingDisplayMode = None
        self.ui.displayMode = selected

    def set_selection_from_cell(self, cell_name: str) -> None:
        try:
            row = int(cell_name.split("D", maxsplit=1)[0][1:])
            det = int(cell_name.split("D", maxsplit=1)[1])
            snap = self.matrixStore.snapshot()
            with self._lock:
                self.ui.selectionRevision += 1
                self.ui.selection = select_group(row, det, snap.activeRows, self.ui.selectionRevision)
        except Exception as exc:
            self._host_log("Selection", "warning", f"selection ignored: {exc}")
            raise ValueError(f"selection failed for {cell_name}: {exc}") from exc

    def clear_all(self) -> None:
        self.matrixStore.clear()
        self.rawLogs.clear_view()
        self.invalidate_baseline("clear all")
        self.ui.clearRevision += 1

    def start_ble_scan(self) -> None:
        threading.Thread(target=self._ble_scan_worker, name="SensorArrayBleDiscovery", daemon=True).start()

    def start_wifi_scan(self) -> None:
        threading.Thread(target=self._wifi_scan_worker, name="SensorArrayWifiDiscovery", daemon=True).start()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> dict[str, Any]:
        now = time.time()
        matrix = self.matrixStore.snapshot()
        baseline_session = self._baseline_session
        baseline_progress = baseline_session.progress(time.monotonic_ns()) if baseline_session else 0.0
        command_snapshot = self.commands.snapshot()
        stats = self.stats.snapshot(stored_fps=0.0)
        selection = self.ui.selection
        with self._lock:
            selection, corrected = correct_selection(selection, matrix.activeRows, self.ui.selectionRevision + 1)
            if corrected:
                self.ui.selection = selection
                self.ui.selectionRevision = selection.selectionRevision
                self._host_log("Selection", "warning", "selection corrected after ROWS change")
        return {
            "matrix": {
                "revision": matrix.revision,
                "domain": matrix.domain,
                "activeRows": matrix.activeRows,
                "seq": matrix.seq,
                "timestampUs": matrix.timestampUs,
                "matrix": matrix.matrix.tolist(),
                "valid": matrix.valid.tolist(),
                "unit": matrix.unit,
                "sessionGeneration": matrix.sessionGeneration,
                "firmwareGeneration": matrix.firmwareGeneration,
                "requestId": matrix.requestId,
            },
            "selection": asdict(selection),
            "ui": {
                "displayMode": self.ui.displayMode.value,
                "pendingDisplayMode": self.ui.pendingDisplayMode.value if self.ui.pendingDisplayMode else None,
                "paused": self.ui.paused,
                "followLatest": self.ui.followLatest,
                "cellText": self.ui.cellText,
                "freezeColor": self.ui.freezeColor,
                "clearRevision": self.ui.clearRevision,
                "trendLatestN": self.ui.trendLatestN,
                "userOffsetsPf": self.offsets_payload(),
            },
            "baseline": {
                "status": self.baseline_status_code(),
                "label": self.ui.baselineStatus,
                "invalidReason": self.ui.baselineInvalidReason,
                "progress": baseline_progress,
                "frameCount": baseline_session.frameCount if baseline_session else (self.ui.baseline.frameCount if self.ui.baseline else 0),
                "rejectedFrameCount": baseline_session.rejectedFrameCount if baseline_session else (self.ui.baseline.rejectedFrameCount if self.ui.baseline else 0),
                "ready": self.ui.baseline is not None,
                "validCells": int(self.ui.baseline.validMask.sum()) if self.ui.baseline else 0,
                "pendingDisplayMode": self.ui.pendingDisplayMode.value if self.ui.pendingDisplayMode else None,
            },
            "battery": self.telemetry.battery_snapshot(now),
            "rail": self.telemetry.rail_snapshot(now),
            "commands": command_snapshot,
            "transport": dict(self.transport.status),
            "device": dict(self.deviceInfo),
            "bootstrap": self.synchronizer.snapshot(),
            "recording": self.recorder.snapshot(),
            "diagnostics": stats,
            "logs": self.rawLogs.snapshot(limit=300),
            "discovery": {
                "bleState": self._discovery_state["ble"],
                "bleResults": list(self._ble_scan_results),
                "wifiState": self._discovery_state["wifi"],
                "wifiResults": list(self._wifi_scan_results),
            },
        }

    def _run_parser(self) -> None:
        while not self._stop.is_set():
            try:
                item = self.inputQueue.get(timeout=0.05)
            except queue.Empty:
                self.synchronizer.tick()
                self._complete_baseline_if_due()
                continue
            if isinstance(item, TransportStateEvent):
                if item.sessionGeneration != self.commands.sessionGeneration:
                    self.registry.reset_connection()
                    self.commands.reset_session(item.sessionGeneration)
                    self._legacyRowsTransaction = None
                    self.matrixStore.reset_session()
                    # connect_* registers a stable target identity before the
                    # generation changes. With no such identity, fail closed:
                    # the state event could belong to a different board.
                    if self.telemetry.deviceIdentity:
                        self.telemetry.mark_connection_stale()
                    else:
                        self.telemetry.reset()
                state = str(item.state).upper()
                connection_generation = int(item.metadata.get("connectionGeneration", 0) or 0)
                if state in {"CONNECTED", "CONNECTION_RESET", "STREAMING"} and connection_generation != self._lastConnectionGeneration:
                    previous_connection_generation = self._lastConnectionGeneration
                    self.registry.reset_connection()
                    self._legacyRowsTransaction = None
                    self.stats.begin_connection_epoch(
                        source=item.source,
                        boot_id=self.commands.bootId,
                        connection_generation=connection_generation,
                        reconnect=previous_connection_generation > 0,
                    )
                    self._lastConnectionGeneration = connection_generation
                    self.commands.begin_connection(connection_generation)
                    self.matrixStore.begin_connection(connection_generation)
                if state == "SERIAL_RX_OVERFLOW":
                    self.registry.reset_connection()
                    self.stats.record_reject("SERIAL_RX_OVERFLOW")
                    self.stats.record_host_transport_drop()
                if state in {"DISCONNECTED", "ERROR", "FAILED", "RECONNECTING", "RECONNECT_WAIT"}:
                    self.telemetry.mark_connection_stale()
                    self.recorder.record_event(
                        "TRANSPORT_DISCONNECT" if state == "DISCONNECTED" else "TRANSPORT_GAP",
                        {
                            "transport": item.source,
                            "state": state,
                            "connectionGeneration": connection_generation,
                            "bootId": self.commands.bootId,
                            "message": item.message,
                        },
                    )
                    self.synchronizer.stop()
                self.transport.apply_state_event(item)
                self._host_log("Transport", "info", f"{item.source} {item.state} {item.message}".strip())
                if state == "STREAMING" and item.source in {"serial", "ble", "wifi"}:
                    self.synchronizer.start(item.source, item.sessionGeneration, connection_generation)
                    self.recorder.record_event(
                        "TRANSPORT_RECONNECT" if connection_generation > 1 else "TRANSPORT_CONNECT",
                        {
                            "transport": item.source,
                            "connectionGeneration": connection_generation,
                            "bootId": self.commands.bootId,
                        },
                    )
                continue
            active_session_generation = int(self.transport.status.get("sessionGeneration", 0) or 0)
            if item.sessionGeneration != active_session_generation:
                # A stopped BLE/Serial worker can have one final notification
                # already queued.  Never let it mutate the newly connected
                # session's parser, transactions, or matrix.
                self.stats.record_reject("stale_session_generation")
                continue
            # One wire message can yield both its raw LogRecord and one or
            # more typed events.  Hold the shared state lock across the whole
            # batch so REST/WebSocket snapshots cannot expose the raw reply
            # before its authoritative typed state/counters are committed.
            with self._lock:
                self.stats.record_transport(len(item.rawPayload))
                events = self.registry.feed(item)
                for event in events:
                    try:
                        self._handle_event(event)
                    except Exception as exc:
                        # One malformed/additive diagnostic must not kill the
                        # sole parser/state worker and freeze the GUI on its
                        # last frame. Surface and count it, then keep the
                        # transport/lifecycle path alive for recovery.
                        self.stats.record_reject("event_handler_error")
                        self._host_log(
                            "Parser",
                            "error",
                            f"EVENT_HANDLER_ERROR,type={type(event).__name__},error={exc}",
                        )
                self.synchronizer.tick()
                self._complete_baseline_if_due()

    def _handle_event(self, event: Any) -> None:
        self.synchronizer.handle(event)
        connection_generation = int(self.transport.status.get("connectionGeneration", 0) or 0)
        if isinstance(event, (CapacitanceFrame, MeasurementFrame, MixedMeasurementFrame)):
            event = replace(
                event,
                connectionGeneration=connection_generation,
                bootId=self.commands.bootId,
            )
            if event.sourceTransport == "replay" and self.matrixStore.resyncRequired:
                self._establish_replay_frame_boundary(event)
        elif isinstance(event, BatteryTelemetry):
            event = replace(event, bootId=self.commands.bootId)
        elif isinstance(event, RailTelemetry):
            event = replace(event, bootId=self.commands.bootId)
        if isinstance(event, BootInfo):
            was_expected_restart = bool(self.commands.expectedRestart)
            expected_restart_command = self.commands.expectedRestartCommandType
            boot_result = self.commands.observe_boot(event.bootId)
            self.lastBootTransition = dict(boot_result)
            self.deviceInfo["boot"] = asdict(event)
            reset_classification = classify_reset_reason(
                event.reset,
                guard=event.guard,
                prev_stage=event.prevStage,
                expected_restart=was_expected_restart,
                expected_command=expected_restart_command,
            )
            lifecycle_kind = "DEVICE_REBOOT" if boot_result["bootChanged"] else (
                "FIRST_ATTACH" if boot_result["firstAttach"] else "TRANSPORT_RECONNECT"
            )
            lifecycle = {
                "kind": lifecycle_kind,
                "oldBootId": boot_result["oldBootId"],
                "newBootId": boot_result["newBootId"],
                "resetReason": event.reset,
                "resetCategory": reset_classification["category"],
                "resetLabel": reset_classification["label"],
                "resetSeverity": reset_classification["severity"],
                "powerRelated": reset_classification["powerRelated"],
                "stage": event.stage,
                "err": event.err,
                "prevStage": event.prevStage,
                "prevErr": event.prevErr,
                "prevSeq": event.seq,
                "prevHeap": event.prevHeap,
                "guard": event.guard,
                "autoRestarts": event.autoRestarts,
                "expected": was_expected_restart,
                "timestamp": time.time(),
            }
            self.deviceInfo["lifecycleEvents"] = [*self.deviceInfo["lifecycleEvents"][-99:], lifecycle]
            self.recorder.record_event(lifecycle_kind, lifecycle)
            if boot_result["bootChanged"]:
                # These diagnostics describe one firmware boot.  Keeping the
                # previous boot's values visible during bootstrap would make
                # CAP/FDC and derived-rail guards act on stale authority.
                self.deviceInfo["ready"] = None
                self.deviceInfo["performance"] = {}
                self.deviceInfo["fdcIsolation"] = None
                self.deviceInfo["usbStream"] = None
                self.deviceInfo["calibration"] = None
                self.matrixStore.observe_device_reboot(event.bootId)
            else:
                self.matrixStore.observe_boot_identity(event.bootId)
            if hasattr(self.telemetry, "observe_boot"):
                self.telemetry.observe_boot(event.bootId)
            return
        if isinstance(event, ReadyInfo):
            self.deviceInfo["ready"] = asdict(event)
            return
        if isinstance(event, ProtocolInfo):
            self.deviceInfo["protocol"] = asdict(event)
            return
        if isinstance(event, BuildInfo):
            self.deviceInfo["build"] = asdict(event)
            return
        if isinstance(event, PerformanceInfo):
            self.deviceInfo["performance"] = {**self.deviceInfo.get("performance", {}), event.kind: asdict(event)}
            source = event.sourceTransport or str(self.transport.status.get("transport", "unknown"))
            connection_generation = int(self.transport.status.get("connectionGeneration", 0) or 0)
            if (
                event.kind == "SF50"
                and event.sequenceStart is not None
                and event.sequenceEnd is not None
                and event.frameCount is not None
                and event.invalidFrames is not None
            ):
                self.stats.record_firmware_output_window(
                    source=source,
                    boot_id=self.commands.bootId,
                    connection_generation=connection_generation,
                    sequence_start=event.sequenceStart,
                    sequence_end=event.sequenceEnd,
                    frame_count=event.frameCount,
                    invalid_frames=event.invalidFrames,
                    firmware_drops=event.firmwareDrops or 0,
                )
            if event.kind == "PERF":
                published = _optional_nonnegative_int(event.rawFields.get("pub"))
                fresh = _optional_nonnegative_int(event.rawFields.get("fresh"))
                performance_sequence_end = _optional_nonnegative_int(event.rawFields.get("frames"))
                if published is not None and fresh is not None:
                    self.stats.record_firmware_performance_counters(
                        source=source,
                        boot_id=self.commands.bootId,
                        connection_generation=connection_generation,
                        published_frames=published,
                        fresh_frames=fresh,
                        sequence_end=performance_sequence_end,
                    )
                for metric in ("dropOut", "usbDrop", "lifeDrop", "queueDrop"):
                    total = _optional_nonnegative_int(event.rawFields.get(metric))
                    if total is not None:
                        self.stats.record_firmware_drop_report(
                            f"{source}:{self.commands.bootId}:{metric}",
                            total,
                            source=source,
                            boot_id=self.commands.bootId,
                            connection_generation=connection_generation,
                            attribute_sequence=(
                                metric == "dropOut"
                                or (metric == "usbDrop" and source.lower() == "serial")
                            ),
                            # PERF counters are cumulative for the current
                            # firmware boot.  On Host attach, the first value
                            # is a baseline; it must not claim losses that
                            # happened before this connection epoch.
                            baseline_first=True,
                            sequence_end=performance_sequence_end,
                        )
            if event.kind == "BL50" and source.lower() == "ble":
                data_drops = _optional_nonnegative_int(event.rawFields.get("dropD"))
                if data_drops is not None:
                    self.stats.record_firmware_drop_report(
                        f"{source}:{self.commands.bootId}:bleDataDrop",
                        data_drops,
                        source=source,
                        boot_id=self.commands.bootId,
                        connection_generation=connection_generation,
                        attribute_sequence=True,
                        baseline_first=True,
                    )
            return
        if isinstance(event, FdcIsolationInfo):
            self.deviceInfo["fdcIsolation"] = asdict(event)
            return
        if isinstance(event, UsbStreamInfo):
            self.deviceInfo["usbStream"] = asdict(event)
            if str(event.state).lower() == "applied":
                self.stats.begin_output_policy(
                    source=str(self.transport.status.get("transport", "serial")),
                    boot_id=self.commands.bootId,
                    connection_generation=int(self.transport.status.get("connectionGeneration", 0) or 0),
                )
                self.commands.observe_action_state("usb_stream", event.mode)
            self._after_configuration_event(event)
            return
        if isinstance(event, CalibrationInfo):
            self.deviceInfo["calibration"] = asdict(event)
            return
        if isinstance(event, RecoveryEvent):
            self.deviceInfo["recovery"] = asdict(event)
            self.recorder.record_event("RECOVERY", asdict(event))
            return
        if isinstance(event, RestartEvent):
            self.deviceInfo["restart"] = asdict(event)
            if str(event.phase).lower() == "restarting":
                self.recorder.record_event(
                    "EXPECTED_RESTART",
                    {
                        "requestId": event.requestId,
                        "commandKind": event.kind,
                        "oldBootId": self.commands.bootId,
                        "connectionGeneration": connection_generation,
                    },
                )
                self.transport.request_expected_restart_reconnect()
            return
        if isinstance(event, CapacitanceFrame):
            if self.matrixStore.add_capacitance(event):
                self._record_frame_stats(event)
                self._record_scientific_frame(event)
                if self._baseline_session is not None:
                    self._baseline_session.add_frame(event)
            else:
                self._frame_drop_log("CAP", event.seq, event.generation, event.requestId)
        elif isinstance(event, MeasurementFrame):
            # A complete current V/R frame may establish mode only on first
            # attach, before this session has observed any measurement or a
            # MAPP generation.  After MAPP (or after CAP data), a late frame
            # from the previous mode must reach the store's wrong-mode gate;
            # it must never be allowed to redefine the applied mode here.
            first_session_measurement = self.matrixStore.snapshot().seq is None
            if (
                self.commands.pendingMode is None
                and self.commands.pendingRowModes is None
                and self.commands.transitionState == "applied"
                and self.commands.rowModeTransitionState == "applied"
                and self.commands.modeRequestId is None
                and self.commands.rowModeRequestId is None
                and not self.commands.modeError
                and not self.commands.rowModeError
                and self.commands.appliedMode != event.mode
                and self.commands.modeGeneration is None
                and self.commands.rowModeGeneration is None
                and first_session_measurement
            ):
                changed = self.commands.observe_mode_frame(event.mode, event.generation, event.requestId, event.seq)
                if changed:
                    self.matrixStore.sync_measurement_mode_from_frame(event)
                    self.invalidate_baseline("measurement mode changed")
                    self._measurement_mode_visual_reset()
            if self.matrixStore.add_measurement(event):
                self._record_frame_stats(event)
                rail_span_uv = (
                    int(event.avddUv) - int(event.avssUv)
                    if event.avddUv is not None and event.avssUv is not None
                    else None
                )
                self.telemetry.update_rail(
                    RailTelemetry(
                        railSpanUv=rail_span_uv,
                        valid=bool(event.railValid),
                        # Firmware has already applied its maximum-age policy
                        # when it publishes rail=1.  A non-zero age is useful
                        # provenance (and normal for a cached monitor result),
                        # not proof that the span is stale.  Host connection
                        # gaps and elapsed wall time are handled separately by
                        # TelemetryStore; treating every age>0 as stale made a
                        # valid VOLT frame such as rail=1,age=1 fail the GUI
                        # read-only rail acceptance.
                        fresh=bool(event.railValid),
                        age=int(event.railAgeFrames),
                        ageMs=None,
                        source="internal_monitor",
                        reason="ok" if event.railValid else "rail_invalid",
                        timestamp=float(event.receivedTime),
                        rawFields=dict(event.rawFields),
                        avddUv=int(event.avddUv),
                        avssUv=int(event.avssUv),
                        bootId=event.bootId,
                    )
                )
                self._record_scientific_frame(event)
            else:
                self._frame_drop_log(event.mode, event.seq, event.generation, event.requestId)
        elif isinstance(event, MixedMeasurementFrame):
            configured_profile = list(self.commands.appliedRowModes)
            configured_profile[: event.rows] = list(event.activeProfile)
            configured_profile_tuple = tuple(configured_profile)
            first_session_measurement = self.matrixStore.snapshot().seq is None
            if (
                self.commands.pendingRowModes is None
                and self.commands.pendingMode is None
                and self.commands.rowModeTransitionState == "applied"
                and self.commands.transitionState == "applied"
                and self.commands.rowModeRequestId is None
                and self.commands.modeRequestId is None
                and not self.commands.rowModeError
                and not self.commands.modeError
                and self.commands.rowModeGeneration is None
                and self.commands.modeGeneration is None
                and first_session_measurement
            ):
                changed = self.commands.observe_row_modes_frame(
                    configured_profile_tuple,
                    event.profileGeneration,
                    event.profileRequestId,
                    event.seq,
                )
                if changed:
                    self.matrixStore.apply_row_modes(
                        configured_profile_tuple,
                        event.profileGeneration,
                        event.profileRequestId,
                        event.seq,
                    )
            if self.matrixStore.add_mixed(event):
                self._record_frame_stats(event)
                self._record_scientific_frame(event)
                if self._baseline_session is not None:
                    self._add_mixed_baseline_frame(event)
            else:
                self._frame_drop_log("MIXED", event.seq, event.profileGeneration, event.profileRequestId)
        elif isinstance(event, VoltageFrame):
            self.matrixStore.add_voltage(event)
            self.stats.record_frame()
        elif isinstance(event, ResistanceFrame):
            self.matrixStore.add_resistance(event)
            self.stats.record_frame()
        elif isinstance(event, BatteryTelemetry):
            self.telemetry.update_battery(event)
        elif isinstance(event, RailTelemetry):
            self.telemetry.update_rail(event)
        elif isinstance(event, LogRecord):
            self.rawLogs.add(event)
            if event.tag == "WIRE_INTERLEAVE":
                self.stats.record_wire_interleave_recovery(
                    dropped_pending_frame=event.parsedFields.get("droppedPendingFrame") == "1"
                )
            if event.tag == "TXDROP":
                reported = _optional_nonnegative_int(event.parsedFields.get("drop"))
                if reported is not None:
                    self.stats.record_firmware_drop_report(
                        f"{event.source}:{event.parsedFields.get('ch', 'unknown')}",
                        reported,
                    )
        elif isinstance(event, CommandAccepted):
            self.commands.accept(event)
            self._legacyRowsTransaction = ("accepted", event.rawText, int(event.sessionGeneration))
        elif isinstance(event, CommandApplied):
            pending_rows = self.commands.pendingRows
            pending_request_id = self.commands.rowsRequestId
            self.commands.apply(event)
            rows_applied = (
                pending_rows is not None
                and event.newRows == pending_rows
                and event.commandId == pending_request_id
                and self.commands.pendingRows is None
            )
            if rows_applied:
                self.matrixStore.apply_rows(
                    self.commands.activeRows,
                    event.generation,
                    event.commandId,
                    event.seq,
                )
                self.invalidate_baseline("ROWS applied")
                self.recorder.record_event(
                    "ROWS_APPLIED",
                    {"rows": self.commands.activeRows, "generation": event.generation, "requestId": event.commandId},
                )
            self._after_configuration_event(event)
            self._legacyRowsTransaction = ("applied", event.rawText, int(event.sessionGeneration))
        elif isinstance(event, CommandTransactionEvent):
            compatibility_key = (str(event.phase).lower(), event.rawText, int(event.sessionGeneration))
            if str(event.commandType).lower() == "rows" and compatibility_key == self._legacyRowsTransaction:
                # TextLogProtocol intentionally emits the legacy RCMD/RAPP
                # event and its generic transaction for API compatibility.
                # Runtime already consumed the legacy event; consuming the
                # paired generic object would count one wire terminal twice.
                self._legacyRowsTransaction = None
                return
            previous_mode = self.commands.appliedMode
            result = self.commands.handle(event)
            if self.commands.authoritativeStateKnown and self.matrixStore.resyncRequired:
                self.matrixStore.complete_resync(
                    mode=self.commands.appliedMode,
                    rows=self.commands.activeRows,
                    row_modes=self.commands.appliedRowModes,
                    rows_generation=self.commands.rowsGeneration,
                    rows_request_id=self.commands.rowsAppliedRequestId,
                    rows_frame_seq=self.commands.rowsFrameSeq,
                    mode_generation=self.commands.modeGeneration,
                    mode_request_id=self.commands.modeRequestId,
                    profile_generation=self.commands.rowModeGeneration,
                    profile_request_id=self.commands.rowModeRequestId,
                )
            if result.get("modeApplied"):
                self.matrixStore.apply_measurement_mode(
                    self.commands.appliedMode,
                    self.commands.modeGeneration,
                    self.commands.modeRequestId,
                    self.commands.modeFrameSeq,
                )
                if self.commands.appliedMode != previous_mode:
                    self.invalidate_baseline("measurement mode changed")
                self._measurement_mode_visual_reset()
                self.recorder.record_event(
                    "MODE_APPLIED",
                    {
                        "mode": self.commands.appliedMode,
                        "generation": self.commands.modeGeneration,
                        "requestId": self.commands.modeRequestId,
                    },
                )
            if result.get("rowModesApplied"):
                self.matrixStore.apply_row_modes(
                    self.commands.appliedRowModes,
                    self.commands.rowModeGeneration,
                    self.commands.rowModeRequestId,
                    self.commands.rowModeFrameSeq,
                )
                self.invalidate_baseline("row measurement modes changed")
                self._measurement_mode_visual_reset()
                self.recorder.record_event(
                    "ROWMODES_APPLIED",
                    {
                        "profile": list(self.commands.appliedRowModes),
                        "generation": self.commands.rowModeGeneration,
                        "requestId": self.commands.rowModeRequestId,
                    },
                )
            if result.get("railApplied") and self.commands.desiredModeAfterRail:
                desired_mode = self.commands.desiredModeAfterRail
                self.commands.desiredModeAfterRail = None
                try:
                    self.commands.request_mode(desired_mode, self.transport.send_command)
                    self._host_log("Commands", "info", f"External rail applied; MODE={desired_mode} sent")
                except Exception as exc:
                    self.commands.transitionState = "error"
                    self.commands.modeError = str(exc)
            self._after_configuration_event(event)
        elif isinstance(event, ParserErrorEvent):
            self.stats.record_reject(event.reason)
            if "crc" in event.reason.lower():
                self.stats.crcFailures += 1
            self.rawLogs.add(
                LogRecord(
                    timestamp=time.time(),
                    monotonicTime=time.monotonic_ns(),
                    source=event.source,
                    channel=event.channel,
                    tag="PARSER",
                    severity="error",
                    rawText=f"{event.reason}: {event.detail} {event.rawText}".strip(),
                    parsedFields={"reason": event.reason},
                    recognised=True,
                    sessionGeneration=event.sessionGeneration,
                )
            )

    def _after_configuration_event(self, event: Any) -> None:
        """Subclass hook for ordered preference restoration after bootstrap."""

    def _establish_replay_frame_boundary(
        self,
        event: CapacitanceFrame | MeasurementFrame | MixedMeasurementFrame,
    ) -> None:
        """Admit a verified offline frame after a replay transport reset.

        Live links must complete ordered bootstrap queries before data clears
        quarantine. A replay file has no device to query, so its strict,
        CRC-verified frame supplies the authoritative offline boundary. The
        command state remains labelled ``resync_confirmed``; no MAPP/RMAPP is
        invented.
        """

        if isinstance(event, MixedMeasurementFrame):
            configured = list(self.commands.appliedRowModes)
            configured[: event.rows] = list(event.activeProfile)
            row_modes = tuple(configured)
            mode = self.commands.appliedMode
            rows_generation = event.rowsGeneration
            rows_request_id = event.rowsRequestId
            rows_frame_seq = event.seq
            mode_generation = None
            mode_request_id = None
            profile_generation = event.profileGeneration
            profile_request_id = event.profileRequestId
        else:
            mode = "CAP" if isinstance(event, CapacitanceFrame) else event.mode
            row_modes = (mode,) * 8
            rows_generation = event.generation if isinstance(event, CapacitanceFrame) else None
            rows_request_id = event.requestId if isinstance(event, CapacitanceFrame) else None
            # C/D/K identity belongs to the independent ROWS context.  A V/R
            # frame has no ROWS generation/request/boundary fields, so its
            # MeasurementMode sequence must not be forged into a ROWS gate.
            # MODE transitions may legitimately restart their frame sequence.
            rows_frame_seq = event.seq if isinstance(event, CapacitanceFrame) else None
            mode_generation = None if isinstance(event, CapacitanceFrame) else event.generation
            mode_request_id = None if isinstance(event, CapacitanceFrame) else event.requestId
            profile_generation = None
            profile_request_id = None

        self.commands.resync_authoritative(
            mode=mode,
            rows=event.rows,
            row_modes=row_modes,
            mode_generation=mode_generation,
            mode_request_id=mode_request_id,
            profile_generation=profile_generation,
            profile_request_id=profile_request_id,
        )
        self.matrixStore.complete_resync(
            mode=mode,
            rows=event.rows,
            row_modes=row_modes,
            rows_generation=rows_generation,
            rows_request_id=rows_request_id,
            rows_frame_seq=rows_frame_seq,
            mode_generation=mode_generation,
            mode_request_id=mode_request_id,
            profile_generation=profile_generation,
            profile_request_id=profile_request_id,
        )

    def _measurement_mode_visual_reset(self) -> None:
        """Hook for the backend runtime's quantity-specific colour cache."""

    def _record_scientific_frame(
        self,
        frame: CapacitanceFrame | MeasurementFrame | MixedMeasurementFrame,
    ) -> None:
        accepted = self.recorder.record_frame(
            frame,
            configured_row_profile=self.commands.appliedRowModes,
            rail=self.telemetry.rail_snapshot(time.time()),
        )
        if self.recorder.state == "RECORDING" and not accepted:
            self.stats.record_reject("recorder_queue_drop")

    def _record_frame_stats(
        self,
        frame: CapacitanceFrame | MeasurementFrame | MixedMeasurementFrame,
    ) -> None:
        usb = self.deviceInfo.get("usbStream") or {}
        self.stats.record_frame(
            seq=frame.seq,
            source=frame.sourceTransport,
            boot_id=frame.bootId,
            connection_generation=frame.connectionGeneration,
            usb_mode=usb.get("mode"),
            data_every=usb.get("dataEvery"),
        )

    def _add_mixed_baseline_frame(self, frame: MixedMeasurementFrame) -> bool:
        """Capture only CAP cells from one mixed frame with per-cell freshness."""

        session = self._baseline_session
        if session is None or session.cancelled:
            return False
        if frame.receivedMonotonicNs < session.startMonotonicNs or frame.receivedMonotonicNs >= session.endMonotonicNs:
            return False
        matches = (
            frame.sessionGeneration == session.sessionGeneration
            and frame.sourceTransport == session.transport
            and (not session.deviceId or frame.deviceId == session.deviceId)
            and frame.rows == session.activeRows
            and frame.profileGeneration == session.firmwareGeneration
            and frame.profileRequestId == session.requestId
        )
        if not matches:
            session.rejectedFrameCount += 1
            return False
        session.frameCount += 1
        offsets = (
            np.asarray(session.userOffsetsPf, dtype=np.float64).reshape(64)
            if session.userOffsetsPf is not None
            else np.zeros(64, dtype=np.float64)
        )
        for row_frame in frame.rowFrames:
            if row_frame.mode != "CAP":
                continue
            row_index = int(row_frame.row) - 1
            values = np.asarray(row_frame.physicalValues, dtype=np.float64).reshape(8)
            valid = np.asarray(row_frame.validMask, dtype=bool).reshape(8)
            fresh = np.asarray(row_frame.freshMask, dtype=bool).reshape(8)
            for col_index in range(8):
                cell_index = row_index * 8 + col_index
                value = float(values[col_index] - offsets[cell_index])
                if valid[col_index] and fresh[col_index] and np.isfinite(value):
                    session._samples[cell_index].append(value)
        return True

    def _frame_drop_log(self, mode: str, seq: int, generation: int | None, request_id: int | None) -> None:
        self._host_log(
            "FRAME_DROP",
            "warning",
            f"Dropped stale/wrong-mode frame mode={mode},seq={seq},gen={generation},rid={request_id}",
        )

    def _complete_baseline_if_due(self) -> None:
        session = self._baseline_session
        if session is None:
            return
        now_ns = time.monotonic_ns()
        if now_ns < session.endMonotonicNs and not session.cancelled:
            return
        result = session.complete()
        valid_count = int(result.validMask.sum())
        if valid_count:
            self.ui.baseline = result
            self.ui.baselineStatus = "Ready"
            self.ui.baselineInvalidReason = ""
            if self.ui.pendingDisplayMode == DisplayMode.DELTA_PERCENT:
                self.ui.displayMode = DisplayMode.DELTA_PERCENT
        else:
            self.ui.baseline = None
            self.ui.displayMode = DisplayMode.ABSOLUTE_C
            reason = _baseline_invalid_reason(result.invalidReasons)
            self.ui.baselineStatus = "Invalid"
            self.ui.baselineInvalidReason = reason
        self.ui.pendingDisplayMode = None
        self._baseline_session = None

    def baseline_status_code(self) -> str:
        if self._baseline_session is not None:
            return "capturing"
        if self.ui.baseline is not None:
            return "ready"
        if self.ui.baselineStatus.lower().startswith("no data"):
            return "no_data"
        if self.ui.baselineStatus.lower().startswith("reset"):
            return "reset"
        if self.ui.baselineStatus.lower().startswith("invalid"):
            return "invalid"
        return "idle"

    def offsets_payload(self) -> list[list[float]]:
        return [[float(value) for value in row] for row in self.ui.userOffsetsPf]

    def user_offsets_array(self) -> np.ndarray:
        return np.asarray(self.ui.userOffsetsPf, dtype=np.float64).reshape(8, 8)

    def _host_log(self, tag: str, severity: str, text: str) -> None:
        self.rawLogs.add(
            LogRecord(
                timestamp=time.time(),
                monotonicTime=time.monotonic_ns(),
                source="host",
                channel="host",
                tag=tag,
                severity=severity,
                rawText=text,
                parsedFields={},
                recognised=True,
                sessionGeneration=self.transport.sessions.generation,
            )
        )

    def _ble_scan_worker(self) -> None:
        self._discovery_state["ble"] = "discovering"
        self._ble_scan_results = [asdict(item) for item in scan_ble(10.0)]
        self._discovery_state["ble"] = "done"
        self._host_log("Discovery", "info", f"BLE scan found {len(self._ble_scan_results)} candidates")

    def _wifi_scan_worker(self) -> None:
        self._discovery_state["wifi"] = "discovering"
        self._wifi_scan_results = [asdict(item) for item in scan_wifi()]
        self._discovery_state["wifi"] = "done"
        self._host_log("Discovery", "info", f"Wi-Fi discovery found {len(self._wifi_scan_results)} candidates")


def _baseline_invalid_reason(reasons: list[str]) -> str:
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    counts.pop("inactive", None)
    counts.pop("valid", None)
    if not counts:
        return "no valid baseline cells"
    reason, count = max(counts.items(), key=lambda item: item[1])
    return f"{reason} ({count} cells)"
