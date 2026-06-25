from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sensorarray_backend.api.dependencies import get_runtime
from sensorarray_backend.core.runtime import BackendRuntime

router = APIRouter(prefix="/api")


class DisplaySettingsRequest(BaseModel):
    measurementDomain: str | None = None
    displayMode: str | None = None
    showCellText: bool | None = None
    pauseDisplay: bool | None = None
    freezeColor: bool | None = None
    unitMode: str | None = None
    circuitOffsetPf: float | None = None


class BaselineRequest(BaseModel):
    action: str


class SelectionRequest(BaseModel):
    cell: str


@router.post("/settings/display")
def display_settings(body: DisplaySettingsRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    try:
        return runtime.update_display_settings(body.model_dump(exclude_unset=True))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/settings/baseline")
def baseline(body: BaselineRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    try:
        return runtime.baseline_action(body.action)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/selection")
def selection(body: SelectionRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    try:
        runtime.set_selection_from_cell(body.cell)
        return runtime.current_selection_payload()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

