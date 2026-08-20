from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from sensorarray_app.domain.models import LogRecord, MeasurementFrame, ParserErrorEvent, TransportEnvelope
from sensorarray_app.protocol.crc import crc32_reflected


HEADER_RE = re.compile(r"^([VR]),")
# D4 is also an authoritative firmware runtime-diagnostic tag. Measurement
# chunks never start with key=value, which safely disambiguates interleaved
# ``D4,d=...`` logs while retaining strict validation of malformed D tokens.
DATA_RE = re.compile(r"^D(\d+),(?![A-Za-z][A-Za-z0-9_]*=)")
# P5/P50 are firmware performance summaries. Packed PGA chunks begin with a
# hexadecimal payload, never a key=value field.
PGA_RE = re.compile(r"^P(\d+),(?![A-Za-z][A-Za-z0-9_]*=)")
TRAILER_RE = re.compile(r"^K,")
X_TOKEN_RE = re.compile(r"^X([0-9A-Fa-f]{2})$")
MAX_CHUNK_VALUES = 16
PENDING_TIMEOUT_NS = 2_000_000_000
VALID_PGA_LITERALS = frozenset({0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20})

# These values are the production sensorarrayCellError_t values retained by
# firmware 8045e9e9. Unknown values must remain visible rather than being
# coerced to zero or causing a parser failure.
CELL_ERROR_REASONS = {
    0x00: "No firmware cell error",
    0x01: "Matrix route error",
    0x02: "ADS SPI error",
    0x03: "ADS DRDY timeout",
    0x04: "Stale measurement",
    0x05: "Reference alarm",
    0x06: "PGA absolute input alarm",
    0x07: "PGA differential input alarm",
    0x08: "ADC saturated",
    0x09: "ADC common-mode violation",
    0x0A: "Rail configuration invalid",
    0x0B: "Reference invalid",
    0x0C: "Resistance divider denominator near zero",
    0x0D: "Open circuit",
    0x0E: "Short circuit",
    0x0F: "Negative resistance",
    0x10: "Measurement out of range",
    0x11: "Measurement overflow",
    0x12: "Unstable measurement",
    0x13: "Autorange failed",
    0x14: "Unsupported measurement",
    0x15: "ADS register readback mismatch",
}


def firmware_cell_error_reason(code: int) -> str:
    numericCode = int(code) & 0xFF
    return CELL_ERROR_REASONS.get(numericCode, f"Unknown firmware cell error 0x{numericCode:02X}")


@dataclass
class MeasurementAsciiStats:
    frames: int = 0
    voltageFrames: int = 0
    resistanceFrames: int = 0
    rejects: int = 0
    crcFailures: int = 0
    sequenceGaps: int = 0
    duplicateD: int = 0
    missingD: int = 0
    extraD: int = 0
    duplicateP: int = 0
    missingP: int = 0
    extraP: int = 0
    shortData: int = 0
    extraData: int = 0
    headerMismatch: int = 0
    pendingTimeouts: int = 0
    strictAsciiFailures: int = 0
    badXTokens: int = 0
    lastSeq: int | None = None
    lastReject: str = ""


@dataclass
class _PendingMeasurementFrame:
    tag: str
    headerLine: str
    headerBytes: bytes
    fields: dict[str, str]
    mode: str
    unit: str
    scale: int
    format: str
    seq: int
    timestampUs: int
    rows: int
    cells: int
    generation: int
    requestId: int
    validBits: int
    freshBits: int
    errorBits: int
    expectedBits: int
    acquiredBits: int
    badCellCount: int
    expectedChunkLines: int
    startMonotonicNs: int
    dataLines: dict[int, bytes] = field(default_factory=dict)
    rawValuesByLine: dict[int, list[float]] = field(default_factory=dict)
    errorCodesByLine: dict[int, list[int]] = field(default_factory=dict)
    pgaLines: dict[int, bytes] = field(default_factory=dict)
    pgaValuesByLine: dict[int, list[int]] = field(default_factory=dict)

    def payload_bytes(self) -> bytes:
        # Firmware CRC order is header, all D lines, then all packed P lines;
        # every contributing line includes LF and K is excluded.
        payload = bytearray(self.headerBytes)
        for lineIndex in sorted(self.dataLines):
            payload.extend(self.dataLines[lineIndex])
        for lineIndex in sorted(self.pgaLines):
            payload.extend(self.pgaLines[lineIndex])
        return bytes(payload)


