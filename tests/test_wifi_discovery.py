from __future__ import annotations

from types import SimpleNamespace

import sensorarray_app.transport.wifi_discovery as wifi_discovery


def test_current_windows_ssids_treats_none_stdout_as_empty(monkeypatch):
    monkeypatch.setattr(
        wifi_discovery.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=None, returncode=1),
    )

    assert wifi_discovery.current_windows_ssids() == []


def test_current_windows_ssids_extracts_non_empty_ssids(monkeypatch):
    output = "\n".join(
        [
            "Interface name : Wi-Fi",
            "SSID 1 : CscArray_CEE500",
            "SSID 2 : Campus WiFi",
            "SSID 3 :   ",
        ]
    )
    monkeypatch.setattr(
        wifi_discovery.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=output, returncode=0),
    )

    assert wifi_discovery.current_windows_ssids() == ["CscArray_CEE500", "Campus WiFi"]
