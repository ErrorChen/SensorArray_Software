from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from sensorarray_backend.api.dependencies import get_runtime
from sensorarray_backend.core.runtime import BackendRuntime

router = APIRouter(prefix="/api/export")


@router.get("/session")
def export_session(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return runtime.export_session_payload()
