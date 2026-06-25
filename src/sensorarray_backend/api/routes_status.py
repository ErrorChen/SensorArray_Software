from __future__ import annotations

from fastapi import APIRouter, Depends

from sensorarray_backend.api.dependencies import get_runtime
from sensorarray_backend.core.runtime import BackendRuntime
from sensorarray_backend.core.snapshot import snapshot_payload

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"ok": True, "service": "sensorarray_backend"}


@router.get("/api/status")
def status(runtime: BackendRuntime = Depends(get_runtime)) -> dict:
    return snapshot_payload(runtime)

