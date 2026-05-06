from __future__ import annotations

import math

import numpy as np

from matrix_log_viewer.config import CELL_NAMES
from matrix_log_viewer.data_store import MatrixDataStore
from matrix_log_viewer.protocol_types import MatrixFrame


def make_frame(frame_type: str, seq: int, timestamp_us: int, valid_mask: int | None = None) -> MatrixFrame:
    return MatrixFrame(
        frameType=frame_type,
        seq=seq,
        timestampUs=timestamp_us,
        durationUs=100,
        unit="uV" if frame_type in ("FAST_BINARY", "MATV") else "raw",
        values={cell: float(index + seq) for index, cell in enumerate(CELL_NAMES)},
        validMask=valid_mask,
        statusFlags=0,
        firstStatusCode=0,
        lastStatusCode=0,
        droppedFrames=0,
        outputDecimatedFrames=0,
        adsDr=15 if frame_type == "FAST_BINARY" else None,
        outputDivider=1 if frame_type == "FAST_BINARY" else None,
    )


def test_streams_are_stored_independently_and_valid_mask_sets_nan():
    store = MatrixDataStore(maxPointsPerCell=10)
    store.addFrame(make_frame("FAST_BINARY", 1, 1_000_000, valid_mask=((1 << 64) - 1) ^ 0x1))
    store.addFrame(make_frame("MATV", 2, 2_000_000))

    fast_matrix = store.getLatestMatrix("FAST_BINARY")
    matv_matrix = store.getLatestMatrix("MATV")

    assert math.isnan(fast_matrix[0, 0])
    assert np.isfinite(fast_matrix[0, 1])
    assert matv_matrix[0, 0] == 2
    assert "FAST_BINARY" in store.getAvailableFrameTypes()
    assert "MATV" in store.getAvailableFrameTypes()


def test_history_windows_and_wide_csv_meta():
    store = MatrixDataStore(maxPointsPerCell=100)
    for seq in range(60):
        store.addFrame(make_frame("FAST_BINARY", seq, seq * 1_000_000))

    last_n = store.getCellHistory("FAST_BINARY", "S1D1", "seq", "last_n", 5, None, None)
    last_30s = store.getCellHistory("FAST_BINARY", "S1D1", "timeSeconds", "last_30s", None, None, None)
    all_rows = store.getCellHistory("FAST_BINARY", "S1D1", "timeSeconds", "all", None, None, None)
    wide = store.toWideDataFrame("FAST_BINARY")

    assert len(last_n) == 5
    assert last_n["seq"].tolist() == [55, 56, 57, 58, 59]
    assert last_30s["timeSeconds"].min() >= 29
    assert len(all_rows) == 60
    assert "frame_type" in wide.columns
    assert "status_flags" in wide.columns


def test_downsample_limits_rendered_points_and_keeps_peaks():
    store = MatrixDataStore(maxPointsPerCell=12000)
    for seq in range(10000):
        frame = make_frame("FAST_BINARY", seq, seq * 1_000)
        values = dict(frame.values)
        values["S1D1"] = 10000.0 if seq == 5000 else float(seq % 10)
        store.addFrame(MatrixFrame(**{**frame.__dict__, "values": values}))

    history = store.getCellHistory("FAST_BINARY", "S1D1", "seq", "all", None, None, None)
    rendered, downsampled = MatrixDataStore.downsampleHistoryFrame(history, "seq", 5000)

    assert downsampled is True
    assert len(rendered) <= 5000
    assert rendered["value"].max() == 10000.0
