from __future__ import annotations

import csv
from typing import Any

from sensorarray_app.domain.battery import parse_battery_fields
from sensorarray_app.domain.models import (
    AdsDiagnosticEvent,
    BatteryTelemetry,
    CommandAccepted,
    CommandApplied,
    CommandTransactionEvent,
    LogRecord,
    TransportEnvelope,
)


# ARL and ADS describe rail/ADC state; treating either as battery telemetry
# used to erase the last real ABAT/AB50 reading. Only actual battery records
# are accepted here. BATD is retained for compatibility with older firmware.
BATTERY_TAGS = {"AB50", "ABAT", "BATD"}

# This is intentionally a superset of the current production summaries. An
# unknown tag is still emitted as a LogRecord with recognised=False so future
# firmware remains observable without crashing the parser.
KNOWN_LOG_TAGS = {
    "S50",
    "F50",
    "A50",
    "O50",
    "SF50",
    "TR50",
    "AB50",
    "OT50",
    "BL50",
    "I2C50",
    "ADS50",
    "ADST50",
    "ADSCHK",
    "ADSCHKSTAT",
    "ABAT",
    "BATD",
    "BAPP",
    "BATPERIOD",
    "RESSETTLE",
    "R50",
    "T50",
    "ROW50",
    "FB50",
    "P50",
    "H50",
    "HC",
    "STK50",
    "TXDROP",
    "BLECORRUPT",
    "CMDERR",
    "RST",
    "ARL",
    "ADS",
    "MODE",
    "ROWS",
    "STATE",
    "RCMD",
    "RAPP",
    "MACK",
    "MAPP",
    "MERR",
    "MFAULT",
    "RACK",
    "RERR",
    "ACK",
    "ERR",
    "CMD_TX",
    "CMD_TX_FAIL",
    "BLE_RX50",
    "BLE_FRAG50",
    "PROTO50",
    "LOGTRUNC",
}


