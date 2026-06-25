from __future__ import annotations

from dataclasses import dataclass

from sensorarray_app.constants import DEFAULT_BACKEND_HOST, DEFAULT_BACKEND_PORT, DEFAULT_SERIAL_BAUD


@dataclass
class AppConfiguration:
    host: str = DEFAULT_BACKEND_HOST
    port: int = DEFAULT_BACKEND_PORT
    serialBaud: int = DEFAULT_SERIAL_BAUD
    maxLogLines: int = 10_000
    historyFrames: int = 18_000
    debug: bool = False
