from __future__ import annotations

import numpy as np

from matrix_log_viewer.app import (
    _build_compact_status_bar,
    _build_connection_panel,
    _build_device_panel,
    _build_parser_diagnostics,
    _is_missing_value,
    _safe_float,
    _safe_int,
    _warning_badge,
)


def chip_values(chips):
    return {chip.children[0].children: chip.children[1].children for chip in chips}


def test_safe_int_handles_missing_decimal_and_hex_values():
    assert _safe_int(None) == 0
    assert _safe_int("-") == 0
    assert _safe_int("") == 0
    assert _safe_int("123") == 123
    assert _safe_int("123.0") == 123
    assert _safe_int("0x10") == 16
    assert _safe_int(np.nan) == 0


def test_safe_float_handles_missing_and_decimal_values():
    assert _safe_float("-") == 0.0
    assert _safe_float("1.25") == 1.25
    assert _is_missing_value("N/A") is True
    assert _is_missing_value(np.nan) is True


def test_compact_status_bar_handles_missing_device_drop_without_crashing():
    warning, _state = _warning_badge({}, {}, {}, {"droppedFrames": "-"}, {})
    chips = _build_compact_status_bar(
        meta={"seq": "-", "droppedFrames": "-", "lastStatusCode": None},
        matrix=np.full((8, 8), np.nan),
        selected_cell="S1D1",
        parser_stats={},
        connection_status={},
        runtime_stats={},
        paused=False,
        display_unit="mV",
        display_type="FAST_BINARY",
        warning_badge=warning,
    )

    values = chip_values(chips)
    assert values["selected"] == "S1D1 -"
    assert values["seq"] == "-"
    assert values["status"] == "-"
    assert values["warning"] == "clear"


def test_compact_status_bar_handles_empty_inputs_without_crashing():
    chips = _build_compact_status_bar(
        meta={},
        matrix=np.full((8, 8), np.nan),
        selected_cell="S1D1",
        parser_stats={},
        connection_status={},
        runtime_stats={},
        paused=False,
        display_unit="uV",
        display_type="FAST_BINARY",
    )

    values = chip_values(chips)
    assert values["connection"].startswith("Disconnected")
    assert values["seq"] == "-"
    assert values["warning"] == "clear"


def test_warning_badge_shows_crc_resync_and_drop_when_positive():
    warning, state = _warning_badge(
        {"binaryCrcErrors": "5", "binaryMagicResyncs": "5"},
        {"droppedInputChunks": "2"},
        {"seqGap": "1", "deviceSummary": {"latestDrop": "3"}},
        {},
        {},
    )

    values = chip_values([warning])
    assert values["warning"] == "CRC +5 / RESYNC +5 / DROP +6"
    assert "warn" in warning.className or "error" in warning.className
    assert state["crc"] == 5


def test_diagnostic_helpers_tolerate_partial_nan_and_dash_values():
    parser_stats = {"binaryCrcErrors": "-", "binaryMagicResyncs": None, "parseErrors": np.nan}
    runtime_stats = {
        "bytesPerSec": "-",
        "renderTickFps": None,
        "renderedFrameFps": np.nan,
        "frontendCoalescedFrames": 2,
        "frontendDroppedFrames": 0,
        "renderCacheSkipped": 1,
        "heatmapUnit": "mV",
        "heatmapFiniteMin": -95.0,
        "heatmapFiniteMax": -70.0,
        "heatmapZMin": -96.25,
        "heatmapZMax": -68.75,
        "heatmapColorMode": "auto",
    }
    connection_status = {"droppedInputChunks": "-", "droppedInputBytes": None, "lastDataTime": "-"}
    meta = {
        "lastStatusCode": "-",
        "statusFlags": None,
        "droppedFrames": np.nan,
        "outputDecimatedFrames": "-",
    }

    diagnostics = _build_parser_diagnostics(parser_stats, runtime_stats, "-")
    values = chip_values(diagnostics)
    assert values["frontend_coalesced"] == "2"
    assert values["frontend_dropped"] == "0"
    assert values["render_cache_skipped"] == "1"
    assert values["heatmapUnit"] == "mV"
    assert _build_connection_panel(connection_status, "-")
    assert _build_device_panel(meta, parser_stats, {"fields": {}}, [])
