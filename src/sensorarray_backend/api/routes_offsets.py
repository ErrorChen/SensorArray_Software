from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from sensorarray_backend.api.dependencies import get_runtime
from sensorarray_backend.core.runtime import BackendRuntime

router = APIRouter(prefix="/api/settings/offsets")


class OffsetCellRequest(BaseModel):
    row: int = Field(ge=1, le=8)
    col: int = Field(ge=1, le=8)
    offsetPf: float


class OffsetBulkRequest(BaseModel):
    offsetsPf: list[list[float]]


class OffsetScopeRequest(BaseModel):
    scope: str = "cell"
    row: int | None = Field(default=None, ge=1, le=8)
    col: int | None = Field(default=None, ge=1, le=8)


@router.get("")
def get_offsets(runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    return runtime.get_offsets_payload()


@router.post("/cell")
def set_offset_cell(body: OffsetCellRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    try:
        return runtime.set_offset_cell(body.row, body.col, body.offsetPf)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/bulk")
def set_offsets_bulk(body: OffsetBulkRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    try:
        return runtime.set_offsets_bulk(body.offsetsPf)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/clear")
def clear_offsets(body: OffsetScopeRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    try:
        return runtime.clear_offsets(body.scope, body.row, body.col)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/zero-current")
def zero_current_offsets(body: OffsetScopeRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict[str, Any]:
    try:
        return runtime.zero_current_offsets(body.scope, body.row, body.col)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
