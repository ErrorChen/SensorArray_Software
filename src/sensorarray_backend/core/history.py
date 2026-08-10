from __future__ import annotations

import numpy as np

from sensorarray_app.domain.baseline import delta_percent


def cell_index(cell: str) -> int:
    row_text, detector_text = cell.upper().split("D", maxsplit=1)
    row = int(row_text.removeprefix("S"))
    detector = int(detector_text)
    if not (1 <= row <= 8 and 1 <= detector <= 8):
        raise ValueError("cell must be S1D1..S8D8")
    return (row - 1) * 8 + (detector - 1)


def history_payload(runtime, latest_n: int | None = None) -> dict:
    selection = runtime.ui.selection
    cells = list(selection.cells)
    indices = [cell_index(cell) for cell in cells]
    latest = int(latest_n if latest_n is not None else getattr(runtime.ui, "trendLatestN", 600))
    current = runtime.matrixStore.snapshot()
    mode = current.mode
    history = runtime.matrixStore.history.slice(indices, latest_n=latest, measurement_mode=mode)
    values = history.values.copy()
    valid = history.valid.copy() & history.fresh.copy() & np.isfinite(values)
    unit = current.unit
    if mode == "CAP":
        offsets = runtime.user_offsets_array().reshape(64)
        values = values - offsets[indices].reshape(1, len(indices))
        valid &= np.isfinite(values)
    if mode == "CAP" and runtime.ui.displayMode.value == "delta_percent" and runtime.ui.baseline is not None:
        unit = "%"
        all_indices = runtime.matrixStore.history.ordered_indices()
        all_indices = all_indices[runtime.matrixStore.history.modes[all_indices] == "CAP"]
        if latest > 0:
            all_indices = all_indices[-latest:]
        full_values = runtime.matrixStore.history.values[all_indices, :].copy() - offsets.reshape(1, 64)
        delta_values = np.vstack([delta_percent(row, runtime.ui.baseline) for row in full_values]) if full_values.size else full_values
        values = delta_values[:, indices]
        baseline_valid = runtime.ui.baseline.validMask[indices]
        source_valid = runtime.matrixStore.history.valid[np.ix_(all_indices, indices)].copy()
        source_fresh = runtime.matrixStore.history.fresh[np.ix_(all_indices, indices)].copy()
        valid = source_valid & source_fresh & baseline_valid.reshape(1, len(indices)) & np.isfinite(values)
    series = []
    for column, cell in enumerate(cells):
        points = []
        for index in range(len(history.seq)):
            value = values[index, column]
            points.append(
                {
                    "seq": int(history.seq[index]),
                    "timeSeconds": _json_number(history.timeSeconds[index]),
                    "value": _json_number(value) if bool(valid[index, column]) else None,
                    "rawFixed": _json_number(history.rawFixed[index, column]),
                    "errorCode": None if int(history.errorCodes[index, column]) < 0 else int(history.errorCodes[index, column]),
                    "pga": None if int(history.pga[index, column]) < 0 else int(history.pga[index, column]),
                }
            )
        series.append({"cell": cell, "points": points})
    return {
        "selectionRevision": selection.selectionRevision,
        "title": selection.title if mode == "CAP" else f"{mode} {selection.rowLabel} D{selection.detectorStart}-D{selection.detectorEnd}",
        "mode": mode,
        "quantity": current.quantity,
        "unit": unit,
        "revision": history.revision,
        "latestN": latest,
        "series": series,
    }


def _json_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number
