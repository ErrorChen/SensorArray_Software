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
        matrix_uv = np.asarray(snapshot.get("matrixUv"), dtype=float)
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
            status_code=snapshot.get("lastStatusCode"),
            status_name=snapshot.get("lastStatusCodeName"),
        )
        return {
            "kind": "heatmap",
            "cacheRevision": self._cacheRevision,
            "stream": snapshot.get("stream") or self.stream,
            "revision": data_revision,
            "seq": snapshot.get("seq"),
            "timestampUs": snapshot.get("timestampUs"),
            "timeSeconds": snapshot.get("timeSeconds"),
            "durationUs": snapshot.get("durationUs"),
            "matrixUv": _json_matrix(matrix_uv),
            "matrixDisplay": _json_matrix(matrix_display),
            "displayUnit": display_unit,
            "text": text,
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
            selected, _downsampled = MatrixDataStore.downsampleHistoryArrays(x_full, y_full, self.maxPoints)
            self._currentKey = key
            self._lastSeq = _last_seq(meta_full.get("seq"))
            self._cacheRevision += 1
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
                "y": _json_array(_convert_uv(y_full[selected], resolved_unit)),
                "lastSeq": self._lastSeq,
                "maxPoints": self.maxPoints,
                "showMarkers": bool(self.showMarkers),
                "revision": int(meta_full.get("revision") or 0),
                "followLatest": bool(self.followLatest),
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
        return {
            "kind": "history",
            "reset": False,
            "cacheRevision": self._cacheRevision,
            "key": key.as_string(),
            "stream": self.stream,
            "selectedCell": self.selectedCell,
            "x": _json_array(x),
            "y": _json_array(_convert_uv(y_uv, key.resolvedUnit)),
            "lastSeq": self._lastSeq,
            "maxPoints": self.maxPoints,
            "showMarkers": bool(self.showMarkers),
            "revision": int(meta.get("revision") or 0),
            "followLatest": bool(self.followLatest),
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
            cell = f"S{source_index + 1}D{detector_index + 1}"
            value = matrix[source_index, detector_index]
            if math.isfinite(float(value)):
                row.append(f"{cell}<br>{_format_display_value(float(value), unit)}")
            else:
                row.append(f"{cell}<br>invalid")
        text.append(row)
    return text


def _build_heatmap_customdata(
    matrix_uv: np.ndarray,
    matrix_display: np.ndarray,
    display_unit: str,
    seq: Any,
    status_code: Any,
    status_name: Any,
) -> list[list[list[Any]]]:
    matrix_uv = np.asarray(matrix_uv, dtype=float)
    matrix_display = np.asarray(matrix_display, dtype=float)
    status_text = _format_hex(status_code, 4)
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
                    "valid" if valid else "invalid",
                    _format_display_number(display_value, display_unit) if valid else "invalid",
                    display_unit,
                    seq if seq is not None else "-",
                    raw_uv if valid else None,
                    status_text,
                    status_name or "-",
                ]
            )
        customdata.append(row)
    return customdata


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
        return "invalid"
    return f"{_format_display_number(float(value), unit)} {unit}".strip()


def _format_display_number(value: float, unit: str) -> str:
    if not math.isfinite(float(value)):
        return "invalid"
    abs_value = abs(float(value))
    if unit == "uV":
        if abs_value < 10:
            return _strip_trailing_zeros(f"{float(value):,.2f}")
        if abs_value < 1_000:
            return _strip_trailing_zeros(f"{float(value):,.1f}")
        if math.isclose(float(value), round(float(value)), rel_tol=0.0, abs_tol=1e-9):
            return f"{int(round(float(value))):,}"
        return _strip_trailing_zeros(f"{float(value):,.1f}")
    if unit == "mV":
        if abs_value >= 100:
            return _strip_trailing_zeros(f"{float(value):,.1f}")
        if abs_value >= 10:
            return _strip_trailing_zeros(f"{float(value):,.2f}")
        return _strip_trailing_zeros(f"{float(value):,.3f}")
    if unit == "V":
        if abs_value >= 10:
            return _strip_trailing_zeros(f"{float(value):,.4f}")
        return _strip_trailing_zeros(f"{float(value):,.6f}")
    return _strip_trailing_zeros(f"{float(value):,.6f}")


def _strip_trailing_zeros(value: str) -> str:
    if "." not in value:
        return value
    return value.rstrip("0").rstrip(".")


def _colorbar_tick_format(unit: str) -> str:
    if unit == "uV":
        return ",.1f"
    if unit == "mV":
        return ",.3f"
    if unit == "V":
        return ",.6f"
    return ",.3f"


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
