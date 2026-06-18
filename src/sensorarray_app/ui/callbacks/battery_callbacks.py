from __future__ import annotations

from dash import Input, Output


def register_battery_callbacks(app, runtime) -> None:
    @app.callback(
        Output("battery-main", "children"),
        Output("battery-detail", "children"),
        Input("snapshot-store", "data"),
    )
    def update_battery(snapshot):
        if not snapshot:
            return "N/A", "No battery telemetry"
        battery = snapshot["battery"]
        main = battery.get("batteryText") or "N/A"
        detail = f"{battery.get('batteryState', 'Unknown')} / {battery.get('reason', 'unknown')} / rail={battery.get('railState')}"
        return main, detail
