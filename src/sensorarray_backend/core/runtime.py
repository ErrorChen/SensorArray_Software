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

_LINE_ENDINGS = {"lf": "\n", "crlf": "\r\n", "none": ""}


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
        if self._ble_scan_disabled():
            message = "BLE scan is disabled while connected; disconnect first."
            self._discovery_state["ble"] = message
            self._host_log("Discovery", "warning", message)
            raise RuntimeError(message)
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

    def color_range(self, matrix: np.ndarray, valid_mask: np.ndarray | None = None) -> tuple[float | None, float | None]:
        if self.ui.freezeColor and self._lastColorRange != (None, None):
            return self._lastColorRange
        valid = np.isfinite(matrix)
        if valid_mask is not None:
            valid &= np.asarray(valid_mask, dtype=bool)
        finite = matrix[valid]
        if finite.size == 0:
            self._lastColorRange = (None, None)
        else:
            minimum = float(np.nanmin(finite))
            maximum = float(np.nanmax(finite))
            if self.ui.displayMode == DisplayMode.DELTA_PERCENT:
                extent = max(abs(minimum), abs(maximum), 0.5)
                self._lastColorRange = (-extent, extent)
            else:
                span = maximum - minimum
                if span == 0:
                    padding = max(abs(minimum) * 0.02, 0.5)
                else:
                    padding = span * 0.02
                self._lastColorRange = (minimum - padding, maximum + padding)
        return self._lastColorRange

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
