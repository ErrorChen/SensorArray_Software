from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, ctx, dcc, html, no_update

from .config import (
    CELL_NAMES,
    DEFAULT_BAUD,
    DEFAULT_DIAGNOSTICS_INTERVAL_MS,
    DEFAULT_GUI_TARGET_FPS,
    DEFAULT_INGEST_INTERVAL_MS,
    DEFAULT_RENDER_INTERVAL_MS,
    DEFAULT_SERIAL_READ_SIZE,
    DEFAULT_STATUS_INTERVAL_MS,
    DETECTOR_LABELS,
    DISPLAY_DOWNSAMPLE_TARGET_POINTS,
    MATRIX_SIZE,
    SOURCE_LABELS,
)
from .connection_manager import ConnectionManager
from .data_store import CsvFrameWriter, MatrixDataStore
from .protocol_parser import SensorArrayStreamParser
from .render_cache import HeatmapRenderCacheThread, HistoryRenderCacheThread

LOGGER = logging.getLogger(__name__)
DEFAULT_SELECTED_CELL = "S1D1"
DEFAULT_FRAME_TYPES = ["FAST_BINARY", "MATV", "MATV_RAW", "MATV_GAIN", "MATV_ERR"]
VOLTAGE_FACTORS_TO_UV = {"uv": 1.0, "mv": 1_000.0, "v": 1_000_000.0}
CANONICAL_VOLTAGE_UNITS = {"uv": "uV", "mv": "mV", "v": "V"}


class RuntimeState:
    def __init__(self):
        self._lock = threading.Lock()
        self.csvRowsWritten = 0
        self.lastCsvError = ""
        self.totalParsedBytes = 0
        self.totalParsedChunks = 0
        self.totalParsedBinaryFrames = 0
        self.totalParsedTextFrames = 0
        self.totalDisplayedFrames = 0
        self.latestSeq: int | None = None
        self.seqGap = 0
        self._lastSeqByType: dict[str, int] = {}
        self._lastDisplayedKey: tuple[str, int] | None = None
        self._rateSamples: deque[dict[str, float]] = deque()
        self._renderSamples: deque[dict[str, float]] = deque()

    def recordCsvWrite(self) -> None:
        with self._lock:
            self.csvRowsWritten += 1

    def recordCsvError(self, message: str) -> None:
        with self._lock:
            self.lastCsvError = message

    def recordParseBatch(self, byte_count: int, chunk_count: int, results: list) -> None:
        now = time.monotonic()
        binary_frames = 0
        text_frames = 0
        with self._lock:
            self.totalParsedBytes += int(byte_count)
            self.totalParsedChunks += int(chunk_count)
            for result in results:
                frame = getattr(result, "frame", None)
                if frame is None:
                    continue
                if frame.frameType == "FAST_BINARY":
                    binary_frames += 1
                else:
                    text_frames += 1
                if frame.seq is not None:
                    previous = self._lastSeqByType.get(frame.frameType)
                    if previous is not None and int(frame.seq) > previous + 1:
                        self.seqGap += int(frame.seq) - previous - 1
                    self._lastSeqByType[frame.frameType] = int(frame.seq)
                    if frame.frameType == "FAST_BINARY":
                        self.latestSeq = int(frame.seq)
            self.totalParsedBinaryFrames += binary_frames
            self.totalParsedTextFrames += text_frames
            self._rateSamples.append(
                {
                    "time": now,
                    "bytes": float(byte_count),
                    "binary": float(binary_frames),
                    "text": float(text_frames),
                    "display": 0.0,
                }
            )
            self._prune_rate_samples_locked(now)

    def recordDisplayFrame(self, meta: dict, paused: bool = False) -> None:
        if paused or meta.get("seq") is None:
            return
        key = (str(meta.get("frameType") or ""), int(meta["seq"]))
        now = time.monotonic()
        with self._lock:
            if key == self._lastDisplayedKey:
                return
            self._lastDisplayedKey = key
            self.totalDisplayedFrames += 1
            self._rateSamples.append({"time": now, "bytes": 0.0, "binary": 0.0, "text": 0.0, "display": 1.0})
            self._prune_rate_samples_locked(now)

    def recordRenderCompletion(self, rendered_frame: bool) -> None:
        now = time.monotonic()
        with self._lock:
            self._renderSamples.append({"time": now, "tick": 1.0, "render": 1.0 if rendered_frame else 0.0})
            self._prune_render_samples_locked(now)

    def getStats(self) -> dict:
        with self._lock:
            now = time.monotonic()
            self._prune_rate_samples_locked(now)
            duration = max(1e-6, now - self._rateSamples[0]["time"]) if self._rateSamples else 1.0
            bytes_in_window = sum(sample["bytes"] for sample in self._rateSamples)
            binary_in_window = sum(sample["binary"] for sample in self._rateSamples)
            text_in_window = sum(sample["text"] for sample in self._rateSamples)
            display_in_window = sum(sample["display"] for sample in self._rateSamples)
            render_duration = max(1e-6, now - self._renderSamples[0]["time"]) if self._renderSamples else 1.0
            render_ticks = sum(sample["tick"] for sample in self._renderSamples)
            rendered_frames = sum(sample["render"] for sample in self._renderSamples)
            rendered_frame_fps = rendered_frames / render_duration
            return {
                "csvRowsWritten": self.csvRowsWritten,
                "lastCsvError": self.lastCsvError,
                "totalParsedBytes": self.totalParsedBytes,
                "totalParsedChunks": self.totalParsedChunks,
                "totalParsedBinaryFrames": self.totalParsedBinaryFrames,
                "totalParsedTextFrames": self.totalParsedTextFrames,
                "totalDisplayedFrames": self.totalDisplayedFrames,
                "bytesPerSec": bytes_in_window / duration,
                "parsedBinaryFps": binary_in_window / duration,
                "parsedTextFps": text_in_window / duration,
                "guiDisplayedFps": rendered_frame_fps,
                "renderTickFps": render_ticks / render_duration,
                "renderedFrameFps": rendered_frame_fps,
                "legacyDisplayFps": display_in_window / duration,
                "latestSeq": self.latestSeq,
                "seqGap": self.seqGap,
            }

    def _prune_rate_samples_locked(self, now: float) -> None:
        cutoff = now - 5.0
        while self._rateSamples and self._rateSamples[0]["time"] < cutoff:
            self._rateSamples.popleft()

    def _prune_render_samples_locked(self, now: float) -> None:
        cutoff = now - 2.0
        while self._renderSamples and self._renderSamples[0]["time"] < cutoff:
            self._renderSamples.popleft()


class InputProcessorThread(threading.Thread):
    def __init__(
        self,
        input_queue: "queue.Queue[bytes]",
        parser: SensorArrayStreamParser,
        data_store: MatrixDataStore,
        csv_writer: CsvFrameWriter | None,
        runtime_state: RuntimeState,
        max_chunks_per_batch: int,
    ):
        super().__init__(name="SensorArrayInputProcessor", daemon=True)
        self.input_queue = input_queue
        self.parser = parser
        self.data_store = data_store
        self.csv_writer = csv_writer
        self.runtime_state = runtime_state
        self.max_chunks_per_batch = max(1, int(max_chunks_per_batch))
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                first_chunk = self.input_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            chunks = [first_chunk]
            byte_count = len(first_chunk)
            while len(chunks) < self.max_chunks_per_batch:
                try:
                    chunk = self.input_queue.get_nowait()
                except queue.Empty:
                    break
                chunks.append(chunk)
                byte_count += len(chunk)

            results = []
            for chunk in chunks:
                results.extend(self.parser.feedBytes(chunk))
            _store_parse_results(results, self.data_store, self.csv_writer, self.runtime_state)
            self.runtime_state.recordParseBatch(byte_count, len(chunks), results)


