from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import CELL_NAMES, DEFAULT_RENDER_TARGET_FPS, MATRIX_SIZE
from .data_store import MatrixDataStore

VOLTAGE_FACTORS_TO_UV = {"uV": 1.0, "mV": 1_000.0, "V": 1_000_000.0}
CANONICAL_UNITS = {"uv": "uV", "mv": "mV", "v": "V"}


@dataclass(frozen=True)
class HistoryKey:
    stream: str
    selectedCell: str
    xAxis: str
    resolvedUnit: str
    historyWindow: str
    customXMin: float | None
    customXMax: float | None
    followLatest: bool
    maxPoints: int
    showMarkers: bool

    def as_string(self) -> str:
        return "|".join(str(value) for value in self.__dict__.values())


class HeatmapRenderCacheThread(threading.Thread):
    def __init__(self, dataStore: MatrixDataStore, targetFps: int = DEFAULT_RENDER_TARGET_FPS):
        super().__init__(name="SensorArrayHeatmapRenderCache", daemon=True)
        self.dataStore = dataStore
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self.targetFps = int(targetFps)
        self.stream = "FAST_BINARY"
        self.selectedCell = "S1D1"
        self.unitMode = "auto"
        self.colorMode = "auto"
        self.fixedMin: float | None = None
        self.fixedMax: float | None = None
        self.latestHeatmapSnapshot: dict | None = None
        self._cacheRevision = 0
        self._lastDataRevision: int | None = None
        self.renderSkipped = 0
        self._fpsSamples: list[float] = []

    def stop(self) -> None:
        self._stop_event.set()

    def updateControls(
        self,
        stream: str | None = None,
        selectedCell: str | None = None,
        targetFps: int | None = None,
        unitMode: str | None = None,
        colorMode: str | None = None,
        fixedMin: float | None = None,
        fixedMax: float | None = None,
    ) -> None:
        with self._lock:
            if stream:
                self.stream = stream
            if selectedCell in CELL_NAMES:
                self.selectedCell = str(selectedCell)
            if targetFps:
                self.targetFps = max(1, int(targetFps))
            if unitMode is not None:
                self.unitMode = str(unitMode or "auto")
            if colorMode is not None:
                self.colorMode = str(colorMode or "auto")
            self.fixedMin = _finite_or_none(fixedMin)
            self.fixedMax = _finite_or_none(fixedMax)
            self._cacheRevision += 1
            self.latestHeatmapSnapshot = self._build_snapshot_locked(force=True)

    def getLatest(self) -> dict | None:
        with self._lock:
            return dict(self.latestHeatmapSnapshot) if self.latestHeatmapSnapshot is not None else None

    def getStats(self) -> dict:
        with self._lock:
            latest = self.latestHeatmapSnapshot or {}
            return {
                "targetFps": self.targetFps,
                "actualFps": _sample_fps(self._fpsSamples),
                "renderSkipped": self.renderSkipped,
                "displayUnit": latest.get("displayUnit"),
                "selectedCell": latest.get("selectedCell") or self.selectedCell,
            }

    def reset(self) -> None:
        with self._lock:
            self._lastDataRevision = None
            self._cacheRevision += 1
            self.latestHeatmapSnapshot = self._build_snapshot_locked(force=True)

    def run(self) -> None:
        while not self._stop_event.is_set():
            start = time.monotonic()
            with self._lock:
                snapshot = self._build_snapshot_locked(force=False)
                if snapshot is not None:
                    self.latestHeatmapSnapshot = snapshot
                    self._fpsSamples.append(start)
                    _prune_samples(self._fpsSamples, start)
            period = 1.0 / max(1, self.targetFps)
            self._stop_event.wait(max(0.001, period - (time.monotonic() - start)))

    def _build_snapshot_locked(self, force: bool) -> dict | None:
        snapshot = self.dataStore.getLatestMatrixSnapshot(self.stream)
        data_revision = int(snapshot.get("revision") or 0)
        if not force and self._lastDataRevision == data_revision and self.latestHeatmapSnapshot is not None:
            return None
        if self._lastDataRevision is not None and data_revision > self._lastDataRevision + 1:
            self.renderSkipped += data_revision - self._lastDataRevision - 1
        self._lastDataRevision = data_revision
        self._cacheRevision += 1
        matrix_uv = np.asarray(snapshot.get("matrixUv"), dtype=float)
        has_frame = snapshot.get("seq") is not None or bool(np.isfinite(matrix_uv).any())
        display_unit = _resolve_display_unit(matrix_uv, self.unitMode)
        matrix_display = _convert_uv(matrix_uv, display_unit)
        zmin, zmax = _resolve_color_range(matrix_display, self.colorMode, self.fixedMin, self.fixedMax)
        zauto = zmin is None or zmax is None
        text = _build_heatmap_text(matrix_display, display_unit)
        customdata = _build_heatmap_customdata(
            matrix_uv=matrix_uv,
            matrix_display=matrix_display,
            display_unit=display_unit,
            seq=snapshot.get("seq"),
            timestamp_us=snapshot.get("timestampUs"),
            duration_us=snapshot.get("durationUs"),
            status_flags=snapshot.get("statusFlags"),
            first_status_code=snapshot.get("firstStatusCode"),
            first_status_name=snapshot.get("firstStatusCodeName"),
            last_status_code=snapshot.get("lastStatusCode"),
            last_status_name=snapshot.get("lastStatusCodeName"),
        )
        json_matrix = _json_matrix(matrix_display)
        return {
            "kind": "heatmap",
            "cacheRevision": self._cacheRevision,
            "stream": snapshot.get("stream") or self.stream,
            "revision": data_revision,
            "seq": snapshot.get("seq"),
            "timestampUs": snapshot.get("timestampUs"),
            "timeSeconds": snapshot.get("timeSeconds"),
            "durationUs": snapshot.get("durationUs"),
            "empty": not has_frame,
            "message": None if has_frame else f"No data for selected stream: {snapshot.get('stream') or self.stream}",
            "matrix": json_matrix,
            "matrixUv": _json_matrix(matrix_uv),
            "matrixDisplay": json_matrix,
            "displayUnit": display_unit,
            "text": text,
            "customData": customdata,
            "customdata": customdata,
            "colorbarTitle": display_unit,
            "colorbarTickFormat": _colorbar_tick_format(display_unit),
            "zmin": zmin,
            "zmax": zmax,
            "zauto": zauto,
            "validMask": snapshot.get("validMask"),
            "selectedCell": self.selectedCell,
            "statusFlags": snapshot.get("statusFlags"),
            "firstStatusCode": snapshot.get("firstStatusCode"),
            "firstStatusCodeName": snapshot.get("firstStatusCodeName"),
            "lastStatusCode": snapshot.get("lastStatusCode"),
            "lastStatusCodeName": snapshot.get("lastStatusCodeName"),
        }


