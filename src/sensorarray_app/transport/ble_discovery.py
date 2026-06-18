from __future__ import annotations

from dataclasses import dataclass

from sensorarray_app.constants import (
    BLE_CTRL_RX_UUID,
    BLE_CTRL_TX_UUID,
    BLE_DATA_TX_UUID,
    BLE_LOG_TX_UUID,
    BLE_NAME_PREFIX,
    BLE_SERVICE_UUID,
)


@dataclass(frozen=True)
class BleCandidate:
    name: str
    address: str
    rssi: int | None
    serviceVerified: bool = False
    characteristicsVerified: bool = False
    error: str = ""


async def scan_ble_candidates(timeout_seconds: float = 10.0) -> list[BleCandidate]:
    try:
        from bleak import BleakScanner
    except Exception as exc:  # pragma: no cover
        return [BleCandidate("", "", None, error=f"bleak unavailable: {exc}")]
    devices = await BleakScanner.discover(timeout=float(timeout_seconds), return_adv=True)
    candidates: list[BleCandidate] = []
    for device, adv in devices.values():
        name = device.name or (adv.local_name if adv else "") or ""
        uuids = {uuid.lower() for uuid in (adv.service_uuids if adv else [])}
        name_match = name.startswith(BLE_NAME_PREFIX)
        service_hint = BLE_SERVICE_UUID.lower() in uuids or "0000ff00-0000-1000-8000-00805f9b34fb" in uuids
        if name_match or service_hint or not name:
            candidates.append(BleCandidate(name=name, address=device.address, rssi=getattr(device, "rssi", None)))
    return sorted(candidates, key=lambda item: item.rssi if item.rssi is not None else -999, reverse=True)


async def verify_ble_candidate(candidate: BleCandidate, timeout_seconds: float = 8.0) -> BleCandidate:
    try:
        from bleak import BleakClient
    except Exception as exc:  # pragma: no cover
        return BleCandidate(candidate.name, candidate.address, candidate.rssi, False, False, f"bleak unavailable: {exc}")
    try:
        async with BleakClient(candidate.address, timeout=float(timeout_seconds)) as client:
            services = client.services
            service_uuids = {service.uuid.lower() for service in services}
            char_uuids = {char.uuid.lower() for service in services for char in service.characteristics}
            service_ok = _uuid_match(BLE_SERVICE_UUID, service_uuids)
            chars_ok = all(
                _uuid_match(uuid, char_uuids)
                for uuid in (BLE_CTRL_RX_UUID, BLE_CTRL_TX_UUID, BLE_DATA_TX_UUID, BLE_LOG_TX_UUID)
            )
            return BleCandidate(candidate.name, candidate.address, candidate.rssi, service_ok, chars_ok, "")
    except Exception as exc:
        return BleCandidate(candidate.name, candidate.address, candidate.rssi, False, False, str(exc))


async def discover_verified_ble_candidates(scan_seconds: float = 10.0) -> list[BleCandidate]:
    candidates = await scan_ble_candidates(scan_seconds)
    verified: list[BleCandidate] = []
    for candidate in candidates:
        if not candidate.address or (candidate.name and not candidate.name.startswith(BLE_NAME_PREFIX)):
            verified.append(candidate)
            continue
        verified.append(await verify_ble_candidate(candidate))
    return sorted(
        verified,
        key=lambda item: (
            1 if item.serviceVerified and item.characteristicsVerified else 0,
            item.rssi if item.rssi is not None else -999,
        ),
        reverse=True,
    )


def _uuid_match(expected: str, values: set[str]) -> bool:
    short = expected[4:8].lower() if expected.startswith("0000") else expected.lower()
    return expected.lower() in values or any(value.startswith(f"0000{short}-") for value in values)
