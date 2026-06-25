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


def history_payload(runtime, latest_n: int = 600) -> dict:
    selection = runtime.ui.selection
    cells = list(selection.cells)
    indices = [cell_index(cell) for cell in cells]
    history = runtime.matrixStore.history.slice(indices, latest_n=latest_n)
    values = history.values.copy()
    valid = history.valid.copy()
    unit = "pF"
    if runtime.ui.displayMode.value == "delta_percent" and runtime.ui.baseline is not None:
        unit = "%"
        all_indices = runtime.matrixStore.history.ordered_indices()
        if latest_n > 0:
            all_indices = all_indices[-latest_n:]
        full_values = runtime.matrixStore.history.values[all_indices, :].copy()
        delta_values = np.vstack([delta_percent(row, runtime.ui.baseline) for row in full_values]) if full_values.size else full_values
        values = delta_values[:, indices]
        baseline_valid = runtime.ui.baseline.validMask[indices]
        valid = valid & baseline_valid.reshape(1, len(indices)) & np.isfinite(values)
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
                }
            )
        series.append({"cell": cell, "points": points})
    return {
        "selectionRevision": selection.selectionRevision,
        "title": selection.title,
        "unit": unit,
        "revision": history.revision,
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

