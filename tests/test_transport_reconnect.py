from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
import queue
import sys
import time
from types import SimpleNamespace

import pytest

from sensorarray_app.constants import BLE_CTRL_RX_UUID, BLE_CTRL_TX_UUID, BLE_DATA_TX_UUID, BLE_LOG_TX_UUID
from sensorarray_app.domain.models import TransportEnvelope, TransportStateEvent
from sensorarray_app.protocol.crc import crc32_reflected
from sensorarray_app.transport import ble_transport as ble_module
from sensorarray_app.transport import serial_transport as serial_module
from sensorarray_app.transport.base import TransportWriteOutcomeUnknown
from sensorarray_app.transport.ble_transport import BleTransport
from sensorarray_app.transport.serial_transport import SerialTransport


class _Characteristic:
    def __init__(self, uuid: str, properties: list[str]):
        self.uuid = uuid
        self.properties = properties


class _Service:
    def __init__(self):
        self.characteristics = [
            _Characteristic(BLE_DATA_TX_UUID, ["notify"]),
            _Characteristic(BLE_LOG_TX_UUID, ["notify"]),
            _Characteristic(BLE_CTRL_TX_UUID, ["notify"]),
            _Characteristic(BLE_CTRL_RX_UUID, ["write"]),
        ]


def test_ble_disconnect_backoff_rescan_new_client_and_resubscribe(monkeypatch: pytest.MonkeyPatch):
    clients: list[object] = []
    scans: list[str] = []
    output: queue.Queue = queue.Queue()
    transport = BleTransport(output, 5, "AA:BB", ble_device=SimpleNamespace(address="AA:BB"))

    class Scanner:
        @staticmethod
        async def find_device_by_address(address: str, timeout: float):
            scans.append(address)
            return SimpleNamespace(address=address, name="CscArray_test")

    class Client:
        def __init__(self, target, disconnected_callback=None):
            self.target = target
            self.callback = disconnected_callback
            self.is_connected = False
            self.services = [_Service()]
            self.subscriptions: list[tuple[str, object]] = []
            self.stopped: list[str] = []
            self.disconnected = False
            clients.append(self)

        async def connect(self):
            self.is_connected = True

        async def start_notify(self, uuid: str, callback):
            self.subscriptions.append((uuid, callback))
            if len(self.subscriptions) == 3:
                loop = asyncio.get_running_loop()
                if len(clients) == 1:
                    loop.call_soon(self.callback, self)
                else:
                    transport._stop_requested.set()
                    loop.call_soon(self.callback, self)

        async def stop_notify(self, uuid: str):
            self.stopped.append(uuid)

        async def disconnect(self):
            self.is_connected = False
            self.disconnected = True

    monkeypatch.setitem(sys.modules, "bleak", SimpleNamespace(BleakClient=Client, BleakScanner=Scanner))

    async def exercise() -> None:
        transport._loop = asyncio.get_running_loop()
        await transport._connection_loop()

    asyncio.run(exercise())
    events = list(output.queue)
    states = [event.state for event in events if isinstance(event, TransportStateEvent)]
    assert len(clients) == 2
    assert scans == ["AA:BB"]
    assert all(len(client.subscriptions) == 3 for client in clients)  # type: ignore[attr-defined]
    assert all(len(client.stopped) == 3 and client.disconnected for client in clients)  # type: ignore[attr-defined]
    assert states.count("STREAMING") == 2
    assert "RECONNECT_WAIT" in states and "SCANNING" in states
    assert transport.connectionGeneration == 2
    assert transport.reconnectAttempt == 1

    # A notification callback retained by the first client must not be
    # relabelled with connection generation 2.
    while not output.empty():
        output.get_nowait()
    first_callback = clients[0].subscriptions[0][1]  # type: ignore[attr-defined]
    first_callback(None, b"BOOT,bootId=999\n")
    assert output.empty()
    assert transport._notify_failures["data"] == 1


def test_ble_fragment_state_is_reset_at_each_successful_connection(monkeypatch: pytest.MonkeyPatch):
    output: queue.Queue = queue.Queue()
    transport = BleTransport(output, 1, "AA:BB")
    payload = b"BOOT,bootId=5\n"
    crc = crc32_reflected(payload)
    first = f"G,L,7,0,2,4,{len(payload)},{crc:08X}\n".encode() + payload[:4]
    second = f"G,L,7,1,2,{len(payload) - 4},{len(payload)},{crc:08X}\n".encode() + payload[4:]
    assert transport._fragmenter.feed("log", first, 1) == []

    class Client:
        is_connected = True
        services = [_Service()]

        def __init__(self, target, disconnected_callback=None):
            self.callback = disconnected_callback

        async def connect(self):
            return None

        async def start_notify(self, uuid, callback):
            if uuid == BLE_CTRL_TX_UUID:
                transport._stop_requested.set()

        async def stop_notify(self, uuid):
            return None

        async def disconnect(self):
            self.is_connected = False

    monkeypatch.setitem(sys.modules, "bleak", SimpleNamespace(BleakClient=Client))

    async def exercise() -> None:
        transport._loop = asyncio.get_running_loop()
        transport.connectionAttemptGeneration = 1
        assert await transport._connect_once(1) is False

    asyncio.run(exercise())
    assert transport._fragmenter.feed("log", second, 2) == []


