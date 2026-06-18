from __future__ import annotations

import numpy as np
import plotly.graph_objects as go

from sensorarray_app.constants import DETECTOR_LABELS, ROW_LABELS
from sensorarray_app.domain.baseline import delta_percent
from sensorarray_app.domain.engineering_units import EngineeringUnitFormatter

FORMATTER = EngineeringUnitFormatter()


def heatmap_figure(snapshot: dict) -> go.Figure:
    matrix_info = snapshot["matrix"]
    selection = snapshot["selection"]
    mode = snapshot["ui"]["displayMode"]
    matrix = np.asarray(matrix_info["matrix"], dtype=np.float64)
    valid = np.asarray(matrix_info["valid"], dtype=bool)
    z = matrix.copy()
    title_unit = matrix_info.get("unit") or "pF"
    color_title = title_unit
    if mode == "delta_percent":
        z = np.full((8, 8), np.nan, dtype=np.float64)
        baseline = snapshot.get("_baseline_object")
        if baseline is not None:
            flat = delta_percent(matrix.reshape(64), baseline)
            z = flat.reshape(8, 8)
        color_title = "%"
    elif matrix_info.get("domain") == "capacitance":
        unit = FORMATTER.choose_unit(z[np.isfinite(z)].flat, "absolute")
        z = FORMATTER.scale(z, unit)
        color_title = unit.name
    z[~valid] = np.nan
    fig = go.Figure(
        data=[
            go.Heatmap(
                z=z,
                x=list(DETECTOR_LABELS),
                y=list(ROW_LABELS),
                zsmooth=False,
                colorscale="RdBu" if mode == "delta_percent" else "Viridis",
                reversescale=mode == "delta_percent",
                colorbar={"title": color_title},
                hovertemplate="Cell %{y}%{x}<br>Value %{z}<extra></extra>",
            )
        ]
    )
    shapes = []
    row = int(selection["rowIndex"])
    x0 = int(selection["detectorStart"]) - 1.5
    x1 = int(selection["detectorEnd"]) - 0.5
    shapes.append(
        {
            "type": "rect",
            "xref": "x",
            "yref": "y",
            "x0": x0,
            "x1": x1,
            "y0": row - 0.5,
            "y1": row + 0.5,
            "line": {"color": "#f59e0b", "width": 3},
            "fillcolor": "rgba(245,158,11,0.12)",
        }
    )
    fig.update_layout(
        title=f"8x8 Heatmap - seq {matrix_info.get('seq') or '-'}",
        margin={"l": 45, "r": 20, "t": 42, "b": 35},
        xaxis={"side": "top", "fixedrange": True},
        yaxis={"autorange": "reversed", "fixedrange": True},
        shapes=shapes,
        uirevision="heatmap",
    )
    return fig


def trend_figure(snapshot: dict, history_slice) -> go.Figure:
    selection = snapshot["selection"]
    mode = snapshot["ui"]["displayMode"]
    fig = go.Figure()
    x = history_slice.timeSeconds
    values = history_slice.values
    unit_name = "pF"
    if mode == "delta_percent":
        y_values = values
        unit_name = "%"
    elif snapshot["matrix"]["domain"] == "capacitance":
        unit = FORMATTER.choose_unit(values[np.isfinite(values)].flat, "trend")
        y_values = FORMATTER.scale(values, unit)
        unit_name = unit.name
    else:
        y_values = values
        unit_name = snapshot["matrix"].get("unit") or ""
    for idx, cell in enumerate(selection["cells"]):
        y = y_values[:, idx] if y_values.ndim == 2 and y_values.shape[1] > idx else []
        fig.add_trace(go.Scattergl(x=x, y=y, mode="lines", name=cell, connectgaps=False))
    fig.update_layout(
        title=selection["title"],
        margin={"l": 55, "r": 15, "t": 42, "b": 40},
        xaxis_title="Time (s)",
        yaxis_title=unit_name,
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "right", "x": 1},
        uirevision="trend",
    )
    return fig


def empty_figure(title: str) -> go.Figure:
    fig = go.Figure()
    fig.update_layout(title=title, margin={"l": 40, "r": 15, "t": 35, "b": 35})
    return fig