class HistoryRenderCacheThread(threading.Thread):
    def __init__(self, dataStore: MatrixDataStore, targetFps: int = DEFAULT_RENDER_TARGET_FPS, appendLimit: int = 256):
        super().__init__(name="SensorArrayHistoryRenderCache", daemon=True)
        self.dataStore = dataStore
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self.targetFps = int(targetFps)
        self.appendLimit = max(16, int(appendLimit))
        self.stream = "FAST_BINARY"
        self.selectedCell = "S1D1"
        self.xAxis = "timeSeconds"
        self.unitMode = "auto"
        self.historyWindow = "last_30s"
        self.lastN: int | None = 1000
        self.customXMin: float | None = None
        self.customXMax: float | None = None
        self.followLatest = True
        self.maxPoints = 1200
        self.showMarkers = False
        self.latestHistorySnapshot: dict | None = None
        self._currentKey: HistoryKey | None = None
        self._lastSeq: int | None = None
        self._cacheRevision = 0
        self.renderSkipped = 0
        self._fpsSamples: list[float] = []
        self._visiblePointCount = 0
        self._renderedPointCount = 0
        self._downsampled = False
        self._resolvedUnit = "uV"

    def stop(self) -> None:
        self._stop_event.set()

    def updateControls(self, **kwargs: Any) -> None:
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self, key) and value is not None:
                    setattr(self, key, value)
            if self.selectedCell not in CELL_NAMES:
                self.selectedCell = "S1D1"
            self.targetFps = max(1, int(self.targetFps))
            self.maxPoints = max(100, int(self.maxPoints))
            self._currentKey = None
            self._lastSeq = None
            self._cacheRevision += 1
            self.latestHistorySnapshot = self._build_snapshot_locked(force_reset=True)

    def getLatest(self) -> dict | None:
        with self._lock:
            return dict(self.latestHistorySnapshot) if self.latestHistorySnapshot is not None else None

    def getStats(self) -> dict:
        with self._lock:
            return {
                "targetFps": self.targetFps,
                "actualFps": _sample_fps(self._fpsSamples),
                "renderSkipped": self.renderSkipped,
                "visiblePointCount": self._visiblePointCount,
                "renderedPointCount": self._renderedPointCount,
                "downsampled": self._downsampled,
                "unit": self._resolvedUnit,
                "selectedCell": self.selectedCell,
            }

    def reset(self) -> None:
        with self._lock:
            self._currentKey = None
            self._lastSeq = None
            self._cacheRevision += 1
            self.latestHistorySnapshot = self._build_snapshot_locked(force_reset=True)

    def run(self) -> None:
        while not self._stop_event.is_set():
            start = time.monotonic()
            with self._lock:
                snapshot = self._build_snapshot_locked(force_reset=False)
                if snapshot is not None:
                    self.latestHistorySnapshot = snapshot
                    self._fpsSamples.append(start)
                    _prune_samples(self._fpsSamples, start)
            period = 1.0 / max(1, self.targetFps)
            self._stop_event.wait(max(0.001, period - (time.monotonic() - start)))

    def _build_snapshot_locked(self, force_reset: bool) -> dict | None:
        x, y_uv, meta = self.dataStore.getCellHistoryArrays(
            self.stream,
            self.selectedCell,
            xAxis=self.xAxis,
            windowMode=self.historyWindow,
            lastN=self.lastN,
            customMin=self.customXMin,
            customMax=self.customXMax,
            sinceSeq=None if force_reset or self._currentKey is None else self._lastSeq,
        )
        resolved_unit = _resolve_unit(y_uv, self.unitMode)
        key = HistoryKey(
            self.stream,
            self.selectedCell,
            meta.get("xColumn") or self.xAxis,
            resolved_unit,
            self.historyWindow,
            self.customXMin,
            self.customXMax,
            bool(self.followLatest),
            int(self.maxPoints),
            bool(self.showMarkers),
        )
        key_changed = key != self._currentKey
        if force_reset or key_changed or self._lastSeq is None:
            x_full, y_full, meta_full = self.dataStore.getCellHistoryArrays(
                self.stream,
                self.selectedCell,
                xAxis=self.xAxis,
                windowMode=self.historyWindow,
                lastN=self.lastN,
                customMin=self.customXMin,
                customMax=self.customXMax,
            )
            resolved_unit = _resolve_unit(y_full, self.unitMode)
            key = HistoryKey(
                self.stream,
                self.selectedCell,
                meta_full.get("xColumn") or self.xAxis,
                resolved_unit,
                self.historyWindow,
                self.customXMin,
                self.customXMax,
                bool(self.followLatest),
                int(self.maxPoints),
                bool(self.showMarkers),
            )
            selected, downsampled = MatrixDataStore.downsampleHistoryArrays(x_full, y_full, self.maxPoints)
            self._currentKey = key
            self._lastSeq = _last_seq(meta_full.get("seq"))
            self._visiblePointCount = int(len(x_full))
            self._renderedPointCount = int(len(selected))
            self._downsampled = bool(downsampled)
            self._resolvedUnit = resolved_unit
            self._cacheRevision += 1
            y_selected_uv = y_full[selected]
            return {
                "kind": "history",
                "reset": True,
                "cacheRevision": self._cacheRevision,
                "key": key.as_string(),
                "stream": self.stream,
                "selectedCell": self.selectedCell,
                "title": f"History of {self.selectedCell} / {self.stream}",
                "xAxis": key.xAxis,
                "unit": resolved_unit,
                "x": _json_array(x_full[selected]),
                "y": _json_array(_convert_uv(y_selected_uv, resolved_unit)),
                "customData": _build_history_customdata(meta_full, selected, self.selectedCell, self.stream, y_selected_uv),
                "lastSeq": self._lastSeq,
                "maxPoints": self.maxPoints,
                "showMarkers": bool(self.showMarkers),
                "revision": int(meta_full.get("revision") or 0),
                "followLatest": bool(self.followLatest),
                "xRange": _history_x_range(x_full, key.xAxis, self.historyWindow, self.lastN, self.customXMin, self.customXMax),
                "visiblePointCount": self._visiblePointCount,
                "renderedPointCount": self._renderedPointCount,
                "downsampled": self._downsampled,
            }

        if len(x) == 0:
            return None
        if len(x) > self.appendLimit:
            self.renderSkipped += len(x) - self.appendLimit
            x = x[-self.appendLimit :]
            y_uv = y_uv[-self.appendLimit :]
            meta["seq"] = meta["seq"][-self.appendLimit :]
            meta["timestampUs"] = meta["timestampUs"][-self.appendLimit :]
            meta["unit"] = meta["unit"][-self.appendLimit :]
            meta["rawValue"] = meta["rawValue"][-self.appendLimit :]
        self._lastSeq = _last_seq(meta.get("seq")) or self._lastSeq
        self._renderedPointCount = min(int(self.maxPoints), int(self._renderedPointCount + len(x)))
        self._visiblePointCount = max(int(self._visiblePointCount), int(self._renderedPointCount))
        self._resolvedUnit = key.resolvedUnit
        self._cacheRevision += 1
        return {
            "kind": "history",
            "reset": False,
            "cacheRevision": self._cacheRevision,
            "key": key.as_string(),
            "stream": self.stream,
            "selectedCell": self.selectedCell,
            "x": _json_array(x),
            "y": _json_array(_convert_uv(y_uv, key.resolvedUnit)),
            "customData": _build_history_customdata(meta, np.arange(len(x), dtype=int), self.selectedCell, self.stream, y_uv),
            "lastSeq": self._lastSeq,
            "maxPoints": self.maxPoints,
            "showMarkers": bool(self.showMarkers),
            "revision": int(meta.get("revision") or 0),
            "followLatest": bool(self.followLatest),
            "xRange": _history_x_range(x, key.xAxis, self.historyWindow, self.lastN, self.customXMin, self.customXMax),
            "visiblePointCount": self._visiblePointCount,
            "renderedPointCount": self._renderedPointCount,
            "downsampled": self._downsampled,
        }


