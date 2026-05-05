from __future__ import annotations

import logging
import math
import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import plotly.graph_objects as go
from dash import Dash, Input, Output, State, dcc, html, no_update

from .config import (
    CELL_NAMES,
    DEFAULT_REFRESH_INTERVAL_MS,
    DETECTOR_LABELS,
    MATRIX_SIZE,
    SOURCE_LABELS,
)
from .data_store import CsvFrameWriter, MatrixDataStore
from .matv_parser import MatvParser

LOGGER = logging.getLogger(__name__)
DEFAULT_SELECTED_CELL = "S1D1"


class RuntimeState:
    def __init__(self):
        self._lock = threading.Lock()
        self.csvRowsWritten = 0
        self.lastCsvError = ""

    def recordCsvWrite(self) -> None:
        with self._lock:
            self.csvRowsWritten += 1

    def recordCsvError(self, message: str) -> None:
        with self._lock:
            self.lastCsvError = message

    def getStats(self) -> dict:
        with self._lock:
            return {
                "csvRowsWritten": self.csvRowsWritten,
                "lastCsvError": self.lastCsvError,
            }


def createDashApp(
    lineQueue: "queue.Queue[str]",
    parser: MatvParser,
    dataStore: MatrixDataStore,
    reader: Any | None = None,
    csvWriter: CsvFrameWriter | None = None,
    maxLinesPerTick: int = 1000,
) -> Dash:
    runtime_state = RuntimeState()
    app = Dash(__name__, title="Matrix Log Viewer")

    app.layout = html.Div(
        [
            dcc.Store(id="selected-cell-store", data=DEFAULT_SELECTED_CELL),
            dcc.Store(id="paused-store", data=False),
            dcc.Interval(
                id="refresh-interval",
                interval=DEFAULT_REFRESH_INTERVAL_MS,
                n_intervals=0,
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.H1("Matrix Log Viewer", style=TITLE_STYLE),
                            html.Div(id="status-bar", style=STATUS_GRID_STYLE),
                        ],
                        style={"minWidth": 0},
                    ),
                    html.Div(
                        [
                            html.Button("Pause", id="pause-button", n_clicks=0, style=BUTTON_STYLE),
                            html.Button(
                                "Clear History",
                                id="clear-button",
                                n_clicks=0,
                                style=SECONDARY_BUTTON_STYLE,
                            ),
                            html.Button(
                                "Save Snapshot CSV",
                                id="save-button",
                                n_clicks=0,
                                style=SECONDARY_BUTTON_STYLE,
                            ),
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
                            html.Label("Color scale", style=LABEL_STYLE),
                            dcc.Dropdown(
                                id="color-mode",
                                value="auto",
                                clearable=False,
                                options=[
                                    {"label": "Auto", "value": "auto"},
                                    {"label": "Symmetric around zero", "value": "symmetric"},
                                    {"label": "Fixed range", "value": "fixed"},
                                ],
                            ),
                        ],
                        style=CONTROL_ITEM_STYLE,
                    ),
                    html.Div(
                        [
                            html.Label("Fixed min", style=LABEL_STYLE),
                            dcc.Input(id="fixed-min", type="number", debounce=True, style=INPUT_STYLE),
                        ],
                        style=CONTROL_ITEM_STYLE,
                    ),
                    html.Div(
                        [
                            html.Label("Fixed max", style=LABEL_STYLE),
                            dcc.Input(id="fixed-max", type="number", debounce=True, style=INPUT_STYLE),
                        ],
                        style=CONTROL_ITEM_STYLE,
                    ),
                    html.Div(
                        [
                            html.Label("Refresh ms", style=LABEL_STYLE),
                            dcc.Input(
                                id="interval-ms",
                                type="number",
                                value=DEFAULT_REFRESH_INTERVAL_MS,
                                min=100,
                                step=100,
                                debounce=True,
                                style=INPUT_STYLE,
                            ),
                        ],
                        style=CONTROL_ITEM_STYLE,
                    ),
                ],
                style=CONTROL_GRID_STYLE,
            ),
            html.Div(
                [
                    html.Div(id="save-status", style=MESSAGE_STYLE),
                    html.Div(id="clear-status", style=MESSAGE_STYLE),
                ],
                style=MESSAGE_ROW_STYLE,
            ),
            html.Div(
                [
                    html.Div(
                        dcc.Graph(
                            id="heatmap",
                            config={"displayModeBar": True, "responsive": True},
                            style={"height": "620px"},
                        ),
                        style=PANEL_STYLE,
                    ),
                    html.Div(
                        dcc.Graph(
                            id="history-graph",
                            config={"displayModeBar": True, "responsive": True},
                            style={"height": "620px"},
                        ),
                        style=PANEL_STYLE,
                    ),
                ],
                style=MAIN_GRID_STYLE,
            ),
        ],
        style=PAGE_STYLE,
    )

    @app.callback(
        Output("refresh-interval", "interval"),
        Input("interval-ms", "value"),
    )
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

    @app.callback(
        Output("selected-cell-store", "data"),
        Input("heatmap", "clickData"),
        State("selected-cell-store", "data"),
    )
    def select_cell(click_data: dict | None, current_cell: str | None) -> str:
        if not click_data:
            return current_cell or DEFAULT_SELECTED_CELL

        try:
            custom_data = click_data["points"][0].get("customdata")
            cell_name = custom_data[0] if isinstance(custom_data, list) else custom_data
        except Exception:
            return current_cell or DEFAULT_SELECTED_CELL

        if cell_name in CELL_NAMES:
            return cell_name
        return current_cell or DEFAULT_SELECTED_CELL

    @app.callback(
        Output("clear-status", "children"),
        Input("clear-button", "n_clicks"),
        prevent_initial_call=True,
    )
    def clear_history(n_clicks: int | None) -> str:
        if not n_clicks:
            return no_update
        dataStore.clear()
        return f"History cleared at {_clock_text()}."

    @app.callback(
        Output("save-status", "children"),
        Input("save-button", "n_clicks"),
        prevent_initial_call=True,
    )
    def save_snapshot(n_clicks: int | None) -> str:
        if not n_clicks:
            return no_update

        snapshot = dataStore.toWideDataFrame()
        if snapshot.empty:
            return "No data to save."

        export_dir = Path.cwd() / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        output_path = export_dir / f"matrix_snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        snapshot.to_csv(output_path, index=False)
        return f"Saved {len(snapshot)} rows to {output_path}"

    @app.callback(
        Output("heatmap", "figure"),
        Output("history-graph", "figure"),
        Output("status-bar", "children"),
        Input("refresh-interval", "n_intervals"),
        Input("paused-store", "data"),
        Input("selected-cell-store", "data"),
        Input("color-mode", "value"),
        Input("fixed-min", "value"),
        Input("fixed-max", "value"),
    )
    def refresh_view(
        _n_intervals: int,
        paused: bool,
        selected_cell: str | None,
        color_mode: str,
        fixed_min: Any,
        fixed_max: Any,
    ) -> tuple[go.Figure, go.Figure, list]:
        if not paused:
            _drain_queue(lineQueue, parser, dataStore, csvWriter, runtime_state, maxLinesPerTick)

        selected_cell = selected_cell if selected_cell in CELL_NAMES else DEFAULT_SELECTED_CELL
        latest_matrix = dataStore.getLatestMatrix()
        latest_meta = dataStore.getLatestFrameMeta()
        parser_stats = parser.getStats()
        reader_status = reader.getStatus() if reader is not None else {}
        runtime_stats = runtime_state.getStats()

        heatmap = _build_heatmap_figure(
            latest_matrix,
            latest_meta,
            selected_cell,
            color_mode,
            fixed_min,
            fixed_max,
        )
        history = _build_history_figure(selected_cell, dataStore.getCellHistory(selected_cell))
        status = _build_status_bar(
            latest_meta,
            parser_stats,
            reader_status,
            runtime_stats,
            paused,
            _safe_qsize(lineQueue),
        )
        return heatmap, history, status

    return app