def createDashApp(
    inputQueue: "queue.Queue[bytes]",
    parser: SensorArrayStreamParser,
    dataStore: MatrixDataStore,
    connectionManager: ConnectionManager | None = None,
    csvWriter: CsvFrameWriter | None = None,
    maxChunksPerTick: int = 200,
    maxParseResultsPerTick: int = 5000,
    **_legacy_kwargs: Any,
) -> Dash:
    runtime_state = RuntimeState()
    manager = connectionManager or ConnectionManager(inputQueue)
    app = Dash(__name__, title="SensorArray Matrix Viewer")
    input_processor = InputProcessorThread(
        inputQueue,
        parser,
        dataStore,
        csvWriter,
        runtime_state,
        maxChunksPerTick,
    )
    input_processor.start()
    app._sensorarray_input_processor = input_processor
    heatmap_cache = HeatmapRenderCacheThread(dataStore, targetFps=DEFAULT_GUI_TARGET_FPS)
    history_cache = HistoryRenderCacheThread(dataStore, targetFps=DEFAULT_GUI_TARGET_FPS)
    heatmap_cache.start()
    history_cache.start()
    app._sensorarray_heatmap_cache = heatmap_cache
    app._sensorarray_history_cache = history_cache
    app._sensorarray_render_caches = (heatmap_cache, history_cache)

    app.layout = _build_layout()

    app.clientside_callback(
        "function(heatmapSnapshot, historySnapshot, current) {"
        " if (!window.SensorArrayLive || !window.SensorArrayLive.applySnapshots) {"
        "   return current || {};"
        " }"
        " return window.SensorArrayLive.applySnapshots(heatmapSnapshot, historySnapshot, current || {});"
        "}",
        Output("frontend-fps-store", "data"),
        Input("heatmap-snapshot-store", "data"),
        Input("history-snapshot-store", "data"),
        State("frontend-fps-store", "data"),
    )

    @app.callback(Output("render-interval", "interval"), Input("interval-ms", "value"))
    def update_render_interval(interval_ms: Any) -> int:
        try:
            return max(16, min(10_000, int(interval_ms)))
        except (TypeError, ValueError):
            return DEFAULT_RENDER_INTERVAL_MS

    @app.callback(
        Output("paused-store", "data"),
        Output("pause-button", "children"),
        Input("pause-button", "n_clicks"),
        State("paused-store", "data"),
    )
    def toggle_pause(n_clicks: int | None, paused: bool) -> tuple[bool, str]:
        if not n_clicks:
            current_paused = bool(paused)
            return current_paused, "Resume" if current_paused else "Pause"
        new_paused = not bool(paused)
        return new_paused, "Resume" if new_paused else "Pause"

    @app.callback(Output("cell-dropdown", "value"), Input("heatmap", "clickData"), State("cell-dropdown", "value"))
    def select_cell_from_heatmap(click_data: dict | None, current_cell: str | None) -> str:
        if not click_data:
            return current_cell or DEFAULT_SELECTED_CELL
        cell_name = _cell_name_from_click_data(click_data)
        return cell_name if cell_name in CELL_NAMES else (current_cell or DEFAULT_SELECTED_CELL)

    @app.callback(
        Output("com-port-dropdown", "options"),
        Output("com-port-dropdown", "value"),
        Output("port-refresh-status", "children"),
        Input("refresh-ports-button", "n_clicks"),
        State("com-port-dropdown", "value"),
    )
    def refresh_ports(_n_clicks: int | None, current_value: str | None) -> tuple[list[dict], str | None, str]:
        ports = manager.listPorts()
        options = [{"label": port["label"], "value": port["value"]} for port in ports]
        values = {option["value"] for option in options}
        next_value = current_value if current_value in values else (options[0]["value"] if options else None)
        status = "pyserial is not installed." if not ports and manager.getStatus().get("dependencyMissing") else f"{len(options)} port(s) found."
        return options, next_value, status

    @app.callback(
        Output("connection-action-status", "children"),
        Input("connect-button", "n_clicks"),
        Input("disconnect-button", "n_clicks"),
        Input("reconnect-button", "n_clicks"),
        Input("start-replay-button", "n_clicks"),
        State("input-mode", "value"),
        State("com-port-dropdown", "value"),
        State("baud-input", "value"),
        State("read-size-input", "value"),
        State("auto-reconnect", "value"),
        State("replay-file-input", "value"),
        State("replay-speed-input", "value"),
        prevent_initial_call=True,
    )
    def handle_connection_action(
        _connect: int | None,
        _disconnect: int | None,
        _reconnect: int | None,
        _start_replay: int | None,
        input_mode: str,
        port: str | None,
        baud: Any,
        read_size: Any,
        auto_reconnect: list[str] | None,
        replay_file: str | None,
        replay_speed: Any,
    ) -> str:
        try:
            triggered = ctx.triggered_id
            if triggered == "disconnect-button":
                manager.disconnect()
                return "Disconnected."
            if triggered == "reconnect-button":
                parser.reset()
                _clear_input_queue(inputQueue)
                manager.reconnect()
                return "Reconnect requested."
            if triggered == "start-replay-button":
                parser.reset()
                _clear_input_queue(inputQueue)
                manager.startReplay(
                    str(replay_file or ""),
                    float(replay_speed or 1.0),
                    int(read_size or DEFAULT_SERIAL_READ_SIZE),
                )
                return "Replay started."
            parser.reset()
            _clear_input_queue(inputQueue)
            manager.connectSerial(
                str(port or ""),
                int(baud or DEFAULT_BAUD),
                "enabled" in (auto_reconnect or []),
                int(read_size or DEFAULT_SERIAL_READ_SIZE),
            )
            return f"Connecting to {port}."
        except Exception as exc:
            return f"Connection error: {exc}"

    @app.callback(Output("clear-status", "children"), Input("clear-button", "n_clicks"), prevent_initial_call=True)
    def clear_history(n_clicks: int | None) -> str:
        if not n_clicks:
            return no_update
        dataStore.clear()
        heatmap_cache.reset()
        history_cache.reset()
        return f"History cleared at {_clock_text()}."

    @app.callback(Output("save-status", "children"), Input("save-button", "n_clicks"), State("frame-type-dropdown", "value"), prevent_initial_call=True)
    def save_snapshot(n_clicks: int | None, frame_type: str | None) -> str:
        if not n_clicks:
            return no_update
        selected_type = frame_type or "FAST_BINARY"
        snapshot = dataStore.toWideDataFrame(selected_type)
        if snapshot.empty:
            return "No data to save."
        export_dir = Path.cwd() / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        safe_type = selected_type.replace("/", "_")
        output_path = export_dir / f"matrix_snapshot_{safe_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        snapshot.to_csv(output_path, index=False)
        return f"Saved {len(snapshot)} rows to {output_path}"

    @app.callback(
        Output("history-window", "options"),
        Output("history-window", "value"),
        Output("color-mode", "options"),
        Output("color-mode", "value"),
        Input("advanced-details", "open"),
        State("history-window", "value"),
        State("color-mode", "value"),
    )
    def update_operator_options(advanced_open: bool, history_value: str | None, color_value: str | None) -> tuple[list[dict], str, list[dict], str]:
        history_options = _history_window_options(include_advanced=bool(advanced_open))
        color_options = _color_mode_options(include_fixed=bool(advanced_open))
        valid_history = {option["value"] for option in history_options}
        valid_color = {option["value"] for option in color_options}
        next_history = history_value if history_value in valid_history else "last_30s"
        next_color = color_value if color_value in valid_color else "auto"
        return history_options, next_history, color_options, next_color

    @app.callback(Output("frame-type-dropdown", "options"), Input("status-interval", "n_intervals"))
    def update_frame_type_options(_n_intervals: int) -> list[dict]:
        return _frame_type_options(dataStore.getAvailableFrameTypes())

    @app.callback(
        Output("ingest-stats-store", "data"),
        Input("ingest-interval", "n_intervals"),
        State("ingest-stats-store", "data"),
    )
    def publish_ingest_revision(_n_intervals: int, previous: dict | None) -> dict:
        available = dataStore.getAvailableFrameTypes()
        payload = {
            "availableFrameTypes": available,
            "revisions": {frame_type: dataStore.getLatestRevision(frame_type) for frame_type in available},
            "latestSeq": {frame_type: dataStore.getLatestSeq(frame_type) for frame_type in available},
            "framesTotal": dataStore.getStats().get("framesTotal", 0),
        }
        if previous == payload:
            return no_update
        return payload

    @app.callback(
        Output("history-follow-store", "data"),
        Input("history-graph", "relayoutData"),
        Input("follow-latest-button", "n_clicks"),
        Input("auto-follow", "value"),
        State("history-follow-store", "data"),
        prevent_initial_call=True,
    )
    def update_history_follow(relayout_data: dict | None, _follow_clicks: int | None, auto_follow_values: list[str] | None, current: bool) -> bool:
        triggered = ctx.triggered_id
        if triggered == "follow-latest-button":
            return True
        if triggered == "auto-follow":
            return "enabled" in (auto_follow_values or [])
        if triggered == "history-graph" and _relayout_has_manual_x_range(relayout_data):
            return False
        return bool(current)

    @app.callback(
        Output("render-control-store", "data"),
        Input("cell-dropdown", "value"),
        Input("frame-type-dropdown", "value"),
        Input("unit-mode", "value"),
        Input("color-mode", "value"),
        Input("fixed-min", "value"),
        Input("fixed-max", "value"),
        Input("x-axis", "value"),
        Input("history-window", "value"),
        Input("last-n-points", "value"),
        Input("custom-x-min", "value"),
        Input("custom-x-max", "value"),
        Input("history-follow-store", "data"),
        Input("render-mode", "value"),
        Input("history-max-points", "value"),
        Input("show-markers", "value"),
    )
    def update_render_controls(
        selected_cell: str | None,
        frame_type: str | None,
        unit_mode: str | None,
        color_mode: str | None,
        fixed_min: Any,
        fixed_max: Any,
        x_axis: str | None,
        history_window: str | None,
        last_n: Any,
        custom_min: Any,
        custom_max: Any,
        follow_latest: bool,
        render_mode: str | None,
        history_max_points: Any,
        show_markers: list[str] | None,
    ) -> dict:
        selected_cell = selected_cell if selected_cell in CELL_NAMES else DEFAULT_SELECTED_CELL
        selected_type = frame_type or "FAST_BINARY"
        target_fps = _target_fps(render_mode)
        max_points = _history_points_limit(history_max_points, render_mode)
        heatmap_cache.updateControls(
            stream=selected_type,
            selectedCell=selected_cell,
            targetFps=target_fps,
            unitMode=unit_mode or "auto",
            colorMode=color_mode or "auto",
            fixedMin=_safe_float(fixed_min),
            fixedMax=_safe_float(fixed_max),
        )
        history_cache.updateControls(
            stream=selected_type,
            selectedCell=selected_cell,
            xAxis=x_axis or "timeSeconds",
            unitMode=unit_mode or "auto",
            historyWindow=history_window or "last_30s",
            lastN=_safe_int(last_n) or 1000,
            customXMin=_safe_float(custom_min),
            customXMax=_safe_float(custom_max),
            followLatest=bool(follow_latest),
            targetFps=target_fps,
            maxPoints=max_points,
            showMarkers="enabled" in (show_markers or []),
        )
        return {
            "selectedCell": selected_cell,
            "stream": selected_type,
            "targetFps": target_fps,
            "maxPoints": max_points,
            "unitMode": unit_mode or "auto",
            "colorMode": color_mode or "auto",
            "fixedMin": _safe_float(fixed_min),
            "fixedMax": _safe_float(fixed_max),
            "xAxis": x_axis or "timeSeconds",
            "historyWindow": history_window or "last_30s",
            "followLatest": bool(follow_latest),
            "showMarkers": "enabled" in (show_markers or []),
        }

    @app.callback(
        Output("heatmap-snapshot-store", "data"),
        Input("render-interval", "n_intervals"),
        Input("render-control-store", "data"),
        Input("paused-store", "data"),
        State("heatmap-snapshot-store", "data"),
    )
    def publish_heatmap_snapshot(_n_intervals: int, _control_state: dict | None, paused: bool, previous: dict | None) -> Any:
        if paused:
            runtime_state.recordRenderCompletion(False)
            return no_update
        snapshot = heatmap_cache.getLatest()
        if not snapshot:
            return no_update
        if previous and previous.get("cacheRevision") == snapshot.get("cacheRevision"):
            runtime_state.recordRenderCompletion(False)
            return no_update
        runtime_state.recordRenderCompletion(True)
        return snapshot

    @app.callback(
        Output("history-snapshot-store", "data"),
        Output("history-stats", "children"),
        Input("render-interval", "n_intervals"),
        Input("render-control-store", "data"),
        Input("paused-store", "data"),
        State("history-snapshot-store", "data"),
    )
    def publish_history_snapshot(_n_intervals: int, control_state: dict | None, paused: bool, previous: dict | None) -> tuple[Any, Any]:
        if paused:
            return no_update, no_update
        snapshot = history_cache.getLatest()
        if not snapshot:
            return no_update, no_update
        if previous and previous.get("cacheRevision") == snapshot.get("cacheRevision"):
            return no_update, no_update
        stats = runtime_state.getStats()
        text = (
            f"stream: {snapshot.get('stream')} | cell: {snapshot.get('selectedCell')} | "
            f"x: {snapshot.get('xAxis', control_state.get('xAxis') if control_state else 'timeSeconds')} | "
            f"points: {len(snapshot.get('x') or [])} | reset: {'yes' if snapshot.get('reset') else 'no'} | "
            f"input fps: {stats.get('parsedBinaryFps', 0.0):.1f}"
        )
        return snapshot, text

    @app.callback(
        Output("status-bar", "children"),
        Output("warning-state-store", "data"),
        Input("status-interval", "n_intervals"),
        Input("paused-store", "data"),
        Input("cell-dropdown", "value"),
        Input("unit-mode", "value"),
        Input("frame-type-dropdown", "value"),
        State("frontend-fps-store", "data"),
        State("warning-state-store", "data"),
    )
    def update_compact_status(
        _n_intervals: int,
        paused: bool,
        selected_cell: str | None,
        unit_mode: str,
        frame_type: str | None,
        frontend_stats: dict | None,
        previous_warning_state: dict | None,
    ) -> tuple[list, dict]:
        selected_cell = selected_cell if selected_cell in CELL_NAMES else DEFAULT_SELECTED_CELL
        selected_type = frame_type or "FAST_BINARY"
        display_type = dataStore.resolveFrameType(selected_type)
        latest_matrix_raw, latest_meta, _revision = dataStore.getLatestMatrixAndMeta(selected_type)
        display_matrix, display_unit = _convert_matrix_for_display(latest_matrix_raw, latest_meta.get("unit") or "", unit_mode)
        parser_stats = parser.getStats()
        connection_status = manager.getStatus()
        runtime_stats = runtime_state.getStats()
        runtime_stats.update(_render_runtime_stats(heatmap_cache, history_cache, frontend_stats or {}))
        runtime_stats["deviceSummary"] = dataStore.getLatestDeviceStatus().get("summary", {})
        warning_badge, next_warning_state = _warning_badge(parser_stats, connection_status, runtime_stats, latest_meta, previous_warning_state or {})
        status = _build_compact_status_bar(
            meta=latest_meta,
            matrix=display_matrix,
            selected_cell=selected_cell,
            parser_stats=parser_stats,
            connection_status=connection_status,
            runtime_stats=runtime_stats,
            paused=bool(paused),
            display_unit=display_unit,
            display_type=display_type,
            warning_badge=warning_badge,
        )
        return status, next_warning_state

    @app.callback(
        Output("key-metrics-panel", "children"),
        Input("status-interval", "n_intervals"),
        State("frontend-fps-store", "data"),
        State("frame-type-dropdown", "value"),
        State("cell-dropdown", "value"),
    )
    def update_key_metrics(
        _n_intervals: int,
        frontend_stats: dict | None,
        frame_type: str | None,
        selected_cell: str | None,
    ) -> list:
        selected_type = frame_type or "FAST_BINARY"
        selected_cell = selected_cell if selected_cell in CELL_NAMES else DEFAULT_SELECTED_CELL
        latest_meta = dataStore.getLatestFrameMeta(selected_type)
        parser_stats = parser.getStats()
        connection_status = manager.getStatus()
        runtime_stats = runtime_state.getStats()
        runtime_stats.update(_render_runtime_stats(heatmap_cache, history_cache, frontend_stats or {}))
        return _build_key_metrics_panel(
            selected_type=dataStore.resolveFrameType(selected_type),
            selected_cell=selected_cell,
            latest_meta=latest_meta,
            parser_stats=parser_stats,
            connection_status=connection_status,
            runtime_stats=runtime_stats,
            latest_device_status=dataStore.getLatestDeviceStatus(),
            queue_depth=_safe_qsize(inputQueue),
        )

    @app.callback(
        Output("diagnostics-panel", "children"),
        Output("connection-status-panel", "children"),
        Output("device-status-panel", "children"),
        Input("diagnostics-interval", "n_intervals"),
        Input("advanced-details", "open"),
        State("frame-type-dropdown", "value"),
    )
    def update_diagnostics(_n_intervals: int, advanced_open: bool, frame_type: str | None) -> tuple[Any, Any, Any]:
        if not advanced_open:
            return no_update, no_update, no_update
        selected_type = frame_type or "FAST_BINARY"
        latest_meta = dataStore.getLatestFrameMeta(selected_type)
        parser_stats = parser.getStats()
        connection_status = manager.getStatus()
        runtime_stats = runtime_state.getStats()
        queue_depth = _safe_qsize(inputQueue)
        return (
            _build_parser_diagnostics(parser_stats, runtime_stats, queue_depth),
            _build_connection_panel(connection_status, queue_depth),
            _build_device_panel(latest_meta, parser_stats, dataStore.getLatestDeviceStatus(), dataStore.getRecentDeviceEvents(20)),
        )

    return app


