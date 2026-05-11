from __future__ import annotations

import binascii
import inspect
import math
import time

from matrix_log_viewer.app import _cell_name_from_click_data, _build_layout
from matrix_log_viewer.binary_frame_parser import FMT, SIZE, BinaryFrameParseError, SensorArrayBinaryFrameParser
from matrix_log_viewer.config import CELL_NAMES
from matrix_log_viewer.data_store import MatrixDataStore
from matrix_log_viewer.protocol_parser import SensorArrayStreamParser
from matrix_log_viewer.protocol_types import MatrixFrame
from matrix_log_viewer.render_cache import HeatmapRenderCacheThread, HistoryRenderCacheThread

from generate_sample_fast_binary import ALL_VALID, build_frame, startup_lines


def _frame(seq: int, s1d1: float = 1.0, s4d4: float = 44.0, valid_mask: int = ALL_VALID) -> MatrixFrame:
    values = {cell: float(index + seq) for index, cell in enumerate(CELL_NAMES)}
    values["S1D1"] = s1d1
    values["S4D4"] = s4d4
    return MatrixFrame(
        frameType="FAST_BINARY",
        frameTypeName="FAST_BINARY",
        seq=seq,
        timestampUs=seq * 1_000_000,
        durationUs=100,
        unit="uV",
        values=values,
        validMask=valid_mask,
        statusFlags=0,
        firstStatusCode=0,
        lastStatusCode=0,
        droppedFrames=0,
        outputDecimatedFrames=0,
        adsDr=15,
        outputDivider=1,
    )


def test_binary_frame_size_upper_speed():
    import struct

    assert SIZE == 312
    assert struct.calcsize(FMT) == 312


def test_binary_crc_upper_speed():
    good = build_frame(1)
    frame = SensorArrayBinaryFrameParser().parseFrame(good)

    assert frame.crc32Computed == binascii.crc32(good[:308]) & 0xFFFFFFFF

    bad = bytearray(good)
    bad[-1] ^= 0x55
    try:
        SensorArrayBinaryFrameParser().parseFrame(bytes(bad))
    except BinaryFrameParseError as exc:
        assert exc.kind == "crc"
        assert "frame=0x" in str(exc)
        assert "computed=0x" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("bad CRC frame parsed")


def test_mapping_order_upper_speed():
    values = list(range(64))
    frame = SensorArrayBinaryFrameParser().parseFrame(build_frame(9, values=values))

    assert frame.values["S1D1"] == 0
    assert frame.values["S1D2"] == 1
    assert frame.values["S1D8"] == 7
    assert frame.values["S2D1"] == 8
    assert frame.values["S8D8"] == 63


def test_valid_mask_invalid_nan_history_does_not_reuse_stale_value():
    store = MatrixDataStore(maxPointsPerCell=10)
    s4d4_bit = (4 - 1) * 8 + (4 - 1)
    store.addFrame(_frame(1, s4d4=123.0))
    store.addFrame(_frame(2, s4d4=456.0, valid_mask=ALL_VALID & ~(1 << s4d4_bit)))

    matrix = store.getLatestMatrix("FAST_BINARY")
    assert math.isnan(matrix[3, 3])

    _x, y, meta = store.getCellHistoryArrays("FAST_BINARY", "S4D4", "seq", "all")
    assert y[0] == 123.0
    assert math.isnan(y[1])
    assert meta["valid"].tolist() == [True, False]


def test_startup_text_then_fast_binary_start():
    parser = SensorArrayStreamParser()
    results = parser.feed(startup_lines() + build_frame(1) + build_frame(2))
    stats = parser.getStats()

    assert [result.frame.seq for result in results if result.frame] == [1, 2]
    assert stats["fastBinaryDiagCount"] == 1
    assert stats["fastBinaryStartSeen"] is True
    assert stats["pureBinaryMode"] is True
    assert stats["startupTextLineCount"] >= 2


def test_pure_binary_no_text_decode_errors():
    parser = SensorArrayStreamParser()
    results = parser.feed(build_frame(1) + build_frame(2))

    assert [result.frame.seq for result in results if result.frame] == [1, 2]
    assert parser.getStats()["parseErrors"] == 0


