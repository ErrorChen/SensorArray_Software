from __future__ import annotations

from dataclasses import dataclass, field

from sensorarray_app.constants import FDC_CIRCUIT_OFFSET_PF
from sensorarray_app.domain.baseline import BaselineResult
from sensorarray_app.domain.models import DisplayMode
from sensorarray_app.domain.selection import FourPointSelection, default_selection


@dataclass
class UiState:
    displayMode: DisplayMode = DisplayMode.ABSOLUTE_C
    measurementDomain: str = "auto"
    paused: bool = False
    followLatest: bool = True
    cellText: bool = True
    freezeColor: bool = False
    unitMode: str = "auto"
    circuitOffsetPf: float = FDC_CIRCUIT_OFFSET_PF
    frozenColorMin: float | None = None
    frozenColorMax: float | None = None
    selection: FourPointSelection = field(default_factory=default_selection)
    selectionRevision: int = 0
    baseline: BaselineResult | None = None
    baselineStatus: str = "Not captured"
    baselineInvalidReason: str = ""
    clearRevision: int = 0
