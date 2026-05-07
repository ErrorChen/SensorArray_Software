from __future__ import annotations

import binascii
import struct
from pathlib import Path

from matrix_log_viewer.binary_frame_parser import FMT, FRAME_TYPE_VOLTAGE_COMPACT, MAGIC, SIZE


def build_frame(seq: int, timestamp_us: int) -> bytes:
    values = [seq * 100 + index for index in range(64)]
    fields = [
        MAGIC,
        1,
        FRAME_TYPE_VOLTAGE_COMPACT,
        seq,
        timestamp_us,
        31_000,
        0,
        0,
        0,
        0,
        (1 << 64) - 1,
        *values,
        15,
        1,
        0,
        0,
    ]
    raw = struct.pack(FMT, *fields)
    fields[-1] = binascii.crc32(raw[: SIZE - 4]) & 0xFFFFFFFF
    return struct.pack(FMT, *fields)


def main() -> int:
    output = Path(__file__).resolve().parent / "sample_logs" / "sample_fast_binary_mixed.bin"
    output.parent.mkdir(parents=True, exist_ok=True)
    chunks = [b"STREAM_INIT,mode=FastSpeed,format=SAC1\n", b"APPMODE,mode=FastSpeed\n"]
    for seq in range(1, 21):
        chunks.append(build_frame(seq, seq * 33_333))
        if seq % 5 == 0:
            chunks.append(
                f"STAT,fps=30.0,pps=1920,scanAvgUs=31000,scanMaxUs=33000,drop=0,decimated=0,qFull=0,drdyTimeout=0,spiFail=0,adsDr=15,adsSps=30000,outputDiv=1,status=0x00000000,code=0x0000\n".encode(
                    "ascii"
                )
            )
    output.write_bytes(b"".join(chunks))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
