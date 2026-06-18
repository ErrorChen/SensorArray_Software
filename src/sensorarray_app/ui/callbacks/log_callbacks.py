from __future__ import annotations

from dash import Input, Output, State


def register_log_callbacks(app, runtime) -> None:
    @app.callback(
        Output("raw-logs", "children"),
        Input("snapshot-store", "data"),
        State("log-search", "value"),
        State("log-severity", "value"),
        State("log-show-data", "value"),
    )
    def update_logs(snapshot, search, severity, show_data):
        logs = runtime.rawLogs.snapshot(show_data="show" in (show_data or []), search=search or "", severity=severity or "", limit=350)
        lines = []
        for row in logs["rows"]:
            lines.append(f"{row['timestamp']:.3f} [{row['source']}/{row['channel']}] {row['tag']} {row['severity']}: {row['rawText']}")
        return "\n".join(lines)

    @app.callback(
        Output("logs-clear", "n_clicks"),
        Input("logs-clear", "n_clicks"),
        prevent_initial_call=True,
    )
    def clear_logs(_clicks):
        runtime.rawLogs.clear_view()
        return 0
