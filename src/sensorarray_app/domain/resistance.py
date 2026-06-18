from __future__ import annotations

from typing import Any

import numpy as np


def parse_resistance_value(value: Any) -> float:
    """Parse a resistance field already supplied by firmware or a replay log.

    The legacy host did not contain a stable voltage-to-ohm formula, so this
    module deliberately preserves direct ohm/kohm/Mohm values instead of
    inventing a divider equation.
    """

    text = str(value).strip().replace("ohm", "").replace("Ohm", "").replace("Ω", "")
    factor = 1.0
    lowered = text.lower()
    if lowered.endswith("kohm") or lowered.endswith("k"):
        factor = 1_000.0
        text = text[:-1]
    elif lowered.endswith("mohm") or lowered.endswith("m"):
        factor = 1_000_000.0
        text = text[:-1]
    return float(text) * factor


def values_to_ohm(values: list[Any]) -> np.ndarray:
    out = np.full(len(values), np.nan, dtype=np.float64)
    for index, value in enumerate(values):
        try:
            out[index] = parse_resistance_value(value)
        except (TypeError, ValueError):
            out[index] = np.nan
    return out
