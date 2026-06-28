from __future__ import annotations

import csv

from sensorarray_app.domain.battery import parse_battery_fields
from sensorarray_app.domain.models import BatteryTelemetry, CommandAccepted, CommandApplied, LogRecord, TransportEnvelope

BATTERY_TAGS = {"AB50", "ABAT", "BATD", "ARL", "ADS"}
KNOWN_LOG_TAGS = {
    "RCMD",
    "RAPP",
    "ACK",
    "ERR",
    "SF50",
    "TR50",
    "AB50",
    "OT50",
    "BL50",
    "I2C50",
    "T50",
    "ROW50",
    "FB50",
    "P50",
    "H50",
    "HC",
    "BATD",
    "ARL",
    "ADS",
    "RST",
    "ABAT",
    "CMD_TX",
    "CMD_TX_FAIL",
    "BLE_RX50",
    "BLE_FRAG50",
    "PROTO50",
}


class TextLogProtocol:
    name = "TextLogProtocol"

    def feed_line(self, line: str, envelope: TransportEnvelope) -> list[LogRecord | BatteryTelemetry | CommandAccepted | CommandApplied]:
        tag = line.split(",", maxsplit=1)[0].strip() or "UNKNOWN"
        fields = parse_key_values(line)
        recognised = tag in KNOWN_LOG_TAGS
        severity = "error" if tag in {"ERR"} or tag.startswith("ERROR") else "warning" if tag.startswith("WARN") else "info"
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
        events: list[LogRecord | BatteryTelemetry | CommandAccepted | CommandApplied] = [record]
        if tag in BATTERY_TAGS:
            events.append(parse_battery_fields(fields, envelope.receivedWallTime))
        elif tag == "RCMD":
            events.append(
                CommandAccepted(
                    commandId=_int(fields.get("id"), -1),
                    oldRows=_optional_int(fields.get("old")),
                    requestedRows=_optional_int(fields.get("req")),
                    generation=_optional_int(fields.get("generation")),
                    sessionGeneration=envelope.sessionGeneration,
                    rawText=line,
                )
            )
        elif tag == "RAPP":
            events.append(
                CommandApplied(
                    commandId=_int(fields.get("id"), -1),
                    seq=_optional_int(fields.get("seq")),
                    oldRows=_optional_int(fields.get("old")),
                    newRows=_optional_int(fields.get("new")),
                    generation=_optional_int(fields.get("gen")),
                    sessionGeneration=envelope.sessionGeneration,
                    rawText=line,
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
