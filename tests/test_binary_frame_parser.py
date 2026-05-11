from __future__ import annotations

import binascii
import struct

import pytest

from matrix_log_viewer.binary_frame_parser import (
    FMT,
    FRAME_TYPE_VOLTAGE_COMPACT,
    MAGIC,
    SIZE,
    BinaryFrameParseError,
    SensorArrayBinaryFrameParser,
)
from matrix_log_viewer.data_store import MatrixDataStore
from matrix_log_viewer.protocol_parser import SensorArrayStreamParser


def build_binary_frame(
    seq: int = 7,
    valid_mask: int = (1 << 64) - 1,
    crc_delta: int = 0,
    version: int = 1,
    frame_type: int = FRAME_TYPE_VOLTAGE_COMPACT,
) -> bytes:
    values = list(range(64))
    fields = [
        MAGIC,
        version,
        frame_type,
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
    assert frame.outputDecimatedFrames == 4
    assert frame.droppedFramesSaturated == 3
    assert frame.outputDecimatedFramesSaturated == 4
    assert frame.parserFrameSize == 312
    assert frame.rawBytes == build_binary_frame()


def test_struct_size_is_312():
    assert SIZE == 312
    assert struct.calcsize(FMT) == 312


def test_format_matches_expected_layout():
    assert FMT == "<IHHIQIIIIHHQ64iBBHI"


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


def test_valid_mask_sets_nan_in_store():
    store = MatrixDataStore()
    frame = SensorArrayBinaryFrameParser().parseFrame(build_binary_frame(valid_mask=((1 << 64) - 1) ^ 0x1))
    store.addFrame(frame)

    matrix = store.getLatestMatrix("FAST_BINARY")
    assert matrix[0, 0] != matrix[0, 0]
    assert matrix[0, 1] == 1


def test_bad_version_rejected():
    with pytest.raises(BinaryFrameParseError) as excinfo:
        SensorArrayBinaryFrameParser().parseFrame(build_binary_frame(version=2))

    assert excinfo.value.kind == "version"


def test_bad_frame_type_rejected():
    with pytest.raises(BinaryFrameParseError) as excinfo:
        SensorArrayBinaryFrameParser().parseFrame(build_binary_frame(frame_type=0x9999))

    assert excinfo.value.kind == "frameType"


def test_binary_with_text_before_and_after():
    parser = SensorArrayStreamParser()
    results = parser.feedBytes(
        b"APPMODE,mode=fast\n" + build_binary_frame(seq=10) + b"STAT,fps=30.0,code=0x0000\n"
    )

    assert [result.frame.seq for result in results if result.frame] == [10]
    assert [result.status.statusType for result in results if result.status] == ["APPMODE", "STAT"]