class StatusCache:
    def __init__(self):
        self.latest: dict[str, Any] = {}
        self.revision = 0

    def update(self, **sections: Any) -> dict:
        self.revision += 1
        self.latest = {"revision": self.revision, **sections}
        return dict(self.latest)


class DiagnosticsCache(StatusCache):
    pass


def _resolve_display_unit(matrix_uv: np.ndarray, unit_mode: str) -> str:
    requested = CANONICAL_UNITS.get(str(unit_mode or "auto").strip().lower())
    if requested in VOLTAGE_FACTORS_TO_UV:
        return requested
    return _resolve_unit(matrix_uv, "auto")


def _build_heatmap_text(matrix_display: np.ndarray, unit: str) -> list[list[str]]:
    text: list[list[str]] = []
    matrix = np.asarray(matrix_display, dtype=float)
    for source_index in range(MATRIX_SIZE):
        row: list[str] = []
        for detector_index in range(MATRIX_SIZE):
            value = matrix[source_index, detector_index]
            if math.isfinite(float(value)):
                row.append(_format_display_value(float(value), unit))
            else:
                row.append("--")
        text.append(row)
    return text


def _build_heatmap_customdata(
    matrix_uv: np.ndarray,
    matrix_display: np.ndarray,
    display_unit: str,
    seq: Any,
    timestamp_us: Any,
    duration_us: Any,
    status_flags: Any,
    first_status_code: Any,
    first_status_name: Any,
    last_status_code: Any,
    last_status_name: Any,
) -> list[list[list[Any]]]:
    matrix_uv = np.asarray(matrix_uv, dtype=float)
    matrix_display = np.asarray(matrix_display, dtype=float)
    status_text = _status_text(status_flags, first_status_code, first_status_name, last_status_code, last_status_name)
    status_flags_text = _format_hex(status_flags, 8)
    first_status_text = _format_status_code(first_status_code, first_status_name)
    last_status_text = _format_status_code(last_status_code, last_status_name)
    customdata: list[list[list[Any]]] = []
    for source_index in range(MATRIX_SIZE):
        row: list[list[Any]] = []
        for detector_index in range(MATRIX_SIZE):
            cell = f"S{source_index + 1}D{detector_index + 1}"
            raw_uv = float(matrix_uv[source_index, detector_index])
            display_value = float(matrix_display[source_index, detector_index])
            valid = math.isfinite(raw_uv) and math.isfinite(display_value)
            row.append(
                [
                    cell,
                    bool(valid),
                    display_unit,
                    "uV",
                    raw_uv if valid else None,
                    seq if seq is not None else "-",
                    status_text,
                    timestamp_us if timestamp_us is not None else "-",
                    duration_us if duration_us is not None else "-",
                    status_flags_text,
                    first_status_text,
                    last_status_text,
                    _format_display_number(display_value, display_unit) if valid else "--",
                    _format_display_value(display_value, display_unit) if valid else "--",
                ]
            )
        customdata.append(row)
    return customdata


