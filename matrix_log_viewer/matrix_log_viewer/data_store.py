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
UINT64_MASK = (1 << 64) - 1
VOLTAGE_TO_UV = {"uv": 1.0, "mv": 1_000.0, "v": 1_000_000.0}


class StreamRingBuffer:
    """Fixed-size numpy ring for one parsed stream."""

    def __init__(self, stream: str, capacityFrames: int):
        self.stream = stream
        self.capacityFrames = max(1, int(capacityFrames))
        self.seqArray = np.full(self.capacityFrames, -1, dtype=np.int64)
        self.timestampUsArray = np.zeros(self.capacityFrames, dtype=np.int64)
        self.timeSecondsArray = np.full(self.capacityFrames, np.nan, dtype=np.float64)
        self.durationUsArray = np.zeros(self.capacityFrames, dtype=np.int32)
        self.valuesUvArray = np.full((self.capacityFrames, len(CELL_NAMES)), np.nan, dtype=np.float64)
        self.validMaskArray = np.zeros(self.capacityFrames, dtype=np.uint64)
        self.statusFlagsArray = np.zeros(self.capacityFrames, dtype=np.uint32)
        self.firstStatusCodeArray = np.zeros(self.capacityFrames, dtype=np.int32)
        self.lastStatusCodeArray = np.zeros(self.capacityFrames, dtype=np.int32)
        self.droppedFramesArray = np.zeros(self.capacityFrames, dtype=np.int32)
        self.outputDecimatedFramesArray = np.zeros(self.capacityFrames, dtype=np.int32)
        self.adsDrArray = np.full(self.capacityFrames, -1, dtype=np.int16)
        self.outputDividerArray = np.full(self.capacityFrames, -1, dtype=np.int16)
        self.unitArray = np.full(self.capacityFrames, "", dtype=object)
        self.writeIndex = 0
        self.frameCount = 0
        self.totalFrames = 0
        self.revision = 0
        self.latestSeq: int | None = None

    def addFrame(self, frame: MatrixFrame) -> dict[str, Any]:
        index = self.writeIndex
        seq = int(frame.seq) if frame.seq is not None else -1
        timestamp_us = int(frame.timestampUs or 0)
        time_seconds = timestamp_us / 1_000_000.0
        valid_mask = int(frame.validMask if frame.validMask is not None else UINT64_MASK) & UINT64_MASK
        values = np.array([float(frame.values.get(cell_name, math.nan)) for cell_name in CELL_NAMES], dtype=np.float64)
        factor, stored_unit = _unit_storage(frame.unit)
        values = values * factor
        invalid = np.array([((valid_mask >> cell_index) & 0x1) == 0 for cell_index in range(len(CELL_NAMES))], dtype=bool)
        values[invalid] = np.nan

        self.seqArray[index] = seq
        self.timestampUsArray[index] = timestamp_us
        self.timeSecondsArray[index] = time_seconds
        self.durationUsArray[index] = int(frame.durationUs or 0)
        self.valuesUvArray[index, :] = values
        self.validMaskArray[index] = np.uint64(valid_mask)
        self.statusFlagsArray[index] = np.uint32(int(frame.statusFlags or 0))
        self.firstStatusCodeArray[index] = np.int32(int(frame.firstStatusCode or 0))
        self.lastStatusCodeArray[index] = np.int32(int(frame.lastStatusCode or 0))
        self.droppedFramesArray[index] = np.int32(int(frame.droppedFrames or 0))
        self.outputDecimatedFramesArray[index] = np.int32(int(frame.outputDecimatedFrames or 0))
        self.adsDrArray[index] = np.int16(int(frame.adsDr if frame.adsDr is not None else -1))
        self.outputDividerArray[index] = np.int16(int(frame.outputDivider if frame.outputDivider is not None else -1))
        self.unitArray[index] = stored_unit

        self.writeIndex = (self.writeIndex + 1) % self.capacityFrames
        self.frameCount = min(self.frameCount + 1, self.capacityFrames)
        self.totalFrames += 1
        self.revision += 1
        self.latestSeq = seq if seq >= 0 else None
        return self._meta_at(index)

    def latestSnapshot(self) -> dict[str, Any] | None:
        if self.frameCount <= 0:
            return None
        index = (self.writeIndex - 1) % self.capacityFrames
        meta = self._meta_at(index)
        return {
            **meta,
            "stream": self.stream,
            "revision": int(self.revision),
            "matrixUv": self.valuesUvArray[index, :].reshape(MATRIX_SIZE, MATRIX_SIZE).copy(),
        }

    def historyArrays(
        self,
        cellName: str,
        xAxis: str = "timeSeconds",
        windowMode: str = "all",
        lastN: int | None = None,
        customMin: float | None = None,
        customMax: float | None = None,
        sinceSeq: int | None = None,
        windowSeconds: float | None = None,
        customRange: tuple[float | None, float | None] | None = None,
    ) -> tuple[np.ndarray, np.ndarray, dict]:
        cell_index = _cell_index(cellName)
        if cell_index is None or self.frameCount <= 0:
            return _empty_history_result(self.stream, cellName, xAxis, self.revision)

        indices = self._ordered_indices()
        seq = self.seqArray[indices].astype(np.float64)
        timestamp_us = self.timestampUsArray[indices].astype(np.float64)
        time_seconds = self.timeSecondsArray[indices].astype(np.float64)
        y_values = self.valuesUvArray[indices, cell_index].astype(np.float64)
        units = self.unitArray[indices].astype(object)
        valid = np.isfinite(y_values)

        if sinceSeq is not None:
            mask = seq > float(sinceSeq)
            seq, timestamp_us, time_seconds, y_values, units, valid = _apply_mask(mask, seq, timestamp_us, time_seconds, y_values, units, valid)

        x_column = xAxis if xAxis in ("seq", "timestampUs", "timeSeconds") else "timeSeconds"
        x_values = {"seq": seq, "timestampUs": timestamp_us, "timeSeconds": time_seconds}[x_column]
        indices2 = self._window_indices(x_values, time_seconds, windowMode, lastN, customMin, customMax, windowSeconds, customRange)
        seq, timestamp_us, time_seconds, y_values, units, valid = (
            seq[indices2],
            timestamp_us[indices2],
            time_seconds[indices2],
            y_values[indices2],
            units[indices2],
            valid[indices2],
        )
        x_values = {"seq": seq, "timestampUs": timestamp_us, "timeSeconds": time_seconds}[x_column]
        meta = {
            "frameType": self.stream,
            "cellName": cellName,
            "xColumn": x_column,
            "revision": int(self.revision),
            "rawPointCount": int(self.frameCount),
            "visiblePointCount": int(len(x_values)),
            "seq": seq,
            "timestampUs": timestamp_us,
            "timeSeconds": time_seconds,
            "unit": units,
            "rawValue": y_values,
            "valid": valid,
        }
        return x_values, y_values, meta

    def _ordered_indices(self) -> np.ndarray:
        if self.frameCount < self.capacityFrames:
            return np.arange(self.frameCount, dtype=np.int64)
        return np.concatenate(
            [
                np.arange(self.writeIndex, self.capacityFrames, dtype=np.int64),
                np.arange(0, self.writeIndex, dtype=np.int64),
            ]
        )

    def _window_indices(
        self,
        x_values: np.ndarray,
        time_values: np.ndarray,
        window_mode: str,
        last_n: int | None,
        custom_min: float | None,
        custom_max: float | None,
        window_seconds: float | None,
        custom_range: tuple[float | None, float | None] | None,
    ) -> np.ndarray:
        if not x_values.size:
            return np.array([], dtype=int)
        if custom_range is not None:
            custom_min, custom_max = custom_range
            window_mode = "custom"
        if window_seconds is not None:
            window_mode = "windowSeconds"
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
            "windowSeconds": float(window_seconds or 0.0),
        }
        if window in seconds_by_window and seconds_by_window[window] > 0:
            finite_times = time_values[np.isfinite(time_values)]
            if not finite_times.size:
                return np.arange(len(x_values), dtype=int)
            latest_time = float(finite_times[-1])
            return np.flatnonzero(time_values >= latest_time - seconds_by_window[window])
        return np.arange(len(x_values), dtype=int)

    def _meta_at(self, index: int) -> dict[str, Any]:
        seq = int(self.seqArray[index])
        return {
            "frameType": self.stream,
            "seq": seq if seq >= 0 else None,
            "timestampUs": int(self.timestampUsArray[index]),
            "timeSeconds": float(self.timeSecondsArray[index]),
            "durationUs": int(self.durationUsArray[index]),
            "unit": str(self.unitArray[index] or ""),
            "validMask": int(self.validMaskArray[index]),
            "statusFlags": int(self.statusFlagsArray[index]),
            "firstStatusCode": int(self.firstStatusCodeArray[index]),
            "firstStatusCodeName": statusCodeName(int(self.firstStatusCodeArray[index])),
            "lastStatusCode": int(self.lastStatusCodeArray[index]),
            "lastStatusCodeName": statusCodeName(int(self.lastStatusCodeArray[index])),
            "droppedFrames": int(self.droppedFramesArray[index]),
            "droppedFramesSaturated": int(self.droppedFramesArray[index]),
            "outputDecimatedFrames": int(self.outputDecimatedFramesArray[index]),
            "outputDecimatedFramesSaturated": int(self.outputDecimatedFramesArray[index]),
            "adsDr": None if int(self.adsDrArray[index]) < 0 else int(self.adsDrArray[index]),
            "outputDivider": None if int(self.outputDividerArray[index]) < 0 else int(self.outputDividerArray[index]),
        }


