from __future__ import annotations

from dataclasses import dataclass, field

from sensorarray_app.constants import FDC_CIRCUIT_OFFSET_PF
from sensorarray_app.domain.baseline import BaselineResult
from sensorarray_app.domain.models import DisplayMode
from sensorarray_app.domain.selection import FourPointSelection, default_selection


def default_user_offsets_pf() -> list[list[float]]:
    return [[0.0 for _ in range(8)] for _ in range(8)]


@dataclass
class UiState:
    displayMode: DisplayMode = DisplayMode.ABSOLUTE_C
    pendingDisplayMode: DisplayMode | None = None
    measurementDomain: str = "auto"
    paused: bool = False
    followLatest: bool = True
    cellText: bool = True
    freezeColor: bool = False
    unitMode: str = "auto"
    voltageReference: str = "vss_relative"
    circuitOffsetPf: float = FDC_CIRCUIT_OFFSET_PF
    frozenColorMin: float | None = None
    frozenColorMax: float | None = None
    selection: FourPointSelection = field(default_factory=default_selection)
    selectionRevision: int = 0
    baseline: BaselineResult | None = None
    baselineStatus: str = "Not captured"
    baselineInvalidReason: str = ""
    clearRevision: int = 0
    trendLatestN: int = 600
    userOffsetsPf: list[list[float]] = field(default_factory=default_user_offsets_pf)
