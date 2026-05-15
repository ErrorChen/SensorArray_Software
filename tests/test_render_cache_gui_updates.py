from __future__ import annotations

import math

import numpy as np
import pytest

from matrix_log_viewer.config import CELL_NAMES
from matrix_log_viewer.data_store import MatrixDataStore
from matrix_log_viewer.protocol_types import MatrixFrame
from matrix_log_viewer.render_cache import HeatmapRenderCacheThread, HistoryRenderCacheThread


ALL_VALID = (1 << 64) - 1


def make_frame(seq: int, timestamp_us: int | None = None, unit: str = "uV", values: dict[str, float] | None = None) -> MatrixFrame:
    return MatrixFrame(
        frameType="FAST_BINARY",
        frameTypeName="FAST_BINARY",
        seq=seq,
        timestampUs=seq * 1_000_000 if timestamp_us is None else timestamp_us,
        durationUs=100,
        unit=unit,
        values=values or {cell: float(seq) for cell in CELL_NAMES},
        validMask=ALL_VALID,
        statusFlags=0,
        firstStatusCode=0,
        lastStatusCode=0,
        droppedFrames=0,
        outputDecimatedFrames=0,
        adsDr=15,
        outputDivider=1,
    )


def test_heatmap_auto_color_range_negative_mv():
    store = MatrixDataStore(maxPointsPerCell=10)
    matrix_mv = np.linspace(-95.0, -70.0, 64).reshape(8, 8)
    store.addFrame(make_frame(1, unit="mV", values={cell: float(value) for cell, value in zip(CELL_NAMES, matrix_mv.ravel())}))
    cache = HeatmapRenderCacheThread(store)

    cache.updateControls(stream="FAST_BINARY", unitMode="mV", colorMode="auto")
    snapshot = cache.getLatest()

    assert snapshot["unit"] == "mV"
    assert snapshot["zmin"] is not None
    assert snapshot["zmax"] is not None
    assert snapshot["zmin"] < -95.0
    assert snapshot["zmax"] > -70.0
    assert not (-0.5 < snapshot["zmin"] < 0.5 and -0.5 < snapshot["zmax"] < 0.5)
    assert snapshot["finiteMin"] == pytest.approx(-95.0)
    assert snapshot["finiteMax"] == pytest.approx(-70.0)


def test_heatmap_auto_color_range_equal_values_has_margin():
    store = MatrixDataStore(maxPointsPerCell=10)
    store.addFrame(make_frame(1, unit="mV", values={cell: -80.0 for cell in CELL_NAMES}))
    cache = HeatmapRenderCacheThread(store)

    cache.updateControls(stream="FAST_BINARY", unitMode="mV", colorMode="auto")
    snapshot = cache.getLatest()

    assert snapshot["zmin"] < -80.0
    assert snapshot["zmax"] > -80.0
    assert snapshot["zmin"] < snapshot["zmax"]


def test_heatmap_auto_color_range_ignores_nan_and_tracks_units():
    matrix_mv = np.linspace(-95.0, -70.0, 64)
    matrix_mv[10] = math.nan
    store = MatrixDataStore(maxPointsPerCell=10)
    store.addFrame(make_frame(1, unit="mV", values={cell: float(value) for cell, value in zip(CELL_NAMES, matrix_mv)}))
    cache = HeatmapRenderCacheThread(store)

    expected = {
        "uV": (-95_000.0, -70_000.0),
        "mV": (-95.0, -70.0),
        "V": (-0.095, -0.07),
    }
    for unit, (finite_min, finite_max) in expected.items():
        cache.updateControls(stream="FAST_BINARY", unitMode=unit, colorMode="auto")
        snapshot = cache.getLatest()

        assert snapshot["unit"] == unit
        assert snapshot["finiteMin"] == pytest.approx(finite_min)
        assert snapshot["finiteMax"] == pytest.approx(finite_max)
        assert snapshot["zmin"] < finite_min
        assert snapshot["zmax"] > finite_max
        assert unit in snapshot["cellText"][0][1]


