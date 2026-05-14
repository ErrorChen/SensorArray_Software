from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import CELL_NAMES, DEFAULT_GUI_TARGET_FPS, MATRIX_SIZE
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
    maxPoints: int
    showMarkers: bool

    def as_string(self) -> str:
        return "|".join(str(value) for value in self.__dict__.values())


class HeatmapRenderCacheThread(threading.Thread):
    def __init__(self, dataStore: MatrixDataStore, targetFps: int = DEFAULT_GUI_TARGET_FPS):
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
            if unitMode:
                self.unitMode = str(unitMode)
            if colorMode:
                self.colorMode = str(colorMode)
            self.fixedMin = fixedMin
            self.fixedMax = fixedMax
            self._cacheRevision += 1
            self.latestHeatmapSnapshot = self._build_snapshot_locked(force=True)

    def getLatest(self) -> dict | None:
        with self._lock:
            return dict(self.latestHeatmapSnapshot) if self.latestHeatmapSnapshot is not None else None

    def getStats(self) -> dict:
        with self._lock:
            return {"targetFps": self.targetFps, "actualFps": _sample_fps(self._fpsSamples), "renderSkipped": self.renderSkipped}

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
        matrix = np.asarray(snapshot.get("matrixUv"), dtype=float)
        unit = _resolve_unit(matrix.ravel(), self.unitMode)
        display_matrix = _convert_uv(matrix, unit)
        zmin, zmax = _resolve_color_range(display_matrix, self.colorMode, self.fixedMin, self.fixedMax)
        finite_min, finite_max = _finite_min_max(display_matrix)
        return {
            "kind": "heatmap",
            "cacheRevision": self._cacheRevision,
            "stream": snapshot.get("stream") or self.stream,
            "revision": data_revision,
            "seq": snapshot.get("seq"),
            "timestampUs": snapshot.get("timestampUs"),
            "timeSeconds": snapshot.get("timeSeconds"),
            "durationUs": snapshot.get("durationUs"),
            "matrixUv": _json_matrix(matrix),
            "matrix": _json_matrix(display_matrix),
            "unit": unit,
            "cellText": _cell_text(display_matrix, unit),
            "cellNames": _cell_names_matrix(),
            "zmin": zmin,
            "zmax": zmax,
            "finiteMin": finite_min,
            "finiteMax": finite_max,
            "colorMode": self.colorMode,
            "validMask": snapshot.get("validMask"),
            "selectedCell": self.selectedCell,
            "statusFlags": snapshot.get("statusFlags"),
            "firstStatusCode": snapshot.get("firstStatusCode"),
            "lastStatusCode": snapshot.get("lastStatusCode"),
        }


