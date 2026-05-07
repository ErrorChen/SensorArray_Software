from __future__ import annotations

import csv
import math
import threading
from collections import defaultdict, deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import CELL_NAMES, MATRIX_SIZE, WIDE_CSV_COLUMNS
from .protocol_types import DeviceEvent, DeviceStatus, MatrixFrame
from .sensorarray_status_codes import statusCodeName

DEFAULT_FRAME_TYPE = "FAST_BINARY"
FALLBACK_FRAME_TYPE = "MATV"
HISTORY_COLUMNS = ["seq", "timestampUs", "timeSeconds", "value", "unit"]


class MatrixDataStore:
    def __init__(self, maxPointsPerCell: int = 5000):
        self.maxPointsPerCell = max(1, int(maxPointsPerCell))
        self._lock = threading.Lock()
        self._cellHistory: dict[str, dict[str, deque]] = {}
        self._latestMatrixByType: dict[str, np.ndarray] = {}
        self._latestMetaByType: dict[str, dict] = {}
        self._latestRevisionByType: dict[str, int] = defaultdict(int)
        self._latestSeqByType: dict[str, int | None] = {}
        self._wideRowsByType: dict[str, deque] = {}
        self._receivedByType: dict[str, int] = defaultdict(int)
        self._deviceStatus: DeviceStatus | None = None
        self._deviceEvents: deque[dict] = deque(maxlen=200)
        self._lastCountersByType: dict[str, dict[str, int]] = {}
        self._stats = {
            "framesTotal": 0,
            "deviceStatuses": 0,
            "deviceEvents": 0,
            "warnings": 0,
            "lastWarning": "",
        }

    def addFrame(self, frame: MatrixFrame) -> None:
        frame_type = frame.frameType or FALLBACK_FRAME_TYPE
        time_seconds = frame.timestampUs / 1_000_000.0
        matrix = np.full((MATRIX_SIZE, MATRIX_SIZE), np.nan, dtype=float)
        wide_row = self._wide_row_base(frame, time_seconds)

        with self._lock:
            self._ensure_frame_type(frame_type)

            for cell_index, cell_name in enumerate(CELL_NAMES):
                value = frame.values.get(cell_name, math.nan)
                if frame.validMask is not None and ((int(frame.validMask) >> cell_index) & 0x1) == 0:
                    value = math.nan

                wide_row[cell_name] = value
                self._cellHistory[frame_type][cell_name].append(
                    {
                        "seq": frame.seq,
                        "timestampUs": frame.timestampUs,
                        "timeSeconds": time_seconds,
                        "value": value,
                        "unit": frame.unit,
                    }
                )

                row_index, column_index = self._cell_indices(cell_name)
                matrix[row_index, column_index] = value

            self._wideRowsByType[frame_type].append(wide_row)
            self._latestMatrixByType[frame_type] = matrix
            self._receivedByType[frame_type] += 1
            self._stats["framesTotal"] += 1
            self._latestMetaByType[frame_type] = self._meta_from_frame(frame, time_seconds, frame_type)
            self._latestSeqByType[frame_type] = int(frame.seq) if frame.seq is not None else None
            self._latestRevisionByType[frame_type] += 1
            self._record_frame_events(frame)

    def addDeviceStatus(self, status: DeviceStatus) -> None:
        with self._lock:
            self._deviceStatus = status
            self._stats["deviceStatuses"] += 1

    def addDeviceEvent(self, event: DeviceEvent) -> None:
        with self._lock:
            self._deviceEvents.append(self._event_dict(event))
            self._stats["deviceEvents"] += 1

    def getLatestMatrix(self, frameType: str = DEFAULT_FRAME_TYPE) -> np.ndarray:
        with self._lock:
            resolved = self._resolve_frame_type(frameType)
            if resolved and resolved in self._latestMatrixByType:
                return self._latestMatrixByType[resolved].copy()
        return np.full((MATRIX_SIZE, MATRIX_SIZE), np.nan, dtype=float)

    def getLatestFrameMeta(self, frameType: str = DEFAULT_FRAME_TYPE) -> dict:
        with self._lock:
            resolved = self._resolve_frame_type(frameType)
            if resolved and resolved in self._latestMetaByType:
                return dict(self._latestMetaByType[resolved])
        return self._empty_meta(frameType)

    def getLatestRevision(self, frameType: str = DEFAULT_FRAME_TYPE) -> int:
        with self._lock:
            resolved = self._resolve_frame_type(frameType)
            return int(self._latestRevisionByType.get(resolved or frameType, 0))

    def getLatestSeq(self, frameType: str = DEFAULT_FRAME_TYPE) -> int | None:
        with self._lock:
            resolved = self._resolve_frame_type(frameType)
            return self._latestSeqByType.get(resolved or frameType)

    def getLatestMatrixAndMeta(self, frameType: str = DEFAULT_FRAME_TYPE) -> tuple[np.ndarray, dict, int]:
        with self._lock:
            resolved = self._resolve_frame_type(frameType)
            key = resolved or frameType
            if resolved and resolved in self._latestMatrixByType:
                matrix = self._latestMatrixByType[resolved].copy()
                meta = dict(self._latestMetaByType.get(resolved, self._empty_meta(resolved)))
                revision = int(self._latestRevisionByType.get(resolved, 0))
                return matrix, meta, revision
            revision = int(self._latestRevisionByType.get(key, 0))
        return np.full((MATRIX_SIZE, MATRIX_SIZE), np.nan, dtype=float), self._empty_meta(frameType), revision

    def resolveFrameType(self, frameType: str | None = DEFAULT_FRAME_TYPE) -> str:
        with self._lock:
            return self._resolve_frame_type(frameType) or (frameType or DEFAULT_FRAME_TYPE)

    def getFrameCount(self, frameType: str) -> int:
        with self._lock:
            return int(self._receivedByType.get(frameType, 0))

    def getCellHistory(
        self,
        frameType: str,
        cellName: str,
        xAxis: str = "timeSeconds",
        windowMode: str = "all",
        lastN: int | None = None,
        customMin: float | None = None,
        customMax: float | None = None,
    ) -> pd.DataFrame:
        with self._lock:
            resolved = self._resolve_frame_type(frameType)
            rows = list(self._cellHistory.get(resolved or frameType, {}).get(cellName, []))

        if not rows:
            return pd.DataFrame(columns=HISTORY_COLUMNS)

        history = pd.DataFrame(rows, columns=HISTORY_COLUMNS)
        return self._filter_history(history, xAxis, windowMode, lastN, customMin, customMax)

    def getCellHistoryArrays(
        self,
        frameType: str,
        cellName: str,
        xAxis: str = "timeSeconds",
        windowMode: str = "all",
        lastN: int | None = None,
        customMin: float | None = None,
        customMax: float | None = None,
    ) -> tuple[np.ndarray, np.ndarray, dict]:
        with self._lock:
            resolved = self._resolve_frame_type(frameType)
            key = resolved or frameType
            rows = list(self._cellHistory.get(key, {}).get(cellName, []))
            revision = int(self._latestRevisionByType.get(key, 0))

        x_column = xAxis if xAxis in ("seq", "timestampUs", "timeSeconds") else "timeSeconds"
        empty_meta = {
            "frameType": key,
            "cellName": cellName,
            "xColumn": x_column,
            "revision": revision,
            "rawPointCount": 0,
            "visiblePointCount": 0,
            "seq": np.array([], dtype=float),
            "timestampUs": np.array([], dtype=float),
            "timeSeconds": np.array([], dtype=float),
            "unit": np.array([], dtype=object),
            "rawValue": np.array([], dtype=float),
        }
        if not rows:
            return np.array([], dtype=float), np.array([], dtype=float), empty_meta

        seq_values = np.array([np.nan if row.get("seq") is None else float(row.get("seq")) for row in rows], dtype=float)
        timestamp_values = np.array([np.nan if row.get("timestampUs") is None else float(row.get("timestampUs")) for row in rows], dtype=float)
        time_values = np.array([np.nan if row.get("timeSeconds") is None else float(row.get("timeSeconds")) for row in rows], dtype=float)
        value_values = np.array([float(row.get("value", math.nan)) for row in rows], dtype=float)
        unit_values = np.array([row.get("unit") or "" for row in rows], dtype=object)

        x_values_by_column = {
            "seq": seq_values,
            "timestampUs": timestamp_values,
            "timeSeconds": time_values,
        }
        x_values = x_values_by_column.get(x_column, time_values)
        indices = self._history_window_indices(
            x_values=x_values,
            time_values=time_values,
            window_mode=windowMode,
            last_n=lastN,
            custom_min=customMin,
            custom_max=customMax,
        )

        meta = {
            "frameType": key,
            "cellName": cellName,
            "xColumn": x_column,
            "revision": revision,
            "rawPointCount": len(rows),
            "visiblePointCount": int(indices.size),
            "seq": seq_values[indices],
            "timestampUs": timestamp_values[indices],
            "timeSeconds": time_values[indices],
            "unit": unit_values[indices],
            "rawValue": value_values[indices],
        }
        return x_values[indices], value_values[indices], meta

    def getAvailableFrameTypes(self) -> list[str]:
        with self._lock:
            dynamic = sorted(frame_type for frame_type, count in self._receivedByType.items() if count > 0)
        preferred = [DEFAULT_FRAME_TYPE, FALLBACK_FRAME_TYPE, "MATV_RAW", "MATV_GAIN", "MATV_ERR"]
        return list(dict.fromkeys([*preferred, *dynamic]))

    def getLatestDeviceStatus(self) -> dict:
        with self._lock:
            if self._deviceStatus is None:
                return {}
            return asdict(self._deviceStatus)

    def getRecentDeviceEvents(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = list(self._deviceEvents)[-max(0, int(limit)):]
        return rows

    def clear(self, frameType: str | None = None) -> None:
        with self._lock:
            if frameType is None:
                known_frame_types = set(self._latestRevisionByType)
                known_frame_types.update(self._cellHistory)
                known_frame_types.update(self._latestMatrixByType)
                known_frame_types.update(self._receivedByType)
                for known_frame_type in known_frame_types:
                    self._latestRevisionByType[known_frame_type] += 1
                self._cellHistory.clear()
                self._latestMatrixByType.clear()
                self._latestMetaByType.clear()
                self._latestSeqByType.clear()
                self._wideRowsByType.clear()
                self._receivedByType.clear()
                self._lastCountersByType.clear()
                self._stats["framesTotal"] = 0
                return

            self._latestRevisionByType[frameType] += 1
            self._cellHistory.pop(frameType, None)
            self._latestMatrixByType.pop(frameType, None)
            self._latestMetaByType.pop(frameType, None)
            self._latestSeqByType.pop(frameType, None)
            self._wideRowsByType.pop(frameType, None)
            self._receivedByType.pop(frameType, None)
            self._lastCountersByType.pop(frameType, None)

    def toWideDataFrame(self, frameType: str = DEFAULT_FRAME_TYPE) -> pd.DataFrame:
        with self._lock:
            resolved = self._resolve_frame_type(frameType)
            rows = list(self._wideRowsByType.get(resolved or frameType, []))

        if not rows:
            return pd.DataFrame(columns=WIDE_CSV_COLUMNS)
        return pd.DataFrame(rows, columns=WIDE_CSV_COLUMNS)

    def getStats(self) -> dict:
        with self._lock:
            available = list(dict.fromkeys([
                DEFAULT_FRAME_TYPE,
                FALLBACK_FRAME_TYPE,
                "MATV_RAW",
                "MATV_GAIN",
                "MATV_ERR",
                *sorted(frame_type for frame_type, count in self._receivedByType.items() if count > 0),
            ]))
            return {
                **self._stats,
                "receivedByType": dict(self._receivedByType),
                "availableFrameTypes": available,
            }

    @staticmethod
    def downsampleHistoryFrame(
        history: pd.DataFrame,
        xAxis: str = "timeSeconds",
        maxPoints: int = 5000,
    ) -> tuple[pd.DataFrame, bool]:
        if history.empty or len(history) <= maxPoints:
            return history, False
        if maxPoints < 4:
            return history.tail(maxPoints), True

        y_values = history["value"].to_numpy(dtype=float)
        bucket_count = max(1, maxPoints // 2)
        bucket_size = int(math.ceil(len(history) / bucket_count))
        selected_indices: set[int] = {0, len(history) - 1}

        # Min/max bucket downsampling keeps spikes visible while bounding browser payload.
        for start in range(0, len(history), bucket_size):
            end = min(len(history), start + bucket_size)
            bucket = y_values[start:end]
            finite = np.where(np.isfinite(bucket))[0]
            if finite.size == 0:
                selected_indices.add(start)
                continue
            finite_values = bucket[finite]
            selected_indices.add(start + int(finite[np.argmin(finite_values)]))
            selected_indices.add(start + int(finite[np.argmax(finite_values)]))

        selected = sorted(selected_indices)
        if len(selected) > maxPoints:
            stride = int(math.ceil(len(selected) / maxPoints))
            selected = selected[::stride][:maxPoints]
        return history.iloc[selected].sort_values(by=xAxis if xAxis in history.columns else "timeSeconds"), True

    @staticmethod
    def downsampleHistoryArrays(
        xValues: np.ndarray,
        yValues: np.ndarray,
        maxPoints: int = 5000,
    ) -> tuple[np.ndarray, bool]:
        if len(yValues) <= maxPoints:
            return np.arange(len(yValues)), False
        if maxPoints < 4:
            return np.arange(max(0, len(yValues) - maxPoints), len(yValues)), True

        bucket_count = max(1, maxPoints // 2)
        bucket_size = int(math.ceil(len(yValues) / bucket_count))
        selected_indices: set[int] = {0, len(yValues) - 1}

        for start in range(0, len(yValues), bucket_size):
            end = min(len(yValues), start + bucket_size)
            bucket = yValues[start:end]
            finite = np.where(np.isfinite(bucket))[0]
            if finite.size == 0:
                selected_indices.add(start)
                continue
            finite_values = bucket[finite]
            selected_indices.add(start + int(finite[np.argmin(finite_values)]))
            selected_indices.add(start + int(finite[np.argmax(finite_values)]))

        selected = np.array(sorted(selected_indices), dtype=int)
        if selected.size > maxPoints:
            stride = int(math.ceil(selected.size / maxPoints))
            selected = selected[::stride][:maxPoints]
        if xValues.size == yValues.size and selected.size:
            selected = selected[np.argsort(xValues[selected], kind="stable")]
        return selected, True

    def _ensure_frame_type(self, frame_type: str) -> None:
        if frame_type not in self._cellHistory:
            self._cellHistory[frame_type] = {
                cell_name: deque(maxlen=self.maxPointsPerCell) for cell_name in CELL_NAMES
            }
        if frame_type not in self._wideRowsByType:
            self._wideRowsByType[frame_type] = deque(maxlen=self.maxPointsPerCell)
        if frame_type not in self._latestMatrixByType:
            self._latestMatrixByType[frame_type] = np.full((MATRIX_SIZE, MATRIX_SIZE), np.nan, dtype=float)
        if frame_type not in self._latestMetaByType:
            self._latestMetaByType[frame_type] = self._empty_meta(frame_type)
        self._latestRevisionByType.setdefault(frame_type, 0)

    def _resolve_frame_type(self, frame_type: str | None) -> str | None:
        if not frame_type:
            frame_type = DEFAULT_FRAME_TYPE
        if frame_type in self._receivedByType and self._receivedByType[frame_type] > 0:
            return frame_type
        if frame_type == DEFAULT_FRAME_TYPE and self._receivedByType.get(FALLBACK_FRAME_TYPE, 0) > 0:
            return FALLBACK_FRAME_TYPE
        return frame_type

    def _record_frame_events(self, frame: MatrixFrame) -> None:
        frame_type = frame.frameType
        last_code = frame.lastStatusCode if frame.lastStatusCode not in (None, 0) else frame.firstStatusCode
        if last_code not in (None, 0):
            self._deviceEvents.append(
                {
                    "eventType": "FRAME_STATUS",
                    "code": int(last_code),
                    "name": statusCodeName(int(last_code)),
                    "fields": {
                        "frameType": frame_type,
                        "seq": str(frame.seq),
                        "statusFlags": self._hex_or_blank(frame.statusFlags, width=8),
                    },
                    "rawLine": "",
                }
            )

        last = self._lastCountersByType.setdefault(
            frame_type, {"droppedFrames": 0, "outputDecimatedFrames": 0}
        )
        dropped = int(frame.droppedFrames or 0)
        decimated = int(frame.outputDecimatedFrames or 0)
        if dropped > last.get("droppedFrames", 0):
            self._append_counter_event(frame, "DROPPED_FRAMES_INCREASED", dropped, last.get("droppedFrames", 0))
        if decimated > last.get("outputDecimatedFrames", 0):
            self._append_counter_event(frame, "OUTPUT_DECIMATED_INCREASED", decimated, last.get("outputDecimatedFrames", 0))
        last["droppedFrames"] = dropped
        last["outputDecimatedFrames"] = decimated

    def _append_counter_event(self, frame: MatrixFrame, event_type: str, current: int, previous: int) -> None:
        self._deviceEvents.append(
            {
                "eventType": event_type,
                "code": frame.lastStatusCode,
                "name": statusCodeName(frame.lastStatusCode),
                "fields": {
                    "frameType": frame.frameType,
                    "seq": str(frame.seq),
                    "previous": str(previous),
                    "current": str(current),
                },
                "rawLine": "",
            }
        )

    @staticmethod
    def _wide_row_base(frame: MatrixFrame, time_seconds: float) -> dict[str, Any]:
        return {
            "frame_type": frame.frameType,
            "seq": frame.seq,
            "timestamp_us": frame.timestampUs,
            "time_s": time_seconds,
            "duration_us": frame.durationUs,
            "unit": frame.unit,
            "valid_mask": frame.validMask,
            "status_flags": frame.statusFlags,
            "first_status_code": frame.firstStatusCode,
            "first_status_code_name": statusCodeName(frame.firstStatusCode),
            "last_status_code": frame.lastStatusCode,
            "last_status_code_name": statusCodeName(frame.lastStatusCode),
            "dropped_frames": frame.droppedFrames,
            "output_decimated_frames": frame.outputDecimatedFrames,
            "ads_dr": frame.adsDr,
            "output_divider": frame.outputDivider,
        }

    @staticmethod
    def _meta_from_frame(frame: MatrixFrame, time_seconds: float, frame_type: str) -> dict:
        return {
            "frameType": frame_type,
            "seq": frame.seq,
            "timestampUs": frame.timestampUs,
            "timeSeconds": time_seconds,
            "durationUs": frame.durationUs,
            "unit": frame.unit,
            "validMask": frame.validMask,
            "statusFlags": frame.statusFlags,
            "firstStatusCode": frame.firstStatusCode,
            "firstStatusCodeName": statusCodeName(frame.firstStatusCode),
            "lastStatusCode": frame.lastStatusCode,
            "lastStatusCodeName": statusCodeName(frame.lastStatusCode),
            "droppedFrames": frame.droppedFrames,
            "outputDecimatedFrames": frame.outputDecimatedFrames,
            "adsDr": frame.adsDr,
            "outputDivider": frame.outputDivider,
        }

    @staticmethod
    def _filter_history(
        history: pd.DataFrame,
        x_axis: str,
        window_mode: str,
        last_n: int | None,
        custom_min: float | None,
        custom_max: float | None,
    ) -> pd.DataFrame:
        if history.empty:
            return history
        x_column = x_axis if x_axis in history.columns else "timeSeconds"
        window = window_mode or "all"
        if window == "last_n":
            count = max(1, int(last_n or 1000))
            return history.tail(count)
        if window == "custom":
            result = history
            if custom_min is not None:
                result = result[result[x_column] >= float(custom_min)]
            if custom_max is not None:
                result = result[result[x_column] <= float(custom_max)]
            return result
        seconds_by_window = {
            "last_10s": 10.0,
            "last_30s": 30.0,
            "last_60s": 60.0,
            "last_5min": 300.0,
        }
        if window in seconds_by_window:
            if x_column == "timeSeconds":
                latest = float(history[x_column].iloc[-1])
                return history[history[x_column] >= latest - seconds_by_window[window]]
            latest_time = float(history["timeSeconds"].iloc[-1])
            return history[history["timeSeconds"] >= latest_time - seconds_by_window[window]]
        return history

    @staticmethod
    def _history_window_indices(
        x_values: np.ndarray,
        time_values: np.ndarray,
        window_mode: str,
        last_n: int | None,
        custom_min: float | None,
        custom_max: float | None,
    ) -> np.ndarray:
        if not x_values.size:
            return np.array([], dtype=int)
        window = window_mode or "all"
        if window == "last_n":
            count = max(1, int(last_n or 1000))
            start = max(0, len(x_values) - count)
            return np.arange(start, len(x_values), dtype=int)
        if window == "custom":
            mask = np.ones(len(x_values), dtype=bool)
            if custom_min is not None:
                mask &= x_values >= float(custom_min)
            if custom_max is not None:
                mask &= x_values <= float(custom_max)
            return np.flatnonzero(mask)

        seconds_by_window = {
            "last_10s": 10.0,
            "last_30s": 30.0,
            "last_60s": 60.0,
            "last_5min": 300.0,
        }
        if window in seconds_by_window:
            finite_times = time_values[np.isfinite(time_values)]
            if not finite_times.size:
                return np.arange(len(x_values), dtype=int)
            latest_time = float(finite_times[-1])
            return np.flatnonzero(time_values >= latest_time - seconds_by_window[window])
        return np.arange(len(x_values), dtype=int)

    @staticmethod
    def _cell_indices(cell_name: str) -> tuple[int, int]:
        source_index, detector_index = cell_name.split("D", maxsplit=1)
        return int(source_index[1:]) - 1, int(detector_index) - 1

    @staticmethod
    def _empty_meta(frame_type: str = DEFAULT_FRAME_TYPE) -> dict:
        return {
            "frameType": frame_type,
            "seq": None,
            "timestampUs": None,
            "timeSeconds": None,
            "durationUs": None,
            "unit": "",
            "validMask": None,
            "statusFlags": None,
            "firstStatusCode": None,
            "firstStatusCodeName": "-",
            "lastStatusCode": None,
            "lastStatusCodeName": "-",
            "droppedFrames": None,
            "outputDecimatedFrames": None,
            "adsDr": None,
            "outputDivider": None,
        }

    @staticmethod
    def _event_dict(event: DeviceEvent) -> dict:
        return {
            "eventType": event.eventType,
            "code": event.code,
            "name": event.name,
            "fields": dict(event.fields),
            "rawLine": event.rawLine,
        }

    @staticmethod
    def _hex_or_blank(value: int | None, width: int = 4) -> str:
        if value is None:
            return ""
        return f"0x{int(value):0{width}X}"


class CsvFrameWriter:
    """Append parsed matrix frames to a wide CSV file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def appendFrame(self, frame: MatrixFrame) -> None:
        time_seconds = frame.timestampUs / 1_000_000.0
        row = MatrixDataStore._wide_row_base(frame, time_seconds)
        for cell_index, cell_name in enumerate(CELL_NAMES):
            value = frame.values.get(cell_name, math.nan)
            if frame.validMask is not None and ((int(frame.validMask) >> cell_index) & 0x1) == 0:
                value = math.nan
            row[cell_name] = value

        with self._lock:
            if self.path.parent:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            needs_header = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=WIDE_CSV_COLUMNS)
                if needs_header:
                    writer.writeheader()
                writer.writerow(row)
