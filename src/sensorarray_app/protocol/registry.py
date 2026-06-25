from __future__ import annotations

import re
import time

from sensorarray_app.domain.models import DomainEvent, LogRecord, ParserErrorEvent, TransportEnvelope
from sensorarray_app.protocol.ble_fragments import BleFragmentReassembler, normalize_ble_channel
from sensorarray_app.protocol.cap_ascii import CapAsciiParser
from sensorarray_app.protocol.legacy_binary_voltage import LegacyFastBinaryVoltageProtocol, MAGIC_BYTES
from sensorarray_app.protocol.legacy_matv import LegacyMatvProtocol
from sensorarray_app.protocol.log_protocol import TextLogProtocol

_CAP_LINE_RE = re.compile(rb"(?m)^(?:C,|D\d+,|K,)")


class ProtocolRegistry:
    def __init__(self, circuit_offset_pf: float = 33.0):
        self.cap = CapAsciiParser(circuit_offset_pf=circuit_offset_pf)
        self.legacy_binary = LegacyFastBinaryVoltageProtocol()
        self.legacy_matv = LegacyMatvProtocol()
        self.log_protocol = TextLogProtocol()
        self.ble_fragments = BleFragmentReassembler()
        self._text_buffers: dict[tuple[str, str, int], bytearray] = {}
        self._feed_count = 0
        self._cap_count = 0
        self._log_count = 0
        self._reject_count = 0

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
        if envelope.channel == "data" or _contains_cap_ascii(envelope.rawPayload):
            cap_events = self.cap.feed(envelope)
            split: list[DomainEvent] = []
            for event in cap_events:
                if isinstance(event, LogRecord):
                    self._log_count += 1
                    split.extend(self.log_protocol.feed_line(event.rawText, envelope))
                elif isinstance(event, ParserErrorEvent):
                    self._reject_count += 1
                    split.append(event)
                else:
                    self._cap_count += 1
                    split.append(event)
            split.extend(self._diagnostic_event(envelope))
            return split
        events = self._feed_text(envelope)
        self._log_count += len([event for event in events if isinstance(event, LogRecord)])
        self._reject_count += len([event for event in events if isinstance(event, ParserErrorEvent)])
        events.extend(self._diagnostic_event(envelope))
        return events

    def _feed_text(self, envelope: TransportEnvelope) -> list[DomainEvent]:
        key = (envelope.source, envelope.channel, envelope.sessionGeneration)
        buffer = self._text_buffers.setdefault(key, bytearray())
        buffer.extend(envelope.rawPayload)
        events: list[DomainEvent] = []
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            raw_line = bytes(buffer[: newline + 1])
            del buffer[: newline + 1]
            try:
                line = raw_line.rstrip(b"\r\n").decode("ascii", errors="strict")
            except UnicodeDecodeError:
                events.append(
                    ParserErrorEvent(
                        source=envelope.source,
                        channel=envelope.channel,
                        reason="strict_ascii",
                        detail="non-ASCII text log line",
                        sessionGeneration=envelope.sessionGeneration,
                        rawText=raw_line.hex(" "),
                    )
                )
                continue
            matv = self.legacy_matv.parse_line(line, envelope)
            if matv is not None:
                events.append(matv)
            else:
                events.extend(self.log_protocol.feed_line(line, envelope))
        return events

    def _diagnostic_event(self, envelope: TransportEnvelope) -> list[LogRecord]:
        if self._feed_count % 50 != 0:
            return []
        raw_text = (
            f"PROTO50,src={envelope.source},ch={envelope.channel},"
            f"cap={self._cap_count},log={self._log_count},reject={self._reject_count},frames={self.cap.stats.frames}"
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
                    "log": str(self._log_count),
                    "reject": str(self._reject_count),
                    "frames": str(self.cap.stats.frames),
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


def _contains_cap_ascii(payload: bytes) -> bool:
    return _CAP_LINE_RE.search(payload) is not None


def _contains_g_fragment(payload: bytes) -> bool:
    return payload.startswith(b"G,") or b"\nG," in payload