class MatrixDataStore:
    def __init__(self, maxPointsPerCell: int = 5000):
        self.maxPointsPerCell = max(1, int(maxPointsPerCell))
        self._lock = threading.RLock()
        self._streams: dict[str, StreamRingBuffer] = {}
        self._latestMetaByType: dict[str, dict] = {}
        self._latestRevisionByType: dict[str, int] = defaultdict(int)
        self._latestSeqByType: dict[str, int | None] = {}
        self._wideRowsByType: dict[str, deque] = {}
        self._receivedByType: dict[str, int] = defaultdict(int)
        self._deviceStatus: DeviceStatus | None = None
        self._deviceSummary: dict[str, Any] = {}
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
        with self._lock:
            stream = self._ensure_stream(frame_type)
            meta = stream.addFrame(frame)
            self._wideRowsByType[frame_type].append(self._wide_row_from_stream_frame(frame, stream, meta))
            self._receivedByType[frame_type] += 1
            self._stats["framesTotal"] += 1
            self._latestMetaByType[frame_type] = dict(meta)
            self._latestSeqByType[frame_type] = meta.get("seq")
            self._latestRevisionByType[frame_type] = stream.revision
            self._record_frame_events(frame)

    def addDeviceStatus(self, status: DeviceStatus) -> None:
        with self._lock:
            self._deviceStatus = status
            self._stats["deviceStatuses"] += 1
            self._merge_device_status(status)

    def addDeviceEvent(self, event: DeviceEvent) -> None:
        with self._lock:
            self._deviceEvents.append(self._event_dict(event))
            self._stats["deviceEvents"] += 1
            if event.eventType == "ASCII_AFTER_FAST_BINARY_START":
                self._deviceSummary["asciiAfterFastBinaryStart"] = True
                self._deviceSummary["protocolPollutionCount"] = int(self._deviceSummary.get("protocolPollutionCount") or 0) + 1

    def getLatestMatrixSnapshot(self, frameType: str = DEFAULT_FRAME_TYPE) -> dict:
        with self._lock:
            resolved = self._resolve_frame_type(frameType)
            stream = self._streams.get(resolved or frameType)
            snapshot = stream.latestSnapshot() if stream else None
            if snapshot is not None:
                return snapshot
            return {**self._empty_meta(frameType), "stream": resolved or frameType, "revision": self.getLatestRevision(frameType), "matrixUv": np.full((MATRIX_SIZE, MATRIX_SIZE), np.nan, dtype=float)}

    def getLatestMatrix(self, frameType: str = DEFAULT_FRAME_TYPE) -> np.ndarray:
        return self.getLatestMatrixSnapshot(frameType)["matrixUv"].copy()

    def getLatestFrameMeta(self, frameType: str = DEFAULT_FRAME_TYPE) -> dict:
        with self._lock:
            resolved = self._resolve_frame_type(frameType)
            if resolved and resolved in self._latestMetaByType:
                return dict(self._latestMetaByType[resolved])
        return self._empty_meta(frameType)

    def getLatestRevision(self, frameType: str = DEFAULT_FRAME_TYPE) -> int:
        with self._lock:
            resolved = self._resolve_frame_type(frameType)
            stream = self._streams.get(resolved or frameType)
            if stream is not None:
                return int(stream.revision)
            return int(self._latestRevisionByType.get(resolved or frameType, 0))

    def getLatestSeq(self, frameType: str = DEFAULT_FRAME_TYPE) -> int | None:
        with self._lock:
            resolved = self._resolve_frame_type(frameType)
            return self._latestSeqByType.get(resolved or frameType)

    def getLatestMatrixAndMeta(self, frameType: str = DEFAULT_FRAME_TYPE) -> tuple[np.ndarray, dict, int]:
        snapshot = self.getLatestMatrixSnapshot(frameType)
        matrix = snapshot.pop("matrixUv").copy()
        revision = int(snapshot.get("revision") or 0)
        return matrix, snapshot, revision

    def resolveFrameType(self, frameType: str | None = DEFAULT_FRAME_TYPE) -> str:
        with self._lock:
            return self._resolve_frame_type(frameType) or (frameType or DEFAULT_FRAME_TYPE)

    def getFrameCount(self, frameType: str) -> int:
        with self._lock:
            resolved = self._resolve_frame_type(frameType)
            return int(self._receivedByType.get(resolved or frameType, 0))

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
        x_values, y_values, meta = self.getCellHistoryArrays(frameType, cellName, xAxis, windowMode, lastN, customMin, customMax)
        if not len(x_values):
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        return pd.DataFrame(
            {
                "seq": meta["seq"],
                "timestampUs": meta["timestampUs"],
                "timeSeconds": meta["timeSeconds"],
                "value": y_values,
                "unit": meta["unit"],
            },
            columns=HISTORY_COLUMNS,
        )

    def getCellHistoryArrays(
        self,
        frameType: str,
        cellName: str,
        xAxis: str = "timeSeconds",
        windowMode: str = "all",
        lastN: int | None = None,
        customMin: float | None = None,
        customMax: float | None = None,
        sinceSeq: int | None = None,
        windowSeconds: float | None = None,
        customRange: tuple[float | None, float | None] | None = None,
    ) -> tuple[np.ndarray, np.ndarray, dict]:
        with self._lock:
            resolved = self._resolve_frame_type(frameType)
            key = resolved or frameType
            stream = self._streams.get(key)
            if stream is None:
                revision = int(self._latestRevisionByType.get(key, 0))
                return _empty_history_result(key, cellName, xAxis, revision)
            return stream.historyArrays(cellName, xAxis, windowMode, lastN, customMin, customMax, sinceSeq, windowSeconds, customRange)

    def getAvailableFrameTypes(self) -> list[str]:
        with self._lock:
            dynamic = sorted(frame_type for frame_type, count in self._receivedByType.items() if count > 0)
        preferred = [DEFAULT_FRAME_TYPE, FALLBACK_FRAME_TYPE, "MATV_RAW", "MATV_GAIN", "MATV_ERR"]
        return list(dict.fromkeys([*preferred, *dynamic]))

    def getLatestDeviceStatus(self) -> dict:
        with self._lock:
            status_dict = asdict(self._deviceStatus) if self._deviceStatus is not None else {}
            if self._deviceSummary:
                status_dict["summary"] = dict(self._deviceSummary)
            return status_dict

    def getRecentDeviceEvents(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = list(self._deviceEvents)[-max(0, int(limit)):]
        return rows

    def clear(self, frameType: str | None = None) -> None:
        with self._lock:
            if frameType is None:
                known_frame_types = set(self._latestRevisionByType)
                known_frame_types.update(self._streams)
                known_frame_types.update(self._receivedByType)
                for known_frame_type in known_frame_types:
                    self._latestRevisionByType[known_frame_type] += 1
                self._streams.clear()
                self._latestMetaByType.clear()
                self._latestSeqByType.clear()
                self._wideRowsByType.clear()
                self._receivedByType.clear()
                self._lastCountersByType.clear()
                self._stats["framesTotal"] = 0
                return

            stream = self._streams.pop(frameType, None)
            self._latestRevisionByType[frameType] = (stream.revision if stream else self._latestRevisionByType.get(frameType, 0)) + 1
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
            stream_stats = {
                stream_name: {
                    "revision": stream.revision,
                    "frameCount": stream.frameCount,
                    "totalFrames": stream.totalFrames,
                    "latestSeq": stream.latestSeq,
                }
                for stream_name, stream in self._streams.items()
            }
            return {
                **self._stats,
                "receivedByType": dict(self._receivedByType),
                "availableFrameTypes": available,
                "streams": stream_stats,
            }

    @staticmethod
    def downsampleHistoryFrame(history: pd.DataFrame, xAxis: str = "timeSeconds", maxPoints: int = 5000) -> tuple[pd.DataFrame, bool]:
        if history.empty or len(history) <= maxPoints:
            return history, False
        selected, downsampled = MatrixDataStore.downsampleHistoryArrays(
            history[xAxis if xAxis in history.columns else "timeSeconds"].to_numpy(dtype=float),
            history["value"].to_numpy(dtype=float),
            maxPoints,
        )
        return history.iloc[selected].sort_values(by=xAxis if xAxis in history.columns else "timeSeconds"), downsampled

    @staticmethod
    def downsampleHistoryArrays(xValues: np.ndarray, yValues: np.ndarray, maxPoints: int = 5000) -> tuple[np.ndarray, bool]:
        x = np.asarray(xValues, dtype=float)
        y = np.asarray(yValues, dtype=float)
        if len(y) <= maxPoints:
            return np.arange(len(y), dtype=int), False
        if maxPoints < 4:
            return np.arange(max(0, len(y) - maxPoints), len(y), dtype=int), True

        bucket_count = max(1, maxPoints // 2)
        bucket_size = int(math.ceil(len(y) / bucket_count))
        selected_indices: set[int] = {0, len(y) - 1}

        for start in range(0, len(y), bucket_size):
            end = min(len(y), start + bucket_size)
            bucket = y[start:end]
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
        if x.size == y.size and selected.size:
            selected = selected[np.argsort(x[selected], kind="stable")]
        return selected, True

    def _ensure_stream(self, frame_type: str) -> StreamRingBuffer:
        if frame_type not in self._streams:
            self._streams[frame_type] = StreamRingBuffer(frame_type, self.maxPointsPerCell)
        if frame_type not in self._wideRowsByType:
            self._wideRowsByType[frame_type] = deque(maxlen=self.maxPointsPerCell)
        self._latestRevisionByType.setdefault(frame_type, 0)
        return self._streams[frame_type]

    def _resolve_frame_type(self, frame_type: str | None) -> str | None:
        if not frame_type:
            frame_type = DEFAULT_FRAME_TYPE
        if self._receivedByType.get(frame_type, 0) > 0:
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

        last = self._lastCountersByType.setdefault(frame_type, {"droppedFrames": 0, "outputDecimatedFrames": 0})
        dropped = int(frame.droppedFrames or 0)
        decimated = int(frame.outputDecimatedFrames or 0)
        if dropped > last.get("droppedFrames", 0):
            self._append_counter_event(frame, "DROPPED_FRAMES_INCREASED", dropped, last.get("droppedFrames", 0))
            self._append_counter_event(frame, "DEVICE_DROP", dropped, last.get("droppedFrames", 0))
        if decimated > last.get("outputDecimatedFrames", 0):
            self._append_counter_event(frame, "OUTPUT_DECIMATED_INCREASED", decimated, last.get("outputDecimatedFrames", 0))
            self._append_counter_event(frame, "DEVICE_DECIMATED", decimated, last.get("outputDecimatedFrames", 0))
        last["droppedFrames"] = dropped
        last["outputDecimatedFrames"] = decimated

    def _append_counter_event(self, frame: MatrixFrame, event_type: str, current: int, previous: int) -> None:
        self._deviceEvents.append(
            {
                "eventType": event_type,
                "code": frame.lastStatusCode,
                "name": statusCodeName(frame.lastStatusCode),
                "fields": {"frameType": frame.frameType, "seq": str(frame.seq), "previous": str(previous), "current": str(current)},
                "rawLine": "",
            }
        )

    def _merge_device_status(self, status: DeviceStatus) -> None:
        fields = _typed_fields(status.fields)
        if status.fastBinaryStartSeen:
            self._deviceSummary["fastBinaryStartSeen"] = True
            self._deviceSummary["fastBinaryStartMeta"] = dict(status.fastBinaryStartMeta or fields)
            self._deviceSummary["pureBinaryMode"] = status.pureBinaryMode or bool(fields.get("pure"))
        if status.fastBinaryDiagLatest:
            self._deviceSummary["fastBinaryDiagLatest"] = dict(status.fastBinaryDiagLatest)
        updates = {
            "droppedBeforeFirstByte": status.droppedBeforeFirstByte,
            "partialAfterFirstByte": status.partialAfterFirstByte,
            "fullFrameWriteCount": status.fullFrameWriteCount,
            "fullFrameWriteFailCount": status.fullFrameWriteFailCount,
            "dropPolicy": status.dropPolicy or fields.get("dropPolicy"),
            "usbExactBinaryWrite": status.usbExactBinaryWrite if status.usbExactBinaryWrite is not None else _typed_bool(fields.get("usbExactBinaryWrite")),
            "fastBinaryStartupDiagMs": status.fastBinaryStartupDiagMs or _typed_int(fields.get("fastBinaryStartupDiagMs")),
            "latestScanFps": status.latestScanFps or _typed_float(fields.get("scanFps")),
            "latestOutFps": status.latestOutFps or _typed_float(fields.get("outFps")),
            "latestOutputDiv": status.latestOutputDiv or _typed_int(_first_present(fields, "outputDiv", "outputDivider")),
            "latestQUsed": status.latestQUsed or _typed_int(fields.get("qUsed")),
            "latestQFull": status.latestQFull or _typed_int(fields.get("qFull")),
            "latestDrop": status.latestDrop or _typed_int(_first_present(fields, "drop", "droppedFrames")),
            "latestDecimated": status.latestDecimated or _typed_int(_first_present(fields, "decimated", "outputDecimatedFrames")),
            "latestShortWrite": _typed_int(fields.get("shortWrite")),
            "latestWriteFail": _typed_int(fields.get("writeFail")),
            "latestUsbAvgUs": _typed_int(fields.get("usbAvgUs")),
            "latestUsbMaxUs": _typed_int(fields.get("usbMaxUs")),
            "latestHeapFree": _typed_int(fields.get("heapFree")),
            "latestHeapMinFree": _typed_int(fields.get("heapMinFree")),
            "latestOutStackMinWords": _typed_int(fields.get("outStackMinWords")),
            "latestScanStackMinWords": _typed_int(fields.get("scanStackMinWords")),
        }
        for key, value in updates.items():
            if value is not None:
                self._deviceSummary[key] = value
        if self._deviceSummary.get("partialAfterFirstByte", 0):
            self._stats["warnings"] += 1
            self._stats["lastWarning"] = "PROTOCOL_RISK: firmware reported partialAfterFirstByte > 0"

    def _wide_row_from_stream_frame(self, frame: MatrixFrame, stream: StreamRingBuffer, meta: dict) -> dict[str, Any]:
        latest_index = (stream.writeIndex - 1) % stream.capacityFrames
        row = self._wide_row_base_from_meta(frame.frameType, meta)
        for cell_index, cell_name in enumerate(CELL_NAMES):
            row[cell_name] = float(stream.valuesUvArray[latest_index, cell_index])
        return row

    @staticmethod
    def _wide_row_base_from_meta(frame_type: str, meta: dict) -> dict[str, Any]:
        return {
            "frame_type": frame_type,
            "seq": meta.get("seq"),
            "timestamp_us": meta.get("timestampUs"),
            "time_s": meta.get("timeSeconds"),
            "duration_us": meta.get("durationUs"),
            "unit": meta.get("unit"),
            "valid_mask": meta.get("validMask"),
            "status_flags": meta.get("statusFlags"),
            "first_status_code": meta.get("firstStatusCode"),
            "first_status_code_name": meta.get("firstStatusCodeName"),
            "last_status_code": meta.get("lastStatusCode"),
            "last_status_code_name": meta.get("lastStatusCodeName"),
            "dropped_frames": meta.get("droppedFrames"),
            "output_decimated_frames": meta.get("outputDecimatedFrames"),
            "ads_dr": meta.get("adsDr"),
            "output_divider": meta.get("outputDivider"),
        }

    @staticmethod
    def _wide_row_base(frame: MatrixFrame, time_seconds: float) -> dict[str, Any]:
        meta = {
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
        return MatrixDataStore._wide_row_base_from_meta(frame.frameType, meta)

    @staticmethod
    def _empty_meta(frame_type: str = DEFAULT_FRAME_TYPE) -> dict:
        return {
            "frameType": frame_type,
            "seq": None,
            "timestampUs": None,
            "timeSeconds": None,
            "durationUs": None,
            "unit": "uV",
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
        return {"eventType": event.eventType, "code": event.code, "name": event.name, "fields": dict(event.fields), "rawLine": event.rawLine}

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
        factor, stored_unit = _unit_storage(frame.unit)
        row["unit"] = stored_unit
        valid_mask = int(frame.validMask if frame.validMask is not None else UINT64_MASK) & UINT64_MASK
        for cell_index, cell_name in enumerate(CELL_NAMES):
            value = float(frame.values.get(cell_name, math.nan)) * factor
            if ((valid_mask >> cell_index) & 0x1) == 0:
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


def _empty_history_result(frame_type: str, cell_name: str, x_axis: str, revision: int) -> tuple[np.ndarray, np.ndarray, dict]:
    x_column = x_axis if x_axis in ("seq", "timestampUs", "timeSeconds") else "timeSeconds"
    empty = np.array([], dtype=float)
    return empty, empty, {
        "frameType": frame_type,
        "cellName": cell_name,
        "xColumn": x_column,
        "revision": int(revision),
        "rawPointCount": 0,
        "visiblePointCount": 0,
        "seq": empty,
        "timestampUs": empty,
        "timeSeconds": empty,
        "unit": np.array([], dtype=object),
        "rawValue": empty,
        "valid": np.array([], dtype=bool),
    }


def _apply_mask(mask: np.ndarray, *arrays: np.ndarray) -> tuple[np.ndarray, ...]:
    return tuple(array[mask] for array in arrays)


def _cell_index(cell_name: str) -> int | None:
    try:
        source_text, detector_text = cell_name.split("D", maxsplit=1)
        source = int(source_text[1:]) - 1
        detector = int(detector_text) - 1
    except Exception:
        return None
    if not (0 <= source < MATRIX_SIZE and 0 <= detector < MATRIX_SIZE):
        return None
    return source * MATRIX_SIZE + detector


def _unit_storage(unit: str) -> tuple[float, str]:
    key = _normalize_unit(unit)
    if key in VOLTAGE_TO_UV:
        return VOLTAGE_TO_UV[key], "uV"
    return 1.0, unit or ""


def _normalize_unit(unit: Any) -> str:
    return str(unit or "").strip().replace("\u00b5", "u").replace("\u03bc", "u").lower()


def _typed_fields(fields: dict[str, str]) -> dict[str, Any]:
    typed: dict[str, Any] = {}
    for key, value in fields.items():
        text = str(value).strip()
        if text == "":
            typed[key] = ""
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
