from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_app.constants import DEFAULT_BACKEND_HOST, DEFAULT_BACKEND_PORT
from sensorarray_backend.app import create_app


def parse_args(argv: list[str] | None = None) -> AppConfiguration:
    parser = argparse.ArgumentParser(description="SensorArray FastAPI backend")
    parser.add_argument("--host", default=DEFAULT_BACKEND_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_BACKEND_PORT)
    parser.add_argument("--history-frames", type=int, default=18_000)
    parser.add_argument("--max-log-lines", type=int, default=10_000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    return AppConfiguration(
        host=args.host,
        port=args.port,
        historyFrames=args.history_frames,
        maxLogLines=args.max_log_lines,
        debug=args.debug,
    )


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if config.debug else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level="debug" if config.debug else "info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