def _drain_queue(
    line_queue: "queue.Queue[str]",
    parser: MatvParser,
    data_store: MatrixDataStore,
    csv_writer: CsvFrameWriter | None,
    runtime_state: RuntimeState,
    max_lines: int,
) -> None:
    processed = 0
    while processed < max_lines:
        try:
            line = line_queue.get_nowait()
        except queue.Empty:
            break

        processed += 1
        frame = parser.parseLine(line)
        if frame is None:
            continue

        data_store.addFrame(frame)
        if csv_writer is None:
            continue

        try:
            csv_writer.appendFrame(frame)
            runtime_state.recordCsvWrite()
        except Exception as exc:
            runtime_state.recordCsvError(str(exc))
            LOGGER.warning("CSV append failed: %s", exc)


def _build_heatmap_figure(
    matrix: np.ndarray,
    meta: dict,
    selected_cell: str,
    color_mode: str,
    fixed_min: Any,
    fixed_max: Any,
) -> go.Figure:
    unit = meta.get("unit") or ""
    custom_data = np.array(
        [[f"S{row_index + 1}D{column_index + 1}" for column_index in range(MATRIX_SIZE)] for row_index in range(MATRIX_SIZE)]
    )
    text = np.array(
        [
            [
                f"{custom_data[row_index, column_index]}<br>{_format_value(matrix[row_index, column_index], unit)}"
                for column_index in range(MATRIX_SIZE)
            ]
            for row_index in range(MATRIX_SIZE)
        ]
    )

    heatmap_kwargs: dict[str, Any] = {}
    zmin, zmax = _resolve_color_range(matrix, color_mode, fixed_min, fixed_max)
    if zmin is not None and zmax is not None:
        heatmap_kwargs["zmin"] = zmin
        heatmap_kwargs["zmax"] = zmax

    seq_text = _dash_if_none(meta.get("seq"))
    timestamp_text = _dash_if_none(meta.get("timestampUs"))
    duration_text = _dash_if_none(meta.get("durationUs"))

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
                    "cell=%{customdata}<br>"
                    "value=%{z:,.6g}<br>"
                    f"unit={unit or '-'}<br>"
                    f"seq={seq_text}<br>"
                    f"timestamp_us={timestamp_text}<br>"
                    f"duration_us={duration_text}<extra></extra>"
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
                marker={
                    "symbol": "square-open",
                    "size": 62,
                    "line": {"color": "#111827", "width": 3},
                },
                customdata=[selected_cell],
                hovertemplate="cell=%{customdata}<extra></extra>",
                showlegend=False,
            )
        )

    fig.update_layout(
        title="8x8 Matrix",
        margin={"l": 48, "r": 24, "t": 56, "b": 36},
        paper_bgcolor="white",
        plot_bgcolor="white",
        font={"family": "Segoe UI, Arial, sans-serif", "size": 12, "color": "#17202a"},
        clickmode="event+select",
    )
    fig.update_xaxes(side="top", constrain="domain")
    fig.update_yaxes(autorange="reversed", scaleanchor="x", scaleratio=1)
    return fig


