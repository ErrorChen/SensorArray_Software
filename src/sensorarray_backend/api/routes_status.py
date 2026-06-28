from __future__ import annotations

from fastapi import APIRouter, Depends

from sensorarray_backend.api.dependencies import get_runtime
from sensorarray_backend.core.runtime import BackendRuntime
from sensorarray_backend.core.snapshot import snapshot_payload

router = APIRouter()


@router.get("/")
def root() -> dict:
    return {
        "ok": True,
        "service": "sensorarray_backend",
        "message": "SensorArray backend is running. Use the Electron desktop app for the GUI.",
        "endpoints": {
            "health": "/health",
            "status": "/api/status",
            "websocket": "/ws",
            "docs": "/docs",
        },
    }


@router.get("/health")
def health() -> dict:
    return {"ok": True, "service": "sensorarray_backend"}


@router.get("/api/status")
def status(runtime: BackendRuntime = Depends(get_runtime)) -> dict:
    return snapshot_payload(runtime)


@router.get("/api/history")
def history(latest_n: int = 600, runtime: BackendRuntime = Depends(get_runtime)) -> dict:
    return runtime.set_trend_latest_n(latest_n)
