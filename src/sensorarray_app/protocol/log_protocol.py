from __future__ import annotations

import csv
from typing import Any

from sensorarray_app.domain.battery import parse_battery_fields
from sensorarray_app.domain.models import (
    AdsDiagnosticEvent,
    BootInfo,
    BuildInfo,
    CalibrationInfo,
    CommandAccepted,
    CommandApplied,
    CommandTransactionEvent,
    FdcIsolationInfo,
    LogCategory,
    LogRecord,
    PerformanceInfo,
    ProtocolInfo,
    RailTelemetry,
    ReadyInfo,
    RecoveryEvent,
    RestartEvent,
    TransportEnvelope,
    UsbStreamInfo,
    normalize_row_modes,
)


# ARL and ADS describe rail/ADC state; treating either as battery telemetry
# used to erase the last real ABAT/AB50 reading. Only actual battery records
# are accepted here. BATD is retained for compatibility with older firmware.
BATTERY_TAGS = {"AB50", "ABAT", "BATD", "BATERR"}

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
    "BATERR",
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
    "RMACK",
    "RMAPP",
    "RMERR",
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
    "RAIL",
    "BOOT",
    "READY",
    "PROTO",
    "BUILD",
    "PERF",
    "USBSTREAM",
    "FDCISO",
    "FACK",
    "FAPP",
    "FERR",
    "CAL",
    "CALSV",
    "CALLD",
    "APP_FATAL",
    "CTRLDROP",
}