def _build_history_customdata(meta: dict, selected: np.ndarray, cell: str, stream: str, y_uv: np.ndarray) -> list[list[Any]]:
    seq = np.asarray(meta.get("seq", []), dtype=object)
    timestamp_us = np.asarray(meta.get("timestampUs", []), dtype=object)
    units = np.asarray(meta.get("unit", []), dtype=object)
    selected = np.asarray(selected, dtype=int)
    y_uv = np.asarray(y_uv, dtype=float)
    rows: list[list[Any]] = []
    for output_index, source_index in enumerate(selected):
        raw_uv = float(y_uv[output_index]) if output_index < len(y_uv) and math.isfinite(float(y_uv[output_index])) else None
        rows.append(
            [
                cell,
                stream,
                _array_value(seq, source_index),
                _array_value(timestamp_us, source_index),
                "uV",
                raw_uv,
                _array_value(units, source_index) or "uV",
            ]
        )
    return rows


def _resolve_color_range(matrix_display: np.ndarray, color_mode: str, fixed_min: Any, fixed_max: Any) -> tuple[float | None, float | None]:
    matrix = np.asarray(matrix_display, dtype=float)
    finite_values = matrix[np.isfinite(matrix)]
    if str(color_mode or "auto") == "symmetric" and finite_values.size:
        max_abs = float(np.max(np.abs(finite_values)))
        if max_abs > 0:
            return -max_abs, max_abs
    if str(color_mode or "auto") == "fixed":
        zmin = _finite_or_none(fixed_min)
        zmax = _finite_or_none(fixed_max)
        if zmin is not None and zmax is not None and zmin < zmax:
            return zmin, zmax
    return None, None


