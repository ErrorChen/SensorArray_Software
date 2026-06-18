from __future__ import annotations


def status_code_name(code: int | None) -> str:
    if code in (None, 0):
        return "-"
    return f"0x{int(code):04X}"
