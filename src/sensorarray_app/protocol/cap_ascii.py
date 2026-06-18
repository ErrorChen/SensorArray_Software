from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sensorarray_app.domain.capacitance import fixed_to_pf, valid_mask_from_fixed
from sensorarray_app.domain.models import CapacitanceFrame, LogRecord, ParserErrorEvent, TransportEnvelope
from sensorarray_app.protocol.crc import crc32_reflected

HEADER_RE = re.compile(r"^C,")
DATA_RE = re.compile(r"^D(\d+),")
TRAILER_RE = re.compile(r"^K,")
MAX_D_VALUES = 16
PENDING_TIMEOUT_NS = 2_000_000_000


@dataclass
class CapAsciiStats:
    frames: int = 0
    rejects: int = 0
    crcFailures: int = 0
    sequenceGaps: int = 0
    duplicateD: int = 0
    missingD: int = 0
    extraD: int = 0
    shortData: int = 0
    extraData: int = 0
    headerMismatch: int = 0
    pendingTimeouts: int = 0
    strictAsciiFailures: int = 0
    lastSeq: int | None = None
    lastReject: str = ""


@dataclass
class _PendingFrame:
    headerLine: str
    headerBytes: bytes
    fields: dict[str, str]
    seq: int
    rows: int
    cells: int
    generation: int
    requestId: int
    rowFreshMask: int
    primaryFreshMask: int
    secondaryFreshMask: int
    badStaleCount: int
    badMixedCount: int
    badInvalidCount: int
    expectedDataLines: int
    startMonotonicNs: int
    dataLines: dict[int, bytes] = field(default_factory=dict)
    valuesByLine: dict[int, list[int]] = field(default_factory=dict)

    def payload_bytes(self) -> bytes:
        data = bytearray(self.headerBytes)
        for index in sorted(self.dataLines):
            data.extend(self.dataLines[index])
        return bytes(data)


