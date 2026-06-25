from __future__ import annotations

import asyncio
import queue

from sensorarray_app.protocol.ble_fragments import BleFragmentReassembler
from sensorarray_app.protocol.crc import crc32_reflected
from sensorarray_app.transport.ble_transport import BleTransport


def make_fragment(channel: str, message_id: int, payload: bytes, index: int = 0, count: int = 1) -> bytes:
    crc = crc32_reflected(payload)
    return f"G,{channel},{message_id},{index},{count},{len(payload)},{len(payload)},{crc:08X}\n".encode() + payload


def test_ble_single_fragment_reassembles_and_normalizes_channel():
    payload = b"C,seq=1,rows=1\n"
    reassembler = BleFragmentReassembler()
    assert reassembler.feed("log", make_fragment("D", 7, payload), 1) == [("data", payload)]
    assert reassembler.stats.reassembled == 1


def test_ble_fragment_reassembly_out_of_order_and_crc():
    payload = b"C,seq=1,ts=1,rows=1,cells=8,gen=1,rid=1,rf=01,pf=01,sf=01,bad=0/0/0,fmt=pf6,n=8\n"
    crc = crc32_reflected(payload)
    first = f"G,data,1,0,2,10,{len(payload)},{crc:08X}\n".encode() + payload[:10]
    second = f"G,data,1,1,2,{len(payload) - 10},{len(payload)},{crc:08X}\n".encode() + payload[10:]
    reassembler = BleFragmentReassembler()
    assert reassembler.feed("data", second, 1) == []
    assert reassembler.feed("data", first, 2) == [("data", payload)]
    assert reassembler.stats.reassembled == 1


def test_ble_notify_with_multiple_fragments_returns_multiple_payloads():
    first_payload = b"AB50,bt=1\n"
    second_payload = b"SF50,fps=10\n"
    notify = make_fragment("L", 1, first_payload) + make_fragment("log", 2, second_payload)
    reassembler = BleFragmentReassembler()
    assert reassembler.feed("log", notify, 1) == [("log", first_payload), ("log", second_payload)]


def test_ble_notify_with_log_and_fragment_mixed_payload():
    payload = b"C,seq=2,rows=1\n"
    notify = b"SF50,fps=10\n" + make_fragment("", 3, payload)
    reassembler = BleFragmentReassembler()
    assert reassembler.feed("data", notify, 1) == [("data", b"SF50,fps=10\n"), ("data", payload)]


def test_ble_duplicate_and_missing_timeout_are_counted():
    payload = b"AB50,bt=1\n"
    crc = crc32_reflected(payload)
    fragment = f"G,log,2,0,2,5,{len(payload)},{crc:08X}\n".encode() + payload[:5]
    reassembler = BleFragmentReassembler(timeout_ns=10)
    assert reassembler.feed("log", fragment, 1) == []
    assert reassembler.feed("log", fragment, 2) == []
    assert reassembler.stats.duplicate == 1
    assert reassembler.feed("log", b"SF50,fps=1\n", 20) == [("log", b"SF50,fps=1\n")]
    assert reassembler.stats.timeout == 1


def test_ble_fragment_crc_failure_is_dropped():
    payload = b"AB50,bt=1\n"
    fragment = f"G,log,2,0,1,{len(payload)},{len(payload)},DEADBEEF\n".encode() + payload
    reassembler = BleFragmentReassembler()
    assert reassembler.feed("log", fragment, 1) == []
    assert reassembler.stats.crcFailure == 1


def test_ble_fragment_length_failure_is_dropped():
    fragment = b"G,log,2,0,1,99,3,00000000\nabc"
    reassembler = BleFragmentReassembler()
    assert reassembler.feed("log", fragment, 1) == []
    assert reassembler.stats.lengthFailure == 1


def test_ble_write_uses_ctrl_characteristic():
    class FakeClient:
        is_connected = True

        def __init__(self):
            self.calls = []

        async def write_gatt_char(self, uuid: str, payload: bytes, response: bool):
            self.calls.append((uuid, payload, response))

    client = FakeClient()
    transport = BleTransport(queue.Queue(), 1, "AA:BB")
    transport._client = client
    transport._ctrl_rx_uuid = "ctrl-uuid"
    transport._ctrl_write_response = False
    assert asyncio.run(transport._write_gatt(b"PING")) == 4
    assert client.calls == [("ctrl-uuid", b"PING", False)]


def test_ble_write_without_ctrl_characteristic_is_clear_error():
    class FakeClient:
        is_connected = True

    transport = BleTransport(queue.Queue(), 1, "AA:BB")
    transport._client = FakeClient()
    transport._ctrl_rx_uuid = None
    try:
        asyncio.run(transport._write_gatt(b"PING"))
    except NotImplementedError as exc:
        assert "ctrl characteristic" in str(exc)
    else:
        raise AssertionError("BLE write without ctrl characteristic succeeded")
