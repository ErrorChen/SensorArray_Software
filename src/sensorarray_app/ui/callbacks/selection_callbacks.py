from __future__ import annotations

from dash import Input, Output


def register_selection_callbacks(app, runtime) -> None:
    @app.callback(
        Output("selection-title", "children"),
        Input("heatmap", "clickData"),
        Input("snapshot-store", "data"),
    )
    def select_cell(click_data, snapshot):
        if click_data and click_data.get("points"):
            point = click_data["points"][0]
            y = point.get("y")
            x = point.get("x")
            if y and x:
                runtime.set_selection_from_cell(f"{y}{x}")
        snap = snapshot or runtime.snapshot()
        return f"Selected: {snap['selection']['title']}"
