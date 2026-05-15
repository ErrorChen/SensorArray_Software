from __future__ import annotations

import re
from pathlib import Path


JS_SOURCE = Path("matrix_log_viewer/matrix_log_viewer/assets/plotly_live_update.js")


def test_history_append_does_not_follow_before_extend_complete():
    source = JS_SOURCE.read_text(encoding="utf-8")

    assert re.search(r"await Plotly\.extendTraces\(.*?\);\s*.*?await applyFollowRange\(div, snapshot, nextX, nextY\);", source, re.DOTALL)


def test_history_reset_does_not_follow_before_react_complete():
    source = JS_SOURCE.read_text(encoding="utf-8")

    assert re.search(r"await Plotly\.react\(.*?\);\s*.*?await applyFollowRange\(div, snapshot, values\.x, values\.y\);", source, re.DOTALL)


def test_snapshot_revision_is_confirmed_only_from_committed_state():
    source = JS_SOURCE.read_text(encoding="utf-8")

    assert "lastHistoryRevision: root.appliedHistoryRevision" in source
    assert "root.appliedHistoryRevision = result.appliedRevision" in source
    assert "lastHistoryRevision: historySnapshot" not in source


def test_graph_div_does_not_fallback_to_dash_outer_container():
    source = JS_SOURCE.read_text(encoding="utf-8")

    assert "|| outer" not in source
    assert 'return outer.querySelector(".js-plotly-plot");' in source
    assert 'reason, "plotly-div-not-ready"' not in source
    assert 'failResult("history", "plotly-div-not-ready"' in source


def test_follow_range_relayout_sets_x_and_y_axes():
    source = JS_SOURCE.read_text(encoding="utf-8")

    assert '"xaxis.autorange": false' in source
    assert '"xaxis.range": xRange' in source
    assert 'update["yaxis.autorange"] = false' in source
    assert 'update["yaxis.range"] = yRange' in source
    assert 'update["yaxis.autorange"] = true' in source


def test_clear_revision_resets_frontend_history_state():
    source = JS_SOURCE.read_text(encoding="utf-8")

    assert "pendingClearRevision" in source
    assert "root.historyInitialized = false" in source
    assert "root.currentHistoryKey = null" in source
    assert "root.appliedHistoryRevision = null" in source
    assert "Plotly.react(historyDiv, [], emptyHistoryLayout()" in source


def test_frontend_distinguishes_coalesced_and_dropped_frames():
    source = JS_SOURCE.read_text(encoding="utf-8")

    assert "coalescedFrames" in source
    assert "coalescedHistoryUpdates" in source
    assert "coalescedHeatmapUpdates" in source
    assert "droppedFrames" in source
    assert "root.coalescedFrames += 1" in source


def test_frontend_reports_separate_fps_metrics():
    source = JS_SOURCE.read_text(encoding="utf-8")

    assert "browserRafFps" in source
    assert "visualUpdateFps" in source
    assert "callbackFps" in source
