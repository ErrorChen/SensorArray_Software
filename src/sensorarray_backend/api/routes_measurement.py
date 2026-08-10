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
        status_code = 409 if "requires measured AVDD/AVSS" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/rail")
def configure_voltage_rail(
    body: VoltageRailRequest,
    runtime: BackendRuntime = Depends(get_runtime),
) -> dict[str, Any]:
    try:
        return runtime.configure_voltage_rail(body.measuredAvddV, body.measuredAvssV)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