def _build_layout() -> html.Div:
    return html.Div(
        [
            dcc.Store(id="paused-store", data=False),
            dcc.Store(id="ingest-stats-store", data={}),
            dcc.Store(id="render-control-store", data={"targetFps": DEFAULT_GUI_TARGET_FPS}),
            dcc.Store(id="heatmap-snapshot-store", data={}),
            dcc.Store(id="history-snapshot-store", data={}),
            dcc.Store(id="frontend-fps-store", data={}),
            dcc.Store(id="heatmap-render-state", data={}),
            dcc.Store(id="history-render-state", data={}),
            dcc.Store(id="history-follow-store", data=True),
            dcc.Store(id="warning-state-store", data={}),
            dcc.Interval(id="ingest-interval", interval=DEFAULT_INGEST_INTERVAL_MS, n_intervals=0),
            dcc.Interval(id="render-interval", interval=DEFAULT_RENDER_INTERVAL_MS, n_intervals=0),
            dcc.Interval(id="status-interval", interval=DEFAULT_STATUS_INTERVAL_MS, n_intervals=0),
            dcc.Interval(id="diagnostics-interval", interval=DEFAULT_DIAGNOSTICS_INTERVAL_MS, n_intervals=0),
            html.Header(
                [
                    html.H1("SensorArray Matrix Viewer", className="app-title"),
                    html.Div(id="status-bar", className="top-status"),
                ],
                className="top-bar",
            ),
            html.Section(
                [
                    html.Div(
                        [
                            _control("COM Port", dcc.Dropdown(id="com-port-dropdown", options=[], placeholder="Refresh ports")),
                            html.Button("Refresh Ports", id="refresh-ports-button", n_clicks=0, className="button secondary"),
                            html.Button("Connect", id="connect-button", n_clicks=0, className="button primary"),
                            html.Button("Disconnect", id="disconnect-button", n_clicks=0, className="button secondary"),
                            html.Button("Reconnect", id="reconnect-button", n_clicks=0, className="button quiet"),
                            html.Button("Pause", id="pause-button", n_clicks=0, className="button secondary"),
                            html.Button("Clear", id="clear-button", n_clicks=0, className="button secondary"),
                            html.Button("Save Snapshot CSV", id="save-button", n_clicks=0, className="button secondary"),
                        ],
                        className="toolbar connection-toolbar",
                    ),
                    html.Div(
                        [
                            _control("Data stream", dcc.Dropdown(id="frame-type-dropdown", value="FAST_BINARY", clearable=False, options=_frame_type_options(DEFAULT_FRAME_TYPES))),
                            _control("Cell", dcc.Dropdown(id="cell-dropdown", value=DEFAULT_SELECTED_CELL, clearable=False, options=[{"label": cell, "value": cell} for cell in CELL_NAMES])),
                            _control("History window", dcc.Dropdown(id="history-window", value="last_30s", clearable=False, options=_history_window_options(False))),
                            _control("Display unit", dcc.Dropdown(id="unit-mode", value="auto", clearable=False, options=[
                                {"label": "Auto", "value": "auto"},
                                {"label": "uV", "value": "uV"},
                                {"label": "mV", "value": "mV"},
                                {"label": "V", "value": "V"},
                            ])),
                            _control("Color scale", dcc.Dropdown(id="color-mode", value="auto", clearable=False, options=_color_mode_options(False))),
                            _control("Render mode", dcc.Dropdown(id="render-mode", value="Normal", clearable=False, options=[
                                {"label": "Normal", "value": "Normal"},
                                {"label": "Performance", "value": "Performance"},
                                {"label": "Quality", "value": "Quality"},
                            ])),
                            html.Button("Follow Latest", id="follow-latest-button", n_clicks=0, className="button secondary"),
                        ],
                        className="toolbar display-toolbar",
                    ),
                    html.Div(
                        [
                            html.Div(id="port-refresh-status", className="message"),
                            html.Div(id="connection-action-status", className="message"),
                            html.Div(id="save-status", className="message"),
                            html.Div(id="clear-status", className="message"),
                            html.Div(id="history-stats", className="message"),
                        ],
                        className="message-row",
                    ),
                ],
                className="operator-panel",
            ),
            html.Section(html.Div(id="key-metrics-panel", className="key-metrics"), className="key-metrics-shell"),
            html.Main(
                [
                    html.Section(
                        dcc.Graph(
                            id="heatmap",
                            config={"displayModeBar": False, "responsive": True, "doubleClick": "reset"},
                            className="graph graph-matrix",
                        ),
                        className="panel matrix-panel",
                    ),
                    html.Section(
                        dcc.Graph(
                            id="history-graph",
                            config={
                                "displayModeBar": True,
                                "modeBarButtonsToRemove": ["select2d", "lasso2d"],
                                "scrollZoom": True,
                                "responsive": True,
                                "doubleClick": "reset",
                            },
                            className="graph graph-history",
                        ),
                        className="panel history-panel",
                    ),
                ],
                className="main-grid",
            ),
            html.Details(
                [
                    html.Summary("Advanced / Diagnostics", className="advanced-summary"),
                    html.Div(
                        [
                            html.Section(
                                [
                                    html.H2("Connection", className="section-title"),
                                    html.Div(
                                        [
                                            _control("Input Mode", dcc.Dropdown(id="input-mode", value="serial", clearable=False, options=[
                                                {"label": "Serial", "value": "serial"},
                                                {"label": "Replay File", "value": "replay"},
                                                {"label": "Disconnected", "value": "disconnected"},
                                            ])),
                                            _control("Baudrate", dcc.Input(id="baud-input", type="number", value=DEFAULT_BAUD, min=1, step=1, className="input")),
                                            _control("Read size", dcc.Input(id="read-size-input", type="number", value=DEFAULT_SERIAL_READ_SIZE, min=4096, step=1024, className="input")),
                                            _control("Auto reconnect", dcc.Checklist(id="auto-reconnect", value=[], options=[{"label": "Enabled", "value": "enabled"}], className="checklist")),
                                            _control("Replay file", dcc.Input(id="replay-file-input", type="text", debounce=True, className="input")),
                                            _control("Replay speed", dcc.Input(id="replay-speed-input", type="number", value=1.0, min=0.01, step=0.5, className="input")),
                                            html.Button("Start Replay", id="start-replay-button", n_clicks=0, className="button secondary"),
                                        ],
                                        className="advanced-grid",
                                    ),
                                ],
                                className="advanced-section",
                            ),
                            html.Section(
                                [
                                    html.H2("Display", className="section-title"),
                                    html.Div(
                                        [
                                            _control("X axis", dcc.Dropdown(id="x-axis", value="timeSeconds", clearable=False, options=[
                                                {"label": "timeSeconds", "value": "timeSeconds"},
                                                {"label": "timestampUs", "value": "timestampUs"},
                                                {"label": "seq", "value": "seq"},
                                            ])),
                                            _control("Last N points", dcc.Input(id="last-n-points", type="number", value=1000, min=1, step=100, className="input")),
                                            _control("Custom x min", dcc.Input(id="custom-x-min", type="number", debounce=True, className="input")),
                                            _control("Custom x max", dcc.Input(id="custom-x-max", type="number", debounce=True, className="input")),
                                            _control("Fixed min (display unit)", dcc.Input(id="fixed-min", type="number", debounce=True, className="input")),
                                            _control("Fixed max (display unit)", dcc.Input(id="fixed-max", type="number", debounce=True, className="input")),
                                            _control("GUI render interval ms", dcc.Input(id="interval-ms", type="number", value=DEFAULT_RENDER_INTERVAL_MS, min=16, step=1, debounce=True, className="input")),
                                            _control("History max points", dcc.Dropdown(id="history-max-points", value="Auto", clearable=False, options=[
                                                {"label": "Auto", "value": "Auto"},
                                                {"label": "1000", "value": "1000"},
                                                {"label": "1200", "value": "1200"},
                                                {"label": "5000", "value": "5000"},
                                            ])),
                                            _control("Auto follow latest", dcc.Checklist(id="auto-follow", value=["enabled"], options=[{"label": "Enabled", "value": "enabled"}], className="checklist")),
                                            _control("History markers", dcc.Checklist(id="show-markers", value=[], options=[{"label": "Show markers", "value": "enabled"}], className="checklist")),
                                        ],
                                        className="advanced-grid",
                                    ),
                                ],
                                className="advanced-section",
                            ),
                            html.Section([html.H2("Parser Stats", className="section-title"), html.Div(id="diagnostics-panel")], className="advanced-section"),
                            html.Section([html.H2("Connection Internals", className="section-title"), html.Div(id="connection-status-panel")], className="advanced-section"),
                            html.Section([html.H2("Device Status", className="section-title"), html.Div(id="device-status-panel")], className="advanced-section"),
                        ],
                        className="advanced-content",
                    ),
                ],
                id="advanced-details",
                open=False,
                className="advanced-panel",
            ),
        ],
        className="page",
    )


def _drain_queue(
    input_queue: "queue.Queue[bytes]",
    parser: SensorArrayStreamParser,
    data_store: MatrixDataStore,
    csv_writer: CsvFrameWriter | None,
    runtime_state: RuntimeState,
    max_chunks: int,
    max_parse_results: int,
) -> None:
    chunks = 0
    parse_results = 0
    byte_count = 0
    results_batch = []
    while chunks < max_chunks and parse_results < max_parse_results:
        try:
            chunk = input_queue.get_nowait()
        except queue.Empty:
            break
        chunks += 1
        byte_count += len(chunk)
        results = parser.feedBytes(chunk)
        parse_results += len(results)
        results_batch.extend(results)
    _store_parse_results(results_batch, data_store, csv_writer, runtime_state)
    if chunks:
        runtime_state.recordParseBatch(byte_count, chunks, results_batch)


