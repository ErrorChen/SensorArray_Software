from __future__ import annotations

STATUS_CODE_NAMES = {
    0x0000: "OK",
    0x1001: "ADS_SPI_FAIL",
    0x1002: "ADS_DRDY_TIMEOUT",
    0x1003: "ADS_CRC_FAIL",
    0x1004: "ADS_REG_VERIFY_FAIL",
    0x1005: "ADS_REF_POLICY_MISMATCH",
    0x1006: "ADS_GAIN_CHANGE_FAIL",
    0x1007: "ADS_DMA_FALLBACK",
    0x1008: "ADS_INPMUX_WRITE_FAIL",
    0x1009: "ADS_DIRECT_READ_FAIL",
    0x100A: "ADS_STATUS_BYTE_BAD",
    0x2001: "TMUX_ROUTE_FAIL",
    0x2002: "TMUX_SW_POLICY_MISMATCH",
    0x2003: "TMUX_SOURCE_FAIL",
    0x3001: "STREAM_QUEUE_FULL",
    0x3002: "STREAM_FRAME_DROPPED",
    0x3003: "USB_STDOUT_BLOCKED",
    0x3004: "USB_STDOUT_WRITE_FAIL",
    0x3005: "USB_STDOUT_SHORT_WRITE",
    0x4001: "SPI_BUS_ACQUIRE_FAIL",
    0x4002: "SPI_BUS_RELEASE_FAIL",
    0x5001: "MODE_POLICY_MISMATCH",
    0x6001: "RATE_OUTPUT_DECIMATED",
    0x6002: "RATE_SCAN_THROTTLED",
    0x6003: "RATE_ADS_DR_REDUCED",
    0x6004: "RATE_MUX_SETTLE_INCREASED",
    0x6005: "RATE_VERIFIED_MUX_FORCED",
    0x6006: "RATE_SAFE_PROFILE_ENTERED",
    0x6007: "RATE_FATAL_STOP",
    0x7FFF: "INTERNAL_ASSERT_FAIL",
}


def statusCodeName(code: int | None) -> str:
    if code is None:
        return "-"
    return STATUS_CODE_NAMES.get(int(code), f"UNKNOWN_0x{int(code) & 0xFFFF:04X}")


def parseStatusCode(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text, 0)
    except ValueError:
        return None
