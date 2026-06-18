from __future__ import annotations

from dataclasses import dataclass

from sensorarray_app.constants import DEFAULT_DASH_HOST, DEFAULT_DASH_PORT, DEFAULT_SERIAL_BAUD, DEFAULT_SERIAL_PORT


@dataclass
class AppConfiguration:
    host: str = DEFAULT_DASH_HOST
    port: int = DEFAULT_DASH_PORT
    serialPort: str = DEFAULT_SERIAL_PORT
    serialBaud: int = DEFAULT_SERIAL_BAUD
    maxLogLines: int = 10_000
    historyFrames: int = 18_000
    debug: bool = False