def _store_parse_results(
    results: list,
    data_store: MatrixDataStore,
    csv_writer: CsvFrameWriter | None,
    runtime_state: RuntimeState,
) -> None:
    for result in results:
        if result.frame is not None:
            data_store.addFrame(result.frame)
            if csv_writer is not None:
                try:
                    csv_writer.appendFrame(result.frame)
                    runtime_state.recordCsvWrite()
                except Exception as exc:
                    runtime_state.recordCsvError(str(exc))
                    LOGGER.warning("CSV append failed: %s", exc)
        if result.status is not None:
            data_store.addDeviceStatus(result.status)
        if result.event is not None:
            data_store.addDeviceEvent(result.event)


def _build_heatmap_figure(
    matrix: np.ndarray,
    meta: dict,
    selected_cell: str,
    color_mode: str,
    fixed_min: Any,
    fixed_max: Any,
    display_unit: str,
    requested_frame_type: str,
) -> go.Figure:
    unit = display_unit or meta.get("unit") or ""
    if meta.get("seq") is None and not np.isfinite(matrix).any():
        return _empty_figure(f"No data for selected stream: {requested_frame_type}", "8x8 Matrix")

    cell_names = np.array([[f"S{row + 1}D{col + 1}" for col in range(MATRIX_SIZE)] for row in range(MATRIX_SIZE)])
    validity = np.where(np.isfinite(matrix), "valid", "invalid")
    custom_data = np.dstack([cell_names, validity])
    text = np.array([[_format_value(matrix[row, col], unit) if np.isfinite(matrix[row, col]) else "--" for col in range(MATRIX_SIZE)] for row in range(MATRIX_SIZE)])

    heatmap_kwargs: dict[str, Any] = {}
    zmin, zmax = _resolve_color_range(matrix, color_mode, fixed_min, fixed_max)
    if zmin is not None and zmax is not None:
        heatmap_kwargs["zmin"] = zmin
        heatmap_kwargs["zmax"] = zmax

    fig = go.Figure(
        data=[
            go.Heatmap(
                z=matrix,
                x=DETECTOR_LABELS,
                y=SOURCE_LABELS,
                text=text,
                customdata=custom_data,
                texttemplate="%{text}",
                hovertemplate=(
                    "cell=%{customdata[0]}<br>"
                    "value=%{z:,.3g}<br>"
                    f"unit={unit or '-'}<br>"
                    f"seq={_dash_if_none(meta.get('seq'))}<br>"
                    f"status={_format_hex(meta.get('lastStatusCode'), 4)}<extra></extra>"
                ),
                colorscale="RdYlBu_r",
                colorbar={
                    "title": {"text": unit or "value"},
                    "tickformat": _colorbar_tick_format(unit),
                    "exponentformat": "none",
                    "separatethousands": True,
                },
                showscale=True,
                **heatmap_kwargs,
            )
        ]
    )

    selected_source, selected_detector = _split_cell_name(selected_cell)
    if selected_source is not None and selected_detector is not None:
        fig.add_trace(
            go.Scatter(
                x=[f"D{selected_detector}"],
                y=[f"S{selected_source}"],
                mode="markers",
                marker={"symbol": "square-open", "size": 62, "line": {"color": "#111827", "width": 3}},
                customdata=[selected_cell],
                hovertemplate="cell=%{customdata}<extra></extra>",
                showlegend=False,
            )
        )

    fig.update_layout(**_base_figure_layout("8x8 Matrix"), clickmode="event+select", uirevision=f"heatmap:{requested_frame_type}")
    fig.update_xaxes(side="top", constrain="domain")
    fig.update_yaxes(autorange="reversed", scaleanchor="x", scaleratio=1)
    return fig


def _build_history_figure(
    cell_name: str,
    frame_type: str,
    history_raw,
    history_rendered,
    x_axis: str,
    unit_mode: str,
    auto_follow: bool,
    window_mode: str,
    last_n: int | None,
    custom_min: float | None,
    custom_max: float | None,
) -> go.Figure:
    if history_raw.empty:
        return _empty_figure(
            f"No visible points for selected window. stream={frame_type}, cell={cell_name}, window={window_mode}",
            f"History of {cell_name} / {frame_type}",
        )

    x_column = x_axis if x_axis in history_rendered.columns else "timeSeconds"
    display_values, unit_label, mixed_units, converted = _convert_history_for_display(history_rendered, unit_mode)
    revision = _history_view_revision(
        frame_type=frame_type,
        cell_name=cell_name,
        x_axis=x_column,
        unit_mode=unit_mode,
        unit_label=unit_label,
        auto_follow=auto_follow,
        window_mode=window_mode,
        last_n=last_n,
        custom_min=custom_min,
        custom_max=custom_max,
    )
    fig = go.Figure()
    custom_data = history_rendered[["seq", "timestampUs", "unit", "value"]].to_numpy()
    value_hover = (
        f"value=%{{y:,.6g}} {unit_label}<br>source=%{{customdata[3]:,.6g}} %{{customdata[2]}}<br>"
        if converted
        else "value=%{y:,.6g}<br>unit=%{customdata[2]}<br>"
    )
    fig.add_trace(
        go.Scattergl(
            x=history_rendered[x_column],
            y=display_values,
            mode="lines",
            name=f"{cell_name} / {frame_type}",
            line={"color": "#0f766e", "width": 2},
            marker={"size": 4},
            customdata=custom_data,
            hovertemplate=(
                f"cell={cell_name}<br>"
                f"stream={frame_type}<br>"
                f"{x_column}=%{{x}}<br>"
                f"{value_hover}"
                "seq=%{customdata[0]}<br>"
                "timestamp_us=%{customdata[1]}<extra></extra>"
            ),
        )
    )
    title = f"History of {cell_name} / {frame_type}" + (" (Mixed units)" if mixed_units else "")
    fig.update_layout(
        **_base_figure_layout(title),
        xaxis_title=x_column,
        yaxis_title=f"value ({unit_label})" if unit_label != "value" else "value",
        uirevision=revision["layout"],
    )
    fig.update_xaxes(showgrid=True, gridcolor="#e5e7eb", uirevision=revision["x"])
    fig.update_yaxes(showgrid=True, gridcolor="#e5e7eb", autorange=True, uirevision=revision["y"])

    if auto_follow:
        range_value = _resolve_follow_range(history_raw, x_column, window_mode, last_n, custom_min, custom_max)
        if range_value is None:
            fig.update_xaxes(autorange=True)
        else:
            fig.update_xaxes(range=range_value)
    return fig


def _build_history_arrays_figure(
    cell_name: str,
    frame_type: str,
    x_values: np.ndarray,
    y_values: np.ndarray,
    raw_values: np.ndarray,
    unit_values: np.ndarray,
    seq_values: np.ndarray,
    timestamp_values: np.ndarray,
    x_column: str,
    unit_label: str,
    mixed_units: bool,
    converted: bool,
    auto_follow: bool,
    window_mode: str,
    last_n: int | None,
    custom_min: float | None,
    custom_max: float | None,
    show_markers: bool,
    raw_x_values: np.ndarray | None = None,
) -> go.Figure:
    if len(x_values) == 0:
        return _empty_figure(
            f"No visible points for selected window. stream={frame_type}, cell={cell_name}, window={window_mode}",
            f"History of {cell_name} / {frame_type}",
        )

    mode = "lines+markers" if show_markers else "lines"
    fig = go.Figure()
    custom_data = np.column_stack([seq_values, timestamp_values, unit_values, raw_values]).tolist()
    value_hover = (
        f"value=%{{y:,.6g}} {unit_label}<br>source=%{{customdata[3]:,.6g}} %{{customdata[2]}}<br>"
        if converted
        else "value=%{y:,.6g}<br>unit=%{customdata[2]}<br>"
    )
    fig.add_trace(
        go.Scattergl(
            x=x_values,
            y=y_values,
            mode=mode,
            name=f"{cell_name} / {frame_type}",
            line={"color": "#0f766e", "width": 2},
            marker={"size": 4},
            customdata=custom_data,
            hovertemplate=(
                f"cell={cell_name}<br>"
                f"stream={frame_type}<br>"
                f"{x_column}=%{{x}}<br>"
                f"{value_hover}"
                "seq=%{customdata[0]}<br>"
                "timestamp_us=%{customdata[1]}<extra></extra>"
            ),
        )
    )
    revision = _history_view_revision(
        frame_type=frame_type,
        cell_name=cell_name,
        x_axis=x_column,
        unit_mode=unit_label,
        unit_label=unit_label,
        auto_follow=auto_follow,
        window_mode=window_mode,
        last_n=last_n,
        custom_min=custom_min,
        custom_max=custom_max,
    )
    title = f"History of {cell_name} / {frame_type}" + (" (Mixed units)" if mixed_units else "")
    fig.update_layout(
        **_base_figure_layout(title),
        xaxis_title=x_column,
        yaxis_title=f"value ({unit_label})" if unit_label != "value" else "value",
        uirevision=revision["layout"],
    )
    fig.update_xaxes(showgrid=True, gridcolor="#e5e7eb", uirevision=revision["x"])
    fig.update_yaxes(showgrid=True, gridcolor="#e5e7eb", autorange=True, uirevision=revision["y"])

    if auto_follow:
        range_value = _resolve_follow_range_arrays(
            raw_x_values if raw_x_values is not None else x_values,
            x_column,
            window_mode,
            last_n,
            custom_min,
            custom_max,
        )
        if range_value is None:
            fig.update_xaxes(autorange=True)
        else:
            fig.update_xaxes(range=range_value)
    return fig


def _history_view_revision(
    frame_type: str,
    cell_name: str,
    x_axis: str,
    unit_mode: str,
    unit_label: str,
    auto_follow: bool,
    window_mode: str,
    last_n: int | None,
    custom_min: float | None,
    custom_max: float | None,
) -> dict[str, str]:
    x_window_key = window_mode or "all"
    if window_mode == "last_n":
        x_window_key = f"last_n:{last_n or 1000}"
    elif window_mode == "custom":
        x_window_key = f"custom:{custom_min}:{custom_max}"

    return {
        "layout": "history",
        "x": f"history:x:{frame_type}:{x_axis}:{x_window_key}",
        "y": f"history:y:{frame_type}:{cell_name}:{unit_mode}:{unit_label}",
    }


def _resolve_follow_range(history, x_column: str, window_mode: str, last_n: int | None, custom_min: float | None, custom_max: float | None) -> list[float] | None:
    if history.empty:
        return None
    if window_mode == "all":
        return None
    if window_mode == "custom":
        if custom_min is not None and custom_max is not None and custom_min < custom_max:
            return [float(custom_min), float(custom_max)]
        return None
    if window_mode == "last_n":
        subset = history.tail(max(1, int(last_n or 1000)))
        return [float(subset[x_column].iloc[0]), float(subset[x_column].iloc[-1])] if x_column in subset.columns else None

    seconds = {"last_10s": 10.0, "last_30s": 30.0, "last_60s": 60.0, "last_5min": 300.0}.get(window_mode)
    if seconds is None:
        return None
    if x_column == "timeSeconds":
        latest = float(history[x_column].iloc[-1])
        return [latest - seconds, latest]
    return [float(history[x_column].iloc[0]), float(history[x_column].iloc[-1])]


