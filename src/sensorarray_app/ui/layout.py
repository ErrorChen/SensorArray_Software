from __future__ import annotations

from dash import dcc, html

from sensorarray_app.constants import DEFAULT_SERIAL_BAUD, DEFAULT_SERIAL_PORT
from sensorarray_app.ui.figures import empty_figure
from sensorarray_app.ui.ids import HEATMAP, RAW_LOGS, SNAPSHOT, TREND


def build_layout() -> html.Div:
    return html.Div(
        className="app-shell",
        children=[
            dcc.Store(id=SNAPSHOT),
            dcc.Interval(id="snapshot-interval", interval=250, n_intervals=0),
            html.Div(
                className="dashboard-grid",
                children=[
                    html.Section(
                        className="panel heatmap-panel",
                        children=[
                            html.Div(className="panel-title", children="8x8 Heatmap"),
                            dcc.Graph(id=HEATMAP, className="heatmap-graph", figure=empty_figure("8x8 Heatmap"), clear_on_unhover=True),
                        ],
                    ),
                    html.Section(
                        className="panel setup-panel",
                        children=[
                            html.Div(className="setup-header", children=[html.H1("SensorArray"), html.Div(id="connection-state", className="state-chip")]),
                            _connection_controls(),
                            _rows_controls(),
                            _display_controls(),
                            _baseline_controls(),
                            _battery_card(),
                            _diagnostics_card(),
                        ],
                    ),
                    html.Section(
                        className="panel trend-panel",
                        children=[
                            html.Div(id="selection-title", className="panel-title", children="Selected: S1 · Primary FDC · D1-D4"),
                            dcc.Graph(id=TREND, className="trend-graph", figure=empty_figure("Four-point Line Chart")),
                        ],
                    ),
                ],
            ),
            html.Section(
                className="panel logs-panel",
                children=[
                    html.Div(className="logs-toolbar", children=[
                        html.Div("Raw Logs", className="panel-title"),
                        dcc.Input(id="log-search", className="input", placeholder="Search", value="", debounce=True),
                        dcc.Dropdown(id="log-severity", className="input", placeholder="Severity", options=["info", "warning", "error"], value=None),
                        dcc.Checklist(id="log-show-data", options=[{"label": "Show DATA", "value": "show"}], value=[]),
                        html.Button("Clear View", id="logs-clear", className="button quiet"),
                    ]),
                    html.Pre(id=RAW_LOGS, className="raw-log-terminal"),
                ],
            ),
        ],
    )


def _connection_controls() -> html.Div:
    return html.Div(
        className="setup-section",
        children=[
            html.H2("Connection"),
            dcc.RadioItems(
                id="transport-selector",
                options=[
                    {"label": "Serial", "value": "serial"},
                    {"label": "Bluetooth LE", "value": "ble"},
                    {"label": "Wi-Fi UDP", "value": "wifi"},
                    {"label": "Replay", "value": "replay"},
                ],
                value="serial",
                inline=True,
                className="segmented",
            ),
            html.Div(
                className="control-grid",
                children=[
                    dcc.Input(id="serial-port", className="input", value=DEFAULT_SERIAL_PORT),
                    dcc.Input(id="serial-baud", className="input", type="number", value=DEFAULT_SERIAL_BAUD),
                    html.Button("Refresh", id="refresh-serial", className="button secondary"),
                    html.Button("Connect", id="connect-button", className="button primary"),
                    html.Button("Disconnect", id="disconnect-button", className="button secondary"),
                    html.Button("BLE Scan", id="ble-scan-button", className="button secondary"),
                    html.Button("Wi-Fi Discover", id="wifi-discover-button", className="button secondary"),
                ],
            ),
            dcc.Input(id="replay-file", className="input wide", placeholder="Replay file path"),
            dcc.Input(id="wifi-host", className="input wide", placeholder="Wi-Fi host fallback, e.g. 192.168.4.1"),
            dcc.Input(id="ble-address", className="input wide", placeholder="BLE address from scan result"),
            html.Div(id="discovery-results", className="small-text"),
        ],
    )


def _rows_controls() -> html.Div:
    return html.Div(
        className="setup-section",
        children=[
            html.H2("Rows"),
            dcc.RadioItems(
                id="rows-control",
                options=[{"label": str(index), "value": index} for index in range(1, 9)],
                value=8,
                inline=True,
                className="segmented",
            ),
            html.Button("Apply ROWS", id="rows-apply", className="button secondary"),
            html.Div(id="rows-status", className="small-text"),
        ],
    )


def _display_controls() -> html.Div:
    return html.Div(
        className="setup-section",
        children=[
            html.H2("Measurement"),
            dcc.Dropdown(
                id="measurement-domain",
                options=["Auto", "Capacitance", "Voltage", "Resistance"],
                value="Auto",
                clearable=False,
                className="input",
            ),
            dcc.RadioItems(
                id="display-mode",
                options=[
                    {"label": "Absolute C", "value": "absolute_c"},
                    {"label": "Delta C/C0 %", "value": "delta_percent"},
                ],
                value="absolute_c",
                inline=True,
                className="segmented",
            ),
            dcc.Checklist(
                id="display-options",
                options=[
                    {"label": "Pause display", "value": "pause"},
                    {"label": "Cell text", "value": "text"},
                    {"label": "Freeze colour", "value": "freeze"},
                ],
                value=[],
            ),
            html.Div("Circuit offset: 33 pF", className="small-text"),
        ],
    )


def _baseline_controls() -> html.Div:
    return html.Div(
        className="setup-section",
        children=[
            html.H2("Baseline"),
            html.Div(className="button-row", children=[
                html.Button("Capture Baseline", id="baseline-capture", className="button primary"),
                html.Button("Reset", id="baseline-reset", className="button secondary"),
                html.Button("Cancel", id="baseline-cancel", className="button quiet"),
            ]),
            html.Progress(id="baseline-progress", value=0, max=100),
            html.Div(id="baseline-status", className="small-text"),
        ],
    )


def _battery_card() -> html.Div:
    return html.Div(
        className="setup-section battery-card",
        children=[
            html.H2("Battery"),
            html.Div(id="battery-main", className="metric"),
            html.Div(id="battery-detail", className="small-text"),
            html.Div(className="button-row", children=[
                html.Button("Refresh", id="battery-refresh", className="button secondary"),
                html.Button("Diagnose", id="battery-diagnose", className="button secondary"),
                html.Button("Rail", id="battery-rail", className="button secondary"),
                html.Button("ADS", id="battery-ads", className="button secondary"),
            ]),
        ],
    )


def _diagnostics_card() -> html.Div:
    return html.Details(
        className="setup-section",
        children=[
            html.Summary("Diagnostics"),
            html.Pre(id="diagnostics-panel", className="diagnostics"),
        ],
    )
