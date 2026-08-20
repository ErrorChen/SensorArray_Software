from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from sensorarray_app.constants import FDC_CIRCUIT_OFFSET_PF
from sensorarray_app.domain.models import (
    LogRecord,
    MixedMeasurementFrame,
    ParserErrorEvent,
    RowMeasurement,
    TransportEnvelope,
    normalize_wire_mixed_profile,
)
from sensorarray_app.protocol.crc import crc32_reflected
from sensorarray_app.protocol.measurement_ascii import (
    VALID_PGA_LITERALS,
    firmware_cell_error_reason,
)


HEADER_RE = re.compile(r"^M,")
ROW_RE = re.compile(r"^MR,")
TRAILER_RE = re.compile(r"^K,")
X_TOKEN_RE = re.compile(r"^X([0-9A-Fa-f]{2})$")
PENDING_TIMEOUT_NS = 2_000_000_000
ROW_CELLS = 8
MODE_WIRE = {
    "CAP": ("pF", -6, "pf6"),
    "VOLT": ("V", -6, "uv-x"),
    "RES": ("ohm", -3, "mohm-x"),
}
MODE_FROM_WIRE = {"CAP": "CAP", "VOLT": "VOLT", "RES": "RES"}


@dataclass
class MixedAsciiStats:
    frames: int = 0
    rejects: int = 0
    crcFailures: int = 0
    sequenceGaps: int = 0
    duplicateRows: int = 0
    missingRows: int = 0
    profileMismatches: int = 0
    pendingTimeouts: int = 0
    strictAsciiFailures: int = 0
    lastSeq: int | None = None
    lastReject: str = ""


@dataclass
class _PendingMixedFrame:
    headerLine: str
    headerBytes: bytes
    seq: int
    timestampUs: int
    rows: int
    cells: int
    rowsGeneration: int
    rowsRequestId: int
    profileGeneration: int
    profileRequestId: int
    profile: tuple[str, ...]
    expectedBits: int
    acquiredBits: int
    startMonotonicNs: int
    rowLines: dict[int, bytes] = field(default_factory=dict)
    rowLineOrder: list[bytes] = field(default_factory=list)
    rowFrames: dict[int, RowMeasurement] = field(default_factory=dict)

    def payload_bytes(self) -> bytes:
        payload = bytearray(self.headerBytes)
        for rowLine in self.rowLineOrder:
            payload.extend(rowLine)
        return bytes(payload)


