from __future__ import annotations

from dash import Input, Output, State, callback_context


def register_setup_callbacks(app, runtime) -> None:
    @app.callback(
        Output("rows-status", "children"),
        Input("rows-apply", "n_clicks"),
        State("rows-control", "value"),
        prevent_initial_call=True,
    )
    def apply_rows(_clicks, rows):
        try:
            runtime.request_rows(int(rows))
            return f"Requested Rows: {rows}; waiting for RAPP"
        except Exception as exc:
            return f"ROWS error: {exc}"

    @app.callback(
        Output("display-mode", "value"),
        Input("display-mode", "value"),
        Input("display-options", "value"),
        prevent_initial_call=True,
    )
    def display_options(mode, options):
        runtime.ui.paused = "pause" in (options or [])
        runtime.ui.cellText = "text" in (options or [])
        runtime.ui.freezeColor = "freeze" in (options or [])
        runtime.set_display_mode(mode)
        return runtime.ui.displayMode.value

    @app.callback(
        Output("baseline-capture", "n_clicks"),
        Output("baseline-reset", "n_clicks"),
        Output("baseline-cancel", "n_clicks"),
        Input("baseline-capture", "n_clicks"),
        Input("baseline-reset", "n_clicks"),
        Input("baseline-cancel", "n_clicks"),
        prevent_initial_call=True,
    )
    def baseline_actions(capture, reset, cancel):
        trigger = callback_context.triggered_id
        if trigger == "baseline-capture":
            runtime.capture_baseline()
        elif trigger == "baseline-reset":
            runtime.reset_baseline()
        elif trigger == "baseline-cancel":
            runtime.cancel_baseline()
        return 0, 0, 0

    @app.callback(
        Output("battery-refresh", "n_clicks"),
        Output("battery-diagnose", "n_clicks"),
        Output("battery-rail", "n_clicks"),
        Output("battery-ads", "n_clicks"),
        Input("battery-refresh", "n_clicks"),
        Input("battery-diagnose", "n_clicks"),
        Input("battery-rail", "n_clicks"),
        Input("battery-ads", "n_clicks"),
        prevent_initial_call=True,
    )
    def battery_actions(refresh, diagnose, rail, ads):
        trigger = callback_context.triggered_id
        command = {"battery-refresh": "BAT?", "battery-diagnose": "BATD", "battery-rail": "RAIL?", "battery-ads": "ADS?"}.get(trigger)
        if command:
            try:
                runtime.send_command(command)
            except Exception as exc:
                runtime._host_log("Battery", "error", str(exc))
        return 0, 0, 0, 0
