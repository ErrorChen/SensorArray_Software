from __future__ import annotations

import binascii
import struct

from matrix_log_viewer.binary_frame_parser import FMT, FRAME_TYPE_VOLTAGE_COMPACT, MAGIC, SIZE
from matrix_log_viewer.protocol_parser import SensorArrayStreamParser


def build_binary_frame(seq: int) -> bytes:
    fields = [
        MAGIC,
        1,
        FRAME_TYPE_VOLTAGE_COMPACT,
        seq,
        seq * 1_000_000,
        100,
        0,
        0,
        0,
        0,
        0,
        (1 << 64) - 1,
        *list(range(64)),
        15,
        1,
        0,
        0,
    ]
    raw = struct.pack(FMT, *fields)
    fields[-1] = binascii.crc32(raw[: SIZE - 4]) & 0xFFFFFFFF
    return struct.pack(FMT, *fields)


def test_binary_stat_binary_mixed_stream():
    parser = SensorArrayStreamParser()
    data = build_binary_frame(1) + b"STAT,seq=1,drop=0,decimated=0,code=0x0000\n" + build_binary_frame(2)

    results = parser.feedBytes(data)

    assert [result.frame.seq for result in results if result.frame] == [1, 2]
    assert [result.status.statusType for result in results if result.status] == ["STAT"]
    stats = parser.getStats()
    assert stats["parsedBinaryFrames"] == 2
    assert stats["parsedStatuses"] == 1


def test_half_stat_line_is_preserved_until_newline():
    parser = SensorArrayStreamParser()

    assert parser.feedBytes(b"STAT,seq=10,") == []
    results = parser.feedBytes(b"code=0x6001\n")

    assert len(results) == 1
    assert results[0].status.statusType == "STAT"
    assert parser.getStats()["lastStatusCode"] == 0x6001


def test_binary_around_garbage_does_not_crash():
    parser = SensorArrayStreamParser()
    results = parser.feedBytes(b"\xff\x00garbage" + build_binary_frame(3) + b"\x81\x82" + b"EVENT,code=0x3002\n")

    assert any(result.frame and result.frame.seq == 3 for result in results)
    assert any(result.event and result.event.code == 0x3002 for result in results)
    stats = parser.getStats()
    assert stats["skippedBytes"] > 0
    assert stats["parseErrors"] == 0
