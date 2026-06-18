from __future__ import annotations

from sensorarray_app.domain.models import DomainEvent, LogRecord, ParserErrorEvent, TransportEnvelope
from sensorarray_app.protocol.cap_ascii import CapAsciiParser
from sensorarray_app.protocol.legacy_binary_voltage import LegacyFastBinaryVoltageProtocol, MAGIC_BYTES
from sensorarray_app.protocol.legacy_matv import LegacyMatvProtocol
from sensorarray_app.protocol.log_protocol import TextLogProtocol


class ProtocolRegistry:
    def __init__(self, circuit_offset_pf: float = 33.0):
        self.cap = CapAsciiParser(circuit_offset_pf=circuit_offset_pf)
        self.legacy_binary = LegacyFastBinaryVoltageProtocol()
        self.legacy_matv = LegacyMatvProtocol()
        self.log_protocol = TextLogProtocol()
        self._text_buffers: dict[tuple[str, str, int], bytearray] = {}

    def feed(self, envelope: TransportEnvelope) -> list[DomainEvent]:
        if envelope.rawPayload.startswith(MAGIC_BYTES) or self.legacy_binary._buffer:
            return self.legacy_binary.feed(envelope)
        if envelope.channel == "data":
            cap_events = self.cap.feed(envelope)
            split: list[DomainEvent] = []
            for event in cap_events:
                if isinstance(event, LogRecord):
                    split.extend(self.log_protocol.feed_line(event.rawText, envelope))
                else:
                    split.append(event)
            return split
        return self._feed_text(envelope)

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
