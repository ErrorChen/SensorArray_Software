from __future__ import annotations

import ipaddress
import socket
import subprocess
from dataclasses import dataclass

from sensorarray_app.constants import WIFI_CTRL_PORT, WIFI_DEFAULT_HOST, WIFI_MDNS_PREFIX, WIFI_NAME_PREFIX


@dataclass(frozen=True)
class WifiCandidate:
    host: str
    method: str
    confirmed: bool = False
    response: str = ""
    error: str = ""


def current_windows_ssids() -> list[str]:
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "networks", "mode=bssid"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return []
    ssids: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("SSID") and ":" in stripped:
            value = stripped.split(":", maxsplit=1)[1].strip()
            if value:
                ssids.append(value)
    return ssids


def discover_wifi_candidates(timeout_seconds: float = 0.3, subnet: str | None = None) -> list[WifiCandidate]:
    candidates: list[WifiCandidate] = []
    seen: set[str] = set()

    def add(host: str, method: str) -> None:
        if host and host not in seen:
            seen.add(host)
            candidates.append(_confirm(host, method, timeout_seconds))

    for ssid in current_windows_ssids():
        if ssid.startswith(WIFI_NAME_PREFIX):
            suffix = ssid[len(WIFI_NAME_PREFIX) :].lower().replace("_", "-")
            add(f"{WIFI_MDNS_PREFIX}{suffix}.local", "mdns_ssid")
    add(WIFI_DEFAULT_HOST, "default_softap")
    if subnet:
        try:
            network = ipaddress.ip_network(subnet, strict=False)
            for host in list(network.hosts())[:64]:
                add(str(host), "bounded_subnet")
        except ValueError:
            pass
    return candidates


def _confirm(host: str, method: str, timeout_seconds: float) -> WifiCandidate:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout_seconds)
            sock.sendto(b"BAT?\n", (host, WIFI_CTRL_PORT))
            data, _ = sock.recvfrom(2048)
        text = data.decode("ascii", errors="strict").strip()
        confirmed = text.startswith(("ABAT", "AB50", "ARL", "ADS", "ACK", "ERR"))
        return WifiCandidate(host, method, confirmed, text)
    except Exception as exc:
        return WifiCandidate(host, method, False, "", str(exc))
