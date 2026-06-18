from __future__ import annotations

import binascii
import struct

import numpy as np

from sensorarray_app.domain.models import ParserErrorEvent, TransportEnvelope, VoltageFrame

MAGIC_U32 = 0x31434153
MAGIC_BYTES = b"SAC1"
VERSION = 1
FRAME_TYPE_VOLTAGE_COMPACT = 0x1261
FRAME_TYPE_NAME = "FAST_BINARY"
FMT = "<IHHIQIIIIHHQ64iBBHI"
FRAME_SIZE = struct.calcsize(FMT)


class LegacyFastBinaryVoltageProtocol:
    name = "LegacyFastBinaryVoltageProtocol"

    def __init__(self):
        self._buffer = bytearray()
        self.frames = 0
        self.crcFailures = 0
        self.resyncs = 0
        self.droppedFrames = 0

    def feed(self, envelope: TransportEnvelope) -> list[VoltageFrame | ParserErrorEvent]:
        events: list[VoltageFrame | ParserErrorEvent] = []
        self._buffer.extend(envelope.rawPayload)
        while True:
            index = self._buffer.find(MAGIC_BYTES)
            if index < 0:
                keep = len(MAGIC_BYTES) - 1
                if len(self._buffer) > keep:
                    self.droppedFrames += len(self._buffer) - keep
                    del self._buffer[:-keep]
                break
            if index > 0:
                self.resyncs += 1
                del self._buffer[:index]
            if len(self._buffer) < FRAME_SIZE:
                break
            raw = bytes(self._buffer[:FRAME_SIZE])
            parsed = self._parse_one(raw, envelope)
            if isinstance(parsed, ParserErrorEvent):
                events.append(parsed)
                del self._buffer[:1]
            else:
                events.append(parsed)
                del self._buffer[:FRAME_SIZE]
        return events

    def _parse_one(self, raw: bytes, envelope: TransportEnvelope) -> VoltageFrame | ParserErrorEvent:
        try:
            fields = struct.unpack(FMT, raw)
        except struct.error as exc:
            return self._error(envelope, "struct", str(exc))
        magic, version, frame_type = fields[0], fields[1], fields[2]
        crc_expected = int(fields[-1])
        crc_actual = binascii.crc32(raw[: FRAME_SIZE - 4]) & 0xFFFFFFFF
        if magic != MAGIC_U32:
            return self._error(envelope, "magic", f"bad magic 0x{magic:08X}")
        if version != VERSION or frame_type != FRAME_TYPE_VOLTAGE_COMPACT:
            return self._error(envelope, "version", "unsupported legacy binary frame")
        if crc_actual != crc_expected:
            self.crcFailures += 1
            return self._error(envelope, "crc", f"crc 0x{crc_expected:08X} != 0x{crc_actual:08X}")
        self.frames += 1
        valid_mask_int = int(fields[11])
        valid = np.array([bool((valid_mask_int >> index) & 1) for index in range(64)], dtype=bool)
        values = np.asarray(fields[12:76], dtype=np.float64)
        values[~valid] = np.nan
        return VoltageFrame(
            seq=int(fields[3]),
            timestampUs=int(fields[4]),
            durationUs=int(fields[5]),
            valuesUv=values,
            validMask=valid,
            sourceTransport=envelope.source,
            sessionGeneration=envelope.sessionGeneration,
            receivedTime=envelope.receivedWallTime,
            droppedFrames=int(fields[9]),
            outputDecimatedFrames=int(fields[10]),
            crc32Frame=crc_expected,
            crc32Computed=crc_actual,
        )

    def _error(self, envelope: TransportEnvelope, reason: str, detail: str) -> ParserErrorEvent:
        return ParserErrorEvent(
            source=envelope.source,
            channel=envelope.channel,
            reason=f"legacy_binary_{reason}",
            detail=detail,
            sessionGeneration=envelope.sessionGeneration,
        )
