from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import asdict
from typing import Any

from .binary_frame_parser import (
    MAGIC_BYTES,
    SIZE as BINARY_FRAME_SIZE,
    BinaryFrameParseError,
    SensorArrayBinaryFrameParser,
)
from .protocol_types import DeviceEvent, DeviceStatus, MatrixFrame, ParseResult
from .sensorarray_status_codes import parseStatusCode, statusCodeName
from .text_log_parser import TextLogParser

LOGGER = logging.getLogger(__name__)


class SensorArrayStreamParser:
    """Byte-stream parser for mixed FastSpeed binary frames and text logs."""

    def __init__(self, maxTextLineBytes: int = 4096):
        self._lock = threading.RLock()
        self._buffer = bytearray()
        self._binaryParser = SensorArrayBinaryFrameParser()
        self._textParser = TextLogParser()
        self.maxTextLineBytes = max(256, int(maxTextLineBytes))
        self._stats = self._empty_stats()

    def feedBytes(self, data: bytes) -> list[ParseResult]:
        if not data:
            return []

        with self._lock:
            self._buffer.extend(data)
            return self._drain_buffer()

    def getStats(self) -> dict:
        with self._lock:
            stats = dict(self._stats)
            stats["parsedByType"] = dict(self._stats["parsedByType"])
            stats["lastDeviceStatus"] = dict(self._stats["lastDeviceStatus"])
            stats["lastDeviceEvent"] = dict(self._stats["lastDeviceEvent"])
            stats["bufferedBytes"] = len(self._buffer)
            return stats

    def reset(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._textParser = TextLogParser()
            self._stats = self._empty_stats()

    def _drain_buffer(self) -> list[ParseResult]:
        results: list[ParseResult] = []

        while self._buffer:
            if self._buffer.startswith(MAGIC_BYTES):
                if len(self._buffer) < BINARY_FRAME_SIZE:
                    break
                raw_frame = bytes(self._buffer[:BINARY_FRAME_SIZE])
                try:
                    frame = self._binaryParser.parseFrame(raw_frame)
                except BinaryFrameParseError as exc:
                    self._record_binary_error(exc)
                    # A CRC-valid frame is fixed length; on CRC failure FastSpeed hosts
                    # should drop that candidate frame and resume scanning for the next SAC1.
                    if exc.kind == "crc":
                        del self._buffer[:BINARY_FRAME_SIZE]
                    else:
                        del self._buffer[:1]
                    continue

                del self._buffer[:BINARY_FRAME_SIZE]
                result = ParseResult(frame=frame)
                self._record_result(result, binary=True)
                results.append(result)
                continue

            magic_index = self._buffer.find(MAGIC_BYTES)
            newline_index = self._buffer.find(b"\n")

            if newline_index >= 0 and (magic_index < 0 or newline_index < magic_index):
                raw_line = bytes(self._buffer[: newline_index + 1])
                del self._buffer[: newline_index + 1]
                raw_line = self._drop_non_text_line_prefix(raw_line)
                if not raw_line:
                    continue
                result = self._parse_text_bytes(raw_line)
                if result is not None and result.hasData():
                    self._record_result(result, binary=False)
                    results.append(result)
                continue

            if magic_index > 0:
                self._record_resync(magic_index)
                del self._buffer[:magic_index]
                continue

            if magic_index == 0:
                continue

            if newline_index < 0:
                # Mixed stdout can contain text line fragments. Keep plausible text
                # fragments intact, but trim binary garbage while preserving the last
                # three bytes so a split "SAC1" magic can still be completed.
                if self._looks_like_text_fragment(self._buffer) and len(self._buffer) <= self.maxTextLineBytes:
                    break
                self._trim_garbage_tail()
                break

            break

        return results

    def _parse_text_bytes(self, raw_line: bytes) -> ParseResult | None:
        before = self._textParser.getStats()
        line = raw_line.rstrip(b"\r\n").decode("utf-8", errors="replace")
        result = self._textParser.parseLine(line)
        after = self._textParser.getStats()
        self._merge_text_stat_delta(before, after)
        return result

    def _merge_text_stat_delta(self, before: dict, after: dict) -> None:
        self._stats["skippedLines"] += after.get("skippedLines", 0) - before.get("skippedLines", 0)
        self._stats["parseErrors"] += after.get("parseErrors", 0) - before.get("parseErrors", 0)
        self._stats["warnings"] += after.get("warnings", 0) - before.get("warnings", 0)
        if after.get("lastError") and after.get("lastError") != before.get("lastError"):
            self._stats["lastError"] = after["lastError"]
        if after.get("lastWarning") and after.get("lastWarning") != before.get("lastWarning"):
            self._stats["lastWarning"] = after["lastWarning"]

    def _record_result(self, result: ParseResult, binary: bool) -> None:
        if result.frame is not None:
            frame = result.frame
            self._stats["parsedFramesTotal"] += 1
            self._stats["parsedByType"][frame.frameType] += 1
            if binary:
                self._stats["parsedBinaryFrames"] += 1
            else:
                self._stats["parsedTextFrames"] += 1
            self._update_status_code_from_frame(frame)
        if result.status is not None:
            self._stats["parsedStatuses"] += 1
            self._stats["lastDeviceStatus"] = asdict(result.status)
            self._update_status_code_from_fields(result.status.fields)
        if result.event is not None:
            self._stats["parsedEvents"] += 1
            self._stats["lastDeviceEvent"] = asdict(result.event)
            if result.event.code is not None:
                self._update_status_code(result.event.code)

    def _record_binary_error(self, exc: BinaryFrameParseError) -> None:
        if exc.kind == "crc":
            self._stats["binaryCrcErrors"] += 1
        elif exc.kind == "short":
            self._stats["binaryShortFrames"] += 1
        else:
            self._stats["parseErrors"] += 1
        self._stats["lastError"] = str(exc)
        LOGGER.debug("Binary parse error: %s", exc)

    def _record_resync(self, skipped: int) -> None:
        self._stats["skippedBytes"] += skipped
        self._stats["binaryMagicResyncs"] += 1

    def _trim_garbage_tail(self) -> None:
        keep = len(MAGIC_BYTES) - 1
        if len(self._buffer) <= keep:
            return
        skipped = len(self._buffer) - keep
        self._stats["skippedBytes"] += skipped
        del self._buffer[:skipped]

    @staticmethod
    def _looks_like_text_fragment(buffer: bytearray) -> bool:
        if not buffer:
            return True
        for value in buffer:
            if value in (9, 13) or 32 <= value <= 126:
                continue
            return False
        text = bytes(buffer).decode("ascii", errors="ignore").strip()
        if not text:
            return True
        token = text.split(",", maxsplit=1)[0].strip()
        known_prefixes = (
            "MATV",
            "MATV_HEADER",
            "MATV_RAW",
            "MATV_RAW_HEADER",
            "MATV_GAIN",
            "MATV_GAIN_HEADER",
            "MATV_ERR",
            "MATV_ERR_HEADER",
            "STAT",
            "EVENT",
            "RATE_EVENT",
            "RATE_FATAL",
            "APPMODE",
            "VOLTSCAN",
            "ADS_",
            "ROUTE_",
            "DBG",
            "WARN",
            "WARNING",
            "ERROR",
            "STREAM_INIT",
        )
        if any(prefix.startswith(token) or token.startswith(prefix) for prefix in known_prefixes):
            return True
        return "," in text and token == token.upper() and token.replace("_", "").isalnum()

    def _drop_non_text_line_prefix(self, raw_line: bytes) -> bytes:
        index = 0
        while index < len(raw_line) and raw_line[index] not in (9, 10, 13) and not (32 <= raw_line[index] <= 126):
            index += 1
        if index:
            self._stats["skippedBytes"] += index
        return raw_line[index:]

    def _update_status_code_from_frame(self, frame: MatrixFrame) -> None:
        code = frame.lastStatusCode
        if code is None or code == 0:
            code = frame.firstStatusCode
        self._update_status_code(code)

    def _update_status_code_from_fields(self, fields: dict[str, str]) -> None:
        for key in ("code", "lastStatusCode", "statusCode", "firstStatusCode"):
            code = parseStatusCode(fields.get(key))
            if code is not None:
                self._update_status_code(code)
                return

    def _update_status_code(self, code: int | None) -> None:
        if code is None:
            return
        self._stats["lastStatusCode"] = int(code)
        self._stats["lastStatusCodeName"] = statusCodeName(int(code))

    @staticmethod
    def _empty_stats() -> dict[str, Any]:
        return {
            "parsedFramesTotal": 0,
            "parsedByType": defaultdict(int),
            "parsedBinaryFrames": 0,
            "parsedTextFrames": 0,
            "parsedStatuses": 0,
            "parsedEvents": 0,
            "skippedBytes": 0,
            "skippedLines": 0,
            "parseErrors": 0,
            "binaryCrcErrors": 0,
            "binaryMagicResyncs": 0,
            "binaryShortFrames": 0,
            "warnings": 0,
            "lastError": "",
            "lastWarning": "",
            "lastDeviceStatus": {},
            "lastDeviceEvent": {},
            "lastStatusCode": None,
            "lastStatusCodeName": "-",
        }


__all__ = [
    "DeviceEvent",
    "DeviceStatus",
    "MatrixFrame",
    "ParseResult",
    "SensorArrayStreamParser",
]
