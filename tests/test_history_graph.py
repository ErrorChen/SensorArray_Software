from __future__ import annotations

import pandas as pd
import pytest

from matrix_log_viewer.app import _build_history_figure


def make_history(values: list[float], unit: str = "mV") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "seq": [1, 2, 3],
            "timestampUs": [1_000_000, 2_000_000, 3_000_000],
            "timeSeconds": [1.0, 2.0, 3.0],
            "value": values,
            "unit": [unit, unit, unit],
        }
    )


def build_history_figure(
    cell_name: str,
    frame_type: str,
    history: pd.DataFrame,
    unit_mode: str = "mV",
):
    return _build_history_figure(
        cell_name=cell_name,
        frame_type=frame_type,
        history_raw=history,
        history_rendered=history,
        x_axis="timeSeconds",
        unit_mode=unit_mode,
        auto_follow=True,
        window_mode="last_30s",
        last_n=1000,
        custom_min=None,
        custom_max=None,
    )


def test_history_figure_resets_y_axis_revision_when_cell_changes():
    s1d1 = make_history([53.0, 53.1, 53.2])
    s4d8 = make_history([-20.1, -20.0, -19.9])

    s1d1_fig = build_history_figure("S1D1", "MATV", s1d1)
    s4d8_fig = build_history_figure("S4D8", "MATV", s4d8)

    assert "S1D1" in s1d1_fig.layout.title.text
    assert list(s1d1_fig.data[0].y) == pytest.approx([53.0, 53.1, 53.2])
    assert "S1D1" in s1d1_fig.layout.yaxis.uirevision

    assert "S4D8" in s4d8_fig.layout.title.text
    assert s4d8_fig.data[0].name == "S4D8 / MATV"
    assert list(s4d8_fig.data[0].y) == pytest.approx([-20.1, -20.0, -19.9])
    assert s4d8_fig.layout.yaxis.uirevision != s1d1_fig.layout.yaxis.uirevision
    assert "S4D8" in s4d8_fig.layout.yaxis.uirevision
    assert s4d8_fig.layout.yaxis.autorange is True
    assert s4d8_fig.layout.yaxis.range is None
    assert s4d8_fig.layout.xaxis.uirevision == s1d1_fig.layout.xaxis.uirevision


def test_history_figure_keeps_y_axis_revision_for_same_cell_refresh():
    s4d8 = make_history([-20.1, -20.0, -19.9])

    first_fig = build_history_figure("S4D8", "MATV", s4d8)
    second_fig = build_history_figure("S4D8", "MATV", s4d8)

    assert second_fig.layout.yaxis.uirevision == first_fig.layout.yaxis.uirevision


def test_history_figure_resets_y_axis_revision_when_stream_changes():
    s4d8 = make_history([-20.1, -20.0, -19.9])

    matv_fig = build_history_figure("S4D8", "MATV", s4d8)
    fast_fig = build_history_figure("S4D8", "FAST_BINARY", s4d8)

    assert fast_fig.layout.yaxis.uirevision != matv_fig.layout.yaxis.uirevision


def test_history_figure_resets_y_axis_revision_when_unit_mode_changes():
    s4d8 = make_history([-20.1, -20.0, -19.9])

    mv_fig = build_history_figure("S4D8", "MATV", s4d8, unit_mode="mV")
    uv_fig = build_history_figure("S4D8", "MATV", s4d8, unit_mode="uV")

    assert list(mv_fig.data[0].y) == pytest.approx([-20.1, -20.0, -19.9])
    assert list(uv_fig.data[0].y) == pytest.approx([-20100.0, -20000.0, -19900.0])
    assert uv_fig.layout.yaxis.uirevision != mv_fig.layout.yaxis.uirevision


def test_empty_history_figure_uses_current_cell_stream_and_window():
    empty = pd.DataFrame(columns=["seq", "timestampUs", "timeSeconds", "value", "unit"])

    fig = build_history_figure("S4D8", "MATV", empty)

    assert fig.layout.title.text == "History of S4D8 / MATV"
    assert "stream=MATV" in fig.layout.annotations[0].text
    assert "cell=S4D8" in fig.layout.annotations[0].text
    assert "window=last_30s" in fig.layout.annotations[0].text
    assert len(fig.data) == 0