class TextLogProtocol:
    name = "TextLogProtocol"

    def feed_line(
        self,
        line: str,
        envelope: TransportEnvelope,
    ) -> list[
        LogRecord
        | BatteryTelemetry
        | CommandAccepted
        | CommandApplied
        | CommandTransactionEvent
        | AdsDiagnosticEvent
    ]:
        tag = line.split(",", maxsplit=1)[0].strip() or "UNKNOWN"
        fields = parse_key_values(line)
        recognised = tag in KNOWN_LOG_TAGS
        severity = (
            "error"
            if tag in {"ERR", "MERR", "RERR", "MFAULT", "CMDERR"} or tag.startswith("ERROR")
            else "warning"
            if tag.startswith("WARN")
            else "info"
        )
        record = LogRecord(
            timestamp=envelope.receivedWallTime,
            monotonicTime=envelope.receivedMonotonicNs,
            source=envelope.source,
            channel=envelope.channel,
            tag=tag,
            severity=severity,
            rawText=line,
            parsedFields=fields,
            recognised=recognised,
            sessionGeneration=envelope.sessionGeneration,
            deviceTimestamp=fields.get("ts"),
        )
        events: list[
            LogRecord
            | BatteryTelemetry
            | CommandAccepted
            | CommandApplied
            | CommandTransactionEvent
            | AdsDiagnosticEvent
        ] = [record]

        if tag in BATTERY_TAGS:
            events.append(parse_battery_fields(fields, envelope.receivedWallTime))

        if tag == "RCMD":
            events.append(
                CommandAccepted(
                    commandId=_int(fields.get("id"), -1),
                    oldRows=_optional_int(fields.get("old")),
                    requestedRows=_optional_int(fields.get("req")),
                    generation=_optional_int(fields.get("generation") or fields.get("gen")),
                    sessionGeneration=envelope.sessionGeneration,
                    rawText=line,
                )
            )
            events.append(
                _transaction(
                    "rows",
                    "accepted",
                    fields,
                    envelope,
                    line,
                    oldValue=_optional_int(fields.get("old")),
                    requestedValue=_optional_int(fields.get("req")),
                )
            )
        elif tag == "RAPP":
            if _is_rail_response(fields):
                railValue = _rail_value(fields)
                events.append(
                    _transaction(
                        "rail",
                        "applied",
                        fields,
                        envelope,
                        line,
                        requestedValue=railValue,
                        appliedValue=railValue,
                    )
                )
            else:
                events.append(
                    CommandApplied(
                        commandId=_int(fields.get("id"), -1),
                        seq=_optional_int(fields.get("seq")),
                        oldRows=_optional_int(fields.get("old")),
                        newRows=_optional_int(fields.get("new")),
                        generation=_optional_int(fields.get("gen") or fields.get("generation")),
                        sessionGeneration=envelope.sessionGeneration,
                        rawText=line,
                    )
                )
                events.append(
                    _transaction(
                        "rows",
                        "applied",
                        fields,
                        envelope,
                        line,
                        oldValue=_optional_int(fields.get("old")),
                        appliedValue=_optional_int(fields.get("new")),
                    )
                )
        elif tag == "MACK":
            events.append(
                _transaction(
                    "mode",
                    "accepted",
                    fields,
                    envelope,
                    line,
                    oldValue=fields.get("old"),
                    requestedValue=fields.get("new"),
                )
            )
        elif tag == "MAPP":
            events.append(
                _transaction(
                    "mode",
                    "applied",
                    fields,
                    envelope,
                    line,
                    oldValue=fields.get("old"),
                    requestedValue=fields.get("new"),
                    appliedValue=fields.get("new"),
                )
            )
        elif tag == "MERR":
            events.append(
                _transaction(
                    "mode",
                    "failed",
                    fields,
                    envelope,
                    line,
                    oldValue=fields.get("old"),
                    requestedValue=fields.get("new"),
                    error=_transaction_error(fields),
                )
            )
        elif tag == "RACK":
            events.append(
                _transaction(
                    "rail",
                    "accepted",
                    fields,
                    envelope,
                    line,
                    requestedValue=_rail_value(fields),
                )
            )
        elif tag == "RERR":
            events.append(
                _transaction(
                    "rail",
                    "rejected",
                    fields,
                    envelope,
                    line,
                    requestedValue=_rail_value(fields),
                    error=_transaction_error(fields),
                )
            )
        elif tag == "ACK" and fields.get("cmd"):
            commandType = _command_type(fields["cmd"])
            events.append(
                _transaction(
                    commandType,
                    "accepted",
                    fields,
                    envelope,
                    line,
                    requestedValue=_ack_requested_value(commandType, fields),
                )
            )
        elif tag == "ERR" and fields.get("cmd"):
            events.append(
                _transaction(
                    _command_type(fields["cmd"]),
                    "rejected",
                    fields,
                    envelope,
                    line,
                    error=_transaction_error(fields),
                )
            )
        elif tag == "BAPP":
            commandType = _command_type(fields.get("cmd", "BATNOW"))
            events.append(
                _transaction(
                    commandType,
                    "complete" if _error_is_zero(fields.get("err")) else "failed",
                    fields,
                    envelope,
                    line,
                    appliedValue={"durationUs": _optional_int(fields.get("durationUs"))},
                    error=None if _error_is_zero(fields.get("err")) else _transaction_error(fields),
                )
            )
        elif tag == "BATPERIOD":
            status = fields.get("status", "snapshot").lower()
            phase = status if status in {"applied", "rejected"} else "snapshot"
            value = {
                "enabled": _optional_bool(fields.get("enabled")),
                "periodMs": _optional_int(fields.get("periodMs")),
            }
            events.append(
                _transaction(
                    "battery_period",
                    phase,
                    fields,
                    envelope,
                    line,
                    requestedValue=value if phase != "snapshot" else None,
                    appliedValue=value if phase == "applied" else None,
                    error=_transaction_error(fields) if phase == "rejected" else None,
                )
            )
        elif tag == "ADS":
            adsEvent = _ads_identity_event(fields, envelope, line)
            events.append(adsEvent)
            events.append(
                _transaction(
                    "ads_identity",
                    "complete",
                    fields,
                    envelope,
                    line,
                    state=adsEvent.state,
                    appliedValue=adsEvent.chip if adsEvent.identityValid else None,
                )
            )
        elif tag == "ADSCHK":
            adsEvent = _ads_check_event(fields, envelope, line)
            events.append(adsEvent)
            events.append(
                _transaction(
                    "ads_check",
                    "checking" if adsEvent.ok else "failed",
                    fields,
                    envelope,
                    line,
                    state=adsEvent.state,
                    error=None if adsEvent.ok else "ADS register/identity check failed",
                )
            )
        elif tag == "ADSCHKSTAT":
            adsEvent = _ads_check_statistics_event(fields, envelope, line)
            events.append(adsEvent)
            events.append(
                _transaction(
                    "ads_check",
                    "complete" if adsEvent.ok else "failed",
                    fields,
                    envelope,
                    line,
                    state=adsEvent.state,
                    error=None if adsEvent.ok else _ads_failure_detail(adsEvent),
                )
            )
        return events


