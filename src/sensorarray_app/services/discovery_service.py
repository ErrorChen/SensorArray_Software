from __future__ import annotations

import asyncio

from sensorarray_app.transport.ble_discovery import BleCandidate, discover_verified_ble_candidates
from sensorarray_app.transport.wifi_discovery import WifiCandidate, discover_wifi_candidates


def scan_ble(timeout_seconds: float = 10.0) -> list[BleCandidate]:
    try:
        return asyncio.run(discover_verified_ble_candidates(timeout_seconds))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(discover_verified_ble_candidates(timeout_seconds))
        finally:
            loop.close()


def scan_wifi(subnet: str | None = None) -> list[WifiCandidate]:
    return discover_wifi_candidates(subnet=subnet)