def _resolve_follow_range_arrays(x_values: np.ndarray, x_column: str, window_mode: str, last_n: int | None, custom_min: float | None, custom_max: float | None) -> list[float] | None:
    finite_x = x_values[np.isfinite(x_values)]
    if not finite_x.size:
        return None
    if window_mode == "all":
        return None
    if window_mode == "custom":
        if custom_min is not None and custom_max is not None and custom_min < custom_max:
            return [float(custom_min), float(custom_max)]
        return None
    if window_mode == "last_n":
        count = max(1, int(last_n or 1000))
        subset = finite_x[-count:]
        return [float(subset[0]), float(subset[-1])] if subset.size else None

    seconds = {"last_10s": 10.0, "last_30s": 30.0, "last_60s": 60.0, "last_5min": 300.0}.get(window_mode)
    if seconds is None:
        return None
    if x_column == "timeSeconds":
        latest = float(finite_x[-1])
        return [latest - seconds, latest]
    return [float(finite_x[0]), float(finite_x[-1])]


def _convert_history_arrays_for_display(values: np.ndarray, units: np.ndarray, unit_mode: str) -> tuple[np.ndarray, str, bool, bool]:
    raw_values = np.asarray(values, dtype=float)
    unit_list = [str(unit) for unit in np.unique(units.astype(str)) if str(unit) != ""]
    unit_keys = [_normalize_unit(unit) for unit in unit_list]
    all_voltage = bool(unit_keys) and all(unit_key in VOLTAGE_FACTORS_TO_UV for unit_key in unit_keys)
    if not all_voltage:
        mixed_units = len(unit_list) > 1
        return raw_values, "mixed units" if mixed_units else (unit_list[0] if unit_list else "value"), mixed_units, False
    if unit_mode == "source" and len(set(unit_keys)) > 1:
        return raw_values, "mixed units", True, False
    source_unit_keys = np.array([_normalize_unit(unit) for unit in units], dtype=object)
    values_uv = np.array([value * VOLTAGE_FACTORS_TO_UV.get(unit_key, np.nan) for value, unit_key in zip(raw_values, source_unit_keys)], dtype=float)
    source_key = unit_keys[0]
    target_key = _resolve_target_voltage_unit(values_uv, unit_mode, source_key)
    return values_uv / VOLTAGE_FACTORS_TO_UV[target_key], CANONICAL_VOLTAGE_UNITS[target_key], False, True


def _history_customdata(history_meta: dict, mask: np.ndarray) -> list:
    return np.column_stack(
        [
            history_meta["seq"][mask],
            history_meta["timestampUs"][mask],
            history_meta["unit"][mask],
            history_meta["rawValue"][mask],
        ]
    ).tolist()


def _history_max_points(window_mode: str, runtime_stats: dict, frame_type: str) -> int:
    seconds = {"last_10s": 10.0, "last_30s": 30.0, "last_60s": 60.0, "last_5min": 300.0}.get(window_mode, 30.0)
    if frame_type == "FAST_BINARY":
        fps = float(runtime_stats.get("parsedBinaryFps") or 20.0)
    else:
        fps = max(float(runtime_stats.get("parsedTextFps") or 0.0), 20.0)
    return min(2000, max(200, int(seconds * fps * 1.5)))


def _history_stats_text(
    display_type: str,
    requested_type: str,
    selected_cell: str,
    history_meta: dict,
    visible_points: int,
    rendered_points: int,
    downsampled: bool,
    auto_follow: bool,
    runtime_stats: dict,
) -> str:
    fallback_text = f" | fallback: {requested_type}->{display_type}" if requested_type == "FAST_BINARY" and display_type != requested_type else ""
    return (
        f"stream: {display_type} (requested {requested_type}){fallback_text} | "
        f"cell: {selected_cell} | "
        f"x: {history_meta.get('xColumn') or 'timeSeconds'} | "
        f"visible points: {visible_points} | "
        f"rendered points: {rendered_points} | "
        f"downsampled: {'yes' if downsampled else 'no'} | "
        f"follow latest: {'yes' if auto_follow else 'no'} | "
        f"input fps: {runtime_stats.get('parsedBinaryFps', 0.0):.1f} | "
        f"gui fps: {runtime_stats.get('renderedFrameFps', 0.0):.1f}"
    )


def _last_finite(values: Any) -> float | None:
    if values is None:
        return None
    array = np.asarray(values, dtype=float)
    finite = array[np.isfinite(array)]
    return float(finite[-1]) if finite.size else None


def _relayout_has_manual_x_range(relayout_data: dict | None) -> bool:
    if not relayout_data:
        return False
    keys = set(relayout_data)
    return bool({"xaxis.range[0]", "xaxis.range[1]", "xaxis.range"} & keys)


def _build_compact_status_bar(
    meta: dict,
    matrix: np.ndarray,
    selected_cell: str,
    parser_stats: dict,
    connection_status: dict,
    runtime_stats: dict,
    paused: bool,
    display_unit: str,
    display_type: str,
    warning_badge: html.Div,
) -> list:
    port = connection_status.get("serialPort") or connection_status.get("port") or "-"
    selected_value = _selected_cell_value(matrix, selected_cell, display_unit)
    status_code = f"{_format_hex(meta.get('lastStatusCode'), 4)} {meta.get('lastStatusCodeName') or '-'}"
    if status_code.startswith("-"):
        status_code = "OK"
    device = runtime_stats.get("deviceSummary") or {}
    device_drop = _first_number(device.get("latestDrop"), meta.get("droppedFrames"), 0)
    device_decimated = _first_number(device.get("latestDecimated"), meta.get("outputDecimatedFrames"), 0)
    output_div = _first_non_empty(device.get("latestOutputDiv"), meta.get("outputDivider"), "-")
    partial = int(device.get("partialAfterFirstByte") or 0)
    pollution = int(parser_stats.get("protocolPollutionCount") or 0)
    return [
        _status_chip("connection", f"{_connection_label(connection_status, paused)} {port}".strip(), "ok" if connection_status.get("serialConnected") or connection_status.get("mode") == "replay" else ""),
        _status_chip("stream", display_type or "-"),
        _status_chip("selected", f"{selected_cell} {selected_value}".strip()),
        _status_chip("seq", _dash_if_none(meta.get("seq"))),
        _status_chip("scanFps", _fmt_rate(device.get("latestScanFps"))),
        _status_chip("outFps", _fmt_rate(device.get("latestOutFps"))),
        _status_chip("parsedFps", f"{runtime_stats.get('parsedBinaryFps', 0.0):.1f}"),
        _status_chip("storedFps", f"{runtime_stats.get('parsedBinaryFps', 0.0):.1f}"),
        _status_chip("GUI heatmap fps", f"{runtime_stats.get('guiHeatmapFps', 0.0):.1f}"),
        _status_chip("GUI history fps", f"{runtime_stats.get('guiHistoryFps', 0.0):.1f}"),
        _status_chip("DEVICE_DROP", device_drop, "error" if int(device_drop or 0) > 0 else ""),
        _status_chip("DEVICE_DECIMATED", device_decimated, "warn" if int(device_decimated or 0) > 0 else ""),
        _status_chip("OUTPUT_DIV", output_div, "warn" if _safe_int(output_div) and int(output_div) > 1 else ""),
        _status_chip("DROPPED_BEFORE_FIRST_BYTE", device.get("droppedBeforeFirstByte", 0), "warn" if int(device.get("droppedBeforeFirstByte") or 0) else ""),
        _status_chip("PARTIAL_AFTER_FIRST_BYTE", partial, "error" if partial > 0 else ""),
        _status_chip("HOST_CRC", parser_stats.get("binaryCrcErrors", 0), "warn" if parser_stats.get("binaryCrcErrors", 0) else ""),
        _status_chip("HOST_RESYNC", parser_stats.get("binaryMagicResyncs", 0), "warn" if parser_stats.get("binaryMagicResyncs", 0) else ""),
        _status_chip("HOST_QUEUE_DROP", connection_status.get("droppedInputChunks", 0), "warn" if connection_status.get("droppedInputChunks", 0) else ""),
        _status_chip("RENDER_SKIPPED", runtime_stats.get("renderSkipped", 0), "info" if runtime_stats.get("renderSkipped", 0) else ""),
        _status_chip("ASCII_AFTER_FAST_BINARY_START", pollution, "error" if pollution else ""),
        _status_chip("status", status_code, "warn" if bool(meta.get("lastStatusCode")) else "ok"),
        warning_badge,
    ]


def _warning_badge(parser_stats: dict, connection_status: dict, runtime_stats: dict, meta: dict, previous: dict) -> tuple[html.Div, dict]:
    current = {
        "crc": int(parser_stats.get("binaryCrcErrors") or 0),
        "resync": int(parser_stats.get("binaryMagicResyncs") or 0),
        "parse": int(parser_stats.get("parseErrors") or 0),
        "seq_gap": int(runtime_stats.get("seqGap") or 0),
        "device_drop": int(meta.get("droppedFrames") or 0),
        "host_drop": int(connection_status.get("droppedInputChunks") or 0),
    }
    deltas = {key: max(0, current[key] - int(previous.get(key, current[key]) or 0)) for key in current}
    labels = []
    if deltas["crc"]:
        labels.append(f"CRC +{deltas['crc']}")
    if deltas["resync"]:
        labels.append(f"RESYNC +{deltas['resync']}")
    if deltas["parse"]:
        labels.append(f"PARSE +{deltas['parse']}")
    drop_delta = deltas["seq_gap"] + deltas["device_drop"] + deltas["host_drop"]
    if drop_delta:
        labels.append(f"DROP +{drop_delta}")
    if labels:
        return _status_chip("warning", " / ".join(labels), "error" if deltas["crc"] or deltas["parse"] else "warn"), current
    has_existing_warning = any(current.values())
    return _status_chip("warning", "OK", "warn" if has_existing_warning else "ok"), current


def _selected_cell_value(matrix: np.ndarray, selected_cell: str, display_unit: str) -> str:
    source, detector = _split_cell_name(selected_cell)
    if source is None or detector is None:
        return ""
    try:
        value = float(matrix[source - 1, detector - 1])
    except Exception:
        return ""
    return _format_value(value, display_unit) if np.isfinite(value) else "invalid"


def _status_chip(label: str, value: Any, tone: str = "") -> html.Div:
    class_name = "status-chip"
    if tone:
        class_name = f"{class_name} {tone}"
    return html.Div([html.Span(label, className="status-label"), html.Strong(str(value), className="status-value")], className=class_name)