def _format_display_value(value: float, unit: str) -> str:
    if not math.isfinite(float(value)):
        return "--"
    return f"{_format_display_number(float(value), unit)} {unit}".strip()


def _format_display_number(value: float, unit: str) -> str:
    if not math.isfinite(float(value)):
        return "--"
    abs_value = abs(float(value))
    if abs_value == 0:
        return "0"
    if abs_value >= 10_000:
        return str(int(round(float(value))))
    if abs_value >= 1:
        return _strip_trailing_zeros(f"{float(value):.4g}")
    if abs_value >= 0.001:
        return _strip_trailing_zeros(f"{float(value):.3g}")
    return f"{float(value):.2e}"


def _strip_trailing_zeros(value: str) -> str:
    if "." not in value:
        return value
    return value.rstrip("0").rstrip(".")


def _colorbar_tick_format(unit: str) -> str:
    return ".3~g"


def _status_text(status_flags: Any, first_code: Any, first_name: Any, last_code: Any, last_name: Any) -> str:
    flags_nonzero = _numeric_nonzero(status_flags)
    first_nonzero = _numeric_nonzero(first_code)
    last_nonzero = _numeric_nonzero(last_code)
    if not flags_nonzero and not first_nonzero and not last_nonzero:
        return "OK"
    return f"flags={_format_hex(status_flags, 8)} first={_format_status_code(first_code, first_name)} last={_format_status_code(last_code, last_name)}"