def test_ble_manual_stop_confirms_worker_and_client_shutdown(monkeypatch: pytest.MonkeyPatch):
    clients: list[object] = []

    class Client:
        def __init__(self, target, disconnected_callback=None):
            self.callback = disconnected_callback
            self.is_connected = False
            self.services = [_Service()]
            self.disconnected = False
            clients.append(self)

        async def connect(self):
            self.is_connected = True

        async def start_notify(self, uuid, callback):
            return None

        async def stop_notify(self, uuid):
            return None

        async def disconnect(self):
            self.is_connected = False
            self.disconnected = True

    monkeypatch.setitem(sys.modules, "bleak", SimpleNamespace(BleakClient=Client))
    output: queue.Queue = queue.Queue()
    transport = BleTransport(output, 1, "AA:BB", ble_device=SimpleNamespace(address="AA:BB"))
    transport.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if any(isinstance(item, TransportStateEvent) and item.state == "STREAMING" for item in list(output.queue)):
            break
        time.sleep(0.005)
    transport.stop()
    assert transport._thread is None
    assert transport._stopped.is_set()
    assert transport.userRequestedDisconnect is True
    assert len(clients) == 1
    assert clients[0].disconnected is True  # type: ignore[attr-defined]


def test_ble_write_timeout_is_outcome_unknown_and_never_retried(monkeypatch: pytest.MonkeyPatch):
    class PendingFuture:
        calls = 0
        cancelled = False

        def result(self, timeout: float):
            self.calls += 1
            raise FutureTimeoutError()

        def cancel(self):
            self.cancelled = True

    future = PendingFuture()

    def submit(coroutine, loop):
        coroutine.close()
        return future

    monkeypatch.setattr(ble_module.asyncio, "run_coroutine_threadsafe", submit)
    transport = BleTransport(queue.Queue(), 1, "AA:BB")
    transport._loop = object()  # type: ignore[assignment]
    transport._client = SimpleNamespace(is_connected=True)
    with pytest.raises(TransportWriteOutcomeUnknown, match="outcome is unknown"):
        transport.write(b"MODE=RES\n")
    assert future.calls == 1
    assert future.cancelled is False


def test_ble_saturated_queue_preserves_control_and_lifecycle_in_priority_backlog():
    output: queue.Queue = queue.Queue(maxsize=1)
    output.put(object())
    transport = BleTransport(output, 1, "AA:BB")
    transport.connectionGeneration = 1
    transport._notify("ctrl", b"MACK,id=1,old=CAP,new=RES,state=accepted\n")
    transport._put_state("DISCONNECTED", "link lost")
    transport._notify("data", b"C,seq=1\n")
    assert transport.priorityBacklog == 2
    assert transport.queueCounters["controlDrops"] == 0
    assert transport.queueCounters["lifecycleDrops"] == 0
    assert transport.queueCounters["measurementDrops"] == 1
    output.get_nowait()
    transport._drain_priority_backlog()
    assert isinstance(output.get_nowait(), TransportEnvelope)


@dataclass
class _Port:
    device: str
    vid: int | None = 0x303A
    pid: int | None = 0x1001
    serial_number: str = "BOARD-1"
    location: str = "1-2"
    hwid: str = "USB VID:PID=303A:1001"
    name: str = ""
    description: str = "SensorArray"
    manufacturer: str = "Espressif"
    product: str = "USB JTAG/serial"


def test_serial_resolves_same_physical_device_after_port_renumber(monkeypatch: pytest.MonkeyPatch):
    ports = SimpleNamespace(comports=lambda: [_Port("COM12")])
    monkeypatch.setattr(serial_module, "list_ports", ports)
    transport = SerialTransport(queue.Queue(), 1, "COM12")
    ports.comports = lambda: [_Port("COM13")]
    assert transport._resolve_reconnect_port() == "COM13"


def test_serial_refuses_ambiguous_reconnect_target(monkeypatch: pytest.MonkeyPatch):
    ports = SimpleNamespace(comports=lambda: [_Port("COM12")])
    monkeypatch.setattr(serial_module, "list_ports", ports)
    transport = SerialTransport(queue.Queue(), 1, "COM12")
    ports.comports = lambda: [_Port("COM13"), _Port("COM14")]
    assert transport._resolve_reconnect_port() is None


