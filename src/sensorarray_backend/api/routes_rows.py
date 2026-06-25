from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from sensorarray_backend.api.dependencies import get_runtime
from sensorarray_backend.core.runtime import BackendRuntime

router = APIRouter(prefix="/api")


class RowsRequest(BaseModel):
    rows: int = Field(ge=1, le=8)


@router.post("/rows")
def set_rows(body: RowsRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict:
    try:
        return runtime.request_rows(body.rows)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