def test_history_follow_latest_range_time_seconds():
    store = MatrixDataStore(maxPointsPerCell=200)
    for seq in range(101):
        store.addFrame(make_frame(seq, timestamp_us=seq * 1_000_000))
    cache = HistoryRenderCacheThread(store)

    cache.updateControls(stream="FAST_BINARY", selectedCell="S1D1", xAxis="timeSeconds", historyWindow="last_30s", followLatest=True)
    snapshot = cache.getLatest()

    assert snapshot["followRangeStart"] == pytest.approx(70.0)
    assert snapshot["followRangeEnd"] == pytest.approx(100.0)
    assert snapshot["latestX"] == pytest.approx(100.0)


def test_history_first_valid_data_after_empty_is_reset_with_lifecycle_fields():
    store = MatrixDataStore(maxPointsPerCell=20)
    cache = HistoryRenderCacheThread(store)
    cache.updateControls(stream="FAST_BINARY", selectedCell="S1D1", xAxis="timeSeconds", historyWindow="last_30s", followLatest=True)
    empty_snapshot = cache.getLatest()

    store.addFrame(make_frame(1, timestamp_us=1_000_000))
    with cache._lock:
        first_data = cache._build_snapshot_locked(force_reset=False)
        cache.latestHistorySnapshot = first_data

    assert empty_snapshot["reset"] is True
    assert first_data["reset"] is True
    assert first_data["revisionReason"] == "first_data"
    assert first_data["resetNonce"] > empty_snapshot["resetNonce"]
    assert first_data["clearRevision"] == empty_snapshot["clearRevision"]
    assert "followRangeStart" in first_data
    assert "followRangeEnd" in first_data


def test_history_append_snapshot_contains_lifecycle_and_follow_range():
    store = MatrixDataStore(maxPointsPerCell=20)
    for seq in range(3):
        store.addFrame(make_frame(seq, timestamp_us=seq * 1_000_000))
    cache = HistoryRenderCacheThread(store)
    cache.updateControls(stream="FAST_BINARY", selectedCell="S1D1", xAxis="timeSeconds", historyWindow="last_30s", followLatest=True)
    reset_snapshot = cache.getLatest()

    store.addFrame(make_frame(3, timestamp_us=3_000_000))
    with cache._lock:
        append_snapshot = cache._build_snapshot_locked(force_reset=False)

    assert reset_snapshot["reset"] is True
    assert append_snapshot["reset"] is False
    assert append_snapshot["revisionReason"] == "append"
    assert append_snapshot["resetNonce"] == reset_snapshot["resetNonce"]
    assert append_snapshot["clearRevision"] == reset_snapshot["clearRevision"]
    assert append_snapshot["followRangeStart"] is not None
    assert append_snapshot["followRangeEnd"] == pytest.approx(3.0)


def test_history_clear_increments_clear_revision_and_next_data_resets():
    store = MatrixDataStore(maxPointsPerCell=20)
    for seq in range(3):
        store.addFrame(make_frame(seq, timestamp_us=seq * 1_000_000))
    cache = HistoryRenderCacheThread(store)
    cache.updateControls(stream="FAST_BINARY", selectedCell="S1D1", xAxis="timeSeconds", historyWindow="last_30s", followLatest=True)
    before_clear = cache.getLatest()

    store.clear()
    cache.reset(reason="clear")
    cleared = cache.getLatest()
    store.addFrame(make_frame(10, timestamp_us=10_000_000))
    with cache._lock:
        first_after_clear = cache._build_snapshot_locked(force_reset=False)

    assert cleared["reset"] is True
    assert cleared["revisionReason"] == "clear"
    assert cleared["clearRevision"] > before_clear["clearRevision"]
    assert cleared["resetNonce"] > before_clear["resetNonce"]
    assert first_after_clear["reset"] is True
    assert first_after_clear["revisionReason"] == "first_data"
    assert first_after_clear["clearRevision"] == cleared["clearRevision"]


