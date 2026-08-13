from __future__ import annotations

import time
from typing import Any

import numpy as np

from sensorarray_app.constants import DETECTOR_LABELS, ROW_LABELS
from sensorarray_app.domain.baseline import delta_percent
from sensorarray_app.domain.models import DisplayMode


def websocket_snapshot(runtime) -> dict[str, Any]:
    return {"type": "snapshot", "timeMs": int(time.time() * 1000), "payload": snapshot_payload(runtime)}


def snapshot_payload(runtime) -> dict[str, Any]:
    matrix = runtime.matrixStore.snapshot()
    selection = runtime.current_selection_payload(matrix.activeRows)
    display_matrix = _display_matrix(runtime, matrix)
    usable = np.asarray(matrix.valid, dtype=bool) & np.asarray(matrix.fresh, dtype=bool) & ~np.asarray(matrix.error, dtype=bool)
    active_mask = np.zeros((8, 8), dtype=bool)
    active_mask[: max(1, min(8, int(matrix.activeRows))), :] = True
    usable &= active_mask & np.isfinite(display_matrix)
    colour_ranges = runtime.colour_ranges(matrix, display_matrix, usable)
    primary_domain = _primary_colour_domain(runtime, matrix)
    primary_range = colour_ranges[primary_domain]
    color_min, color_max = primary_range["min"], primary_range["max"]
    transport = dict(runtime.transport.status)
    active_transport = str(transport.get("transport", "none") or "none")
    transport_mode = runtime.selectedMode if active_transport == "none" else active_transport
    diagnostics = runtime.stats.snapshot(0.0)
    diagnostics.update(
        {
            "staleGenerationDrops": runtime.matrixStore.rejectedStaleGeneration,
            "wrongModeDrops": runtime.matrixStore.rejectedWrongMode,
            "preBoundaryDrops": runtime.matrixStore.rejectedBeforeBoundary,
        }
    )
    measurement = runtime.commands.measurement_snapshot()
    rail_telemetry = runtime.telemetry.rail_snapshot(time.time())
    measurement["railTelemetry"] = rail_telemetry
    battery = runtime.telemetry.battery_snapshot(time.time())
    ads = _ads_snapshot(runtime)
    rates = _rate_snapshot(runtime)
    display_unit = "%" if matrix.mode == "CAP" and runtime.ui.displayMode == DisplayMode.DELTA_PERCENT else matrix.unit
    error_codes = _integer_matrix_to_json(matrix.errorCodes, missing=-1)
    pga = _integer_matrix_to_json(matrix.pga, missing=-1)
    return {
        "connection": {
            "transportMode": transport_mode,
            # Compatibility alias for the 1.0 frontend and exported sessions.
            "mode": transport_mode,
            "state": str(transport.get("state", "DISCONNECTED")).lower(),
            "deviceLabel": transport.get("device", ""),
            "generation": int(transport.get("sessionGeneration", 0) or 0),
            "error": transport.get("error", ""),
        },
        "measurement": {"mode": measurement["appliedMode"], **measurement},
        "frame": {
            "seq": matrix.seq,
            # parserFps is host ingest rate, not firmware physical capture FPS.
            "fps": float(diagnostics.get("parserFps", 0.0)),
            "hostParserFps": float(diagnostics.get("parserFps", 0.0)),
            "rows": matrix.activeRows,
            "valid": matrix.seq is not None,
            "timestampUs": matrix.timestampUs,
            "revision": matrix.revision,
            "generation": matrix.firmwareGeneration,
            "requestId": matrix.requestId,
            "layout": matrix.layout,
            "rowModes": list(matrix.rowModes),
            "profileGeneration": matrix.profileGeneration,
            "profileRequestId": matrix.profileRequestId,
        },
        "matrix": {
            "rows": list(ROW_LABELS),
            "cols": list(DETECTOR_LABELS),
            "quantity": matrix.quantity,
            "mode": matrix.mode,
            "unit": display_unit,
            "wireUnit": matrix.unit,
            "scale": matrix.scale,
            "format": matrix.format,
            "values": _matrix_to_json(matrix.matrix),
            "displayValues": _matrix_to_json(display_matrix),
            "rawFixed": _matrix_to_json(matrix.rawFixed),
            "valid": matrix.valid.astype(bool).tolist(),
            "validMask": matrix.valid.astype(bool).tolist(),
            "fresh": matrix.fresh.astype(bool).tolist(),
            "error": matrix.error.astype(bool).tolist(),
            "errorCodes": error_codes,
            "errorReasons": _string_matrix_to_json(matrix.errorReasons),
            "pga": pga,
            "pgaBypass": matrix.pgaBypass.astype(bool).tolist(),
            "sourceTransport": matrix.sourceTransport,
            "generation": matrix.firmwareGeneration,
            "requestId": matrix.requestId,
            "diagnostics": dict(matrix.diagnostics),
            "rawHeader": matrix.rawHeader,
            "rawTrailer": matrix.rawTrailer,
            # CAP compatibility fields are null outside capacitance mode.
            "correctedPf": _matrix_to_json(matrix.correctedPf),
            "rawPf": _matrix_to_json(matrix.rawPf),
            "userOffsetPf": _matrix_to_json(runtime.user_offsets_array()),
            "domain": matrix.domain,
            "modeByRow": list(matrix.rowModes),
            "unitByRow": list(matrix.rowUnits),
            "scaleByRow": list(matrix.rowScales),
            "capValues": _matrix_to_json(matrix.capValues),
            "voltValues": _matrix_to_json(matrix.voltValues),
            "resValues": _matrix_to_json(matrix.resValues),
        },
        "capacitance": {
            "available": any(mode == "CAP" for mode in matrix.rowModes[: matrix.activeRows]),
            "rawPf": _matrix_to_json(matrix.rawPf),
            "correctedPf": _matrix_to_json(matrix.correctedPf),
            "userOffsetPf": _matrix_to_json(runtime.user_offsets_array()),
            "displayPf": _matrix_to_json(
                np.where(
                    np.asarray([mode == "CAP" for mode in matrix.rowModes], dtype=bool).reshape(8, 1),
                    display_matrix,
                    np.nan,
                )
            ),
            "displayMode": runtime.ui.displayMode.value,
        },
        "selection": selection,
        "display": {
            "displayMode": runtime.ui.displayMode.value,
            "pendingDisplayMode": runtime.ui.pendingDisplayMode.value if runtime.ui.pendingDisplayMode else None,
            "measurementDomain": matrix.quantity,
            "showCellText": runtime.ui.cellText,
            "pauseDisplay": runtime.ui.paused,
            "freezeColor": runtime.ui.freezeColor,
            "unitMode": runtime.ui.unitMode,
            "circuitOffsetPf": runtime.ui.circuitOffsetPf,
            "trendLatestN": runtime.ui.trendLatestN,
            "colorRange": {"min": color_min, "max": color_max, "frozen": runtime.ui.freezeColor},
            "colourRanges": colour_ranges,
        },
        "baseline": runtime.baseline_payload(),
        "commands": runtime.commands.snapshot(),
        "battery": battery,
        "ads": ads,
        "rates": rates,
        "logs": runtime.rawLogs.snapshot(limit=300),
        "discovery": runtime.discovery_payload(),
        "diagnostics": diagnostics,
    }


