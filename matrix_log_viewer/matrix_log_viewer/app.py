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
        "function(heatmapSnapshot, historySnapshot, clearRevision, statusTick, current) {"
        " if (!window.SensorArrayLive || !window.SensorArrayLive.applySnapshots) {"
        "   return current || {};"
        " }"
        " return window.SensorArrayLive.applySnapshots(heatmapSnapshot, historySnapshot, clearRevision || {}, statusTick, current || {});"
        "}",
        Output("frontend-fps-store", "data"),
        Input("heatmap-snapshot-store", "data"),
        Input("history-snapshot-store", "data"),
        Input("clear-revision-store", "data"),
        Input("status-interval", "n_intervals"),
        State("frontend-fps-store", "data"),
    )

    @app.callback(Output("render-interval", "interval"), Input("interval-ms", "value"))
    def update_render_interval(interval_ms: Any) -> int:
        try:
            return max(16, min(10_000, int(interval_ms)))
        except (TypeError, ValueError):
            return DEFAULT_RENDER_INTERVAL_MS

    @app.callback(Output("interval-ms", "value"), Input("gui-target-fps", "value"), prevent_initial_call=True)
    def sync_render_interval_to_target_fps(gui_target_fps: Any) -> int:
        return _render_interval_for_target_fps(gui_target_fps)

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

    @app.callback(
        Output("clear-status", "children"),
        Output("clear-revision-store", "data"),
        Output("history-follow-store", "data", allow_duplicate=True),
        Output("history-follow-revision-store", "data", allow_duplicate=True),
        Output("auto-follow", "value"),
        Output("heatmap-snapshot-store", "data", allow_duplicate=True),
        Output("history-snapshot-store", "data", allow_duplicate=True),
        Input("clear-button", "n_clicks"),
        State("clear-revision-store", "data"),
        State("history-follow-revision-store", "data"),
        prevent_initial_call=True,
    )
    def clear_history(n_clicks: int | None, clear_state: dict | None, follow_revision: Any) -> tuple[Any, dict, bool, int, list[str], dict, dict]:
        if not n_clicks:
            return no_update, no_update, no_update, no_update, no_update, no_update, no_update
        dataStore.clear()
        heatmap_cache.reset(reason="clear")
        history_cache.reset(reason="clear")
        clear_revision = (_safe_int((clear_state or {}).get("revision"), default=0) or 0) + 1
        next_follow_revision = (_safe_int(follow_revision, default=0) or 0) + 1
        clear_payload = {"revision": clear_revision, "reason": "clear", "time": time.time()}
        return (
            f"History cleared at {_clock_text()}.",
            clear_payload,
            True,
            next_follow_revision,
            ["enabled"],
            {},
            {},
        )

    @app.callback(Output("save-status", "children"), Input("save-button", "n_clicks"), State("frame-type-dropdown", "value"), prevent_initial_call=True)
    def save_snapshot(n_clicks: int | None, frame_type: str | None) -> str:
        if not n_clicks:
            return no_update
        selected_type = _requested_frame_type(frame_type)
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
        Output("history-follow-revision-store", "data"),
        Input("history-graph", "relayoutData"),
        Input("follow-latest-button", "n_clicks"),
        Input("auto-follow", "value"),
        State("history-follow-store", "data"),
        State("history-follow-revision-store", "data"),
        State("history-snapshot-store", "data"),
        prevent_initial_call=True,
    )
    def update_history_follow(
        relayout_data: dict | None,
        _follow_clicks: int | None,
        auto_follow_values: list[str] | None,
        current: bool,
        current_revision: Any,
        history_snapshot: dict | None,
    ) -> tuple[bool, int]:
        triggered = ctx.triggered_id
        revision = _safe_int(current_revision, default=0) or 0
        if triggered == "follow-latest-button":
            return True, revision + 1
        if triggered == "auto-follow":
            enabled = "enabled" in (auto_follow_values or [])
            return enabled, revision + 1 if enabled and not bool(current) else revision
        if triggered == "history-graph" and _relayout_has_manual_x_range(relayout_data):
            if _relayout_matches_expected_follow_range(relayout_data, history_snapshot):
                return bool(current), revision
            return False, revision
        return bool(current), revision

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
        Input("history-follow-revision-store", "data"),
        Input("render-mode", "value"),
        Input("gui-target-fps", "value"),
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
        follow_revision: Any,
        render_mode: str | None,
        gui_target_fps: Any,
        history_max_points: Any,
        show_markers: list[str] | None,
    ) -> dict:
        selected_cell = selected_cell if selected_cell in CELL_NAMES else DEFAULT_SELECTED_CELL
        selected_type = _requested_frame_type(frame_type)
        target_fps = _target_fps(render_mode, gui_target_fps)
        max_points = _history_points_limit(history_max_points, render_mode)
        heatmap_cache.updateControls(
            stream=selected_type,
            selectedCell=selected_cell,
            targetFps=target_fps,
            unitMode=unit_mode or "auto",
            colorMode=color_mode or "auto",
            fixedMin=_safe_float(fixed_min, default=None),
            fixedMax=_safe_float(fixed_max, default=None),
        )
        history_cache.updateControls(
            stream=selected_type,
            selectedCell=selected_cell,
            xAxis=x_axis or "timeSeconds",
            unitMode=unit_mode or "auto",
            historyWindow=history_window or "last_30s",
            lastN=_safe_int(last_n) or 1000,
            customXMin=_safe_float(custom_min, default=None),
            customXMax=_safe_float(custom_max, default=None),
            followLatest=bool(follow_latest),
            followRevision=_safe_int(follow_revision, default=0) or 0,
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
            "xAxis": x_axis or "timeSeconds",
            "historyWindow": history_window or "last_30s",
            "followLatest": bool(follow_latest),
            "followRevision": _safe_int(follow_revision, default=0) or 0,
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
        selected_type = _requested_frame_type(frame_type)
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
        selected_type = _requested_frame_type(frame_type)
        latest_meta = dataStore.getLatestFrameMeta(selected_type)
        parser_stats = parser.getStats()
        connection_status = manager.getStatus()
        runtime_stats = runtime_state.getStats()
        runtime_stats.update(_render_runtime_stats(heatmap_cache, history_cache, {}))
        queue_depth = _safe_qsize(inputQueue)
        return (
            _build_parser_diagnostics(parser_stats, runtime_stats, queue_depth),
            _build_connection_panel(connection_status, queue_depth),
            _build_device_panel(latest_meta, parser_stats, dataStore.getLatestDeviceStatus(), dataStore.getRecentDeviceEvents(50)),
        )

    return app


def _build_layout() -> html.Div:
    return html.Div(
        [
            dcc.Store(id="paused-store", data=False),
            dcc.Store(id="ingest-stats-store", data={}),
            dcc.Store(id="render-control-store", data={}),
            dcc.Store(id="heatmap-snapshot-store", data={}),
            dcc.Store(id="history-snapshot-store", data={}),
            dcc.Store(id="frontend-fps-store", data={}),
            dcc.Store(id="heatmap-render-state", data={}),
            dcc.Store(id="history-render-state", data={}),
            dcc.Store(id="history-follow-store", data=True),
            dcc.Store(id="history-follow-revision-store", data=0),
            dcc.Store(id="clear-revision-store", data={"revision": 0}),
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
                        ],
                        className="control-card connection-card",
                    ),
                    html.Div(
                        [
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
                                    html.Button("Follow Latest", id="follow-latest-button", n_clicks=0, className="button secondary"),
                                ],
                                className="toolbar display-toolbar",
                            ),
                        ],
                        className="control-card view-card",
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
            html.Main(
                [
                    html.Section(
                        dcc.Graph(
                            id="heatmap",
                            figure=_empty_figure("No data for selected stream.", "8x8 Matrix"),
                            config={"displayModeBar": False, "responsive": True, "doubleClick": "reset"},
                            className="graph graph-matrix",
                        ),
                        className="panel matrix-panel",
                    ),
                    html.Section(
                        dcc.Graph(
                            id="history-graph",
                            figure=_empty_figure("No history data for selected cell.", "History"),
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
                                            _control("Fixed min", dcc.Input(id="fixed-min", type="number", debounce=True, className="input")),
                                            _control("Fixed max", dcc.Input(id="fixed-max", type="number", debounce=True, className="input")),
                                            _control("Render mode", dcc.Dropdown(id="render-mode", value="Normal", clearable=False, options=[
                                                {"label": "Normal", "value": "Normal"},
                                                {"label": "Performance", "value": "Performance"},
                                                {"label": "Quality", "value": "Quality"},
                                            ])),
                                            _control("GUI target fps", dcc.Dropdown(id="gui-target-fps", value=DEFAULT_GUI_TARGET_FPS, clearable=False, options=[
                                                {"label": "30", "value": 30},
                                                {"label": "60", "value": 60},
                                            ])),
                                            _control("GUI render interval ms", dcc.Input(id="interval-ms", type="number", value=DEFAULT_RENDER_INTERVAL_MS, min=16, step=1, debounce=True, className="input")),
                                            _control("History max points", dcc.Dropdown(id="history-max-points", value="Auto", clearable=False, options=[
                                                {"label": "Auto", "value": "Auto"},
                                                {"label": "1000", "value": "1000"},
                                                {"label": "1200", "value": "1200"},
                                                {"label": "5000", "value": "5000"},
                                            ])),
                                            _control("Heatmap text refresh", dcc.Dropdown(id="heatmap-text-refresh", value="10Hz", clearable=False, options=[
                                                {"label": "Off", "value": "Off"},
                                                {"label": "5Hz", "value": "5Hz"},
                                                {"label": "10Hz", "value": "10Hz"},
                                                {"label": "30Hz", "value": "30Hz"},
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
        className="app-shell page",
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
    matrix = np.asarray(matrix, dtype=float)
    unit = display_unit or meta.get("unit") or ""
    if meta.get("seq") is None and not np.isfinite(matrix).any():
        return _empty_figure(f"No data for selected stream: {requested_frame_type}", "8x8 Matrix")

    cell_names = np.array([[f"S{row + 1}D{col + 1}" for col in range(MATRIX_SIZE)] for row in range(MATRIX_SIZE)])
    validity = np.where(np.isfinite(matrix), "valid", "invalid")
    custom_data = np.dstack([cell_names, validity])
    text = np.array([[f"{cell_names[row, col]}<br>{_format_value(matrix[row, col], unit) if np.isfinite(matrix[row, col]) else '-'}" for col in range(MATRIX_SIZE)] for row in range(MATRIX_SIZE)])

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
                colorbar={"title": unit or "value", "len": 0.82, "thickness": 12},
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
        subset = history.tail(max(1, _safe_int(last_n, default=1000)))
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
        count = max(1, _safe_int(last_n, default=1000))
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
        fps = _safe_float(runtime_stats.get("parsedBinaryFps"), default=20.0)
    else:
        fps = max(_safe_float(runtime_stats.get("parsedTextFps"), default=0.0), 20.0)
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
        f"visual fps: {runtime_stats.get('visualUpdateFps', 0.0):.1f}"
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


def _relayout_matches_expected_follow_range(relayout_data: dict | None, snapshot: dict | None) -> bool:
    actual = _relayout_x_range(relayout_data)
    expected = _expected_follow_range_from_snapshot(snapshot)
    if actual is None or expected is None:
        return False
    return _ranges_close(actual, expected)


def _relayout_x_range(relayout_data: dict | None) -> tuple[float, float] | None:
    if not relayout_data:
        return None
    if isinstance(relayout_data.get("xaxis.range"), (list, tuple)) and len(relayout_data["xaxis.range"]) >= 2:
        start = _safe_float(relayout_data["xaxis.range"][0], default=None)
        end = _safe_float(relayout_data["xaxis.range"][1], default=None)
    else:
        start = _safe_float(relayout_data.get("xaxis.range[0]"), default=None)
        end = _safe_float(relayout_data.get("xaxis.range[1]"), default=None)
    if start is None or end is None:
        return None
    return (float(start), float(end))


def _expected_follow_range_from_snapshot(snapshot: dict | None) -> tuple[float, float] | None:
    if not snapshot or not snapshot.get("followLatest", False):
        return None
    start = _safe_float(snapshot.get("followRangeStart"), default=None)
    end = _safe_float(snapshot.get("followRangeEnd"), default=None)
    if start is not None and end is not None:
        return _pad_expected_follow_range(float(start), float(end), str(snapshot.get("xAxis") or "timeSeconds"), str(snapshot.get("historyWindow") or ""))

    x_values = snapshot.get("xAppend") or snapshot.get("x") or []
    finite_x = [float(value) for value in (_safe_float(value, default=None) for value in x_values) if value is not None]
    if not finite_x:
        return None
    latest = finite_x[-1]
    seconds = {"last_10s": 10.0, "last_30s": 30.0, "last_60s": 60.0, "last_5min": 300.0}.get(str(snapshot.get("historyWindow") or ""))
    x_axis = str(snapshot.get("xAxis") or "timeSeconds")
    if seconds and x_axis == "timeSeconds":
        return (latest - seconds, latest)
    if seconds and x_axis == "timestampUs":
        return (latest - seconds * 1_000_000.0, latest)
    if len(finite_x) == 1:
        pad = 500_000.0 if x_axis == "timestampUs" else 0.5
        return (latest - pad, latest + pad)
    return (min(finite_x), max(finite_x))


def _pad_expected_follow_range(start: float, end: float, x_axis: str, history_window: str) -> tuple[float, float]:
    if end < start:
        start, end = end, start
    if end > start:
        return (start, end)
    seconds = {"last_10s": 10.0, "last_30s": 30.0, "last_60s": 60.0, "last_5min": 300.0}.get(history_window)
    if seconds and x_axis == "timestampUs":
        pad = max(500_000.0, seconds * 1_000_000.0 * 0.05)
    elif seconds and x_axis == "timeSeconds":
        pad = max(0.5, seconds * 0.05)
    else:
        pad = 500_000.0 if x_axis == "timestampUs" else 0.5
    return (start - pad, end + pad)


def _ranges_close(actual: tuple[float, float], expected: tuple[float, float]) -> bool:
    if not all(math.isfinite(value) for value in (*actual, *expected)):
        return False
    span = max(abs(expected[1] - expected[0]), abs(actual[1] - actual[0]), 1.0)
    tolerance = max(span * 0.002, 1e-6)
    return abs(actual[0] - expected[0]) <= tolerance and abs(actual[1] - expected[1]) <= tolerance


def _build_compact_status_bar(
    meta: dict | None,
    matrix: np.ndarray,
    selected_cell: str,
    parser_stats: dict | None,
    connection_status: dict | None,
    runtime_stats: dict | None,
    paused: bool,
    display_unit: str,
    display_type: str,
    warning_badge: html.Div | None = None,
) -> list:
    meta = meta or {}
    parser_stats = parser_stats or {}
    connection_status = connection_status or {}
    runtime_stats = runtime_stats or {}
    port = connection_status.get("serialPort") or connection_status.get("port") or "-"
    selected_value = _selected_cell_value(matrix, selected_cell, display_unit)
    last_status = _safe_int(meta.get("lastStatusCode"), default=None)
    if last_status is None:
        status_code = "-"
        status_tone = "neutral"
    else:
        status_name = meta.get("lastStatusCodeName") or ("OK" if last_status == 0 else "-")
        status_code = f"{_format_hex(last_status, 4)} {status_name}".strip()
        status_tone = "ok" if last_status == 0 else "warn"
    connection_tone = _connection_tone(connection_status)
    input_fps = _first_non_missing(
        runtime_stats.get("parsedBinaryFps") if (display_type or "").upper() == "FAST_BINARY" else runtime_stats.get("parsedTextFps"),
        runtime_stats.get("parsedBinaryFps"),
        0.0,
    )
    visual_fps = _first_non_missing(runtime_stats.get("visualUpdateFps"), runtime_stats.get("guiHistoryFps"), runtime_stats.get("guiHeatmapFps"), 0.0)
    cb_fps = _first_non_missing(runtime_stats.get("callbackFps"), runtime_stats.get("renderTickFps"), 0.0)
    raf_fps = _first_non_missing(runtime_stats.get("browserRafFps"), 0.0)
    idle_callbacks = _safe_int(runtime_stats.get("idleCallbacks"), default=0)
    no_revision_callbacks = _safe_int(runtime_stats.get("noRevisionCallbacks"), default=0)
    coalesced_frames = _safe_int(runtime_stats.get("frontendCoalescedFrames"), default=0)
    dropped_frames = _safe_int(runtime_stats.get("frontendDroppedFrames"), default=0) + _safe_int(runtime_stats.get("renderCacheSkipped"), default=0)
    warning_chip = warning_badge or _status_chip("warning", "clear", "ok")
    return [
        _status_chip("connection", f"{_connection_label(connection_status, paused)} {port}".strip(), connection_tone),
        _status_chip("stream", display_type or "-"),
        _status_chip("selected", f"{selected_cell} {selected_value}".strip()),
        _status_chip("seq", _format_counter(meta.get("seq"))),
        _status_chip("input fps", _fmt_rate(input_fps)),
        _status_chip("visual fps", _fmt_rate(visual_fps)),
        _status_chip("cb fps", _fmt_rate(cb_fps)),
        _status_chip("raf fps", _fmt_rate(raf_fps)),
        _status_chip("idle/no rev", f"{idle_callbacks}/{no_revision_callbacks}"),
        _status_chip("coalesced/drop", f"{_format_counter(coalesced_frames)}/{_format_counter(dropped_frames)}"),
        _status_chip("status", status_code, status_tone),
        warning_chip,
    ]


def _warning_badge(parser_stats: dict | None, connection_status: dict | None, runtime_stats: dict | None, meta: dict | None, previous: dict | None) -> tuple[html.Div, dict]:
    parser_stats = parser_stats or {}
    connection_status = connection_status or {}
    runtime_stats = runtime_stats or {}
    meta = meta or {}
    previous = previous or {}
    device = runtime_stats.get("deviceSummary") or {}
    current = {
        "crc": _safe_int(parser_stats.get("binaryCrcErrors"), default=0),
        "resync": _safe_int(parser_stats.get("binaryMagicResyncs"), default=0),
        "parse": _safe_int(parser_stats.get("parseErrors"), default=0),
        "seq_gap": _safe_int(runtime_stats.get("seqGap"), default=0),
        "device_drop": _safe_int(_first_non_missing(device.get("latestDrop"), meta.get("droppedFrames")), default=0),
        "host_drop": _safe_int(connection_status.get("droppedInputChunks"), default=0),
    }
    deltas = {key: max(0, current[key] - _safe_int(previous.get(key), default=0)) for key in current}
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
    total_drop = current["seq_gap"] + current["device_drop"] + current["host_drop"]
    if current["crc"]:
        labels.append(f"CRC {current['crc']}")
    if current["resync"]:
        labels.append(f"RESYNC {current['resync']}")
    if current["parse"]:
        labels.append(f"PARSE {current['parse']}")
    if total_drop:
        labels.append(f"DROP {total_drop}")
    if labels:
        return _status_chip("warning", " / ".join(labels), "error" if current["crc"] or current["parse"] else "warn"), current
    return _status_chip("warning", "clear", "ok"), current


def _selected_cell_value(matrix: np.ndarray, selected_cell: str, display_unit: str) -> str:
    source, detector = _split_cell_name(selected_cell)
    if source is None or detector is None:
        return ""
    try:
        value = float(matrix[source - 1, detector - 1])
    except Exception:
        return "-"
    return _format_value(value, display_unit) if _is_finite_number(value) else "-"


def _status_chip(label: str, value: Any, tone: str = "neutral", title: str | None = None) -> html.Div:
    class_name = "status-chip"
    if tone and tone != "neutral":
        class_name = f"{class_name} {tone}"
    display_value = _dash_if_missing(value)
    return html.Div(
        [html.Span(label, className="status-label"), html.Strong(str(display_value), className="status-value")],
        className=class_name,
        title=title,
    )


def _build_parser_diagnostics(parser_stats: dict, runtime_stats: dict, queue_depth: int | str) -> list:
    items = [
        ("parsed_total", _format_counter(parser_stats.get("parsedFramesTotal"))),
        ("parsed_binary", _format_counter(parser_stats.get("parsedBinaryFrames"))),
        ("parsed_text", _format_counter(parser_stats.get("parsedTextFrames"))),
        ("crc_errors", _format_counter(parser_stats.get("binaryCrcErrors"))),
        ("resyncs", _format_counter(parser_stats.get("binaryMagicResyncs"))),
        ("parse_errors", _format_counter(parser_stats.get("parseErrors"))),
        ("bytes/sec", f"{_safe_float(runtime_stats.get('bytesPerSec'), default=0.0):.0f}"),
        ("queue_depth", queue_depth),
        ("browser_raf_fps", _fmt_rate(runtime_stats.get("browserRafFps"))),
        ("visual_update_fps", _fmt_rate(runtime_stats.get("visualUpdateFps"))),
        ("callback_fps", _fmt_rate(runtime_stats.get("callbackFps"))),
        ("history_plotly_fps", _fmt_rate(runtime_stats.get("guiHistoryFps"))),
        ("heatmap_plotly_fps", _fmt_rate(runtime_stats.get("guiHeatmapFps"))),
        ("render_tick_fps", _fmt_rate(runtime_stats.get("renderTickFps"))),
        ("rendered_frame_fps", _fmt_rate(runtime_stats.get("renderedFrameFps"))),
        ("frontend_coalesced", _format_counter(runtime_stats.get("frontendCoalescedFrames"))),
        ("frontend_dropped", _format_counter(runtime_stats.get("frontendDroppedFrames"))),
        ("render_cache_skipped", _format_counter(runtime_stats.get("renderCacheSkipped"))),
        ("heatmapUnit", runtime_stats.get("heatmapUnit") or "-"),
        ("heatmapFiniteMin", _fmt_rate(runtime_stats.get("heatmapFiniteMin"))),
        ("heatmapFiniteMax", _fmt_rate(runtime_stats.get("heatmapFiniteMax"))),
        ("heatmapZMin", _fmt_rate(runtime_stats.get("heatmapZMin"))),
        ("heatmapZMax", _fmt_rate(runtime_stats.get("heatmapZMax"))),
        ("heatmapColorMode", runtime_stats.get("heatmapColorMode") or "-"),
        ("last_error", _first_non_empty(runtime_stats.get("lastCsvError", ""), parser_stats.get("lastError", ""), "-")),
    ]
    return [_status_item(label, value, warning=label in {"crc_errors", "resyncs", "parse_errors"} and _safe_counter_positive(value)) for label, value in items]


def _build_status_bar(meta: dict, parser_stats: dict, connection_status: dict, runtime_stats: dict, paused: bool, queue_depth: int | str, display_unit: str) -> list:
    dropped = _safe_int(meta.get("droppedFrames"), default=0)
    decimated = _safe_int(meta.get("outputDecimatedFrames"), default=0)
    host_drop_chunks = _safe_int(connection_status.get("droppedInputChunks"), default=0)
    seq_gap = _safe_int(runtime_stats.get("seqGap"), default=0)
    last_status = _safe_int(meta.get("lastStatusCode"), default=0)
    return [
        _status_item("Input", _connection_label(connection_status, paused)),
        _status_item("frame_type", meta.get("frameType") or "-"),
        _status_item("seq", _format_counter(meta.get("seq"))),
        _status_item("latest_seq", _format_counter(runtime_stats.get("latestSeq"))),
        _status_item("seq_gap", seq_gap, warning=seq_gap > 0),
        _status_item("timestamp_us", _dash_if_none(meta.get("timestampUs"))),
        _status_item("duration_us", _dash_if_none(meta.get("durationUs"))),
        _status_item("unit", meta.get("unit") or "-"),
        _status_item("display_unit", display_unit or meta.get("unit") or "-"),
        _status_item("status_code", f"{_format_hex(meta.get('lastStatusCode'), 4)} {meta.get('lastStatusCodeName') or '-'}", warning=last_status > 0),
        _status_item("device_dropped", dropped, warning=dropped > 0),
        _status_item("device_decimated", decimated, warning=decimated > 0),
        _status_item("host_drop_chunks", host_drop_chunks, warning=host_drop_chunks > 0),
        _status_item("adsDr", _dash_if_none(meta.get("adsDr"))),
        _status_item("outputDiv", _dash_if_none(meta.get("outputDivider"))),
        _status_item("bytes/sec", f"{_safe_float(runtime_stats.get('bytesPerSec'), default=0.0):.0f}"),
        _status_item("binary_fps", _fmt_rate(runtime_stats.get("parsedBinaryFps"))),
        _status_item("text_fps", _fmt_rate(runtime_stats.get("parsedTextFps"))),
        _status_item("visual_fps", _fmt_rate(runtime_stats.get("visualUpdateFps"))),
        _status_item("browser_raf_fps", _fmt_rate(runtime_stats.get("browserRafFps"))),
        _status_item("bytes", _format_counter(connection_status.get("bytesReceived"))),
        _status_item("chunks", _format_counter(connection_status.get("chunksReceived"))),
        _status_item("queue_depth", queue_depth),
        _status_item("parsed_total", _format_counter(parser_stats.get("parsedFramesTotal"))),
        _status_item("parsed_binary", _format_counter(parser_stats.get("parsedBinaryFrames"))),
        _status_item("parsed_text", _format_counter(parser_stats.get("parsedTextFrames"))),
        _status_item("crc_errors", _format_counter(parser_stats.get("binaryCrcErrors")), warning=_safe_counter_positive(parser_stats.get("binaryCrcErrors"))),
        _status_item("resyncs", _format_counter(parser_stats.get("binaryMagicResyncs")), warning=_safe_counter_positive(parser_stats.get("binaryMagicResyncs"))),
        _status_item("parse_errors", _format_counter(parser_stats.get("parseErrors")), warning=_safe_counter_positive(parser_stats.get("parseErrors"))),
        _status_item("csv_rows", _format_counter(runtime_stats.get("csvRowsWritten"))),
        _status_item("last_error", _first_non_empty(runtime_stats.get("lastCsvError", ""), connection_status.get("lastError", ""), parser_stats.get("lastError", ""), "-"), warning=bool(_first_non_empty(runtime_stats.get("lastCsvError", ""), connection_status.get("lastError", ""), parser_stats.get("lastError", ""), ""))),
    ]


def _build_connection_panel(status: dict, queue_depth: int | str) -> list:
    return [
        _status_item("mode", status.get("mode", "disconnected")),
        _status_item("serial_port", status.get("serialPort") or status.get("port") or "-"),
        _status_item("baud", _dash_if_none(status.get("baud"))),
        _status_item("read_size", _dash_if_none(status.get("readSize"))),
        _status_item("connected", "yes" if status.get("serialConnected") else "no", warning=status.get("mode") == "serial" and not status.get("serialConnected")),
        _status_item("bytes_received", _format_counter(status.get("bytesReceived"))),
        _status_item("chunks_received", _format_counter(status.get("chunksReceived"))),
        _status_item("raw_lines", _format_counter(status.get("rawLinesReceived"))),
        _status_item("dropped_chunks", _format_counter(status.get("droppedInputChunks")), warning=_safe_counter_positive(status.get("droppedInputChunks"))),
        _status_item("dropped_bytes", _format_counter(status.get("droppedInputBytes")), warning=_safe_counter_positive(status.get("droppedInputBytes"))),
        _status_item("queue_depth", queue_depth),
        _status_item("last_data", _format_wall_time(status.get("lastDataTime"))),
        _status_item("reconnects", _format_counter(status.get("reconnectAttempts"))),
        _status_item("auto_reconnect", "on" if status.get("autoReconnect") else "off"),
        _status_item("dependency", status.get("dependencyMissing") or "-"),
        _status_item("last_serial_error", status.get("lastError") or "-", warning=bool(status.get("lastError"))),
    ]


def _build_device_panel(meta: dict, parser_stats: dict, latest_status: dict, events: list[dict]) -> list:
    event_rows = []
    for event in reversed(events[-12:]):
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
    stat_keys = [
        "fps",
        "pps",
        "scanAvgUs",
        "scanMaxUs",
        "drop",
        "decimated",
        "qFull",
        "drdyTimeout",
        "spiFail",
        "adsDr",
        "adsSps",
        "outputDiv",
        "status",
        "code",
    ]
    return [
        html.Div(
            [
                _status_item("lastStatusCode", f"{_format_hex(meta.get('lastStatusCode'), 4)} {meta.get('lastStatusCodeName') or '-'}", warning=_safe_counter_positive(meta.get("lastStatusCode"))),
                _status_item("statusFlags", _format_hex(meta.get("statusFlags"), 8), warning=_safe_counter_positive(meta.get("statusFlags"))),
                _status_item("droppedFrames", _format_counter(meta.get("droppedFrames")), warning=_safe_counter_positive(meta.get("droppedFrames"))),
                _status_item("outputDecimatedFrames", _format_counter(meta.get("outputDecimatedFrames")), warning=_safe_counter_positive(meta.get("outputDecimatedFrames"))),
                _status_item("last parser status", parser_stats.get("lastStatusCodeName") or "-"),
            ],
            style=STATUS_GRID_STYLE,
        ),
        html.Div(
            [_status_item(key, status_fields.get(key, "-")) for key in stat_keys],
            style={**STATUS_GRID_STYLE, "marginTop": "10px"},
        ),
        html.Div(status_text, style={**MESSAGE_STYLE, "marginTop": "10px", "whiteSpace": "pre-wrap"}),
        html.Table(
            [
                html.Thead(html.Tr([html.Th("Type", style=TABLE_HEADER_STYLE), html.Th("Code", style=TABLE_HEADER_STYLE), html.Th("Name", style=TABLE_HEADER_STYLE), html.Th("Fields", style=TABLE_HEADER_STYLE)])),
                html.Tbody(event_rows),
            ],
            style=TABLE_STYLE,
        ),
    ]


def _status_item(label: str, value: Any, warning: bool = False) -> html.Div:
    style = dict(STATUS_ITEM_STYLE)
    if warning:
        style.update(WARNING_STATUS_ITEM_STYLE)
    return html.Div([html.Div(label, style=STATUS_LABEL_STYLE), html.Div(str(_dash_if_missing(value)), style=STATUS_VALUE_STYLE)], style=style)


def _connection_label(status: dict, paused: bool) -> str:
    prefix = "Paused / " if paused else ""
    mode = status.get("mode", "disconnected")
    if mode == "replay":
        suffix = "finished" if status.get("replayFinished") else "running"
        return f"{prefix}Replay ({suffix})"
    if mode == "serial":
        return f"{prefix}Connected" if status.get("serialConnected") else f"{prefix}Disconnected"
    return f"{prefix}Disconnected"


def _connection_tone(status: dict) -> str:
    if status.get("lastError"):
        return "error"
    if status.get("serialConnected") or status.get("mode") == "replay":
        return "ok"
    return "neutral"


def _resolve_color_range(matrix: np.ndarray, color_mode: str, fixed_min: Any, fixed_max: Any) -> tuple[float | None, float | None]:
    finite_values = matrix[np.isfinite(matrix)]
    mode = str(color_mode or "auto").lower()
    if mode == "fixed":
        zmin = _safe_float(fixed_min, default=None)
        zmax = _safe_float(fixed_max, default=None)
        if zmin is not None and zmax is not None and zmin < zmax:
            return zmin, zmax
    if not finite_values.size:
        return None, None
    if mode == "symmetric":
        max_abs = float(np.max(np.abs(finite_values)))
        if max_abs > 0:
            return -max_abs, max_abs
        return -1e-9, 1e-9
    vmin = float(np.min(finite_values))
    vmax = float(np.max(finite_values))
    if math.isclose(vmin, vmax, rel_tol=0.0, abs_tol=1e-12):
        margin = max(abs(vmin) * 0.05, 1e-9)
    else:
        margin = (vmax - vmin) * 0.05
    return vmin - margin, vmax + margin


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
    if not _is_finite_number(value):
        return "NaN"
    parsed = float(value)
    if math.isclose(parsed, round(parsed), rel_tol=0.0, abs_tol=1e-9):
        value_text = f"{int(round(parsed)):,}"
    else:
        value_text = f"{parsed:,.3g}"
    return f"{value_text} {unit}".strip()


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


def _requested_frame_type(frame_type: str | None) -> str:
    if not frame_type or str(frame_type).strip().lower() == "auto":
        return "FAST_BINARY"
    return str(frame_type)


def _frame_type_options(frame_types: list[str]) -> list[dict]:
    return [{"label": frame_type, "value": frame_type} for frame_type in list(dict.fromkeys(["Auto", *DEFAULT_FRAME_TYPES, *frame_types]))]


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
        {"label": "Fixed", "value": "fixed"},
        {"label": "Symmetric", "value": "symmetric"},
    ]
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


MISSING_DISPLAY_VALUES = {"", "-", "--", "—", "n/a", "na", "nan", "none", "null"}


def _is_missing_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in MISSING_DISPLAY_VALUES
    try:
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, float):
            return math.isnan(value)
        return bool(np.isscalar(value) and np.isnan(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _is_finite_number(value: Any) -> bool:
    if _is_missing_value(value):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _safe_int(value: Any, default: int | None = 0) -> int | None:
    if _is_missing_value(value):
        return default
    try:
        if isinstance(value, str):
            text = value.strip()
            if text.lower().startswith(("0x", "+0x", "-0x")):
                return int(text, 0)
            parsed = float(text) if any(char in text for char in ".eE") else int(text, 10)
            if isinstance(parsed, float):
                if not math.isfinite(parsed):
                    return default
                return int(parsed)
            return parsed
        if isinstance(value, (np.integer,)):
            return int(value)
        if isinstance(value, (float, np.floating)):
            parsed_float = float(value)
            return int(parsed_float) if math.isfinite(parsed_float) else default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value: Any, default: float | None = 0.0) -> float | None:
    if _is_missing_value(value):
        return default
    try:
        if isinstance(value, str):
            text = value.strip()
            parsed = float(int(text, 0)) if text.lower().startswith(("0x", "+0x", "-0x")) else float(text)
        else:
            parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _safe_counter_positive(value: Any) -> bool:
    return _safe_int(value, default=0) > 0


def _format_counter(value: Any, missing: str = "-") -> str:
    if _is_missing_value(value):
        return missing
    parsed = _safe_int(value, default=None)
    if parsed is None:
        return str(value)
    return str(parsed)


def _render_interval_for_target_fps(gui_target_fps: Any) -> int:
    target = _safe_int(gui_target_fps, default=DEFAULT_GUI_TARGET_FPS) or DEFAULT_GUI_TARGET_FPS
    return 33 if target <= 30 else DEFAULT_RENDER_INTERVAL_MS


def _target_fps(render_mode: str | None, gui_target_fps: Any) -> int:
    explicit = _safe_int(gui_target_fps, default=None)
    if explicit in (30, 60):
        return explicit
    if render_mode == "Performance":
        return 60
    return DEFAULT_GUI_TARGET_FPS


def _history_points_limit(value: Any, render_mode: str | None) -> int:
    if str(value or "").lower() == "auto":
        return 5000 if render_mode == "Quality" else 1200
    parsed = _safe_int(value, default=None)
    return max(100, parsed or 1200)


def _render_runtime_stats(heatmap_cache: HeatmapRenderCacheThread, history_cache: HistoryRenderCacheThread, frontend_stats: dict) -> dict:
    heatmap_stats = heatmap_cache.getStats()
    history_stats = history_cache.getStats()
    heatmap_snapshot = heatmap_cache.getLatest() or {}
    heatmap_fps = _safe_float(_first_non_missing(frontend_stats.get("heatmapActualFps"), heatmap_stats.get("actualFps")), default=0.0)
    history_fps = _safe_float(_first_non_missing(frontend_stats.get("historyActualFps"), history_stats.get("actualFps")), default=0.0)
    visual_fps = _safe_float(frontend_stats.get("visualUpdateFps"), default=0.0)
    browser_raf_fps = _safe_float(frontend_stats.get("browserRafFps"), default=0.0)
    callback_fps = _safe_float(frontend_stats.get("callbackFps"), default=0.0)
    frontend_dropped = _safe_int(frontend_stats.get("droppedFrames"), default=0)
    frontend_coalesced = (
        _safe_int(frontend_stats.get("coalescedFrames"), default=0)
        + _safe_int(frontend_stats.get("coalescedHistoryUpdates"), default=0)
        + _safe_int(frontend_stats.get("coalescedHeatmapUpdates"), default=0)
    )
    cache_skipped = _safe_int(heatmap_stats.get("renderSkipped"), default=0) + _safe_int(history_stats.get("renderSkipped"), default=0)
    return {
        "guiHeatmapFps": heatmap_fps,
        "guiHistoryFps": history_fps,
        "visualUpdateFps": visual_fps,
        "browserRafFps": browser_raf_fps,
        "callbackFps": callback_fps,
        "renderSkipped": frontend_dropped + cache_skipped,
        "frontendCoalescedFrames": frontend_coalesced,
        "frontendDroppedFrames": frontend_dropped,
        "renderCacheSkipped": cache_skipped,
        "lastClientError": frontend_stats.get("lastClientError", ""),
        "lastHistoryError": frontend_stats.get("lastHistoryError", ""),
        "lastHeatmapError": frontend_stats.get("lastHeatmapError", ""),
        "heatmapUnit": heatmap_snapshot.get("unit") or "-",
        "heatmapFiniteMin": heatmap_snapshot.get("finiteMin"),
        "heatmapFiniteMax": heatmap_snapshot.get("finiteMax"),
        "heatmapZMin": heatmap_snapshot.get("zmin"),
        "heatmapZMax": heatmap_snapshot.get("zmax"),
        "heatmapColorMode": heatmap_snapshot.get("colorMode") or "-",
    }


def _fmt_rate(value: Any) -> str:
    parsed = _safe_float(value, default=None)
    if parsed is None:
        return "-"
    return f"{parsed:.1f}"


def _format_wall_time(timestamp: float | None) -> str:
    parsed = _safe_float(timestamp, default=None)
    if parsed is None or parsed <= 0:
        return "-"
    return datetime.fromtimestamp(parsed).strftime("%H:%M:%S")


def _clock_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _dash_if_none(value: Any) -> Any:
    return "-" if value is None else value


def _dash_if_missing(value: Any) -> Any:
    return "-" if _is_missing_value(value) else value


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value:
            return value
    return "-"


def _first_non_missing(*values: Any) -> Any:
    for value in values:
        if not _is_missing_value(value):
            return value
    return "-"


def _format_hex(value: Any, width: int = 4) -> str:
    if _is_missing_value(value):
        return "-"
    parsed = _safe_int(value, default=None)
    if parsed is None:
        return str(value)
    return f"0x{parsed:0{width}X}"


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
