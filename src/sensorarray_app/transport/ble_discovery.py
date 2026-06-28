from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Any

from sensorarray_app.constants import (
    BLE_CTRL_RX_UUID,
    BLE_CTRL_TX_UUID,
    BLE_DATA_TX_UUID,
    BLE_LOG_TX_UUID,
    BLE_NAME_PREFIX,
    BLE_SERVICE_UUID,
)

PROJECT_NAME_HINTS = ("sensorarray", "cscarray", "csarray")


@dataclass(frozen=True)
class BleCandidate:
    name: str
    address: str
    rssi: int | None
    serviceUuids: tuple[str, ...] = field(default_factory=tuple)
    verified: bool = False
    serviceVerified: bool = False
    characteristicsVerified: bool = False
    matchReason: str = ""
    reason: str = ""
    advanced: bool = False


async def scan_ble_candidates(timeout_seconds: float = 10.0) -> list[BleCandidate]:
    try:
        from bleak import BleakScanner
    except Exception as exc:  # pragma: no cover
        return [BleCandidate("", "", None, reason=f"bleak unavailable: {exc}", advanced=True)]
    devices = await BleakScanner.discover(timeout=float(timeout_seconds), return_adv=True)
    candidates: list[BleCandidate] = []
    for device, adv in devices.values():
        name = device.name or (adv.local_name if adv else "") or ""
        uuids = tuple(sorted({uuid.lower() for uuid in (adv.service_uuids if adv else [])}))
        match_reason = _scan_match_reason(name, uuids)
        advanced = match_reason == "unnamed"
        if match_reason or not name:
            candidates.append(
                BleCandidate(
                    name=name,
                    address=device.address,
                    rssi=resolve_ble_rssi(device, adv),
                    serviceUuids=uuids,
                    matchReason=match_reason or "unverified",
                    advanced=advanced,
                )
            )
    return _sort_candidates(candidates)


async def verify_ble_candidate(candidate: BleCandidate, timeout_seconds: float = 8.0) -> BleCandidate:
    try:
        from bleak import BleakClient
    except Exception as exc:  # pragma: no cover
        return _replace(candidate, reason=f"bleak unavailable: {exc}")
    if not candidate.address:
        return _replace(candidate, reason="missing address", advanced=True)
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
            reason = "service and notify characteristics verified" if service_ok and chars_ok else "GATT did not match expected UUID set"
            return _replace(
                candidate,
                verified=service_ok and chars_ok,
                serviceVerified=service_ok,
                characteristicsVerified=chars_ok,
                serviceUuids=tuple(sorted(service_uuids or set(candidate.serviceUuids))),
                reason=reason,
            )
    except Exception as exc:
        return _replace(candidate, reason=str(exc))


async def discover_verified_ble_candidates(scan_seconds: float = 10.0) -> list[BleCandidate]:
    candidates = await scan_ble_candidates(scan_seconds)
    verified: list[BleCandidate] = []
    for candidate in candidates:
        # Only connect-probe likely SensorArray devices. Unnamed and unrelated
        # devices remain available under the advanced UI list but are not promoted.
        if candidate.matchReason in {"service", "project_name"}:
            verified.append(await verify_ble_candidate(candidate))
        else:
            verified.append(candidate)
    return _sort_candidates(verified)


def _scan_match_reason(name: str, uuids: tuple[str, ...]) -> str:
    lower_name = name.lower()
    if _uuid_match(BLE_SERVICE_UUID, set(uuids)):
        return "service"
    if name.startswith(BLE_NAME_PREFIX) or any(hint in lower_name for hint in PROJECT_NAME_HINTS):
        return "project_name"
    if not name:
        return "unnamed"
    return ""


def _sort_candidates(candidates: list[BleCandidate]) -> list[BleCandidate]:
    return sorted(
        candidates,
        key=lambda item: (
            1 if item.verified else 0,
            1 if item.matchReason in {"service", "project_name"} else 0,
            1 if item.rssi is not None else 0,
            item.rssi if item.rssi is not None else -999,
            0 if item.advanced else 1,
        ),
        reverse=True,
    )


def resolve_ble_rssi(device: Any, adv: Any) -> int | None:
    """Return the best RSSI from bleak device/ad data without trusting one backend shape."""
    for source in (
        device,
        adv,
        _safe_getattr(device, "details"),
        _safe_getattr(adv, "platform_data"),
    ):
        value = _extract_rssi(source, seen=set(), depth=0, direct=False)
        if value is not None:
            return value
    return None


def _extract_rssi(value: Any, seen: set[int], depth: int, direct: bool) -> int | None:
    if value is None or depth > 5:
        return None
    object_id = id(value)
    if object_id in seen:
        return None
    seen.add(object_id)
    if direct:
        parsed = _coerce_rssi(value)
        if parsed is not None:
            return parsed
    if isinstance(value, dict):
        for key, child in value.items():
            if "rssi" in str(key).lower():
                parsed = _coerce_rssi(child)
                if parsed is not None:
                    return parsed
        for child in value.values():
            parsed = _extract_rssi(child, seen, depth + 1, direct=False)
            if parsed is not None:
                return parsed
        return None
    if isinstance(value, (list, tuple, set, frozenset)):
        sequence = list(value)
        for index in range(0, max(0, len(sequence) - 1)):
            if "rssi" in str(sequence[index]).lower():
                parsed = _coerce_rssi(sequence[index + 1])
                if parsed is not None:
                    return parsed
        for child in sequence:
            parsed = _extract_rssi(child, seen, depth + 1, direct=False)
            if parsed is not None:
                return parsed
        return None
    for attr_name in ("rssi", "RSSI"):
        parsed = _coerce_rssi(_safe_getattr(value, attr_name))
        if parsed is not None:
            return parsed
    for attr_name in ("details", "platform_data", "manufacturer_data", "service_data", "__dict__"):
        parsed = _extract_rssi(_safe_getattr(value, attr_name), seen, depth + 1, direct=False)
        if parsed is not None:
            return parsed
    return None


def _safe_getattr(value: Any, name: str) -> Any:
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _coerce_rssi(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number):
        return None
    rounded = int(number)
    if -140 <= rounded <= 30:
        return rounded
    return None


def _replace(candidate: BleCandidate, **updates) -> BleCandidate:
    data = candidate.__dict__.copy()
    data.update(updates)
    return BleCandidate(**data)


def _uuid_match(expected: str, values: set[str]) -> bool:
    short = expected[4:8].lower() if expected.startswith("0000") else expected.lower()
    return expected.lower() in values or any(value.startswith(f"0000{short}-") for value in values)
