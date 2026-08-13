from __future__ import annotations

import asyncio
import queue

import pytest

from sensorarray_app.constants import BLE_CTRL_TX_UUID, BLE_DATA_TX_UUID, BLE_LOG_TX_UUID
from sensorarray_app.domain.models import CommandTransactionEvent
from sensorarray_app.protocol.ble_fragments import BleFragmentReassembler
from sensorarray_app.protocol.crc import crc32_reflected
from sensorarray_app.protocol.registry import ProtocolRegistry
from sensorarray_app.services.command_service import CommandService
from sensorarray_app.transport.ble_transport import BleTransport


def make_fragment(channel: str, message_id: int, payload: bytes, index: int = 0, count: int = 1) -> bytes:
    crc = crc32_reflected(payload)
    return f"G,{channel},{message_id},{index},{count},{len(payload)},{len(payload)},{crc:08X}\n".encode() + payload


def make_fragments(channel: str, message_id: int, payload: bytes, split: int) -> list[bytes]:
    chunks = (payload[:split], payload[split:])
    crc = crc32_reflected(payload)
    return [
        f"G,{channel},{message_id},{index},2,{len(chunk)},{len(payload)},{crc:08X}\n".encode() + chunk
        for index, chunk in enumerate(chunks)
    ]


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


@pytest.mark.parametrize("fragmented_mack", [False, True])
def test_ff11_mack_and_fragmented_ff30_mapp_reach_strict_command_transaction(fragmented_mack: bool):
    """Exercise the real BLE notify/reassembly/registry/log/service path."""

    output = queue.Queue()
    transport = BleTransport(output, 7, "AA:BB")
    registry = ProtocolRegistry()
    service = CommandService()
    service.reset_session(7)
    sent: list[str] = []
    service.request_mode("RES", sent.append)

    # FF11 is registered as the existing ctrl notify and FF30 as the existing
    # log notify. No second client or duplicate subscription is involved.
    class Characteristic:
        def __init__(self, uuid: str):
            self.uuid = uuid
            self.properties = ["notify"]

    class Service:
        characteristics = [Characteristic(BLE_DATA_TX_UUID), Characteristic(BLE_LOG_TX_UUID), Characteristic(BLE_CTRL_TX_UUID)]

    mapping = transport._resolve_notify_characteristics([Service()])
    assert mapping["ctrl"] == BLE_CTRL_TX_UUID
    assert mapping["log"] == BLE_LOG_TX_UUID

    mack = b"MACK,id=42,old=CAP,new=RES,state=accepted\n"
    if fragmented_mack:
        for packet in make_fragments("ctrl", 10, mack, 15):
            transport._notify("ctrl", packet)
    else:
        transport._notify("ctrl", mack)
    while not output.empty():
        for event in registry.feed(output.get_nowait()):
            if isinstance(event, CommandTransactionEvent):
                service.handle(event)
    assert sent == ["MODE=RES"]
    assert service.pendingMode == "RES"
    assert service.appliedMode == "CAP"
    assert service.modeRequestId == 42

    mapp = b"MAPP,id=42,gen=9,old=CAP,new=RES,seq=301,state=applied\n"
    packets = make_fragments("log", 11, mapp, 21)
    transport._notify("log", packets[0])
    assert output.empty()
    transport._notify("log", packets[1])
    for event in registry.feed(output.get_nowait()):
        if isinstance(event, CommandTransactionEvent):
            service.handle(event)
    assert service.pendingMode is None
    assert service.appliedMode == "RES"
    assert service.modeRequestId == 42
    assert service.modeGeneration == 9
    assert service.modeFrameSeq == 301


def test_fragmented_ff30_wrong_id_mapp_is_rejected_end_to_end():
    output = queue.Queue()
    transport = BleTransport(output, 4, "AA:BB")
    registry = ProtocolRegistry()
    service = CommandService()
    service.reset_session(4)
    service.request_mode("VOLT", lambda _command: None)
    transport._notify("ctrl", b"MACK,id=7,old=CAP,new=VOLT,state=accepted\n")
    for event in registry.feed(output.get_nowait()):
        if isinstance(event, CommandTransactionEvent):
            service.handle(event)

    wrong = b"MAPP,id=99,gen=3,old=CAP,new=VOLT,seq=44,state=applied\n"
    for packet in make_fragments("log", 12, wrong, 18):
        transport._notify("log", packet)
    for event in registry.feed(output.get_nowait()):
        if isinstance(event, CommandTransactionEvent):
            service.handle(event)
    assert service.appliedMode == "CAP"
    assert service.pendingMode == "VOLT"
    assert service.modeRequestId == 7