def _display_matrix(runtime, matrix) -> np.ndarray:
    values = np.asarray(matrix.matrix, dtype=np.float64).copy()
    if matrix.mode not in {"CAP", "MIXED"}:
        # CAP offsets and Delta C/C0 are never applied to voltage/resistance.
        return values
    cap_rows = np.asarray([mode == "CAP" for mode in matrix.rowModes], dtype=bool)
    display = values.copy()
    display[cap_rows, :] -= runtime.user_offsets_array()[cap_rows, :]
    if runtime.ui.displayMode == DisplayMode.DELTA_PERCENT and runtime.ui.baseline is not None:
        delta = delta_percent(display.reshape(64), runtime.ui.baseline).reshape(8, 8)
        display[cap_rows, :] = delta[cap_rows, :]
    return display


def _primary_colour_domain(runtime, matrix) -> str:
    if matrix.mode == "CAP":
        return "cap_delta" if runtime.ui.displayMode == DisplayMode.DELTA_PERCENT else "cap_absolute"
    if matrix.mode == "VOLT":
        return "voltage"
    if matrix.mode == "RES":
        return "resistance"
    for mode in matrix.rowModes[: matrix.activeRows]:
        if mode == "CAP":
            return "cap_delta" if runtime.ui.displayMode == DisplayMode.DELTA_PERCENT else "cap_absolute"
        if mode == "VOLT":
            return "voltage"
        if mode == "RES":
            return "resistance"
    return "cap_absolute"


