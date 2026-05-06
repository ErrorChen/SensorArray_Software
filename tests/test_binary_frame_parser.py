from __future__ import annotations

import binascii
import struct

from matrix_log_viewer.binary_frame_parser import (
    FMT,
    FRAME_TYPE_VOLTAGE_COMPACT,
    MAGIC,
    SIZE,
    SensorArrayBinaryFrameParser,
)
from matrix_log_viewer.protocol_parser import SensorArrayStreamParser


def build_binary_frame(seq: int = 7, valid_mask: int = (1 << 64) - 1, crc_delta: int = 0) -> bytes:
    values = list(range(64))
    fields = [
        MAGIC,
        1,
        FRAME_TYPE_VOLTAGE_COMPACT,
        seq,
        123456789,
        321,
        0x20,
        0x3001,
        0x3002,
        3,
        4,
        valid_mask,
        *values,
        15,
        2,
        0,
        0,
    ]
    raw = struct.pack(FMT, *fields)
    crc = (binascii.crc32(raw[: SIZE - 4]) + crc_delta) & 0xFFFFFFFF
    fields[-1] = crc
    return struct.pack(FMT, *fields)


def test_parse_valid_fast_binary_frame():
    frame = SensorArrayBinaryFrameParser().parseFrame(build_binary_frame())

    assert frame.frameType == "FAST_BINARY"
    assert frame.seq == 7
    assert frame.timestampUs == 123456789
    assert frame.durationUs == 321
    assert frame.statusFlags == 0x20
    assert frame.validMask == (1 << 64) - 1
    assert frame.values["S1D1"] == 0
    assert frame.values["S8D8"] == 63
    assert frame.unit == "uV"
    assert frame.adsDr == 15
    assert frame.outputDivider == 2


def test_crc_error_is_dropped_and_counted():
    parser = SensorArrayStreamParser()
    results = parser.feedBytes(build_binary_frame(crc_delta=1))

    assert results == []
    assert parser.getStats()["binaryCrcErrors"] == 1


def test_magic_resync_before_frame():
    parser = SensorArrayStreamParser()
    results = parser.feedBytes(b"garbage" + build_binary_frame(seq=8))

    assert len(results) == 1
    assert results[0].frame.seq == 8
    stats = parser.getStats()
    assert stats["binaryMagicResyncs"] == 1
    assert stats["skippedBytes"] == len(b"garbage")


def test_half_frame_then_complete_frame():
    parser = SensorArrayStreamParser()
    raw = build_binary_frame(seq=9)

    assert parser.feedBytes(raw[:20]) == []
    results = parser.feedBytes(raw[20:])

    assert len(results) == 1
    assert results[0].frame.seq == 9


def test_two_frames_in_one_feed():
    parser = SensorArrayStreamParser()
    results = parser.feedBytes(build_binary_frame(seq=1) + build_binary_frame(seq=2))

    assert [result.frame.seq for result in results] == [1, 2]
