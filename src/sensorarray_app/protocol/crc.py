from __future__ import annotations

import binascii


def crc32_reflected(data: bytes) -> int:
    return binascii.crc32(data) & 0xFFFFFFFF
