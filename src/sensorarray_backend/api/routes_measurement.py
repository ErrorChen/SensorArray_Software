from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sensorarray_backend.api.dependencies import get_runtime
from sensorarray_backend.core.runtime import BackendRuntime

router = APIRouter(prefix="/api/measurement")


class MeasurementModeRequest(BaseModel):
    mode: str
    measuredAvddV: float | None = None
    measuredAvssV: float | None = None


class VoltageRailRequest(BaseModel):
    measuredAvddV: float
    measuredAvssV: float


class RowModesRequest(BaseModel):
    modes: list[str]


@router.get("/mode")
def measurement_mode(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return runtime.measurement_mode_payload()


@router.post("/mode")
def set_measurement_mode(
    body: MeasurementModeRequest,
    runtime: BackendRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return runtime.request_measurement_mode_api(body.mode, body.measuredAvddV, body.measuredAvssV)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/row-modes")
def row_modes(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return runtime.row_modes_payload()


@router.post("/row-modes")
def set_row_modes(body: RowModesRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    try:
        return runtime.request_row_modes_api(body.modes)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/rail")
def configure_voltage_rail(
    body: VoltageRailRequest,
    runtime: BackendRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    """Deprecated debug-only RAILCFG endpoint retained for old tooling."""
    try:
        return runtime.configure_voltage_rail(body.measuredAvddV, body.measuredAvssV)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
