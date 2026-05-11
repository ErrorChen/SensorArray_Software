from __future__ import annotations

import queue

from dash.development.base_component import Component

from matrix_log_viewer.app import _build_key_metrics_panel, _cell_name_from_click_data, createDashApp
from matrix_log_viewer.config import DEFAULT_RENDER_TARGET_FPS
from matrix_log_viewer.connection_manager import ConnectionManager
from matrix_log_viewer.data_store import MatrixDataStore
from matrix_log_viewer.protocol_parser import SensorArrayStreamParser


def find_component(root: Component, component_id: str) -> Component | None:
    if getattr(root, "id", None) == component_id:
        return root
    children = getattr(root, "children", None)
    if children is None:
        return None
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        if isinstance(child, Component):
            found = find_component(child, component_id)
            if found is not None:
                return found
    return None


def make_app():
    input_queue: queue.Queue[bytes] = queue.Queue()
    app = createDashApp(
        input_queue,
        SensorArrayStreamParser(),
        MatrixDataStore(maxPointsPerCell=10),
        ConnectionManager(input_queue),
    )
    return app


def stop_app(app) -> None:
    app._sensorarray_input_processor.stop()
    app._sensorarray_input_processor.join(timeout=1.0)
    for cache in getattr(app, "_sensorarray_render_caches", ()):
        cache.stop()
        cache.join(timeout=1.0)


def test_gui_layout_contains_compact_controls():
    app = make_app()
    try:
        layout = app.layout
        for component_id in (
            "com-port-dropdown",
            "refresh-ports-button",
            "connect-button",
            "disconnect-button",
            "pause-button",
            "clear-button",
            "save-button",
            "cell-dropdown",
            "history-window",
            "unit-mode",
            "color-mode",
        ):
            assert find_component(layout, component_id) is not None
        assert find_component(layout, "gui-target-fps") is None
        render_store = find_component(layout, "render-control-store")
        assert render_store.data["targetFps"] == DEFAULT_RENDER_TARGET_FPS
    finally:
        stop_app(app)


def test_advanced_diagnostics_hidden_by_default():
    app = make_app()
    try:
        advanced = find_component(app.layout, "advanced-details")

        assert advanced is not None
        assert advanced.open is False
        assert find_component(advanced, "diagnostics-panel") is not None
        assert find_component(advanced, "replay-file-input") is not None
    finally:
        stop_app(app)


def test_heatmap_selection_still_maps_click_to_cell():
    click_data = {"points": [{"customdata": ["S2D7", "valid"]}]}

    assert _cell_name_from_click_data(click_data) == "S2D7"


def test_fast_binary_and_matv_stream_options_remain_available():
    app = make_app()
    try:
        frame_dropdown = find_component(app.layout, "frame-type-dropdown")
        values = {option["value"] for option in frame_dropdown.options}

        assert "FAST_BINARY" in values
        assert "MATV" in values
    finally:
        stop_app(app)


def test_key_metrics_panel_shows_error_runtime_and_device_counters():
    panel = _build_key_metrics_panel(
        selected_type="FAST_BINARY",
        selected_cell="S1D1",
        latest_meta={
            "seq": 42,
            "statusFlags": 0x2,
            "firstStatusCode": 0,
            "firstStatusCodeName": "OK",
            "lastStatusCode": 0x101,
            "lastStatusCodeName": "ROUTE_ERROR",
            "droppedFrames": 7,
            "outputDecimatedFrames": 3,
            "adsDr": 15,
            "outputDivider": 2,
        },
        parser_stats={
            "binaryCrcErrors": 2,
            "binaryMagicResyncs": 4,
            "parseErrors": 1,
            "skippedBytes": 9,
            "skippedLines": 5,
            "bufferedBytes": 12,
            "lastError": "crc mismatch",
            "lastWarning": "",
        },
        connection_status={"droppedInputChunks": 6, "droppedInputBytes": 1024},
        runtime_stats={
            "latestSeq": 42,
            "seqGap": 8,
            "parsedBinaryFps": 59.7,
            "parsedTextFps": 1.2,
            "bytesPerSec": 2048,
            "guiDisplayedFps": 58.9,
            "guiHeatmapFps": 60.1,
            "guiHistoryFps": 57.5,
            "renderTickFps": 60.0,
            "renderSkipped": 10,
            "lastClientError": "plot failed",
        },
        latest_device_status={"summary": {"latestDrop": 7, "latestDecimated": 3}},
        queue_depth=11,
    )
    text = repr(panel)

    assert "binary CRC errors" in text
    assert "resync count / magic resyncs" in text
    assert "parsed binary fps" in text
    assert "device droppedFrames" in text
    assert "device outputDecimatedFrames" in text
    assert "host dropped input chunks" in text
    assert "last client error" in text
    assert "plot failed" in text
