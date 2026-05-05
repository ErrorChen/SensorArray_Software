from __future__ import annotations

import csv
import math
import threading
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

from .config import CELL_NAMES, MATRIX_SIZE, WIDE_CSV_COLUMNS
from .matv_parser import MatvFrame


class MatrixDataStore:
    def __init__(self, maxPointsPerCell: int = 5000):
        self.maxPointsPerCell = max(1, int(maxPointsPerCell))
        self._lock = threading.Lock()
        self._cellHistory = {
            cell_name: deque(maxlen=self.maxPointsPerCell) for cell_name in CELL_NAMES
        }
        self._wideRows = deque(maxlen=self.maxPointsPerCell)
        self._latestMatrix = np.full((MATRIX_SIZE, MATRIX_SIZE), np.nan, dtype=float)
        self._latestMeta = self._empty_meta()
        self._receivedFrames = 0

    def addFrame(self, frame: MatvFrame) -> None:
        time_seconds = frame.timestampUs / 1_000_000.0
        wide_row = {
            "seq": frame.seq,
            "timestamp_us": frame.timestampUs,
            "time_s": time_seconds,
            "duration_us": frame.durationUs,
            "unit": frame.unit,
        }

        with self._lock:
            for cell_name in CELL_NAMES:
                value = frame.values.get(cell_name, math.nan)
                wide_row[cell_name] = value
                self._cellHistory[cell_name].append(
                    {
                        "seq": frame.seq,
                        "timestampUs": frame.timestampUs,
                        "timeSeconds": time_seconds,
                        "value": value,
                        "unit": frame.unit,
                    }
                )

                row_index, column_index = self._cell_indices(cell_name)
                self._latestMatrix[row_index, column_index] = value

            self._wideRows.append(wide_row)
            self._receivedFrames += 1
            self._latestMeta = {
                "seq": frame.seq,
                "timestampUs": frame.timestampUs,
                "timeSeconds": time_seconds,
                "durationUs": frame.durationUs,
                "unit": frame.unit,
                "receivedFrames": self._receivedFrames,
            }

    def getLatestMatrix(self) -> np.ndarray:
        with self._lock:
            return self._latestMatrix.copy()

    def getLatestFrameMeta(self) -> dict:
        with self._lock:
            return dict(self._latestMeta)

    def getCellHistory(self, cellName: str) -> pd.DataFrame:
        with self._lock:
            rows = list(self._cellHistory.get(cellName, []))

        if not rows:
            return pd.DataFrame(columns=["seq", "timestampUs", "timeSeconds", "value", "unit"])
        return pd.DataFrame(rows, columns=["seq", "timestampUs", "timeSeconds", "value", "unit"])

    def clear(self) -> None:
        with self._lock:
            self._cellHistory = {
                cell_name: deque(maxlen=self.maxPointsPerCell) for cell_name in CELL_NAMES
            }
            self._wideRows = deque(maxlen=self.maxPointsPerCell)
            self._latestMatrix = np.full((MATRIX_SIZE, MATRIX_SIZE), np.nan, dtype=float)
            self._latestMeta = self._empty_meta()
            self._receivedFrames = 0

    def toWideDataFrame(self) -> pd.DataFrame:
        with self._lock:
            rows = list(self._wideRows)

        if not rows:
            return pd.DataFrame(columns=WIDE_CSV_COLUMNS)
        return pd.DataFrame(rows, columns=WIDE_CSV_COLUMNS)

    @staticmethod
    def _cell_indices(cell_name: str) -> tuple[int, int]:
        # Natural MATV order maps S1D1 to matrix[0, 0] and S8D8 to matrix[7, 7].
        source_index, detector_index = cell_name.split("D", maxsplit=1)
        return int(source_index[1:]) - 1, int(detector_index) - 1

    @staticmethod
    def _empty_meta() -> dict:
        return {
            "seq": None,
            "timestampUs": None,
            "timeSeconds": None,
            "durationUs": None,
            "unit": "",
            "receivedFrames": 0,
        }


class CsvFrameWriter:
    """Append parsed MATV frames to a wide CSV file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def appendFrame(self, frame: MatvFrame) -> None:
        time_seconds = frame.timestampUs / 1_000_000.0
        row = {
            "seq": frame.seq,
            "timestamp_us": frame.timestampUs,
            "time_s": time_seconds,
            "duration_us": frame.durationUs,
            "unit": frame.unit,
        }
        for cell_name in CELL_NAMES:
            row[cell_name] = frame.values.get(cell_name, math.nan)

        with self._lock:
            if self.path.parent:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            needs_header = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=WIDE_CSV_COLUMNS)
                if needs_header:
                    writer.writeheader()
                writer.writerow(row)

