from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np


@dataclass(frozen=True)
class EngineeringUnit:
    name: str
    factor: float


PF = EngineeringUnit("pF", 1.0)
NF = EngineeringUnit("nF", 1_000.0)
UF = EngineeringUnit("uF", 1_000_000.0)


class EngineeringUnitFormatter:
    """Shared pF/nF/uF formatter with simple hysteresis."""

    def __init__(self, hysteresis: float = 0.08):
        self.hysteresis = max(0.0, float(hysteresis))
        self._last_absolute_unit = PF
        self._last_trend_unit = PF

    def choose_unit(self, values_pf: Iterable[float], scope: str = "absolute") -> EngineeringUnit:
        values = np.asarray(list(values_pf), dtype=np.float64)
        finite = np.abs(values[np.isfinite(values)])
        if finite.size == 0:
            chosen = PF
        else:
            representative = float(np.nanpercentile(finite, 95))
            chosen = self._unit_for_abs(representative, self._last(scope))
        if scope == "trend":
            self._last_trend_unit = chosen
        else:
            self._last_absolute_unit = chosen
        return chosen

    def format_value(self, value_pf: float, unit: EngineeringUnit | None = None, digits: int = 3) -> str:
        if not np.isfinite(value_pf):
            return "N/A"
        selected = unit or self.choose_unit([value_pf])
        value = float(value_pf) / selected.factor
        return f"{value:.{digits}g} {selected.name}"

    def scale(self, values_pf: np.ndarray, unit: EngineeringUnit) -> np.ndarray:
        return np.asarray(values_pf, dtype=np.float64) / unit.factor

    def _last(self, scope: str) -> EngineeringUnit:
        return self._last_trend_unit if scope == "trend" else self._last_absolute_unit

    def _unit_for_abs(self, magnitude_pf: float, last: EngineeringUnit) -> EngineeringUnit:
        pf_to_nf = 1_000.0
        nf_to_uf = 1_000_000.0
        down = 1.0 - self.hysteresis
        up = 1.0 + self.hysteresis
        mag = abs(float(magnitude_pf))

        if last == PF:
            return NF if mag >= pf_to_nf * up else PF
        if last == NF:
            if mag >= nf_to_uf * up:
                return UF
            if mag < pf_to_nf * down:
                return PF
            return NF
        if mag < nf_to_uf * down:
            return NF
        return UF
