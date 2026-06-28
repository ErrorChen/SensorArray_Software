from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from sensorarray_backend.api.dependencies import get_runtime
from sensorarray_backend.core.runtime import BackendRuntime

router = APIRouter(prefix="/api/setup")


@router.get("/profile")
def get_setup_profile(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return runtime.setup_profile_payload()


@router.post("/profile")
def apply_setup_profile(payload: dict[str, Any], runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    try:
        return runtime.apply_setup_profile(payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
