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
    droppedFramesSaturated: int | None = None
    outputDecimatedFramesSaturated: int | None = None
    adsDr: int | None = None
    outputDivider: int | None = None
    frameTypeName: str | None = None
    crc32Frame: int | None = None
    crc32Computed: int | None = None
    parserFrameSize: int | None = None
    rawLine: str | None = None
    rawBytes: bytes | None = None


@dataclass(frozen=True)
class DeviceStatus:
    statusType: str
    fields: dict[str, str]
    rawLine: str
    fastBinaryStartSeen: bool = False
    fastBinaryStartMeta: dict[str, int | str | bool] | None = None
    fastBinaryDiagLatest: dict[str, int | str | bool] | None = None
    pureBinaryMode: bool = False
    startupDiagWindowSeen: bool = False
    asciiAfterFastBinaryStart: bool = False
    protocolPollutionCount: int = 0
    droppedBeforeFirstByte: int | None = None
    partialAfterFirstByte: int | None = None
    fullFrameWriteCount: int | None = None
    fullFrameWriteFailCount: int | None = None
    dropPolicy: str | None = None
    usbExactBinaryWrite: bool | None = None
    fastBinaryStartupDiagMs: int | None = None
    latestScanFps: float | None = None
    latestOutFps: float | None = None
    latestOutputDiv: int | None = None
    latestQUsed: int | None = None
    latestQFull: int | None = None
    latestDrop: int | None = None
    latestDecimated: int | None = None


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
