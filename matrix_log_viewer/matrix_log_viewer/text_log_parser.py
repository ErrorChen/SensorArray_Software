from __future__ import annotations

import csv
import logging
import math
import re
import threading
from typing import Any

from .config import CELL_NAMES
from .protocol_types import DeviceEvent, DeviceStatus, MatrixFrame, ParseResult
from .sensorarray_status_codes import parseStatusCode, statusCodeName

LOGGER = logging.getLogger(__name__)

MATRIX_FRAME_TYPES = {"MATV", "MATV_RAW", "MATV_GAIN", "MATV_ERR"}
HEADER_TYPES = {f"{name}_HEADER": name for name in MATRIX_FRAME_TYPES}
CELL_NAME_RE = re.compile(r"^S[1-8]D[1-8]$")
EVENT_PREFIXES = {
    "EVENT",
    "RATE_EVENT",
    "RATE_FATAL",
    "VOLTSCAN_FATAL",
    "MATV_ABORT",
    "WARN",
    "WARNING",
    "ERROR",
}
STATUS_PREFIXES = {
    "STAT",
    "APPMODE",
    "APP_VERSION",
    "RESET_REASON",
    "BUILD_CONFIG",
    "FAST_BINARY_START",
    "FAST_BINARY_DIAG",
    "STREAM_INIT",
    "STREAM_MEM",
    "VOLTSCAN_INIT",
    "VOLTSCAN_GAIN",
    "ADS_FAST_CONFIG",
    "VOLTSCAN_CONFIG",
    "ROUTE_POLICY",
    "ADS_POLICY",
    "DBG",
    "DBGCTRL",
    "DBGROUTE",
    "DBGADSREF",
    "DBGADSREFPOLICY",
    "DBGROUTEPOLICY",
    "DBGTMUXPOLICY",
    "DBGREFCONFLICT",
}


def _typed_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _typed_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _typed_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        text = str(value).strip().lower()
        if text in {"true", "yes", "on"}:
            return True
        if text in {"false", "no", "off"}:
            return False
    return None


def _first_present(fields: dict, *keys: str) -> Any:
    for key in keys:
        if key in fields:
            return fields[key]
    return None


