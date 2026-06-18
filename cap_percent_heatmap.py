#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cap_percent_heatmap_cf63.py

Single-file capacitance percentage-change heatmap tool for SensorArray_Software,
adapted for SensorArray firmware commit cf63c186b8c80ab982a9791d673667aa696166fd.

Purpose
-------
This script reads an 8x8 capacitance matrix from a serial port, a replay log, or a demo
source. For cf63 firmware, the primary supported live frame is:

    Cap,s=<seq>,t=<us>,pa=<0|1>,q=<F|P>,cm=0x...,fm=0x...,wm=0x...,em=0x...,iv=-1.000000,pf=[64 pF values]

The legacy `MATRIXFDC_CAP,...,pf=[...]` form is still accepted. Diagnostic compact
records such as S/P5/R5/T5/Q5/I5/RS/D4/OT/OV/BN are displayed in the log panel but
are deliberately excluded from heatmap parsing.

After the embedded side finishes its initial full sweep, the script collects a
per-cell baseline for a configurable duration, then plots:

    percentChange = (C - C0) / abs(C0) * 100

where C0 is the per-cell baseline capacitance, and C is the live capacitance value.

Why the baseline is collected after full sweep
---------------------------------------------
The embedded FDC measurement code may spend the first few seconds doing full sweep,
fast sweep, channel setup, cache rebuild, or recovery. Those values are not a stable
"resting" capacitance. This tool therefore waits for a full-sweep-done marker when it
can find one. If no explicit marker exists, it falls back to waiting for a few complete
non-sweep matrix frames before starting the 10-second baseline window.

Installation
------------
    pip install numpy matplotlib pyserial

Examples
--------
    python tools/cap_percent_heatmap.py --port COM7 --baud 115200
    python tools/cap_percent_heatmap.py --port /dev/ttyACM0 --baud 115200
    python tools/cap_percent_heatmap.py --demo
    python tools/cap_percent_heatmap.py --replay-log logs/run.txt --replay-speed 5

Recommended fixed colour scale for early experiments:
    python tools/cap_percent_heatmap.py --port COM7 --vmin -20 --vmax 20

Notes
-----
- Invalid values such as -1, NaN, and inf are masked and shown as NA.
- Unitless numeric matrices are assumed to be pF. Percentage change is unit-invariant
  as long as live C and baseline C0 use the same unit.
- Frequency-only frames such as Hz/kHz/MHz/GHz are deliberately rejected. Do not fake
  capacitance from frequency unless the LC conversion constants are known.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import math
import os
import queue
import random
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import numpy as np

try:
    import serial  # type: ignore
    import serial.tools.list_ports  # type: ignore
except Exception:  # pragma: no cover - handled at runtime
    serial = None

try:
    import matplotlib.pyplot as plt
    import matplotlib.animation as animation
except Exception as matplotlibImportError:  # pragma: no cover - handled at runtime
    plt = None
    animation = None
    _MATPLOTLIB_IMPORT_ERROR = matplotlibImportError
else:
    _MATPLOTLIB_IMPORT_ERROR = None


ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
# Capture a number and an optional nearby unit. Negative lookbehind avoids capturing S1/D3 labels.
NUMBER_WITH_UNIT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"([-+]?(?:(?:\d+\.\d*)|(?:\.\d+)|(?:\d+))(?:[eE][-+]?\d+)?)"
    r"\s*"
    r"(fF|pF|nF|uF|µF|mF|F|ff|pf|nf|uf|µf|mf|"
    r"Hz|hz|kHz|KHz|khz|MHz|mhz|Mhz|GHz|ghz|Ghz)?"
)

CAPACITANCE_UNITS = {"ff", "pf", "nf", "uf", "µf", "mf", "F"}
FREQUENCY_UNITS = {"hz", "khz", "mhz", "ghz"}

FULL_SWEEP_DONE_PATTERNS = [
    re.compile(r"full[_\s-]*sweep.*(?:done|complete|completed|finish|finished|ok)", re.IGNORECASE),
    re.compile(r"(?:done|complete|completed|finish|finished|ok).*full[_\s-]*sweep", re.IGNORECASE),
    re.compile(r"fdc.*sweep.*(?:done|complete|completed|finish|finished|ok)", re.IGNORECASE),
    re.compile(r"sweep.*(?:done|complete|completed|finish|finished|ok)", re.IGNORECASE),
    re.compile(r"normal[_\s-]*(?:read|frame|mode)", re.IGNORECASE),
    re.compile(r"matrix[_\s-]*(?:ready|live)", re.IGNORECASE),
]

SWEEP_OR_CALIBRATION_WORDS = (
    "sweep",
    "full",
    "fast",
    "calib",
    "calibration",
    "scan",
    "reconfig",
    "config",
    "profile",
    "cache",
)

ERROR_WORDS = (
    "error",
    "err=",
    "timeout",
    "fail",
    "failed",
    "nack",
    "offline",
    "watchdog",
    "abort",
)

# Lines containing these tokens are usually low-level FDC/I2C/debug records, not a
# complete 8x8 capacitance matrix. V2 was too permissive and could misread lines like
#   FDC_DEVICE_READ4, dev=primary, s=2, raw28=[...]
# as a 64-value frame. That caused baseline collection to start during sweep.
KNOWN_NON_MATRIX_WORDS = (
    "fdc_device_read",
    "fdc_device_read4",
    "raw28",
    "raw=",
    "raw[",
    "dev=",
    "device=",
    "board_i2c_xfer",
    "i2c_xfer",
    "addr=",
    "elapsedus=",
    "nackcount",
    "timeoutcount",
    "status=",
    "drdy=",
    "unread=",
)

# Positive hints that a line is intended to be a complete display frame. Keep this
# deliberately broad for temporary firmware formats, but never broad enough to accept
# raw device debug lines.
FRAME_HINT_WORDS = (
    "cap_frame",
    "capframe",
    "cap_matrix",
    "capmatrix",
    "capacitance",
    "matrix",
    "frame",
    "matc",
    "fdc_cap",
    "pf",
)

# cf63 formal capacitance frame tags. `Cap` is the default; `MATRIXFDC_CAP` is kept
# for old builds compiled with CONFIG_SENSORARRAY_OUTPUT_LEGACY_MATRIXFDC_CAP=y.
FORMAL_CAP_FRAME_TAGS = {"cap", "matrixfdc_cap"}

# cf63 compact diagnostics. Many of these are comma-separated key/value lines and
# contain several numbers. They must never be assembled into fake row buffers.
CF63_COMPACT_NON_MATRIX_TAGS = {
    "s",      # frame summary, not the S-row of the physical matrix
    "fo",     # legacy frame-output diagnostic
    "ot",
    "ov",
    "bn",
    "rb",
    "sti",
    "stt",
    "sth",
    "stm",
    "rwt",
    "rws",
    "rr",
    "sr",
    "rwd",
    "fb",
    "rs",
    "re",
    "ps",
    "d4",
    "d4c",
    "fdc_result_merge_bug",
    "fdc_cache_miss",
    "fdc_deferred_repair",
    "fdc_rescue",
    "fdc_rescue_decision",
    "fdc_rescue_suppressed",
    "p5",
    "p5_full",
    "pr",
    "pfu",
    "r5",
    "t5",
    "q5",
    "i5",
    "ca5",
    "cawarn",
    "matrixfdc_freq",
    "matrixfdc_diag",
    "debugfdc_raw",
    "frame_error",
    "app_fatal",
}

PF_ARRAY_KEYS = ("pf", "capPf", "cap_pf", "capTotalPf", "cap_total_pf", "cap")


@dataclass
class HeatmapConfig:
    port: Optional[str]
    baud: int
    rows: int
    cols: int
    baselineSeconds: float
    minBaselineSamples: int
    stableFramesBeforeBaseline: int
    fullSweepMaxWaitSeconds: float
    postSweepStableFrames: int
    postSweepMinDelaySeconds: float
    showLogPanel: bool
    logPanelLines: int
    logMaxLines: int
    printRawLog: bool
    saveRawLogPath: Optional[str]
    firstMatrixStartsBaseline: bool
    minValidCellsToStartBaseline: int
    vmin: Optional[float]
    vmax: Optional[float]
    cmap: str
    demo: bool
    replayLog: Optional[str]
    replaySpeed: float
    replayLineDelay: float
    saveBaselinePath: Optional[str]
    saveCsvPath: Optional[str]
    textDecimals: int
    updateIntervalMs: int
    serialTimeout: float
    reconnectSerial: bool
    reconnectDelaySeconds: float
    serialIdleReconnectSeconds: float
    noWaitFullSweep: bool
    minAbsAutoScale: float
    robustPercentile: float
    maxQueueDrainPerUpdate: int
    assumeUnit: str


@dataclass
class MatrixFrame:
    timestamp: float
    values: np.ndarray
    unit: str = "pF"
    rawLine: Optional[str] = None
    source: str = "unknown"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReaderEvent:
    eventType: str
    timestamp: float
    message: str
    rawLine: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


QueueItem = Union[MatrixFrame, ReaderEvent]


def stripAnsi(text: str) -> str:
    return ANSI_ESCAPE_PATTERN.sub("", text)


def nowIsoString() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def getGitCommitOrUnknown() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        )
        commit = result.stdout.strip()
        if result.returncode == 0 and commit:
            return commit
    except Exception:
        pass
    return "unknown"


def isInvalidNumericValue(value: float) -> bool:
    if value is None:
        return True
    if not np.isfinite(value):
        return True
    # The embedded side has used -1 as an error sentinel. It must not enter baseline.
    if abs(value + 1.0) < 1e-12:
        return True
    return False


def sanitizeMatrix(matrix: np.ndarray) -> np.ndarray:
    matrix = matrix.astype(float, copy=True)
    invalidMask = ~np.isfinite(matrix) | np.isclose(matrix, -1.0, atol=1e-12, rtol=0.0)
    matrix[invalidMask] = np.nan
    return matrix


