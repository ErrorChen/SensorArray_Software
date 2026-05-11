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
    parser = argparse.ArgumentParser(description="Debug SensorArray SAC1 UpperSpeed binary streams")
    parser.add_argument("--port", help="Serial COM/device path, for example COM5 or /dev/ttyACM0")
    parser.add_argument("--input-file", help="Read a captured/replay binary file instead of a serial port")
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
    if not args.port and not args.input_file:
        parser.error("either --port or --input-file is required")
    return args


def main() -> int:
    args = parse_args()
    if serial is None and not args.input_file:
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
        with _open_input(args) as input_stream:
            while True:
                now = time.monotonic()
                if args.duration is not None and now - start_time >= args.duration:
                    break

                chunk = input_stream.read(max(4096, int(args.read_size)))
                if args.input_file and not chunk:
                    break
                if chunk:
                    bytes_total += len(chunk)
                    if raw_dump is not None:
                        raw_dump.write(chunk)

                    results = parser.feed(chunk)
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
            total_elapsed = max(1e-6, time.monotonic() - start_time)
            _print_summary(
                bytes_per_sec=bytes_total / total_elapsed,
                binary_fps=binary_total / total_elapsed,
                text_fps=text_total / total_elapsed,
                latest_frame=latest_frame,
                latest_status_fields=latest_status_fields,
                seq_gap=seq_gap,
                parser_stats=parser.getStats(),
            )
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"serial/debug error: {exc}", file=sys.stderr)
        return 1
    finally:
        if raw_dump is not None:
            raw_dump.close()

    return 0


def _open_input(args: argparse.Namespace):
    if args.input_file:
        return Path(args.input_file).open("rb")
    return serial.Serial(args.port, baudrate=args.baud, timeout=0.05)


def _print_summary(
    bytes_per_sec: float,
    binary_fps: float,
    text_fps: float,
    latest_frame: MatrixFrame | None,
    latest_status_fields: dict[str, str],
    seq_gap: int,
    parser_stats: dict[str, Any],
) -> None:
    latest_diag = parser_stats.get("fastBinaryDiagLatest") or {}
    scan_duration = latest_frame.durationUs if latest_frame is not None else "-"
    dropped = latest_diag.get("drop", latest_frame.droppedFrames if latest_frame is not None else "-")
    decimated = latest_diag.get("decimated", latest_frame.outputDecimatedFrames if latest_frame is not None else "-")
    latest_seq = latest_frame.seq if latest_frame is not None else "-"
    partial = int(latest_diag.get("partialAfterFirstByte") or 0)
    warning = " | PROTOCOL_RISK: firmware reported partialAfterFirstByte > 0" if partial > 0 else ""
    if partial > 0:
        warning = f"\033[31m{warning}\033[0m"
    print(
        " | ".join(
            [
                f"bytes/s={bytes_per_sec:.0f}",
                f"binary_fps={binary_fps:.1f}",
                f"latest_seq={latest_seq}",
                f"seq_gap={parser_stats.get('seqGapTotal', seq_gap)}",
                f"scanDurationUs={scan_duration}",
                f"buffer={parser_stats.get('bufferBytes', parser_stats.get('bufferedBytes', 0))}",
                f"HOST_CRC={parser_stats.get('binaryCrcErrors', 0)}",
                f"HOST_RESYNC={parser_stats.get('binaryMagicResyncs', 0)}",
                f"HOST_SKIPPED_BYTES={parser_stats.get('skippedBytes', 0)}",
                f"ASCII_AFTER_FAST_BINARY_START={parser_stats.get('protocolPollutionCount', 0)}",
                f"startupText={parser_stats.get('startupTextLineCount', 0)}",
                f"fastBinaryStartSeen={int(bool(parser_stats.get('fastBinaryStartSeen')))}",
                f"pureBinaryMode={int(bool(parser_stats.get('pureBinaryMode')))}",
                f"scanFps={latest_diag.get('scanFps', latest_status_fields.get('scanFps', '-'))}",
                f"outFps={latest_diag.get('outFps', latest_status_fields.get('outFps', '-'))}",
                f"qUsed={latest_diag.get('qUsed', latest_status_fields.get('qUsed', '-'))}",
                f"qFull={latest_diag.get('qFull', latest_status_fields.get('qFull', '-'))}",
                f"DEVICE_DROP={dropped}",
                f"DEVICE_DECIMATED={decimated}",
                f"outputDiv={latest_diag.get('outputDiv', latest_status_fields.get('outputDiv', '-'))}",
                f"droppedBeforeFirstByte={latest_diag.get('droppedBeforeFirstByte', '-')}",
                f"partialAfterFirstByte={partial}",
                f"fullFrameWriteCount={latest_diag.get('fullFrameWriteCount', '-')}",
                f"fullFrameWriteFailCount={latest_diag.get('fullFrameWriteFailCount', '-')}",
                f"shortWrite={latest_diag.get('shortWrite', latest_status_fields.get('shortWrite', '-'))}",
                f"writeFail={latest_diag.get('writeFail', latest_status_fields.get('writeFail', '-'))}",
            ]
        )
        + warning,
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
