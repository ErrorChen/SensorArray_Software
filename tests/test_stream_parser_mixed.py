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


def test_appmode_binary_stat_binary_mixed_stream():
    parser = SensorArrayStreamParser()
    data = (
        b"APPMODE,mode=FastSpeed\n"
        + build_binary_frame(1)
        + b"STAT,fps=30.0,pps=1920,scanAvgUs=31000,drop=0,decimated=0,code=0x0000\n"
        + build_binary_frame(2)
    )

    results = parser.feedBytes(data)

    assert [result.frame.seq for result in results if result.frame] == [1, 2]
    assert [result.status.statusType for result in results if result.status] == ["APPMODE", "STAT"]
    stats = parser.getStats()
    assert stats["parsedBinaryFrames"] == 2
    assert stats["parsedStatuses"] == 2
    assert stats["binaryCrcErrors"] == 0
