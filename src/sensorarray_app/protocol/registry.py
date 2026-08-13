from __future__ import annotations

import time
from dataclasses import dataclass

from sensorarray_app.domain.models import (
    CapacitanceFrame,
    DomainEvent,
    LogRecord,
    MeasurementFrame,
    MixedMeasurementFrame,
    ParserErrorEvent,
    TransportEnvelope,
)
from sensorarray_app.protocol.ble_fragments import BleFragmentReassembler, normalize_ble_channel
from sensorarray_app.protocol.cap_ascii import CapAsciiParser
from sensorarray_app.protocol.legacy_binary_voltage import LegacyFastBinaryVoltageProtocol, MAGIC_BYTES
from sensorarray_app.protocol.legacy_matv import LegacyMatvProtocol
from sensorarray_app.protocol.log_protocol import TextLogProtocol
from sensorarray_app.protocol.measurement_ascii import MeasurementAsciiParser
from sensorarray_app.protocol.mixed_ascii import MixedMeasurementAsciiParser


@dataclass
class _StreamState:
    cap: CapAsciiParser
    measurement: MeasurementAsciiParser
    mixed: MixedMeasurementAsciiParser
    activeFrameType: str | None = None


class ProtocolRegistry:
    def __init__(self, circuit_offset_pf: float = 33.0):
        self.cap = CapAsciiParser(circuit_offset_pf=circuit_offset_pf)
        self.measurement = MeasurementAsciiParser()
        self.mixed = MixedMeasurementAsciiParser(circuitOffsetPf=circuit_offset_pf)
        self.legacy_binary = LegacyFastBinaryVoltageProtocol()
        self.legacy_matv = LegacyMatvProtocol()
        self.log_protocol = TextLogProtocol()
        self.ble_fragments = BleFragmentReassembler()
        self._text_buffers: dict[tuple[str, str, int], bytearray] = {}
        self._feed_count = 0
        self._cap_count = 0
        self._voltage_count = 0
        self._resistance_count = 0
        self._mixed_count = 0
        self._log_count = 0
        self._reject_count = 0
        self._streamStates: dict[tuple[str, str, int], _StreamState] = {}

    def reset_session(self) -> None:
        """Clear all partial protocol state before accepting a new session."""

        self.cap.reset()
        self.measurement.reset()
        self.mixed.reset()
        self.legacy_binary.reset()
        self.ble_fragments.reset()
        self._text_buffers.clear()
        self._streamStates.clear()

    def feed(self, envelope: TransportEnvelope) -> list[DomainEvent]:
        normalized = _normalized_envelope(envelope)
        if _contains_g_fragment(normalized.rawPayload):
            events: list[DomainEvent] = []
            for channel, payload in self.ble_fragments.feed(
                normalized.channel,
                normalized.rawPayload,
                normalized.receivedMonotonicNs,
            ):
                events.extend(self.feed(_replace_envelope(normalized, channel, payload)))
            return events
        return self._feed_routed(normalized)

    def _feed_routed(self, envelope: TransportEnvelope) -> list[DomainEvent]:
        self._feed_count += 1
        if envelope.rawPayload.startswith(MAGIC_BYTES) or self.legacy_binary._buffer:
            return self.legacy_binary.feed(envelope)
        events = self._feed_text(envelope)
        self._count_events(events)
        events.extend(self._diagnostic_event(envelope))
        return events

    def _feed_text(self, envelope: TransportEnvelope) -> list[DomainEvent]:
        key = (envelope.source, envelope.channel, envelope.sessionGeneration)
        state = self._stream_state(key)
        buffer = self._text_buffers.setdefault(key, bytearray())
        buffer.extend(envelope.rawPayload)
        events: list[DomainEvent] = []
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            raw_line = bytes(buffer[: newline + 1])
            del buffer[: newline + 1]
            protocolEvents = self._feed_current_line(raw_line, envelope, state)
            for event in protocolEvents:
                if isinstance(event, LogRecord):
                    matv = self.legacy_matv.parse_line(event.rawText, envelope)
                    if matv is not None:
                        events.append(matv)
                    else:
                        events.extend(self.log_protocol.feed_line(event.rawText, envelope))
                else:
                    events.append(event)
        events.extend(state.cap.check_timeout(envelope.receivedMonotonicNs, envelope))
        events.extend(state.measurement.check_timeout(envelope.receivedMonotonicNs, envelope))
        events.extend(state.mixed.check_timeout(envelope.receivedMonotonicNs, envelope))
        if state.activeFrameType == "C" and not state.cap.hasPendingFrame:
            state.activeFrameType = None
        elif state.activeFrameType in {"V", "R"} and not state.measurement.hasPendingFrame:
            state.activeFrameType = None
        elif state.activeFrameType == "M" and not state.mixed.hasPendingFrame:
            state.activeFrameType = None
        return events

    def _stream_state(self, key: tuple[str, str, int]) -> _StreamState:
        state = self._streamStates.get(key)
        if state is None:
            if not self._streamStates:
                state = _StreamState(self.cap, self.measurement, self.mixed)
            else:
                state = _StreamState(
                    CapAsciiParser(circuit_offset_pf=self.cap.circuit_offset_pf),
                    MeasurementAsciiParser(),
                    MixedMeasurementAsciiParser(circuitOffsetPf=self.cap.circuit_offset_pf),
                )
            self._streamStates[key] = state
        # Backend display settings historically update registry.cap directly.
        # Mirror that value into every channel-specific CAP adapter.
        state.cap.circuit_offset_pf = self.cap.circuit_offset_pf
        state.mixed.circuitOffsetPf = self.cap.circuit_offset_pf
        return state

    def _feed_current_line(
        self,
        rawLine: bytes,
        envelope: TransportEnvelope,
        state: _StreamState,
    ) -> list[CapacitanceFrame | MeasurementFrame | MixedMeasurementFrame | LogRecord | ParserErrorEvent]:
        try:
            line = rawLine.rstrip(b"\r\n").decode("ascii", errors="strict")
        except UnicodeDecodeError:
            if state.activeFrameType == "C":
                return state.cap.feed_line(rawLine, envelope)
            if state.activeFrameType in {"V", "R"}:
                return state.measurement.feed_line(rawLine, envelope)
            if state.activeFrameType == "M":
                return state.mixed.feed_line(rawLine, envelope)
            return [
                ParserErrorEvent(
                    source=envelope.source,
                    channel=envelope.channel,
                    reason="strict_ascii",
                    detail="non-ASCII text protocol line",
                    sessionGeneration=envelope.sessionGeneration,
                    rawText=rawLine.hex(" "),
                )
            ]

        tag = line.split(",", maxsplit=1)[0].strip()
        events: list[CapacitanceFrame | MeasurementFrame | MixedMeasurementFrame | LogRecord | ParserErrorEvent] = []
        if tag == "C":
            if state.activeFrameType in {"V", "R"}:
                events.extend(state.measurement.abort_pending("interrupted_frame", "C header interrupted pending V/R frame", envelope))
            if state.activeFrameType == "M":
                events.extend(state.mixed.abort_pending("interrupted_frame", "C header interrupted pending mixed frame", envelope))
            state.activeFrameType = "C"
            events.extend(state.cap.feed_line(rawLine, envelope))
            return events
        if tag in {"V", "R"}:
            if state.activeFrameType == "C":
                events.extend(state.cap.abort_pending("interrupted_frame", f"{tag} header interrupted pending C frame", envelope))
            if state.activeFrameType == "M":
                events.extend(state.mixed.abort_pending("interrupted_frame", f"{tag} header interrupted pending mixed frame", envelope))
            state.activeFrameType = tag
            events.extend(state.measurement.feed_line(rawLine, envelope))
            return events
        if tag == "M":
            # Firmware also owns a diagnostic `M,stage=...,reason=...` log tag
            # (BLE memory/allocation telemetry).  Only the frame header has
            # the complete mixed identity/geometry signature.  Treating every
            # `M` log as a frame would create a false parser reject and could
            # interrupt a legitimate stream on the same transport.
            if not _looks_like_mixed_header(line):
                return state.cap.feed_line(rawLine, envelope)
            if state.activeFrameType == "C":
                events.extend(state.cap.abort_pending("interrupted_frame", "M header interrupted pending C frame", envelope))
            if state.activeFrameType in {"V", "R"}:
                events.extend(state.measurement.abort_pending("interrupted_frame", "M header interrupted pending V/R frame", envelope))
            state.activeFrameType = "M"
            events.extend(state.mixed.feed_line(rawLine, envelope))
            return events
        if tag == "MR":
            return state.mixed.feed_line(rawLine, envelope)
        if tag.startswith("D") and tag[1:].isdigit():
            if state.activeFrameType in {"V", "R"}:
                return state.measurement.feed_line(rawLine, envelope)
            return state.cap.feed_line(rawLine, envelope)
        if tag.startswith("P") and tag[1:].isdigit():
            if state.activeFrameType in {"V", "R"}:
                return state.measurement.feed_line(rawLine, envelope)
            # P50 is a normal summary outside a V/R frame. Other orphan P rows
            # are rejected by the V/R parser instead of being mistaken for CAP.
            if tag == "P50" or state.activeFrameType == "C":
                return state.cap.feed_line(rawLine, envelope)
            return state.measurement.feed_line(rawLine, envelope)
        if tag == "K":
            activeFrameType = state.activeFrameType
            state.activeFrameType = None
            if activeFrameType in {"V", "R"}:
                return state.measurement.feed_line(rawLine, envelope)
            if activeFrameType == "M":
                return state.mixed.feed_line(rawLine, envelope)
            return state.cap.feed_line(rawLine, envelope)
        # Runtime log records can be interleaved between frame lines on the
        # shared Serial stream.  Only C/V/R/D/P/K are measurement grammar;
        # other tags remain observable logs and must not destroy a pending
        # CRC frame.
        return state.cap.feed_line(rawLine, envelope)

    def _count_events(self, events: list[DomainEvent]) -> None:
        for event in events:
            if isinstance(event, CapacitanceFrame):
                self._cap_count += 1
            elif isinstance(event, MeasurementFrame):
                if event.mode == "VOLT":
                    self._voltage_count += 1
                elif event.mode == "RES":
                    self._resistance_count += 1
            elif isinstance(event, MixedMeasurementFrame):
                self._mixed_count += 1
            elif isinstance(event, LogRecord):
                self._log_count += 1
            elif isinstance(event, ParserErrorEvent):
                self._reject_count += 1

    def _diagnostic_event(self, envelope: TransportEnvelope) -> list[LogRecord]:
        if self._feed_count % 50 != 0:
            return []
        raw_text = (
            f"PROTO50,src={envelope.source},ch={envelope.channel},"
            f"cap={self._cap_count},volt={self._voltage_count},res={self._resistance_count},"
            f"mixed={self._mixed_count},log={self._log_count},reject={self._reject_count},"
            f"frames={self._cap_count + self._voltage_count + self._resistance_count + self._mixed_count}"
        )
        return [
            LogRecord(
                timestamp=time.time(),
                monotonicTime=envelope.receivedMonotonicNs,
                source="host",
                channel="host",
                tag="PROTO50",
                severity="info",
                rawText=raw_text,
                parsedFields={
                    "src": envelope.source,
                    "ch": envelope.channel,
                    "cap": str(self._cap_count),
                    "volt": str(self._voltage_count),
                    "res": str(self._resistance_count),
                    "mixed": str(self._mixed_count),
                    "log": str(self._log_count),
                    "reject": str(self._reject_count),
                    "frames": str(self._cap_count + self._voltage_count + self._resistance_count + self._mixed_count),
                },
                recognised=True,
                sessionGeneration=envelope.sessionGeneration,
            )
        ]


