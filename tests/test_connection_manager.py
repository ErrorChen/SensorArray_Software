from __future__ import annotations

import queue
import time

import pytest

from matrix_log_viewer import connection_manager as connection_manager_module
from matrix_log_viewer.connection_manager import ConnectionManager


def test_list_ports_without_pyserial_returns_empty(monkeypatch):
    monkeypatch.setattr(connection_manager_module, "list_ports", None)
    manager = ConnectionManager(queue.Queue())

    assert manager.listPorts() == []
    assert "pyserial" in manager.getStatus()["lastError"]


def test_connect_serial_validates_arguments():
    manager = ConnectionManager(queue.Queue())

    with pytest.raises(ValueError):
        manager.connectSerial("", 115200, False)
    with pytest.raises(ValueError):
        manager.connectSerial("COM5", 0, False)


def test_disconnect_is_repeatable():
    manager = ConnectionManager(queue.Queue())

    manager.disconnect()
    manager.disconnect()

    assert manager.getStatus()["mode"] == "disconnected"


def test_reconnect_without_history_reports_error():
    manager = ConnectionManager(queue.Queue())

    with pytest.raises(RuntimeError):
        manager.reconnect()
    assert "No previous serial" in manager.getStatus()["lastError"]


def test_reconnect_no_duplicate_reader(tmp_path):
    replay = tmp_path / "sample.bin"
    replay.write_bytes(b"abc" * 4096)
    manager = ConnectionManager(queue.Queue())

    manager.startReplay(str(replay), 0.1, readSize=4096)
    first = manager.reader
    time.sleep(0.02)
    manager.startReplay(str(replay), 0.1, readSize=4096)
    second = manager.reader
    try:
        assert first is not second
        assert first is None or not first.is_alive()
        assert second is not None
        assert second.is_alive()
    finally:
        manager.disconnect()
