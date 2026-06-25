from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_backend.api import routes_replay, routes_rows, routes_settings, routes_status, routes_transport, websocket
from sensorarray_backend.core.runtime import BackendRuntime


def create_app(config: AppConfiguration | None = None) -> FastAPI:
    cfg = config or AppConfiguration()
    runtime = BackendRuntime(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime.start()
        app.state.runtime = runtime
        try:
            yield
        finally:
            runtime.stop()

    app = FastAPI(title="SensorArray Backend", version="0.3.0", lifespan=lifespan)
    app.state.runtime = runtime
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173", "file://"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(routes_status.router)
    app.include_router(routes_transport.router)
    app.include_router(routes_replay.router)
    app.include_router(routes_rows.router)
    app.include_router(routes_settings.router)
    app.include_router(websocket.router)
    return app

