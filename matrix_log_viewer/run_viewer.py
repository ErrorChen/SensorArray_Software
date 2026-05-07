from __future__ import annotations

import argparse
import logging
import queue
import sys
import threading
import webbrowser

from matrix_log_viewer.app import createDashApp
from matrix_log_viewer.config import (
    DEFAULT_BAUD,
    DEFAULT_DASH_HOST,
    DEFAULT_DASH_PORT,
    DEFAULT_INPUT_QUEUE_MAX_CHUNKS,
    DEFAULT_MAX_POINTS_PER_CELL,
    DEFAULT_REPLAY_SPEED,
    DEFAULT_SERIAL_READ_SIZE,
    MAX_INPUT_CHUNKS_PER_DASH_TICK,
    MAX_PARSE_RESULTS_PER_TICK,
)
from matrix_log_viewer.connection_manager import ConnectionManager
from matrix_log_viewer.data_store import CsvFrameWriter, MatrixDataStore
from matrix_log_viewer.protocol_parser import SensorArrayStreamParser


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SensorArray FastSpeed/MATV 8x8 matrix viewer")
    parser.add_argument("--port", help="Initial serial COM/device path, for example COM5 or /dev/ttyUSB0")
    parser.add_argument(
        "--baud",
        type=int,
        default=DEFAULT_BAUD,
        help="Host serial API baud rate. ESP32-S3 USB Serial/JTAG and USB CDC are not limited like a physical UART.",
    )
    parser.add_argument("--read-size", type=int, default=DEFAULT_SERIAL_READ_SIZE, help="Serial/replay bytes read per chunk")
    parser.add_argument("--auto-reconnect", action="store_true", help="Retry serial connection after disconnect")
    parser.add_argument(
        "--input-mode",
        choices=["serial", "replay", "disconnected"],
        help="Initial input mode. Defaults from --port/--replay-file.",
    )
    parser.add_argument("--max-points", type=int, default=DEFAULT_MAX_POINTS_PER_CELL, help="Maximum history points retained per stream/cell")
    parser.add_argument("--save-csv", help="Optional CSV path. Parsed matrix frames are appended in real time.")
    parser.add_argument("--replay-file", help="Read a historical text or binary log file instead of opening a serial port")
    parser.add_argument("--replay-speed", type=float, default=DEFAULT_REPLAY_SPEED, help="Replay speed multiplier")
    parser.add_argument("--host", default=DEFAULT_DASH_HOST, help="Dash server host")
    parser.add_argument("--port-web", type=int, default=DEFAULT_DASH_PORT, help="Dash server port")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically")
    parser.add_argument("--debug", action="store_true", help="Enable DEBUG logging")
    args = parser.parse_args()

    if args.max_points < 1:
        parser.error("--max-points must be at least 1")
    if args.replay_speed <= 0:
        parser.error("--replay-speed must be greater than 0")
    if args.baud <= 0:
        parser.error("--baud must be greater than 0")
    if args.read_size < 4096:
        parser.error("--read-size must be at least 4096")
    return args


def _infer_input_mode(args: argparse.Namespace) -> str:
    if args.input_mode:
        return args.input_mode
    if args.replay_file:
        return "replay"
    if args.port:
        return "serial"
    return "disconnected"


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    input_queue: queue.Queue[bytes] = queue.Queue(maxsize=DEFAULT_INPUT_QUEUE_MAX_CHUNKS)
    parser = SensorArrayStreamParser()
    data_store = MatrixDataStore(maxPointsPerCell=args.max_points)
    csv_writer = CsvFrameWriter(args.save_csv) if args.save_csv else None
    connection_manager = ConnectionManager(input_queue)

    input_mode = _infer_input_mode(args)
    try:
        if input_mode == "serial" and args.port:
            connection_manager.connectSerial(args.port, args.baud, args.auto_reconnect, args.read_size)
            logging.info("Initial serial connection requested on %s at %d baud", args.port, args.baud)
        elif input_mode == "replay" and args.replay_file:
            connection_manager.startReplay(args.replay_file, args.replay_speed, args.read_size)
            logging.info("Initial replay started from %s", args.replay_file)
        else:
            logging.info("Starting disconnected; choose a COM port in the web UI")
    except Exception as exc:
        logging.warning("Initial input setup failed: %s", exc)

    app = createDashApp(
        inputQueue=input_queue,
        parser=parser,
        dataStore=data_store,
        connectionManager=connection_manager,
        csvWriter=csv_writer,
        maxChunksPerTick=MAX_INPUT_CHUNKS_PER_DASH_TICK,
        maxParseResultsPerTick=MAX_PARSE_RESULTS_PER_TICK,
    )

    url = f"http://{args.host}:{args.port_web}"
    logging.info("Open the UI at %s", url)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        app.run(host=args.host, port=args.port_web, debug=args.debug, use_reloader=False)
    except KeyboardInterrupt:
        logging.info("Interrupted, shutting down")
    finally:
        connection_manager.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
