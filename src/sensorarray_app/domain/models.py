from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

import numpy as np

TransportSource = Literal["serial", "ble", "wifi", "replay", "host"]
TransportChannel = Literal["data", "log", "ctrl", "host"]


class DisplayMode(str, Enum):
    ABSOLUTE_C = "absolute_pf"
    DELTA_PERCENT = "delta_percent"


class MeasurementDomain(str, Enum):
    AUTO = "auto"
    CAPACITANCE = "capacitance"
    VOLTAGE = "voltage"
    RESISTANCE = "resistance"


@dataclass(frozen=True)
class TransportEnvelope:
    source: TransportSource
    channel: str
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
class MeasurementFrame:
    """One CRC-verified current-firmware VOLT or RES text frame.

    ``rawFixedValues`` retains the firmware's integer fixed-point quantity and
    uses NaN only where the wire carried an ``Xhh`` token. ``physicalValues``
    is expressed in the SI unit named by ``unit`` (volts for VOLT and ohms for
    RES). The three masks are deliberately independent: a value can be valid
    but stale, and consumers must not infer freshness from validity.
    """

    mode: str
    seq: int
    timestampUs: int
    durationUs: int
    rows: int
    cells: int
    generation: int
    requestId: int
    unit: str
    scale: int
    format: str
    rawFixedValues: np.ndarray
    physicalValues: np.ndarray
    validMask: np.ndarray
    freshMask: np.ndarray
    errorMask: np.ndarray
    errorCodes: np.ndarray
    errorReasons: tuple[str, ...]
    pgaValues: np.ndarray
    pgaBypassMask: np.ndarray
    reference: str
    railValid: bool
    railAgeFrames: int
    avddUv: int
    avssUv: int
    matrixReferenceUv: int
    referenceResistorOhms: int
    transitionDurationUs: int
    gainChangeCount: int
    overrangeCount: int
    autorangeAttemptCount: int
    autorangeFallbackCount: int
    recoveredRetryCount: int
    drdyTimeoutCount: int
    staleCount: int
    spiErrorCount: int
    badCellCount: int
    sourceTransport: str
    sessionGeneration: int
    receivedTime: float
    receivedMonotonicNs: int
    deviceId: str = ""
    rawHeader: str = ""
    rawTrailer: str = ""
    rawFields: dict[str, str] = field(default_factory=dict)


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
    valid: bool | None = None
    ageMs: int | None = None
    periodMs: int | None = None
    due: bool | None = None
    runCount: int | None = None
    validRunCount: int | None = None
    invalidRunCount: int | None = None
    skipCount: int | None = None
    deferCount: int | None = None
    boundaryCount: int | None = None
    restoreFailureCount: int | None = None
    retryCount: int | None = None
    retryLimit: int | None = None
    retryLastCount: int | None = None
    retryTotalCount: int | None = None
    unstableCount: int | None = None
    timeoutCount: int | None = None
    spreadRaw: int | None = None
    spreadMaximumRaw: int | None = None
    rawAdc: int | None = None
    batteryDividerNumerator: int | None = None
    batteryDividerDenominator: int | None = None
    vbiasEnabled: bool | None = None
    sampleCount: int | None = None
    sampleAverageUs: int | None = None
    sampleMaximumUs: int | None = None
    restoreResult: str | None = None


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
class CommandTransactionEvent:
    """Protocol-neutral accepted/applied/failed command transaction update."""

    commandType: str
    phase: str
    requestId: int | None = None
    state: str | None = None
    oldValue: Any | None = None
    requestedValue: Any | None = None
    appliedValue: Any | None = None
    generation: int | None = None
    frameSeq: int | None = None
    error: str | None = None
    rawFields: dict[str, str] = field(default_factory=dict)
    sessionGeneration: int = 0
    rawText: str = ""


@dataclass(frozen=True)
class AdsDiagnosticEvent:
    """Structured ADS identity or active-check diagnostic event.

    The chip remains a string so ``chip=unknown,valid=0`` cannot accidentally
    become an ADS1262 identity through a numeric default.
    """

    eventType: str
    state: str
    requestId: int | None = None
    chip: str = "unknown"
    identityValid: bool | None = None
    ok: bool | None = None
    requestedSamples: int | None = None
    freshSamples: int | None = None
    changedSamples: int | None = None
    periodMinUs: int | None = None
    periodAverageUs: int | None = None
    periodMaxUs: int | None = None
    spiErrors: int | None = None
    drdyTimeouts: int | None = None
    staleSamples: int | None = None
    statusErrors: int | None = None
    resetCount: int | None = None
    restoreResult: str | None = None
    durationUs: int | None = None
    rawFields: dict[str, str] = field(default_factory=dict)
    sessionGeneration: int = 0
    rawText: str = ""


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
    | MeasurementFrame
    | VoltageFrame
    | ResistanceFrame
    | BatteryTelemetry
    | LogRecord
    | CommandAccepted
    | CommandApplied
    | CommandTransactionEvent
    | AdsDiagnosticEvent
    | TransportStateEvent
    | ParserErrorEvent
    | DiagnosticSummary
)
