from __future__ import annotations

import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

import numpy as np

from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_app.app.runtime import SensorArrayRuntime
from sensorarray_app.constants import APP_VERSION, CAP_FIXED_SCALE, CAP_INVALID_SENTINEL, DEFAULT_SERIAL_BAUD, WIFI_DEFAULT_HOST
from sensorarray_app.domain.models import (
    CapacitanceFrame,
    CommandApplied,
    CommandTransactionEvent,
    DisplayMode,
    MeasurementFrame,
    MixedMeasurementFrame,
    TransportEnvelope,
    UsbStreamInfo,
)
from sensorarray_app.services.discovery_service import scan_ble, scan_wifi
from sensorarray_app.transport.serial_transport import SerialTransport
from sensorarray_backend.core.history import history_payload
from sensorarray_backend.core.session_data import (
    SessionFrame,
    export_session_bytes,
    frames_to_measurement_ascii_bytes,
    load_session_frames,
)
from sensorarray_backend.core.snapshot import snapshot_payload

_LINE_ENDINGS = {"lf": "\n", "crlf": "\r\n", "none": ""}


class BackendRuntime(SensorArrayRuntime):
    def __init__(self, config: AppConfiguration):
        super().__init__(config)
        self.selectedMode = "serial"
        self.serialPort = ""
        self.serialBaud = int(config.serialBaud or DEFAULT_SERIAL_BAUD)
        self.bleAddress = ""
        self.bleDeviceId = ""
        self.wifiHost = WIFI_DEFAULT_HOST
        self.wifiFallbackHost = WIFI_DEFAULT_HOST
        self.replayPath: str | None = None
        self.replaySpeed = 1.0
        self.commandLineEnding = "lf"
        self.defaultSaveDirectory = str(Path.cwd())
        self._lastColourRanges: dict[str, tuple[float | None, float | None]] = {
            "cap_absolute": (None, None),
            "cap_delta": (None, None),
            "voltage": (None, None),
            "resistance": (None, None),
        }
        self._resolvedColourRanges = dict(self._lastColourRanges)
        self._frozenColourRanges = dict(self._lastColourRanges)
        self.preferredMeasurementMode = "CAP"
        self.preferredRowModes: tuple[str, ...] = ("CAP",) * 8
        self.preferredRows = 8
        self.autoReconnect = True
        self.resumeMeasurementAfterDeviceRestart = False
        self.preferredUsbStream = "DEVICE_DEFAULT"
        self._preferenceApplyState: dict[str, Any] = {
            "state": "IDLE",
            "reason": "",
            "targetBootId": None,
            "error": "",
            "commands": [],
        }
        self.synchronizer.onComplete = self._on_bootstrap_complete
        self.measuredAvddV: float | None = None
        self.measuredAvssV: float | None = None
        self._exportSessionId = str(uuid.uuid4())

    def set_transport_mode(self, mode: str) -> dict[str, str]:
        if mode not in {"serial", "ble", "wifi", "replay"}:
            raise ValueError("mode must be serial, ble, wifi, or replay")
        self.selectedMode = mode
        return {"mode": mode}

    def list_serial_ports(self) -> list[dict[str, str]]:
        return SerialTransport.list_ports()

    def connect_serial(self, port: str, baud: int = DEFAULT_SERIAL_BAUD, auto_reconnect: bool = True) -> None:
        if not str(port or "").strip():
            raise ValueError("serial port is required")
        self.selectedMode = "serial"
        self.serialPort = str(port).strip()
        self.serialBaud = int(baud or DEFAULT_SERIAL_BAUD)
        self.autoReconnect = bool(auto_reconnect)
        super().connect_serial(self.serialPort, self.serialBaud, self.autoReconnect)

    def connect_ble(self, address: str, device_id: str = "", auto_reconnect: bool | None = None) -> None:
        if not str(address or "").strip():
            raise ValueError("BLE address is required")
        self.selectedMode = "ble"
        self.bleAddress = str(address).strip()
        self.bleDeviceId = str(device_id or "")
        reconnect = self.autoReconnect if auto_reconnect is None else bool(auto_reconnect)
        self.autoReconnect = reconnect
        super().connect_ble(self.bleAddress, self.bleDeviceId, reconnect)

    def connect_wifi(self, host: str) -> None:
        if not str(host or "").strip():
            raise ValueError("Wi-Fi host is required")
        self.selectedMode = "wifi"
        self.wifiHost = str(host).strip()
        self.wifiFallbackHost = self.wifiHost
        super().connect_wifi(self.wifiHost)

    def open_replay(self, path: str, speed: float = 1.0) -> dict[str, Any]:
        replay_path = Path(path)
        if not replay_path.exists():
            raise FileNotFoundError(str(replay_path))
        self.replayPath = str(replay_path)
        self.replaySpeed = max(0.01, float(speed or 1.0))
        self.selectedMode = "replay"
        imported_session = self._restore_exported_session_settings(replay_path)
        return {"path": self.replayPath, "speed": self.replaySpeed, "importedSession": imported_session}

    def start_replay(self) -> None:
        if not self.replayPath:
            raise RuntimeError("no replay file is open")
        self.selectedMode = "replay"
        super().connect_replay(self.replayPath, self.replaySpeed)

    def stop_replay(self) -> None:
        if self.selectedMode == "replay":
            self.disconnect()

    def request_rows(self, rows: int) -> dict[str, Any]:
        rows = int(rows)
        if not (1 <= rows <= 8):
            raise ValueError("ROWS must be 1..8")
        self.preferredRows = rows
        transport = self.transport.status.get("transport", "none")
        if transport in {"none", "replay"}:
            self.matrixStore.set_active_rows_for_display(rows)
            self.commands.requestedRows = rows
            self.commands.activeRows = rows
            self.commands.pendingRows = None
            self._host_log("Commands", "info", f"ROWS={rows} display-only ({transport})")
            return {
                "ok": True,
                "requestedRows": rows,
                "appliedRows": rows,
                "displayOnly": True,
                "activeTransport": transport,
                "status": "display_only",
                "applied": True,
                "rows": rows,
            }
        super().request_rows(rows)
        return {
            "ok": True,
            "requestedRows": rows,
            "appliedRows": self.commands.activeRows,
            "displayOnly": False,
            "activeTransport": transport,
            "status": "sent",
            "applied": False,
            "rows": rows,
        }

    def request_measurement_mode_api(
        self,
        mode: str,
        measured_avdd_v: float | None = None,
        measured_avss_v: float | None = None,
    ) -> dict[str, Any]:
        normalized = str(mode).strip().upper()
        normalized = {"CAPACITANCE": "CAP", "VOLTAGE": "VOLT", "RESISTANCE": "RES"}.get(normalized, normalized)
        self._guard_cap_available((normalized,))
        # measuredAvddV/measuredAvssV remain accepted for wire/API backwards
        # compatibility, but normal MODE operation no longer configures rails.
        # The legacy /rail endpoint is the only path which emits RAILCFG.
        super().request_measurement_mode(normalized)
        self.preferredMeasurementMode = normalized
        return {"ok": True, "measurement": self.commands.measurement_snapshot()}

    def request_row_modes_api(self, modes: Any) -> dict[str, Any]:
        normalized = _normalize_row_modes(modes)
        self._guard_cap_available(normalized)
        super().request_row_modes(normalized)
        self.preferredRowModes = normalized
        return {
            "ok": True,
            "modes": list(normalized),
            "measurement": self.commands.measurement_snapshot(),
        }

    def row_modes_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "modes": list(self.commands.appliedRowModes),
            "rowProfile": self.commands.measurement_snapshot()["rowProfile"],
        }

    def configure_voltage_rail(self, measured_avdd_v: float, measured_avss_v: float) -> dict[str, Any]:
        if self.commands.appliedMode == "VOLT":
            raise ValueError("Cannot apply RAILCFG while VOLT is active; switch to CAP or RES first.")
        avdd_v = _finite_float(measured_avdd_v, "measuredAvddV")
        avss_v = _finite_float(measured_avss_v, "measuredAvssV")
        avdd_uv = int(round(avdd_v * 1_000_000.0))
        avss_uv = int(round(avss_v * 1_000_000.0))
        self.measuredAvddV = avdd_v
        self.measuredAvssV = avss_v
        self.commands.request_rail(avdd_uv, avss_uv, self.transport.send_command)
        self._host_log("Commands", "info", f"RAILCFG={avdd_uv},{avss_uv} requested; waiting for RACK/RAPP")
        return {"ok": True, "measurement": self.commands.measurement_snapshot()}

    def measurement_mode_payload(self) -> dict[str, Any]:
        return {"ok": True, "measurement": self.commands.measurement_snapshot()}

    def device_status_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "device": dict(self.deviceInfo),
            "bootstrap": self.synchronizer.snapshot(),
            "transport": dict(self.transport.status),
            "measurement": self.commands.measurement_snapshot(),
        }

    def request_usb_stream(self, mode: str) -> dict[str, Any]:
        normalized = str(mode).strip().upper()
        if normalized not in {"DEBUG", "FULL"}:
            raise ValueError("USB stream mode must be DEBUG or FULL")
        if self.transport.status.get("transport") != "serial":
            raise ValueError("USBSTREAM applies only to the Serial USB sink")
        self.commands.record_action("usb_stream", f"USBSTREAM={normalized}", normalized, self.transport.send_command)
        return {"ok": True, "requested": normalized, "usbStream": self.deviceInfo.get("usbStream")}

    def request_fdc_isolation(self, enabled: bool) -> dict[str, Any]:
        requested = "ON" if bool(enabled) else "OFF"
        current = self.deviceInfo.get("fdcIsolation") or {}
        if requested == "OFF" and current.get("restartRequired"):
            raise ValueError("Restart required to reinitialise FDC frontends")
        if requested == "ON":
            if current.get("restartRequired") or str(current.get("sd", "")).lower() == "high":
                raise ValueError("FDC shutdown is already active; restart the device before using CAP again")
            profile = tuple(self.commands.appliedRowModes)
            homogeneous_ads = (
                len(profile) == 8
                and len(set(profile)) == 1
                and profile[0] in {"VOLT", "RES"}
                and self.commands.appliedMode == profile[0]
            )
            if not self.commands.authoritativeStateKnown:
                raise ValueError("Wait for device bootstrap to confirm MODE/ROWS/ROWMODES before enabling FDC isolation")
            if self.commands.pendingMode is not None or self.commands.pendingRowModes is not None:
                raise ValueError("Wait for the current measurement transaction before enabling FDC isolation")
            if not homogeneous_ads:
                raise ValueError("FDC isolation requires an authoritative homogeneous VOLT or RES profile")
        self.commands.record_action(
            "fdc_isolation",
            f"FDCISO={requested}",
            requested,
            self.transport.send_command,
        )
        return {"ok": True, "requested": requested, "fdcIsolation": current}

    def _guard_cap_available(self, modes: tuple[str, ...]) -> None:
        current = self.deviceInfo.get("fdcIsolation") or {}
        if current.get("restartRequired") and "CAP" in modes:
            raise ValueError("CAP is unavailable while FDC shutdown is active; restart the device to reinitialise FDC frontends")

    def request_restart(self) -> dict[str, Any]:
        record = self.commands.record_action("restart", "RESTART", None, self.transport.send_command)
        return {"ok": True, "state": record.state, "message": "Restarting device when firmware accepts the request"}

    def request_recover(self, level: int | None = None) -> dict[str, Any]:
        if level is not None and int(level) not in {0, 1, 2}:
            raise ValueError("RECOVER level must be 0, 1, or 2")
        command = "RECOVER" if level is None else f"RECOVER={int(level)}"
        # Bare RECOVER is defined by the 8045 firmware contract as the full
        # level-1 recovery.  Record that effective value so the subsequent
        # RACK,level=1 can be correlated instead of being mistaken for an
        # unsolicited transaction.
        effective_level = 1 if level is None else int(level)
        record = self.commands.record_action(
            "recover", command, {"level": effective_level}, self.transport.send_command
        )
        return {"ok": True, "state": record.state, "level": level}

    def request_calibration(self, operation: str) -> dict[str, Any]:
        normalized = str(operation).strip().upper()
        if normalized not in {"SAVE", "LOAD"}:
            raise ValueError("calibration operation must be SAVE or LOAD")
        record = self.commands.record_action(
            f"calibration_{normalized.lower()}",
            f"CAL={normalized}",
            normalized,
            self.transport.send_command,
        )
        return {"ok": True, "state": record.state, "operation": normalized}

    def start_scientific_recording(self, directory: str, *, allow_reduced_stream: bool = False) -> dict[str, Any]:
        usb = self.deviceInfo.get("usbStream") or {}
        if (
            self.transport.status.get("transport") == "serial"
            and str(usb.get("mode") or "").upper() == "DEBUG"
            and not allow_reduced_stream
        ):
            return {
                "ok": False,
                "requiresConfirmation": True,
                "reason": "USB stream is DEBUG and only a subset of physical frames is received.",
                "actions": ["switch_to_full", "record_reduced_stream", "cancel"],
            }
        status = self.recorder.start(
            directory,
            {
                "appVersion": APP_VERSION,
                "device": self.transport.status.get("device", ""),
                "transport": self.transport.status.get("transport", "none"),
                "bootId": self.commands.bootId,
                "configuredRowProfile": list(self.commands.appliedRowModes),
            },
        )
        self._host_log("Recording", "info", f"Scientific recording started: {status['directory']}")
        return {"ok": True, "recording": status}

    def stop_scientific_recording(self) -> dict[str, Any]:
        status = self.recorder.stop()
        self._host_log("Recording", "info" if not status["error"] else "error", "Scientific recording finalized")
        return {"ok": not bool(status["error"]), "recording": status}

    def update_display_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "displayMode" in payload and payload["displayMode"] is not None:
            mode = str(payload["displayMode"])
            if mode == "absolute_c":
                mode = "absolute_pf"
            self.set_display_mode(mode)
        if "measurementDomain" in payload and payload["measurementDomain"] is not None:
            self.ui.measurementDomain = str(payload["measurementDomain"])
        if "showCellText" in payload and payload["showCellText"] is not None:
            self.ui.cellText = bool(payload["showCellText"])
        if "pauseDisplay" in payload and payload["pauseDisplay"] is not None:
            self.ui.paused = bool(payload["pauseDisplay"])
        if "freezeColor" in payload and payload["freezeColor"] is not None:
            freeze = bool(payload["freezeColor"])
            if freeze and not self.ui.freezeColor:
                self._frozenColourRanges = dict(self._resolvedColourRanges)
            elif not freeze:
                self._frozenColourRanges = {domain: (None, None) for domain in self._frozenColourRanges}
            self.ui.freezeColor = freeze
        if "unitMode" in payload and payload["unitMode"] is not None:
            self.ui.unitMode = str(payload["unitMode"])
        if "voltageReference" in payload and payload["voltageReference"] is not None:
            reference = str(payload["voltageReference"]).strip().lower()
            if reference not in {"ground", "vss_relative", "rail_normalized"}:
                raise ValueError("voltageReference must be ground, vss_relative, or rail_normalized")
            self.ui.voltageReference = reference
        if "circuitOffsetPf" in payload and payload["circuitOffsetPf"] is not None:
            offset = float(payload["circuitOffsetPf"])
            if offset != self.ui.circuitOffsetPf:
                self.ui.circuitOffsetPf = offset
                self.registry.cap.circuit_offset_pf = offset
                self.invalidate_baseline("circuit offset changed")
        return self.display_payload()

    def update_lifecycle_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "autoReconnect" in payload:
            self.autoReconnect = bool(payload["autoReconnect"])
        if "resumeMeasurementAfterDeviceRestart" in payload:
            self.resumeMeasurementAfterDeviceRestart = bool(payload["resumeMeasurementAfterDeviceRestart"])
        if "preferredUsbStream" in payload:
            preference = str(payload["preferredUsbStream"] or "DEVICE_DEFAULT").strip().upper()
            if preference not in {"DEVICE_DEFAULT", "DEBUG", "FULL"}:
                raise ValueError("preferredUsbStream must be DEVICE_DEFAULT, DEBUG, or FULL")
            self.preferredUsbStream = preference
        return {"ok": True, "lifecycle": self.lifecycle_settings_payload()}

    def lifecycle_settings_payload(self) -> dict[str, Any]:
        return {
            "autoReconnect": self.autoReconnect,
            "resumeMeasurementAfterDeviceRestart": self.resumeMeasurementAfterDeviceRestart,
            "preferredUsbStream": self.preferredUsbStream,
            "preferenceApply": dict(self._preferenceApplyState),
        }

    def set_display_mode(self, mode: str) -> None:
        selected = DisplayMode(mode)
        snapshot = self.matrixStore.snapshot()
        has_cap_rows = any(row_mode == "CAP" for row_mode in snapshot.rowModes[: snapshot.activeRows])
        if selected == DisplayMode.DELTA_PERCENT and not has_cap_rows:
            raise ValueError("Delta C/C0 is available when an active row uses CAP.")
        if selected == DisplayMode.DELTA_PERCENT and self.ui.baseline is None:
            self.ui.pendingDisplayMode = DisplayMode.DELTA_PERCENT
            self.capture_baseline()
            return
        self.ui.pendingDisplayMode = None
        self.ui.displayMode = selected if self.ui.baseline is not None or selected == DisplayMode.ABSOLUTE_C else DisplayMode.ABSOLUTE_C

    def baseline_action(self, action: str) -> dict[str, Any]:
        if action == "capture":
            self.capture_baseline()
        elif action == "reset":
            self.reset_baseline()
        elif action == "cancel":
            self.cancel_baseline()
        else:
            raise ValueError("baseline action must be capture, reset, or cancel")
        return self.baseline_payload()

    def current_selection_payload(self, active_rows: int | None = None) -> dict[str, Any]:
        if active_rows is None:
            active_rows = self.matrixStore.snapshot().activeRows
        with self._lock:
            selection, corrected = self._correct_selection_locked(int(active_rows))
            if corrected:
                self._host_log("Selection", "warning", "selection corrected after ROWS change")
            return selection.to_payload()

    def discovery_payload(self) -> dict[str, Any]:
        return {
            "bleState": self._discovery_state["ble"],
            "bleResults": list(self._ble_scan_results),
            "wifiState": self._discovery_state["wifi"],
            "wifiResults": list(self._wifi_scan_results),
        }

    def scan_ble_once(self, timeout_seconds: float = 10.0) -> list[dict[str, Any]]:
        if self._ble_scan_disabled():
            message = "BLE scan is disabled while connected; disconnect first."
            self._discovery_state["ble"] = message
            self._host_log("Discovery", "warning", message)
            raise RuntimeError(message)
        self._discovery_state["ble"] = "scanning"
        try:
            self._ble_scan_results = [asdict(item) for item in scan_ble(timeout_seconds)]
            diagnostic_error = _ble_diagnostic_error(self._ble_scan_results)
            if diagnostic_error:
                self._discovery_state["ble"] = f"failed: {diagnostic_error}"
                self._host_log("Discovery", "error", f"BLE scan failed: {diagnostic_error}")
            else:
                self._discovery_state["ble"] = f"found {len(self._ble_scan_results)} devices"
                self._host_log("Discovery", "info", f"BLE scan found {len(self._ble_scan_results)} devices")
            return list(self._ble_scan_results)
        except Exception as exc:
            self._discovery_state["ble"] = f"failed: {exc}"
            self._host_log("Discovery", "error", f"BLE scan failed: {exc}")
            raise

    def write_to_active_transport(
        self,
        text: str,
        line_ending: str = "lf",
        encoding: str = "utf-8",
        mode: str = "text",
        hex_text: str | None = None,
    ) -> dict[str, Any]:
        try:
            payload = self._encode_write_payload(text, line_ending, encoding, mode, hex_text)
            result = self.transport.write(payload)
            transport = str(result.get("transport", "unknown"))
            bytes_written = int(result.get("bytesWritten", 0))
            self.commandLineEnding = str(line_ending or "lf")
            self._host_log("Commands", "info", f"CMD_TX,mode={transport},bytes={bytes_written},ending={line_ending}")
            return {"ok": True, "transport": transport, "bytesWritten": bytes_written}
        except Exception as exc:
            transport = str(self.transport.status.get("transport", "none"))
            self._host_log("Commands", "error", f"CMD_TX_FAIL,mode={transport},error={_truncate(str(exc), 120)}")
            return {"ok": False, "error": str(exc), "transport": transport, "bytesWritten": 0}

    def scan_wifi_once(self, subnet: str | None = None) -> list[dict[str, Any]]:
        self._discovery_state["wifi"] = "discovering"
        try:
            self._wifi_scan_results = [asdict(item) for item in scan_wifi(subnet)]
            self._discovery_state["wifi"] = f"found {len(self._wifi_scan_results)} devices"
            self._host_log("Discovery", "info", f"Wi-Fi discovery found {len(self._wifi_scan_results)} candidates")
            return list(self._wifi_scan_results)
        except Exception as exc:
            self._discovery_state["wifi"] = f"failed: {exc}"
            self._host_log("Discovery", "error", f"Wi-Fi discovery failed: {exc}")
            raise

    def baseline_payload(self) -> dict[str, Any]:
        session = self._baseline_session
        now_ns = time.monotonic_ns()
        return {
            "ok": True,
            "status": self.baseline_status_code(),
            "label": self.ui.baselineStatus,
            "invalidReason": self.ui.baselineInvalidReason,
            "progress": session.progress(now_ns) if session else 0.0,
            "ready": self.ui.baseline is not None,
            "validCells": int(self.ui.baseline.validMask.sum()) if self.ui.baseline else 0,
            "frameCount": session.frameCount if session else (self.ui.baseline.frameCount if self.ui.baseline else 0),
            "rejectedFrameCount": session.rejectedFrameCount if session else (self.ui.baseline.rejectedFrameCount if self.ui.baseline else 0),
            "pendingDisplayMode": self.ui.pendingDisplayMode.value if self.ui.pendingDisplayMode else None,
        }

    def display_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "displayMode": self.ui.displayMode.value,
            "pendingDisplayMode": self.ui.pendingDisplayMode.value if self.ui.pendingDisplayMode else None,
            "measurementDomain": self.ui.measurementDomain,
            "showCellText": self.ui.cellText,
            "pauseDisplay": self.ui.paused,
            "freezeColor": self.ui.freezeColor,
            "unitMode": self.ui.unitMode,
            "voltageReference": self.ui.voltageReference,
            "circuitOffsetPf": self.ui.circuitOffsetPf,
            "trendLatestN": self.ui.trendLatestN,
            "baselineStatus": self.baseline_payload(),
        }

    def set_trend_latest_n(self, latest_n: int) -> dict[str, Any]:
        latest = int(latest_n)
        if latest <= 0:
            latest = self.matrixStore.history.capacity
        latest = min(max(latest, 1), self.matrixStore.history.capacity)
        self.ui.trendLatestN = latest
        return history_payload(self, latest_n=latest)

    def offsets_payload(self) -> list[list[float]]:
        return super().offsets_payload()

    def get_offsets_payload(self) -> dict[str, Any]:
        return {"ok": True, "offsetsPf": self.offsets_payload()}

    def set_offset_cell(self, row: int, col: int, offset_pf: float) -> dict[str, Any]:
        row_index, col_index = _cell_indices(row, col)
        offsets = self.user_offsets_array()
        offsets[row_index, col_index] = _finite_float(offset_pf, "offsetPf")
        self._commit_offsets(offsets, "cell offset changed")
        return self.get_offsets_payload()

    def set_offsets_bulk(self, offsets_pf: list[list[float]]) -> dict[str, Any]:
        offsets = _validate_offsets_matrix(offsets_pf)
        self._commit_offsets(offsets, "cell offset changed")
        return self.get_offsets_payload()

    def clear_offsets(self, scope: str, row: int | None = None, col: int | None = None) -> dict[str, Any]:
        offsets = self.user_offsets_array()
        normalized_scope = _normalize_scope(scope)
        if normalized_scope == "all":
            offsets[:, :] = 0.0
        elif normalized_scope == "row":
            if row is None:
                raise ValueError("row is required for row scope")
            row_index = _row_index(row)
            offsets[row_index, :] = 0.0
        else:
            if row is None or col is None:
                raise ValueError("row and col are required for cell scope")
            row_index, col_index = _cell_indices(row, col)
            offsets[row_index, col_index] = 0.0
        self._commit_offsets(offsets, "cell offset changed")
        return self.get_offsets_payload()

    def zero_current_offsets(self, scope: str, row: int | None = None, col: int | None = None) -> dict[str, Any]:
        snapshot = self.matrixStore.snapshot()
        cap_rows = np.asarray([mode == "CAP" for mode in snapshot.rowModes], dtype=bool)
        active_rows = np.arange(8) < int(snapshot.activeRows)
        if not bool(np.any(cap_rows & active_rows)):
            raise ValueError("Capacitance offsets are available when an active row uses CAP.")
        corrected = np.asarray(snapshot.matrix, dtype=np.float64)
        valid = (
            np.asarray(snapshot.valid, dtype=bool)
            & np.asarray(snapshot.fresh, dtype=bool)
            & ~np.asarray(snapshot.error, dtype=bool)
            & np.isfinite(corrected)
            & cap_rows.reshape(8, 1)
            & active_rows.reshape(8, 1)
        )
        if snapshot.seq is None:
            raise ValueError("no capacitance frame yet")
        offsets = self.user_offsets_array()
        normalized_scope = _normalize_scope(scope)
        changed = 0
        if normalized_scope == "all":
            mask = valid
            offsets[mask] = corrected[mask]
            changed = int(mask.sum())
        elif normalized_scope == "row":
            if row is None:
                raise ValueError("row is required for row scope")
            row_index = _row_index(row)
            if not cap_rows[row_index] or not active_rows[row_index]:
                raise ValueError("selected row is not an active capacitance row")
            mask = valid[row_index, :]
            offsets[row_index, mask] = corrected[row_index, mask]
            changed = int(mask.sum())
        else:
            if row is None or col is None:
                raise ValueError("row and col are required for cell scope")
            row_index, col_index = _cell_indices(row, col)
            if not cap_rows[row_index] or not active_rows[row_index]:
                raise ValueError("selected cell is not in an active capacitance row")
            if not bool(valid[row_index, col_index]):
                raise ValueError("selected cell has no valid current value")
            offsets[row_index, col_index] = corrected[row_index, col_index]
            changed = 1
        self._commit_offsets(offsets, "cell offset changed")
        payload = self.get_offsets_payload()
        payload["changedCells"] = changed
        return payload

    def export_session_payload(self) -> dict[str, Any]:
        snap = snapshot_payload(self)
        history = self._history_export_frames()
        logs = self.rawLogs.snapshot(show_data=True, limit=self.rawLogs.maxLines)
        session_id = self.recorder.sessionId or self._exportSessionId
        for frame in history:
            if not frame.get("sessionId"):
                frame["sessionId"] = session_id
        return {
            "metadata": {
                "appVersion": APP_VERSION,
                "schemaVersion": 3,
                "sessionId": session_id,
                "exportedAt": datetime.now(timezone.utc).isoformat(),
                "sourceTransport": snap["connection"]["mode"],
                "device": snap["connection"]["deviceLabel"],
                "frameSeq": snap["frame"]["seq"],
                "fps": snap["frame"]["fps"],
                "rows": snap["frame"]["rows"],
                "measurementMode": snap["measurement"]["appliedMode"],
                "quantity": snap["matrix"]["quantity"],
                "unit": snap["matrix"]["wireUnit"],
                "scale": snap["matrix"]["scale"],
            },
            "display": snap["display"],
            "offsetsPf": self.offsets_payload(),
            "currentMatrix": snap["matrix"],
            "selection": snap["selection"],
            "history": history_payload(self, latest_n=self.matrixStore.history.capacity),
            "historyFrames": history,
            "rawLogs": logs["rows"],
            "events": list(self.deviceInfo.get("lifecycleEvents") or []),
            "parsedStatus": [],
            "diagnostics": snap["diagnostics"],
        }

    def export_session_file(self, fmt: str) -> tuple[bytes, str, str]:
        return export_session_bytes(self.export_session_payload(), fmt)

    def import_session_file(self, path: str) -> dict[str, Any]:
        frames = load_session_frames(path)
        if not frames:
            raise ValueError("session contains no measurement frames")
        # Import is an offline replay boundary, not a live command response.
        # Disconnect and reset every session-scoped state machine atomically so
        # a pending MACK/RAIL/ROWS transaction cannot leave the imported matrix
        # disagreeing with measurement.appliedMode.
        self.disconnect()
        import_session_generation = int(self.transport.status.get("sessionGeneration", 0) or 0)
        self.registry.reset_session()
        self.commands.reset_session(import_session_generation)
        self.telemetry.reset()
        self.matrixStore.reset_session()
        imported_frames = 0
        for frame in frames:
            # Re-enter imported data through the exact current ASCII parser so
            # import and Replay exercise the same C/V/R, mask, Xhh, PGA and
            # CRC semantics as live transports.  The exported mode is an
            # authoritative offline-session boundary; it is not an optimistic
            # hardware mode transition.
            payload = frames_to_measurement_ascii_bytes([frame])
            envelope = TransportEnvelope(
                source="replay",
                channel="data",
                deviceId=str(Path(path)),
                sessionGeneration=import_session_generation,
                receivedMonotonicNs=time.monotonic_ns(),
                receivedWallTime=time.time(),
                rawPayload=payload,
            )
            parsed_frames = [
                event
                for event in self.registry.feed(envelope)
                if isinstance(event, (CapacitanceFrame, MeasurementFrame, MixedMeasurementFrame))
            ]
            if len(parsed_frames) != 1:
                raise ValueError(f"session frame {frame.seq} did not produce exactly one valid measurement frame")
            parsed = parsed_frames[0]
            if isinstance(parsed, MixedMeasurementFrame):
                configured = frame.configuredRowProfile or tuple(
                    mode if mode != "NONE" else "CAP" for mode in frame.row_mode_values()
                )
                # Offline import is an authoritative replay boundary. It has
                # no live STATE? transaction, so install the identities
                # carried by the verified M/MR/K frame before admitting it.
                self.commands.resync_authoritative(
                    mode=self.commands.appliedMode,
                    rows=parsed.rows,
                    row_modes=configured,
                    profile_generation=parsed.profileGeneration,
                    profile_request_id=parsed.profileRequestId,
                )
                self.matrixStore.complete_resync(
                    mode=self.commands.appliedMode,
                    rows=parsed.rows,
                    row_modes=configured,
                    rows_generation=parsed.rowsGeneration,
                    rows_request_id=parsed.rowsRequestId,
                    rows_frame_seq=parsed.seq,
                    mode_generation=None,
                    mode_request_id=None,
                    profile_generation=parsed.profileGeneration,
                    profile_request_id=parsed.profileRequestId,
                )
            else:
                mode = "CAP" if isinstance(parsed, CapacitanceFrame) else parsed.mode
                configured = frame.configuredRowProfile or (mode,) * 8
                mode_generation = None if isinstance(parsed, CapacitanceFrame) else parsed.generation
                mode_request_id = None if isinstance(parsed, CapacitanceFrame) else parsed.requestId
                rows_generation = parsed.generation if isinstance(parsed, CapacitanceFrame) else frame.rowsGeneration
                rows_request_id = parsed.requestId if isinstance(parsed, CapacitanceFrame) else frame.rowsRequestId
                self.commands.resync_authoritative(
                    mode=mode,
                    rows=parsed.rows,
                    row_modes=configured,
                    mode_generation=mode_generation,
                    mode_request_id=mode_request_id,
                    profile_generation=frame.profileGeneration,
                    profile_request_id=frame.profileRequestId,
                )
                self.matrixStore.complete_resync(
                    mode=mode,
                    rows=parsed.rows,
                    row_modes=configured,
                    rows_generation=rows_generation,
                    rows_request_id=rows_request_id,
                    rows_frame_seq=parsed.seq,
                    mode_generation=mode_generation,
                    mode_request_id=mode_request_id,
                    profile_generation=frame.profileGeneration,
                    profile_request_id=frame.profileRequestId,
                )
            self._handle_event(parsed)
            imported_frames += 1
        self.selectedMode = "replay"
        self.replayPath = str(Path(path))
        self._host_log("Replay", "info", f"Imported session data: {Path(path).name}")
        return {
            "ok": True,
            "path": str(Path(path)),
            "frames": imported_frames,
            "rows": frames[-1].rows,
            "measurementMode": frames[-1].mode,
        }

    def setup_profile_payload(self) -> dict[str, Any]:
        return {
            "schemaVersion": 3,
            "appVersion": APP_VERSION,
            "transport": {
                "mode": self.selectedMode,
                "serial": {"port": self.serialPort, "baud": self.serialBaud},
                "wifi": {"host": self.wifiHost, "fallbackHost": self.wifiFallbackHost},
                "ble": {"address": self.bleAddress, "deviceId": self.bleDeviceId},
                "replay": {"path": self.replayPath or "", "speed": self.replaySpeed},
            },
            "acquisition": {
                "rows": self.preferredRows,
                "measurementMode": self.preferredMeasurementMode,
                "rowModes": list(self.preferredRowModes),
            },
            "voltageRail": {
                "measuredAvddV": self.measuredAvddV,
                "measuredAvssV": self.measuredAvssV,
            },
            "display": {
                "displayMode": self.ui.displayMode.value,
                "measurementDomain": self.ui.measurementDomain,
                "showCellText": self.ui.cellText,
                "pauseDisplay": self.ui.paused,
                "freezeColor": self.ui.freezeColor,
                "unitMode": self.ui.unitMode,
                "voltageReference": self.ui.voltageReference,
                "circuitOffsetPf": self.ui.circuitOffsetPf,
                "trendLatestN": self.ui.trendLatestN,
            },
            "offsetsPf": self.offsets_payload(),
            "lifecycle": {
                "autoReconnect": self.autoReconnect,
                "resumeMeasurementAfterDeviceRestart": self.resumeMeasurementAfterDeviceRestart,
                "preferredUsbStream": self.preferredUsbStream,
            },
            "command": {"lineEnding": self.commandLineEnding},
            "paths": {"defaultSaveDirectory": self.defaultSaveDirectory},
        }

    def apply_setup_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        warnings: list[str] = []
        if int(payload.get("schemaVersion", 1)) not in {1, 2, 3}:
            raise ValueError("unsupported setup profile schemaVersion")
        transport = payload.get("transport") if isinstance(payload.get("transport"), dict) else {}
        serial = transport.get("serial") if isinstance(transport.get("serial"), dict) else {}
        wifi = transport.get("wifi") if isinstance(transport.get("wifi"), dict) else {}
        ble = transport.get("ble") if isinstance(transport.get("ble"), dict) else {}
        replay = transport.get("replay") if isinstance(transport.get("replay"), dict) else {}
        if "mode" in transport and transport["mode"]:
            self.set_transport_mode(str(transport["mode"]))
        if "port" in serial:
            self.serialPort = str(serial.get("port") or "")
        if "baud" in serial:
            baud = int(serial.get("baud") or 0)
            if baud <= 0:
                raise ValueError("transport.serial.baud must be positive")
            self.serialBaud = baud
        if "host" in wifi:
            self.wifiHost = str(wifi.get("host") or "")
        if "fallbackHost" in wifi:
            self.wifiFallbackHost = str(wifi.get("fallbackHost") or "")
        if "address" in ble:
            self.bleAddress = str(ble.get("address") or "")
        if "deviceId" in ble:
            self.bleDeviceId = str(ble.get("deviceId") or "")
        if "path" in replay:
            self.replayPath = str(replay.get("path") or "") or None
        if "speed" in replay:
            self.replaySpeed = max(0.01, float(replay.get("speed") or 1.0))
        display = payload.get("display") if isinstance(payload.get("display"), dict) else {}
        if display:
            self.update_display_settings(display)
            if "trendLatestN" in display and display["trendLatestN"] is not None:
                self.ui.trendLatestN = max(1, int(display["trendLatestN"]))
        offsets = payload.get("offsetsPf")
        if offsets is not None:
            self.set_offsets_bulk(offsets)
        command = payload.get("command") if isinstance(payload.get("command"), dict) else {}
        if "lineEnding" in command:
            ending = str(command.get("lineEnding") or "lf")
            if ending not in _LINE_ENDINGS:
                raise ValueError("command.lineEnding must be lf, crlf, or none")
            self.commandLineEnding = ending
        paths = payload.get("paths") if isinstance(payload.get("paths"), dict) else {}
        if "defaultSaveDirectory" in paths:
            directory = str(paths.get("defaultSaveDirectory") or "").strip()
            if not directory:
                raise ValueError("paths.defaultSaveDirectory must not be empty")
            self.defaultSaveDirectory = directory
        lifecycle = payload.get("lifecycle") if isinstance(payload.get("lifecycle"), dict) else {}
        if lifecycle:
            self.update_lifecycle_settings(lifecycle)
        acquisition = payload.get("acquisition") if isinstance(payload.get("acquisition"), dict) else {}
        if "measurementMode" in acquisition:
            requested_mode = str(acquisition.get("measurementMode") or "CAP").upper()
            if requested_mode not in {"CAP", "VOLT", "RES"}:
                raise ValueError("acquisition.measurementMode must be CAP, VOLT, or RES")
            self.preferredMeasurementMode = requested_mode
        row_modes = acquisition.get("rowModes")
        if row_modes is None:
            # Schema 1/2 migration: the old global preference described every
            # row. Do not make legacy profiles fail to open.
            row_modes = [self.preferredMeasurementMode] * 8
        normalized_row_modes = _normalize_row_modes(row_modes)
        self.preferredRowModes = normalized_row_modes
        voltage_rail = payload.get("voltageRail") if isinstance(payload.get("voltageRail"), dict) else {}
        if voltage_rail.get("measuredAvddV") is not None:
            self.measuredAvddV = _finite_float(voltage_rail["measuredAvddV"], "voltageRail.measuredAvddV")
        if voltage_rail.get("measuredAvssV") is not None:
            self.measuredAvssV = _finite_float(voltage_rail["measuredAvssV"], "voltageRail.measuredAvssV")
        if "rows" in acquisition:
            rows = int(acquisition.get("rows") or 0)
            if not (1 <= rows <= 8):
                raise ValueError("acquisition.rows must be 1..8")
            self.preferredRows = rows
        active_transport = str(self.transport.status.get("transport", "none"))
        if active_transport in {"serial", "ble", "wifi"}:
            self._start_preference_apply(
                "explicit_setup_profile",
                restore_measurement=True,
                target_boot_id=self.commands.bootId,
            )
            if self._preferenceApplyState["state"] == "WAITING_BOOTSTRAP":
                warnings.append("measurement preferences stored and will apply in order after bootstrap")
        else:
            # A profile remains a host preference while offline/replaying.
            # Update only display geometry; no synthetic firmware transaction
            # is created and no command is queued for a future connection.
            self.matrixStore.set_active_rows_for_display(self.preferredRows)
            self.commands.requestedRows = self.preferredRows
            self.commands.activeRows = self.preferredRows
            self.commands.pendingRows = None
        return {"ok": True, "profile": self.setup_profile_payload(), "warnings": warnings}

    def _on_bootstrap_complete(self, _status: Any) -> None:
        transition = self.lastBootTransition
        boot_id = self.commands.bootId
        existing_target = self._preferenceApplyState.get("targetBootId")
        interrupted_apply = existing_target is not None and existing_target == boot_id and self._preferenceApplyState.get("state") not in {
            "COMPLETE", "DISABLED", "IDLE"
        }
        if transition.get("bootChanged") and self.resumeMeasurementAfterDeviceRestart:
            self._start_preference_apply("device_reboot", restore_measurement=True, target_boot_id=boot_id)
            return
        if interrupted_apply:
            self._preferenceApplyState["state"] = "READY"
            self._advance_preference_apply()
            return
        if transition.get("bootChanged") and not self.resumeMeasurementAfterDeviceRestart:
            self._preferenceApplyState = {
                "state": "DISABLED",
                "reason": "resume_after_device_restart_disabled",
                "targetBootId": boot_id,
                "restoreMeasurement": False,
                "error": "",
                "commands": [],
            }
        if self.preferredUsbStream != "DEVICE_DEFAULT" and self.synchronizer.status.source == "serial":
            self._start_preference_apply("safe_usb_preference", restore_measurement=False, target_boot_id=boot_id)

    def _start_preference_apply(
        self,
        reason: str,
        *,
        restore_measurement: bool,
        target_boot_id: int | None = None,
    ) -> None:
        self._preferenceApplyState = {
            "state": "READY",
            "reason": str(reason),
            "targetBootId": self.commands.bootId if target_boot_id is None else target_boot_id,
            "restoreMeasurement": bool(restore_measurement),
            "error": "",
            "commands": [],
        }
        self._advance_preference_apply()

    def _advance_preference_apply(self) -> None:
        state = str(self._preferenceApplyState.get("state", "IDLE"))
        if state in {"IDLE", "DISABLED", "COMPLETE", "ERROR"}:
            return
        if self.synchronizer.status.state != "SYNCED" or not self.commands.authoritativeStateKnown:
            self._preferenceApplyState["state"] = "WAITING_BOOTSTRAP"
            return
        if self.commands.pendingRows is not None or self.commands.pendingRowModes is not None or self.commands.pendingMode is not None:
            self._preferenceApplyState["state"] = "WAITING_TRANSACTION"
            return
        try:
            if self._preferenceApplyState.get("restoreMeasurement"):
                if self.commands.activeRows != self.preferredRows:
                    self.request_rows(self.preferredRows)
                    self._preferenceApplyState["state"] = "WAITING_ROWS"
                    self._preferenceApplyState["commands"].append(f"ROWS={self.preferredRows}")
                    return
                if tuple(self.commands.appliedRowModes) != tuple(self.preferredRowModes):
                    encoded = "".join({"CAP": "C", "VOLT": "V", "RES": "R"}[mode] for mode in self.preferredRowModes)
                    self.request_row_modes_api(self.preferredRowModes)
                    self._preferenceApplyState["state"] = "WAITING_ROW_MODES"
                    self._preferenceApplyState["commands"].append(f"ROWMODES={encoded}")
                    return
            if self.preferredUsbStream != "DEVICE_DEFAULT" and self.synchronizer.status.source == "serial":
                current_usb = self.deviceInfo.get("usbStream") or (
                    asdict(self.synchronizer.usbStream) if self.synchronizer.usbStream is not None else {}
                )
                if str(current_usb.get("mode", "")).upper() != self.preferredUsbStream:
                    self.request_usb_stream(self.preferredUsbStream)
                    self._preferenceApplyState["state"] = "WAITING_USB_STREAM"
                    self._preferenceApplyState["commands"].append(f"USBSTREAM={self.preferredUsbStream}")
                    return
            self._preferenceApplyState["state"] = "COMPLETE"
        except Exception as exc:
            self._preferenceApplyState["state"] = "ERROR"
            self._preferenceApplyState["error"] = str(exc)
            self._host_log("Preferences", "error", f"Ordered preference restore failed: {exc}")

    def _after_configuration_event(self, event: Any) -> None:
        state = str(self._preferenceApplyState.get("state", "IDLE"))
        if state in {"IDLE", "DISABLED", "COMPLETE", "ERROR", "WAITING_BOOTSTRAP"}:
            return
        if isinstance(event, UsbStreamInfo):
            if state != "WAITING_USB_STREAM" or event.mode != self.preferredUsbStream:
                return
            self._advance_preference_apply()
            return
        if isinstance(event, CommandTransactionEvent):
            if str(event.phase).lower() in {"failed", "rejected", "error"} and str(event.commandType).lower() in {
                "rows", "row_modes"
            }:
                self._preferenceApplyState["state"] = "ERROR"
                self._preferenceApplyState["error"] = str(event.error or event.state or "firmware rejected preference")
                return
            if str(event.phase).lower() != "applied":
                return
        elif not isinstance(event, CommandApplied):
            return
        self._advance_preference_apply()

    def _commit_offsets(self, offsets: np.ndarray, reason: str) -> None:
        current = self.user_offsets_array()
        if np.array_equal(current, offsets):
            return
        self.ui.userOffsetsPf = [[float(value) for value in row] for row in offsets.reshape(8, 8)]
        if self.ui.baseline is not None or self._baseline_session is not None:
            self.invalidate_baseline(reason)
            self._host_log("Display", "warning", f"Baseline invalid: {reason}")

    def _history_export_frames(self) -> list[dict[str, Any]]:
        history = self.matrixStore.history
        frames: list[dict[str, Any]] = []
        for index in history.ordered_indices():
            values = np.asarray(history.values[index, :], dtype=np.float64)
            frames.append(
                {
                    "seq": int(history.seq[index]),
                    "timeSeconds": _json_number(history.timeSeconds[index]),
                    "deviceTimestampUs": _none_if_negative(history.deviceTimestampUs[index]),
                    "hostWallTime": _json_number(history.hostWallTimes[index]),
                    "hostReceivedUtc": (
                        datetime.fromtimestamp(float(history.hostWallTimes[index]), timezone.utc).isoformat()
                        if np.isfinite(history.hostWallTimes[index])
                        else ""
                    ),
                    "hostReceivedMonotonicNs": _none_if_negative(history.hostMonotonicNs[index]),
                    "rows": int(history.rows[index]),
                    "measurementMode": str(history.modes[index]),
                    "frameKind": str(history.modes[index]),
                    "unit": str(history.units[index]),
                    "scale": int(history.scales[index]),
                    "physicalValues": [_json_number(value) for value in values],
                    "valuesPf": [_json_number(value) for value in values] if history.modes[index] == "CAP" else [None] * 64,
                    "rawFixed": [_json_number(value) for value in history.rawFixed[index, :]],
                    "valid": [bool(value) for value in history.valid[index, :].tolist()],
                    "fresh": [bool(value) for value in history.fresh[index, :].tolist()],
                    "freshKnown": [bool(value) for value in history.freshKnown[index, :].tolist()],
                    "expected": [bool(value) for value in history.expected[index, :].tolist()],
                    "expectedKnown": [bool(value) for value in history.expectedKnown[index, :].tolist()],
                    "acquired": [bool(value) for value in history.acquired[index, :].tolist()],
                    "acquiredKnown": [bool(value) for value in history.acquiredKnown[index, :].tolist()],
                    "error": [bool(value) for value in history.error[index, :].tolist()],
                    "errorCodes": [None if int(value) < 0 else int(value) for value in history.errorCodes[index, :]],
                    "errorReasons": [str(value) for value in history.errorReasons[index, :]],
                    "pga": [None if int(value) < 0 else int(value) for value in history.pga[index, :]],
                    "pgaBypass": [bool(value) for value in history.pgaBypass[index, :].tolist()],
                    "generation": None if int(history.generations[index]) < 0 else int(history.generations[index]),
                    "requestId": None if int(history.requestIds[index]) < 0 else int(history.requestIds[index]),
                    "connectionGeneration": int(history.connectionGenerations[index]),
                    "bootId": _none_if_negative(history.bootIds[index]),
                    "rowsGeneration": _none_if_negative(history.rowsGenerations[index]),
                    "rowsRequestId": _none_if_negative(history.rowsRequestIds[index]),
                    "modeGeneration": _none_if_negative(history.modeGenerations[index]),
                    "modeRequestId": _none_if_negative(history.modeRequestIds[index]),
                    "profileGeneration": _none_if_negative(history.profileGenerations[index]),
                    "profileRequestId": _none_if_negative(history.profileRequestIds[index]),
                    "configuredRowProfile": [str(value) for value in history.rowModes[index, :]],
                    "wireRowProfile": str(history.wireProfiles[index] or "") or None,
                    "rowModes": [str(value) for value in history.rowModes[index, :]],
                    "rowUnits": [str(value) for value in history.rowUnits[index, :]],
                    "rowScales": [int(value) for value in history.rowScales[index, :]],
                    "rail": {
                        "railValid": bool(history.railValid[index]),
                        "railFresh": bool(history.railFresh[index]),
                        "railAge": _none_if_negative(history.railAge[index]),
                        "avddUv": _none_if_negative(history.avddUv[index]),
                        "avssUv": _none_if_signed_missing(history.avssUv[index]),
                        "railSpanUv": _none_if_negative(history.railSpanUv[index]),
                        "railSource": str(history.railSource[index]),
                        "railReason": str(history.railReason[index]),
                        "bootId": _none_if_negative(history.bootIds[index]),
                    },
                    "source": str(history.sources[index] or "history"),
                }
            )
        return frames

    def _session_frame_to_capacitance(self, frame: SessionFrame):
        from sensorarray_app.domain.models import CapacitanceFrame

        rows = max(1, min(8, int(frame.rows)))
        cells = rows * 8
        values = np.asarray(frame.valuesPf, dtype=np.float64).reshape(64)
        valid = np.asarray(frame.valid, dtype=bool).reshape(64) & np.isfinite(values)
        corrected = values[:cells].copy()
        corrected[~valid[:cells]] = np.nan
        raw_pf = corrected + float(self.ui.circuitOffsetPf)
        raw_fixed = np.full(cells, CAP_INVALID_SENTINEL, dtype=np.int64)
        raw_fixed[valid[:cells]] = np.rint(raw_pf[valid[:cells]] * CAP_FIXED_SCALE).astype(np.int64)
        return CapacitanceFrame(
            seq=int(frame.seq),
            timestampUs=int(float(frame.timeSeconds) * 1_000_000),
            rows=rows,
            cells=cells,
            generation=1,
            requestId=1,
            rowFreshMask=(1 << rows) - 1,
            primaryFreshMask=(1 << rows) - 1,
            secondaryFreshMask=(1 << rows) - 1,
            badStaleCount=0,
            badMixedCount=0,
            badInvalidCount=int((~valid[:cells]).sum()),
            rawFixedValues=raw_fixed,
            rawPfValues=raw_pf,
            correctedPfValues=corrected,
            validMask=valid[:cells],
            sourceTransport="import",
            sessionGeneration=0,
            receivedTime=time.time(),
            receivedMonotonicNs=time.monotonic_ns(),
        )

    def _restore_exported_session_settings(self, replay_path: Path) -> bool:
        if replay_path.suffix.lower() != ".json":
            return False
        try:
            import json

            payload = json.loads(replay_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(payload, dict) or "metadata" not in payload or "currentMatrix" not in payload:
            return False
        offsets = payload.get("offsetsPf")
        if isinstance(offsets, list):
            self.ui.userOffsetsPf = [[float(value) for value in row] for row in _validate_offsets_matrix(offsets).tolist()]
        display = payload.get("display")
        if isinstance(display, dict):
            mode = display.get("displayMode")
            self.ui.displayMode = DisplayMode.ABSOLUTE_C if mode == DisplayMode.DELTA_PERCENT.value else DisplayMode(str(mode or DisplayMode.ABSOLUTE_C.value))
            self.ui.pendingDisplayMode = None
        return True

    def color_range(
        self,
        matrix: np.ndarray,
        valid_mask: np.ndarray | None = None,
        domain: str | None = None,
    ) -> tuple[float | None, float | None]:
        """Compatibility wrapper around the authoritative domain algorithm."""

        if domain is None:
            current = self.matrixStore.snapshot()
            if current.mode == "CAP":
                domain = "cap_delta" if self.ui.displayMode == DisplayMode.DELTA_PERCENT else "cap_absolute"
            elif current.mode == "VOLT":
                domain = "voltage"
            elif current.mode == "RES":
                domain = "resistance"
            else:
                domain = "cap_absolute"
        return self._colour_range_for_domain(domain, matrix, valid_mask)

    def colour_ranges(
        self,
        matrix_snapshot: Any,
        display_matrix: np.ndarray,
        usable_mask: np.ndarray,
    ) -> dict[str, dict[str, float | bool | None]]:
        active_rows = max(1, min(8, int(matrix_snapshot.activeRows)))
        active_mask = np.zeros((8, 8), dtype=bool)
        active_mask[:active_rows, :] = True
        usable = np.asarray(usable_mask, dtype=bool).reshape(8, 8) & active_mask
        row_modes = tuple(matrix_snapshot.rowModes)
        output: dict[str, dict[str, float | bool | None]] = {}
        cap_domain = "cap_delta" if self.ui.displayMode == DisplayMode.DELTA_PERCENT else "cap_absolute"
        for mode, domain in (("CAP", cap_domain), ("VOLT", "voltage"), ("RES", "resistance")):
            mode_mask = np.zeros((8, 8), dtype=bool)
            for row_index in range(active_rows):
                if row_modes[row_index] == mode:
                    mode_mask[row_index, :] = True
            minimum, maximum = self._colour_range_for_domain(domain, display_matrix, usable & mode_mask)
            output[domain] = {
                "min": minimum,
                "max": maximum,
                "frozen": bool(self.ui.freezeColor),
            }
        # Always expose all typed domains; absent domains receive a
        # deterministic cold-start or their own previous nondegenerate range.
        for domain in ("cap_absolute", "cap_delta", "voltage", "resistance"):
            if domain in output:
                continue
            minimum, maximum = self._colour_range_for_domain(domain, np.empty(0), np.empty(0, dtype=bool))
            output[domain] = {"min": minimum, "max": maximum, "frozen": bool(self.ui.freezeColor)}
        return output

    def _colour_range_for_domain(
        self,
        domain: str,
        matrix: np.ndarray,
        valid_mask: np.ndarray | None,
    ) -> tuple[float, float]:
        if domain not in self._lastColourRanges:
            raise ValueError(f"unknown colour domain: {domain}")
        cached = self._lastColourRanges[domain]
        frozen = self._frozenColourRanges[domain]
        if self.ui.freezeColor and frozen != (None, None):
            return float(frozen[0]), float(frozen[1])
        values = np.asarray(matrix, dtype=np.float64)
        valid = np.isfinite(values)
        if valid_mask is not None:
            valid &= np.asarray(valid_mask, dtype=bool).reshape(values.shape)
        finite = values[valid]
        if finite.size:
            minimum = float(np.min(finite))
            maximum = float(np.max(finite))
            if maximum > minimum:
                if domain in {"cap_delta", "voltage"}:
                    extent_minimum = 0.5 if domain == "cap_delta" else 0.001
                    extent = max(abs(minimum), abs(maximum), extent_minimum)
                    result = (-extent, extent)
                else:
                    padding = (maximum - minimum) * 0.02
                    result = (minimum - padding, maximum + padding)
                self._lastColourRanges[domain] = result
                return self._remember_colour_range(domain, result)
        if cached != (None, None):
            result = (float(cached[0]), float(cached[1]))
            return self._remember_colour_range(domain, result)
        value = float(finite[0]) if finite.size else 0.0
        if domain == "cap_delta":
            extent = max(abs(value) * 1.05, 0.5)
            result = (-extent, extent)
            return self._remember_colour_range(domain, result)
        if domain == "voltage":
            extent = max(abs(value) * 1.05, 0.001)
            result = (-extent, extent)
            return self._remember_colour_range(domain, result)
        minimum_extent = 1.0
        if value > 0:
            result = (0.0, max(value * 1.05, minimum_extent))
            return self._remember_colour_range(domain, result)
        if value < 0:
            result = (min(value * 1.05, -minimum_extent), 0.0)
            return self._remember_colour_range(domain, result)
        result = (0.0, minimum_extent)
        return self._remember_colour_range(domain, result)

    def _remember_colour_range(self, domain: str, result: tuple[float, float]) -> tuple[float, float]:
        self._resolvedColourRanges[domain] = result
        if self.ui.freezeColor and self._frozenColourRanges[domain] == (None, None):
            self._frozenColourRanges[domain] = result
        return result

    def _measurement_mode_visual_reset(self) -> None:
        # Ranges are isolated by physical domain, so a mode switch cannot
        # contaminate the next quantity and no global reset is needed.
        return None

    def _correct_selection_locked(self, active_rows: int):
        from sensorarray_app.domain.selection import correct_selection

        selection, corrected = correct_selection(self.ui.selection, active_rows, self.ui.selectionRevision + 1)
        if corrected:
            self.ui.selection = selection
            self.ui.selectionRevision = selection.selectionRevision
        return selection, corrected

    def _ble_scan_disabled(self) -> bool:
        status = self.transport.status
        state = str(status.get("state", "DISCONNECTED")).upper()
        return status.get("transport") == "ble" and state in {"CONNECTING", "CONNECTED", "STREAMING", "RECONNECTING"}

    def _encode_write_payload(
        self,
        text: str,
        line_ending: str,
        encoding: str,
        mode: str,
        hex_text: str | None,
    ) -> bytes:
        selected_mode = str(mode or "text").lower()
        if selected_mode == "hex":
            source = hex_text if hex_text is not None else text
            try:
                return bytes.fromhex(source)
            except ValueError as exc:
                raise ValueError("invalid hex command bytes") from exc
        if selected_mode != "text":
            raise ValueError("mode must be text or hex")
        ending = _LINE_ENDINGS.get(str(line_ending or "lf").lower())
        if ending is None:
            raise ValueError("lineEnding must be lf, crlf, or none")
        try:
            return (str(text) + ending).encode(encoding or "utf-8", errors="strict")
        except LookupError as exc:
            raise ValueError(f"unknown encoding: {encoding}") from exc


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "..."


def _ble_diagnostic_error(results: list[dict[str, Any]]) -> str:
    if len(results) != 1:
        return ""
    candidate = results[0]
    reason = str(candidate.get("reason") or "").strip()
    if candidate.get("address") or not candidate.get("advanced") or not reason:
        return ""
    return reason


def _row_index(row: int) -> int:
    index = int(row) - 1
    if not (0 <= index < 8):
        raise ValueError("row must be 1..8")
    return index


def _cell_indices(row: int, col: int) -> tuple[int, int]:
    row_index = _row_index(row)
    col_index = int(col) - 1
    if not (0 <= col_index < 8):
        raise ValueError("col must be 1..8")
    return row_index, col_index


def _finite_float(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not np.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _validate_offsets_matrix(offsets_pf: list[list[float]]) -> np.ndarray:
    offsets = np.asarray(offsets_pf, dtype=np.float64)
    if offsets.shape != (8, 8):
        raise ValueError("offsetsPf must be an 8x8 matrix")
    if not np.isfinite(offsets).all():
        raise ValueError("offsetsPf must contain only finite numbers")
    return offsets


def _normalize_scope(scope: str) -> str:
    normalized = str(scope or "cell").lower()
    if normalized not in {"cell", "row", "all"}:
        raise ValueError("scope must be cell, row, or all")
    return normalized


def _normalize_row_modes(modes: Any) -> tuple[str, ...]:
    aliases = {"CAPACITANCE": "CAP", "VOLTAGE": "VOLT", "RESISTANCE": "RES"}
    if isinstance(modes, str):
        compact = modes.strip().upper()
        if len(compact) != 8 or not set(compact) <= {"C", "V", "R"}:
            raise ValueError("modes must contain exactly 8 CAP, VOLT, or RES entries")
        modes = [{"C": "CAP", "V": "VOLT", "R": "RES"}[value] for value in compact]
    try:
        normalized = tuple(aliases.get(str(mode).strip().upper(), str(mode).strip().upper()) for mode in modes)
    except TypeError as exc:
        raise ValueError("modes must contain exactly 8 CAP, VOLT, or RES entries") from exc
    if len(normalized) != 8 or any(mode not in {"CAP", "VOLT", "RES"} for mode in normalized):
        raise ValueError("modes must contain exactly 8 CAP, VOLT, or RES entries")
    return normalized


def _json_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def _none_if_negative(value: Any) -> int | None:
    parsed = int(value)
    return None if parsed < 0 else parsed


def _none_if_signed_missing(value: Any) -> int | None:
    parsed = int(value)
    return None if parsed == np.iinfo(np.int64).min else parsed
