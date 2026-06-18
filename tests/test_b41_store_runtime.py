from __future__ import annotations

from pathlib import Path

from sensorarray_app.app.bootstrap import create_app
from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_app.domain.models import CapacitanceFrame
from sensorarray_app.protocol.registry import ProtocolRegistry
from sensorarray_app.store.matrix_store import MatrixStore
from sensorarray_app.transport.envelope import TransportEnvelope

FIXTURES = Path(__file__).parent / "fixtures" / "b41"


def test_matrix_store_expands_inactive_rows_to_nan():
    registry = ProtocolRegistry()
    env = TransportEnvelope("replay", "data", "fixture", 1, 1, 1.0, (FIXTURES / "rows1_valid.txt").read_bytes())
    frame = [event for event in registry.feed(env) if isinstance(event, CapacitanceFrame)][0]
    store = MatrixStore()
    store.add_capacitance(frame)
    snapshot = store.snapshot()
    assert snapshot.activeRows == 1
    assert snapshot.matrix[0, 0] == 0.0
    assert snapshot.valid[0, 0]
    assert not snapshot.valid[1, 0]


def test_new_dash_app_contains_required_panels():
    app = create_app(AppConfiguration())
    try:
        layout_text = str(app.layout)
        assert "8x8 Heatmap" in layout_text
        assert "Connection" in layout_text
        assert "Battery" in layout_text
        assert "Raw Logs" in layout_text
    finally:
        app._sensorarray_runtime.stop()
