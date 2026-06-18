from __future__ import annotations

from sensorarray_app.ui.figures import trend_figure


def register_trend_callbacks(app, runtime) -> None:
    from dash import Input, Output

    @app.callback(
        Output("trend-chart", "figure"),
        Input("snapshot-store", "data"),
    )
    def update_trend(snapshot):
        snap = snapshot or runtime.snapshot()
        cells = snap["selection"]["cells"]
        indices = []
        for cell in cells:
            row = int(cell.split("D")[0][1:]) - 1
            det = int(cell.split("D")[1]) - 1
            indices.append(row * 8 + det)
        history = runtime.matrixStore.history.slice(indices, window_seconds=30.0)
        return trend_figure(snap, history)