class MeasurementAsciiParser:
    """Strict parser for current firmware V/R, D, packed-P, K frames."""

    name = "CurrentMeasurementAsciiProtocol"

    def __init__(self):
        self._lineBuffer = bytearray()
        self._pending: _PendingMeasurementFrame | None = None
        self.stats = MeasurementAsciiStats()

    @property
    def hasPendingFrame(self) -> bool:
        return self._pending is not None

    def feed(self, envelope: TransportEnvelope) -> list[MeasurementFrame | LogRecord | ParserErrorEvent]:
        events: list[MeasurementFrame | LogRecord | ParserErrorEvent] = []
        self._lineBuffer.extend(envelope.rawPayload)
        while True:
            newlineIndex = self._lineBuffer.find(b"\n")
            if newlineIndex < 0:
                break
            rawLine = bytes(self._lineBuffer[: newlineIndex + 1])
            del self._lineBuffer[: newlineIndex + 1]
            events.extend(self.feed_line(rawLine, envelope))
        events.extend(self.check_timeout(envelope.receivedMonotonicNs, envelope))
        return events

    def feed_line(
        self,
        rawLine: bytes,
        envelope: TransportEnvelope,
    ) -> list[MeasurementFrame | LogRecord | ParserErrorEvent]:
        try:
            line = rawLine.rstrip(b"\r\n").decode("ascii", errors="strict")
        except UnicodeDecodeError:
            self.stats.strictAsciiFailures += 1
            if self._pending is not None:
                return [self._reject_pending("strict_ascii", "non-ASCII byte in V/R/D/P/K stream", envelope)]
            return [self._reject("strict_ascii", "non-ASCII byte in V/R/D/P/K stream", envelope, rawLine)]

        headerMatch = HEADER_RE.match(line)
        if headerMatch is not None:
            events: list[MeasurementFrame | LogRecord | ParserErrorEvent] = []
            if self._pending is not None:
                events.append(self._reject_pending("duplicate_header", "new V/R header before pending K", envelope))
            parsedHeader = self._parse_header(line, _crc_line_bytes(rawLine), headerMatch.group(1), envelope)
            if isinstance(parsedHeader, ParserErrorEvent):
                events.append(parsedHeader)
            else:
                self._pending = parsedHeader
            return events

        dataMatch = DATA_RE.match(line)
        if dataMatch is not None:
            if self._pending is None:
                return [self._reject("orphan_d", "D line without V/R header", envelope, rawLine)]
            return self._parse_data(line, rawLine, int(dataMatch.group(1)), envelope)

        pgaMatch = PGA_RE.match(line)
        if pgaMatch is not None:
            # P50 is a firmware performance summary, never a packed PGA row.
            if line.startswith("P50,"):
                return [self._log_record(line, envelope)]
            if self._pending is None:
                return [self._reject("orphan_p", "P line without V/R header", envelope, rawLine)]
            return self._parse_pga(line, rawLine, int(pgaMatch.group(1)), envelope)

        if TRAILER_RE.match(line):
            if self._pending is None:
                return [self._reject("orphan_k", "K line without V/R header", envelope, rawLine)]
            return self._parse_trailer(line, envelope)

        return [self._log_record(line, envelope)]

    def abort_pending(self, reason: str, detail: str, envelope: TransportEnvelope) -> list[ParserErrorEvent]:
        if self._pending is None:
            return []
        return [self._reject_pending(reason, detail, envelope)]

    def check_timeout(
        self,
        nowMonotonicNs: int,
        envelope: TransportEnvelope | None = None,
    ) -> list[ParserErrorEvent]:
        if self._pending is None:
            return []
        if nowMonotonicNs - self._pending.startMonotonicNs < PENDING_TIMEOUT_NS:
            return []
        self.stats.pendingTimeouts += 1
        fallbackEnvelope = envelope or TransportEnvelope(
            source="host",
            channel="host",
            deviceId="",
            sessionGeneration=0,
            receivedMonotonicNs=nowMonotonicNs,
            receivedWallTime=0.0,
            rawPayload=b"",
        )
        return [self._reject_pending("pending_timeout", "V/R/D/P/K pending frame timed out", fallbackEnvelope)]

    def reset(self) -> None:
        self._lineBuffer.clear()
        self._pending = None

    def _parse_header(
        self,
        line: str,
        rawLine: bytes,
        tag: str,
        envelope: TransportEnvelope,
    ) -> _PendingMeasurementFrame | ParserErrorEvent:
        fields = _parse_key_values(line)
        try:
            seq = _parse_decimal(fields, "seq")
            timestampUs = _parse_decimal(fields, "ts")
            rows = _parse_decimal(fields, "rows")
            cells = _parse_decimal(fields, "cells")
            generation = _parse_decimal(fields, "gen")
            requestId = _parse_decimal(fields, "rid")
            mode = fields["mode"]
            unit = fields["unit"]
            scale = _parse_decimal(fields, "scale")
            validBits = _parse_mask(fields, "valid")
            freshBits = _parse_mask(fields, "fresh")
            errorBits = _parse_mask(fields, "error")
            expectedBits = _parse_mask(fields, "expected", exactWidth=16)
            acquiredBits = _parse_mask(fields, "acquired", exactWidth=16)
            badCellCount = _parse_decimal(fields, "bad")
            # The current production formatter always emits this diagnostic
            # set. Parse every field now so malformed/missing metadata cannot
            # silently become a plausible zero in the domain model.
            reference = fields["ref"]
            _parse_binary_bool(fields, "rail")
            diagnosticValues = {
                name: _parse_decimal(fields, name)
                for name in ("age", "avdd", "avss", "vexc", "rref", "dur", "tr", "gc", "ov", "aa", "fb", "ir", "to", "st", "spi")
            }
            frameFormat = fields["fmt"]
            count = _parse_decimal(fields, "n")
        except (KeyError, ValueError) as exc:
            return self._reject("bad_header", f"invalid {tag} header fields: {exc}", envelope, rawLine)

        expectedMode, expectedUnit, expectedScale, expectedFormat = (
            ("VOLT", "V", -6, "uv-x") if tag == "V" else ("RES", "ohm", -3, "mohm-x")
        )
        if not (1 <= rows <= 8):
            return self._reject("bad_rows", f"rows out of range: {rows}", envelope, rawLine)
        if cells != rows * 8:
            return self._reject("bad_cells", f"cells {cells} != rows*8 {rows * 8}", envelope, rawLine)
        if count != cells:
            return self._reject("bad_n", f"n {count} != cells {cells}", envelope, rawLine)
        if (mode, unit, scale, frameFormat) != (expectedMode, expectedUnit, expectedScale, expectedFormat):
            return self._reject(
                "quantity_mismatch",
                f"{tag} requires mode={expectedMode},unit={expectedUnit},scale={expectedScale},fmt={expectedFormat}",
                envelope,
                rawLine,
            )
        if min(seq, timestampUs, generation, requestId) < 0:
            return self._reject("negative_identity", "V/R identities and timestamp must be non-negative", envelope, rawLine)
        activeBits = (1 << cells) - 1 if cells < 64 else (1 << 64) - 1
        if any(mask & ~activeBits for mask in (validBits, freshBits, errorBits, expectedBits, acquiredBits)):
            return self._reject("mask_out_of_range", "V/R mask contains inactive cell bits", envelope, rawLine)
        if acquiredBits & ~expectedBits:
            return self._reject("acquired_outside_expected", "V/R acquired must be a subset of expected", envelope, rawLine)
        if freshBits & ~acquiredBits:
            return self._reject("fresh_outside_acquired", "V/R fresh must be a subset of acquired", envelope, rawLine)
        expectedBadCellCount = cells - validBits.bit_count()
        if badCellCount != expectedBadCellCount:
            return self._reject(
                "bad_count_mismatch",
                f"bad {badCellCount} != cells-valid {expectedBadCellCount}",
                envelope,
                rawLine,
            )
        if not reference:
            return self._reject("bad_reference", "ref must not be empty", envelope, rawLine)
        if any(diagnosticValues[name] < 0 for name in ("age", "rref", "dur", "tr", "gc", "ov", "aa", "fb", "ir", "to", "st", "spi")):
            return self._reject("bad_diagnostic", "unsigned V/R diagnostic field is negative", envelope, rawLine)

        return _PendingMeasurementFrame(
            tag=tag,
            headerLine=line,
            headerBytes=rawLine,
            fields=fields,
            mode=mode,
            unit=unit,
            scale=scale,
            format=frameFormat,
            seq=seq,
            timestampUs=timestampUs,
            rows=rows,
            cells=cells,
            generation=generation,
            requestId=requestId,
            validBits=validBits,
            freshBits=freshBits,
            errorBits=errorBits,
            expectedBits=expectedBits,
            acquiredBits=acquiredBits,
            badCellCount=badCellCount,
            expectedChunkLines=(cells + MAX_CHUNK_VALUES - 1) // MAX_CHUNK_VALUES,
            startMonotonicNs=envelope.receivedMonotonicNs,
        )

    def _parse_data(
        self,
        line: str,
        rawLine: bytes,
        lineIndex: int,
        envelope: TransportEnvelope,
    ) -> list[ParserErrorEvent]:
        assert self._pending is not None
        pending = self._pending
        if pending.pgaLines:
            return [self._reject_pending("d_after_p", f"D{lineIndex} arrived after the PGA section began", envelope)]
        if lineIndex in pending.dataLines:
            self.stats.duplicateD += 1
            return [self._reject_pending("duplicate_d", f"duplicate D{lineIndex}", envelope)]
        if lineIndex >= pending.expectedChunkLines:
            self.stats.extraD += 1
            return [self._reject_pending("extra_d", f"unexpected D{lineIndex}", envelope)]
        expectedIndex = len(pending.dataLines)
        if lineIndex != expectedIndex:
            self.stats.missingD += 1
            return [self._reject_pending("missing_d", f"expected D{expectedIndex}, got D{lineIndex}", envelope)]

        tokens = line.split(",")[1:]
        expectedValues = min(MAX_CHUNK_VALUES, pending.cells - lineIndex * MAX_CHUNK_VALUES)
        if len(tokens) != expectedValues:
            if len(tokens) < expectedValues:
                self.stats.shortData += 1
                reason = "short_d_values"
            else:
                self.stats.extraData += 1
                reason = "too_many_d_values"
            return [self._reject_pending(reason, f"D{lineIndex} has {len(tokens)} values, expected {expectedValues}", envelope)]

        rawValues: list[float] = []
        errorCodes: list[int] = []
        for token in tokens:
            strippedToken = token.strip()
            xMatch = X_TOKEN_RE.match(strippedToken)
            if xMatch is not None:
                rawValues.append(float("nan"))
                errorCodes.append(int(xMatch.group(1), 16))
                continue
            if strippedToken.upper().startswith("X"):
                self.stats.badXTokens += 1
                return [self._reject_pending("bad_x", f"invalid firmware cell error token: {strippedToken}", envelope)]
            try:
                rawValues.append(float(int(strippedToken, 10)))
            except ValueError as exc:
                return [self._reject_pending("bad_d_value", f"invalid D value: {exc}", envelope)]
            errorCodes.append(0)

        pending.dataLines[lineIndex] = _crc_line_bytes(rawLine)
        pending.rawValuesByLine[lineIndex] = rawValues
        pending.errorCodesByLine[lineIndex] = errorCodes
        return []

    def _parse_pga(
        self,
        line: str,
        rawLine: bytes,
        lineIndex: int,
        envelope: TransportEnvelope,
    ) -> list[ParserErrorEvent]:
        assert self._pending is not None
        pending = self._pending
        if len(pending.dataLines) != pending.expectedChunkLines:
            self.stats.missingD += 1
            return [self._reject_pending("missing_d", f"P{lineIndex} arrived before all D lines", envelope)]
        if lineIndex in pending.pgaLines:
            self.stats.duplicateP += 1
            return [self._reject_pending("duplicate_p", f"duplicate P{lineIndex}", envelope)]
        if lineIndex >= pending.expectedChunkLines:
            self.stats.extraP += 1
            return [self._reject_pending("extra_p", f"unexpected P{lineIndex}", envelope)]
        expectedIndex = len(pending.pgaLines)
        if lineIndex != expectedIndex:
            self.stats.missingP += 1
            return [self._reject_pending("missing_p", f"expected P{expectedIndex}, got P{lineIndex}", envelope)]

        parts = line.split(",")
        packed = parts[1].strip() if len(parts) == 2 else ""
        expectedValues = min(MAX_CHUNK_VALUES, pending.cells - lineIndex * MAX_CHUNK_VALUES)
        if len(parts) != 2 or len(packed) != expectedValues * 2:
            return [
                self._reject_pending(
                    "bad_p_length",
                    f"P{lineIndex} packed hex length {len(packed)}, expected {expectedValues * 2}",
                    envelope,
                )
            ]
        try:
            pgaValues = [int(packed[offset : offset + 2], 16) for offset in range(0, len(packed), 2)]
        except ValueError as exc:
            return [self._reject_pending("bad_p_value", f"invalid packed PGA hex: {exc}", envelope)]
        invalidValues = [value for value in pgaValues if value not in VALID_PGA_LITERALS]
        if invalidValues:
            return [self._reject_pending("bad_p_gain", f"unsupported PGA literal 0x{invalidValues[0]:02X}", envelope)]

        pending.pgaLines[lineIndex] = _crc_line_bytes(rawLine)
        pending.pgaValuesByLine[lineIndex] = pgaValues
        return []

    def _parse_trailer(self, line: str, envelope: TransportEnvelope) -> list[MeasurementFrame | ParserErrorEvent]:
        assert self._pending is not None
        pending = self._pending
        fields = _parse_key_values(line)
        try:
            seq = _parse_decimal(fields, "seq")
            generation = _parse_decimal(fields, "gen")
            requestId = _parse_decimal(fields, "rid")
            expectedCrc = int(fields["crc"], 16)
        except (KeyError, ValueError) as exc:
            return [self._reject_pending("bad_k", f"invalid K fields: {exc}", envelope)]
        if re.fullmatch(r"[0-9A-Fa-f]{8}", fields.get("crc", "")) is None:
            return [self._reject_pending("bad_crc_width", "V/R K crc must be exactly eight hex characters", envelope)]

        if (seq, generation, requestId) != (pending.seq, pending.generation, pending.requestId):
            self.stats.headerMismatch += 1
            return [self._reject_pending("k_mismatch", "K seq/gen/rid does not match V/R header", envelope)]
        if len(pending.dataLines) != pending.expectedChunkLines:
            self.stats.missingD += 1
            return [self._reject_pending("missing_d", "not all D lines received before K", envelope)]
        if len(pending.pgaLines) != pending.expectedChunkLines:
            self.stats.missingP += 1
            return [self._reject_pending("missing_p", "not all P lines received before K", envelope)]

        actualCrc = crc32_reflected(pending.payload_bytes())
        if actualCrc != expectedCrc:
            self.stats.crcFailures += 1
            return [self._reject_pending("crc", f"crc 0x{expectedCrc:08X} != computed 0x{actualCrc:08X}", envelope)]

        rawValues = np.asarray(
            [value for lineIndex in sorted(pending.rawValuesByLine) for value in pending.rawValuesByLine[lineIndex]],
            dtype=np.float64,
        )
        errorCodes = np.asarray(
            [value for lineIndex in sorted(pending.errorCodesByLine) for value in pending.errorCodesByLine[lineIndex]],
            dtype=np.uint8,
        )
        pgaValues = np.asarray(
            [value for lineIndex in sorted(pending.pgaValuesByLine) for value in pending.pgaValuesByLine[lineIndex]],
            dtype=np.uint8,
        )
        if rawValues.size != pending.cells or errorCodes.size != pending.cells or pgaValues.size != pending.cells:
            self.stats.shortData += 1
            return [self._reject_pending("wrong_cell_count", "D/P cell count does not match header cells", envelope)]

        validMask = _mask_array(pending.validBits, pending.cells)
        freshMask = _mask_array(pending.freshBits, pending.cells)
        errorMask = _mask_array(pending.errorBits, pending.cells)
        expectedMask = _mask_array(pending.expectedBits, pending.cells)
        acquiredMask = _mask_array(pending.acquiredBits, pending.cells)
        tokenInvalid = ~np.isfinite(rawValues)
        if not np.array_equal(validMask, ~tokenInvalid):
            return [self._reject_pending("valid_token_mismatch", "header valid mask disagrees with D Xhh tokens", envelope)]
        if not np.array_equal(errorMask, tokenInvalid):
            return [self._reject_pending("error_token_mismatch", "header error mask disagrees with D Xhh tokens", envelope)]

        physicalValues = rawValues * (10.0**pending.scale)
        frame = MeasurementFrame(
            mode=pending.mode,
            seq=pending.seq,
            timestampUs=pending.timestampUs,
            durationUs=_parse_decimal(pending.fields, "dur"),
            rows=pending.rows,
            cells=pending.cells,
            generation=pending.generation,
            requestId=pending.requestId,
            unit=pending.unit,
            scale=pending.scale,
            format=pending.format,
            rawFixedValues=rawValues,
            physicalValues=physicalValues,
            validMask=validMask,
            freshMask=freshMask,
            errorMask=errorMask,
            errorCodes=errorCodes,
            errorReasons=tuple(firmware_cell_error_reason(code) if invalid else "" for code, invalid in zip(errorCodes, tokenInvalid, strict=True)),
            pgaValues=pgaValues,
            pgaBypassMask=pgaValues == 0,
            reference=pending.fields["ref"],
            railValid=_parse_binary_bool(pending.fields, "rail"),
            railAgeFrames=_parse_decimal(pending.fields, "age"),
            avddUv=_parse_decimal(pending.fields, "avdd"),
            avssUv=_parse_decimal(pending.fields, "avss"),
            matrixReferenceUv=_parse_decimal(pending.fields, "vexc"),
            referenceResistorOhms=_parse_decimal(pending.fields, "rref"),
            transitionDurationUs=_parse_decimal(pending.fields, "tr"),
            gainChangeCount=_parse_decimal(pending.fields, "gc"),
            overrangeCount=_parse_decimal(pending.fields, "ov"),
            autorangeAttemptCount=_parse_decimal(pending.fields, "aa"),
            autorangeFallbackCount=_parse_decimal(pending.fields, "fb"),
            recoveredRetryCount=_parse_decimal(pending.fields, "ir"),
            drdyTimeoutCount=_parse_decimal(pending.fields, "to"),
            staleCount=_parse_decimal(pending.fields, "st"),
            spiErrorCount=_parse_decimal(pending.fields, "spi"),
            badCellCount=pending.badCellCount,
            sourceTransport=envelope.source,
            sessionGeneration=envelope.sessionGeneration,
            receivedTime=envelope.receivedWallTime,
            receivedMonotonicNs=envelope.receivedMonotonicNs,
            deviceId=envelope.deviceId,
            rawHeader=pending.headerLine,
            rawTrailer=line,
            rawFields=dict(pending.fields),
            expectedMask=expectedMask,
            acquiredMask=acquiredMask,
            acquisitionMasksKnown=True,
            connectionGeneration=envelope.connectionGeneration,
            bootId=envelope.bootId,
        )
        if self.stats.lastSeq is not None and pending.seq > self.stats.lastSeq + 1:
            self.stats.sequenceGaps += pending.seq - self.stats.lastSeq - 1
        self.stats.lastSeq = pending.seq
        self.stats.frames += 1
        if pending.mode == "VOLT":
            self.stats.voltageFrames += 1
        else:
            self.stats.resistanceFrames += 1
        self._pending = None
        return [frame]

    def _reject(self, reason: str, detail: str, envelope: TransportEnvelope, rawLine: bytes) -> ParserErrorEvent:
        self.stats.rejects += 1
        self.stats.lastReject = reason
        return ParserErrorEvent(
            source=envelope.source,
            channel=envelope.channel,
            reason=reason,
            detail=detail,
            sessionGeneration=envelope.sessionGeneration,
            rawText=_safe_ascii(rawLine),
        )

    def _reject_pending(self, reason: str, detail: str, envelope: TransportEnvelope) -> ParserErrorEvent:
        self._pending = None
        return self._reject(reason, detail, envelope, b"")

    @staticmethod
    def _log_record(line: str, envelope: TransportEnvelope) -> LogRecord:
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
    for item in line.split(",")[1:]:
        if "=" not in item:
            continue
        key, value = item.split("=", maxsplit=1)
        fields[key.strip()] = value.strip()
    return fields