class HistoryRenderCacheThread(threading.Thread):
    def __init__(self, dataStore: MatrixDataStore, targetFps: int = DEFAULT_GUI_TARGET_FPS, appendLimit: int = 256):
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
        self.followRevision = 0
        self.maxPoints = 1200
        self.showMarkers = False
        self.latestHistorySnapshot: dict | None = None
        self._currentKey: HistoryKey | None = None
        self._lastSeq: int | None = None
        self._cacheRevision = 0
        self.renderSkipped = 0
        self._fpsSamples: list[float] = []

    def stop(self) -> None:
        self._stop_event.set()

    def updateControls(self, **kwargs: Any) -> None:
        with self._lock:
            old_reset_state = (
                self.stream,
                self.selectedCell,
                self.xAxis,
                self.unitMode,
                self.historyWindow,
                self.lastN,
                self.customXMin,
                self.customXMax,
                self.maxPoints,
                self.showMarkers,
            )
            old_follow_latest = self.followLatest
            old_follow_revision = self.followRevision
            for key, value in kwargs.items():
                if hasattr(self, key) and value is not None:
                    setattr(self, key, value)
            if self.selectedCell not in CELL_NAMES:
                self.selectedCell = "S1D1"
            self.targetFps = max(1, int(self.targetFps))
            self.followRevision = max(0, int(self.followRevision))
            self.maxPoints = max(100, int(self.maxPoints))
            new_reset_state = (
                self.stream,
                self.selectedCell,
                self.xAxis,
                self.unitMode,
                self.historyWindow,
                self.lastN,
                self.customXMin,
                self.customXMax,
                self.maxPoints,
                self.showMarkers,
            )
            follow_revision_changed = old_follow_revision != self.followRevision
            force_reset = (
                self.latestHistorySnapshot is None
                or old_reset_state != new_reset_state
                or (not old_follow_latest and self.followLatest)
                or follow_revision_changed
            )
            if force_reset:
                self._currentKey = None
                self._lastSeq = None
                self._cacheRevision += 1
                self.latestHistorySnapshot = self._build_snapshot_locked(force_reset=True, follow_forced=follow_revision_changed)

    def getLatest(self) -> dict | None:
        with self._lock:
            return dict(self.latestHistorySnapshot) if self.latestHistorySnapshot is not None else None

    def getStats(self) -> dict:
        with self._lock:
            return {"targetFps": self.targetFps, "actualFps": _sample_fps(self._fpsSamples), "renderSkipped": self.renderSkipped}

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

    def _build_snapshot_locked(self, force_reset: bool, follow_forced: bool = False) -> dict | None:
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
                int(self.maxPoints),
                bool(self.showMarkers),
            )
            selected, _downsampled = MatrixDataStore.downsampleHistoryArrays(x_full, y_full, self.maxPoints)
            self._currentKey = key
            self._lastSeq = _last_seq(meta_full.get("seq"))
            self._cacheRevision += 1
            rendered_x = x_full[selected]
            follow_range = _resolve_follow_range(
                x_full,
                key.xAxis,
                self.historyWindow,
                self.lastN,
                self.customXMin,
                self.customXMax,
                follow_forced,
            )
            earliest_x, latest_x = _finite_bounds(x_full)
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
                "historyWindow": self.historyWindow,
                "showMarkers": bool(self.showMarkers),
                "x": _json_array(rendered_x),
                "y": _json_array(_convert_uv(y_full[selected], resolved_unit)),
                "lastSeq": self._lastSeq,
                "maxPoints": self.maxPoints,
                "revision": int(meta_full.get("revision") or 0),
                "followLatest": bool(self.followLatest),
                "followRevision": int(self.followRevision),
                "followForced": bool(follow_forced),
                "earliestX": earliest_x,
                "latestX": latest_x,
                "followRangeStart": follow_range[0] if follow_range is not None else None,
                "followRangeEnd": follow_range[1] if follow_range is not None else None,
            }

        if len(x) == 0:
            return None
        if len(x) > self.appendLimit:
            self.renderSkipped += len(x) - self.appendLimit
            x = x[-self.appendLimit :]
            y_uv = y_uv[-self.appendLimit :]
            meta["seq"] = meta["seq"][-self.appendLimit :]
        self._lastSeq = _last_seq(meta.get("seq")) or self._lastSeq
        self._cacheRevision += 1
        range_x = x
        if self.followLatest and _needs_full_range_source(key.xAxis, self.historyWindow):
            range_x, _range_y, _range_meta = self.dataStore.getCellHistoryArrays(
                self.stream,
                self.selectedCell,
                xAxis=self.xAxis,
                windowMode=self.historyWindow,
                lastN=self.lastN,
                customMin=self.customXMin,
                customMax=self.customXMax,
            )
        follow_range = _resolve_follow_range(
            range_x,
            key.xAxis,
            self.historyWindow,
            self.lastN,
            self.customXMin,
            self.customXMax,
            follow_forced=False,
        )
        earliest_x, latest_x = _finite_bounds(range_x)
        return {
            "kind": "history",
            "reset": False,
            "cacheRevision": self._cacheRevision,
            "key": key.as_string(),
            "stream": self.stream,
            "selectedCell": self.selectedCell,
            "title": f"History of {self.selectedCell} / {self.stream}",
            "xAxis": key.xAxis,
            "unit": key.resolvedUnit,
            "historyWindow": self.historyWindow,
            "showMarkers": bool(self.showMarkers),
            "x": _json_array(x),
            "y": _json_array(_convert_uv(y_uv, key.resolvedUnit)),
            "lastSeq": self._lastSeq,
            "maxPoints": self.maxPoints,
            "revision": int(meta.get("revision") or 0),
            "followLatest": bool(self.followLatest),
            "followRevision": int(self.followRevision),
            "followForced": False,
            "earliestX": earliest_x,
            "latestX": latest_x,
            "followRangeStart": follow_range[0] if follow_range is not None else None,
            "followRangeEnd": follow_range[1] if follow_range is not None else None,
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


def _resolve_unit(values_uv: np.ndarray, unit_mode: str) -> str:
    requested = _canonical_unit(unit_mode)
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


def _canonical_unit(unit: Any) -> str:
    text = str(unit or "").strip().replace("\u00b5", "u").replace("\u03bc", "u").lower()
    return CANONICAL_UNITS.get(text, str(unit or ""))


def _resolve_color_range(matrix: np.ndarray, color_mode: str, fixed_min: Any, fixed_max: Any) -> tuple[float | None, float | None]:
    values = np.asarray(matrix, dtype=float)
    finite = values[np.isfinite(values)]
    mode = str(color_mode or "auto").lower()
    if mode == "fixed":
        zmin = _safe_float(fixed_min)
        zmax = _safe_float(fixed_max)
        if zmin is not None and zmax is not None and zmin < zmax:
            return zmin, zmax
    if not finite.size:
        return None, None
    if mode == "symmetric":
        max_abs = float(np.max(np.abs(finite)))
        if max_abs > 0:
            return -max_abs, max_abs
        return -1e-9, 1e-9
    vmin = float(np.min(finite))
    vmax = float(np.max(finite))
    if math.isclose(vmin, vmax, rel_tol=0.0, abs_tol=1e-12):
        margin = max(abs(vmin) * 0.05, 1e-9)
    else:
        margin = (vmax - vmin) * 0.05
    return vmin - margin, vmax + margin


def _finite_min_max(values: np.ndarray) -> tuple[float | None, float | None]:
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    if not finite.size:
        return None, None
    return float(np.min(finite)), float(np.max(finite))


def _finite_bounds(values: np.ndarray) -> tuple[float | None, float | None]:
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    if not finite.size:
        return None, None
    return float(finite[0]), float(finite[-1])


def _resolve_follow_range(
    x_values: np.ndarray,
    x_axis: str,
    history_window: str,
    last_n: int | None,
    custom_min: float | None,
    custom_max: float | None,
    follow_forced: bool = False,
) -> tuple[float, float] | None:
    finite = np.asarray(x_values, dtype=float)
    finite = finite[np.isfinite(finite)]
    if not finite.size:
        return None

    window = str(history_window or "all")
    latest = float(finite[-1])
    earliest = float(finite[0])
    if window == "all":
        return None
    if window == "custom" and not follow_forced:
        if custom_min is not None and custom_max is not None and custom_min < custom_max:
            return float(custom_min), float(custom_max)
        return None
    if window == "last_n":
        count = max(1, int(last_n or 1000))
        if (x_axis or "timeSeconds") == "seq":
            return latest - float(count), latest
        subset = finite[-count:]
        return (float(subset[0]), float(subset[-1])) if subset.size else None

    seconds = _window_seconds(window)
    if seconds is None:
        return (earliest, latest) if follow_forced and latest > earliest else None
    axis = x_axis or "timeSeconds"
    if axis == "timeSeconds":
        return latest - seconds, latest
    if axis == "timestampUs":
        return latest - (seconds * 1_000_000.0), latest
    return earliest, latest


def _window_seconds(history_window: str) -> float | None:
    return {
        "last_10s": 10.0,
        "last_30s": 30.0,
        "last_60s": 60.0,
        "last_5min": 300.0,
    }.get(history_window)


def _needs_full_range_source(x_axis: str, history_window: str) -> bool:
    return (x_axis or "timeSeconds") == "seq" and _window_seconds(str(history_window or "")) is not None


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _cell_names_matrix() -> list[list[str]]:
    return [[f"S{row + 1}D{col + 1}" for col in range(MATRIX_SIZE)] for row in range(MATRIX_SIZE)]


def _cell_text(matrix: np.ndarray, unit: str) -> list[list[str]]:
    values = np.asarray(matrix, dtype=float)
    names = _cell_names_matrix()
    return [[f"{names[row][col]}<br>{_format_value(values[row, col], unit)}" for col in range(MATRIX_SIZE)] for row in range(MATRIX_SIZE)]


def _format_value(value: Any, unit: str) -> str:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(parsed):
        return "-"
    if math.isclose(parsed, round(parsed), rel_tol=0.0, abs_tol=1e-9):
        text = f"{int(round(parsed)):,}"
    else:
        text = f"{parsed:,.3g}"
    return f"{text} {unit}".strip()


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
