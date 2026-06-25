from __future__ import annotations

from starlette.requests import HTTPConnection

from sensorarray_backend.core.runtime import BackendRuntime


def get_runtime(connection: HTTPConnection) -> BackendRuntime:
    return connection.app.state.runtime
