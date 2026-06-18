from __future__ import annotations

from dash import Input, Output

from sensorarray_app.ui.figures import heatmap_figure


def register_heatmap_callbacks(app, runtime) -> None:
    @app.callback(
        Output("heatmap", "figure"),
        Input("snapshot-store", "data"),
    )
    def update_heatmap(snapshot):
        if not snapshot:
            return heatmap_figure(runtime.snapshot())
        return heatmap_figure(_attach_baseline(snapshot, runtime))


def _attach_baseline(snapshot, runtime):
    snapshot = dict(snapshot)
    snapshot["_baseline_object"] = runtime.ui.baseline
    return snapshot
