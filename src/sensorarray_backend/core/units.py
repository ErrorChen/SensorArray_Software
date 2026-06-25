from __future__ import annotations

import math
from typing import Literal

import numpy as np

from sensorarray_app.constants import CAP_FIXED_SCALE, CAP_INVALID_SENTINEL, FDC_CIRCUIT_OFFSET_PF

UnitMode = Literal["auto", "pf", "nf", "uf"]


def fixed_to_pf(
    raw_fixed_values: list[int] | np.ndarray,
    circuit_offset_pf: float = FDC_CIRCUIT_OFFSET_PF,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Convert b41 fixed-point capacitance to raw and corrected pF arrays.

    The invalid sentinel is handled before conversion and before the circuit
    offset is applied, so invalid cells remain NaN instead of being turned into
    plausible numeric data.
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


def auto_unit(values: np.ndarray) -> str:
    finite = np.asarray(values, dtype=np.float64)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return "pF"
    maximum = float(np.nanmax(np.abs(finite)))
    if maximum >= 1_000_000.0:
        return "uF"
    if maximum >= 1_000.0:
        return "nF"
    return "pF"


def scale_pf(value_pf: float | None, unit: str) -> float | None:
    if value_pf is None or not math.isfinite(float(value_pf)):
        return None
    if unit == "uF":
        return float(value_pf) / 1_000_000.0
    if unit == "nF":
        return float(value_pf) / 1_000.0
    return float(value_pf)


def format_engineering_value(value_pf: float | None, unit_mode: UnitMode = "auto") -> tuple[str, str]:
    if value_pf is None or not math.isfinite(float(value_pf)):
        return "NA", "pF"
    unit = auto_unit(np.asarray([value_pf])) if unit_mode == "auto" else {"pf": "pF", "nf": "nF", "uf": "uF"}[unit_mode]
    scaled = scale_pf(value_pf, unit)
    if scaled is None:
        return "NA", unit
    return f"{scaled:.3g}", unit

