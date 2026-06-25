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
    assert selection["title"] == "S2 路 Secondary FDC 路 D5-D8"
    assert status["selection"]["title"] == selection["title"]