def test_ascii_after_fast_binary_start_warning_and_recovery():
    parser = SensorArrayStreamParser()
    payload = startup_lines() + build_frame(1) + b"STAT,seq=1,drop=0\nMATV_HEADER,seq\n" + build_frame(2)
    results = parser.feed(payload)
    stats = parser.getStats()

    assert [result.frame.seq for result in results if result.frame] == [1, 2]
    assert stats["protocolPollutionCount"] >= 1
    assert stats["lastWarning"] == "ASCII_AFTER_FAST_BINARY_START"


def test_crc_resync_recovery_upper_speed():
    parser = SensorArrayStreamParser()
    bad = build_frame(2, crc_delta=1)
    results = parser.feed(startup_lines() + build_frame(1) + bad + b"garbage" + build_frame(3))
    stats = parser.getStats()

    assert [result.frame.seq for result in results if result.frame] == [1, 3]
    assert stats["binaryCrcErrors"] == 1
    assert stats["binaryMagicResyncs"] >= 1
    assert stats["skippedBytes"] > 0


def test_upper_speed_diag_fields_and_partial_warning():
    parser = SensorArrayStreamParser()
    results = parser.feed(startup_lines(partial_after_first_byte=2, drop=3, decimated=4, output_div=8))
    status = [result.status for result in results if result.status and result.status.statusType == "FAST_BINARY_DIAG"][0]
    stats = parser.getStats()

    assert status.droppedBeforeFirstByte == 0
    assert status.partialAfterFirstByte == 2
    assert status.fullFrameWriteCount == 0
    assert status.latestDrop == 3
    assert status.latestOutputDiv == 8
    assert "PROTOCOL_RISK" in stats["lastWarning"]


def test_selected_cell_single_authority_layout_and_click():
    layout = _build_layout()

    assert "selected-cell-store" not in repr(layout)
    assert _cell_name_from_click_data({"points": [{"customdata": ["S4D4", "valid"]}]}) == "S4D4"


def test_history_switch_cell_resets_title_and_data():
    store = MatrixDataStore(maxPointsPerCell=10)
    store.addFrame(_frame(1, s1d1=10.0, s4d4=40.0))
    store.addFrame(_frame(2, s1d1=11.0, s4d4=41.0))
    cache = HistoryRenderCacheThread(store, targetFps=30)

    cache.updateControls(stream="FAST_BINARY", selectedCell="S1D1", xAxis="seq", unitMode="uV")
    first = cache.getLatest()
    cache.updateControls(stream="FAST_BINARY", selectedCell="S4D4", xAxis="seq", unitMode="uV")
    second = cache.getLatest()

    assert first["title"] == "History of S1D1 / FAST_BINARY"
    assert second["reset"] is True
    assert second["title"] == "History of S4D4 / FAST_BINARY"
    assert second["y"] == [40.0, 41.0]


def test_render_cache_decouples_input_and_gui():
    store = MatrixDataStore(maxPointsPerCell=200)
    cache = HeatmapRenderCacheThread(store, targetFps=30)
    cache.start()
    try:
        for seq in range(100):
            store.addFrame(_frame(seq))
        time.sleep(0.15)
    finally:
        cache.stop()
        cache.join(timeout=1.0)

    assert store.getFrameCount("FAST_BINARY") == 100
    assert cache.getLatest()["seq"] == 99
    assert cache.getStats()["renderSkipped"] > 0


def test_no_pandas_in_live_render_cache_path():
    source = inspect.getsource(__import__("matrix_log_viewer.render_cache", fromlist=["dummy"]))

    assert "pandas" not in source
    assert "pd.DataFrame" not in source


def test_clear_resets_cache_and_preserves_selected_cell():
    store = MatrixDataStore(maxPointsPerCell=10)
    store.addFrame(_frame(1))
    cache = HistoryRenderCacheThread(store)
    cache.updateControls(stream="FAST_BINARY", selectedCell="S4D4", xAxis="seq", unitMode="uV")
    store.clear()
    cache.reset()
    snapshot = cache.getLatest()

    assert store.getFrameCount("FAST_BINARY") == 0
    assert snapshot["reset"] is True
    assert snapshot["selectedCell"] == "S4D4"
    assert snapshot["x"] == []