def _normalized_envelope(envelope: TransportEnvelope) -> TransportEnvelope:
    channel = normalize_ble_channel(envelope.channel) if envelope.source == "ble" else envelope.channel
    if channel == envelope.channel:
        return envelope
    return _replace_envelope(envelope, channel, envelope.rawPayload)


def _replace_envelope(envelope: TransportEnvelope, channel: str, payload: bytes) -> TransportEnvelope:
    return TransportEnvelope(
        source=envelope.source,
        channel=channel,
        deviceId=envelope.deviceId,
        sessionGeneration=envelope.sessionGeneration,
        receivedMonotonicNs=envelope.receivedMonotonicNs,
        receivedWallTime=envelope.receivedWallTime,
        rawPayload=payload,
        remoteAddress=envelope.remoteAddress,
        metadata=dict(envelope.metadata),
    )


def _contains_g_fragment(payload: bytes) -> bool:
    return payload.startswith(b"G,") or b"\nG," in payload


def _looks_like_mixed_header(line: str) -> bool:
    fields = {
        item.split("=", maxsplit=1)[0].strip()
        for item in line.split(",")[1:]
        if "=" in item
    }
    geometry = {"seq", "rows", "cells", "profile"}
    canonicalIdentity = {"rgen", "rrid", "pgen", "prid"}
    legacyIdentity = {"rowsGen", "rowsRid", "profileGen", "profileRid"}
    return geometry <= fields and (canonicalIdentity <= fields or legacyIdentity <= fields)