def test_history_key_change_resets_and_bumps_reset_nonce():
    store = MatrixDataStore(maxPointsPerCell=20)
    for seq in range(3):
        store.addFrame(make_frame(seq, timestamp_us=seq * 1_000_000))
    cache = HistoryRenderCacheThread(store)
    cache.updateControls(stream="FAST_BINARY", selectedCell="S1D1", xAxis="timeSeconds", historyWindow="last_30s", followLatest=True)
    first = cache.getLatest()

    cache.updateControls(stream="FAST_BINARY", selectedCell="S1D2", xAxis="timeSeconds", historyWindow="last_30s", followLatest=True)
    changed = cache.getLatest()

    assert changed["reset"] is True
    assert changed["revisionReason"] == "key_changed"
    assert changed["selectedCell"] == "S1D2"
    assert changed["resetNonce"] > first["resetNonce"]


def test_follow_latest_click_forces_revision():
    store = MatrixDataStore(maxPointsPerCell=20)
    for seq in range(5):
        store.addFrame(make_frame(seq))
    cache = HistoryRenderCacheThread(store)

    cache.updateControls(stream="FAST_BINARY", selectedCell="S1D1", xAxis="timeSeconds", historyWindow="last_30s", followLatest=True, followRevision=0)
    first = cache.getLatest()
    cache.updateControls(stream="FAST_BINARY", selectedCell="S1D1", xAxis="timeSeconds", historyWindow="last_30s", followLatest=True, followRevision=1)
    second = cache.getLatest()

    assert second["followRevision"] == 1
    assert second["followForced"] is True
    assert second["reset"] is True
    assert second["cacheRevision"] > first["cacheRevision"]
    assert second["followRangeEnd"] == pytest.approx(4.0)


def test_manual_zoom_can_be_recovered_by_follow_latest_revision():
    store = MatrixDataStore(maxPointsPerCell=20)
    for seq in range(5):
        store.addFrame(make_frame(seq))
    cache = HistoryRenderCacheThread(store)

    cache.updateControls(stream="FAST_BINARY", selectedCell="S1D1", xAxis="timeSeconds", historyWindow="last_30s", followLatest=False, followRevision=0)
    manual = cache.getLatest()
    cache.updateControls(stream="FAST_BINARY", selectedCell="S1D1", xAxis="timeSeconds", historyWindow="last_30s", followLatest=True, followRevision=1)
    restored = cache.getLatest()

    assert manual["followLatest"] is False
    assert restored["followLatest"] is True
    assert restored["followRevision"] == 1
    assert restored["followRangeEnd"] == pytest.approx(4.0)


def test_history_append_follow_range_tracks_browser_maxpoints_clip():
    store = MatrixDataStore(maxPointsPerCell=8000)
    sample_rate_hz = 120
    for seq in range(sample_rate_hz * 60):
        store.addFrame(make_frame(seq, timestamp_us=int(seq * 1_000_000 / sample_rate_hz)))
    cache = HistoryRenderCacheThread(store)
    cache.updateControls(
        stream="FAST_BINARY",
        selectedCell="S1D1",
        xAxis="timeSeconds",
        historyWindow="last_30s",
        followLatest=True,
        maxPoints=1200,
    )
    reset_snapshot = cache.getLatest()

    for seq in range(sample_rate_hz * 60, sample_rate_hz * 61):
        store.addFrame(make_frame(seq, timestamp_us=int(seq * 1_000_000 / sample_rate_hz)))
    with cache._lock:
        append_snapshot = cache._build_snapshot_locked(force_reset=False)
        cache.latestHistorySnapshot = append_snapshot

    assert len(reset_snapshot["x"]) <= 1200
    assert append_snapshot["reset"] is False
    assert append_snapshot["followRangeEnd"] == pytest.approx(61.0 - (1.0 / sample_rate_hz), rel=0, abs=0.02)
    assert append_snapshot["followRangeStart"] == pytest.approx(31.0 - (1.0 / sample_rate_hz), rel=0, abs=0.02)
