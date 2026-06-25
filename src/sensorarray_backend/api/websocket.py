from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from sensorarray_backend.api.dependencies import get_runtime
from sensorarray_backend.core.history import history_payload
from sensorarray_backend.core.runtime import BackendRuntime
from sensorarray_backend.core.snapshot import websocket_snapshot

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, runtime: BackendRuntime = Depends(get_runtime)) -> None:
    await websocket.accept()
    last_history_revision = -1
    try:
        while True:
            await websocket.send_json(websocket_snapshot(runtime))
            history = history_payload(runtime)
            if history["revision"] != last_history_revision:
                last_history_revision = history["revision"]
                await websocket.send_json({"type": "history", "payload": history})
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        return

