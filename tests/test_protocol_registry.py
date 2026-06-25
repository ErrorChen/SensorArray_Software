from __future__ import annotations

import time
from pathlib import Path

from sensorarray_app.domain.models import CapacitanceFrame, LogRecord, TransportEnvelope
from sensorarray_app.protocol.registry import ProtocolRegistry

FIXTURES = Path(__file__).parent / "fixtures" / "b41"


def envelope(channel: str, payload: bytes, source: str = "ble") -> TransportEnvelope:
    return TransportEnvelope(source, channel, "device", 1, time.monotonic_ns(), time.time(), payload)


def test_log_channel_with_cap_ascii_payload_updates_cap_parser():
    registry = ProtocolRegistry()
    events = registry.feed(envelope("log", (FIXTURES / "rows1_valid.txt").read_bytes()))
    frames = [event for event in events if isinstance(event, CapacitanceFrame)]
    assert len(frames) == 1
    assert frames[0].rows == 1
    assert frames[0].sourceTransport == "ble"


def test_l_channel_with_cap_ascii_payload_is_normalized_and_parsed():
    registry = ProtocolRegistry()
    events = registry.feed(envelope("L", (FIXTURES / "rows2_valid.txt").read_bytes()))
    frames = [event for event in events if isinstance(event, CapacitanceFrame)]
    assert len(frames) == 1
    assert frames[0].rows == 2


def test_log_lines_do_not_create_capacitance_frames():
    registry = ProtocolRegistry()
    events = registry.feed(envelope("log", b"SF50,fps=12.5\nTR50,a=1\n"))
    assert not any(isinstance(event, CapacitanceFrame) for event in events)
    assert [event.tag for event in events if isinstance(event, LogRecord)] == ["SF50", "TR50"]


def test_reassembled_g_body_is_routed_again_by_content():
    from sensorarray_app.protocol.crc import crc32_reflected

    payload = (FIXTURES / "rows1_valid.txt").read_bytes()
    crc = crc32_reflected(payload)
    fragment = f"G,L,9,0,1,{len(payload)},{len(payload)},{crc:08X}\n".encode() + payload
    registry = ProtocolRegistry()
    events = registry.feed(envelope("log", fragment))
    assert any(isinstance(event, CapacitanceFrame) for event in events)
