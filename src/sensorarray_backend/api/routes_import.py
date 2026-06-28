from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from sensorarray_backend.api.dependencies import get_runtime
from sensorarray_backend.core.runtime import BackendRuntime

router = APIRouter(prefix="/api/import")


class SessionImportRequest(BaseModel):
    path: str = Field(min_length=1)


@router.post("/session")
def import_session(body: SessionImportRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    try:
        return runtime.import_session_file(body.path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