def _parse_decimal(fields: dict[str, str], name: str, default: int | None = None) -> int:
    if name not in fields:
        if default is not None:
            return int(default)
        raise KeyError(name)
    return int(fields[name], 10)


def _parse_mask(fields: dict[str, str], name: str, *, exactWidth: int = 16) -> int:
    text = fields[name].strip()
    if re.fullmatch(rf"[0-9A-Fa-f]{{{exactWidth}}}", text) is None:
        raise ValueError(f"{name} must be exactly {exactWidth} hex characters")
    return int(text, 16)


def _parse_binary_bool(fields: dict[str, str], name: str) -> bool:
    value = fields[name].strip()
    if value not in {"0", "1"}:
        raise ValueError(f"{name} must be 0 or 1")
    return value == "1"


def _mask_array(mask: int, cells: int) -> np.ndarray:
    return np.asarray([bool((mask >> cellIndex) & 1) for cellIndex in range(cells)], dtype=bool)


def _safe_ascii(rawLine: bytes) -> str:
    try:
        return rawLine.decode("ascii", errors="strict").rstrip("\r\n")
    except UnicodeDecodeError:
        return rawLine.hex(" ")


def _crc_line_bytes(rawLine: bytes) -> bytes:
    if rawLine.endswith(b"\r\n"):
        return rawLine[:-2] + b"\n"
    return rawLine


__all__ = [
    "CELL_ERROR_REASONS",
    "MeasurementAsciiParser",
    "MeasurementAsciiStats",
    "firmware_cell_error_reason",
]