class TextLogParser:
    """Parse legacy MATV CSV rows and UpperSpeed startup/status text lines."""

    def __init__(self):
        self._lock = threading.RLock()
        self.headerCellsByType: dict[str, list[str]] = {}
        self.defaultCellNames = list(CELL_NAMES)
        self.headerSeen = False

        self.parsedFramesTotal = 0
        self.parsedByType: dict[str, int] = {}
        self.parsedStatuses = 0
        self.parsedEvents = 0
        self.skippedLines = 0
        self.parseErrors = 0
        self.warnings = 0
        self.lastError = ""
        self.lastWarning = ""

    def parseLine(self, line: str) -> ParseResult | None:
        with self._lock:
            try:
                stripped_line = (line or "").strip()
                if not stripped_line:
                    self._record_skip("empty line")
                    return None

                fields = self._split_csv(stripped_line)
                if not fields:
                    self._record_skip("empty CSV row")
                    return None

                prefix = fields[0].strip()
                if prefix in HEADER_TYPES:
                    self._parse_header(prefix, fields)
                    return None
                if prefix in MATRIX_FRAME_TYPES:
                    frame = self._parse_matrix_frame(prefix, fields, stripped_line)
                    if frame is None:
                        return None
                    self.parsedFramesTotal += 1
                    self.parsedByType[prefix] = self.parsedByType.get(prefix, 0) + 1
                    return ParseResult(frame=frame)
                if self._is_event_prefix(prefix):
                    event = self._parse_event(prefix, fields, stripped_line)
                    self.parsedEvents += 1
                    return ParseResult(event=event)
                if self._is_status_prefix(prefix):
                    status = self._parse_status(prefix, fields, stripped_line)
                    self.parsedStatuses += 1
                    return ParseResult(status=status)

                if self._looks_key_value_row(fields):
                    status = self._parse_status(prefix, fields, stripped_line)
                    self.parsedStatuses += 1
                    return ParseResult(status=status)

                self._record_skip(f"unknown text row: {prefix}")
                return None
            except Exception as exc:  # Defensive guard for corrupted logs.
                self._record_error(f"unexpected text parser error: {exc}")
                LOGGER.debug("Failed to parse text line: %r", line, exc_info=True)
                return None

    def getStats(self) -> dict:
        with self._lock:
            return {
                "parsedFramesTotal": self.parsedFramesTotal,
                "parsedByType": dict(self.parsedByType),
                "parsedStatuses": self.parsedStatuses,
                "parsedEvents": self.parsedEvents,
                "skippedLines": self.skippedLines,
                "parseErrors": self.parseErrors,
                "warnings": self.warnings,
                "lastError": self.lastError,
                "lastWarning": self.lastWarning,
                "headerSeen": self.headerSeen,
                "cellNames": list(self.defaultCellNames),
            }

    def _parse_header(self, prefix: str, fields: list[str]) -> None:
        frame_type = HEADER_TYPES[prefix]
        cell_fields = [field.strip() for field in fields if CELL_NAME_RE.match(field.strip())]
        if len(cell_fields) < len(CELL_NAMES):
            self._record_error(
                f"incomplete {prefix}: got {len(cell_fields)} cell names, expected {len(CELL_NAMES)}"
            )
            return

        if len(cell_fields) > len(CELL_NAMES):
            self._record_warning(f"{prefix} has extra cell names; ignoring after 64")
        cell_fields = cell_fields[: len(CELL_NAMES)]
        if len(set(cell_fields)) != len(CELL_NAMES):
            self._record_error(f"{prefix} contains duplicate cell names")
            return

        if frame_type == "MATV":
            self.defaultCellNames = list(cell_fields)
            for matrix_type in MATRIX_FRAME_TYPES:
                self.headerCellsByType.setdefault(matrix_type, list(cell_fields))
        self.headerCellsByType[frame_type] = list(cell_fields)
        self.headerSeen = True

    def _parse_matrix_frame(self, frame_type: str, fields: list[str], raw_line: str) -> MatrixFrame | None:
        if len(fields) < 3:
            self._record_error(f"incomplete {frame_type} row: got {len(fields)} fields")
            return None

        try:
            seq = int(fields[1].strip())
            timestamp_us = int(fields[2].strip())
        except ValueError as exc:
            self._record_error(f"invalid {frame_type} sequence/timestamp: {exc}")
            return None

        duration_us = 0
        unit = self._default_unit_for_type(frame_type)
        value_start = 3

        if frame_type == "MATV":
            if len(fields) < 5 + len(CELL_NAMES):
                self._record_error(
                    f"incomplete MATV row: got {len(fields)} fields, expected {5 + len(CELL_NAMES)}"
                )
                return None
            try:
                duration_us = int(fields[3].strip())
            except ValueError as exc:
                self._record_error(f"invalid MATV duration_us: {exc}")
                return None
            unit = fields[4].strip()
            value_start = 5
        elif len(fields) >= 5 + len(CELL_NAMES) and self._looks_int(fields[3]):
            duration_us = int(fields[3].strip())
            unit = fields[4].strip() or unit
            value_start = 5

        value_fields = fields[value_start:]
        if len(value_fields) < len(CELL_NAMES):
            self._record_error(
                f"incomplete {frame_type} row: got {len(value_fields)} values, expected {len(CELL_NAMES)}"
            )
            return None
        if len(value_fields) > len(CELL_NAMES):
            self._record_warning(
                f"{frame_type} row has {len(value_fields)} values; ignoring values after index 63"
            )
            value_fields = value_fields[: len(CELL_NAMES)]

        cell_names = self.headerCellsByType.get(frame_type) or self.defaultCellNames
        values: dict[str, float] = {}
        for cell_name, value_text in zip(cell_names, value_fields):
            try:
                values[cell_name] = float(value_text.strip())
            except ValueError:
                if frame_type == "MATV_ERR":
                    values[cell_name] = math.nan
                    self._record_warning(f"MATV_ERR non-numeric field for {cell_name}: {value_text!r}")
                else:
                    self._record_error(f"invalid {frame_type} numeric field for {cell_name}: {value_text!r}")
                    return None

        return MatrixFrame(
            frameType=frame_type,
            seq=seq,
            timestampUs=timestamp_us,
            durationUs=duration_us,
            unit=unit,
            values=values,
            rawLine=raw_line,
        )

    def _parse_status(self, prefix: str, fields: list[str], raw_line: str) -> DeviceStatus:
        parsed_fields = self._parse_key_values(fields[1:])
        typed = self._typed_key_values(parsed_fields)
        fast_start_meta = typed if prefix == "FAST_BINARY_START" else None
        fast_diag = typed if prefix == "FAST_BINARY_DIAG" else None
        return DeviceStatus(
            statusType=prefix,
            fields=parsed_fields,
            rawLine=raw_line,
            fastBinaryStartSeen=prefix == "FAST_BINARY_START",
            fastBinaryStartMeta=fast_start_meta,
            fastBinaryDiagLatest=fast_diag,
            pureBinaryMode=bool(typed.get("pure")) if prefix == "FAST_BINARY_START" else False,
            startupDiagWindowSeen=prefix in {"FAST_BINARY_DIAG", "FAST_BINARY_START", "BUILD_CONFIG", "VOLTSCAN_CONFIG", "STREAM_MEM"},
            droppedBeforeFirstByte=_typed_int(typed.get("droppedBeforeFirstByte")),
            partialAfterFirstByte=_typed_int(typed.get("partialAfterFirstByte")),
            fullFrameWriteCount=_typed_int(typed.get("fullFrameWriteCount")),
            fullFrameWriteFailCount=_typed_int(typed.get("fullFrameWriteFailCount")),
            dropPolicy=str(typed.get("dropPolicy")) if typed.get("dropPolicy") is not None else None,
            usbExactBinaryWrite=_typed_bool(typed.get("usbExactBinaryWrite")),
            fastBinaryStartupDiagMs=_typed_int(typed.get("fastBinaryStartupDiagMs")),
            latestScanFps=_typed_float(typed.get("scanFps")),
            latestOutFps=_typed_float(typed.get("outFps")),
            latestOutputDiv=_typed_int(_first_present(typed, "outputDiv", "outputDivider")),
            latestQUsed=_typed_int(typed.get("qUsed")),
            latestQFull=_typed_int(typed.get("qFull")),
            latestDrop=_typed_int(_first_present(typed, "drop", "droppedFrames")),
            latestDecimated=_typed_int(_first_present(typed, "decimated", "outputDecimatedFrames")),
        )

    def _parse_event(self, prefix: str, fields: list[str], raw_line: str) -> DeviceEvent:
        parsed_fields = self._parse_key_values(fields[1:])
        code = self._extract_code(parsed_fields)
        name = parsed_fields.get("name") or parsed_fields.get("action") or (statusCodeName(code) if code is not None else None)
        return DeviceEvent(
            eventType=prefix,
            code=code,
            name=name,
            fields=parsed_fields,
            rawLine=raw_line,
        )

    def _extract_code(self, fields: dict[str, str]) -> int | None:
        for key in ("code", "lastStatusCode", "statusCode", "firstStatusCode"):
            code = parseStatusCode(fields.get(key))
            if code is not None:
                return code
        return None

    @staticmethod
    def _parse_key_values(fields: list[str]) -> dict[str, str]:
        parsed: dict[str, str] = {}
        positional_index = 0
        for field in fields:
            item = field.strip()
            if not item:
                continue
            if "=" in item:
                key, value = item.split("=", maxsplit=1)
                parsed[key.strip()] = value.strip()
            else:
                parsed[f"arg{positional_index}"] = item
                positional_index += 1
        return parsed

    @staticmethod
    def _typed_key_values(fields: dict[str, str]) -> dict[str, int | float | str | bool]:
        typed: dict[str, int | float | str | bool] = {}
        for key, value in fields.items():
            text = str(value).strip()
            if text == "":
                typed[key] = ""
                continue
            lowered = text.lower()
            if lowered in {"true", "yes", "on"}:
                typed[key] = True
                continue
            if lowered in {"false", "no", "off"}:
                typed[key] = False
                continue
            try:
                typed[key] = int(text, 0)
                continue
            except ValueError:
                pass
            try:
                typed[key] = float(text)
                continue
            except ValueError:
                pass
            typed[key] = text
        return typed

    @staticmethod
    def _split_csv(line: str) -> list[str]:
        reader = csv.reader([line])
        return [field.strip() for field in next(reader)]

    @staticmethod
    def _default_unit_for_type(frame_type: str) -> str:
        if frame_type == "MATV_RAW":
            return "raw"
        if frame_type == "MATV_GAIN":
            return "gain"
        if frame_type == "MATV_ERR":
            return "err"
        return "uV"

    @staticmethod
    def _looks_int(value: Any) -> bool:
        try:
            int(str(value).strip(), 0)
            return True
        except ValueError:
            return False

    @staticmethod
    def _looks_key_value_row(fields: list[str]) -> bool:
        return any("=" in field for field in fields[1:])

    @staticmethod
    def _is_event_prefix(prefix: str) -> bool:
        return prefix in EVENT_PREFIXES or prefix.startswith("EVENT_") or prefix.startswith("WARN") or prefix.startswith("ERROR")

    @staticmethod
    def _is_status_prefix(prefix: str) -> bool:
        return prefix in STATUS_PREFIXES or prefix.startswith("DBG") or prefix.startswith("VOLTSCAN_")

    def _record_skip(self, reason: str) -> None:
        self.skippedLines += 1
        LOGGER.debug("Skipped text line: %s", reason)

    def _record_error(self, message: str) -> None:
        self.skippedLines += 1
        self.parseErrors += 1
        self.lastError = message
        LOGGER.debug("Text parse error: %s", message)

    def _record_warning(self, message: str) -> None:
        self.warnings += 1
        self.lastWarning = message
        LOGGER.debug("Text parse warning: %s", message)