def parse_key_values(line: str) -> dict[str, str]:
    try:
        parts = next(csv.reader([line]))
    except csv.Error:
        parts = line.split(",")
    out: dict[str, str] = {}
    positional = 0
    for item in parts[1:]:
        text = item.strip()
        if not text:
            continue
        if "=" in text:
            key, value = text.split("=", maxsplit=1)
            out[key.strip()] = value.strip()
        else:
            out[f"arg{positional}"] = text
            positional += 1
    return out


def _transaction(
    commandType: str,
    phase: str,
    fields: dict[str, str],
    envelope: TransportEnvelope,
    line: str,
    *,
    oldValue: Any | None = None,
    requestedValue: Any | None = None,
    appliedValue: Any | None = None,
    state: str | None = None,
    error: str | None = None,
) -> CommandTransactionEvent:
    return CommandTransactionEvent(
        commandType=commandType,
        phase=phase,
        requestId=_optional_int(fields.get("id")),
        state=state or fields.get("state") or fields.get("status"),
        oldValue=oldValue,
        requestedValue=requestedValue,
        appliedValue=appliedValue,
        generation=_optional_int(fields.get("gen") or fields.get("generation")),
        frameSeq=_optional_int(fields.get("seq")),
        error=error,
        rawFields=dict(fields),
        sessionGeneration=envelope.sessionGeneration,
        rawText=line,
    )


def _is_rail_response(fields: dict[str, str]) -> bool:
    return fields.get("source") == "external" or "avdd" in fields or "avss" in fields


def _rail_value(fields: dict[str, str]) -> dict[str, Any]:
    return {
        "avddUv": _optional_int(fields.get("avdd")),
        "avssUv": _optional_int(fields.get("avss")),
        "source": fields.get("source"),
    }


def _command_type(command: str) -> str:
    normalized = command.strip().upper()
    return {
        "MODE": "mode",
        "ROWS": "rows",
        "ROWLIMIT": "rows",
        "SCANROWS": "rows",
        "RAILCFG": "rail",
        "ADSCHK": "ads_check",
        "ADS?": "ads_identity",
        "BATNOW": "battery_now",
        "BATD": "battery_diagnostic",
        "BATPERIOD": "battery_period",
    }.get(normalized, normalized.lower())


def _ack_requested_value(commandType: str, fields: dict[str, str]) -> Any | None:
    if commandType == "ads_check":
        return _optional_int(fields.get("samples"))
    if commandType == "battery_period":
        return {
            "enabled": _optional_bool(fields.get("enabled")),
            "periodMs": _optional_int(fields.get("periodMs")),
        }
    remaining = {key: value for key, value in fields.items() if key not in {"cmd", "id", "status", "state"}}
    return remaining or None