def normaliseUnit(unit: Optional[str]) -> str:
    if not unit:
        return ""
    return unit.strip().replace("μ", "µ")


def isFrequencyUnit(unit: Optional[str]) -> bool:
    unit = normaliseUnit(unit).lower()
    return unit in FREQUENCY_UNITS


def isCapacitanceUnit(unit: Optional[str]) -> bool:
    if not unit:
        return False
    normalised = normaliseUnit(unit)
    if normalised == "F":
        return True
    return normalised.lower() in {"ff", "pf", "nf", "uf", "µf", "mf"}


def capacitanceToPf(value: float, unit: Optional[str], assumeUnit: str = "pF") -> float:
    """Convert a capacitance value to pF. Unitless values are interpreted as assumeUnit."""
    unit = normaliseUnit(unit)
    if not unit:
        unit = assumeUnit
    if unit == "F":
        return value * 1e12
    lowerUnit = unit.lower()
    if lowerUnit == "ff":
        return value * 1e-3
    if lowerUnit == "pf":
        return value
    if lowerUnit == "nf":
        return value * 1e3
    if lowerUnit in ("uf", "µf"):
        return value * 1e6
    if lowerUnit == "mf":
        return value * 1e9
    # Fallback: keep unitless-like numbers as pF rather than crashing parser.
    return value


def detectFullSweepDone(line: str) -> bool:
    cleanLine = stripAnsi(line).strip()
    if not cleanLine:
        return False
    for pattern in FULL_SWEEP_DONE_PATTERNS:
        if pattern.search(cleanLine):
            return True
    return False


def isSweepOrCalibrationLine(line: Optional[str]) -> bool:
    if not line:
        return False
    lowerLine = line.lower()
    return any(word in lowerLine for word in SWEEP_OR_CALIBRATION_WORDS)


def isLikelyErrorLine(line: Optional[str]) -> bool:
    if not line:
        return False
    lowerLine = line.lower()
    return any(word in lowerLine for word in ERROR_WORDS)


def isKnownNonMatrixDataLine(line: Optional[str]) -> bool:
    """Return True for low-level device/debug records that must not become heatmap frames."""
    if not line:
        return False
    if isFormalCapFrameLine(line):
        return False
    if isCf63CompactNonMatrixLine(line):
        return True
    lowerLine = line.lower()
    return any(word in lowerLine for word in KNOWN_NON_MATRIX_WORDS)


def hasFrameHint(line: Optional[str]) -> bool:
    if not line:
        return False
    lowerLine = line.lower()
    return any(word in lowerLine for word in FRAME_HINT_WORDS)


def getLineTag(line: Optional[str]) -> str:
    """Return the first comma/colon/space separated tag in lower case."""
    if not line:
        return ""
    stripped = stripAnsi(line).strip()
    if not stripped:
        return ""
    firstToken = re.split(r"[,\s:]+", stripped, maxsplit=1)[0]
    return firstToken.strip().lower()


def isFormalCapFrameLine(line: Optional[str]) -> bool:
    return getLineTag(line) in FORMAL_CAP_FRAME_TAGS


def isCf63CompactNonMatrixLine(line: Optional[str]) -> bool:
    tag = getLineTag(line)
    if not tag:
        return False
    return tag in CF63_COMPACT_NON_MATRIX_TAGS


def isPureNumericMatrixLine(line: str, expectedCount: int) -> bool:
    """Accept a bare 64-value CSV/array line, but reject debug key=value records."""
    stripped = stripAnsi(line).strip()
    if not stripped:
        return False
    if "=" in stripped:
        return False
    # Remove brackets and separators, then require only numeric syntax. This permits:
    # [1,2,...] or 1,2,... but rejects FDC_DEVICE_READ4/raw28/etc.
    simplified = stripped.replace("[", " ").replace("]", " ").replace(",", " ").replace(";", " ")
    if re.search(r"[A-Za-z_]", simplified):
        return False
    pairs = extractNumberUnitPairs(stripped)
    if len(pairs) != expectedCount:
        return False
    return True


def lineLooksLikeCompleteCapacitanceFrame(line: str, pairs: Sequence[Tuple[float, Optional[str]]], expectedCount: int) -> bool:
    """Conservative full-frame gate before interpreting many numbers as an 8x8 matrix."""
    if isKnownNonMatrixDataLine(line):
        return False
    if len(pairs) < expectedCount:
        return False
    capacitanceUnitCount = sum(1 for _, unit in pairs if isCapacitanceUnit(unit))
    frequencyUnitCount = sum(1 for _, unit in pairs if isFrequencyUnit(unit))
    if frequencyUnitCount >= expectedCount and capacitanceUnitCount == 0:
        return False
    if capacitanceUnitCount > 0:
        return True
    if hasFrameHint(line):
        return True
    # Allow only truly bare numeric lines as unitless pF frames.
    if isPureNumericMatrixLine(line, expectedCount):
        return True
    return False


def extractNumberUnitPairs(line: str) -> List[Tuple[float, Optional[str]]]:
    pairs: List[Tuple[float, Optional[str]]] = []
    for match in NUMBER_WITH_UNIT_PATTERN.finditer(line):
        numberText = match.group(1)
        unitText = match.group(2)
        try:
            value = float(numberText)
        except ValueError:
            continue
        pairs.append((value, unitText))
    return pairs


def extractKeyValue(line: str, key: str) -> Optional[str]:
    match = re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}\s*=\s*([^,\]\s]+)", line, re.IGNORECASE)
    if match is None:
        return None
    return match.group(1).strip()


def detectFrequencyOnlyMatrixLine(line: str, expectedCount: int) -> bool:
    pairs = extractNumberUnitPairs(line)
    if len(pairs) < expectedCount:
        return False
    frequencyCount = sum(1 for _, unit in pairs if isFrequencyUnit(unit))
    capacitanceCount = sum(1 for _, unit in pairs if isCapacitanceUnit(unit))
    lowerLine = line.lower()
    mentionsFrequency = "freq" in lowerLine or "frequency" in lowerLine or "hz" in lowerLine
    mentionsCapacitance = "cap" in lowerLine or "pf" in lowerLine or "capacitance" in lowerLine
    return frequencyCount >= expectedCount and capacitanceCount == 0 and mentionsFrequency and not mentionsCapacitance


def shortenForLog(text: str, maxLength: int = 160) -> str:
    text = " ".join(str(text).split())
    if len(text) <= maxLength:
        return text
    return text[: maxLength - 3] + "..."


def listSerialPorts() -> List[str]:
    if serial is None:
        return []
    try:
        return [port.device for port in serial.tools.list_ports.comports()]
    except Exception:
        return []


