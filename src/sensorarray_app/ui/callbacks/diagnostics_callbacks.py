from __future__ import annotations

import json

from dash import Input, Output


def register_diagnostics_callbacks(app, runtime) -> None:
    @app.callback(
        Output("baseline-progress", "value"),
        Output("baseline-status", "children"),
        Output("diagnostics-panel", "children"),
        Input("snapshot-store", "data"),
    )
    def update_diagnostics(snapshot):
        if not snapshot:
            return 0, "", ""
        baseline = snapshot["baseline"]
        diagnostics = snapshot["diagnostics"]
        status = (
            f"{baseline['status']} progress={baseline['progress'] * 100:.0f}% "
            f"frames={baseline['frameCount']} rejected={baseline['rejectedFrameCount']} "
            f"validCells={baseline['validCells']} reason={baseline['invalidReason']}"
        )
        return int(baseline["progress"] * 100), status, json.dumps(diagnostics, indent=2)
