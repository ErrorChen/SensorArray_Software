from __future__ import annotations

from sensorarray_app.protocol.ble_fragments import BleFragmentReassembler
from sensorarray_app.protocol.crc import crc32_reflected


def test_ble_fragment_reassembly_out_of_order_and_crc():
    payload = b"C,seq=1,ts=1,rows=1,cells=8,gen=1,rid=1,rf=01,pf=01,sf=01,bad=0/0/0,fmt=pf6,n=8\n"
    crc = crc32_reflected(payload)
    first = f"G,data,1,0,2,10,{len(payload)},{crc:08X}\n".encode() + payload[:10]
    second = f"G,data,1,1,2,{len(payload) - 10},{len(payload)},{crc:08X}\n".encode() + payload[10:]
    reassembler = BleFragmentReassembler()
    assert reassembler.feed("data", second, 1) == []
    assert reassembler.feed("data", first, 2) == [("data", payload)]
    assert reassembler.stats.reassembled == 1


def test_ble_fragment_crc_failure_is_dropped():
    payload = b"AB50,bt=1\n"
    fragment = f"G,log,2,0,1,{len(payload)},{len(payload)},DEADBEEF\n".encode() + payload
    reassembler = BleFragmentReassembler()
    assert reassembler.feed("log", fragment, 1) == []
    assert reassembler.stats.crcFailure == 1
