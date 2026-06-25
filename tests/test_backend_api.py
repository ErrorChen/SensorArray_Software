from __future__ import annotations

from fastapi.testclient import TestClient

from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_app.transport.serial_transport import SerialTransport
from sensorarray_backend.app import create_app


def test_serial_ports_are_discovered_not_defaulted(monkeypatch):
    monkeypatch.setattr(
        SerialTransport,
        "list_ports",
        staticmethod(lambda: [{"device": "COM12", "label": "COM12 - USB Serial", "value": "COM12"}]),
    )
    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        ports = client.get("/api/transport/serial/ports").json()["ports"]
        blank_connect = client.post("/api/transport/serial/connect", json={"port": "", "baud": 115200})
    assert ports[0]["device"] == "COM12"
    assert blank_connect.status_code == 422


def test_display_settings_rows_and_selection_endpoints():
    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        rows = client.post("/api/rows", json={"rows": 4}).json()
        display = client.post(
            "/api/settings/display",
            json={"showCellText": True, "freezeColor": True, "measurementDomain": "auto", "displayMode": "absolute_pf"},
        ).json()
        selection = client.post("/api/selection", json={"cell": "S2D7"}).json()
        status = client.get("/api/status").json()
    assert rows["displayOnly"] is True
    assert display["freezeColor"] is True
    assert selection["cells"] == ["S2D5", "S2D6", "S2D7", "S2D8"]
    assert selection["title"] == "S2 Secondary FDC D5-D8"
    assert status["selection"]["title"] == selection["title"]


def test_transport_write_disconnected_returns_clear_error():
    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        response = client.post("/api/transport/write", json={"text": "PING", "lineEnding": "lf", "mode": "text"})
    payload = response.json()
    assert response.status_code == 200
    assert payload["ok"] is False
    assert "not connected" in payload["error"]


def test_transport_write_serial_respects_line_ending():
    class FakeSerialTransport:
        source = "serial"

        def __init__(self):
            self.written = b""

        def stop(self):
            pass

        def write(self, data: bytes) -> int:
            self.written = bytes(data)
            return len(self.written)

    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        runtime = app.state.runtime
        fake = FakeSerialTransport()
        runtime.transport.current = fake
        runtime.transport.status.update({"transport": "serial", "state": "STREAMING", "device": "COM12"})
        response = client.post("/api/transport/write", json={"text": "PING", "lineEnding": "crlf", "mode": "text"})
    payload = response.json()
    assert payload == {"ok": True, "transport": "serial", "bytesWritten": 6}
    assert fake.written == b"PING\r\n"


def test_transport_write_replay_not_supported():
    class FakeReplayTransport:
        source = "replay"

        def stop(self):
            pass

        def write(self, data: bytes) -> int:
            raise NotImplementedError("replay transport does not support write")

    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        runtime = app.state.runtime
        runtime.transport.current = FakeReplayTransport()
        runtime.transport.status.update({"transport": "replay", "state": "STREAMING", "device": "fixture"})
        response = client.post("/api/transport/write", json={"text": "PING", "lineEnding": "none", "mode": "text"})
    payload = response.json()
    assert payload["ok"] is False
    assert "does not support write" in payload["error"]


def test_ble_scan_is_rejected_while_connected():
    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        runtime = app.state.runtime
        runtime.transport.status.update({"transport": "ble", "state": "STREAMING", "device": "AA:BB"})
        response = client.get("/api/transport/ble/scan")
    assert response.status_code == 409
    assert "BLE scan is disabled while connected" in response.json()["detail"]