class CapAsciiParser:
    name = "B41CapAsciiProtocol"

    def __init__(self, circuit_offset_pf: float = 33.0):
        self.circuit_offset_pf = float(circuit_offset_pf)
        self._line_buffer = bytearray()
        self._pending: _PendingFrame | None = None
        self.stats = CapAsciiStats()

    def feed(self, envelope: TransportEnvelope) -> list[CapacitanceFrame | LogRecord | ParserErrorEvent]:
        events: list[CapacitanceFrame | LogRecord | ParserErrorEvent] = []
        self._line_buffer.extend(envelope.rawPayload)
        while True:
            newline = self._line_buffer.find(b"\n")
            if newline < 0:
                break
            raw_line = bytes(self._line_buffer[: newline + 1])
            del self._line_buffer[: newline + 1]
            events.extend(self.feed_line(raw_line, envelope))
        events.extend(self.check_timeout(envelope.receivedMonotonicNs, envelope))
        return events

    def feed_line(self, raw_line: bytes, envelope: TransportEnvelope) -> list[CapacitanceFrame | LogRecord | ParserErrorEvent]:
        try:
            line = raw_line.rstrip(b"\r\n").decode("ascii", errors="strict")
        except UnicodeDecodeError:
            self.stats.strictAsciiFailures += 1
            return [self._reject("strict_ascii", "non-ASCII byte in C/D/K stream", envelope, raw_line)]

        if HEADER_RE.match(line):
            events: list[CapacitanceFrame | LogRecord | ParserErrorEvent] = []
            if self._pending is not None:
                events.append(self._reject_pending("duplicate_c", "new C header before pending K", envelope))
            pending_or_error = self._parse_header(line, _crc_line_bytes(raw_line), envelope)
            if isinstance(pending_or_error, ParserErrorEvent):
                events.append(pending_or_error)
            else:
                self._pending = pending_or_error
            return events

        if DATA_RE.match(line):
            if self._pending is None:
                return [self._reject("orphan_d", "D line without C header", envelope, raw_line)]
            return self._parse_data(line, raw_line, envelope)

        if TRAILER_RE.match(line):
            if self._pending is None:
                return [self._reject("orphan_k", "K line without C header", envelope, raw_line)]
            return self._parse_trailer(line, raw_line, envelope)

        return [self._log_record(line, envelope)]

    def check_timeout(
        self,
        now_monotonic_ns: int,
        envelope: TransportEnvelope | None = None,
    ) -> list[ParserErrorEvent]:
        if self._pending is None:
            return []
        if now_monotonic_ns - self._pending.startMonotonicNs < PENDING_TIMEOUT_NS:
            return []
        self.stats.pendingTimeouts += 1
        dummy = envelope or TransportEnvelope(
            source="host",
            channel="host",
            deviceId="",
            sessionGeneration=0,
            receivedMonotonicNs=now_monotonic_ns,
            receivedWallTime=0.0,
            rawPayload=b"",
        )
        return [self._reject_pending("pending_timeout", "C/D/K pending frame timed out", dummy)]

    def reset(self) -> None:
        self._line_buffer.clear()
        self._pending = None

    def _parse_header(self, line: str, raw_line: bytes, envelope: TransportEnvelope) -> _PendingFrame | ParserErrorEvent:
        fields = _parse_key_values(line)
        try:
            rows = int(fields["rows"], 0)
            cells = int(fields["cells"], 0)
            n = int(fields["n"], 0)
            seq = int(fields["seq"], 0)
            gen = int(fields["gen"], 0)
            rid = int(fields["rid"], 0)
        except (KeyError, ValueError) as exc:
            return self._reject("bad_header", f"invalid C header fields: {exc}", envelope, raw_line)
        if not (1 <= rows <= 8):
            return self._reject("bad_rows", f"rows out of range: {rows}", envelope, raw_line)
        if cells != rows * 8:
            return self._reject("bad_cells", f"cells {cells} != rows*8 {rows * 8}", envelope, raw_line)
        if n != cells:
            return self._reject("bad_n", f"n {n} != cells {cells}", envelope, raw_line)
        bad = _parse_bad_tuple(fields.get("bad", "0/0/0"))
        return _PendingFrame(
            headerLine=line,
            headerBytes=raw_line,
            fields=fields,
            seq=seq,
            rows=rows,
            cells=cells,
            generation=gen,
            requestId=rid,
            rowFreshMask=_parse_hex(fields.get("rf")),
            primaryFreshMask=_parse_hex(fields.get("pf")),
            secondaryFreshMask=_parse_hex(fields.get("sf")),
            badStaleCount=bad[0],
            badMixedCount=bad[1],
            badInvalidCount=bad[2],
            expectedDataLines=(cells + MAX_D_VALUES - 1) // MAX_D_VALUES,
            startMonotonicNs=envelope.receivedMonotonicNs,
        )

    def _parse_data(self, line: str, raw_line: bytes, envelope: TransportEnvelope) -> list[ParserErrorEvent]:
        assert self._pending is not None
        match = DATA_RE.match(line)
        if match is None:
            return [self._reject("bad_d", "invalid D prefix", envelope, raw_line)]
        index = int(match.group(1))
        expected_index = len(self._pending.dataLines)
        if index in self._pending.dataLines:
            self.stats.duplicateD += 1
            return [self._reject_pending("duplicate_d", f"duplicate D{index}", envelope)]
        if index != expected_index:
            self.stats.missingD += 1
            return [self._reject_pending("missing_d", f"expected D{expected_index}, got D{index}", envelope)]
        if index >= self._pending.expectedDataLines:
            self.stats.extraD += 1
            return [self._reject_pending("extra_d", f"unexpected D{index}", envelope)]

        fields = line.split(",")[1:]
        try:
            values = [int(item.strip(), 0) for item in fields if item.strip() != ""]
        except ValueError as exc:
            return [self._reject_pending("bad_d_value", f"invalid D value: {exc}", envelope)]
        is_last = index == self._pending.expectedDataLines - 1
        if len(values) > MAX_D_VALUES:
            self.stats.extraData += 1
            return [self._reject_pending("too_many_d_values", f"D{index} has {len(values)} values", envelope)]
        if not is_last and len(values) != MAX_D_VALUES:
            self.stats.shortData += 1
            return [self._reject_pending("short_d_values", f"D{index} has {len(values)} values", envelope)]
        self._pending.dataLines[index] = _crc_line_bytes(raw_line)
        self._pending.valuesByLine[index] = values
        return []

    def _parse_trailer(
        self,
        line: str,
        raw_line: bytes,
        envelope: TransportEnvelope,
    ) -> list[CapacitanceFrame | ParserErrorEvent]:
        assert self._pending is not None
        pending = self._pending
        fields = _parse_key_values(line)
        try:
            seq = int(fields["seq"], 0)
            gen = int(fields["gen"], 0)
            rid = int(fields["rid"], 0)
            expected_crc = int(fields["crc"], 16)
        except (KeyError, ValueError) as exc:
            return [self._reject_pending("bad_k", f"invalid K fields: {exc}", envelope)]

        if seq != pending.seq or gen != pending.generation or rid != pending.requestId:
            self.stats.headerMismatch += 1
            return [self._reject_pending("k_mismatch", "K seq/gen/rid does not match C", envelope)]
        if len(pending.dataLines) != pending.expectedDataLines:
            self.stats.missingD += 1
            return [self._reject_pending("missing_d", "not all D lines received before K", envelope)]
        values = [value for index in sorted(pending.valuesByLine) for value in pending.valuesByLine[index]]
        if len(values) != pending.cells:
            if len(values) < pending.cells:
                self.stats.shortData += 1
                reason = "short_data"
            else:
                self.stats.extraData += 1
                reason = "extra_data"
            return [self._reject_pending(reason, f"collected {len(values)} values for {pending.cells} cells", envelope)]

        actual_crc = crc32_reflected(pending.payload_bytes())
        if actual_crc != expected_crc:
            self.stats.crcFailures += 1
            return [self._reject_pending("crc", f"crc 0x{expected_crc:08X} != computed 0x{actual_crc:08X}", envelope)]

        raw_fixed, raw_pf, corrected_pf = fixed_to_pf(values, self.circuit_offset_pf)
        valid = valid_mask_from_fixed(raw_fixed)
        frame = CapacitanceFrame(
            seq=pending.seq,
            timestampUs=int(pending.fields.get("ts", "0"), 0),
            rows=pending.rows,
            cells=pending.cells,
            generation=pending.generation,
            requestId=pending.requestId,
            rowFreshMask=pending.rowFreshMask,
            primaryFreshMask=pending.primaryFreshMask,
            secondaryFreshMask=pending.secondaryFreshMask,
            badStaleCount=pending.badStaleCount,
            badMixedCount=pending.badMixedCount,
            badInvalidCount=pending.badInvalidCount,
            rawFixedValues=raw_fixed,
            rawPfValues=raw_pf,
            correctedPfValues=corrected_pf,
            validMask=valid,
            sourceTransport=envelope.source,
            sessionGeneration=envelope.sessionGeneration,
            receivedTime=envelope.receivedWallTime,
            receivedMonotonicNs=envelope.receivedMonotonicNs,
            deviceId=envelope.deviceId,
            rawHeader=pending.headerLine,
            rawTrailer=line,
        )
        if self.stats.lastSeq is not None and pending.seq > self.stats.lastSeq + 1:
            self.stats.sequenceGaps += pending.seq - self.stats.lastSeq - 1
        self.stats.lastSeq = pending.seq
        self.stats.frames += 1
        self._pending = None
        return [frame]

    def _reject(self, reason: str, detail: str, envelope: TransportEnvelope, raw_line: bytes) -> ParserErrorEvent:
        self.stats.rejects += 1
        self.stats.lastReject = reason
        return ParserErrorEvent(
            source=envelope.source,
            channel=envelope.channel,
            reason=reason,
            detail=detail,
            sessionGeneration=envelope.sessionGeneration,
            rawText=_safe_ascii(raw_line),
        )

    def _reject_pending(self, reason: str, detail: str, envelope: TransportEnvelope) -> ParserErrorEvent:
        self._pending = None
        return self._reject(reason, detail, envelope, b"")

    def _log_record(self, line: str, envelope: TransportEnvelope) -> LogRecord:
        tag = line.split(",", maxsplit=1)[0].strip() or "UNKNOWN"
        fields = _parse_key_values(line)
        severity = "error" if tag.startswith(("ERR", "ERROR")) else "warning" if tag.startswith(("WARN", "W")) else "info"
        return LogRecord(
            timestamp=envelope.receivedWallTime,
            monotonicTime=envelope.receivedMonotonicNs,
            source=envelope.source,
            channel=envelope.channel,
            tag=tag,
            severity=severity,
            rawText=line,
            parsedFields=fields,
            recognised=False,
            sessionGeneration=envelope.sessionGeneration,
        )


def _parse_key_values(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    parts = [part.strip() for part in line.split(",")]
    for item in parts[1:]:
        if not item:
            continue
        if "=" in item:
            key, value = item.split("=", maxsplit=1)
            fields[key.strip()] = value.strip()
    return fields


def _parse_bad_tuple(value: str) -> tuple[int, int, int]:
    parts = str(value).split("/")
    out = []
    for item in parts[:3]:
        try:
            out.append(int(item, 0))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return out[0], out[1], out[2]


def _parse_hex(value: Any) -> int:
    if value is None:
        return 0
    text = str(value).strip()
    try:
        return int(text, 16)
    except ValueError:
        try:
            return int(text, 0)
        except ValueError:
            return 0


def _safe_ascii(raw_line: bytes) -> str:
    try:
        return raw_line.decode("ascii", errors="strict").rstrip("\r\n")
    except UnicodeDecodeError:
        return raw_line.hex(" ")


def _crc_line_bytes(raw_line: bytes) -> bytes:
    if raw_line.endswith(b"\r\n"):
        return raw_line[:-2] + b"\n"
    return raw_line
