from __future__ import annotations

import time
from typing import Any

from sensorarray_app.domain.models import BatteryTelemetry


def parse_battery_fields(fields: dict[str, str], received_time: float | None = None) -> BatteryTelemetry:
    """Parse both the detailed ``ABAT`` and compact ``AB50`` schemas.

    Firmware is authoritative for reason strings, so an unfamiliar value is
    retained verbatim. This matters for forward compatibility: replacing a
    new reason with ``unknown`` would discard the actual diagnostic evidence.
    """

    reason = fields.get("br") or fields.get("reason") or "unknown"
    battery_mv = _int_or_none(fields.get("bt"))
    if battery_mv == -1:
        battery_mv = None
    valid = _bool_or_none(fields.get("valid"))
    state = fields.get("bs") or ("present" if valid is True else "invalid" if valid is False else "unknown")
    if valid is None and "bs" in fields:
        valid = _battery_state_fresh(state)
    # In compact AB50, ``bs`` describes battery freshness while ``fresh``
    # later in the record belongs to the ADS cache.  Detailed ABAT has its own
    # numeric battery ``fresh`` field.
    battery_fresh = _battery_state_fresh(state) if "bs" in fields else _bool_or_none(fields.get("fresh"))
    z_mean, z_std = _parse_pair(fields.get("z"))
    retry_count, retry_limit = _parse_pair(fields.get("retry"))
    sample_average_us, sample_maximum_us = _parse_pair(fields.get("sampleUs"))
    if sample_average_us is None:
        sample_average_us = _int_or_none(fields.get("sampleUs"))
    ratio_numerator, ratio_denominator = _parse_pair(fields.get("ratio"))
    rail_state = fields.get("railState") or fields.get("rs")
    rail_valid = _bool_or_none(fields.get("railValid") or fields.get("rv"))
    if rail_valid is None:
        rail_valid = _rail_state_valid(rail_state)
    return BatteryTelemetry(
        batteryMv=battery_mv,
        batteryState=state,
        reason=reason,
        fresh=battery_fresh,
        ageFrames=_int_or_none(fields.get("age")),
        railUv=_int_or_none(fields.get("rail")),
        railValid=rail_valid,
        railState=rail_state,
        railErrorUv=_int_or_none(fields.get("railErrorUv") or fields.get("re")),
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
        valid=valid,
        ageMs=_int_or_none(fields.get("ageMs")),
        periodMs=_int_or_none(fields.get("periodMs")),
        due=_bool_or_none(fields.get("due")),
        runCount=_int_or_none(fields.get("run")),
        validRunCount=_int_or_none(fields.get("validRun")),
        invalidRunCount=_int_or_none(fields.get("invalidRun")),
        skipCount=_int_or_none(fields.get("skip")),
        deferCount=_int_or_none(fields.get("defer")),
        boundaryCount=_int_or_none(fields.get("boundary")),
        restoreFailureCount=_int_or_none(fields.get("restoreFail")),
        retryCount=retry_count,
        retryLimit=retry_limit,
        retryLastCount=retry_count,
        retryTotalCount=retry_limit,
        unstableCount=_int_or_none(fields.get("unstable")),
        timeoutCount=_int_or_none(fields.get("timeout")),
        spreadRaw=_int_or_none(fields.get("spreadRaw")),
        spreadMaximumRaw=_int_or_none(fields.get("spreadMaxRaw")),
        rawAdc=_int_or_none(fields.get("raw")),
        batteryDividerNumerator=ratio_numerator,
        batteryDividerDenominator=ratio_denominator,
        vbiasEnabled=_bool_or_none(fields.get("vbias")),
        sampleCount=_int_or_none(fields.get("samples")),
        sampleAverageUs=sample_average_us,
        sampleMaximumUs=sample_maximum_us,
        restoreResult=fields.get("restore"),
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


def _battery_state_fresh(state: Any) -> bool | None:
    normalized = str(state or "").strip().lower()
    if normalized == "present":
        return True
    if normalized in {"stale", "invalid"}:
        return False
    return None


def _rail_state_valid(state: Any) -> bool | None:
    normalized = str(state or "").strip().lower()
    if normalized in {"ok", "valid", "fresh"}:
        return True
    # Production railStatus values are ok/hold/bad. HOLD can reuse a bounded
    # last-good rail for the battery calculation, but the current rail sample
    # is still invalid (firmware railValid is false).
    if normalized in {"hold", "bad", "invalid", "fault", "error", "stale", "missing"}:
        return False
    return None
