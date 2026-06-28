from __future__ import annotations

from types import SimpleNamespace

from sensorarray_app.transport.ble_discovery import BleCandidate, _sort_candidates, resolve_ble_rssi


def test_resolve_ble_rssi_from_device():
    assert resolve_ble_rssi(SimpleNamespace(rssi=-62), None) == -62


def test_resolve_ble_rssi_from_adv():
    assert resolve_ble_rssi(SimpleNamespace(), SimpleNamespace(rssi=-71)) == -71


def test_resolve_ble_rssi_from_platform_data_and_float():
    adv = SimpleNamespace(platform_data=({"RSSI": -63.8},))
    assert resolve_ble_rssi(SimpleNamespace(), adv) == -63


def test_resolve_ble_rssi_handles_missing_and_bad_strings():
    assert resolve_ble_rssi(SimpleNamespace(details={"name": "CscArray"}), SimpleNamespace(platform_data={"rssi": "bad"})) is None


def test_ble_candidate_sort_prefers_verified_match_and_real_rssi():
    candidates = [
        BleCandidate("Other", "1", -20, matchReason="unverified"),
        BleCandidate("CscArray_Weak", "2", -90, matchReason="project_name"),
        BleCandidate("CscArray_NoRssi", "3", None, matchReason="project_name"),
        BleCandidate("CscArray_Strong", "4", -40, matchReason="project_name"),
        BleCandidate("Verified", "5", None, verified=True, matchReason="service"),
    ]
    ordered = _sort_candidates(candidates)
    assert [item.address for item in ordered] == ["5", "4", "2", "3", "1"]
