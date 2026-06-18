from __future__ import annotations

import numpy as np

from sensorarray_app.constants import CAP_FIXED_SCALE, CAP_INVALID_SENTINEL, FDC_CIRCUIT_OFFSET_PF


def fixed_to_pf(
    raw_fixed_values: list[int] | np.ndarray,
    circuit_offset_pf: float = FDC_CIRCUIT_OFFSET_PF,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return raw fixed, raw pF, corrected pF arrays.

    The b41 invalid sentinel is checked before pF conversion and before the
    circuit offset is applied.
    """

    raw_fixed = np.asarray(raw_fixed_values, dtype=np.int64)
    raw_pf = np.full(raw_fixed.shape, np.nan, dtype=np.float64)
    corrected_pf = np.full(raw_fixed.shape, np.nan, dtype=np.float64)
    valid = raw_fixed != CAP_INVALID_SENTINEL
    raw_pf[valid] = raw_fixed[valid].astype(np.float64) / CAP_FIXED_SCALE
    corrected_pf[valid] = raw_pf[valid] - float(circuit_offset_pf)
    return raw_fixed, raw_pf, corrected_pf


def valid_mask_from_fixed(raw_fixed_values: list[int] | np.ndarray) -> np.ndarray:
    return np.asarray(raw_fixed_values, dtype=np.int64) != CAP_INVALID_SENTINEL


def expand_rows_to_matrix(values: np.ndarray, rows: int) -> np.ndarray:
    matrix = np.full((8, 8), np.nan, dtype=np.float64)
    if rows <= 0:
        return matrix
    usable = np.asarray(values, dtype=np.float64)[: rows * 8]
    matrix[:rows, :] = usable.reshape(rows, 8)
    return matrix
