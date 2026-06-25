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
    color_min, color_max = runtime.color_range(display_matrix)
    transport = dict(runtime.transport.status)
    diagnostics = runtime.stats.snapshot(0.0)
    return {
        "connection": {
            "mode": runtime.selectedMode,
            "state": str(transport.get("state", "DISCONNECTED")).lower(),
            "deviceLabel": transport.get("device", ""),
            "generation": int(transport.get("sessionGeneration", 0) or 0),
            "error": transport.get("error", ""),
        },
        "frame": {
            "seq": matrix.seq,
            "fps": float(diagnostics.get("parserFps", 0.0)),
            "rows": matrix.activeRows,
            "valid": matrix.seq is not None,
            "timestampUs": matrix.timestampUs,
            "revision": matrix.revision,
        },
        "matrix": {
            "rows": list(ROW_LABELS),
            "cols": list(DETECTOR_LABELS),
            "correctedPf": _matrix_to_json(matrix.matrix),
            "rawPf": _matrix_to_json(matrix.rawPf),
            "rawFixed": _matrix_to_json(matrix.rawFixed),
            "displayValues": _matrix_to_json(display_matrix),
            "validMask": matrix.valid.astype(bool).tolist(),
            "unit": "%" if runtime.ui.displayMode == DisplayMode.DELTA_PERCENT else "pF",
            "domain": matrix.domain,
        },
        "selection": selection,
        "display": {
            "displayMode": runtime.ui.displayMode.value,
            "measurementDomain": runtime.ui.measurementDomain,
            "showCellText": runtime.ui.cellText,
            "pauseDisplay": runtime.ui.paused,
            "freezeColor": runtime.ui.freezeColor,
            "unitMode": runtime.ui.unitMode,
            "circuitOffsetPf": runtime.ui.circuitOffsetPf,
            "colorRange": {"min": color_min, "max": color_max, "frozen": runtime.ui.freezeColor},
        },
        "baseline": runtime.baseline_payload(),
        "commands": runtime.commands.snapshot(),
        "logs": runtime.rawLogs.snapshot(limit=300),
        "discovery": runtime.discovery_payload(),
        "diagnostics": diagnostics,
    }


def _display_matrix(runtime, matrix) -> np.ndarray:
    if runtime.ui.displayMode == DisplayMode.DELTA_PERCENT and runtime.ui.baseline is not None:
        flat = delta_percent(matrix.matrix.reshape(64), runtime.ui.baseline)
        return flat.reshape(8, 8)
    return matrix.matrix.copy()


def _matrix_to_json(matrix: np.ndarray) -> list[list[float | None]]:
    output: list[list[float | None]] = []
    for row in np.asarray(matrix):
        output.append([_json_number(value) for value in row])
    return output


def _json_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number