class FallbackMatrixParser:
    """
    Wide but conservative parser for temporary experiments.

    It supports:
    1. Python/JSON-like nested 8x8 list: [[...], [...], ...]
    2. Flat 64-value line: 1,2,3,...,64
    3. Eight consecutive row lines containing 8 numbers each
    4. Values with pF/fF/nF/uF/F units, converted to pF

    It deliberately ignores frequency-only matrices. A capacitance heatmap should not be
    faked from frequency unless the LC conversion constants are known.
    """

    def __init__(self, rows: int, cols: int, assumeUnit: str = "pF") -> None:
        self.rows = rows
        self.cols = cols
        self.expectedCount = rows * cols
        self.assumeUnit = assumeUnit
        self.rowBuffer: List[List[float]] = []
        self.rowBufferFirstTimestamp: Optional[float] = None
        self.parseErrorCount = 0
        self.parsedFrameCount = 0
        self.frequencyOnlyMatrixCount = 0

    def resetRowBuffer(self) -> None:
        self.rowBuffer.clear()
        self.rowBufferFirstTimestamp = None

    def _tryParseCf63CapFrame(self, line: str, source: str) -> Optional[MatrixFrame]:
        """Parse cf63 formal `Cap,...,pf=[...]` or legacy `MATRIXFDC_CAP,...,pf=[...]` lines.

        The generic parser is intentionally permissive for old temporary logs. cf63 emits many
        compact diagnostic key/value lines, so the hot path should explicitly parse only the
        formal frame tag and the pF array.
        """
        if not isFormalCapFrameLine(line):
            return None

        arrayText: Optional[str] = None
        arrayKey: Optional[str] = None
        for key in PF_ARRAY_KEYS:
            # Match `pf = [` case-insensitively but keep the original slice for numeric parsing.
            match = re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}\s*=\s*\[", line, re.IGNORECASE)
            if match is None:
                continue
            openBracketIndex = line.find("[", match.end() - 1)
            if openBracketIndex < 0:
                continue
            closeBracketIndex = line.find("]", openBracketIndex + 1)
            if closeBracketIndex < 0:
                self.parseErrorCount += 1
                return None
            arrayText = line[openBracketIndex + 1 : closeBracketIndex]
            arrayKey = key
            break

        if arrayText is None:
            # It is a Cap-tagged line, but not a capacitance array line. Do not fall through
            # to the generic row parser because the key/value metadata numbers are misleading.
            self.parseErrorCount += 1
            self.resetRowBuffer()
            return None

        pairs = extractNumberUnitPairs(arrayText)
        if len(pairs) != self.expectedCount:
            self.parseErrorCount += 1
            self.resetRowBuffer()
            return None

        values = self._convertPairsToPf(pairs)
        matrix = np.array(values, dtype=float).reshape(self.rows, self.cols)
        metadata: Dict[str, Any] = {
            "parser": "cf63_cap_pf",
            "tag": getLineTag(line),
            "arrayKey": arrayKey or "pf",
            "numericCount": len(pairs),
        }
        for key in ("s", "t", "pa", "q", "cm", "cv", "fm", "vm", "wm", "em"):
            value = extractKeyValue(line, key)
            if value is not None:
                metadata[key] = value

        frame = MatrixFrame(
            timestamp=time.time(),
            values=sanitizeMatrix(matrix),
            unit="pF",
            rawLine=line,
            source=source,
            metadata=metadata,
        )
        self.parsedFrameCount += 1
        self.resetRowBuffer()
        return frame

    def parseLine(self, line: str, source: str = "line") -> Optional[MatrixFrame]:
        cleanLine = stripAnsi(line).strip()
        if not cleanLine:
            return None

        cf63CapFrame = self._tryParseCf63CapFrame(cleanLine, source)
        if cf63CapFrame is not None:
            return cf63CapFrame

        if isKnownNonMatrixDataLine(cleanLine):
            # Do not let raw FDC_DEVICE_READ4/raw28/I2C/compact diagnostic lines become fake 8x8 matrices.
            self.resetRowBuffer()
            return None

        if detectFrequencyOnlyMatrixLine(cleanLine, self.expectedCount):
            self.frequencyOnlyMatrixCount += 1
            return None

        nestedFrame = self._tryParseNestedMatrix(cleanLine, source)
        if nestedFrame is not None:
            return nestedFrame

        pairs = extractNumberUnitPairs(cleanLine)
        if len(pairs) >= self.expectedCount and lineLooksLikeCompleteCapacitanceFrame(cleanLine, pairs, self.expectedCount):
            values = self._convertPairsToPf(pairs[-self.expectedCount :])
            matrix = np.array(values, dtype=float).reshape(self.rows, self.cols)
            frame = MatrixFrame(
                timestamp=time.time(),
                values=sanitizeMatrix(matrix),
                unit="pF",
                rawLine=line,
                source=source,
                metadata={"parser": "flat_values", "numericCount": len(pairs)},
            )
            self.parsedFrameCount += 1
            self.resetRowBuffer()
            return frame

        # Support eight row lines. This is intentionally stricter than len==cols only,
        # because debug logs may accidentally contain eight random numbers.
        if len(pairs) == self.cols and self._looksLikeMatrixRow(cleanLine):
            rowValues = self._convertPairsToPf(pairs)
            if self.rowBufferFirstTimestamp is None:
                self.rowBufferFirstTimestamp = time.time()
            self.rowBuffer.append(rowValues)
            if len(self.rowBuffer) == self.rows:
                matrix = np.array(self.rowBuffer, dtype=float).reshape(self.rows, self.cols)
                frame = MatrixFrame(
                    timestamp=self.rowBufferFirstTimestamp or time.time(),
                    values=sanitizeMatrix(matrix),
                    unit="pF",
                    rawLine="<assembled from row lines>",
                    source=source,
                    metadata={"parser": "row_buffer"},
                )
                self.parsedFrameCount += 1
                self.resetRowBuffer()
                return frame
            return None

        # If a line is unrelated, clear an old row buffer after it becomes suspiciously stale.
        if self.rowBuffer and self.rowBufferFirstTimestamp is not None:
            if time.time() - self.rowBufferFirstTimestamp > 2.0:
                self.resetRowBuffer()
        return None

    def _tryParseNestedMatrix(self, line: str, source: str) -> Optional[MatrixFrame]:
        first = line.find("[[")
        last = line.rfind("]]")
        if first < 0 or last < first:
            return None
        candidate = line[first : last + 2]
        candidate = candidate.replace("nan", "None").replace("NaN", "None")
        candidate = candidate.replace("inf", "None").replace("Inf", "None")
        try:
            parsed = ast.literal_eval(candidate)
        except Exception:
            self.parseErrorCount += 1
            return None

        try:
            matrixList: List[List[float]] = []
            if not isinstance(parsed, (list, tuple)) or len(parsed) != self.rows:
                return None
            for row in parsed:
                if not isinstance(row, (list, tuple)) or len(row) != self.cols:
                    return None
                matrixRow: List[float] = []
                for cellValue in row:
                    if cellValue is None:
                        matrixRow.append(float("nan"))
                    else:
                        matrixRow.append(float(cellValue))
                matrixList.append(matrixRow)
            matrix = np.array(matrixList, dtype=float)
        except Exception:
            self.parseErrorCount += 1
            return None

        frame = MatrixFrame(
            timestamp=time.time(),
            values=sanitizeMatrix(matrix),
            unit=self.assumeUnit,
            rawLine=line,
            source=source,
            metadata={"parser": "nested_matrix"},
        )
        self.parsedFrameCount += 1
        self.resetRowBuffer()
        return frame

    def _convertPairsToPf(self, pairs: Sequence[Tuple[float, Optional[str]]]) -> List[float]:
        values: List[float] = []
        for value, unit in pairs:
            if isInvalidNumericValue(value):
                values.append(float("nan"))
            elif isFrequencyUnit(unit):
                values.append(float("nan"))
            else:
                values.append(capacitanceToPf(value, unit, assumeUnit=self.assumeUnit))
        return values

    def _looksLikeMatrixRow(self, line: str) -> bool:
        if isKnownNonMatrixDataLine(line):
            return False
        stripped = line.strip()
        lowerLine = stripped.lower()
        if "[" in stripped and "]" in stripped:
            return True
        if lowerLine.startswith(("row", "r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8")):
            return True
        if re.match(r"^s\s*[1-8]\s*[:=,]", lowerLine):
            return True
        if stripped.count(",") >= self.cols - 1 and "=" not in stripped:
            return True
        return False


class BaselineEstimator:
    def __init__(self, rows: int, cols: int, minSamples: int) -> None:
        self.rows = rows
        self.cols = cols
        self.minSamples = minSamples
        self.reset()

    def reset(self) -> None:
        self.sumMatrix = np.zeros((self.rows, self.cols), dtype=float)
        self.sumSqMatrix = np.zeros((self.rows, self.cols), dtype=float)
        self.countMatrix = np.zeros((self.rows, self.cols), dtype=np.int64)
        self.frameCount = 0
        self.validFrameCount = 0

    def addFrame(self, frame: MatrixFrame) -> None:
        matrix = frame.values
        validMask = np.isfinite(matrix)
        self.sumMatrix[validMask] += matrix[validMask]
        self.sumSqMatrix[validMask] += matrix[validMask] ** 2
        self.countMatrix[validMask] += 1
        self.frameCount += 1
        if np.any(validMask):
            self.validFrameCount += 1

    def finalise(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        meanMatrix = np.full((self.rows, self.cols), np.nan, dtype=float)
        stdMatrix = np.full((self.rows, self.cols), np.nan, dtype=float)
        validCountMask = self.countMatrix > 0
        meanMatrix[validCountMask] = self.sumMatrix[validCountMask] / self.countMatrix[validCountMask]
        varianceMatrix = np.full((self.rows, self.cols), np.nan, dtype=float)
        varianceMatrix[validCountMask] = (
            self.sumSqMatrix[validCountMask] / self.countMatrix[validCountMask]
            - meanMatrix[validCountMask] ** 2
        )
        varianceMatrix = np.maximum(varianceMatrix, 0.0)
        stdMatrix[validCountMask] = np.sqrt(varianceMatrix[validCountMask])
        invalidBaselineMask = self.countMatrix < self.minSamples
        meanMatrix[invalidBaselineMask] = np.nan
        stdMatrix[invalidBaselineMask] = np.nan
        return meanMatrix, stdMatrix, self.countMatrix.copy(), invalidBaselineMask


class PercentChangeProcessor:
    def __init__(self, baselineMeanMatrix: np.ndarray, invalidBaselineMask: np.ndarray) -> None:
        self.baselineMeanMatrix = baselineMeanMatrix.astype(float, copy=True)
        self.invalidBaselineMask = invalidBaselineMask.copy()
        # pF epsilon. This is tiny relative to normal pF-level capacitance and only avoids divide-by-zero.
        self.epsilonPf = 1e-6

    def process(self, currentMatrix: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        currentMatrix = currentMatrix.astype(float, copy=True)
        invalidCurrentMask = ~np.isfinite(currentMatrix)
        invalidMask = invalidCurrentMask | self.invalidBaselineMask | ~np.isfinite(self.baselineMeanMatrix)
        safeBaseline = np.maximum(np.abs(self.baselineMeanMatrix), self.epsilonPf)
        deltaMatrix = currentMatrix - self.baselineMeanMatrix
        percentMatrix = deltaMatrix / safeBaseline * 100.0
        deltaMatrix[invalidMask] = np.nan
        percentMatrix[invalidMask] = np.nan
        return deltaMatrix, percentMatrix, invalidMask


class CsvFrameWriter:
    def __init__(self, path: Optional[str], rows: int, cols: int) -> None:
        self.path = path
        self.rows = rows
        self.cols = cols
        self.fileHandle: Optional[Any] = None
        self.writer: Optional[csv.writer] = None
        self.writeCount = 0
        if path:
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            self.fileHandle = open(path, "w", newline="", encoding="utf-8")
            self.writer = csv.writer(self.fileHandle)
            self.writer.writerow(self._buildHeader())

    def _buildHeader(self) -> List[str]:
        header = ["timestampIso", "timestampUnix", "frameIndex", "fps"]
        for prefix in ("C", "dC", "pct"):
            for rowIndex in range(self.rows):
                for colIndex in range(self.cols):
                    header.append(f"{prefix}_S{rowIndex + 1}_D{colIndex + 1}")
        return header

    def writeFrame(
        self,
        frame: MatrixFrame,
        frameIndex: int,
        fps: float,
        deltaMatrix: np.ndarray,
        percentMatrix: np.ndarray,
    ) -> None:
        if self.writer is None:
            return
        timestampIso = datetime.fromtimestamp(frame.timestamp).astimezone().isoformat(timespec="milliseconds")
        row: List[Any] = [timestampIso, f"{frame.timestamp:.6f}", frameIndex, f"{fps:.3f}"]
        for matrix in (frame.values, deltaMatrix, percentMatrix):
            flatValues = matrix.reshape(-1)
            for value in flatValues:
                if np.isfinite(value):
                    row.append(f"{float(value):.9g}")
                else:
                    row.append("")
        self.writer.writerow(row)
        self.writeCount += 1
        if self.fileHandle is not None and self.writeCount % 20 == 0:
            self.fileHandle.flush()

    def close(self) -> None:
        if self.fileHandle is not None:
            try:
                self.fileHandle.flush()
                self.fileHandle.close()
            except Exception:
                pass
        self.fileHandle = None
        self.writer = None


class BaseFrameReader:
    def __init__(self, outputQueue: "queue.Queue[QueueItem]", parser: FallbackMatrixParser) -> None:
        self.outputQueue = outputQueue
        self.parser = parser
        self.stopEvent = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopEvent.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=1.5)

    def putEvent(self, eventType: str, message: str, rawLine: Optional[str] = None, **metadata: Any) -> None:
        self.outputQueue.put(
            ReaderEvent(
                eventType=eventType,
                timestamp=time.time(),
                message=message,
                rawLine=rawLine,
                metadata=metadata,
            )
        )

    def handleLine(self, line: str, source: str) -> None:
        # Always forward raw input lines to the UI log panel. This is intentionally separate
        # from matrix parsing, so we can debug why the state machine is waiting.
        self.putEvent("raw_log", line, rawLine=line, source=source)

        if detectFullSweepDone(line):
            self.putEvent("full_sweep_done", "Detected full sweep completion marker", rawLine=line)

        if detectFrequencyOnlyMatrixLine(line, self.parser.expectedCount):
            self.putEvent(
                "frequency_matrix_detected",
                "Detected frequency-only matrix. Capacitance heatmap requires capacitance values, not Hz/MHz.",
                rawLine=line,
            )
            return

        frame = self.parser.parseLine(line, source=source)
        if frame is not None:
            self.outputQueue.put(frame)

    def run(self) -> None:  # pragma: no cover - abstract-like
        raise NotImplementedError


class SerialFrameReader(BaseFrameReader):
    def __init__(
        self,
        outputQueue: "queue.Queue[QueueItem]",
        parser: FallbackMatrixParser,
        port: str,
        baud: int,
        timeout: float,
        reconnectSerial: bool,
        reconnectDelaySeconds: float,
        idleReconnectSeconds: float,
    ) -> None:
        super().__init__(outputQueue, parser)
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.reconnectSerial = reconnectSerial
        self.reconnectDelaySeconds = max(reconnectDelaySeconds, 0.2)
        self.idleReconnectSeconds = max(idleReconnectSeconds, 0.0)
        self.serialHandle: Optional[Any] = None

    def _closeSerialHandle(self) -> None:
        try:
            if self.serialHandle is not None:
                self.serialHandle.close()
        except Exception:
            pass
        self.serialHandle = None

    def _openSerialHandle(self) -> bool:
        try:
            self.serialHandle = serial.Serial(self.port, self.baud, timeout=self.timeout)
            self.putEvent("connected", f"Serial connected: {self.port} @ {self.baud}")
            return True
        except Exception as exc:
            self.putEvent("reconnecting", f"Failed to open {self.port}: {exc}; retrying in {self.reconnectDelaySeconds:.1f}s")
            return False

    def run(self) -> None:
        if serial is None:
            self.putEvent(
                "error",
                "pyserial is not installed. Install it with: pip install pyserial",
            )
            return

        while not self.stopEvent.is_set():
            if not self._openSerialHandle():
                if not self.reconnectSerial:
                    self.putEvent("error", f"Failed to open serial port {self.port}")
                    return
                time.sleep(self.reconnectDelaySeconds)
                continue

            lastByteTime = time.time()
            reconnectReason = "Serial reader stopped"
            while not self.stopEvent.is_set() and self.serialHandle is not None:
                try:
                    rawBytes = self.serialHandle.readline()
                except Exception as exc:
                    reconnectReason = f"Serial read failed: {exc}"
                    self.putEvent("disconnected", reconnectReason)
                    break

                if not rawBytes:
                    if (
                        self.idleReconnectSeconds > 0
                        and time.time() - lastByteTime >= self.idleReconnectSeconds
                    ):
                        reconnectReason = f"No serial data for {self.idleReconnectSeconds:.1f}s; reopening port"
                        self.putEvent("disconnected", reconnectReason)
                        break
                    continue

                lastByteTime = time.time()
                try:
                    line = rawBytes.decode("utf-8", errors="replace").rstrip("\r\n")
                except Exception:
                    line = str(rawBytes)
                self.handleLine(line, source="serial")

            self._closeSerialHandle()
            if self.stopEvent.is_set() or not self.reconnectSerial:
                break
            self.putEvent("reconnecting", f"{reconnectReason}; reconnecting in {self.reconnectDelaySeconds:.1f}s")
            time.sleep(self.reconnectDelaySeconds)

        self._closeSerialHandle()
        self.putEvent("disconnected", "Serial reader stopped")


class LogReplayReader(BaseFrameReader):
    def __init__(
        self,
        outputQueue: "queue.Queue[QueueItem]",
        parser: FallbackMatrixParser,
        logPath: str,
        replaySpeed: float,
        lineDelay: float,
    ) -> None:
        super().__init__(outputQueue, parser)
        self.logPath = logPath
        self.replaySpeed = max(replaySpeed, 0.01)
        self.lineDelay = max(lineDelay, 0.0)

    def run(self) -> None:
        if not os.path.exists(self.logPath):
            self.putEvent("error", f"Replay log does not exist: {self.logPath}")
            return
        self.putEvent("connected", f"Replaying log: {self.logPath}")
        effectiveDelay = self.lineDelay / self.replaySpeed
        try:
            with open(self.logPath, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if self.stopEvent.is_set():
                        break
                    self.handleLine(line.rstrip("\r\n"), source="replay")
                    if effectiveDelay > 0:
                        time.sleep(effectiveDelay)
        except Exception as exc:
            self.putEvent("error", f"Failed while replaying log: {exc}")
            return
        self.putEvent("eof", "Replay log finished")


class DemoFrameReader(BaseFrameReader):
    def __init__(
        self,
        outputQueue: "queue.Queue[QueueItem]",
        parser: FallbackMatrixParser,
        rows: int,
        cols: int,
        frameRateHz: float = 20.0,
    ) -> None:
        super().__init__(outputQueue, parser)
        self.rows = rows
        self.cols = cols
        self.frameRateHz = frameRateHz
        rowAxis = np.arange(rows, dtype=float).reshape(rows, 1)
        colAxis = np.arange(cols, dtype=float).reshape(1, cols)
        self.baseMatrix = 20.0 + 0.25 * rowAxis + 0.15 * colAxis

    def run(self) -> None:
        startTime = time.time()
        self.putEvent("connected", "Demo source started")
        # Simulate embedded startup/full sweep. No matrix frames are emitted during this period.
        while not self.stopEvent.is_set() and time.time() - startTime < 2.0:
            time.sleep(0.05)
        if self.stopEvent.is_set():
            return
        self.putEvent("full_sweep_done", "Demo full sweep completed")

        framePeriod = 1.0 / max(self.frameRateHz, 1.0)
        while not self.stopEvent.is_set():
            currentTime = time.time()
            elapsed = currentTime - startTime
            noiseMatrix = np.random.normal(loc=0.0, scale=0.015, size=(self.rows, self.cols))
            percentSignal = np.zeros((self.rows, self.cols), dtype=float)

            # Start pressure-like signal after the default 10-second baseline would normally finish.
            signalRamp = min(max((elapsed - 13.0) / 8.0, 0.0), 1.0)
            if signalRamp > 0:
                percentSignal += self._gaussianSpot(centerRow=2.0, centerCol=3.0, amplitude=15.0 * signalRamp, sigma=0.9)
                percentSignal += self._gaussianSpot(centerRow=4.5, centerCol=4.5, amplitude=8.0 * signalRamp, sigma=1.1)
                percentSignal += 0.4 * math.sin(elapsed * 1.2)

            currentMatrix = self.baseMatrix * (1.0 + percentSignal / 100.0) + noiseMatrix

            # Occasionally simulate one invalid value to prove mask handling.
            if random.random() < 0.015:
                currentMatrix[random.randrange(self.rows), random.randrange(self.cols)] = -1.0

            self.outputQueue.put(
                MatrixFrame(
                    timestamp=currentTime,
                    values=sanitizeMatrix(currentMatrix),
                    unit="pF",
                    rawLine="<demo>",
                    source="demo",
                    metadata={"parser": "demo"},
                )
            )
            time.sleep(framePeriod)

    def _gaussianSpot(self, centerRow: float, centerCol: float, amplitude: float, sigma: float) -> np.ndarray:
        rowAxis = np.arange(self.rows, dtype=float).reshape(self.rows, 1)
        colAxis = np.arange(self.cols, dtype=float).reshape(1, self.cols)
        distanceSq = (rowAxis - centerRow) ** 2 + (colAxis - centerCol) ** 2
        return amplitude * np.exp(-distanceSq / (2.0 * sigma * sigma))


class CapPercentHeatmapApp:
    def __init__(self, config: HeatmapConfig) -> None:
        if plt is None or animation is None:
            raise RuntimeError(f"matplotlib import failed: {_MATPLOTLIB_IMPORT_ERROR}")
        self.config = config
        self.queue: "queue.Queue[QueueItem]" = queue.Queue(maxsize=10000)
        self.parser = FallbackMatrixParser(config.rows, config.cols, assumeUnit=config.assumeUnit)
        self.reader = self._createReader()
        self.baselineEstimator = BaselineEstimator(config.rows, config.cols, config.minBaselineSamples)
        self.baselineMeanMatrix: Optional[np.ndarray] = None
        self.baselineStdMatrix: Optional[np.ndarray] = None
        self.baselineCountMatrix: Optional[np.ndarray] = None
        self.invalidBaselineMask: Optional[np.ndarray] = None
        self.processor: Optional[PercentChangeProcessor] = None
        self.csvWriter = CsvFrameWriter(config.saveCsvPath, config.rows, config.cols)

        self.state = "CONNECTING"
        self.statusMessage = "Starting..."
        self.startTime = time.time()
        self.waitingStartTime: Optional[float] = None
        self.baselineStartTime: Optional[float] = None
        self.liveFrameIndex = 0
        self.allFrameCount = 0
        self.waitingStableFrameCount = 0
        self.waitingCompleteFrameCount = 0
        self.waitingSweepLikeFrameCount = 0
        self.fullSweepMarkerSeen = False
        self.fullSweepMarkerTime: Optional[float] = None
        self.postSweepStableFrameCount = 0
        self.lastWaitingRejectReason = "none"
        self.parseErrorCountAtLastStatus = 0
        self.frequencyWarningShown = False
        self.latestRawMatrix = np.full((config.rows, config.cols), np.nan, dtype=float)
        self.latestDeltaMatrix = np.full((config.rows, config.cols), np.nan, dtype=float)
        self.latestPercentMatrix = np.full((config.rows, config.cols), np.nan, dtype=float)
        self.latestInvalidMask = np.ones((config.rows, config.cols), dtype=bool)
        self.liveFrameTimestamps: deque[float] = deque(maxlen=60)
        self.lastStatusPrintTime = 0.0

        self.fig: Optional[Any] = None
        self.ax: Optional[Any] = None
        self.image: Optional[Any] = None
        self.colorbar: Optional[Any] = None
        self.cellTexts: List[List[Any]] = []
        self.statusTextArtist: Optional[Any] = None
        self.logAx: Optional[Any] = None
        self.logTextArtist: Optional[Any] = None
        self.rawLogLines: deque[str] = deque(maxlen=max(10, config.logMaxLines))
        # logScrollOffset is measured in lines from the newest end. 0 means auto-follow newest lines.
        self.logScrollOffset = 0
        self.rawLogFileHandle: Optional[Any] = None
        if config.saveRawLogPath:
            rawLogDirectory = os.path.dirname(os.path.abspath(config.saveRawLogPath))
            if rawLogDirectory:
                os.makedirs(rawLogDirectory, exist_ok=True)
            self.rawLogFileHandle = open(config.saveRawLogPath, "a", encoding="utf-8", errors="replace")
        self.animationHandle: Optional[Any] = None
        self.colourMapObject: Optional[Any] = None

    def _createReader(self) -> BaseFrameReader:
        if self.config.demo:
            return DemoFrameReader(self.queue, self.parser, self.config.rows, self.config.cols)
        if self.config.replayLog:
            return LogReplayReader(
                self.queue,
                self.parser,
                self.config.replayLog,
                self.config.replaySpeed,
                self.config.replayLineDelay,
            )
        if not self.config.port:
            availablePorts = listSerialPorts()
            portHint = ", ".join(availablePorts) if availablePorts else "no ports detected"
            raise ValueError(
                "No --port provided. Use --demo, --replay-log, or provide --port. "
                f"Available serial ports: {portHint}"
            )
        return SerialFrameReader(
            self.queue,
            self.parser,
            self.config.port,
            self.config.baud,
            self.config.serialTimeout,
            self.config.reconnectSerial,
            self.config.reconnectDelaySeconds,
            self.config.serialIdleReconnectSeconds,
        )

    def run(self) -> None:
        self._setupPlot()
        self.reader.start()
        self.state = "WAITING_FULL_SWEEP"
        self.waitingStartTime = time.time()
        if self.config.noWaitFullSweep:
            self._enterBaseline("--no-wait-full-sweep was provided")
        else:
            self.statusMessage = "Waiting for full sweep to finish..."
        self.animationHandle = animation.FuncAnimation(
            self.fig,
            self._onAnimationUpdate,
            interval=self.config.updateIntervalMs,
            blit=False,
            cache_frame_data=False,
        )
        try:
            plt.show()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        try:
            self.reader.stop()
        finally:
            self.csvWriter.close()
            if self.rawLogFileHandle is not None:
                try:
                    self.rawLogFileHandle.flush()
                    self.rawLogFileHandle.close()
                except Exception:
                    pass
                self.rawLogFileHandle = None

    def _setupPlot(self) -> None:
        if self.config.showLogPanel:
            self.fig = plt.figure(figsize=(13.5, 7.2))
            gridSpec = self.fig.add_gridspec(1, 2, width_ratios=[3.2, 1.45], wspace=0.18)
            self.ax = self.fig.add_subplot(gridSpec[0, 0])
            self.logAx = self.fig.add_subplot(gridSpec[0, 1])
            self.logAx.set_title("Raw / state log  (scrollable)")
            self.logAx.axis("off")
            self.logTextArtist = self.logAx.text(
                0.0,
                1.0,
                "Waiting for serial log...",
                transform=self.logAx.transAxes,
                ha="left",
                va="top",
                fontsize=8,
                family="monospace",
                wrap=False,
            )
        else:
            self.fig, self.ax = plt.subplots(figsize=(9, 7))
        self.fig.canvas.manager.set_window_title("SensorArray capacitance ΔC/C0 heatmap")
        self.fig.canvas.mpl_connect("key_press_event", self._onKeyPress)
        self.fig.canvas.mpl_connect("scroll_event", self._onScroll)
        self.fig.canvas.mpl_connect("close_event", lambda _event: self.shutdown())

        try:
            self.colourMapObject = plt.get_cmap(self.config.cmap).copy()
        except Exception:
            print(f"[WARN] Unknown cmap '{self.config.cmap}', falling back to coolwarm", file=sys.stderr)
            self.colourMapObject = plt.get_cmap("coolwarm").copy()
        try:
            self.colourMapObject.set_bad("lightgray")
        except Exception:
            pass

        initialMatrix = np.ma.masked_invalid(self.latestPercentMatrix)
        self.image = self.ax.imshow(
            initialMatrix,
            interpolation="nearest",
            cmap=self.colourMapObject,
            vmin=self.config.vmin if self.config.vmin is not None else -self.config.minAbsAutoScale,
            vmax=self.config.vmax if self.config.vmax is not None else self.config.minAbsAutoScale,
            origin="upper",
        )
        self.colorbar = self.fig.colorbar(self.image, ax=self.ax)
        self.colorbar.set_label("ΔC / C0 (%)")

        self.ax.set_xlabel("Column / D channel")
        self.ax.set_ylabel("Row / S channel")
        self.ax.set_xticks(np.arange(self.config.cols))
        self.ax.set_yticks(np.arange(self.config.rows))
        self.ax.set_xticklabels([f"D{i + 1}" for i in range(self.config.cols)])
        self.ax.set_yticklabels([f"S{i + 1}" for i in range(self.config.rows)])
        self.ax.set_xticks(np.arange(-0.5, self.config.cols, 1), minor=True)
        self.ax.set_yticks(np.arange(-0.5, self.config.rows, 1), minor=True)
        self.ax.grid(which="minor", linewidth=0.5)
        self.ax.tick_params(which="minor", bottom=False, left=False)

        self.cellTexts = []
        for rowIndex in range(self.config.rows):
            textRow = []
            for colIndex in range(self.config.cols):
                text = self.ax.text(
                    colIndex,
                    rowIndex,
                    "NA",
                    ha="center",
                    va="center",
                    fontsize=8,
                )
                textRow.append(text)
            self.cellTexts.append(textRow)

        self.statusTextArtist = self.ax.text(
            0.0,
            -0.13,
            "Starting...",
            transform=self.ax.transAxes,
            ha="left",
            va="top",
            fontsize=9,
        )
        self.ax.set_title("Capacitance Relative Change Heatmap: ΔC / C0 (%)")
        self.fig.tight_layout()

    def _onKeyPress(self, event: Any) -> None:
        if event.key in ("q", "escape"):
            plt.close(self.fig)
        elif event.key == "r":
            self._enterBaseline("Manual re-baseline requested with keyboard key 'r'")
        elif event.key == "s":
            self._saveBaselineIfAvailable(forcePath=None)
        elif event.key in ("pageup", "up", "["):
            self._scrollLog(+self.config.logPanelLines)
        elif event.key in ("pagedown", "down", "]"):
            self._scrollLog(-self.config.logPanelLines)
        elif event.key == "home":
            self.logScrollOffset = max(0, len(self.rawLogLines) - self.config.logPanelLines)
            self._refreshLogPanel()
        elif event.key == "end":
            self.logScrollOffset = 0
            self._refreshLogPanel()

    def _onScroll(self, event: Any) -> None:
        if not self.config.showLogPanel:
            return
        if self.logAx is not None and event.inaxes is not self.logAx:
            return
        step = max(1, int(self.config.logPanelLines / 3))
        if getattr(event, "button", None) == "up":
            self._scrollLog(+step)
        elif getattr(event, "button", None) == "down":
            self._scrollLog(-step)

    def _scrollLog(self, deltaLines: int) -> None:
        if not self.config.showLogPanel:
            return
        maxOffset = max(0, len(self.rawLogLines) - self.config.logPanelLines)
        self.logScrollOffset = int(min(max(self.logScrollOffset + deltaLines, 0), maxOffset))
        self._refreshLogPanel()

    def _onAnimationUpdate(self, _frameIndex: int) -> List[Any]:
        self._drainQueue()
        self._updateImageAndText()
        self._printPeriodicStatus()
        return []

    def _drainQueue(self) -> None:
        drained = 0
        while drained < self.config.maxQueueDrainPerUpdate:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            drained += 1
            if isinstance(item, ReaderEvent):
                self._handleEvent(item)
            elif isinstance(item, MatrixFrame):
                self._handleFrame(item)

    def _handleEvent(self, event: ReaderEvent) -> None:
        if event.eventType == "raw_log":
            self._appendLogPanel(event.message, prefix="RAW")
            if self.config.printRawLog:
                print(f"[RAW] {event.message}")
            return
        if event.eventType == "connected":
            self.statusMessage = event.message
            self._appendLogPanel(event.message, prefix="INFO")
        elif event.eventType == "disconnected":
            self.statusMessage = event.message
            self._appendLogPanel(event.message, prefix="CONN")
        elif event.eventType == "reconnecting":
            self.statusMessage = event.message
            self._appendLogPanel(event.message, prefix="CONN")
        elif event.eventType == "eof":
            self.statusMessage = event.message
        elif event.eventType == "error":
            self.state = "ERROR"
            self.statusMessage = event.message
            self._appendLogPanel(event.message, prefix="ERROR")
            print(f"[ERROR] {event.message}", file=sys.stderr)
        elif event.eventType == "full_sweep_done":
            # Do not start baseline directly from a marker. Some firmware revisions print
            # a sweep/ready marker before low-level FDC_DEVICE_READ4/raw28 sweep traffic
            # fully stops. Mark it, then require stable complete capacitance frames.
            self._appendLogPanel(event.message, prefix="MARK")
            self.fullSweepMarkerSeen = True
            self.fullSweepMarkerTime = time.time()
            self.postSweepStableFrameCount = 0
            if self.state == "WAITING_FULL_SWEEP":
                self.statusMessage = (
                    "Full sweep marker seen; waiting for stable complete capacitance frames "
                    f"({self.config.postSweepStableFrames} frames, "
                    f">={self.config.postSweepMinDelaySeconds:.1f}s delay)"
                )
            else:
                self.statusMessage = event.message
        elif event.eventType == "frequency_matrix_detected":
            self.frequencyWarningShown = True
            self.statusMessage = event.message
            self._appendLogPanel(event.message, prefix="WARN")
            print(f"[WARN] {event.message}", file=sys.stderr)
        else:
            self.statusMessage = event.message


    def _appendLogPanel(self, message: str, prefix: str = "LOG") -> None:
        """Append one compact line to the right-side debug log panel and optional raw log file."""
        if message is None:
            return
        timestampText = datetime.now().strftime("%H:%M:%S")
        rawMessage = str(message).replace("\r", " ").replace("\n", " ")
        compactMessage = shortenForLog(rawMessage, 220)
        formattedLine = f"{timestampText} [{prefix}] {compactMessage}"
        self.rawLogLines.append(formattedLine)
        if self.rawLogFileHandle is not None:
            try:
                self.rawLogFileHandle.write(f"{datetime.now().isoformat(timespec='milliseconds')} [{prefix}] {rawMessage}\n")
                if prefix in ("STATE", "ERROR", "MARK", "CONN"):
                    self.rawLogFileHandle.flush()
            except Exception:
                pass

    def _refreshLogPanel(self) -> None:
        if not self.config.showLogPanel or self.logTextArtist is None:
            return
        allLines = list(self.rawLogLines)
        totalLines = len(allLines)
        if totalLines == 0:
            self.logTextArtist.set_text("No log lines yet")
            return
        maxOffset = max(0, totalLines - self.config.logPanelLines)
        self.logScrollOffset = int(min(max(self.logScrollOffset, 0), maxOffset))
        endIndex = totalLines - self.logScrollOffset
        startIndex = max(0, endIndex - self.config.logPanelLines)
        visibleLines = allLines[startIndex:endIndex]
        header = (
            f"Log {startIndex + 1}-{endIndex}/{totalLines} | "
            f"stored={self.config.logMaxLines} | "
            f"scroll: wheel/PageUp/PageDown/Home/End | "
            f"offset={self.logScrollOffset}"
        )
        self.logTextArtist.set_text(header + "\n" + "\n".join(visibleLines))

    def _handleFrame(self, frame: MatrixFrame) -> None:
        self.allFrameCount += 1
        self.latestRawMatrix = frame.values

        if self.state == "WAITING_FULL_SWEEP":
            self.waitingCompleteFrameCount += 1
            waitingElapsed = time.time() - (self.waitingStartTime or self.startTime)
            validCells = int(np.count_nonzero(np.isfinite(frame.values)))
            totalCells = self.config.rows * self.config.cols
            parserName = frame.metadata.get("parser", "unknown")
            rawPreview = shortenForLog(frame.rawLine or "", 140)

            if self.config.noWaitFullSweep:
                self._enterBaseline("No-wait mode")
                self.baselineEstimator.addFrame(frame)
                return

            # V4 behaviour: the first parsed returned heatmap array is treated as the
            # full-sweep completion marker. Raw FDC_DEVICE_READ4/raw28/I2C debug lines
            # are filtered by the parser before this point, so a MatrixFrame here is the
            # first user-facing array worth using for baseline.
            if self.config.firstMatrixStartsBaseline and validCells >= self.config.minValidCellsToStartBaseline:
                self.fullSweepMarkerSeen = True
                self.fullSweepMarkerTime = time.time()
                self.waitingStableFrameCount += 1
                self.lastWaitingRejectReason = "first_returned_matrix_array_started_baseline"
                self._appendLogPanel(
                    f"first returned matrix accepted as full-sweep completion; "
                    f"parser={parserName}; valid={validCells}/{totalCells}; line={rawPreview}",
                    prefix="STATE",
                )
                self._enterBaseline(
                    f"First returned capacitance matrix after startup "
                    f"(parser={parserName}, valid={validCells}/{totalCells})"
                )
                self.baselineEstimator.addFrame(frame)
                return

            if self.config.firstMatrixStartsBaseline and validCells < self.config.minValidCellsToStartBaseline:
                self.waitingSweepLikeFrameCount += 1
                self.lastWaitingRejectReason = (
                    f"insufficient_valid_cells_for_first_matrix_baseline: "
                    f"{validCells}/{self.config.minValidCellsToStartBaseline}"
                )
                self.statusMessage = (
                    "Waiting for a full cf63 Cap frame before baseline... "
                    f"valid={validCells}/{totalCells}, "
                    f"required={self.config.minValidCellsToStartBaseline}, "
                    f"parser={parserName}"
                )
                if self.waitingSweepLikeFrameCount <= 20 or self.waitingSweepLikeFrameCount % 20 == 0:
                    self._appendLogPanel(
                        f"frame rejected before baseline; reason={self.lastWaitingRejectReason}; "
                        f"parser={parserName}; line={rawPreview}",
                        prefix="STATE",
                    )
                return

            # Legacy conservative path for users who explicitly disable first-matrix behaviour.
            markerElapsed = (time.time() - self.fullSweepMarkerTime) if self.fullSweepMarkerTime else None
            sweepLike = isSweepOrCalibrationLine(frame.rawLine)
            errorLike = isLikelyErrorLine(frame.rawLine)
            lowLevelLike = isKnownNonMatrixDataLine(frame.rawLine)

            if sweepLike or errorLike or lowLevelLike:
                self.waitingSweepLikeFrameCount += 1
                rejectParts = []
                if sweepLike:
                    rejectParts.append("sweep/config word in line")
                if errorLike:
                    rejectParts.append("error-like word in line")
                if lowLevelLike:
                    rejectParts.append("low-level raw/debug line")
                self.lastWaitingRejectReason = ", ".join(rejectParts) if rejectParts else "unknown"
                self.statusMessage = (
                    "Waiting for post-sweep stable capacitance frame... "
                    f"elapsed={waitingElapsed:.1f}s, markerSeen={self.fullSweepMarkerSeen}, "
                    f"stable={self.waitingStableFrameCount}, rejected={self.waitingSweepLikeFrameCount}, "
                    f"lastReason={self.lastWaitingRejectReason}"
                )
                if self.waitingSweepLikeFrameCount <= 20 or self.waitingSweepLikeFrameCount % 20 == 0:
                    self._appendLogPanel(
                        f"frame rejected before baseline; reason={self.lastWaitingRejectReason}; "
                        f"parser={parserName}; valid={validCells}/{totalCells}; line={rawPreview}",
                        prefix="STATE",
                    )
                return

            self.waitingStableFrameCount += 1
            self.lastWaitingRejectReason = "accepted_as_stable_capacitance_frame"

            if self.fullSweepMarkerSeen:
                self.postSweepStableFrameCount += 1
                delayOk = markerElapsed is not None and markerElapsed >= self.config.postSweepMinDelaySeconds
                framesOk = self.postSweepStableFrameCount >= self.config.postSweepStableFrames
                self.statusMessage = (
                    "Full sweep marker seen; waiting for stable live frames... "
                    f"markerElapsed={(markerElapsed or 0):.1f}s/{self.config.postSweepMinDelaySeconds:.1f}s, "
                    f"postFrames={self.postSweepStableFrameCount}/{self.config.postSweepStableFrames}, "
                    f"allParsedFrames={self.waitingCompleteFrameCount}"
                )
                self._appendLogPanel(
                    f"post-marker stable frame {self.postSweepStableFrameCount}/{self.config.postSweepStableFrames}; "
                    f"delayOk={delayOk}; parser={parserName}; valid={validCells}/{totalCells}",
                    prefix="STATE",
                )
                if delayOk and framesOk:
                    self._enterBaseline(
                        f"Post-sweep stable: marker + {self.postSweepStableFrameCount} complete frames"
                    )
                return

            self.statusMessage = (
                "Waiting for full sweep marker or stable live frames... "
                f"elapsed={waitingElapsed:.1f}s, "
                f"stableFrames={self.waitingStableFrameCount}/{self.config.stableFramesBeforeBaseline}, "
                f"allParsedFrames={self.waitingCompleteFrameCount}"
            )
            self._appendLogPanel(
                f"stable capacitance frame {self.waitingStableFrameCount}/{self.config.stableFramesBeforeBaseline}; "
                f"parser={parserName}; valid={validCells}/{totalCells}",
                prefix="STATE",
            )
            if self.waitingStableFrameCount >= self.config.stableFramesBeforeBaseline:
                self._enterBaseline(
                    f"Fallback: received {self.waitingStableFrameCount} complete capacitance frames"
                )
                return

            if (
                self.state == "WAITING_FULL_SWEEP"
                and self.config.fullSweepMaxWaitSeconds > 0
                and waitingElapsed >= self.config.fullSweepMaxWaitSeconds
                and self.waitingStableFrameCount >= 1
            ):
                self._enterBaseline(
                    "Timed fallback: parsed complete capacitance frames for "
                    f"{waitingElapsed:.1f}s without a full-sweep marker "
                    f"(stableFrames={self.waitingStableFrameCount}, "
                    f"ignoredRawFrames={self.waitingSweepLikeFrameCount})"
                )
            return

        if self.state == "BASELINING":
            if self.baselineStartTime is None:
                self.baselineStartTime = time.time()
            self.baselineEstimator.addFrame(frame)
            elapsed = time.time() - self.baselineStartTime
            self.statusMessage = (
                f"Collecting baseline: {elapsed:.1f}/{self.config.baselineSeconds:.1f}s, "
                f"frames={self.baselineEstimator.frameCount}"
            )
            if elapsed >= self.config.baselineSeconds:
                self._finishBaseline()
            return

        if self.state == "LIVE":
            if self.processor is None:
                return
            deltaMatrix, percentMatrix, invalidMask = self.processor.process(frame.values)
            self.latestDeltaMatrix = deltaMatrix
            self.latestPercentMatrix = percentMatrix
            self.latestInvalidMask = invalidMask
            self.liveFrameIndex += 1
            self.liveFrameTimestamps.append(frame.timestamp)
            fps = self._calculateFps()
            validCount = int(np.count_nonzero(np.isfinite(percentMatrix)))
            self.statusMessage = (
                f"LIVE | frame={self.liveFrameIndex} | fps={fps:.2f} | "
                f"validCells={validCount}/{self.config.rows * self.config.cols}"
            )
            self.csvWriter.writeFrame(frame, self.liveFrameIndex, fps, deltaMatrix, percentMatrix)

    def _enterBaseline(self, reason: str) -> None:
        self.state = "BASELINING"
        self.baselineStartTime = time.time()
        self.baselineEstimator.reset()
        self.processor = None
        self.latestDeltaMatrix[:] = np.nan
        self.latestPercentMatrix[:] = np.nan
        self.latestInvalidMask[:] = True
        self.statusMessage = f"Baseline started: {reason}"
        self._appendLogPanel(self.statusMessage, prefix="STATE")
        print(f"[INFO] {self.statusMessage}")

    def _finishBaseline(self) -> None:
        meanMatrix, stdMatrix, countMatrix, invalidMask = self.baselineEstimator.finalise()
        self.baselineMeanMatrix = meanMatrix
        self.baselineStdMatrix = stdMatrix
        self.baselineCountMatrix = countMatrix
        self.invalidBaselineMask = invalidMask
        self.processor = PercentChangeProcessor(meanMatrix, invalidMask)
        invalidCount = int(np.count_nonzero(invalidMask))
        self.state = "LIVE"
        self.statusMessage = (
            f"Baseline complete. invalidBaselineCells={invalidCount}/{self.config.rows * self.config.cols}. "
            "Live percentage heatmap started."
        )
        self._appendLogPanel(self.statusMessage, prefix="STATE")
        print(f"[INFO] {self.statusMessage}")
        self._saveBaselineIfAvailable(forcePath=self.config.saveBaselinePath)

    def _saveBaselineIfAvailable(self, forcePath: Optional[str]) -> None:
        path = forcePath or self.config.saveBaselinePath
        if not path:
            return
        if self.baselineMeanMatrix is None or self.baselineStdMatrix is None or self.baselineCountMatrix is None:
            print("[WARN] Baseline is not available yet; cannot save baseline JSON.", file=sys.stderr)
            return
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = {
            "createdAt": nowIsoString(),
            "commit": getGitCommitOrUnknown(),
            "port": self.config.port,
            "baud": self.config.baud,
            "baselineSeconds": self.config.baselineSeconds,
            "unit": "pF",
            "rows": self.config.rows,
            "cols": self.config.cols,
            "baselineMean": self._matrixToJsonList(self.baselineMeanMatrix),
            "baselineStd": self._matrixToJsonList(self.baselineStdMatrix),
            "baselineCount": self.baselineCountMatrix.astype(int).tolist(),
            "invalidBaselineMask": np.asarray(self.invalidBaselineMask, dtype=bool).tolist()
            if self.invalidBaselineMask is not None
            else None,
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        print(f"[INFO] Baseline saved to {path}")

    def _matrixToJsonList(self, matrix: np.ndarray) -> List[List[Optional[float]]]:
        output: List[List[Optional[float]]] = []
        for row in matrix:
            outputRow: List[Optional[float]] = []
            for value in row:
                if np.isfinite(value):
                    outputRow.append(float(value))
                else:
                    outputRow.append(None)
            output.append(outputRow)
        return output

    def _calculateFps(self) -> float:
        if len(self.liveFrameTimestamps) < 2:
            return 0.0
        elapsed = self.liveFrameTimestamps[-1] - self.liveFrameTimestamps[0]
        if elapsed <= 0:
            return 0.0
        return (len(self.liveFrameTimestamps) - 1) / elapsed

    def _updateImageAndText(self) -> None:
        if self.image is None or self.ax is None:
            return
        matrixToDisplay = self.latestPercentMatrix.copy()
        maskedMatrix = np.ma.masked_invalid(matrixToDisplay)
        self.image.set_data(maskedMatrix)

        if self.config.vmin is not None and self.config.vmax is not None:
            self.image.set_clim(self.config.vmin, self.config.vmax)
        else:
            validValues = matrixToDisplay[np.isfinite(matrixToDisplay)]
            if validValues.size > 0:
                robustAbs = float(np.nanpercentile(np.abs(validValues), self.config.robustPercentile))
                robustAbs = max(robustAbs, self.config.minAbsAutoScale)
                self.image.set_clim(-robustAbs, robustAbs)

        for rowIndex in range(self.config.rows):
            for colIndex in range(self.config.cols):
                value = matrixToDisplay[rowIndex, colIndex]
                textObject = self.cellTexts[rowIndex][colIndex]
                if np.isfinite(value):
                    textObject.set_text(f"{value:+.{self.config.textDecimals}f}%")
                else:
                    textObject.set_text("NA")

        if self.state == "BASELINING" and self.baselineStartTime is not None:
            elapsed = time.time() - self.baselineStartTime
            remaining = max(self.config.baselineSeconds - elapsed, 0.0)
            title = f"Collecting baseline: {remaining:.1f}s remaining"
        elif self.state == "WAITING_FULL_SWEEP":
            title = "Waiting for full sweep to finish..."
        elif self.state == "LIVE":
            title = "Capacitance Relative Change Heatmap: ΔC / C0 (%)"
        elif self.state == "ERROR":
            title = "ERROR - check terminal output"
        else:
            title = self.state
        self.ax.set_title(title)

        if self.statusTextArtist is not None:
            queueSize = self.queue.qsize()
            waitElapsed = 0.0
            if self.waitingStartTime is not None:
                waitElapsed = time.time() - self.waitingStartTime
            parserStatus = (
                f"parserFrames={self.parser.parsedFrameCount}, "
                f"parseErrors={self.parser.parseErrorCount}, "
                f"freqOnly={self.parser.frequencyOnlyMatrixCount}, queue={queueSize}, "
                f"waitElapsed={waitElapsed:.1f}s, waitAll={self.waitingCompleteFrameCount}, "
                f"waitStable={self.waitingStableFrameCount}, waitSweepLike={self.waitingSweepLikeFrameCount}"
            )
            self.statusTextArtist.set_text(
                f"{self.statusMessage}\n"
                f"state={self.state}, totalFrames={self.allFrameCount}, {parserStatus}\n"
                "Keys: r = re-baseline, s = save baseline JSON, q/Esc = quit, "
                "mouse wheel/PageUp/PageDown over log = scroll log"
            )

        self._refreshLogPanel()

        if self.fig is not None:
            self.fig.canvas.draw_idle()

    def _printPeriodicStatus(self) -> None:
        currentTime = time.time()
        if currentTime - self.lastStatusPrintTime < 5.0:
            return
        self.lastStatusPrintTime = currentTime
        fps = self._calculateFps()
        waitElapsed = 0.0
        if self.waitingStartTime is not None:
            waitElapsed = currentTime - self.waitingStartTime
        print(
            f"[STATUS] state={self.state}, frames={self.allFrameCount}, live={self.liveFrameIndex}, "
            f"fps={fps:.2f}, parserFrames={self.parser.parsedFrameCount}, "
            f"parseErrors={self.parser.parseErrorCount}, freqOnly={self.parser.frequencyOnlyMatrixCount}, "
            f"waitElapsed={waitElapsed:.1f}s, waitAll={self.waitingCompleteFrameCount}, "
            f"waitStable={self.waitingStableFrameCount}, waitSweepLike={self.waitingSweepLikeFrameCount}, "
            f"lastWaitReason={self.lastWaitingRejectReason}"
        )


def buildArgumentParser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Temporary SensorArray capacitance ΔC/C0 percentage heatmap.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", default=None, help="Serial port, e.g. COM7, /dev/ttyACM0, /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate")
    parser.add_argument("--rows", type=int, default=8, help="Matrix rows, normally S1-S8")
    parser.add_argument("--cols", type=int, default=8, help="Matrix columns, normally D1-D8")
    parser.add_argument("--baseline-seconds", type=float, default=10.0, help="Baseline collection time after full sweep")
    parser.add_argument("--min-baseline-samples", type=int, default=3, help="Minimum valid samples required per cell")
    parser.add_argument(
        "--stable-frames-before-baseline",
        type=int,
        default=3,
        help="Fallback: start baseline after this many complete non-sweep frames if no sweep-done marker appears",
    )
    parser.add_argument(
        "--full-sweep-max-wait-seconds",
        type=float,
        default=20.0,
        help=(
            "Safety fallback: after this many seconds, start baseline if complete capacitance frames "
            "are already being parsed. V4 starts baseline from the first parsed returned matrix; raw28/FDC_DEVICE_READ4 debug lines are still ignored by the parser. "
            "Set 0 to disable."
        ),
    )
    parser.add_argument(
        "--post-sweep-stable-frames",
        type=int,
        default=3,
        help="After a sweep-done marker, require this many complete non-debug capacitance frames before baseline.",
    )
    parser.add_argument(
        "--post-sweep-min-delay-seconds",
        type=float,
        default=1.5,
        help="After a sweep-done marker, wait at least this many seconds before baseline.",
    )
    parser.add_argument("--hide-log-panel", action="store_true", help="Hide the right-side raw/state log panel")
    parser.add_argument("--log-panel-lines", type=int, default=34, help="Number of log lines shown in the right-side panel")
    parser.add_argument("--log-max-lines", type=int, default=5000, help="Maximum raw/state log lines retained for scrolling in the UI")
    parser.add_argument("--print-raw-log", action="store_true", help="Also print every raw serial/log line to terminal")
    parser.add_argument("--save-raw-log", dest="saveRawLogPath", default=None, help="Optional path to save raw/state log context to a text file")
    parser.add_argument(
        "--first-matrix-starts-baseline",
        dest="firstMatrixStartsBaseline",
        action="store_true",
        default=True,
        help="Treat the first parsed returned matrix array as the end of full sweep and immediately start baseline",
    )
    parser.add_argument(
        "--no-first-matrix-starts-baseline",
        dest="firstMatrixStartsBaseline",
        action="store_false",
        help="Use the older conservative marker/stable-frame waiting logic instead of starting from the first returned matrix",
    )
    parser.add_argument(
        "--min-valid-cells-to-start-baseline",
        type=int,
        default=0,
        help="Minimum finite cells required before the first returned matrix can start baseline. 0 means all cells.",
    )
    parser.add_argument("--vmin", type=float, default=None, help="Fixed heatmap colour minimum in percent")
    parser.add_argument("--vmax", type=float, default=None, help="Fixed heatmap colour maximum in percent")
    parser.add_argument("--cmap", default="coolwarm", help="Matplotlib colour map")
    parser.add_argument("--demo", action="store_true", help="Run without hardware using simulated frames")
    parser.add_argument("--replay-log", default=None, help="Replay an existing serial log file")
    parser.add_argument("--replay-speed", type=float, default=1.0, help="Replay speed multiplier")
    parser.add_argument("--replay-line-delay", type=float, default=0.02, help="Delay per replayed log line at 1x speed")
    parser.add_argument("--save-baseline", dest="saveBaselinePath", default=None, help="Save baseline JSON to this path")
    parser.add_argument("--save-csv", dest="saveCsvPath", default=None, help="Save live C/dC/percent frames to CSV")
    parser.add_argument("--text-decimals", type=int, default=1, help="Decimal places for cell percentage labels")
    parser.add_argument("--update-interval-ms", type=int, default=50, help="Matplotlib refresh interval")
    parser.add_argument("--serial-timeout", type=float, default=0.2, help="Serial readline timeout in seconds")
    parser.add_argument("--no-reconnect", action="store_true", help="Disable automatic serial reconnect")
    parser.add_argument("--reconnect-delay-seconds", type=float, default=1.5, help="Delay before reopening a disconnected serial port")
    parser.add_argument(
        "--serial-idle-reconnect-seconds",
        type=float,
        default=6.0,
        help="If no serial bytes arrive for this long, close and reopen the port. Set 0 to disable idle reconnect.",
    )
    parser.add_argument(
        "--no-wait-full-sweep",
        action="store_true",
        help="Start baseline immediately. Use only when the board is already in stable live mode.",
    )
    parser.add_argument("--min-abs-auto-scale", type=float, default=1.0, help="Minimum symmetric autoscale range in percent")
    parser.add_argument("--robust-percentile", type=float, default=95.0, help="Percentile used for robust symmetric autoscale")
    parser.add_argument("--max-queue-drain-per-update", type=int, default=500, help="Max queued frames/events processed per UI refresh")
    parser.add_argument("--assume-unit", default="pF", help="Unit for unitless parsed numbers; normally pF")
    parser.add_argument("--list-ports", action="store_true", help="List available serial ports and exit")
    return parser


def configFromArgs(args: argparse.Namespace) -> HeatmapConfig:
    if args.rows <= 0 or args.cols <= 0:
        raise ValueError("--rows and --cols must be positive")
    if args.baseline_seconds <= 0:
        raise ValueError("--baseline-seconds must be positive")
    if args.min_baseline_samples <= 0:
        raise ValueError("--min-baseline-samples must be positive")
    if args.full_sweep_max_wait_seconds < 0:
        raise ValueError("--full-sweep-max-wait-seconds must be >= 0")
    if args.post_sweep_stable_frames <= 0:
        raise ValueError("--post-sweep-stable-frames must be positive")
    if args.post_sweep_min_delay_seconds < 0:
        raise ValueError("--post-sweep-min-delay-seconds must be >= 0")
    if args.reconnect_delay_seconds < 0:
        raise ValueError("--reconnect-delay-seconds must be >= 0")
    if args.serial_idle_reconnect_seconds < 0:
        raise ValueError("--serial-idle-reconnect-seconds must be >= 0")
    if args.log_panel_lines <= 0:
        raise ValueError("--log-panel-lines must be positive")
    if args.log_max_lines < args.log_panel_lines:
        raise ValueError("--log-max-lines must be >= --log-panel-lines")
    totalCells = args.rows * args.cols
    if args.min_valid_cells_to_start_baseline < 0:
        raise ValueError("--min-valid-cells-to-start-baseline must be >= 0")
    effectiveMinValidCellsToStartBaseline = (
        totalCells if args.min_valid_cells_to_start_baseline == 0 else args.min_valid_cells_to_start_baseline
    )
    if effectiveMinValidCellsToStartBaseline > totalCells:
        raise ValueError("--min-valid-cells-to-start-baseline must be <= rows * cols, or 0 for all cells")
    if args.vmin is not None and args.vmax is not None and args.vmin >= args.vmax:
        raise ValueError("--vmin must be smaller than --vmax")
    if (args.vmin is None) ^ (args.vmax is None):
        raise ValueError("Please provide both --vmin and --vmax, or neither")
    return HeatmapConfig(
        port=args.port,
        baud=args.baud,
        rows=args.rows,
        cols=args.cols,
        baselineSeconds=args.baseline_seconds,
        minBaselineSamples=args.min_baseline_samples,
        stableFramesBeforeBaseline=args.stable_frames_before_baseline,
        fullSweepMaxWaitSeconds=args.full_sweep_max_wait_seconds,
        postSweepStableFrames=args.post_sweep_stable_frames,
        postSweepMinDelaySeconds=args.post_sweep_min_delay_seconds,
        showLogPanel=not args.hide_log_panel,
        logPanelLines=args.log_panel_lines,
        logMaxLines=args.log_max_lines,
        printRawLog=args.print_raw_log,
        saveRawLogPath=args.saveRawLogPath,
        firstMatrixStartsBaseline=args.firstMatrixStartsBaseline,
        minValidCellsToStartBaseline=effectiveMinValidCellsToStartBaseline,
        vmin=args.vmin,
        vmax=args.vmax,
        cmap=args.cmap,
        demo=args.demo,
        replayLog=args.replay_log,
        replaySpeed=args.replay_speed,
        replayLineDelay=args.replay_line_delay,
        saveBaselinePath=args.saveBaselinePath,
        saveCsvPath=args.saveCsvPath,
        textDecimals=args.text_decimals,
        updateIntervalMs=args.update_interval_ms,
        serialTimeout=args.serial_timeout,
        reconnectSerial=not args.no_reconnect,
        reconnectDelaySeconds=args.reconnect_delay_seconds,
        serialIdleReconnectSeconds=args.serial_idle_reconnect_seconds,
        noWaitFullSweep=args.no_wait_full_sweep,
        minAbsAutoScale=args.min_abs_auto_scale,
        robustPercentile=args.robust_percentile,
        maxQueueDrainPerUpdate=args.max_queue_drain_per_update,
        assumeUnit=args.assume_unit,
    )


def installSignalHandlers(appHolder: Dict[str, Optional[CapPercentHeatmapApp]]) -> None:
    def handleSignal(_signum: int, _frame: Any) -> None:
        app = appHolder.get("app")
        if app is not None:
            app.shutdown()
        try:
            if plt is not None:
                plt.close("all")
        finally:
            sys.exit(0)

    try:
        signal.signal(signal.SIGINT, handleSignal)
        signal.signal(signal.SIGTERM, handleSignal)
    except Exception:
        pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    argumentParser = buildArgumentParser()
    args = argumentParser.parse_args(argv)

    if args.list_ports:
        ports = listSerialPorts()
        if not ports:
            print("No serial ports detected, or pyserial is not installed.")
        else:
            for port in ports:
                print(port)
        return 0

    try:
        config = configFromArgs(args)
        appHolder: Dict[str, Optional[CapPercentHeatmapApp]] = {"app": None}
        installSignalHandlers(appHolder)
        app = CapPercentHeatmapApp(config)
        appHolder["app"] = app
        app.run()
    except Exception as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
