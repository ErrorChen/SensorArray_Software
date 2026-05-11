from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import asdict
from typing import Any

from .binary_frame_parser import (
    FRAME_TYPE_VOLTAGE_COMPACT,
    MAGIC_BYTES,
    MAGIC_U32,
    SIZE as BINARY_FRAME_SIZE,
    VERSION,
    BinaryFrameParseError,
    SensorArrayBinaryFrameParser,
)
from .protocol_types import DeviceEvent, DeviceStatus, MatrixFrame, ParseResult
from .sensorarray_status_codes import parseStatusCode, statusCodeName
from .text_log_parser import TextLogParser

LOGGER = logging.getLogger(__name__)

STATE_STARTUP_TEXT_OR_BINARY = "STARTUP_TEXT_OR_BINARY"
STATE_PURE_BINARY = "PURE_BINARY"
MAX_BUFFER_BYTES = 1_048_576
POLLUTION_TOKENS = (
    "STAT",
    "RATE_EVENT",
    "MATV",
    "MATV_HEADER",
    "MATV_RAW",
    "MATV_GAIN",
    "FAST_BINARY_DIAG",
    "seq,timestamp",
    "timestamp_us",
)


class SensorArrayStreamParser:
    """Byte-stream parser for UpperSpeed startup text followed by pure SAC1 binary."""

    def __init__(self, maxTextLineBytes: int = 4096, maxBufferBytes: int = MAX_BUFFER_BYTES):
        self._lock = threading.RLock()
        self._buffer = bytearray()
        self._binaryParser = SensorArrayBinaryFrameParser()
        self._textParser = TextLogParser()
        self.maxTextLineBytes = max(256, int(maxTextLineBytes))
        self.maxBufferBytes = max(BINARY_FRAME_SIZE * 2, int(maxBufferBytes))
        self._state = STATE_STARTUP_TEXT_OR_BINARY
        self._stats = self._empty_stats()

    def feed(self, data: bytes) -> list[ParseResult]:
        return self.feedBytes(data)

    def feedBytes(self, data: bytes) -> list[ParseResult]:
        if not data:
            return []

        with self._lock:
            self._buffer.extend(data)
            self._enforce_buffer_limit()
            return self._drain_buffer()

    def getStats(self) -> dict:
        with self._lock:
            stats = dict(self._stats)
            stats["parsedByType"] = dict(self._stats["parsedByType"])
            stats["lastDeviceStatus"] = dict(self._stats["lastDeviceStatus"])
            stats["lastDeviceEvent"] = dict(self._stats["lastDeviceEvent"])
            stats["bufferBytes"] = len(self._buffer)
            stats["bufferedBytes"] = len(self._buffer)
            stats["pureBinaryMode"] = self._state == STATE_PURE_BINARY
            stats["state"] = self._state
            stats["binaryFrameCount"] = stats.get("parsedBinaryFrames", 0)
            stats["startupTextLineCount"] = stats.get("startupTextLineCount", 0)
            return stats

    def recordHostQueueDrop(self, chunks: int = 0, bytesDropped: int = 0) -> None:
        with self._lock:
            self._stats["hostQueueDropChunks"] += max(0, int(chunks or 0))
            self._stats["hostQueueDropBytes"] += max(0, int(bytesDropped or 0))

    def reset(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._textParser = TextLogParser()
            self._state = STATE_STARTUP_TEXT_OR_BINARY
            self._stats = self._empty_stats()

    def _drain_buffer(self) -> list[ParseResult]:
        results: list[ParseResult] = []

        while self._buffer:
            before_len = len(self._buffer)
            before_state = self._state
            if self._state == STATE_PURE_BINARY:
                self._drain_pure_binary(results)
                break
            progressed = self._drain_startup_once(results)
            if not progressed:
                break
            if before_len == len(self._buffer) and before_state == self._state:
                break

        return results

    def _drain_startup_once(self, results: list[ParseResult]) -> bool:
        if not self._buffer:
            return False

        if self._buffer.startswith(MAGIC_BYTES):
            if len(self._buffer) < BINARY_FRAME_SIZE:
                self._stats["shortFrameWaits"] += 1
                return False
            return self._parse_binary_candidate(results)

        magic_index = self._buffer.find(MAGIC_BYTES)
        newline_index = self._buffer.find(b"\n")

        raw_line_candidate = bytes(self._buffer[: newline_index + 1]) if newline_index >= 0 else b""
        if newline_index >= 0 and (
            magic_index < 0
            or newline_index < magic_index
            or (not self._buffer.startswith(MAGIC_BYTES) and self._looks_like_text_fragment(bytearray(raw_line_candidate.rstrip(b"\r\n"))))
        ):
            raw_line = bytes(self._buffer[: newline_index + 1])
            del self._buffer[: newline_index + 1]
            raw_line = self._drop_non_text_line_prefix(raw_line)
            if not raw_line:
                return True
            result = self._parse_text_bytes(raw_line)
            if result is not None and result.hasData():
                self._record_result(result, binary=False)
                results.append(result)
                if result.status is not None and result.status.statusType == "FAST_BINARY_START":
                    self._handle_fast_binary_start(result.status)
                    if self._state == STATE_PURE_BINARY and self._buffer:
                        self._drain_pure_binary(results)
                return True
            return True

        if magic_index > 0:
            skipped = bytes(self._buffer[:magic_index])
            self._record_resync(len(skipped))
            del self._buffer[:magic_index]
            return True

        if magic_index == 0:
            return True

        if newline_index < 0:
            if self._looks_like_text_fragment(self._buffer) and len(self._buffer) <= self.maxTextLineBytes:
                return False
            self._trim_garbage_tail()
            return False

        return False

    def _drain_pure_binary(self, results: list[ParseResult]) -> None:
        while self._buffer:
            magic_index = self._buffer.find(MAGIC_BYTES)
            if magic_index < 0:
                keep = len(MAGIC_BYTES) - 1
                if len(self._buffer) > keep:
                    segment = bytes(self._buffer[:-keep])
                    event = self._record_protocol_garbage(segment)
                    if event is not None:
                        results.append(event)
                    self._stats["skippedBytes"] += len(segment)
                    self._stats["binaryMagicResyncs"] += 1
                    del self._buffer[:-keep]
                break

            if magic_index > 0:
                segment = bytes(self._buffer[:magic_index])
                event = self._record_protocol_garbage(segment)
                if event is not None:
                    results.append(event)
                self._record_resync(magic_index)
                del self._buffer[:magic_index]
                continue

            if len(self._buffer) < BINARY_FRAME_SIZE:
                self._stats["shortFrameWaits"] += 1
                break

            self._parse_binary_candidate(results)

    def _parse_binary_candidate(self, results: list[ParseResult]) -> bool:
        raw_frame = bytes(self._buffer[:BINARY_FRAME_SIZE])
        try:
            frame = self._binaryParser.parseFrame(raw_frame)
        except BinaryFrameParseError as exc:
            self._record_binary_error(exc)
            self._resync_after_binary_error(exc)
            return True

        del self._buffer[:BINARY_FRAME_SIZE]
        result = ParseResult(frame=frame)
        self._record_result(result, binary=True)
        results.append(result)
        return True

    def _resync_after_binary_error(self, exc: BinaryFrameParseError) -> None:
        search_limit = min(len(self._buffer), BINARY_FRAME_SIZE + len(MAGIC_BYTES))
        next_magic = self._buffer.find(MAGIC_BYTES, 1, search_limit)
        if next_magic > 0:
            self._record_resync(next_magic)
            del self._buffer[:next_magic]
            return
        if exc.kind == "crc":
            del self._buffer[:1]
            self._stats["skippedBytes"] += 1
            self._stats["binaryMagicResyncs"] += 1
            return
        del self._buffer[:1]

    def _parse_text_bytes(self, raw_line: bytes) -> ParseResult | None:
        before = self._textParser.getStats()
        line = raw_line.rstrip(b"\r\n").decode("utf-8", errors="replace")
        self._stats["startupTextLineCount"] += 1
        result = self._textParser.parseLine(line)
        after = self._textParser.getStats()
        self._merge_text_stat_delta(before, after)
        if result and result.status is not None:
            status_type = result.status.statusType
            if status_type == "FAST_BINARY_DIAG":
                self._stats["fastBinaryDiagCount"] += 1
            elif status_type == "FAST_BINARY_START":
                self._stats["fastBinaryStartSeen"] = True
        return result

    def _handle_fast_binary_start(self, status: DeviceStatus) -> None:
        fields = status.fields
        meta = _parse_marker_meta(fields)
        self._stats["fastBinaryStartSeen"] = True
        self._stats["fastBinaryStartMeta"] = dict(meta)
        valid = (
            meta.get("magic") == MAGIC_U32
            and meta.get("magicBytes") == "SAC1"
            and meta.get("version") == VERSION
            and meta.get("frameType") == FRAME_TYPE_VOLTAGE_COMPACT
            and meta.get("frameSize") == BINARY_FRAME_SIZE
        )
        if not valid:
            self._stats["warnings"] += 1
            self._stats["lastWarning"] = "FAST_BINARY_START metadata did not match UpperSpeed SAC1/312-byte protocol"
        if bool(meta.get("pure", False)) or valid:
            self._state = STATE_PURE_BINARY
            self._stats["pureBinaryMode"] = True

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
                self._update_seq_stats(frame)
            else:
                self._stats["parsedTextFrames"] += 1
            self._update_status_code_from_frame(frame)
        if result.status is not None:
            self._stats["parsedStatuses"] += 1
            self._stats["lastDeviceStatus"] = asdict(result.status)
            self._update_device_status_from_status(result.status)
            self._update_status_code_from_fields(result.status.fields)
        if result.event is not None:
            self._stats["parsedEvents"] += 1
            self._stats["lastDeviceEvent"] = asdict(result.event)
            if result.event.code is not None:
                self._update_status_code(result.event.code)

    def _update_seq_stats(self, frame: MatrixFrame) -> None:
        if frame.seq is None:
            return
        seq = int(frame.seq)
        previous = self._stats.get("lastGoodSeq")
        if previous is not None and seq > int(previous) + 1:
            gap = seq - int(previous) - 1
            self._stats["seqGapCount"] += 1
            self._stats["seqGapTotal"] += gap
        self._stats["lastGoodSeq"] = seq

    def _update_device_status_from_status(self, status: DeviceStatus) -> None:
        if status.fastBinaryStartSeen:
            self._stats["fastBinaryStartSeen"] = True
            self._stats["fastBinaryStartMeta"] = dict(status.fastBinaryStartMeta or {})
        if status.fastBinaryDiagLatest:
            self._stats["fastBinaryDiagLatest"] = dict(status.fastBinaryDiagLatest)
        if status.partialAfterFirstByte and status.partialAfterFirstByte > 0:
            self._stats["warnings"] += 1
            self._stats["lastWarning"] = "PROTOCOL_RISK: firmware reported partialAfterFirstByte > 0"

    def _record_binary_error(self, exc: BinaryFrameParseError) -> None:
        if exc.kind == "crc":
            self._stats["binaryCrcErrors"] += 1
        elif exc.kind == "short":
            self._stats["binaryShortFrames"] += 1
            self._stats["shortFrameWaits"] += 1
        else:
            self._stats["parseErrors"] += 1
        self._stats["lastError"] = str(exc)
        LOGGER.debug("Binary parse error: %s", exc)

    def _record_resync(self, skipped: int) -> None:
        self._stats["skippedBytes"] += skipped
        self._stats["binaryMagicResyncs"] += 1

    def _record_protocol_garbage(self, segment: bytes) -> ParseResult | None:
        if not segment:
            return None
        text = _printable_ascii_snippet(segment)
        if not text:
            return None
        if any(token in text for token in POLLUTION_TOKENS):
            self._stats["protocolPollutionCount"] += 1
            self._stats["lastProtocolPollutionSnippet"] = text[:200]
            self._stats["warnings"] += 1
            self._stats["lastWarning"] = "ASCII_AFTER_FAST_BINARY_START"
            event = DeviceEvent(
                eventType="ASCII_AFTER_FAST_BINARY_START",
                code=None,
                name="ASCII_AFTER_FAST_BINARY_START",
                fields={"snippet": text[:200], "pureBinaryMode": "1"},
                rawLine=text[:200],
            )
            self._record_result(ParseResult(event=event), binary=False)
            return ParseResult(event=event)
        return None

    def _trim_garbage_tail(self) -> None:
        keep = len(MAGIC_BYTES) - 1
        if len(self._buffer) <= keep:
            return
        skipped = len(self._buffer) - keep
        self._stats["skippedBytes"] += skipped
        del self._buffer[:skipped]

    def _enforce_buffer_limit(self) -> None:
        if len(self._buffer) <= self.maxBufferBytes:
            return
        keep = BINARY_FRAME_SIZE - 1
        skipped = len(self._buffer) - keep
        del self._buffer[:skipped]
        self._stats["skippedBytes"] += skipped
        self._stats["bufferOverflowCount"] += 1
        self._stats["lastWarning"] = f"parser buffer overflow; skipped {skipped} bytes"
        self._stats["warnings"] += 1

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
            "RESET_REASON",
            "APPMODE",
            "APP_VERSION",
            "BUILD_CONFIG",
            "VOLTSCAN",
            "STREAM_INIT",
            "STREAM_MEM",
            "FAST_BINARY_START",
            "FAST_BINARY_DIAG",
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
            "ADS_",
            "ROUTE_",
            "DBG",
            "WARN",
            "WARNING",
            "ERROR",
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
            "binaryFrameCount": 0,
            "startupTextLineCount": 0,
            "fastBinaryDiagCount": 0,
            "fastBinaryStartSeen": False,
            "fastBinaryStartMeta": {},
            "fastBinaryDiagLatest": {},
            "pureBinaryMode": False,
            "skippedBytes": 0,
            "skippedLines": 0,
            "parseErrors": 0,
            "binaryCrcErrors": 0,
            "binaryMagicResyncs": 0,
            "binaryShortFrames": 0,
            "shortFrameWaits": 0,
            "protocolPollutionCount": 0,
            "lastProtocolPollutionSnippet": "",
            "hostQueueDropChunks": 0,
            "hostQueueDropBytes": 0,
            "bufferOverflowCount": 0,
            "warnings": 0,
            "lastError": "",
            "lastWarning": "",
            "lastDeviceStatus": {},
            "lastDeviceEvent": {},
            "lastStatusCode": None,
            "lastStatusCodeName": "-",
            "lastGoodSeq": None,
            "seqGapCount": 0,
            "seqGapTotal": 0,
        }


def _parse_marker_meta(fields: dict[str, str]) -> dict[str, int | str | bool]:
    meta: dict[str, int | str | bool] = {}
    for key, value in fields.items():
        text = str(value).strip()
        if key == "magicBytes":
            meta[key] = text
        elif key == "pure":
            try:
                meta[key] = bool(int(text, 0))
            except ValueError:
                meta[key] = text.lower() in {"true", "yes", "on"}
        else:
            try:
                meta[key] = int(text, 0)
            except ValueError:
                meta[key] = text
    return meta


def _printable_ascii_snippet(segment: bytes) -> str:
    filtered = bytearray()
    for value in segment[:4096]:
        if value in (9, 10, 13) or 32 <= value <= 126:
            filtered.append(value)
        elif filtered and filtered[-1] != 32:
            filtered.append(32)
    return bytes(filtered).decode("ascii", errors="ignore").strip()


__all__ = [
    "DeviceEvent",
    "DeviceStatus",
    "MatrixFrame",
    "ParseResult",
    "SensorArrayStreamParser",
    "STATE_STARTUP_TEXT_OR_BINARY",
    "STATE_PURE_BINARY",
]
