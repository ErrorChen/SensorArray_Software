from __future__ import annotations

from dataclasses import dataclass, field

from sensorarray_app.domain.baseline import BaselineResult
from sensorarray_app.domain.models import DisplayMode
from sensorarray_app.domain.selection import FourPointSelection, default_selection


@dataclass
class UiState:
    displayMode: DisplayMode = DisplayMode.ABSOLUTE_C
    paused: bool = False
    followLatest: bool = True
    cellText: bool = False
    freezeColor: bool = False
    selection: FourPointSelection = field(default_factory=default_selection)
    selectionRevision: int = 0
    baseline: BaselineResult | None = None
    baselineStatus: str = "Not captured"
    baselineInvalidReason: str = ""
    clearRevision: int = 0
