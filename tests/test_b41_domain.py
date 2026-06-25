from __future__ import annotations

import math
import time

import numpy as np

from sensorarray_app.domain.baseline import BaselineSession, delta_percent
from sensorarray_app.domain.battery import parse_battery_fields
from sensorarray_app.domain.capacitance import fixed_to_pf
from sensorarray_app.domain.engineering_units import EngineeringUnitFormatter
from sensorarray_app.domain.models import CapacitanceFrame
from sensorarray_app.domain.selection import select_group


def make_frame(seq: int, ns: int, values: list[float]) -> CapacitanceFrame:
    raw_fixed = np.asarray([int((value + 33.0) * 1_000_000) for value in values], dtype=np.int64)
    raw_pf = raw_fixed.astype(float) / 1_000_000.0
    corrected = np.asarray(values, dtype=np.float64)
    valid = np.ones(len(values), dtype=bool)
    return CapacitanceFrame(seq, seq, 1, 8, 2, 7, 1, 1, 1, 0, 0, 0, raw_fixed, raw_pf, corrected, valid, "serial", 1, time.time(), ns, "COM12")


def test_capacitance_conversion_and_offset():
    raw, raw_pf, corrected = fixed_to_pf([33_000_000, 34_000_000, -1_000_000, 32_000_000])
    assert raw.tolist() == [33_000_000, 34_000_000, -1_000_000, 32_000_000]
    assert raw_pf[0] == 33.0
    assert corrected[0] == 0.0
    assert corrected[1] == 1.0
    assert math.isnan(raw_pf[2])
    assert math.isnan(corrected[2])
    assert corrected[3] == -1.0


def test_engineering_units_thresholds_and_shared_unit():
    formatter = EngineeringUnitFormatter(hysteresis=0.0)
    assert formatter.choose_unit([999.0]).name == "pF"
    assert formatter.choose_unit([1000.0]).name == "nF"
    assert formatter.choose_unit([999_999.0]).name == "nF"
    assert formatter.choose_unit([1_000_000.0]).name == "uF"
    assert formatter.scale(np.array([1_000_000.0]), formatter.choose_unit([1_000_000.0]))[0] == 1.0


def test_selection_primary_secondary_and_inactive():
    primary = select_group(3, 2, 8)
    secondary = select_group(5, 7, 8)
    assert primary.cells == ("S3D1", "S3D2", "S3D3", "S3D4")
    assert primary.title == "S3 路 Primary FDC 路 D1-D4"
    assert secondary.cells == ("S5D5", "S5D6", "S5D7", "S5D8")
    assert secondary.title == "S5 路 Secondary FDC 路 D5-D8"
    try:
        select_group(5, 7, 4)
    except ValueError as exc:
        assert "inactive" in str(exc)
    else:
        raise AssertionError("inactive row selection was accepted")


def test_baseline_two_second_window_median_and_percent():
    session = BaselineSession(1, "serial", "COM12", 1, 2, 7, "capacitance", 33.0, 1_000_000_000)
    session.add_frame(make_frame(1, 900_000_000, [1.0] * 8))
    session.add_frame(make_frame(2, 1_100_000_000, [10.0] * 8))
    session.add_frame(make_frame(3, 1_200_000_000, [12.0] * 8))
    session.add_frame(make_frame(4, 1_300_000_000, [1000.0] * 8))
    session.add_frame(make_frame(5, 3_100_000_000, [14.0] * 8))
    result = session.complete()
    assert result.frameCount == 3
    assert result.validMask[0]
    assert result.valuesPf[0] == 12.0
    percent = delta_percent(np.array([18.0] + [12.0] * 63), result)
    assert percent[0] == 50.0


def test_battery_bt_minus_one_is_invalid():
    telemetry = parse_battery_fields({"bt": "-1", "br": "range_error", "bs": "stale"})
    assert telemetry.batteryMv is None
    assert telemetry.reason == "range_error"
