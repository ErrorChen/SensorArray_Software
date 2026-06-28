from __future__ import annotations

import queue
import threading
import time
from dataclasses import asdict
from typing import Any

import numpy as np

from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_app.app.state import UiState as UiModel
from sensorarray_app.constants import RAW_INPUT_QUEUE_SIZE
from sensorarray_app.domain.baseline import BaselineSession
from sensorarray_app.domain.models import (
    BatteryTelemetry,
    CapacitanceFrame,
    CommandAccepted,
    CommandApplied,
    DisplayMode,
    LogRecord,
    ParserErrorEvent,
    ResistanceFrame,
    TransportEnvelope,
    TransportStateEvent,
    VoltageFrame,
)
from sensorarray_app.domain.selection import correct_selection, select_group
from sensorarray_app.protocol.registry import ProtocolRegistry
from sensorarray_app.services.command_service import CommandService
from sensorarray_app.services.discovery_service import scan_ble, scan_wifi
from sensorarray_app.store.matrix_store import MatrixStore
from sensorarray_app.store.raw_log_store import RawLogStore
from sensorarray_app.store.statistics_store import StatisticsStore
from sensorarray_app.store.telemetry_store import TelemetryStore
from sensorarray_app.transport.manager import TransportManager


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
        self.ui = UiModel()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run_parser, name="SensorArrayParserWorker", daemon=True)
        self._baseline_session: BaselineSession | None = None
        self._ble_scan_results: list[dict[str, Any]] = []
        self._wifi_scan_results: list[dict[str, Any]] = []
        self._discovery_state = {"ble": "idle", "wifi": "idle"}

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.transport.disconnect()
        self._stop.set()
        self._thread.join(timeout=2.0)

    def connect_serial(self, port: str, baud: int, auto_reconnect: bool) -> None:
        self.invalidate_baseline("session changed")
        self.transport.connect_serial(port, baud, auto_reconnect)

    def connect_replay(self, path: str, speed: float = 1.0) -> None:
        self.invalidate_baseline("replay restart")
        self.transport.connect_replay(path, speed)

    def connect_ble(self, address: str, device_id: str = "") -> None:
        self.invalidate_baseline("session changed")
        self.transport.connect_ble(address, device_id)

    def connect_wifi(self, host: str) -> None:
        self.invalidate_baseline("session changed")
        self.transport.connect_wifi(host)

    def disconnect(self) -> None:
        self.invalidate_baseline("disconnect")
        self.transport.disconnect()

    def request_rows(self, rows: int) -> None:
        self.commands.request_rows(rows, self.transport.send_command)
        self._host_log("Commands", "info", f"ROWS={rows} requested; waiting for RCMD/RAPP")

    def send_command(self, command: str) -> None:
        self.transport.send_command(command)
        self._host_log("Commands", "info", command)

    def capture_baseline(self) -> None:
        snap = self.matrixStore.snapshot()
        if snap.seq is None or snap.firmwareGeneration is None or snap.requestId is None:
            self._baseline_session = None
            self.ui.pendingDisplayMode = None
            self.ui.baseline = None
            self.ui.displayMode = DisplayMode.ABSOLUTE_C
            self.ui.baselineStatus = "No data"
            self.ui.baselineInvalidReason = "No capacitance frame yet"
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
            firmwareGeneration=snap.firmwareGeneration,
            requestId=snap.requestId,
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
            "commands": command_snapshot,
            "transport": dict(self.transport.status),
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
                self._complete_baseline_if_due()
                continue
            if isinstance(item, TransportStateEvent):
                self.transport.apply_state_event(item)
                self._host_log("Transport", "info", f"{item.source} {item.state} {item.message}".strip())
                continue
            self.stats.record_transport(len(item.rawPayload))
            events = self.registry.feed(item)
            for event in events:
                self._handle_event(event)
            self._complete_baseline_if_due()

    def _handle_event(self, event: Any) -> None:
        if isinstance(event, CapacitanceFrame):
            self.matrixStore.add_capacitance(event)
            self.stats.record_frame()
            if self._baseline_session is not None:
                self._baseline_session.add_frame(event)
        elif isinstance(event, VoltageFrame):
            self.matrixStore.add_voltage(event)
            self.stats.record_frame()
        elif isinstance(event, ResistanceFrame):
            self.matrixStore.add_resistance(event)
            self.stats.record_frame()
        elif isinstance(event, BatteryTelemetry):
            self.telemetry.update_battery(event)
        elif isinstance(event, LogRecord):
            self.rawLogs.add(event)
        elif isinstance(event, CommandAccepted):
            self.commands.accept(event)
        elif isinstance(event, CommandApplied):
            old = self.commands.activeRows
            self.commands.apply(event)
            if event.newRows is not None and event.newRows != old:
                self.invalidate_baseline("ROWS applied")
        elif isinstance(event, ParserErrorEvent):
            self.stats.record_reject(event.reason)
            if event.reason == "crc":
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