class TextLogProtocol:
    name = "TextLogProtocol"

    def feed_line(
        self,
        line: str,
        envelope: TransportEnvelope,
    ) -> list[Any]:
        tag = line.split(",", maxsplit=1)[0].strip() or "UNKNOWN"
        fields = parse_key_values(line)
        recognised = tag in KNOWN_LOG_TAGS
        severity = (
            "error"
            if tag in {"ERR", "MERR", "RMERR", "RERR", "FERR", "MFAULT", "APP_FATAL", "CMDERR"} or tag.startswith("ERROR")
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
            category=_log_category(tag, envelope.source),
            connectionGeneration=envelope.connectionGeneration,
            bootId=envelope.bootId,
        )
        events: list[Any] = [record]

        if tag in BATTERY_TAGS:
            events.append(parse_battery_fields(fields, envelope.receivedWallTime))
        if tag in {"ARL", "RAIL"}:
            events.append(_rail_telemetry(fields, envelope))

        typed = _typed_status_event(tag, fields, envelope)
        if typed is not None:
            events.append(typed)
        if tag == "MODE" and "fdcSd" in fields:
            events.append(
                FdcIsolationInfo(
                    sd=fields.get("fdcSd", "unknown"),
                    verified=_optional_bool(fields.get("fdcSdVerified")) is True,
                    restartRequired=_optional_bool(fields.get("fdcRestartRequired")) is True,
                    state="snapshot",
                    requestId=_optional_int(fields.get("rid")),
                    rawFields=dict(fields),
                )
            )

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
            deferredCommand = (fields.get("cmd") or "").strip().upper()
            if deferredCommand in {"RESTART", "RECOVER"}:
                commandType = deferredCommand.lower()
                deferredState = (fields.get("state") or "unknown").strip().lower()
                phase = (
                    "restarting"
                    if deferredState == "restarting"
                    else "complete"
                    if deferredState in {"applied", "safe", "complete", "completed"}
                    else "failed"
                )
                events.append(
                    _transaction(
                        commandType,
                        phase,
                        fields,
                        envelope,
                        line,
                        appliedValue={"level": _optional_int(fields.get("level")), "kind": fields.get("kind")},
                        error=_transaction_error(fields) if phase == "failed" else None,
                    )
                )
                if deferredCommand == "RECOVER":
                    events.append(
                        RecoveryEvent(
                            kind=deferredState,
                            expected=deferredState == "restarting",
                            resetReason=fields.get("reason", "unknown"),
                            details=dict(fields),
                        )
                    )
                if deferredCommand == "RESTART" or deferredState == "restarting":
                    events.append(
                        RestartEvent(
                            phase=phase,
                            requestId=_optional_int(fields.get("id")),
                            kind=fields.get("kind") or ("manual" if deferredCommand == "RESTART" else "auto"),
                            rawFields=dict(fields),
                        )
                    )
            elif _is_rail_response(fields):
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
        elif tag == "RMACK":
            requestedProfile = _row_profile(fields.get("new") or fields.get("profile") or fields.get("requested"))
            oldProfile = _row_profile(fields.get("old"))
            # A malformed eight-row profile stays visible in the LogRecord,
            # but must never advance typed command state.
            if requestedProfile is not None:
                events.append(
                    _transaction(
                        "row_modes",
                        "accepted",
                        fields,
                        envelope,
                        line,
                        oldValue=oldProfile,
                        requestedValue=requestedProfile,
                    )
                )
        elif tag == "RMAPP":
            appliedProfile = _row_profile(fields.get("new") or fields.get("profile") or fields.get("applied"))
            oldProfile = _row_profile(fields.get("old"))
            if appliedProfile is not None:
                events.append(
                    _transaction(
                        "row_modes",
                        "applied",
                        fields,
                        envelope,
                        line,
                        oldValue=oldProfile,
                        requestedValue=appliedProfile,
                        appliedValue=appliedProfile,
                    )
                )
        elif tag == "RMERR":
            events.append(
                _transaction(
                    "row_modes",
                    "failed",
                    fields,
                    envelope,
                    line,
                    oldValue=_row_profile(fields.get("old")),
                    requestedValue=_row_profile(fields.get("new") or fields.get("profile") or fields.get("requested")),
                    error=_transaction_error(fields),
                )
            )
        elif tag == "RACK":
            deferredCommand = (fields.get("cmd") or "").strip().upper()
            commandType = deferredCommand.lower() if deferredCommand in {"RECOVER", "RESTART"} else "rail"
            requestedValue = (
                {"level": _optional_int(fields.get("level"))}
                if commandType == "recover"
                else (_rail_value(fields) if commandType == "rail" else None)
            )
            events.append(_transaction(commandType, "accepted", fields, envelope, line, requestedValue=requestedValue))
        elif tag == "RERR":
            deferredCommand = (fields.get("cmd") or "").strip().upper()
            commandType = deferredCommand.lower() if deferredCommand in {"RECOVER", "RESTART"} else "rail"
            events.append(
                _transaction(
                    commandType,
                    "rejected",
                    fields,
                    envelope,
                    line,
                    requestedValue=(
                        {"level": _optional_int(fields.get("level"))}
                        if commandType == "recover"
                        else _rail_value(fields)
                        if commandType == "rail"
                        else None
                    ),
                    error=_transaction_error(fields),
                )
            )
            if commandType == "recover":
                events.append(
                    RecoveryEvent(
                        kind="rejected",
                        expected=False,
                        resetReason=fields.get("reason", "unknown"),
                        details=dict(fields),
                    )
                )
            elif commandType == "restart":
                events.append(
                    RestartEvent(
                        phase="failed",
                        requestId=_optional_int(fields.get("id")),
                        kind=fields.get("kind", "manual"),
                        error=_transaction_error(fields),
                        rawFields=dict(fields),
                    )
                )
        elif tag == "ACK" and fields.get("cmd"):
            commandType = _command_type(fields["cmd"])
            if commandType == "calibration":
                operation = (fields.get("v") or "").strip().upper()
                if operation in {"SAVE", "LOAD"}:
                    commandType = f"calibration_{operation.lower()}"
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
        elif tag == "FACK":
            fdc_phase = "complete" if fields.get("state") == "unchanged" else "accepted"
            events.append(_transaction("fdc_isolation", fdc_phase, fields, envelope, line))
        elif tag == "FAPP":
            events.append(_transaction("fdc_isolation", "applied", fields, envelope, line, appliedValue="ON"))
        elif tag == "FERR":
            events.append(
                _transaction(
                    "fdc_isolation",
                    "failed",
                    fields,
                    envelope,
                    line,
                    error=_transaction_error(fields),
                )
            )
        elif tag in {"CALSV", "CALLD"}:
            commandType = "calibration_save" if tag == "CALSV" else "calibration_load"
            successful = fields.get("state") not in {"rejected", "failed", "error"} and _error_is_zero(fields.get("err"))
            events.append(
                _transaction(
                    commandType,
                    "complete" if successful else "failed",
                    fields,
                    envelope,
                    line,
                    appliedValue=dict(fields) if successful else None,
                    error=None if successful else _transaction_error(fields),
                )
            )
        elif tag == "USBSTREAM":
            state = fields.get("state", "snapshot").lower()
            events.append(
                _transaction(
                    "usb_stream",
                    "applied" if state == "applied" else "snapshot",
                    fields,
                    envelope,
                    line,
                    appliedValue=(fields.get("v") or "UNKNOWN").upper(),
                )
            )
        elif tag == "MODE":
            events.append(_transaction("device_state", "snapshot", fields, envelope, line, appliedValue=dict(fields)))
        elif tag == "ROWS":
            events.append(
                _transaction(
                    "rows",
                    "snapshot",
                    fields,
                    envelope,
                    line,
                    appliedValue=_optional_int(fields.get("active")),
                )
            )
        elif tag == "ROWMODES":
            events.append(
                _transaction(
                    "row_modes",
                    "snapshot",
                    fields,
                    envelope,
                    line,
                    appliedValue=_row_profile(fields.get("active")),
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
        requestId=_optional_int(
            fields.get("id")
            or fields.get("rid")
            or fields.get("appliedId")
            or fields.get("requestId")
        ),
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


def _rail_telemetry(fields: dict[str, str], envelope: TransportEnvelope) -> RailTelemetry:
    state = (fields.get("rs") or fields.get("state") or "unknown").strip().lower()
    valid = _optional_bool(fields.get("rv") or fields.get("valid"))
    explicitFresh = _optional_bool(fields.get("fresh"))
    if explicitFresh is not None:
        fresh = explicitFresh
    elif state in {"stale", "hold", "bad", "invalid", "fault", "error", "missing"}:
        fresh = False
    elif state in {"ok", "fresh", "valid"}:
        fresh = valid
    else:
        fresh = None
    source = (fields.get("src") or fields.get("source") or "internal_monitor").strip().lower()
    if source in {"monitor", "internal", "ads_monitor", "internal-monitor"}:
        source = "internal_monitor"
    return RailTelemetry(
        railSpanUv=_optional_int(fields.get("railSpanUv") or fields.get("spanUv") or fields.get("span") or fields.get("rail")),
        valid=valid,
        fresh=fresh,
        age=_optional_int(fields.get("age")),
        ageMs=_optional_int(fields.get("ageMs")),
        source=source,
        reason=fields.get("reason") or state,
        timestamp=float(envelope.receivedWallTime),
        rawFields=dict(fields),
        avddUv=_optional_int(fields.get("avdd") or fields.get("avddUv")),
        avssUv=_optional_int(fields.get("avss") or fields.get("avssUv")),
        bootId=envelope.bootId,
    )


def _row_profile(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    try:
        return normalize_row_modes(value)
    except ValueError:
        return None


def _command_type(command: str) -> str:
    normalized = command.strip().upper()
    return {
        "MODE": "mode",
        "ROWMODES": "row_modes",
        "ROWS": "rows",
        "ROWLIMIT": "rows",
        "SCANROWS": "rows",
        "RAILCFG": "rail",
        "ADSCHK": "ads_check",
        "ADS?": "ads_identity",
        "BATNOW": "battery_now",
        "BATD": "battery_diagnostic",
        "BATPERIOD": "battery_period",
        "FDCISO": "fdc_isolation",
        "USBSTREAM": "usb_stream",
        "RECOVER": "recover",
        "RESTART": "restart",
        "CAL": "calibration",
    }.get(normalized, normalized.lower())


def _typed_status_event(tag: str, fields: dict[str, str], envelope: TransportEnvelope) -> Any | None:
    if tag == "BOOT":
        boot_id = _optional_int(fields.get("bootId"))
        if boot_id is None:
            return None
        return BootInfo(
            boot=_optional_int(fields.get("boot")),
            bootId=boot_id,
            reset=fields.get("reset", "unknown"),
            stage=fields.get("stage", "unknown"),
            err=fields.get("err", "0x0"),
            seq=_optional_int(fields.get("seq")),
            heap=_optional_int(fields.get("heap")),
            heapMin=_optional_int(fields.get("heapMin")),
            prevStage=fields.get("prevStage"),
            prevErr=fields.get("prevErr"),
            prevHeap=_optional_int(fields.get("prevHeap")),
            guard=fields.get("guard"),
            autoRestarts=_optional_int(fields.get("autoRestarts")),
            windowAgeS=_optional_int(fields.get("windowAgeS")),
            ready=_optional_bool(fields.get("ready")),
            sessionGeneration=envelope.sessionGeneration,
            connectionGeneration=envelope.connectionGeneration,
            rawFields=dict(fields),
        )
    if tag == "READY":
        ready = _optional_bool(fields.get("ready"))
        if ready is None:
            return None
        return ReadyInfo(
            ready=ready,
            stage=fields.get("stage", "unknown"),
            err=fields.get("err", "0x0"),
            bootId=_optional_int(fields.get("bootId")),
            boot=_optional_int(fields.get("boot")),
            sessionGeneration=envelope.sessionGeneration,
            connectionGeneration=envelope.connectionGeneration,
            rawFields=dict(fields),
        )
    if tag == "PROTO":
        version = fields.get("version", "")
        wires = fields.get("wires", "").lower()
        ctrl_max = _int(fields.get("ctrlMax"), 0)
        data_max = _int(fields.get("dataMax"), 0)
        channels = tuple(item for item in fields.get("channels", "").split("/") if item)
        incompatibility = ""
        if version != "1":
            incompatibility = f"unsupported protocol version {version or 'missing'}"
        elif wires != "ascii":
            incompatibility = f"unsupported wire encoding {wires or 'missing'}"
        elif ctrl_max < 512 or data_max < 1536:
            incompatibility = "firmware message limits are below the 8045 contract"
        elif not {"CTRL", "DATA", "LOG", "LIFECYCLE"}.issubset(set(channels)):
            incompatibility = "firmware channels do not include the 8045 lifecycle contract"
        return ProtocolInfo(version, wires, ctrl_max, data_max, channels, not incompatibility, incompatibility, dict(fields))
    if tag == "BUILD":
        return BuildInfo(
            idf=fields.get("idf", ""),
            target=fields.get("target", ""),
            project=fields.get("project", ""),
            proto=fields.get("proto", ""),
            rawFields=dict(fields),
        )
    if tag in {
        "PERF", "SF50", "TR50", "OT50", "BL50", "I2C50", "ADS50",
        "ADST50", "STK50", "TXDROP", "CTRLDROP", "T50", "R50", "P50", "H50",
    }:
        output_fps = _split_float_tuple(fields.get("ofps"))
        drops = _split_int_tuple(fields.get("drop") or fields.get("dropOut"))
        queues = _split_int_tuple(fields.get("q"))
        sequence_start, sequence_end = _split_sequence_range(fields.get("seq"))
        bad = _split_int_tuple(fields.get("bad"))
        return PerformanceInfo(
            kind=tag,
            physicalCaptureFps=_optional_float(fields.get("cfps")),
            emittedFps=_optional_float(fields.get("efps")),
            outputFps=output_fps[0] if len(output_fps) == 1 else None,
            firmwareDrops=sum(drops) if drops else None,
            outputFpsBySink=output_fps,
            firmwareDropsBySink=drops,
            queueDepthBySink=queues,
            sequenceStart=sequence_start,
            sequenceEnd=sequence_end,
            frameCount=_optional_int(fields.get("n")),
            staleFrames=bad[0] if len(bad) >= 1 else None,
            mixedFrames=bad[1] if len(bad) >= 2 else None,
            invalidFrames=bad[2] if len(bad) >= 3 else None,
            sourceTransport=str(envelope.source),
            sessionGeneration=envelope.sessionGeneration,
            connectionGeneration=envelope.connectionGeneration,
            bootId=envelope.bootId,
            metrics={key: _metric_value(value) for key, value in fields.items()},
            rawFields=dict(fields),
        )
    if tag == "USBSTREAM":
        mode = (fields.get("v") or "UNKNOWN").upper()
        return UsbStreamInfo(
            mode=mode,
            dataEvery=max(0, _int(fields.get("dataEvery"), 0)),
            diagEvery=max(0, _int(fields.get("diagEvery"), 0)),
            state=fields.get("state", "snapshot"),
            rawFields=dict(fields),
        )
    if tag in {"FDCISO", "FACK", "FAPP", "FERR"}:
        state = fields.get("state") or ("snapshot" if tag == "FDCISO" else tag.lower())
        return FdcIsolationInfo(
            sd=fields.get("sd", "unknown"),
            verified=_optional_bool(fields.get("verified")) is True,
            restartRequired=_optional_bool(fields.get("restartRequired")) is True,
            state=state,
            requestId=_optional_int(fields.get("id")),
            error=_transaction_error(fields) if tag == "FERR" else None,
            rawFields=dict(fields),
        )
    if tag in {"CAL", "CALSV", "CALLD"}:
        return CalibrationInfo(
            source=fields.get("source", "0"),
            schema=_optional_int(fields.get("schema")),
            valid=_optional_bool(fields.get("valid")) is True,
            boardId=fields.get("boardId"),
            hardwareRev=_optional_int(fields.get("hardwareRev")),
            payloadLength=_optional_int(fields.get("payloadLength")),
            state=fields.get("state", "snapshot"),
            rawFields=dict(fields),
        )
    return None


def _log_category(tag: str, source: str) -> str:
    if source == "host":
        return LogCategory.HOST.value
    if tag in {"C", "V", "R", "M", "MR", "K"} or (
        len(tag) > 1 and tag[0] in {"D", "P"} and tag[1:].isdigit()
    ):
        return LogCategory.MEASUREMENT.value
    if tag in {"BOOT", "READY", "RST", "RMAPP", "RMERR", "MAPP", "MERR", "RAPP", "FAPP", "FERR"}:
        return LogCategory.LIFECYCLE.value
    if tag in {"MFAULT", "APP_FATAL", "TXDROP", "CTRLDROP", "BLECORRUPT", "CMDERR"} or "FAULT" in tag:
        return LogCategory.FAULT.value
    if tag in {"ACK", "ERR", "RCMD", "MACK", "RMACK", "RACK", "FACK", "MODE", "ROWS", "ROWMODES"}:
        return LogCategory.CONTROL.value
    return LogCategory.DIAGNOSTIC.value


def _ack_requested_value(commandType: str, fields: dict[str, str]) -> Any | None:
    if commandType == "ads_check":
        return _optional_int(fields.get("samples"))
    if commandType == "battery_period":
        return {
            "enabled": _optional_bool(fields.get("enabled")),
            "periodMs": _optional_int(fields.get("periodMs")),
        }
    if commandType in {"calibration_save", "calibration_load"}:
        return (fields.get("v") or "").upper()
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


def _optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _split_int_tuple(value: str | None) -> tuple[int, ...]:
    if value is None or value == "":
        return ()
    parsed = tuple(_optional_int(item) for item in str(value).split("/"))
    return tuple(int(item) for item in parsed if item is not None)


def _split_sequence_range(value: str | None) -> tuple[int | None, int | None]:
    if value is None:
        return None, None
    text = str(value).strip()
    if "-" not in text:
        parsed = _optional_int(text)
        return parsed, parsed
    start_text, end_text = text.split("-", maxsplit=1)
    return _optional_int(start_text), _optional_int(end_text)


def _split_float_tuple(value: str | None) -> tuple[float, ...]:
    if value is None or value == "":
        return ()
    parsed = tuple(_optional_float(item) for item in str(value).split("/"))
    return tuple(float(item) for item in parsed if item is not None)


def _metric_value(value: str) -> Any:
    if "/" in value:
        parts = value.split("/")
        converted = tuple(_metric_value(item) for item in parts)
        return converted
    integer = _optional_int(value)
    if integer is not None:
        return integer
    floating = _optional_float(value)
    if floating is not None:
        return floating
    return value


def _int(value: str | None, default: int) -> int:
    parsed = _optional_int(value)
    return default if parsed is None else parsed


__all__ = ["BATTERY_TAGS", "KNOWN_LOG_TAGS", "TextLogProtocol", "parse_key_values"]