def test_serial_auto_reconnect_discards_partial_epoch_and_reopens_renumbered_port(monkeypatch: pytest.MonkeyPatch):
    output: queue.Queue = queue.Queue()
    ports_calls = 0

    def comports():
        nonlocal ports_calls
        ports_calls += 1
        return [_Port("COM12" if ports_calls == 1 else "COM13")]

    monkeypatch.setattr(serial_module, "list_ports", SimpleNamespace(comports=comports))
    opened: list[str] = []
    transport_holder: dict[str, SerialTransport] = {}

    class FirstHandle:
        def __init__(self):
            self.reads = 0

        def read(self, size: int):
            self.reads += 1
            if self.reads == 1:
                return b"V,seq=1,partial"
            raise OSError("USB unplugged")

        def close(self):
            return None

    class SecondHandle:
        def read(self, size: int):
            transport_holder["transport"]._stop.set()
            return b"BOOT,bootId=2\n"

        def close(self):
            return None

    configured: list[dict[str, object]] = []

    class SerialFactory:
        port = ""
        baudrate = 0
        timeout = 0.0
        write_timeout = 0.0
        dtr = True
        rts = True

        def open(self):
            configured.append(
                {
                    "port": self.port,
                    "baudrate": self.baudrate,
                    "timeout": self.timeout,
                    "write_timeout": self.write_timeout,
                    "dtr": self.dtr,
                    "rts": self.rts,
                }
            )
            opened.append(self.port)
            self._delegate = FirstHandle() if len(opened) == 1 else SecondHandle()

        def read(self, size: int):
            return self._delegate.read(size)

        def close(self):
            return self._delegate.close()

    monkeypatch.setattr(serial_module, "serial", SimpleNamespace(Serial=SerialFactory))
    transport = SerialTransport(output, 3, "COM12", auto_reconnect=True)
    transport_holder["transport"] = transport
    transport.start()
    deadline = time.monotonic() + 3.0
    while transport._thread is not None and transport._thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    transport.stop()

    items = list(output.queue)
    states = [item.state for item in items if isinstance(item, TransportStateEvent)]
    payloads = [item.rawPayload for item in items if isinstance(item, TransportEnvelope)]
    assert opened == ["COM12", "COM13"]
    assert configured == [
        {
            "port": "COM12",
            "baudrate": 115200,
            "timeout": 0.05,
            "write_timeout": 1.0,
            "dtr": False,
            "rts": False,
        },
        {
            "port": "COM13",
            "baudrate": 115200,
            "timeout": 0.05,
            "write_timeout": 1.0,
            "dtr": False,
            "rts": False,
        },
    ]
    assert transport.connectionGeneration == 2
    assert "CONNECTION_RESET" in states and "REENUMERATED" in states
    reset_index = states.index("CONNECTION_RESET")
    reenumerated_index = states.index("REENUMERATED")
    second_streaming_index = states.index("STREAMING", reset_index)
    assert reset_index < reenumerated_index < second_streaming_index
    assert payloads == [b"V,seq=1,partial", b"BOOT,bootId=2\n"]


def test_serial_expected_restart_reopens_stale_cdc_handle_without_error(monkeypatch: pytest.MonkeyPatch):
    output: queue.Queue = queue.Queue()
    monkeypatch.setattr(
        serial_module,
        "list_ports",
        SimpleNamespace(comports=lambda: [_Port("COM12")]),
    )
    opened: list[str] = []
    holder: dict[str, SerialTransport] = {}

    class SerialFactory:
        port = ""
        baudrate = 0
        timeout = 0.0
        write_timeout = 0.0
        dtr = True
        rts = True

        def open(self):
            opened.append(self.port)

        def read(self, size: int):
            if len(opened) == 1:
                time.sleep(0.005)
                return b""
            holder["transport"]._stop.set()
            return b"BOOT,bootId=2\n"

        def close(self):
            return None

    monkeypatch.setattr(serial_module, "serial", SimpleNamespace(Serial=SerialFactory))
    transport = SerialTransport(output, 3, "COM12", auto_reconnect=True)
    holder["transport"] = transport
    transport.start()
    deadline = time.monotonic() + 1.0
    while not any(
        isinstance(item, TransportStateEvent) and item.state == "STREAMING"
        for item in list(output.queue)
    ) and time.monotonic() < deadline:
        time.sleep(0.005)

    transport.request_reconnect("firmware acknowledged expected RESTART")
    deadline = time.monotonic() + 2.0
    while transport._thread is not None and transport._thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    transport.stop()

    items = list(output.queue)
    states = [item.state for item in items if isinstance(item, TransportStateEvent)]
    payloads = [item.rawPayload for item in items if isinstance(item, TransportEnvelope)]
    assert opened == ["COM12", "COM12"]
    assert states.count("STREAMING") == 2
    assert "CONNECTION_RESET" in states
    assert "RECONNECTING" in states
    assert "ERROR" not in states
    assert payloads == [b"BOOT,bootId=2\n"]


def test_serial_queue_overflow_is_explicit_before_next_raw_chunk(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(serial_module, "list_ports", None)
    output: queue.Queue = queue.Queue(maxsize=2)
    output.put(object())
    output.put(object())
    transport = SerialTransport(output, 1, "COM12")
    transport._put_envelope(b"old")
    assert transport.rawQueueOverflow == 1
    assert transport._overflow_pending is True
    output.get_nowait()
    output.get_nowait()
    transport._put_envelope(b"new")
    first = output.get_nowait()
    second = output.get_nowait()
    assert isinstance(first, TransportStateEvent) and first.state == "SERIAL_RX_OVERFLOW"
    assert isinstance(second, TransportEnvelope) and second.rawPayload == b"new"