def _format_status_code(code: Any, name: Any = None) -> str:
    if code is None or code == "":
        return "-"
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        return str(code)
    if code_int == 0:
        return "OK"
    return f"{_format_hex(code_int, 4)} {name or '-'}"


def _numeric_nonzero(value: Any) -> bool:
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return bool(value and value != "-")


def _array_value(values: np.ndarray, index: int) -> Any:
    if index < 0 or index >= len(values):
        return "-"
    value = values[index]
    if isinstance(value, (np.integer, np.int64, np.int32)):
        return int(value)
    if isinstance(value, (np.floating, np.float64, np.float32)):
        return float(value) if math.isfinite(float(value)) else "-"
    return value.item() if hasattr(value, "item") else value


def _history_x_range(
    x_values: np.ndarray,
    x_axis: str,
    history_window: str,
    last_n: int | None,
    custom_min: float | None,
    custom_max: float | None,
) -> list[float] | None:
    x = np.asarray(x_values, dtype=float)
    finite_x = x[np.isfinite(x)]
    if not finite_x.size:
        return None
    if history_window == "custom" and custom_min is not None and custom_max is not None and custom_min < custom_max:
        return [float(custom_min), float(custom_max)]
    if history_window == "last_n":
        count = max(1, int(last_n or 1000))
        subset = finite_x[-count:]
        return [float(subset[0]), float(subset[-1])] if subset.size > 1 else None
    seconds = {"last_10s": 10.0, "last_30s": 30.0, "last_60s": 60.0, "last_5min": 300.0}.get(history_window)
    if seconds is None:
        return None
    latest = float(finite_x[-1])
    if x_axis == "timeSeconds":
        return [latest - seconds, latest]
    return [float(finite_x[0]), latest] if finite_x.size > 1 else None


def _finite_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _resolve_unit(values_uv: np.ndarray, unit_mode: str) -> str:
    requested = CANONICAL_UNITS.get(str(unit_mode or "auto").strip().lower(), str(unit_mode or "auto"))
    if requested in VOLTAGE_FACTORS_TO_UV:
        return requested
    finite = np.asarray(values_uv, dtype=float)
    finite = finite[np.isfinite(finite)]
    if not finite.size:
        return "uV"
    max_abs = float(np.max(np.abs(finite)))
    if max_abs >= 1_000_000.0:
        return "V"
    if max_abs >= 1_000.0:
        return "mV"
    return "uV"


def _convert_uv(values_uv: np.ndarray, unit: str) -> np.ndarray:
    factor = VOLTAGE_FACTORS_TO_UV.get(unit, 1.0)
    return np.asarray(values_uv, dtype=float) / factor


def _json_matrix(matrix: np.ndarray) -> list[list[float | None]]:
    return [_json_array(row) for row in np.asarray(matrix, dtype=float)]


def _json_array(values: np.ndarray) -> list[float | None]:
    output: list[float | None] = []
    for value in np.asarray(values, dtype=float):
        output.append(float(value) if math.isfinite(float(value)) else None)
    return output


def _last_seq(values: Any) -> int | None:
    if values is None:
        return None
    array = np.asarray(values, dtype=float)
    finite = array[np.isfinite(array)]
    return int(finite[-1]) if finite.size else None


def _prune_samples(samples: list[float], now: float) -> None:
    cutoff = now - 2.0
    while samples and samples[0] < cutoff:
        del samples[0]


def _sample_fps(samples: list[float]) -> float:
    if len(samples) < 2:
        return 0.0
    return (len(samples) - 1) / max(1e-6, samples[-1] - samples[0])


def _format_hex(value: Any, width: int = 4) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"0x{int(value):0{width}X}"
    except (TypeError, ValueError):
        return str(value)
