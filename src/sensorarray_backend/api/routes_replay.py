from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from sensorarray_backend.api.dependencies import get_runtime
from sensorarray_backend.core.runtime import BackendRuntime

router = APIRouter(prefix="/api/replay")


class ReplayOpenRequest(BaseModel):
    path: str = Field(min_length=1)
    speed: float = 1.0


class ReplaySeekRequest(BaseModel):
    positionSeconds: float = 0.0


@router.post("/open")
def replay_open(body: ReplayOpenRequest, runtime: BackendRuntime = Depends(get_runtime)) -> dict:
    try:
        return runtime.open_replay(body.path, body.speed)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/start")
def replay_start(runtime: BackendRuntime = Depends(get_runtime)) -> dict:
    try:
        runtime.start_replay()
        return {"ok": True, "path": runtime.replayPath}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/stop")
def replay_stop(runtime: BackendRuntime = Depends(get_runtime)) -> dict:
    runtime.stop_replay()
    return {"ok": True}


@router.post("/seek")
def replay_seek(_body: ReplaySeekRequest) -> dict:
    return {"ok": False, "message": "Replay seek is not supported by the streaming replay transport yet."}

