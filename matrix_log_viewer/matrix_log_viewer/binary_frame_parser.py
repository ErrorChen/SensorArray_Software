from __future__ import annotations

import binascii
import struct

from .config import CELL_NAMES
from .protocol_types import MatrixFrame

MAGIC_U32 = 0x31434153
MAGIC = MAGIC_U32  # Compatibility alias for older tests/scripts.
MAGIC_BYTES = b"SAC1"
VERSION = 1
FRAME_TYPE_VOLTAGE_COMPACT = 0x1261
FRAME_TYPE_NAME = "FAST_BINARY"
# UpperSpeed compact voltage frame from SensorArray@4afe843:
# sensorarrayVoltageCompactFrame_t in main/sensorarrayVoltageScan.h.
FMT = "<IHHIQIIIIHHQ64iBBHI"
SIZE = struct.calcsize(FMT)
if SIZE != 312:  # pragma: no cover - import-time protocol guard.
    raise RuntimeError(f"UpperSpeed binary frame format size mismatch: {SIZE} != 312")
FRAME_SIZE = SIZE


class BinaryFrameParseError(ValueError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class SensorArrayBinaryFrameParser:
    """Parse UpperSpeed packed `sensorarrayVoltageCompactFrame_t` frames."""

    def parseFrame(self, rawFrame: bytes) -> MatrixFrame:
        if len(rawFrame) < SIZE:
            raise BinaryFrameParseError("short", f"short binary frame: {len(rawFrame)} < {SIZE}")

        try:
            fields = struct.unpack(FMT, rawFrame[:SIZE])
        except struct.error as exc:
            raise BinaryFrameParseError("struct", f"binary struct unpack failed: {exc}") from exc

        magic = fields[0]
        version = fields[1]
        frame_type = fields[2]
        crc_expected = fields[-1]

        if magic != MAGIC_U32:
            raise BinaryFrameParseError("magic", f"bad binary magic: 0x{magic:08X}")
        if version != VERSION:
            raise BinaryFrameParseError("version", f"unsupported binary version: {version}")
        if frame_type != FRAME_TYPE_VOLTAGE_COMPACT:
            raise BinaryFrameParseError("frameType", f"unsupported binary frameType: 0x{frame_type:04X}")

        # UpperSpeed writes IEEE CRC32 over every packed byte before the crc32 field.
        crc_actual = binascii.crc32(rawFrame[: SIZE - 4]) & 0xFFFFFFFF
        if crc_actual != crc_expected:
            raise BinaryFrameParseError(
                "crc",
                f"binary crc mismatch: frame=0x{crc_expected:08X} computed=0x{crc_actual:08X}",
            )

        sequence = int(fields[3])
        timestamp_us = int(fields[4])
        duration_us = int(fields[5])
        status_flags = int(fields[6])
        first_status_code = int(fields[7])
        last_status_code = int(fields[8])
        dropped_frames_saturated = int(fields[9])
        output_decimated_frames_saturated = int(fields[10])
        valid_mask = int(fields[11])
        microvolts = fields[12:76]
        ads_dr = int(fields[76])
        output_divider = int(fields[77])

        if len(microvolts) != len(CELL_NAMES):
            raise BinaryFrameParseError("value", f"binary value count mismatch: {len(microvolts)} != {len(CELL_NAMES)}")

        values = {cell_name: float(value) for cell_name, value in zip(CELL_NAMES, microvolts)}

        return MatrixFrame(
            frameType=FRAME_TYPE_NAME,
            frameTypeName=FRAME_TYPE_NAME,
            seq=sequence,
            timestampUs=timestamp_us,
            durationUs=duration_us,
            unit="uV",
            values=values,
            validMask=valid_mask,
            statusFlags=status_flags,
            firstStatusCode=first_status_code,
            lastStatusCode=last_status_code,
            droppedFrames=dropped_frames_saturated,
            outputDecimatedFrames=output_decimated_frames_saturated,
            droppedFramesSaturated=dropped_frames_saturated,
            outputDecimatedFramesSaturated=output_decimated_frames_saturated,
            adsDr=ads_dr,
            outputDivider=output_divider,
            crc32Frame=int(crc_expected),
            crc32Computed=int(crc_actual),
            parserFrameSize=SIZE,
            rawBytes=rawFrame[:SIZE],
        )
