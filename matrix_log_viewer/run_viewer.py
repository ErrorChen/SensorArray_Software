from __future__ import annotations

import argparse
import logging
import queue
import sys

from matrix_log_viewer.app import createDashApp
from matrix_log_viewer.config import (
    DEFAULT_BAUD,
    DEFAULT_DASH_HOST,
    DEFAULT_DASH_PORT,
    DEFAULT_MAX_POINTS_PER_CELL,
    DEFAULT_REPLAY_SPEED,
    MAX_LINES_PER_DASH_TICK,
)
from matrix_log_viewer.data_store import CsvFrameWriter, MatrixDataStore
from matrix_log_viewer.matv_parser import MatvParser
from matrix_log_viewer.serial_reader import ReplayReaderThread, SerialReaderThread


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Real-time MATV 8x8 matrix log viewer")
    parser.add_argument("--port", help="Serial COM/device path, for example COM5 or /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="Serial baud rate")
    parser.add_argument(
        "--max-points",
        type=int,
        default=DEFAULT_MAX_POINTS_PER_CELL,
        help="Maximum history points retained per matrix cell",
    )
    parser.add_argument(
        "--save-csv",
        help="Optional CSV path. Parsed MATV frames are appended in real time.",
    )
    parser.add_argument(
        "--replay-file",
        help="Read a historical log file instead of opening a serial port",
    )
    parser.add_argument(
        "--replay-speed",
        type=float,
        default=DEFAULT_REPLAY_SPEED,
        help="Replay speed multiplier. 10.0 is ten times faster than the original timestamps.",
    )
    parser.add_argument("--host", default=DEFAULT_DASH_HOST, help="Dash server host")
    parser.add_argument("--port-web", type=int, default=DEFAULT_DASH_PORT, help="Dash server port")
    parser.add_argument("--debug", action="store_true", help="Enable DEBUG logging")
    args = parser.parse_args()

    if not args.replay_file and not args.port:
        parser.error("either --port or --replay-file is required")

    if args.max_points < 1:
        parser.error("--max-points must be at least 1")

    if args.replay_speed <= 0:
        parser.error("--replay-speed must be greater than 0")

    return args


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    line_queue: queue.Queue[str] = queue.Queue()
    parser = MatvParser()
    data_store = MatrixDataStore(maxPointsPerCell=args.max_points)
    csv_writer = CsvFrameWriter(args.save_csv) if args.save_csv else None

    if args.replay_file:
        reader = ReplayReaderThread(args.replay_file, args.replay_speed, line_queue)
        logging.info("Using replay file input; serial port arguments will be ignored")
    else:
        reader = SerialReaderThread(args.port, args.baud, line_queue)

    reader.start()

    app = createDashApp(
        lineQueue=line_queue,
        parser=parser,
        dataStore=data_store,
        reader=reader,
        csvWriter=csv_writer,
        maxLinesPerTick=MAX_LINES_PER_DASH_TICK,
    )

    logging.info("Open the UI at http://%s:%d", args.host, args.port_web)
    try:
        app.run(host=args.host, port=args.port_web, debug=args.debug, use_reloader=False)
    except KeyboardInterrupt:
        logging.info("Interrupted, shutting down")
    finally:
        reader.stop()
        reader.join(timeout=2.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())