def _build_history_figure(cell_name: str, history) -> go.Figure:
    fig = go.Figure()
    if history.empty:
        fig.add_annotation(
            text="No data yet",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font={"size": 18, "color": "#6b7280"},
        )
        title = f"History of {cell_name}"
        y_title = "value"
    else:
        units = [unit for unit in history["unit"].dropna().unique().tolist() if unit != ""]
        mixed_units = len(units) > 1
        unit_label = "mixed units" if mixed_units else (units[0] if units else "value")
        title = f"History of {cell_name}" + (" (Mixed units)" if mixed_units else "")
        y_title = f"value ({unit_label})" if unit_label != "value" else "value"
        custom_data = history[["seq", "timestampUs", "unit"]].to_numpy()
        fig.add_trace(
            go.Scattergl(
                x=history["timeSeconds"],
                y=history["value"],
                mode="lines+markers",
                line={"color": "#0f766e", "width": 2},
                marker={"size": 4},
                customdata=custom_data,
                hovertemplate=(
                    "time=%{x:.6f}s<br>"
                    "value=%{y:,.6g}<br>"
                    "unit=%{customdata[2]}<br>"
                    "seq=%{customdata[0]}<br>"
                    "timestamp_us=%{customdata[1]}<extra></extra>"
                ),
            )
        )

    fig.update_layout(
        title=title,
        margin={"l": 58, "r": 24, "t": 56, "b": 52},
        paper_bgcolor="white",
        plot_bgcolor="white",
        font={"family": "Segoe UI, Arial, sans-serif", "size": 12, "color": "#17202a"},
        xaxis_title="timeSeconds (s)",
        yaxis_title=y_title,
    )
    fig.update_xaxes(showgrid=True, gridcolor="#e5e7eb")
    fig.update_yaxes(showgrid=True, gridcolor="#e5e7eb")
    return fig