def _build_key_metrics_panel(
    selected_type: str,
    selected_cell: str,
    latest_meta: dict,
    parser_stats: dict,
    connection_status: dict,
    runtime_stats: dict,
    latest_device_status: dict,
    queue_depth: int | str,
) -> list:
    device_summary = latest_device_status.get("summary") or {}
    host_drop_chunks = int(connection_status.get("droppedInputChunks") or parser_stats.get("hostQueueDropChunks") or 0)
    host_drop_bytes = int(connection_status.get("droppedInputBytes") or parser_stats.get("hostQueueDropBytes") or 0)
    last_parser_issue = _first_non_empty(parser_stats.get("lastError", ""), parser_stats.get("lastWarning", ""), "-")
    protocol_pollution = int(parser_stats.get("protocolPollutionCount") or device_summary.get("protocolPollutionCount") or 0)
    partial_after_first = int(device_summary.get("partialAfterFirstByte") or 0)
    short_write = int(device_summary.get("latestShortWrite") or 0)
    write_fail = int(device_summary.get("latestWriteFail") or device_summary.get("fullFrameWriteFailCount") or 0)
    q_full = int(device_summary.get("latestQFull") or 0)
    device_drop = _first_number(device_summary.get("latestDrop"), latest_meta.get("droppedFrames"), 0)
    device_decimated = _first_number(device_summary.get("latestDecimated"), latest_meta.get("outputDecimatedFrames"), 0)
    return [
        _metric_card(
            "Input / Throughput",
            [
                ("stream", selected_type or "-"),
                ("selected cell", selected_cell or "-"),
                ("latest seq", _dash_if_none(latest_meta.get("seq") if latest_meta.get("seq") is not None else runtime_stats.get("latestSeq"))),
                ("seq gap", runtime_stats.get("seqGap", parser_stats.get("seqGapTotal", 0)), _metric_is_nonzero(runtime_stats.get("seqGap", parser_stats.get("seqGapTotal", 0)))),
                ("parsed binary fps", runtime_stats.get("parsedBinaryFps", 0.0)),
                ("parsed text fps", runtime_stats.get("parsedTextFps", 0.0)),
                ("bytes/sec", runtime_stats.get("bytesPerSec", 0.0)),
                ("GUI heatmap fps", runtime_stats.get("guiHeatmapFps", 0.0)),
                ("GUI history fps", runtime_stats.get("guiHistoryFps", 0.0)),
                ("rendered frame fps", runtime_stats.get("renderedFrameFps", runtime_stats.get("guiDisplayedFps", 0.0))),
                ("input queue depth", queue_depth, _metric_is_nonzero(queue_depth)),
            ],
        ),
        _metric_card(
            "Host Parser Errors",
            [
                ("HOST_CRC / binaryCrcErrors", parser_stats.get("binaryCrcErrors", 0), _metric_is_nonzero(parser_stats.get("binaryCrcErrors", 0))),
                ("HOST_RESYNC / binaryMagicResyncs", parser_stats.get("binaryMagicResyncs", 0), _metric_is_nonzero(parser_stats.get("binaryMagicResyncs", 0))),
                ("binary CRC errors", parser_stats.get("binaryCrcErrors", 0), _metric_is_nonzero(parser_stats.get("binaryCrcErrors", 0))),
                ("resync count / magic resyncs", parser_stats.get("binaryMagicResyncs", 0), _metric_is_nonzero(parser_stats.get("binaryMagicResyncs", 0))),
                ("parse errors", parser_stats.get("parseErrors", 0), _metric_is_nonzero(parser_stats.get("parseErrors", 0))),
                ("skipped bytes", parser_stats.get("skippedBytes", 0), _metric_is_nonzero(parser_stats.get("skippedBytes", 0))),
                ("skipped lines", parser_stats.get("skippedLines", 0), _metric_is_nonzero(parser_stats.get("skippedLines", 0))),
                ("protocol pollution / ASCII_AFTER_FAST_BINARY_START", protocol_pollution, _metric_is_nonzero(protocol_pollution)),
                ("buffered bytes", parser_stats.get("bufferedBytes", parser_stats.get("bufferBytes", 0)), _metric_is_nonzero(parser_stats.get("bufferedBytes", parser_stats.get("bufferBytes", 0)))),
                ("host dropped input chunks", host_drop_chunks, _metric_is_nonzero(host_drop_chunks)),
                ("host dropped input bytes", host_drop_bytes, _metric_is_nonzero(host_drop_bytes)),
                ("last parser warning/error", last_parser_issue, last_parser_issue != "-"),
            ],
        ),
        _metric_card(
            "Device / Firmware",
            [
                ("scanFps", device_summary.get("latestScanFps")),
                ("outFps", device_summary.get("latestOutFps")),
                ("qUsed", device_summary.get("latestQUsed")),
                ("qFull", q_full, _metric_is_nonzero(q_full)),
                ("drop / DEVICE_DROP", device_drop, _metric_is_nonzero(device_drop)),
                ("decimated / DEVICE_DECIMATED", device_decimated, _metric_is_nonzero(device_decimated)),
                ("outputDiv", _first_non_empty(device_summary.get("latestOutputDiv"), latest_meta.get("outputDivider"), "-"), _metric_is_nonzero(_first_non_empty(device_summary.get("latestOutputDiv"), latest_meta.get("outputDivider"), 0))),
                ("droppedBeforeFirstByte", device_summary.get("droppedBeforeFirstByte", 0), _metric_is_nonzero(device_summary.get("droppedBeforeFirstByte", 0))),
                ("partialAfterFirstByte / PROTOCOL_RISK", partial_after_first, _metric_is_nonzero(partial_after_first)),
                ("shortWrite", short_write, _metric_is_nonzero(short_write)),
                ("writeFail", write_fail, _metric_is_nonzero(write_fail)),
                ("latest statusFlags", _format_hex(latest_meta.get("statusFlags"), 8), _metric_is_nonzero(latest_meta.get("statusFlags"))),
                ("firstStatusCode", _format_status_code(latest_meta.get("firstStatusCode"), latest_meta.get("firstStatusCodeName")), _metric_is_nonzero(latest_meta.get("firstStatusCode"))),
                ("lastStatusCode", _format_status_code(latest_meta.get("lastStatusCode"), latest_meta.get("lastStatusCodeName")), _metric_is_nonzero(latest_meta.get("lastStatusCode"))),
                ("adsDr", _dash_if_none(latest_meta.get("adsDr"))),
            ],
        ),
        _metric_card(
            "Display",
            [
                ("active stream", selected_type or "-"),
                ("selected cell", selected_cell or "-"),
                ("display unit", runtime_stats.get("heatmapDisplayUnit") or "-"),
                ("history unit", runtime_stats.get("historyDisplayUnit") or "-"),
                ("visible history points", runtime_stats.get("visibleHistoryPoints", 0)),
                ("rendered history points", runtime_stats.get("renderedHistoryPoints", 0)),
                ("downsampled", "yes" if runtime_stats.get("historyDownsampled") else "no"),
                ("render tick fps", runtime_stats.get("renderTickFps", 0.0)),
                ("frontend render skipped", runtime_stats.get("frontendRenderSkipped", 0), _metric_is_nonzero(runtime_stats.get("frontendRenderSkipped", 0))),
                ("render skipped", runtime_stats.get("renderSkipped", 0), _metric_is_nonzero(runtime_stats.get("renderSkipped", 0))),
                ("heatmap cache skipped", runtime_stats.get("heatmapRenderSkipped", 0), _metric_is_nonzero(runtime_stats.get("heatmapRenderSkipped", 0))),
                ("history cache skipped", runtime_stats.get("historyRenderSkipped", 0), _metric_is_nonzero(runtime_stats.get("historyRenderSkipped", 0))),
                ("last client error", runtime_stats.get("lastClientError") or "-", bool(runtime_stats.get("lastClientError"))),
            ],
        ),
    ]


def _metric_card(title: str, rows: list[tuple]) -> html.Div:
    return html.Div(
        [
            html.H2(title, className="metric-card-title"),
            html.Div([_metric_row(*row) for row in rows], className="metric-rows"),
        ],
        className="metric-card",
    )


def _metric_row(label: str, value: Any, warning: bool = False) -> html.Div:
    class_name = "metric-row warn" if warning else "metric-row"
    return html.Div(
        [html.Span(label, className="metric-label"), html.Strong(_format_metric(label, value), className="metric-value")],
        className=class_name,
    )


def _build_parser_diagnostics(parser_stats: dict, runtime_stats: dict, queue_depth: int | str) -> list:
    rows = [
        ("parsed frames total", parser_stats.get("parsedFramesTotal", 0)),
        ("parsed binary frames", parser_stats.get("parsedBinaryFrames", 0)),
        ("parsed text frames", parser_stats.get("parsedTextFrames", 0)),
        ("parsed binary fps", runtime_stats.get("parsedBinaryFps", 0.0)),
        ("parsed text fps", runtime_stats.get("parsedTextFps", 0.0)),
        ("bytes/sec", runtime_stats.get("bytesPerSec", 0.0)),
        ("binary CRC errors", parser_stats.get("binaryCrcErrors", 0), _metric_is_nonzero(parser_stats.get("binaryCrcErrors", 0))),
        ("resync count / magic resyncs", parser_stats.get("binaryMagicResyncs", 0), _metric_is_nonzero(parser_stats.get("binaryMagicResyncs", 0))),
        ("parse errors", parser_stats.get("parseErrors", 0), _metric_is_nonzero(parser_stats.get("parseErrors", 0))),
        ("skipped bytes", parser_stats.get("skippedBytes", 0), _metric_is_nonzero(parser_stats.get("skippedBytes", 0))),
        ("skipped lines", parser_stats.get("skippedLines", 0), _metric_is_nonzero(parser_stats.get("skippedLines", 0))),
        ("buffered bytes", parser_stats.get("bufferedBytes", parser_stats.get("bufferBytes", 0)), _metric_is_nonzero(parser_stats.get("bufferedBytes", parser_stats.get("bufferBytes", 0)))),
        ("input queue depth", queue_depth, _metric_is_nonzero(queue_depth)),
        ("seq gap count", parser_stats.get("seqGapCount", 0), _metric_is_nonzero(parser_stats.get("seqGapCount", 0))),
        ("seq gap total", runtime_stats.get("seqGap", parser_stats.get("seqGapTotal", 0)), _metric_is_nonzero(runtime_stats.get("seqGap", parser_stats.get("seqGapTotal", 0)))),
        ("host dropped input chunks", parser_stats.get("hostQueueDropChunks", 0), _metric_is_nonzero(parser_stats.get("hostQueueDropChunks", 0))),
        ("host dropped input bytes", parser_stats.get("hostQueueDropBytes", 0), _metric_is_nonzero(parser_stats.get("hostQueueDropBytes", 0))),
        ("parser state", parser_stats.get("state") or "-"),
        ("last status code", _format_status_code(parser_stats.get("lastStatusCode"), parser_stats.get("lastStatusCodeName")), _metric_is_nonzero(parser_stats.get("lastStatusCode"))),
        ("last parser warning/error", _first_non_empty(parser_stats.get("lastError", ""), parser_stats.get("lastWarning", ""), runtime_stats.get("lastCsvError", ""), "-"), _has_non_empty(parser_stats.get("lastError", ""), parser_stats.get("lastWarning", ""), runtime_stats.get("lastCsvError", ""))),
    ]
    return [_kv_table(None, rows)]


def _build_status_bar(meta: dict, parser_stats: dict, connection_status: dict, runtime_stats: dict, paused: bool, queue_depth: int | str, display_unit: str) -> list:
    dropped = int(meta.get("droppedFrames") or 0)
    decimated = int(meta.get("outputDecimatedFrames") or 0)
    host_drop_chunks = int(connection_status.get("droppedInputChunks") or 0)
    return [
        _status_item("Input", _connection_label(connection_status, paused)),
        _status_item("frame_type", meta.get("frameType") or "-"),
        _status_item("seq", _dash_if_none(meta.get("seq"))),
        _status_item("latest_seq", _dash_if_none(runtime_stats.get("latestSeq"))),
        _status_item("seq_gap", runtime_stats.get("seqGap", 0), warning=runtime_stats.get("seqGap", 0) > 0),
        _status_item("timestamp_us", _dash_if_none(meta.get("timestampUs"))),
        _status_item("duration_us", _dash_if_none(meta.get("durationUs"))),
        _status_item("unit", meta.get("unit") or "-"),
        _status_item("display_unit", display_unit or meta.get("unit") or "-"),
        _status_item("status_code", f"{_format_hex(meta.get('lastStatusCode'), 4)} {meta.get('lastStatusCodeName') or '-'}", warning=bool(meta.get("lastStatusCode"))),
        _status_item("device_dropped", dropped, warning=dropped > 0),
        _status_item("device_decimated", decimated, warning=decimated > 0),
        _status_item("host_drop_chunks", host_drop_chunks, warning=host_drop_chunks > 0),
        _status_item("adsDr", _dash_if_none(meta.get("adsDr"))),
        _status_item("outputDiv", _dash_if_none(meta.get("outputDivider"))),
        _status_item("bytes/sec", f"{runtime_stats.get('bytesPerSec', 0.0):.0f}"),
        _status_item("binary_fps", f"{runtime_stats.get('parsedBinaryFps', 0.0):.1f}"),
        _status_item("text_fps", f"{runtime_stats.get('parsedTextFps', 0.0):.1f}"),
        _status_item("gui_fps", f"{runtime_stats.get('guiDisplayedFps', 0.0):.1f}"),
        _status_item("bytes", connection_status.get("bytesReceived", 0)),
        _status_item("chunks", connection_status.get("chunksReceived", 0)),
        _status_item("queue_depth", queue_depth),
        _status_item("parsed_total", parser_stats.get("parsedFramesTotal", 0)),
        _status_item("parsed_binary", parser_stats.get("parsedBinaryFrames", 0)),
        _status_item("parsed_text", parser_stats.get("parsedTextFrames", 0)),
        _status_item("crc_errors", parser_stats.get("binaryCrcErrors", 0), warning=parser_stats.get("binaryCrcErrors", 0) > 0),
        _status_item("resyncs", parser_stats.get("binaryMagicResyncs", 0), warning=parser_stats.get("binaryMagicResyncs", 0) > 0),
        _status_item("parse_errors", parser_stats.get("parseErrors", 0), warning=parser_stats.get("parseErrors", 0) > 0),
        _status_item("csv_rows", runtime_stats.get("csvRowsWritten", 0)),
        _status_item("last_error", _first_non_empty(runtime_stats.get("lastCsvError", ""), connection_status.get("lastError", ""), parser_stats.get("lastError", ""), "-"), warning=bool(_first_non_empty(runtime_stats.get("lastCsvError", ""), connection_status.get("lastError", ""), parser_stats.get("lastError", ""), ""))),
    ]


