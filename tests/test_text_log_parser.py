from __future__ import annotations

import math

from matrix_log_viewer.config import CELL_NAMES
from matrix_log_viewer.text_log_parser import TextLogParser


def values(prefix: str = "") -> str:
    return ",".join(f"{prefix}{index}" for index in range(64))


def test_matv_header_and_matv_parse():
    parser = TextLogParser()
    header = "MATV_HEADER,seq,timestamp_us,duration_us,unit," + ",".join(CELL_NAMES)
    assert parser.parseLine(header) is None

    result = parser.parseLine("MATV,10,2000000,300,uV," + values())

    assert result.frame.frameType == "MATV"
    assert result.frame.seq == 10
    assert result.frame.timestampUs == 2000000
    assert result.frame.durationUs == 300
    assert result.frame.values["S1D1"] == 0
    assert result.frame.values["S8D8"] == 63


def test_matv_without_header_uses_default_order():
    result = TextLogParser().parseLine("MATV,1,2,3,uV," + values())

    assert result.frame.values["S1D1"] == 0
    assert result.frame.values["S8D8"] == 63


def test_raw_gain_err_streams_parse():
    parser = TextLogParser()

    raw = parser.parseLine("MATV_RAW,1,100," + values())
    gain = parser.parseLine("MATV_GAIN,1,100," + values())
    err = parser.parseLine("MATV_ERR,1,100,not-a-number," + ",".join(str(i) for i in range(1, 64)))

    assert raw.frame.frameType == "MATV_RAW"
    assert raw.frame.unit == "raw"
    assert gain.frame.frameType == "MATV_GAIN"
    assert gain.frame.unit == "gain"
    assert err.frame.frameType == "MATV_ERR"
    assert math.isnan(err.frame.values["S1D1"])
    assert parser.getStats()["warnings"] == 1


def test_status_and_event_rows_do_not_create_matrix_frames():
    parser = TextLogParser()

    rows = [
        "STAT,seq=100,fps=120,drop=0,decimated=1,code=0x6001",
        "EVENT,code=0x3002,name=STREAM_FRAME_DROPPED",
        "APPMODE,active=PIEZO_READ,sw=GND",
        "VOLTSCAN_INIT,mode=PIEZO_VOLTAGE,unit=uV",
        "DBGROUTEPOLICY,mode=PIEZO_READ,sw=GND,result=ok",
        "DBGTMUXPOLICY,stage=voltage_scan_init,result=ok",
    ]
    results = [parser.parseLine(row) for row in rows]

    assert results[0].status.statusType == "STAT"
    assert results[1].event.code == 0x3002
    assert all(result.frame is None for result in results)
    stats = parser.getStats()
    assert stats["parsedStatuses"] == 5
    assert stats["parsedEvents"] == 1


def test_bad_inputs_are_counted_not_raised():
    parser = TextLogParser()

    assert parser.parseLine("") is None
    assert parser.parseLine("MATV,1,2,3,uV,1") is None
    parser.parseLine("MATV,1,2,3,uV," + values() + ",999")

    stats = parser.getStats()
    assert stats["skippedLines"] >= 2
    assert stats["parseErrors"] >= 1
    assert stats["warnings"] >= 1
