from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

import numpy as np

TransportSource = Literal["serial", "ble", "wifi", "replay", "host"]
TransportChannel = Literal["data", "log", "ctrl", "host"]


class DisplayMode(str, Enum):
    ABSOLUTE_C = "absolute_c"
    DELTA_PERCENT = "delta_percent"


class MeasurementDomain(str, Enum):
    AUTO = "auto"
    CAPACITANCE = "capacitance"
    VOLTAGE = "voltage"
    RESISTANCE = "resistance"


@dataclass(frozen=True)
class TransportEnvelope:
    source: TransportSource
    channel: TransportChannel
    deviceId: str
    sessionGeneration: int
    receivedMonotonicNs: int
    receivedWallTime: float
    rawPayload: bytes
    remoteAddress: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapacitanceFrame:
    seq: int
    timestampUs: int
    rows: int
    cells: int
    generation: int
    requestId: int
    rowFreshMask: int
    primaryFreshMask: int
    secondaryFreshMask: int
    badStaleCount: int
    badMixedCount: int
    badInvalidCount: int
    rawFixedValues: np.ndarray
    rawPfValues: np.ndarray
    correctedPfValues: np.ndarray
    validMask: np.ndarray
    sourceTransport: str
    sessionGeneration: int
    receivedTime: float
    receivedMonotonicNs: int
    deviceId: str = ""
    rawHeader: str = ""
    rawTrailer: str = ""


@dataclass(frozen=True)
class VoltageFrame:
    seq: int
    timestampUs: int
    durationUs: int
    valuesUv: np.ndarray
    validMask: np.ndarray
    sourceTransport: str
    sessionGeneration: int
    receivedTime: float
    frameType: str = "FAST_BINARY"
    droppedFrames: int = 0
    outputDecimatedFrames: int = 0
    crc32Frame: int | None = None
    crc32Computed: int | None = None


@dataclass(frozen=True)
class ResistanceFrame:
    seq: int
    timestampUs: int
    valuesOhm: np.ndarray
    validMask: np.ndarray
    sourceTransport: str
    sessionGeneration: int
    receivedTime: float
    formula: str = "log_value"


@dataclass(frozen=True)
class BatteryTelemetry:
    batteryMv: int | None
    batteryState: str
    reason: str
    fresh: bool | None
    ageFrames: int | None
    railUv: int | None
    railValid: bool | None
    railState: str | None
    railErrorUv: int | None
    ain8DiffUv: int | None
    aincomGndUv: int | None
    ain8GndUv: int | None
    zeroMeanUv: int | None
    zeroStdUv: int | None
    statusByte: int | None
    drdyGenerationDelta: int | None
    adsChipId: int | None
    receivedTime: float
    rawFields: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LogRecord:
    timestamp: float
    monotonicTime: int
    source: str
    channel: str
    tag: str
    severity: str
    rawText: str
    parsedFields: dict[str, str]
    recognised: bool
    sessionGeneration: int
    deviceTimestamp: str | None = None


@dataclass(frozen=True)
class CommandAccepted:
    commandId: int
    oldRows: int | None
    requestedRows: int | None
    generation: int | None
    sessionGeneration: int
    rawText: str


@dataclass(frozen=True)
class CommandApplied:
    commandId: int
    seq: int | None
    oldRows: int | None
    newRows: int | None
    generation: int | None
    sessionGeneration: int
    rawText: str


@dataclass(frozen=True)
class TransportStateEvent:
    source: str
    state: str
    sessionGeneration: int
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParserErrorEvent:
    source: str
    channel: str
    reason: str
    detail: str
    sessionGeneration: int
    rawText: str = ""


@dataclass(frozen=True)
class DiagnosticSummary:
    transportBytes: int = 0
    transportPackets: int = 0
    parserFrames: int = 0
    parserRejects: int = 0
    crcFailures: int = 0
    sequenceGaps: int = 0
    fragmentDrops: int = 0
    hostQueueDrops: int = 0
    historyOverwrites: int = 0
    renderSkipped: int = 0
    visualFps: float = 0.0
    parserFps: float = 0.0
    storedFps: float = 0.0


DomainEvent = (
    CapacitanceFrame
    | VoltageFrame
    | ResistanceFrame
    | BatteryTelemetry
    | LogRecord
    | CommandAccepted
    | CommandApplied
    | TransportStateEvent
    | ParserErrorEvent
    | DiagnosticSummary
)