class MixedMeasurementAsciiParser:
    """Strict atomic parser for heterogeneous ``M/MR/K`` frames.

    Canonical wire schema::

        M,seq=1,ts=2,rows=2,cells=16,rgen=3,rrid=4,
          pgen=5,prid=6,profile=RVVCCVVR,fmt=mix1
        MR,s=1,m=RES,unit=ohm,scale=-3,valid=FF,fresh=FF,
          error=00,fmt=mohm-x,D=...,...
        MR,s=2,m=VOLT,unit=V,scale=-6,...,fmt=uv-x,D=...,...
        K,seq=1,rgen=3,rrid=4,pgen=5,prid=6,crc=12345678

    This is the exact schema emitted by firmware 8045e9e9.  The wire profile
    always has eight characters, with C/V/R in the active prefix and N in the
    inactive suffix.  Physical row records may be routed by ``s`` rather than
    arrival order, but must be unique and complete.  CRC covers the exact M
    and MR bytes in arrival order including LF, with CRLF normalised to LF,
    and excludes K.
    """

    name = "MixedMeasurementAsciiProtocol"

    def __init__(self, circuitOffsetPf: float = FDC_CIRCUIT_OFFSET_PF):
        self.circuitOffsetPf = float(circuitOffsetPf)
        self._lineBuffer = bytearray()
        self._pending: _PendingMixedFrame | None = None
        self.stats = MixedAsciiStats()

    @property
    def hasPendingFrame(self) -> bool:
        return self._pending is not None

    def feed(self, envelope: TransportEnvelope) -> list[MixedMeasurementFrame | LogRecord | ParserErrorEvent]:
        events: list[MixedMeasurementFrame | LogRecord | ParserErrorEvent] = []
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
    ) -> list[MixedMeasurementFrame | LogRecord | ParserErrorEvent]:
        try:
            line = rawLine.rstrip(b"\r\n").decode("ascii", errors="strict")
        except UnicodeDecodeError:
            self.stats.strictAsciiFailures += 1
            if self._pending is not None:
                return [self._reject_pending("strict_ascii", "non-ASCII byte in M/MR/K stream", envelope)]
            return [self._reject("strict_ascii", "non-ASCII byte in M/MR/K stream", envelope, rawLine)]

        if HEADER_RE.match(line):
            events: list[MixedMeasurementFrame | LogRecord | ParserErrorEvent] = []
            if self._pending is not None:
                events.append(self._reject_pending("duplicate_header", "new M header before pending K", envelope))
            parsedHeader = self._parse_header(line, _crc_line_bytes(rawLine), envelope)
            if isinstance(parsedHeader, ParserErrorEvent):
                events.append(parsedHeader)
            else:
                self._pending = parsedHeader
            return events
        if ROW_RE.match(line):
            if self._pending is None:
                return [self._reject("orphan_mr", "MR row without M header", envelope, rawLine)]
            return self._parse_row(line, rawLine, envelope)
        if TRAILER_RE.match(line):
            if self._pending is None:
                return [self._reject("orphan_k", "K trailer without M header", envelope, rawLine)]
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
        if self._pending is None or nowMonotonicNs - self._pending.startMonotonicNs < PENDING_TIMEOUT_NS:
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
        return [self._reject_pending("pending_timeout", "M/MR/K pending frame timed out", fallbackEnvelope)]

    def reset(self) -> None:
        self._lineBuffer.clear()
        self._pending = None

    def _parse_header(
        self,
        line: str,
        rawLine: bytes,
        envelope: TransportEnvelope,
    ) -> _PendingMixedFrame | ParserErrorEvent:
        fields = _parse_key_values(line)
        try:
            seq = _decimal(fields, "seq")
            timestampUs = _decimal(fields, "ts")
            rows = _decimal(fields, "rows")
            cells = _decimal(fields, "cells")
            rowsGeneration = _decimal(fields, "rgen")
            rowsRequestId = _decimal(fields, "rrid")
            profileGeneration = _decimal(fields, "pgen")
            profileRequestId = _decimal(fields, "prid")
            profile = normalize_wire_mixed_profile(fields["profile"], rows)
            expectedBits = _hex(fields, "expected", width=16)
            acquiredBits = _hex(fields, "acquired", width=16)
        except (KeyError, ValueError) as exc:
            return self._reject("bad_m_header", f"invalid M header fields: {exc}", envelope, rawLine)
        if not (1 <= rows <= 8):
            return self._reject("bad_rows", f"rows out of range: {rows}", envelope, rawLine)
        if cells != rows * ROW_CELLS:
            return self._reject("bad_cells", f"cells must equal rows*8 ({rows * ROW_CELLS})", envelope, rawLine)
        if fields.get("fmt") != "mix1":
            return self._reject(
                "header_format_mismatch",
                "M header requires fmt=mix1",
                envelope,
                rawLine,
            )
        if min(seq, timestampUs, rowsGeneration, rowsRequestId, profileGeneration, profileRequestId) < 0:
            return self._reject("negative_identity", "M identities and timestamp must be non-negative", envelope, rawLine)
        activeBits = (1 << cells) - 1 if cells < 64 else (1 << 64) - 1
        if expectedBits & ~activeBits or acquiredBits & ~activeBits:
            return self._reject("mask_out_of_range", "M expected/acquired contains inactive cell bits", envelope, rawLine)
        if acquiredBits & ~expectedBits:
            return self._reject("acquired_outside_expected", "M acquired must be a subset of expected", envelope, rawLine)
        return _PendingMixedFrame(
            headerLine=line,
            headerBytes=rawLine,
            seq=seq,
            timestampUs=timestampUs,
            rows=rows,
            cells=cells,
            rowsGeneration=rowsGeneration,
            rowsRequestId=rowsRequestId,
            profileGeneration=profileGeneration,
            profileRequestId=profileRequestId,
            profile=profile,
            expectedBits=expectedBits,
            acquiredBits=acquiredBits,
            startMonotonicNs=envelope.receivedMonotonicNs,
        )

    def _parse_row(self, line: str, rawLine: bytes, envelope: TransportEnvelope) -> list[ParserErrorEvent]:
        assert self._pending is not None
        pending = self._pending
        fields = _parse_key_values(line)
        if "s" not in fields or "m" not in fields or ",D=" not in line:
            return [self._reject_pending("mixed_row_schema", "MR row requires s,m,D", envelope)]
        if fields["m"].strip().upper() not in MODE_FROM_WIRE:
            return [self._reject_pending("bad_row_mode_wire", "MR m must be CAP, VOLT, or RES", envelope)]
        for maskName in ("expected", "acquired", "valid", "fresh", "error"):
            if re.fullmatch(r"[0-9A-Fa-f]{2}", fields.get(maskName, "")) is None:
                return [
                    self._reject_pending(
                        "bad_row_mask_width",
                        f"MR {maskName} must be exactly two hex characters",
                        envelope,
                    )
                ]
        try:
            row = _decimal(fields, "s")
            rawMode = fields["m"].strip().upper()
            mode = MODE_FROM_WIRE[rawMode]
            unit = fields["unit"].strip()
            scale = _decimal(fields, "scale", allowNegative=True)
            expectedBits = _hex(fields, "expected", width=2)
            acquiredBits = _hex(fields, "acquired", width=2)
            validBits = _hex(fields, "valid", width=2)
            freshBits = _hex(fields, "fresh", width=2)
            errorBits = _hex(fields, "error", width=2)
            valueTokens = _mixed_value_tokens(line, fields)
        except (KeyError, ValueError) as exc:
            return [self._reject_pending("bad_mr", f"invalid MR fields: {exc}", envelope)]

        if row in pending.rowFrames:
            self.stats.duplicateRows += 1
            return [self._reject_pending("duplicate_row", f"duplicate MR row {row}", envelope)]
        if not (1 <= row <= pending.rows):
            return [self._reject_pending("row_out_of_range", f"MR row {row} outside active rows", envelope)]
        if mode not in MODE_WIRE:
            return [self._reject_pending("bad_row_mode", f"unsupported MR mode {mode}", envelope)]
        if mode != pending.profile[row - 1]:
            self.stats.profileMismatches += 1
            return [self._reject_pending("profile_mismatch", f"MR row {row} mode {mode} != profile", envelope)]
        expectedUnit, expectedScale, expectedFormat = MODE_WIRE[mode]
        if (unit, scale) != (expectedUnit, expectedScale):
            return [
                self._reject_pending(
                    "row_quantity_mismatch",
                    f"{mode} requires unit={expectedUnit},scale={expectedScale}",
                    envelope,
                )
            ]
        rowFormat = fields.get("fmt")
        if rowFormat != expectedFormat:
            return [
                self._reject_pending(
                    "row_format_mismatch",
                    f"{mode} requires fmt={expectedFormat}",
                    envelope,
                )
            ]
        if len(valueTokens) != ROW_CELLS:
            return [self._reject_pending("row_cell_count", f"MR row {row} has {len(valueTokens)} values, expected 8", envelope)]
        if any(mask & ~0xFF for mask in (expectedBits, acquiredBits, validBits, freshBits, errorBits)):
            return [self._reject_pending("row_mask_out_of_range", f"MR row {row} mask exceeds 8 cells", envelope)]
        if acquiredBits & ~expectedBits:
            return [self._reject_pending("acquired_outside_expected", f"MR row {row} acquired is outside expected", envelope)]
        if freshBits & ~acquiredBits:
            return [self._reject_pending("fresh_outside_acquired", f"MR row {row} fresh is outside acquired", envelope)]

        rawValues: list[float] = []
        errorCodes: list[int] = []
        for token in valueTokens:
            strippedToken = token.strip()
            xMatch = X_TOKEN_RE.fullmatch(strippedToken)
            if xMatch is not None:
                rawValues.append(float("nan"))
                errorCodes.append(int(xMatch.group(1), 16))
                continue
            if strippedToken.upper().startswith("X"):
                return [self._reject_pending("bad_x", f"invalid firmware cell error token {strippedToken}", envelope)]
            try:
                rawValues.append(float(int(strippedToken, 10)))
            except ValueError as exc:
                return [self._reject_pending("bad_row_value", f"invalid MR value: {exc}", envelope)]
            errorCodes.append(0)

        rawFixedValues = np.asarray(rawValues, dtype=np.float64)
        errorCodeValues = np.asarray(errorCodes, dtype=np.uint8)
        validMask = _mask_array(validBits)
        freshMask = _mask_array(freshBits)
        errorMask = _mask_array(errorBits)
        expectedMask = _mask_array(expectedBits)
        acquiredMask = _mask_array(acquiredBits)
        tokenInvalid = ~np.isfinite(rawFixedValues)
        if not np.array_equal(validMask, ~tokenInvalid):
            return [self._reject_pending("valid_token_mismatch", f"MR row {row} valid mask disagrees with Xhh", envelope)]
        if not np.array_equal(errorMask, tokenInvalid):
            return [self._reject_pending("error_token_mismatch", f"MR row {row} error mask disagrees with Xhh", envelope)]

        pgaValues: np.ndarray | None = None
        pgaBypassMask: np.ndarray | None = None
        packedPga = fields.get("pga")
        if mode == "CAP":
            if packedPga is not None:
                return [self._reject_pending("unexpected_pga", "CAP MR row must not carry pga", envelope)]
            physicalValues = rawFixedValues * (10.0**scale) - self.circuitOffsetPf
        else:
            if packedPga is not None:
                if len(packedPga) != ROW_CELLS * 2:
                    return [self._reject_pending("bad_pga_length", f"{mode} MR row pga must contain 16 hex characters", envelope)]
                try:
                    pgaValues = np.asarray(
                        [int(packedPga[offset : offset + 2], 16) for offset in range(0, len(packedPga), 2)],
                        dtype=np.uint8,
                    )
                except ValueError as exc:
                    return [self._reject_pending("bad_pga_value", f"invalid MR packed PGA: {exc}", envelope)]
                unsupportedPga = [value for value in pgaValues.tolist() if value not in VALID_PGA_LITERALS]
                if unsupportedPga:
                    return [self._reject_pending("bad_pga_gain", f"unsupported PGA literal 0x{unsupportedPga[0]:02X}", envelope)]
                pgaBypassMask = pgaValues == 0
            physicalValues = rawFixedValues * (10.0**scale)

        try:
            railValid = _optional_binary_bool(fields, "rail")
            railAgeFrames = _optional_nonnegative_decimal(fields, "age")
        except ValueError as exc:
            return [self._reject_pending("bad_row_diagnostic", str(exc), envelope)]
        rowFrame = RowMeasurement(
            row=row,
            mode=mode,  # type: ignore[arg-type]
            unit=unit,
            scale=scale,
            rawFixedValues=rawFixedValues,
            physicalValues=physicalValues,
            validMask=validMask,
            freshMask=freshMask,
            errorMask=errorMask,
            errorCodes=errorCodeValues,
            errorReasons=tuple(
                firmware_cell_error_reason(code) if invalid else ""
                for code, invalid in zip(errorCodeValues, tokenInvalid, strict=True)
            ),
            pgaValues=pgaValues,
            pgaBypassMask=pgaBypassMask,
            reference=fields.get("ref"),
            railValid=railValid,
            railAgeFrames=railAgeFrames,
            rawFields=dict(fields),
            expectedMask=expectedMask,
            acquiredMask=acquiredMask,
        )
        pending.rowLines[row] = _crc_line_bytes(rawLine)
        pending.rowLineOrder.append(_crc_line_bytes(rawLine))
        pending.rowFrames[row] = rowFrame
        return []

    def _parse_trailer(
        self,
        line: str,
        envelope: TransportEnvelope,
    ) -> list[MixedMeasurementFrame | ParserErrorEvent]:
        assert self._pending is not None
        pending = self._pending
        fields = _parse_key_values(line)
        if not all(key in fields for key in ("rgen", "rrid", "pgen", "prid")):
            return [
                self._reject_pending(
                    "mixed_trailer_schema",
                    "mixed K trailer requires rgen,rrid,pgen,prid",
                    envelope,
                )
            ]
        if re.fullmatch(r"[0-9A-Fa-f]{8}", fields.get("crc", "")) is None:
            return [
                self._reject_pending(
                    "bad_crc_width",
                    "canonical mixed K crc must be exactly eight hex characters",
                    envelope,
                )
            ]
        try:
            identities = (
                _decimal(fields, "seq"),
                _decimal(fields, "rgen"),
                _decimal(fields, "rrid"),
                _decimal(fields, "pgen"),
                _decimal(fields, "prid"),
            )
            expectedCrc = int(fields["crc"], 16)
        except (KeyError, ValueError) as exc:
            return [self._reject_pending("bad_k", f"invalid mixed K fields: {exc}", envelope)]
        expectedIdentities = (
            pending.seq,
            pending.rowsGeneration,
            pending.rowsRequestId,
            pending.profileGeneration,
            pending.profileRequestId,
        )
        if identities != expectedIdentities:
            return [self._reject_pending("k_mismatch", "K identities do not match M header", envelope)]
        if len(pending.rowFrames) != pending.rows:
            self.stats.missingRows += 1
            return [self._reject_pending("missing_rows", "not all active MR rows received before K", envelope)]
        composedExpected = 0
        composedAcquired = 0
        for row, rowFrame in pending.rowFrames.items():
            composedExpected |= _mask_bits(rowFrame.expectedMask) << ((row - 1) * ROW_CELLS)
            composedAcquired |= _mask_bits(rowFrame.acquiredMask) << ((row - 1) * ROW_CELLS)
        if composedExpected != pending.expectedBits or composedAcquired != pending.acquiredBits:
            return [
                self._reject_pending(
                    "MIXED_MASK_MISMATCH",
                    "M global expected/acquired does not equal composed MR row masks",
                    envelope,
                )
            ]
        actualCrc = crc32_reflected(pending.payload_bytes())
        if actualCrc != expectedCrc:
            self.stats.crcFailures += 1
            return [self._reject_pending("crc", f"crc 0x{expectedCrc:08X} != computed 0x{actualCrc:08X}", envelope)]

        frame = MixedMeasurementFrame(
            seq=pending.seq,
            timestampUs=pending.timestampUs,
            rows=pending.rows,
            cells=pending.cells,
            rowsGeneration=pending.rowsGeneration,
            rowsRequestId=pending.rowsRequestId,
            profileGeneration=pending.profileGeneration,
            profileRequestId=pending.profileRequestId,
            profile=pending.profile,
            rowFrames=tuple(pending.rowFrames[row] for row in range(1, pending.rows + 1)),
            sourceTransport=envelope.source,
            sessionGeneration=envelope.sessionGeneration,
            receivedTime=envelope.receivedWallTime,
            receivedMonotonicNs=envelope.receivedMonotonicNs,
            deviceId=envelope.deviceId,
            rawHeader=pending.headerLine,
            rawTrailer=line,
            expectedMask=_mask_array_width(pending.expectedBits, pending.cells),
            acquiredMask=_mask_array_width(pending.acquiredBits, pending.cells),
            connectionGeneration=envelope.connectionGeneration,
            bootId=envelope.bootId,
        )
        if self.stats.lastSeq is not None and pending.seq > self.stats.lastSeq + 1:
            self.stats.sequenceGaps += pending.seq - self.stats.lastSeq - 1
        self.stats.lastSeq = pending.seq
        self.stats.frames += 1
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
        return LogRecord(
            timestamp=envelope.receivedWallTime,
            monotonicTime=envelope.receivedMonotonicNs,
            source=envelope.source,
            channel=envelope.channel,
            tag=tag,
            severity="info",
            rawText=line,
            parsedFields=_parse_key_values(line),
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


def _decimal(fields: dict[str, str], name: str, *, allowNegative: bool = False) -> int:
    value = int(fields[name], 10)
    if not allowNegative and value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _string_alias(fields: dict[str, str], canonical: str, legacy: str) -> str:
    if canonical in fields:
        return fields[canonical]
    return fields[legacy]


def _decimal_alias(
    fields: dict[str, str],
    canonical: str,
    legacy: str,
    *,
    allowNegative: bool = False,
) -> int:
    value = int(_string_alias(fields, canonical, legacy), 10)
    if not allowNegative and value < 0:
        raise ValueError(f"{canonical} must be non-negative")
    return value


def _mixed_value_tokens(line: str, fields: dict[str, str]) -> list[str]:
    """Read canonical comma-separated ``D=`` or the saved replay alias.

    Firmware places ``D`` last in each MR record, so the remainder of the
    physical line is the eight-token row payload.  Older host-generated replay
    fixtures used a pipe-separated ``values`` field and remain readable.
    """

    if ",D=" in line:
        return line.split(",D=", maxsplit=1)[1].split(",")
    return fields["values"].split("|")


def _hex(fields: dict[str, str], name: str, *, width: int) -> int:
    text = fields[name].strip()
    if re.fullmatch(rf"[0-9A-Fa-f]{{{width}}}", text) is None:
        raise ValueError(f"{name} must be exactly {width} hex characters")
    return int(text, 16)


def _optional_binary_bool(fields: dict[str, str], name: str) -> bool | None:
    if name not in fields:
        return None
    value = fields[name]
    if value not in {"0", "1"}:
        raise ValueError(f"{name} must be 0 or 1")
    return value == "1"


def _optional_nonnegative_decimal(fields: dict[str, str], name: str) -> int | None:
    if name not in fields:
        return None
    value = int(fields[name], 10)
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _mask_array(mask: int) -> np.ndarray:
    return np.asarray([bool((mask >> cellIndex) & 1) for cellIndex in range(ROW_CELLS)], dtype=bool)


def _mask_array_width(mask: int, cells: int) -> np.ndarray:
    return np.asarray([bool((mask >> cellIndex) & 1) for cellIndex in range(cells)], dtype=bool)


def _mask_bits(mask: np.ndarray) -> int:
    return sum((1 << index) for index, value in enumerate(np.asarray(mask, dtype=bool)) if bool(value))


def _safe_ascii(rawLine: bytes) -> str:
    try:
        return rawLine.decode("ascii", errors="strict").rstrip("\r\n")
    except UnicodeDecodeError:
        return rawLine.hex(" ")


def _crc_line_bytes(rawLine: bytes) -> bytes:
    if rawLine.endswith(b"\r\n"):
        return rawLine[:-2] + b"\n"
    return rawLine


__all__ = ["MODE_WIRE", "MixedAsciiStats", "MixedMeasurementAsciiParser"]
