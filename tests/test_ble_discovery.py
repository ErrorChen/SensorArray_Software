from __future__ import annotations

import asyncio
from types import SimpleNamespace

import sensorarray_app.transport.ble_discovery as ble_discovery
from sensorarray_app.constants import BLE_SERVICE_UUID
from sensorarray_app.transport.ble_discovery import BleCandidate, _scan_match_reason, _sort_candidates, resolve_ble_rssi


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


def test_project_name_takes_priority_over_generic_ff_service():
    assert _scan_match_reason("CscArray_CEE500", (BLE_SERVICE_UUID,)) == "project_name"
    assert _scan_match_reason("UnrelatedVendorDevice", (BLE_SERVICE_UUID,)) == "service"


def test_discovery_gatt_verifies_only_project_named_candidates(monkeypatch):
    project_candidate = BleCandidate(
        "CscArray_CEE500",
        "project-address",
        -45,
        serviceUuids=(BLE_SERVICE_UUID,),
        matchReason="project_name",
    )
    generic_ff_candidate = BleCandidate(
        "OtherVendor",
        "generic-address",
        -20,
        serviceUuids=(BLE_SERVICE_UUID,),
        matchReason="service",
        advanced=True,
    )
    verified_addresses: list[str] = []

    async def fake_scan(_timeout_seconds: float):
        return [generic_ff_candidate, project_candidate]

    async def fake_verify(candidate: BleCandidate, timeout_seconds: float = 8.0):
        verified_addresses.append(candidate.address)
        return BleCandidate(
            **{
                **candidate.__dict__,
                "verified": True,
                "serviceVerified": True,
                "characteristicsVerified": True,
                "reason": f"verified in {timeout_seconds:g}s",
            }
        )

    monkeypatch.setattr(ble_discovery, "scan_ble_candidates", fake_scan)
    monkeypatch.setattr(ble_discovery, "verify_ble_candidate", fake_verify)

    discovered = asyncio.run(ble_discovery.discover_verified_ble_candidates(1.0))

    assert verified_addresses == ["project-address"]
    by_address = {candidate.address: candidate for candidate in discovered}
    assert by_address["project-address"].verified is True
    assert by_address["project-address"].characteristicsVerified is True
    assert by_address["generic-address"].verified is False
    assert by_address["generic-address"].advanced is True