def _build_connection_panel(status: dict, queue_depth: int | str) -> list:
    rows = [
        ("mode", status.get("mode", "disconnected")),
        ("serial port", status.get("serialPort") or status.get("port") or "-"),
        ("baud", _dash_if_none(status.get("baud"))),
        ("read size", _dash_if_none(status.get("readSize"))),
        ("connected", "yes" if status.get("serialConnected") else "no", status.get("mode") == "serial" and not status.get("serialConnected")),
        ("bytes received", status.get("bytesReceived", 0)),
        ("chunks received", status.get("chunksReceived", 0)),
        ("raw lines received", status.get("rawLinesReceived", 0)),
        ("host dropped input chunks", status.get("droppedInputChunks", 0), _metric_is_nonzero(status.get("droppedInputChunks", 0))),
        ("host dropped input bytes", status.get("droppedInputBytes", 0), _metric_is_nonzero(status.get("droppedInputBytes", 0))),
        ("input queue depth", queue_depth, _metric_is_nonzero(queue_depth)),
        ("last data", _format_wall_time(status.get("lastDataTime"))),
        ("reconnect attempts", status.get("reconnectAttempts", 0)),
        ("auto reconnect", "on" if status.get("autoReconnect") else "off"),
        ("dependency", status.get("dependencyMissing") or "-"),
        ("last serial error", status.get("lastError") or "-", bool(status.get("lastError"))),
    ]
    return [_kv_table(None, rows)]


def _build_device_panel(meta: dict, parser_stats: dict, latest_status: dict, events: list[dict]) -> list:
    event_rows = []
    for event in reversed(events[-20:]):
        fields = event.get("fields") or {}
        event_rows.append(
            html.Tr(
                [
                    html.Td(event.get("eventType") or "-", style=TABLE_CELL_STYLE),
                    html.Td(_format_hex(event.get("code"), 4), style=TABLE_CELL_STYLE),
                    html.Td(event.get("name") or "-", style=TABLE_CELL_STYLE),
                    html.Td(", ".join(f"{key}={value}" for key, value in list(fields.items())[:6]) or event.get("rawLine") or "-", style=TABLE_CELL_STYLE),
                ]
            )
        )
    if not event_rows:
        event_rows.append(html.Tr([html.Td("No recent events", colSpan=4, style=TABLE_CELL_STYLE)]))

    status_fields = latest_status.get("fields") or {}
    status_text = latest_status.get("rawLine") or ", ".join(f"{key}={value}" for key, value in status_fields.items()) or "-"
    summary = latest_status.get("summary") or {}
    field_keys = ["fps", "pps", "scanAvgUs", "scanMaxUs", "drop", "decimated", "qFull", "drdyTimeout", "spiFail", "adsDr", "adsSps", "outputDiv", "status", "code"]
    rows = [
        ("latest seq", _dash_if_none(meta.get("seq"))),
        ("latest statusFlags", _format_hex(meta.get("statusFlags"), 8), _metric_is_nonzero(meta.get("statusFlags"))),
        ("firstStatusCode", _format_status_code(meta.get("firstStatusCode"), meta.get("firstStatusCodeName")), _metric_is_nonzero(meta.get("firstStatusCode"))),
        ("lastStatusCode", _format_status_code(meta.get("lastStatusCode"), meta.get("lastStatusCodeName")), _metric_is_nonzero(meta.get("lastStatusCode"))),
        ("device droppedFrames", _first_number(meta.get("droppedFrames"), summary.get("latestDrop"), 0), _metric_is_nonzero(_first_number(meta.get("droppedFrames"), summary.get("latestDrop"), 0))),
        ("device outputDecimatedFrames", _first_number(meta.get("outputDecimatedFrames"), summary.get("latestDecimated"), 0), _metric_is_nonzero(_first_number(meta.get("outputDecimatedFrames"), summary.get("latestDecimated"), 0))),
        ("adsDr", _dash_if_none(meta.get("adsDr"))),
        ("outputDivider", _dash_if_none(meta.get("outputDivider"))),
        ("last parser status", parser_stats.get("lastStatusCodeName") or "-"),
    ]
    rows.extend((key, status_fields.get(key, "-"), _metric_is_nonzero(status_fields.get(key)) if key in {"drop", "decimated", "qFull", "drdyTimeout", "spiFail", "status", "code"} else False) for key in field_keys)
    return [
        _kv_table(None, rows),
        html.Div(status_text, className="diagnostic-message"),
        html.Table(
            [
                html.Thead(html.Tr([html.Th("Type", style=TABLE_HEADER_STYLE), html.Th("Code", style=TABLE_HEADER_STYLE), html.Th("Name", style=TABLE_HEADER_STYLE), html.Th("Fields", style=TABLE_HEADER_STYLE)])),
                html.Tbody(event_rows),
            ],
            style=TABLE_STYLE,
        ),
    ]


def _kv_table(title: str | None, rows: list[tuple]) -> html.Div:
    children: list[Any] = []
    if title:
        children.append(html.H3(title, className="kv-title"))
    body_rows = []
    for row in rows:
        label = row[0]
        value = row[1] if len(row) > 1 else ""
        warning = bool(row[2]) if len(row) > 2 else False
        body_rows.append(
            html.Tr(
                [
                    html.Td(str(label), className="kv-metric"),
                    html.Td(_format_metric(str(label), value), className="kv-value"),
                ],
                className="warn" if warning else "",
            )
        )
    children.append(
        html.Table(
            [
                html.Thead(html.Tr([html.Th("Metric"), html.Th("Value")])),
                html.Tbody(body_rows),
            ],
            className="kv-table",
        )
    )
    return html.Div(children, className="kv-panel")


def _format_metric(label: str, value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, str):
        return value
    label_key = label.lower()
    if "bytes/sec" in label_key or "b/s" in label_key:
        return _format_bytes_per_sec(value)
    if "fps" in label_key:
        try:
            return f"{float(value):.1f}"
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, (int, np.integer)):
        return f"{int(value):,}"
    if isinstance(value, (float, np.floating)):
        if not math.isfinite(float(value)):
            return "-"
        if math.isclose(float(value), round(float(value)), rel_tol=0.0, abs_tol=1e-9):
            return f"{int(round(float(value))):,}"
        return f"{float(value):,.3f}".rstrip("0").rstrip(".")
    return str(value)


def _format_bytes_per_sec(value: Any) -> str:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return "-"
    if not math.isfinite(rate):
        return "-"
    abs_rate = abs(rate)
    if abs_rate >= 1024 * 1024:
        return f"{rate / (1024 * 1024):.2f} MB/s"
    if abs_rate >= 1024:
        return f"{rate / 1024:.1f} KB/s"
    return f"{rate:.0f} B/s"


def _format_status_code(code: Any, name: Any = None) -> str:
    if code is None or code == "":
        return "-"
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        return str(code)
    decoded = name or ("OK" if code_int == 0 else "-")
    return f"{_format_hex(code_int, 4)} {decoded}"


def _metric_is_nonzero(value: Any) -> bool:
    try:
        return float(value) != 0.0
    except (TypeError, ValueError):
        return bool(value and value != "-")


def _first_number(*values: Any) -> int | float:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0


def _has_non_empty(*values: Any) -> bool:
    return any(value not in (None, "", "-") for value in values)


def _status_item(label: str, value: Any, warning: bool = False) -> html.Div:
    style = dict(STATUS_ITEM_STYLE)
    if warning:
        style.update(WARNING_STATUS_ITEM_STYLE)
    return html.Div([html.Div(label, style=STATUS_LABEL_STYLE), html.Div(str(value), style=STATUS_VALUE_STYLE)], style=style)


def _connection_label(status: dict, paused: bool) -> str:
    prefix = "Paused / " if paused else ""
    mode = status.get("mode", "disconnected")
    if mode == "replay":
        suffix = "finished" if status.get("replayFinished") else "running"
        return f"{prefix}Replay ({suffix})"
    if mode == "serial":
        return f"{prefix}Connected" if status.get("serialConnected") else f"{prefix}Disconnected"
    return f"{prefix}Disconnected"


def _resolve_color_range(matrix: np.ndarray, color_mode: str, fixed_min: Any, fixed_max: Any) -> tuple[float | None, float | None]:
    finite_values = matrix[np.isfinite(matrix)]
    if color_mode == "symmetric" and finite_values.size:
        max_abs = float(np.max(np.abs(finite_values)))
        if max_abs > 0:
            return -max_abs, max_abs
    elif color_mode == "fixed":
        try:
            zmin = float(fixed_min)
            zmax = float(fixed_max)
            if math.isfinite(zmin) and math.isfinite(zmax) and zmin < zmax:
                return zmin, zmax
        except (TypeError, ValueError):
            pass
    return None, None


def _convert_matrix_for_display(matrix: np.ndarray, source_unit: str, unit_mode: str) -> tuple[np.ndarray, str]:
    source_key = _normalize_unit(source_unit)
    if source_key not in VOLTAGE_FACTORS_TO_UV:
        return matrix.copy(), source_unit
    source_factor = VOLTAGE_FACTORS_TO_UV[source_key]
    values_uv = matrix.astype(float, copy=True) * source_factor
    target_key = _resolve_target_voltage_unit(values_uv, unit_mode, source_key)
    return values_uv / VOLTAGE_FACTORS_TO_UV[target_key], CANONICAL_VOLTAGE_UNITS[target_key]


def _convert_history_for_display(history, unit_mode: str) -> tuple[np.ndarray, str, bool, bool]:
    raw_values = history["value"].to_numpy(dtype=float)
    units = [unit for unit in history["unit"].dropna().unique().tolist() if unit != ""]
    unit_keys = [_normalize_unit(unit) for unit in units]
    all_voltage = bool(unit_keys) and all(unit_key in VOLTAGE_FACTORS_TO_UV for unit_key in unit_keys)
    if not all_voltage:
        mixed_units = len(units) > 1
        return raw_values, "mixed units" if mixed_units else (units[0] if units else "value"), mixed_units, False
    if unit_mode == "source" and len(set(unit_keys)) > 1:
        return raw_values, "mixed units", True, False
    source_unit_keys = history["unit"].map(_normalize_unit).to_numpy()
    values_uv = np.array([value * VOLTAGE_FACTORS_TO_UV.get(unit_key, np.nan) for value, unit_key in zip(raw_values, source_unit_keys)], dtype=float)
    source_key = unit_keys[0]
    target_key = _resolve_target_voltage_unit(values_uv, unit_mode, source_key)
    return values_uv / VOLTAGE_FACTORS_TO_UV[target_key], CANONICAL_VOLTAGE_UNITS[target_key], False, True


