from __future__ import annotations

import csv
import logging
import re
import threading
from dataclasses import dataclass
from typing import Optional

from .config import CELL_NAMES

LOGGER = logging.getLogger(__name__)

BASE_HEADER_FIELDS = ["seq", "timestamp_us", "duration_us", "unit"]
EXPECTED_FIELD_COUNT = 5 + len(CELL_NAMES)
CELL_NAME_RE = re.compile(r"^S[1-8]D[1-8]$")


@dataclass(frozen=True)
class MatvFrame:
    seq: int
    timestampUs: int
    durationUs: int
    unit: str
    values: dict[str, float]
    rawLine: str


class MatvParser:
    """Parse MATV_HEADER/MATV CSV log lines without letting bad lines escape."""

    def __init__(self):
        self._lock = threading.RLock()
        self.headerFields: list[str] | None = None
        self.cellNames: list[str] = list(CELL_NAMES)
        self.headerSeen = False

        self.matvFramesParsed = 0
        self.skippedLines = 0
        self.parseErrors = 0
        self.warnings = 0
        self.lastError = ""
        self.lastWarning = ""

    def parseLine(self, line: str) -> Optional[MatvFrame]:
        with self._lock:
            try:
                stripped_line = (line or "").strip()
                if not stripped_line:
                    self._record_skip("empty line")
                    return None

                if stripped_line.startswith("MATV_HEADER,"):
                    return self._parse_header(stripped_line)

                if stripped_line.startswith("MATV,"):
                    return self._parse_matv(stripped_line)

                self._record_skip("non-MATV line")
                return None
            except Exception as exc:  # Defensive guard for corrupted input.
                self._record_error(f"unexpected parser error: {exc}")
                LOGGER.debug("Failed to parse line: %r", line, exc_info=True)
                return None

    def getStats(self) -> dict:
        with self._lock:
            return {
                "matvFramesParsed": self.matvFramesParsed,
                "skippedLines": self.skippedLines,
                "parseErrors": self.parseErrors,
                "warnings": self.warnings,
                "lastError": self.lastError,
                "lastWarning": self.lastWarning,
                "headerSeen": self.headerSeen,
                "cellNames": list(self.cellNames),
            }

    def _parse_header(self, line: str) -> None:
        fields = self._split_csv(line)

        if len(fields) < EXPECTED_FIELD_COUNT:
            self._record_error(
                f"incomplete MATV_HEADER: got {len(fields)} fields, expected {EXPECTED_FIELD_COUNT}"
            )
            return None

        if fields[0] != "MATV_HEADER":
            self._record_error("invalid MATV_HEADER prefix")
            return None

        base_fields = fields[1:5]
        if base_fields != BASE_HEADER_FIELDS:
            self._record_error(
                "invalid MATV_HEADER base fields: "
                f"got {base_fields}, expected {BASE_HEADER_FIELDS}"
            )
            return None

        cell_fields = fields[5:EXPECTED_FIELD_COUNT]
        bad_cells = [name for name in cell_fields if not CELL_NAME_RE.match(name)]
        if bad_cells:
            self._record_error(f"invalid MATV_HEADER cell names: {bad_cells[:5]}")
            return None

        if len(set(cell_fields)) != len(CELL_NAMES):
            self._record_error("MATV_HEADER contains duplicate cell names")
            return None

        if len(fields) > EXPECTED_FIELD_COUNT:
            self._record_warning(
                f"MATV_HEADER has {len(fields)} fields; ignoring fields after index {EXPECTED_FIELD_COUNT - 1}"
            )

        self.headerFields = fields[:EXPECTED_FIELD_COUNT]
        self.cellNames = list(cell_fields)
        self.headerSeen = True
        LOGGER.info("MATV header accepted with %d matrix cells", len(cell_fields))
        return None

    def _parse_matv(self, line: str) -> Optional[MatvFrame]:
        fields = self._split_csv(line)

        if len(fields) < EXPECTED_FIELD_COUNT:
            self._record_error(
                f"incomplete MATV row: got {len(fields)} fields, expected {EXPECTED_FIELD_COUNT}"
            )
            return None

        if len(fields) > EXPECTED_FIELD_COUNT:
            self._record_warning(
                f"MATV row has {len(fields)} fields; ignoring fields after index {EXPECTED_FIELD_COUNT - 1}"
            )
            fields = fields[:EXPECTED_FIELD_COUNT]

        try:
            seq = int(fields[1])
            timestamp_us = int(fields[2])
            duration_us = int(fields[3])
            unit = str(fields[4])
            values = {
                cell_name: float(value_text)
                for cell_name, value_text in zip(self.cellNames, fields[5:EXPECTED_FIELD_COUNT])
            }
        except ValueError as exc:
            self._record_error(f"invalid MATV numeric field: {exc}")
            return None

        self.matvFramesParsed += 1
        return MatvFrame(
            seq=seq,
            timestampUs=timestamp_us,
            durationUs=duration_us,
            unit=unit,
            values=values,
            rawLine=line,
        )

    @staticmethod
    def _split_csv(line: str) -> list[str]:
        reader = csv.reader([line])
        return [field.strip() for field in next(reader)]

    def _record_skip(self, reason: str) -> None:
        self.skippedLines += 1
        LOGGER.debug("Skipped line: %s", reason)

    def _record_error(self, message: str) -> None:
        self.skippedLines += 1
        self.parseErrors += 1
        self.lastError = message
        LOGGER.debug("MATV parse error: %s", message)

    def _record_warning(self, message: str) -> None:
        self.warnings += 1
        self.lastWarning = message
        LOGGER.debug("MATV parse warning: %s", message)

