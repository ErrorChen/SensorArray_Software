from __future__ import annotations

import time
from typing import Any

from sensorarray_app.domain.models import BatteryTelemetry

BATTERY_REASONS = {
    "adc_timeout",
    "adc_stale",
    "adc_status_error",
    "rail_invalid",
    "reference_invalid",
    "absent_or_open",
    "range_error",
    "unknown",
}


def parse_battery_fields(fields: dict[str, str], received_time: float | None = None) -> BatteryTelemetry:
    reason = fields.get("br") or fields.get("reason") or "unknown"
    if reason not in BATTERY_REASONS:
        reason = "unknown"
    battery_mv = _int_or_none(fields.get("bt"))
    if battery_mv == -1:
        battery_mv = None
    state = fields.get("bs") or ("present" if battery_mv is not None else "unknown")
    z_mean, z_std = _parse_pair(fields.get("z"))
    return BatteryTelemetry(
        batteryMv=battery_mv,
        batteryState=state,
        reason=reason,
        fresh=_bool_or_none(fields.get("fresh")),
        ageFrames=_int_or_none(fields.get("age")),
        railUv=_int_or_none(fields.get("rail")),
        railValid=_bool_or_none(fields.get("rv")),
        railState=fields.get("rs"),
        railErrorUv=_int_or_none(fields.get("re")),
        ain8DiffUv=_int_or_none(fields.get("a8d")),
        aincomGndUv=_int_or_none(fields.get("ac")),
        ain8GndUv=_int_or_none(fields.get("a8g")),
        zeroMeanUv=z_mean,
        zeroStdUv=z_std,
        statusByte=_int_or_none(fields.get("status")),
        drdyGenerationDelta=_int_or_none(fields.get("dg")),
        adsChipId=_int_or_none(fields.get("chip")),
        receivedTime=time.time() if received_time is None else float(received_time),
        rawFields=dict(fields),
    )


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "na":
        return None
    try:
        return int(text, 0)
    except ValueError:
        return None


def _bool_or_none(value: Any) -> bool | None:
    parsed = _int_or_none(value)
    if parsed is None:
        return None
    return bool(parsed)


def _parse_pair(value: Any) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    parts = str(value).split("/", maxsplit=1)
    if len(parts) != 2:
        return None, None
    return _int_or_none(parts[0]), _int_or_none(parts[1])