def _ads_snapshot(runtime) -> dict[str, Any]:
    diagnostics = dict(runtime.commands.adsDiagnostics)
    identity = dict(diagnostics.get("identity") or _latest_log_fields(runtime, "ADS"))
    diagnostics["identity"] = identity
    identity_available = bool(identity)
    chip = str(identity.get("chip", "unknown") or "unknown")
    valid = str(identity.get("valid", "0")) in {"1", "true", "True"}
    diagnostics["chip"] = chip
    diagnostics["valid"] = valid
    diagnostics["identityAvailable"] = identity_available
    diagnostics["identityConfirmed"] = (valid and chip.lower() != "unknown") if identity_available else None
    diagnostics["label"] = (
        f"ADS{chip}"
        if diagnostics["identityConfirmed"]
        else "ADS identity unconfirmed"
        if identity_available
        else "ADS identity not queried"
    )
    return diagnostics


def _rate_snapshot(runtime) -> dict[str, Any]:
    sf50 = _latest_log_fields(runtime, "SF50")
    output_text = str(sf50.get("ofps", ""))
    output_parts = output_text.split("/") if output_text else []
    return {
        "captureFps": _optional_float(sf50.get("cfps")),
        "emittedFps": _optional_float(sf50.get("efps")),
        "serialOutputFps": _optional_float(output_parts[0]) if len(output_parts) > 0 else None,
        "bleOutputFps": _optional_float(output_parts[1]) if len(output_parts) > 1 else None,
        "wifiOutputFps": _optional_float(output_parts[2]) if len(output_parts) > 2 else None,
        "targetFps": _optional_float(sf50.get("target")),
        "hostParserFps": float(runtime.stats.snapshot(0.0).get("parserFps", 0.0)),
    }


def _latest_log_fields(runtime, tag: str) -> dict[str, str]:
    rows = runtime.rawLogs.snapshot(show_data=True, limit=runtime.rawLogs.maxLines).get("rows", [])
    active_generation = int(runtime.transport.status.get("sessionGeneration", 0) or 0)
    for row in reversed(rows):
        if row.get("tag") == tag and int(row.get("sessionGeneration", -1)) == active_generation:
            return dict(row.get("parsedFields") or {})
    return {}


def _matrix_to_json(matrix: np.ndarray) -> list[list[float | None]]:
    output: list[list[float | None]] = []
    for row in np.asarray(matrix):
        output.append([_json_number(value) for value in row])
    return output


def _integer_matrix_to_json(matrix: np.ndarray, missing: int) -> list[list[int | None]]:
    return [[None if int(value) == missing else int(value) for value in row] for row in np.asarray(matrix)]


def _string_matrix_to_json(matrix: np.ndarray) -> list[list[str | None]]:
    return [[str(value) if str(value) else None for value in row] for row in np.asarray(matrix, dtype=object)]


def _json_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def _optional_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None
