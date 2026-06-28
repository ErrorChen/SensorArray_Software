from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np

from sensorarray_app.domain.models import CapacitanceFrame, ParserErrorEvent, TransportEnvelope
from sensorarray_app.protocol.cap_ascii import CapAsciiParser
from sensorarray_app.protocol.crc import crc32_reflected

FIXTURES = Path(__file__).parent / "fixtures" / "b41"


def envelope(payload: bytes, ns: int = 1_000_000_000) -> TransportEnvelope:
    return TransportEnvelope("serial", "data", "SERIAL_TEST_PORT", 1, ns, time.time(), payload)


def parse_fixture(name: str) -> list:
    parser = CapAsciiParser()
    return parser.feed(envelope((FIXTURES / name).read_bytes()))


def test_rows_1_2_4_8_valid_crc_and_dynamic_cells():
    for filename, rows in [("rows1_valid.txt", 1), ("rows2_valid.txt", 2), ("rows4_valid.txt", 4), ("rows8_valid.txt", 8)]:
        events = parse_fixture(filename)
        frames = [event for event in events if isinstance(event, CapacitanceFrame)]
        assert len(frames) == 1
        assert frames[0].rows == rows
        assert frames[0].cells == rows * 8
        body = "\n".join((FIXTURES / filename).read_text(encoding="ascii").splitlines()[:-1]) + "\n"
        assert crc32_reflected(body.encode("ascii")) == int(frames[0].rawTrailer.split("crc=")[1], 16)


def test_invalid_sentinel_is_nan_before_offset():
    frame = [event for event in parse_fixture("invalid_sentinel.txt") if isinstance(event, CapacitanceFrame)][0]
    assert math.isnan(frame.rawPfValues[0])
    assert math.isnan(frame.correctedPfValues[0])
    assert frame.validMask[0] == np.False_
    assert frame.correctedPfValues[1] == 1.0


def test_crc_failure_and_missing_duplicate_data_reject():
    for filename in ["crc_failure.txt", "missing_data.txt", "duplicate_data.txt"]:
        events = parse_fixture(filename)
        assert any(isinstance(event, ParserErrorEvent) for event in events), filename
        assert not any(isinstance(event, CapacitanceFrame) for event in events), filename


def test_sequence_gap_is_counted():
    parser = CapAsciiParser()
    events = parser.feed(envelope((FIXTURES / "sequence_gap.txt").read_bytes()))
    assert len([event for event in events if isinstance(event, CapacitanceFrame)]) == 2
    assert parser.stats.sequenceGaps == 1


def test_strict_ascii_rejects_invalid_bytes():
    parser = CapAsciiParser()
    events = parser.feed(envelope(b"C,seq=1,\xff\n"))
    assert any(isinstance(event, ParserErrorEvent) and event.reason == "strict_ascii" for event in events)
