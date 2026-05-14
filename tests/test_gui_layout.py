from __future__ import annotations

import queue

from dash.development.base_component import Component

from matrix_log_viewer.app import _cell_name_from_click_data, createDashApp
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


def test_gui_target_fps_control_defaults_to_60():
    app = make_app()
    try:
        fps_dropdown = find_component(app.layout, "gui-target-fps")
        interval_input = find_component(app.layout, "interval-ms")

        assert fps_dropdown.value == 60
        assert interval_input.value <= 17
        assert app._sensorarray_heatmap_cache.targetFps == 60
        assert app._sensorarray_history_cache.targetFps == 60
    finally:
        stop_app(app)
