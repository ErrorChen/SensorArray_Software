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
    DEFAULT_REFRESH_INTERVAL_MS,
    DEFAULT_SERIAL_READ_SIZE,
    DETECTOR_LABELS,
    DISPLAY_DOWNSAMPLE_TARGET_POINTS,
    MATRIX_SIZE,
    SOURCE_LABELS,
)
from .connection_manager import ConnectionManager
from .data_store import CsvFrameWriter, MatrixDataStore
from .protocol_parser import SensorArrayStreamParser

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

    def getStats(self) -> dict:
        with self._lock:
            now = time.monotonic()
            self._prune_rate_samples_locked(now)
            duration = max(1e-6, now - self._rateSamples[0]["time"]) if self._rateSamples else 1.0
            bytes_in_window = sum(sample["bytes"] for sample in self._rateSamples)
            binary_in_window = sum(sample["binary"] for sample in self._rateSamples)
            text_in_window = sum(sample["text"] for sample in self._rateSamples)
            display_in_window = sum(sample["display"] for sample in self._rateSamples)
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
                "guiDisplayedFps": display_in_window / duration,
                "latestSeq": self.latestSeq,
                "seqGap": self.seqGap,
            }

    def _prune_rate_samples_locked(self, now: float) -> None:
        cutoff = now - 5.0
        while self._rateSamples and self._rateSamples[0]["time"] < cutoff:
            self._rateSamples.popleft()


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

    app.layout = html.Div(
        [
            dcc.Store(id="selected-cell-store", data=DEFAULT_SELECTED_CELL),
            dcc.Store(id="paused-store", data=False),
            dcc.Interval(id="refresh-interval", interval=DEFAULT_REFRESH_INTERVAL_MS, n_intervals=0),
            html.Div(
                [
                    html.Div([html.H1("SensorArray Matrix Viewer", style=TITLE_STYLE), html.Div(id="status-bar", style=STATUS_GRID_STYLE)]),
                    html.Div(
                        [
                            html.Button("Pause", id="pause-button", n_clicks=0, style=BUTTON_STYLE),
                            html.Button("Clear History", id="clear-button", n_clicks=0, style=SECONDARY_BUTTON_STYLE),
                            html.Button("Save Snapshot CSV", id="save-button", n_clicks=0, style=SECONDARY_BUTTON_STYLE),
                        ],
                        style=BUTTON_ROW_STYLE,
                    ),
                ],
                style=TOP_GRID_STYLE,
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.H2("Connection", style=SECTION_TITLE_STYLE),
                            html.Div(
                                [
                                    _control("Input Mode", dcc.Dropdown(id="input-mode", value="serial", clearable=False, options=[
                                        {"label": "Serial", "value": "serial"},
                                        {"label": "Replay File", "value": "replay"},
                                        {"label": "Disconnected", "value": "disconnected"},
                                    ])),
                                    _control("COM Port", dcc.Dropdown(id="com-port-dropdown", options=[], placeholder="Refresh ports")),
                                    html.Button("Refresh Ports", id="refresh-ports-button", n_clicks=0, style=SECONDARY_BUTTON_STYLE),
                                    _control("Baudrate", dcc.Input(id="baud-input", type="number", value=DEFAULT_BAUD, min=1, step=1, style=INPUT_STYLE)),
                                    _control("Read size", dcc.Input(id="read-size-input", type="number", value=DEFAULT_SERIAL_READ_SIZE, min=4096, step=1024, style=INPUT_STYLE)),
                                    _control("Auto reconnect", dcc.Checklist(id="auto-reconnect", value=[], options=[{"label": "Enabled", "value": "enabled"}], style=CHECKLIST_STYLE)),
                                    html.Button("Connect", id="connect-button", n_clicks=0, style=BUTTON_STYLE),
                                    html.Button("Disconnect", id="disconnect-button", n_clicks=0, style=SECONDARY_BUTTON_STYLE),
                                    html.Button("Reconnect", id="reconnect-button", n_clicks=0, style=SECONDARY_BUTTON_STYLE),
                                    _control("Replay file", dcc.Input(id="replay-file-input", type="text", debounce=True, style=INPUT_STYLE)),
                                    _control("Replay speed", dcc.Input(id="replay-speed-input", type="number", value=1.0, min=0.01, step=0.5, style=INPUT_STYLE)),
                                    html.Button("Start Replay", id="start-replay-button", n_clicks=0, style=SECONDARY_BUTTON_STYLE),
                                ],
                                style=CONNECTION_GRID_STYLE,
                            ),
                            html.Div(id="port-refresh-status", style=MESSAGE_STYLE),
                            html.Div(id="connection-action-status", style=MESSAGE_STYLE),
                            html.Div(id="connection-status-panel", style=STATUS_GRID_STYLE),
                        ],
                        style=PANEL_STYLE,
                    ),
                ],
                style={"marginTop": "16px"},
            ),
            html.Div(
                [
                    _control("Data stream", dcc.Dropdown(id="frame-type-dropdown", value="FAST_BINARY", clearable=False, options=_frame_type_options(DEFAULT_FRAME_TYPES))),
                    _control("Cell", dcc.Dropdown(id="cell-dropdown", value=DEFAULT_SELECTED_CELL, clearable=False, options=[{"label": cell, "value": cell} for cell in CELL_NAMES])),
                    _control("X axis", dcc.Dropdown(id="x-axis", value="timeSeconds", clearable=False, options=[
                        {"label": "timeSeconds", "value": "timeSeconds"},
                        {"label": "timestampUs", "value": "timestampUs"},
                        {"label": "seq", "value": "seq"},
                    ])),
                    _control("History window", dcc.Dropdown(id="history-window", value="last_30s", clearable=False, options=[
                        {"label": "All", "value": "all"},
                        {"label": "Last 10 s", "value": "last_10s"},
                        {"label": "Last 30 s", "value": "last_30s"},
                        {"label": "Last 60 s", "value": "last_60s"},
                        {"label": "Last 5 min", "value": "last_5min"},
                        {"label": "Last N points", "value": "last_n"},
                        {"label": "Custom range", "value": "custom"},
                    ])),
                    _control("Last N points", dcc.Input(id="last-n-points", type="number", value=1000, min=1, step=100, style=INPUT_STYLE)),
                    _control("Custom x min", dcc.Input(id="custom-x-min", type="number", debounce=True, style=INPUT_STYLE)),
                    _control("Custom x max", dcc.Input(id="custom-x-max", type="number", debounce=True, style=INPUT_STYLE)),
                    _control("Auto follow latest", dcc.Checklist(id="auto-follow", value=["enabled"], options=[{"label": "Enabled", "value": "enabled"}], style=CHECKLIST_STYLE)),
                    _control("Color scale", dcc.Dropdown(id="color-mode", value="auto", clearable=False, options=[
                        {"label": "Auto", "value": "auto"},
                        {"label": "Symmetric around zero", "value": "symmetric"},
                        {"label": "Fixed range", "value": "fixed"},
                    ])),
                    _control("Fixed min", dcc.Input(id="fixed-min", type="number", debounce=True, style=INPUT_STYLE)),
                    _control("Fixed max", dcc.Input(id="fixed-max", type="number", debounce=True, style=INPUT_STYLE)),
                    _control("Display unit", dcc.Dropdown(id="unit-mode", value="auto", clearable=False, options=[
                        {"label": "Auto uV/mV/V", "value": "auto"},
                        {"label": "Source unit", "value": "source"},
                        {"label": "uV", "value": "uV"},
                        {"label": "mV", "value": "mV"},
                        {"label": "V", "value": "V"},
                    ])),
                    _control("Refresh ms", dcc.Input(id="interval-ms", type="number", value=DEFAULT_REFRESH_INTERVAL_MS, min=100, step=100, debounce=True, style=INPUT_STYLE)),
                ],
                style=CONTROL_GRID_STYLE,
            ),
            html.Div([html.Div(id="save-status", style=MESSAGE_STYLE), html.Div(id="clear-status", style=MESSAGE_STYLE), html.Div(id="history-stats", style=MESSAGE_STYLE)], style=MESSAGE_ROW_STYLE),
            html.Div(
                [
                    html.Div(dcc.Graph(id="heatmap", config={"displayModeBar": True, "responsive": True}, style={"height": "620px"}), style=PANEL_STYLE),
                    html.Div(dcc.Graph(id="history-graph", config={"displayModeBar": True, "scrollZoom": True, "responsive": True}, style={"height": "620px"}), style=PANEL_STYLE),
                ],
                style=MAIN_GRID_STYLE,
            ),
            html.Div([html.H2("Device Status", style=SECTION_TITLE_STYLE), html.Div(id="device-status-panel")], style={**PANEL_STYLE, "marginTop": "16px"}),
        ],
        style=PAGE_STYLE,
    )

    @app.callback(Output("refresh-interval", "interval"), Input("interval-ms", "value"))
    def update_interval(interval_ms: Any) -> int:
        try:
            return max(100, min(10_000, int(interval_ms)))
        except (TypeError, ValueError):
            return DEFAULT_REFRESH_INTERVAL_MS

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

    @app.callback(Output("selected-cell-store", "data"), Input("cell-dropdown", "value"), State("selected-cell-store", "data"))
    def select_cell_from_dropdown(cell_name: str | None, current_cell: str | None) -> str:
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
                manager.reconnect()
                return "Reconnect requested."
            if triggered == "start-replay-button" or input_mode == "replay":
                manager.startReplay(
                    str(replay_file or ""),
                    float(replay_speed or 1.0),
                    int(read_size or DEFAULT_SERIAL_READ_SIZE),
                )
                return "Replay started."
            if input_mode == "disconnected":
                manager.disconnect()
                return "Disconnected."
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

    @app.callback(Output("frame-type-dropdown", "options"), Input("refresh-interval", "n_intervals"))
    def update_frame_type_options(_n_intervals: int) -> list[dict]:
        return _frame_type_options(dataStore.getAvailableFrameTypes())

    @app.callback(
        Output("heatmap", "figure"),
        Output("history-graph", "figure"),
        Output("status-bar", "children"),
        Output("connection-status-panel", "children"),
        Output("device-status-panel", "children"),
        Output("history-stats", "children"),
        Input("refresh-interval", "n_intervals"),
        Input("paused-store", "data"),
        Input("selected-cell-store", "data"),
        Input("color-mode", "value"),
        Input("fixed-min", "value"),
        Input("fixed-max", "value"),
        Input("unit-mode", "value"),
        Input("frame-type-dropdown", "value"),
        Input("x-axis", "value"),
        Input("history-window", "value"),
        Input("last-n-points", "value"),
        Input("custom-x-min", "value"),
        Input("custom-x-max", "value"),
        Input("auto-follow", "value"),
    )
    def refresh_view(
        _n_intervals: int,
        paused: bool,
        selected_cell: str | None,
        color_mode: str,
        fixed_min: Any,
        fixed_max: Any,
        unit_mode: str,
        frame_type: str | None,
        x_axis: str,
        history_window: str,
        last_n: Any,
        custom_min: Any,
        custom_max: Any,
        auto_follow_values: list[str] | None,
    ) -> tuple[go.Figure, go.Figure, list, list, list, str]:
        selected_cell = selected_cell if selected_cell in CELL_NAMES else DEFAULT_SELECTED_CELL
        selected_type = frame_type or "FAST_BINARY"
        display_type = dataStore.resolveFrameType(selected_type)
        fallback_active = selected_type == "FAST_BINARY" and display_type != selected_type
        latest_matrix_raw = dataStore.getLatestMatrix(selected_type)
        latest_meta = dataStore.getLatestFrameMeta(selected_type)
        runtime_state.recordDisplayFrame(latest_meta, bool(paused))
        parser_stats = parser.getStats()
        connection_status = manager.getStatus()
        runtime_stats = runtime_state.getStats()
        display_matrix, display_unit = _convert_matrix_for_display(latest_matrix_raw, latest_meta.get("unit") or "", unit_mode)

        heatmap = _build_heatmap_figure(display_matrix, latest_meta, selected_cell, color_mode, fixed_min, fixed_max, display_unit, display_type)

        history_raw = dataStore.getCellHistory(
            selected_type,
            selected_cell,
            xAxis=x_axis,
            windowMode=history_window,
            lastN=_safe_int(last_n),
            customMin=_safe_float(custom_min),
            customMax=_safe_float(custom_max),
        )
        history_rendered, downsampled = MatrixDataStore.downsampleHistoryFrame(history_raw, x_axis, DISPLAY_DOWNSAMPLE_TARGET_POINTS)
        auto_follow = "enabled" in (auto_follow_values or [])
        history = _build_history_figure(
            selected_cell,
            display_type,
            history_raw,
            history_rendered,
            x_axis,
            unit_mode,
            auto_follow,
            history_window,
            _safe_int(last_n),
            _safe_float(custom_min),
            _safe_float(custom_max),
        )
        status = _build_status_bar(latest_meta, parser_stats, connection_status, runtime_stats, paused, _safe_qsize(inputQueue), display_unit)
        connection_panel = _build_connection_panel(connection_status, _safe_qsize(inputQueue))
        device_panel = _build_device_panel(latest_meta, parser_stats, dataStore.getLatestDeviceStatus(), dataStore.getRecentDeviceEvents(50))
        fallback_text = f" | fallback: {selected_type}->{display_type}" if fallback_active else ""
        history_stats = (
            f"stream: {display_type} (requested {selected_type}){fallback_text} | "
            f"cell: {selected_cell} | "
            f"x: {x_axis} | "
            f"window: {history_window} | "
            f"visible raw points: {len(history_raw)} | "
            f"rendered points: {len(history_rendered)} | "
            f"downsampled: {'yes' if downsampled else 'no'} | "
            f"auto follow: {'yes' if auto_follow else 'no'} | "
            f"parsed binary fps: {runtime_stats.get('parsedBinaryFps', 0.0):.1f} | "
            f"gui displayed fps: {runtime_stats.get('guiDisplayedFps', 0.0):.1f}"
        )
        return heatmap, history, status, connection_panel, device_panel, history_stats

    return app


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
    text = np.array([[f"{cell_names[row, col]}<br>{_format_value(matrix[row, col], unit) if np.isfinite(matrix[row, col]) else 'invalid'}" for col in range(MATRIX_SIZE)] for row in range(MATRIX_SIZE)])

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
                    "state=%{customdata[1]}<br>"
                    "value=%{z:,.6g}<br>"
                    f"unit={unit or '-'}<br>"
                    f"seq={_dash_if_none(meta.get('seq'))}<br>"
                    f"statusFlags={_format_hex(meta.get('statusFlags'), 8)}<extra></extra>"
                ),
                colorscale="RdYlBu_r",
                colorbar={"title": unit or "value"},
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

    fig.update_layout(**_base_figure_layout("8x8 Matrix"), clickmode="event+select")
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
            mode="lines+markers",
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
    interaction_mode = "follow" if auto_follow else "manual"
    x_window_key = window_mode or "all"
    if window_mode == "last_n":
        x_window_key = f"last_n:{last_n or 1000}"
    elif window_mode == "custom":
        x_window_key = f"custom:{custom_min}:{custom_max}"

    return {
        "layout": f"history:{interaction_mode}",
        "x": f"history:x:{frame_type}:{x_axis}:{x_window_key}:{interaction_mode}",
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
    return [
        _status_item("mode", status.get("mode", "disconnected")),
        _status_item("serial_port", status.get("serialPort") or status.get("port") or "-"),
        _status_item("baud", _dash_if_none(status.get("baud"))),
        _status_item("read_size", _dash_if_none(status.get("readSize"))),
        _status_item("connected", "yes" if status.get("serialConnected") else "no", warning=status.get("mode") == "serial" and not status.get("serialConnected")),
        _status_item("bytes_received", status.get("bytesReceived", 0)),
        _status_item("chunks_received", status.get("chunksReceived", 0)),
        _status_item("raw_lines", status.get("rawLinesReceived", 0)),
        _status_item("dropped_chunks", status.get("droppedInputChunks", 0), warning=status.get("droppedInputChunks", 0) > 0),
        _status_item("dropped_bytes", status.get("droppedInputBytes", 0), warning=status.get("droppedInputBytes", 0) > 0),
        _status_item("queue_depth", queue_depth),
        _status_item("last_data", _format_wall_time(status.get("lastDataTime"))),
        _status_item("reconnects", status.get("reconnectAttempts", 0)),
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
                _status_item("lastStatusCode", f"{_format_hex(meta.get('lastStatusCode'), 4)} {meta.get('lastStatusCodeName') or '-'}", warning=bool(meta.get("lastStatusCode"))),
                _status_item("statusFlags", _format_hex(meta.get("statusFlags"), 8), warning=bool(meta.get("statusFlags"))),
                _status_item("droppedFrames", _dash_if_none(meta.get("droppedFrames")), warning=bool(meta.get("droppedFrames"))),
                _status_item("outputDecimatedFrames", _dash_if_none(meta.get("outputDecimatedFrames")), warning=bool(meta.get("outputDecimatedFrames"))),
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
    return str(unit or "").strip().replace("碌", "u").replace("渭", "u").lower()


def _format_value(value: float, unit: str = "") -> str:
    if not np.isfinite(value):
        return "NaN"
    if math.isclose(float(value), round(float(value)), rel_tol=0.0, abs_tol=1e-9):
        value_text = f"{int(round(float(value))):,}"
    else:
        value_text = f"{float(value):,.3g}"
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
    return html.Div([html.Label(label, style=LABEL_STYLE), child], style=CONTROL_ITEM_STYLE)


def _frame_type_options(frame_types: list[str]) -> list[dict]:
    return [{"label": frame_type, "value": frame_type} for frame_type in list(dict.fromkeys([*DEFAULT_FRAME_TYPES, *frame_types]))]


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
