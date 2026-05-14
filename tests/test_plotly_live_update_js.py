from __future__ import annotations

import re
from pathlib import Path


JS_SOURCE = Path("matrix_log_viewer/matrix_log_viewer/assets/plotly_live_update.js")


def test_history_append_does_not_follow_before_extend_complete():
    source = JS_SOURCE.read_text(encoding="utf-8")

    assert re.search(
        r"Promise\.resolve\(\s*Plotly\.extendTraces\(.*?\)\s*\)\.then\(function \(\) \{\s*return applyFollowRange\(div, snapshot\);",
        source,
        re.DOTALL,
    )


def test_history_reset_does_not_follow_before_react_complete():
    source = JS_SOURCE.read_text(encoding="utf-8")

    assert "Promise.resolve(Plotly.react" in source
    assert re.search(
        r"reactPromise\.then\(function \(\) \{.*?return applyFollowRange\(div, snapshot\);",
        source,
        re.DOTALL,
    )


def test_frontend_distinguishes_coalesced_and_dropped_frames():
    source = JS_SOURCE.read_text(encoding="utf-8")

    assert "coalescedFrames" in source
    assert "droppedFrames" in source
    assert "root.coalescedFrames += 1" in source
