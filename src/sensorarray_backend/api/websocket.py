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
    last_history_key: tuple[int, int, int, str, str] | None = None
    try:
        while True:
            await websocket.send_json(websocket_snapshot(runtime))
            history = history_payload(runtime)
            offset_signature = str(hash(tuple(tuple(row) for row in runtime.ui.userOffsetsPf)))
            history_key = (
                int(history["revision"]),
                int(history.get("latestN", runtime.ui.trendLatestN)),
                int(history["selectionRevision"]),
                runtime.ui.displayMode.value,
                offset_signature,
            )
            if history_key != last_history_key:
                last_history_key = history_key
                await websocket.send_json({"type": "history", "payload": history})
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        return