def _build_status_bar(
    meta: dict,
    parser_stats: dict,
    reader_status: dict,
    runtime_stats: dict,
    paused: bool,
    queue_depth: int | str,
) -> list:
    return [
        _status_item("Input", _reader_label(reader_status, paused)),
        _status_item("seq", _dash_if_none(meta.get("seq"))),
        _status_item("timestamp_us", _dash_if_none(meta.get("timestampUs"))),
        _status_item("duration_us", _dash_if_none(meta.get("durationUs"))),
        _status_item("unit", meta.get("unit") or "-"),
        _status_item("received_frames", meta.get("receivedFrames", 0)),
        _status_item("raw_lines", reader_status.get("rawLinesReceived", 0)),
        _status_item("queued_lines", queue_depth),
        _status_item("parsed_total", parser_stats.get("matvFramesParsed", 0)),
        _status_item("skipped_lines", parser_stats.get("skippedLines", 0)),
        _status_item("parse_errors", parser_stats.get("parseErrors", 0)),
        _status_item("warnings", parser_stats.get("warnings", 0)),
        _status_item("last_line", _format_wall_time(reader_status.get("lastLineTime"))),
        _status_item("csv_rows", runtime_stats.get("csvRowsWritten", 0)),
        _status_item("last_error", _first_non_empty(
            runtime_stats.get("lastCsvError", ""),
            reader_status.get("lastError", ""),
            parser_stats.get("lastError", ""),
            "-",
        )),
    ]


def _status_item(label: str, value: Any) -> html.Div:
    return html.Div(
        [html.Div(label, style=STATUS_LABEL_STYLE), html.Div(str(value), style=STATUS_VALUE_STYLE)],
        style=STATUS_ITEM_STYLE,
    )


def _reader_label(reader_status: dict, paused: bool) -> str:
    prefix = "Paused / " if paused else ""
    mode = reader_status.get("mode", "waiting")
    if mode == "replay":
        suffix = "finished" if reader_status.get("replayFinished") else "running"
        return f"{prefix}Replay File ({suffix})"
    if mode == "serial":
        if reader_status.get("serialConnected"):
            return f"{prefix}Connected"
        if reader_status.get("rawLinesReceived", 0) == 0 and not reader_status.get("lastError"):
            return f"{prefix}Waiting"
        return f"{prefix}Disconnected"
    return f"{prefix}Waiting"


def _resolve_color_range(
    matrix: np.ndarray,
    color_mode: str,
    fixed_min: Any,
    fixed_max: Any,
) -> tuple[float | None, float | None]:
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


def _format_value(value: float, unit: str = "") -> str:
    if not np.isfinite(value):
        return "NaN"
    if math.isclose(float(value), round(float(value)), rel_tol=0.0, abs_tol=1e-9):
        value_text = f"{int(round(float(value))):,}"
    else:
        value_text = f"{float(value):,.3g}"
    return f"{value_text} {unit}".strip()


