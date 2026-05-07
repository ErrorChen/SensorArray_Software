from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

try:
    import serial
except ImportError:  # pragma: no cover - depends on local environment.
    serial = None

from matrix_log_viewer.config import CELL_NAMES, DEFAULT_BAUD, DEFAULT_SERIAL_READ_SIZE, MATRIX_SIZE
from matrix_log_viewer.data_store import CsvFrameWriter
from matrix_log_viewer.protocol_parser import SensorArrayStreamParser
from matrix_log_viewer.protocol_types import MatrixFrame


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Debug SensorArray SAC1 FastSpeed binary streams")
    parser.add_argument("--port", required=True, help="Serial COM/device path, for example COM5 or /dev/ttyACM0")
    parser.add_argument(
        "--baud",
        type=int,
        default=DEFAULT_BAUD,
        help="Host serial API baud rate. USB Serial/JTAG and USB CDC are not physical UART bottlenecks.",
    )
    parser.add_argument("--read-size", type=int, default=DEFAULT_SERIAL_READ_SIZE, help="Bytes per serial.read() call")
    parser.add_argument("--duration", type=float, help="Stop after this many seconds")
    parser.add_argument("--save-csv", help="Append parsed matrix frames to this CSV")
    parser.add_argument("--raw-dump", help="Write raw serial bytes to this binary file")
    parser.add_argument("--print-matrix", action="store_true", help="Print the latest 8x8 matrix in each status update")
    parser.add_argument("--no-matrix", action="store_true", help="Do not print matrix data")
    parser.add_argument("--report-interval", type=float, default=1.0, help="Seconds between summary lines")
    args = parser.parse_args()
    if args.baud <= 0:
        parser.error("--baud must be greater than 0")
    if args.read_size < 4096:
        parser.error("--read-size must be at least 4096")
    if args.duration is not None and args.duration <= 0:
        parser.error("--duration must be greater than 0")
    if args.report_interval <= 0:
        parser.error("--report-interval must be greater than 0")
    return args


def main() -> int:
    args = parse_args()
    if serial is None:
        print("pyserial is not installed; run pip install -r matrix_log_viewer/requirements.txt", file=sys.stderr)
        return 2

    parser = SensorArrayStreamParser()
    csv_writer = CsvFrameWriter(args.save_csv) if args.save_csv else None
    raw_dump = Path(args.raw_dump).open("wb") if args.raw_dump else None

    latest_frame: MatrixFrame | None = None
    latest_status_fields: dict[str, str] = {}
    latest_seq: int | None = None
    seq_gap = 0
    bytes_total = 0
    binary_total = 0
    text_total = 0
    last_report_time = time.monotonic()
    last_report_bytes = 0
    last_report_binary = 0
    last_report_text = 0
    start_time = last_report_time

    try:
        with serial.Serial(args.port, baudrate=args.baud, timeout=0.05) as serial_port:
            while True:
                now = time.monotonic()
                if args.duration is not None and now - start_time >= args.duration:
                    break

                chunk = serial_port.read(max(4096, int(args.read_size)))
                if chunk:
                    bytes_total += len(chunk)
                    if raw_dump is not None:
                        raw_dump.write(chunk)

                    results = parser.feedBytes(chunk)
                    for result in results:
                        if result.frame is not None:
                            if result.frame.frameType == "FAST_BINARY":
                                binary_total += 1
                                if latest_seq is not None and result.frame.seq > latest_seq + 1:
                                    seq_gap += result.frame.seq - latest_seq - 1
                                latest_seq = result.frame.seq
                                latest_frame = result.frame
                            else:
                                text_total += 1
                            if csv_writer is not None:
                                csv_writer.appendFrame(result.frame)
                        if result.status is not None:
                            latest_status_fields = dict(result.status.fields)

                now = time.monotonic()
                if now - last_report_time >= args.report_interval:
                    elapsed = max(1e-6, now - last_report_time)
                    parser_stats = parser.getStats()
                    binary_fps = (binary_total - last_report_binary) / elapsed
                    text_fps = (text_total - last_report_text) / elapsed
                    bytes_per_sec = (bytes_total - last_report_bytes) / elapsed
                    _print_summary(
                        bytes_per_sec=bytes_per_sec,
                        binary_fps=binary_fps,
                        text_fps=text_fps,
                        latest_frame=latest_frame,
                        latest_status_fields=latest_status_fields,
                        seq_gap=seq_gap,
                        parser_stats=parser_stats,
                    )
                    if args.print_matrix and not args.no_matrix and latest_frame is not None:
                        _print_matrix(latest_frame)
                    last_report_time = now
                    last_report_bytes = bytes_total
                    last_report_binary = binary_total
                    last_report_text = text_total
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"serial/debug error: {exc}", file=sys.stderr)
        return 1
    finally:
        if raw_dump is not None:
            raw_dump.close()

    return 0


def _print_summary(
    bytes_per_sec: float,
    binary_fps: float,
    text_fps: float,
    latest_frame: MatrixFrame | None,
    latest_status_fields: dict[str, str],
    seq_gap: int,
    parser_stats: dict[str, Any],
) -> None:
    scan_duration = latest_frame.durationUs if latest_frame is not None else "-"
    dropped = latest_frame.droppedFrames if latest_frame is not None else "-"
    decimated = latest_frame.outputDecimatedFrames if latest_frame is not None else "-"
    if decimated in (None, 0) and latest_status_fields.get("decimated") is not None:
        decimated = latest_status_fields["decimated"]
    latest_seq = latest_frame.seq if latest_frame is not None else "-"
    print(
        " | ".join(
            [
                f"bytes/s={bytes_per_sec:.0f}",
                f"binary_fps={binary_fps:.1f}",
                f"text_fps={text_fps:.1f}",
                f"latest_seq={latest_seq}",
                f"seq_gap={seq_gap}",
                f"scanDurationUs={scan_duration}",
                f"droppedFrames={dropped}",
                f"outputDecimatedFrames={decimated}",
                f"crc_errors={parser_stats.get('binaryCrcErrors', 0)}",
                f"resyncs={parser_stats.get('binaryMagicResyncs', 0)}",
                f"buffered={parser_stats.get('bufferedBytes', 0)}",
            ]
        ),
        flush=True,
    )


def _print_matrix(frame: MatrixFrame) -> None:
    matrix = np.full((MATRIX_SIZE, MATRIX_SIZE), np.nan, dtype=float)
    for index, cell_name in enumerate(CELL_NAMES):
        source = int(cell_name[1]) - 1
        detector = int(cell_name.split("D", maxsplit=1)[1]) - 1
        value = frame.values.get(cell_name, math.nan)
        if frame.validMask is not None and ((int(frame.validMask) >> index) & 0x1) == 0:
            value = math.nan
        matrix[source, detector] = value
    for row in matrix:
        print(" ".join("      NaN" if not math.isfinite(value) else f"{value:9.0f}" for value in row))


if __name__ == "__main__":
    raise SystemExit(main())
