from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_app.domain.models import CapacitanceFrame
from sensorarray_app.protocol.registry import ProtocolRegistry
from sensorarray_app.store.matrix_store import MatrixStore
from sensorarray_app.transport.envelope import TransportEnvelope
from sensorarray_backend.app import create_app

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
    assert snapshot.rawPf[0, 0] == 33.0
    assert snapshot.rawFixed[0, 0] == 33_000_000.0
    assert snapshot.valid[0, 0]
    assert not snapshot.valid[1, 0]
    assert snapshot.matrix[1, 0] != snapshot.matrix[1, 0]


def test_fastapi_status_schema_contains_required_snapshot_fields():
    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        assert client.get("/health").json()["ok"] is True
        payload = client.get("/api/status").json()
    assert payload["connection"]["mode"] == "serial"
    assert payload["frame"]["rows"] == 8
    assert len(payload["matrix"]["correctedPf"]) == 8
    assert len(payload["matrix"]["correctedPf"][0]) == 8
    assert "title" in payload["selection"]
    assert payload["display"]["displayMode"] == "absolute_pf"
    assert payload["display"]["circuitOffsetPf"] == 33.0


def test_websocket_publishes_snapshot_and_history():
    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            first = websocket.receive_json()
            second = websocket.receive_json()
    assert first["type"] == "snapshot"
    assert first["payload"]["selection"]["title"]
    assert second["type"] == "history"
    assert len(second["payload"]["series"]) == 4