def _split_cell_name(cell_name: str) -> tuple[int | None, int | None]:
    try:
        source_text, detector_text = cell_name.split("D", maxsplit=1)
        return int(source_text[1:]), int(detector_text)
    except Exception:
        return None, None


def _safe_qsize(line_queue: "queue.Queue[str]") -> int | str:
    try:
        return line_queue.qsize()
    except NotImplementedError:
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


PAGE_STYLE = {
    "minHeight": "100vh",
    "padding": "20px",
    "background": "#eef2f5",
    "color": "#17202a",
    "fontFamily": "Segoe UI, Arial, sans-serif",
    "boxSizing": "border-box",
}

TITLE_STYLE = {
    "margin": "0 0 14px 0",
    "fontSize": "30px",
    "fontWeight": 700,
    "letterSpacing": "0",
}

TOP_GRID_STYLE = {
    "display": "grid",
    "gridTemplateColumns": "minmax(360px, 1fr) auto",
    "gap": "16px",
    "alignItems": "start",
}

STATUS_GRID_STYLE = {
    "display": "grid",
    "gridTemplateColumns": "repeat(auto-fit, minmax(138px, 1fr))",
    "gap": "8px",
}

STATUS_ITEM_STYLE = {
    "background": "white",
    "border": "1px solid #d9e2ec",
    "borderRadius": "8px",
    "padding": "9px 10px",
    "minHeight": "54px",
    "boxSizing": "border-box",
}

STATUS_LABEL_STYLE = {
    "fontSize": "11px",
    "color": "#52606d",
    "lineHeight": "15px",
}

STATUS_VALUE_STYLE = {
    "fontSize": "14px",
    "fontWeight": 600,
    "lineHeight": "18px",
    "whiteSpace": "nowrap",
    "overflow": "hidden",
    "textOverflow": "ellipsis",
}

BUTTON_ROW_STYLE = {
    "display": "flex",
    "flexWrap": "wrap",
    "gap": "8px",
    "justifyContent": "flex-end",
}

BUTTON_STYLE = {
    "height": "38px",
    "border": "1px solid #0f766e",
    "background": "#0f766e",
    "color": "white",
    "borderRadius": "8px",
    "padding": "0 14px",
    "fontWeight": 700,
    "cursor": "pointer",
}

SECONDARY_BUTTON_STYLE = {
    "height": "38px",
    "border": "1px solid #a7b7c7",
    "background": "white",
    "color": "#17202a",
    "borderRadius": "8px",
    "padding": "0 14px",
    "fontWeight": 600,
    "cursor": "pointer",
}

CONTROL_GRID_STYLE = {
    "display": "grid",
    "gridTemplateColumns": "repeat(auto-fit, minmax(180px, 1fr))",
    "gap": "12px",
    "marginTop": "16px",
    "alignItems": "end",
}

CONTROL_ITEM_STYLE = {
    "minWidth": 0,
}

LABEL_STYLE = {
    "display": "block",
    "fontSize": "12px",
    "fontWeight": 700,
    "color": "#334e68",
    "marginBottom": "5px",
}

INPUT_STYLE = {
    "width": "100%",
    "height": "38px",
    "border": "1px solid #cbd5e1",
    "borderRadius": "6px",
    "padding": "0 10px",
    "boxSizing": "border-box",
}

MESSAGE_ROW_STYLE = {
    "display": "flex",
    "gap": "16px",
    "minHeight": "24px",
    "marginTop": "10px",
    "flexWrap": "wrap",
}

MESSAGE_STYLE = {
    "fontSize": "13px",
    "color": "#334e68",
}

MAIN_GRID_STYLE = {
    "display": "grid",
    "gridTemplateColumns": "repeat(auto-fit, minmax(360px, 1fr))",
    "gap": "16px",
    "marginTop": "12px",
}

PANEL_STYLE = {
    "background": "white",
    "border": "1px solid #d9e2ec",
    "borderRadius": "8px",
    "padding": "8px",
    "minWidth": 0,
}

