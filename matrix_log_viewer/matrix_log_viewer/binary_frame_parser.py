from __future__ import annotations

import binascii
import struct

from .config import CELL_NAMES
from .protocol_types import MatrixFrame

MAGIC = 0x31434153
MAGIC_BYTES = b"SAC1"
VERSION = 1
FRAME_TYPE_VOLTAGE_COMPACT = 0x1261
FRAME_TYPE_NAME = "FAST_BINARY"
# FastSpeed compact voltage frame. Keep this string in sync with the firmware
# protocol note in README; the GUI/parser use SIZE to resynchronise the stream.
FMT = "<IHHIQIIIIIQ64iBBHI"
SIZE = struct.calcsize(FMT)
if SIZE != 312:  # pragma: no cover - import-time protocol guard.
    raise RuntimeError(f"FastSpeed binary frame format size mismatch: {SIZE} != 312")


class BinaryFrameParseError(ValueError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class SensorArrayBinaryFrameParser:
    """Parse FastSpeed packed `sensorarrayVoltageCompactFrame_t` frames."""

    def parseFrame(self, rawFrame: bytes) -> MatrixFrame:
        if len(rawFrame) < SIZE:
            raise BinaryFrameParseError("short", f"short binary frame: {len(rawFrame)} < {SIZE}")

        fields = struct.unpack(FMT, rawFrame[:SIZE])
        magic = fields[0]
        version = fields[1]
        frame_type = fields[2]
        crc_expected = fields[-1]

        if magic != MAGIC:
            raise BinaryFrameParseError("magic", f"bad binary magic: 0x{magic:08X}")
        if version != VERSION:
            raise BinaryFrameParseError("version", f"unsupported binary version: {version}")
        if frame_type != FRAME_TYPE_VOLTAGE_COMPACT:
            raise BinaryFrameParseError("frameType", f"unsupported binary frameType: 0x{frame_type:04X}")

        # FastSpeed writes IEEE CRC32 over every packed byte before the crc32 field.
        crc_actual = binascii.crc32(rawFrame[: SIZE - 4]) & 0xFFFFFFFF
        if crc_actual != crc_expected:
            raise BinaryFrameParseError(
                "crc",
                f"binary crc mismatch: got 0x{crc_expected:08X}, expected 0x{crc_actual:08X}",
            )

        sequence = int(fields[3])
        timestamp_us = int(fields[4])
        duration_us = int(fields[5])
        status_flags = int(fields[6])
        first_status_code = int(fields[7])
        last_status_code = int(fields[8])
        dropped_frames = int(fields[9])
        valid_mask = int(fields[10])
        microvolts = fields[11:75]
        ads_dr = int(fields[75])
        output_divider = int(fields[76])
        output_decimated_frames = 0

        values = {
            cell_name: float(value)
            for cell_name, value in zip(CELL_NAMES, microvolts)
        }

        return MatrixFrame(
            frameType=FRAME_TYPE_NAME,
            seq=sequence,
            timestampUs=timestamp_us,
            durationUs=duration_us,
            unit="uV",
            values=values,
            validMask=valid_mask,
            statusFlags=status_flags,
            firstStatusCode=first_status_code,
            lastStatusCode=last_status_code,
            droppedFrames=dropped_frames,
            outputDecimatedFrames=output_decimated_frames,
            adsDr=ads_dr,
            outputDivider=output_divider,
            rawBytes=rawFrame[:SIZE],
        )
