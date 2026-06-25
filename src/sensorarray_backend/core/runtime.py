from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_app.app.runtime import SensorArrayRuntime
from sensorarray_app.constants import DEFAULT_SERIAL_BAUD
from sensorarray_app.domain.models import DisplayMode
from sensorarray_app.services.discovery_service import scan_ble, scan_wifi
from sensorarray_app.transport.serial_transport import SerialTransport


class BackendRuntime(SensorArrayRuntime):
    def __init__(self, config: AppConfiguration):
        super().__init__(config)
        self.selectedMode = "serial"
        self.replayPath: str | None = None
        self.replaySpeed = 1.0
        self._lastColorRange: tuple[float | None, float | None] = (None, None)

    def set_transport_mode(self, mode: str) -> dict[str, str]:
        if mode not in {"serial", "ble", "wifi", "replay"}:
            raise ValueError("mode must be serial, ble, wifi, or replay")
        self.selectedMode = mode
        return {"mode": mode}

    def list_serial_ports(self) -> list[dict[str, str]]:
        return SerialTransport.list_ports()

    def connect_serial(self, port: str, baud: int = DEFAULT_SERIAL_BAUD, auto_reconnect: bool = False) -> None:
        if not str(port or "").strip():
            raise ValueError("serial port is required")
        self.selectedMode = "serial"
        super().connect_serial(str(port).strip(), int(baud or DEFAULT_SERIAL_BAUD), bool(auto_reconnect))

    def connect_ble(self, address: str, device_id: str = "") -> None:
        if not str(address or "").strip():
            raise ValueError("BLE address is required")
        self.selectedMode = "ble"
        super().connect_ble(str(address).strip(), device_id)

    def connect_wifi(self, host: str) -> None:
        if not str(host or "").strip():
            raise ValueError("Wi-Fi host is required")
        self.selectedMode = "wifi"
        super().connect_wifi(str(host).strip())

    def open_replay(self, path: str, speed: float = 1.0) -> dict[str, Any]:
        replay_path = Path(path)
        if not replay_path.exists():
            raise FileNotFoundError(str(replay_path))
        self.replayPath = str(replay_path)
        self.replaySpeed = max(0.01, float(speed or 1.0))
        self.selectedMode = "replay"
        return {"path": self.replayPath, "speed": self.replaySpeed}

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
        transport = self.transport.status.get("transport", "none")
        if transport in {"none", "replay"}:
            self.matrixStore.set_active_rows_for_display(rows)
            self.commands.requestedRows = rows
            self.commands.activeRows = rows
            self.commands.pendingRows = None
            self._host_log("Commands", "info", f"ROWS={rows} display-only ({transport})")
            return {"rows": rows, "displayOnly": True, "applied": transport == "replay"}
        super().request_rows(rows)
        return {"rows": rows, "displayOnly": False, "applied": False}

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
            self.ui.freezeColor = bool(payload["freezeColor"])
        if "unitMode" in payload and payload["unitMode"] is not None:
            self.ui.unitMode = str(payload["unitMode"])
        if "circuitOffsetPf" in payload and payload["circuitOffsetPf"] is not None:
            offset = float(payload["circuitOffsetPf"])
            if offset != self.ui.circuitOffsetPf:
                self.ui.circuitOffsetPf = offset
                self.registry.cap.circuit_offset_pf = offset
                self.invalidate_baseline("circuit offset changed")
        return self.display_payload()

    def set_display_mode(self, mode: str) -> None:
        selected = DisplayMode(mode)
        if selected == DisplayMode.DELTA_PERCENT and self.ui.baseline is None:
            self.capture_baseline()
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
        self._discovery_state["ble"] = "scanning"
        try:
            self._ble_scan_results = [asdict(item) for item in scan_ble(timeout_seconds)]
            self._discovery_state["ble"] = f"found {len(self._ble_scan_results)} devices"
            self._host_log("Discovery", "info", f"BLE scan found {len(self._ble_scan_results)} devices")
            return list(self._ble_scan_results)
        except Exception as exc:
            self._discovery_state["ble"] = f"failed: {exc}"
            self._host_log("Discovery", "error", f"BLE scan failed: {exc}")
            raise

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
            "status": self.ui.baselineStatus,
            "invalidReason": self.ui.baselineInvalidReason,
            "progress": session.progress(now_ns) if session else 0.0,
            "ready": self.ui.baseline is not None,
            "validCells": int(self.ui.baseline.validMask.sum()) if self.ui.baseline else 0,
        }

    def display_payload(self) -> dict[str, Any]:
        return {
            "displayMode": self.ui.displayMode.value,
            "measurementDomain": self.ui.measurementDomain,
            "showCellText": self.ui.cellText,
            "pauseDisplay": self.ui.paused,
            "freezeColor": self.ui.freezeColor,
            "unitMode": self.ui.unitMode,
            "circuitOffsetPf": self.ui.circuitOffsetPf,
        }

    def color_range(self, matrix: np.ndarray) -> tuple[float | None, float | None]:
        if self.ui.freezeColor and self._lastColorRange != (None, None):
            return self._lastColorRange
        finite = matrix[np.isfinite(matrix)]
        if finite.size == 0:
            self._lastColorRange = (None, None)
        else:
            self._lastColorRange = (float(np.nanmin(finite)), float(np.nanmax(finite)))
        return self._lastColorRange

    def _correct_selection_locked(self, active_rows: int):
        from sensorarray_app.domain.selection import correct_selection

        selection, corrected = correct_selection(self.ui.selection, active_rows, self.ui.selectionRevision + 1)
        if corrected:
            self.ui.selection = selection
            self.ui.selectionRevision = selection.selectionRevision
        return selection, corrected
