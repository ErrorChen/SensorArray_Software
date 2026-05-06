from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MatrixFrame:
    frameType: str
    seq: int
    timestampUs: int
    durationUs: int
    unit: str
    values: dict[str, float]
    validMask: int | None = None
    statusFlags: int | None = None
    firstStatusCode: int | None = None
    lastStatusCode: int | None = None
    droppedFrames: int | None = None
    outputDecimatedFrames: int | None = None
    adsDr: int | None = None
    outputDivider: int | None = None
    rawLine: str | None = None
    rawBytes: bytes | None = None


@dataclass(frozen=True)
class DeviceStatus:
    statusType: str
    fields: dict[str, str]
    rawLine: str


@dataclass(frozen=True)
class DeviceEvent:
    eventType: str
    code: int | None
    name: str | None
    fields: dict[str, str]
    rawLine: str


@dataclass(frozen=True)
class ParseResult:
    frame: MatrixFrame | None = None
    status: DeviceStatus | None = None
    event: DeviceEvent | None = None

    def hasData(self) -> bool:
        return self.frame is not None or self.status is not None or self.event is not None


def compactDataclassDict(value: Any) -> dict:
    """Return a small dict suitable for status panels without importing dataclasses everywhere."""
    if value is None:
        return {}
    return dict(getattr(value, "__dict__", {}))