def _ads_identity_event(fields: dict[str, str], envelope: TransportEnvelope, line: str) -> AdsDiagnosticEvent:
    chip = fields.get("chip", "unknown")
    identityValid = _optional_bool(fields.get("valid"))
    confirmed = identityValid is True and chip in {"1262", "1263"}
    return AdsDiagnosticEvent(
        eventType="identity",
        state="confirmed" if confirmed else "unconfirmed",
        chip=chip,
        identityValid=identityValid,
        ok=confirmed,
        rawFields=dict(fields),
        sessionGeneration=envelope.sessionGeneration,
        rawText=line,
    )


def _ads_check_event(fields: dict[str, str], envelope: TransportEnvelope, line: str) -> AdsDiagnosticEvent:
    ok = _optional_bool(fields.get("ok")) is True
    return AdsDiagnosticEvent(
        eventType="check",
        state="checking" if ok else "failed",
        requestId=_optional_int(fields.get("id")),
        chip=fields.get("chip", "unknown"),
        identityValid=fields.get("chip") in {"1262", "1263"},
        ok=ok,
        rawFields=dict(fields),
        sessionGeneration=envelope.sessionGeneration,
        rawText=line,
    )


def _ads_check_statistics_event(
    fields: dict[str, str],
    envelope: TransportEnvelope,
    line: str,
) -> AdsDiagnosticEvent:
    requestedSamples = _optional_int(fields.get("samples"))
    freshSamples = _optional_int(fields.get("fresh"))
    spiErrors = _optional_int(fields.get("spi"))
    drdyTimeouts = _optional_int(fields.get("timeout"))
    staleSamples = _optional_int(fields.get("stale"))
    statusErrors = _optional_int(fields.get("statusErr"))
    resetCount = _optional_int(fields.get("reset"))
    restoreResult = fields.get("restore")
    requiredCounts = (spiErrors, drdyTimeouts, staleSamples, statusErrors, resetCount)
    ok = (
        requestedSamples is not None
        and freshSamples == requestedSamples
        and all(value == 0 for value in requiredCounts)
        and restoreResult == "ok"
    )
    return AdsDiagnosticEvent(
        eventType="statistics",
        state="completed" if ok else "failed",
        requestId=_optional_int(fields.get("id")),
        ok=ok,
        requestedSamples=requestedSamples,
        freshSamples=freshSamples,
        changedSamples=_optional_int(fields.get("changed")),
        periodMinUs=_optional_int(fields.get("periodMinUs")),
        periodAverageUs=_optional_int(fields.get("periodAvgUs")),
        periodMaxUs=_optional_int(fields.get("periodMaxUs")),
        spiErrors=spiErrors,
        drdyTimeouts=drdyTimeouts,
        staleSamples=staleSamples,
        statusErrors=statusErrors,
        resetCount=resetCount,
        restoreResult=restoreResult,
        durationUs=_optional_int(fields.get("durationUs")),
        rawFields=dict(fields),
        sessionGeneration=envelope.sessionGeneration,
        rawText=line,
    )


def _ads_failure_detail(event: AdsDiagnosticEvent) -> str:
    return (
        "ADS check failed: "
        f"fresh={event.freshSamples}/{event.requestedSamples},spi={event.spiErrors},"
        f"timeout={event.drdyTimeouts},stale={event.staleSamples},"
        f"statusErr={event.statusErrors},reset={event.resetCount},restore={event.restoreResult}"
    )


def _transaction_error(fields: dict[str, str]) -> str | None:
    for key in ("reason", "error", "err"):
        value = fields.get(key)
        if value is not None and value != "":
            if key == "err" and _error_is_zero(value):
                continue
            return value
    return None


def _error_is_zero(value: str | None) -> bool:
    if value is None:
        return True
    parsed = _optional_int(value)
    return parsed == 0


def _optional_bool(value: str | None) -> bool | None:
    parsed = _optional_int(value)
    if parsed is None:
        return None
    return bool(parsed)


def _optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip(), 0)
    except ValueError:
        return None


def _int(value: str | None, default: int) -> int:
    parsed = _optional_int(value)
    return default if parsed is None else parsed


__all__ = ["BATTERY_TAGS", "KNOWN_LOG_TAGS", "TextLogProtocol", "parse_key_values"]
