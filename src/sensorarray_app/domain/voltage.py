from __future__ import annotations

import numpy as np

VOLTAGE_FACTORS_TO_UV = {"uv": 1.0, "uV": 1.0, "mv": 1_000.0, "mV": 1_000.0, "v": 1_000_000.0, "V": 1_000_000.0}


def voltage_to_uv(values: np.ndarray, unit: str) -> np.ndarray:
    return np.asarray(values, dtype=np.float64) * VOLTAGE_FACTORS_TO_UV.get(str(unit), 1.0)