def _resolve_target_voltage_unit(values_uv: np.ndarray, unit_mode: str, source_key: str) -> str:
    requested_key = _normalize_unit(unit_mode)
    if requested_key in VOLTAGE_FACTORS_TO_UV:
        return requested_key
    if unit_mode == "source" and source_key in VOLTAGE_FACTORS_TO_UV:
        return source_key
    return _choose_auto_voltage_unit(values_uv)


def _choose_auto_voltage_unit(values_uv: np.ndarray) -> str:
    finite_values = values_uv[np.isfinite(values_uv)]
    if not finite_values.size:
        return "uv"
    max_abs_uv = float(np.max(np.abs(finite_values)))
    if max_abs_uv >= VOLTAGE_FACTORS_TO_UV["v"]:
        return "v"
    if max_abs_uv >= VOLTAGE_FACTORS_TO_UV["mv"]:
        return "mv"
    return "uv"


def _normalize_unit(unit: Any) -> str:
    return str(unit or "").strip().replace("\u00b5", "u").replace("\u03bc", "u").lower()


def _format_value(value: float, unit: str = "") -> str:
    if not np.isfinite(value):
        return "NaN"
    abs_value = abs(float(value))
    if unit == "uV":
        if abs_value < 10:
            value_text = f"{float(value):,.2f}".rstrip("0").rstrip(".")
        elif abs_value < 1_000:
            value_text = f"{float(value):,.1f}".rstrip("0").rstrip(".")
        elif math.isclose(float(value), round(float(value)), rel_tol=0.0, abs_tol=1e-9):
            value_text = f"{int(round(float(value))):,}"
        else:
            value_text = f"{float(value):,.1f}".rstrip("0").rstrip(".")
    elif unit == "mV":
        if abs_value >= 100:
            value_text = f"{float(value):,.1f}".rstrip("0").rstrip(".")
        elif abs_value >= 10:
            value_text = f"{float(value):,.2f}".rstrip("0").rstrip(".")
        else:
            value_text = f"{float(value):,.3f}".rstrip("0").rstrip(".")
    elif unit == "V":
        value_text = (f"{float(value):,.4f}" if abs_value >= 10 else f"{float(value):,.6f}").rstrip("0").rstrip(".")
    else:
        value_text = f"{float(value):,.6f}".rstrip("0").rstrip(".")
    return f"{value_text} {unit}".strip()


def _colorbar_tick_format(unit: str) -> str:
    return ".3~g"


def _empty_figure(message: str, title: str) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=message, x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False, font={"size": 18, "color": "#6b7280"})
    fig.update_layout(**_base_figure_layout(title))
    return fig


def _base_figure_layout(title: str) -> dict:
    return {
        "title": title,
        "margin": {"l": 58, "r": 24, "t": 56, "b": 52},
        "paper_bgcolor": "white",
        "plot_bgcolor": "white",
        "font": {"family": "Segoe UI, Arial, sans-serif", "size": 12, "color": "#17202a"},
    }


def _control(label: str, child: Any) -> html.Div:
    return html.Div([html.Label(label, className="control-label"), child], className="compact-control")


def _frame_type_options(frame_types: list[str]) -> list[dict]:
    return [{"label": frame_type, "value": frame_type} for frame_type in list(dict.fromkeys([*DEFAULT_FRAME_TYPES, *frame_types]))]


def _history_window_options(include_advanced: bool = False) -> list[dict]:
    options = [
        {"label": "10 s", "value": "last_10s"},
        {"label": "30 s", "value": "last_30s"},
        {"label": "60 s", "value": "last_60s"},
        {"label": "5 min", "value": "last_5min"},
    ]
    if include_advanced:
        options.extend(
            [
                {"label": "Last N points", "value": "last_n"},
                {"label": "Custom range", "value": "custom"},
                {"label": "All", "value": "all"},
            ]
        )
    return options


def _color_mode_options(include_fixed: bool = False) -> list[dict]:
    options = [
        {"label": "Auto", "value": "auto"},
        {"label": "Symmetric", "value": "symmetric"},
    ]
    if include_fixed:
        options.append({"label": "Fixed", "value": "fixed"})
    return options


def _cell_name_from_click_data(click_data: dict | None) -> str | None:
    try:
        custom_data = click_data["points"][0].get("customdata")
    except Exception:
        return None
    if isinstance(custom_data, list):
        return custom_data[0]
    return custom_data


def _split_cell_name(cell_name: str) -> tuple[int | None, int | None]:
    try:
        source_text, detector_text = cell_name.split("D", maxsplit=1)
        return int(source_text[1:]), int(detector_text)
    except Exception:
        return None, None


def _safe_qsize(input_queue: "queue.Queue[bytes]") -> int | str:
    try:
        return input_queue.qsize()
    except NotImplementedError:
        return "-"


def _clear_input_queue(input_queue: "queue.Queue[bytes]") -> None:
    while True:
        try:
            input_queue.get_nowait()
        except queue.Empty:
            break


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _target_fps(_render_mode: str | None = None) -> int:
    return DEFAULT_GUI_TARGET_FPS


def _history_points_limit(value: Any, render_mode: str | None) -> int:
    if str(value or "").lower() == "auto":
        return 5000 if render_mode == "Quality" else 1200
    parsed = _safe_int(value)
    return max(100, parsed or 1200)


def _render_runtime_stats(heatmap_cache: HeatmapRenderCacheThread, history_cache: HistoryRenderCacheThread, frontend_stats: dict) -> dict:
    heatmap_stats = heatmap_cache.getStats()
    history_stats = history_cache.getStats()
    heatmap_fps = float(frontend_stats.get("heatmapActualFps") or heatmap_stats.get("actualFps") or 0.0)
    history_fps = float(frontend_stats.get("historyActualFps") or history_stats.get("actualFps") or 0.0)
    frontend_skipped = int(frontend_stats.get("frontendRenderSkipped") or 0)
    heatmap_skipped = int(heatmap_stats.get("renderSkipped") or 0)
    history_skipped = int(history_stats.get("renderSkipped") or 0)
    render_skipped = frontend_skipped + heatmap_skipped + history_skipped
    return {
        "guiHeatmapFps": heatmap_fps,
        "guiHistoryFps": history_fps,
        "heatmapTargetFps": heatmap_stats.get("targetFps"),
        "historyTargetFps": history_stats.get("targetFps"),
        "frontendRenderSkipped": frontend_skipped,
        "renderSkipped": render_skipped,
        "heatmapRenderSkipped": heatmap_skipped,
        "historyRenderSkipped": history_skipped,
        "heatmapDisplayUnit": heatmap_stats.get("displayUnit"),
        "historyDisplayUnit": history_stats.get("unit"),
        "visibleHistoryPoints": history_stats.get("visiblePointCount", 0),
        "renderedHistoryPoints": history_stats.get("renderedPointCount", 0),
        "historyDownsampled": history_stats.get("downsampled", False),
        "lastClientError": frontend_stats.get("lastClientError") or "",
    }


def _fmt_rate(value: Any) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "-"


def _format_wall_time(timestamp: float | None) -> str:
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")


def _clock_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _dash_if_none(value: Any) -> Any:
    return "-" if value is None else value


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value:
            return value
    return "-"


def _format_hex(value: Any, width: int = 4) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"0x{int(value):0{width}X}"
    except (TypeError, ValueError):
        return str(value)


PAGE_STYLE = {
    "minHeight": "100vh",
    "padding": "20px",
    "background": "#eef2f5",
    "color": "#17202a",
    "fontFamily": "Segoe UI, Arial, sans-serif",
    "boxSizing": "border-box",
}
TITLE_STYLE = {"margin": "0 0 14px 0", "fontSize": "30px", "fontWeight": 700, "letterSpacing": "0"}
SECTION_TITLE_STYLE = {"margin": "0 0 10px 0", "fontSize": "18px", "fontWeight": 700, "letterSpacing": "0"}
TOP_GRID_STYLE = {"display": "grid", "gridTemplateColumns": "minmax(360px, 1fr) auto", "gap": "16px", "alignItems": "start"}
STATUS_GRID_STYLE = {"display": "grid", "gridTemplateColumns": "repeat(auto-fit, minmax(138px, 1fr))", "gap": "8px"}
STATUS_ITEM_STYLE = {"background": "white", "border": "1px solid #d9e2ec", "borderRadius": "8px", "padding": "9px 10px", "minHeight": "54px", "boxSizing": "border-box"}
WARNING_STATUS_ITEM_STYLE = {"border": "1px solid #d97706", "background": "#fff7ed"}
STATUS_LABEL_STYLE = {"fontSize": "11px", "color": "#52606d", "lineHeight": "15px"}
STATUS_VALUE_STYLE = {"fontSize": "14px", "fontWeight": 600, "lineHeight": "18px", "whiteSpace": "nowrap", "overflow": "hidden", "textOverflow": "ellipsis"}
BUTTON_ROW_STYLE = {"display": "flex", "flexWrap": "wrap", "gap": "8px", "justifyContent": "flex-end"}
BUTTON_STYLE = {"height": "38px", "border": "1px solid #0f766e", "background": "#0f766e", "color": "white", "borderRadius": "8px", "padding": "0 14px", "fontWeight": 700, "cursor": "pointer"}
SECONDARY_BUTTON_STYLE = {"height": "38px", "border": "1px solid #a7b7c7", "background": "white", "color": "#17202a", "borderRadius": "8px", "padding": "0 14px", "fontWeight": 600, "cursor": "pointer"}
CONNECTION_GRID_STYLE = {"display": "grid", "gridTemplateColumns": "repeat(auto-fit, minmax(150px, 1fr))", "gap": "10px", "alignItems": "end"}
CONTROL_GRID_STYLE = {"display": "grid", "gridTemplateColumns": "repeat(auto-fit, minmax(160px, 1fr))", "gap": "12px", "marginTop": "16px", "alignItems": "end"}
CONTROL_ITEM_STYLE = {"minWidth": 0}
LABEL_STYLE = {"display": "block", "fontSize": "12px", "fontWeight": 700, "color": "#334e68", "marginBottom": "5px"}
INPUT_STYLE = {"width": "100%", "height": "38px", "border": "1px solid #cbd5e1", "borderRadius": "6px", "padding": "0 10px", "boxSizing": "border-box"}
CHECKLIST_STYLE = {"height": "38px", "display": "flex", "alignItems": "center"}
MESSAGE_ROW_STYLE = {"display": "flex", "gap": "16px", "minHeight": "24px", "marginTop": "10px", "flexWrap": "wrap"}
MESSAGE_STYLE = {"fontSize": "13px", "color": "#334e68"}
MAIN_GRID_STYLE = {"display": "grid", "gridTemplateColumns": "repeat(auto-fit, minmax(360px, 1fr))", "gap": "16px", "marginTop": "12px"}
PANEL_STYLE = {"background": "white", "border": "1px solid #d9e2ec", "borderRadius": "8px", "padding": "10px", "minWidth": 0}
TABLE_STYLE = {"width": "100%", "borderCollapse": "collapse", "marginTop": "10px", "fontSize": "12px"}
TABLE_HEADER_STYLE = {"textAlign": "left", "borderBottom": "1px solid #d9e2ec", "padding": "6px", "color": "#334e68"}
TABLE_CELL_STYLE = {"borderBottom": "1px solid #eef2f5", "padding": "6px", "verticalAlign": "top"}
