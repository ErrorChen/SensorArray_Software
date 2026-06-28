from __future__ import annotations

import time

import numpy as np
from fastapi.testclient import TestClient

from sensorarray_app.app.configuration import AppConfiguration
from sensorarray_app.domain.models import CapacitanceFrame
from sensorarray_app.transport.serial_transport import SerialTransport
from sensorarray_backend.app import create_app
from sensorarray_backend.core.session_data import load_session_frames


def test_serial_ports_are_discovered_not_defaulted(monkeypatch):
    monkeypatch.setattr(
        SerialTransport,
        "list_ports",
        staticmethod(lambda: [{"device": "SERIAL_TEST_PORT", "label": "SERIAL_TEST_PORT - USB Serial", "value": "SERIAL_TEST_PORT"}]),
    )
    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        ports = client.get("/api/transport/serial/ports").json()["ports"]
        blank_connect = client.post("/api/transport/serial/connect", json={"port": "", "baud": 115200})
    assert ports[0]["device"] == "SERIAL_TEST_PORT"
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
        runtime.transport.status.update({"transport": "serial", "state": "STREAMING", "device": "SERIAL_TEST_PORT"})
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


def test_delta_request_without_frame_reports_no_data():
    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        payload = client.post("/api/settings/display", json={"displayMode": "delta_percent"}).json()
    assert payload["displayMode"] == "absolute_pf"
    assert payload["pendingDisplayMode"] is None
    assert payload["baselineStatus"]["status"] == "no_data"


def test_delta_request_with_frame_pending_then_auto_applies():
    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        runtime = app.state.runtime
        runtime.matrixStore.add_capacitance(make_cap_frame(1, [10.0] * 64))
        payload = client.post("/api/settings/display", json={"displayMode": "delta_percent"}).json()
        assert payload["displayMode"] == "absolute_pf"
        assert payload["pendingDisplayMode"] == "delta_percent"
        assert payload["baselineStatus"]["status"] == "capturing"
        for seq in [2, 3, 4]:
            runtime._handle_event(make_cap_frame(seq, [10.0] * 64))
        assert runtime._baseline_session is not None
        runtime._baseline_session.durationSeconds = 0
        runtime._complete_baseline_if_due()
        status = client.get("/api/status").json()
    assert status["display"]["displayMode"] == "delta_percent"
    assert status["baseline"]["ready"] is True
    assert status["baseline"]["pendingDisplayMode"] is None


def test_offsets_apply_to_snapshot_history_and_export():
    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        runtime = app.state.runtime
        for seq in [1, 2]:
            runtime._handle_event(make_cap_frame(seq, [20.0] * 64))
        offset = client.post("/api/settings/offsets/cell", json={"row": 1, "col": 1, "offsetPf": 10.0}).json()
        status = client.get("/api/status").json()
        history = client.get("/api/history?latest_n=300").json()
        exported = runtime.export_session_payload()
    assert offset["offsetsPf"][0][0] == 10.0
    assert status["matrix"]["correctedPf"][0][0] == 20.0
    assert status["matrix"]["displayValues"][0][0] == 10.0
    assert history["series"][0]["points"][-1]["value"] == 10.0
    assert exported["offsetsPf"][0][0] == 10.0
    assert "metadata" in exported
    assert "historyFrames" in exported


def test_session_export_formats_round_trip(tmp_path):
    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        runtime = app.state.runtime
        runtime._handle_event(make_cap_frame(1, [20.0] * 64))
        for fmt in ["csv", "xlsx", "mat", "h5"]:
            response = client.get(f"/api/export/session?format={fmt}")
            assert response.status_code == 200
            path = tmp_path / f"session.{fmt}"
            path.write_bytes(response.content)
            frames = load_session_frames(path)
            assert frames
            assert frames[-1].values_matrix().shape == (8, 8)
            assert frames[-1].values_matrix()[0, 0] == 20.0


def test_import_session_data_route(tmp_path):
    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        runtime = app.state.runtime
        runtime._handle_event(make_cap_frame(1, [22.0] * 64))
        response = client.get("/api/export/session?format=csv")
        path = tmp_path / "session.csv"
        path.write_bytes(response.content)
        imported = client.post("/api/import/session", json={"path": str(path)}).json()
        status = client.get("/api/status").json()
    assert imported["ok"] is True
    assert imported["frames"] >= 1
    assert status["matrix"]["correctedPf"][0][0] == 22.0


def test_setup_profile_get_and_apply():
    app = create_app(AppConfiguration())
    with TestClient(app) as client:
        profile = client.get("/api/setup/profile").json()
        profile["transport"]["serial"]["baud"] = 230400
        profile["acquisition"]["rows"] = 4
        profile["offsetsPf"][0][0] = 7.5
        profile["paths"]["defaultSaveDirectory"] = "C:/SensorArrayExports"
        response = client.post("/api/setup/profile", json=profile)
        payload = response.json()
        status = client.get("/api/status").json()
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["profile"]["transport"]["serial"]["baud"] == 230400
    assert payload["profile"]["offsetsPf"][0][0] == 7.5
    assert payload["profile"]["paths"]["defaultSaveDirectory"] == "C:/SensorArrayExports"
    assert status["frame"]["rows"] == 4


def make_cap_frame(seq: int, values: list[float]) -> CapacitanceFrame:
    corrected = np.asarray(values, dtype=np.float64)
    raw_pf = corrected + 33.0
    raw_fixed = np.rint(raw_pf * 1_000_000).astype(np.int64)
    valid = np.ones(64, dtype=bool)
    now_ns = time.monotonic_ns()
    return CapacitanceFrame(
        seq=seq,
        timestampUs=seq * 1000,
        rows=8,
        cells=64,
        generation=1,
        requestId=1,
        rowFreshMask=0xFF,
        primaryFreshMask=0xFF,
        secondaryFreshMask=0xFF,
        badStaleCount=0,
        badMixedCount=0,
        badInvalidCount=0,
        rawFixedValues=raw_fixed,
        rawPfValues=raw_pf,
        correctedPfValues=corrected,
        validMask=valid,
        sourceTransport="none",
        sessionGeneration=0,
        receivedTime=time.time(),
        receivedMonotonicNs=now_ns,
    )
