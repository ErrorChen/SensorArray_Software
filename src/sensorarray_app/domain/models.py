from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

import numpy as np

TransportSource = Literal["serial", "ble", "wifi", "replay", "host"]
TransportChannel = Literal["data", "log", "ctrl", "host"]
RowMeasurementMode = Literal["CAP", "VOLT", "RES"]


def normalize_row_modes(modes: Any) -> tuple[RowMeasurementMode, ...]:
    """Return one validated, immutable eight-row measurement profile.

    The firmware stores all eight row modes even when the current ``ROWS``
    geometry exposes fewer rows.  Normalising at the domain boundary prevents
    a partial UI draft or malformed log record from becoming applied state.
    """

    if isinstance(modes, str):
        tokens = tuple({"C": "CAP", "V": "VOLT", "R": "RES"}.get(char, char) for char in modes.strip().upper())
    else:
        try:
            tokens = tuple(str(mode).strip().upper() for mode in modes)
        except TypeError as exc:
            raise ValueError("row mode profile must be an iterable of eight modes") from exc
    if len(tokens) != 8:
        raise ValueError(f"row mode profile must contain exactly 8 modes, got {len(tokens)}")
    invalid = [mode for mode in tokens if mode not in {"CAP", "VOLT", "RES"}]
    if invalid:
        raise ValueError(f"unsupported row measurement mode: {invalid[0]}")
    return tokens  # type: ignore[return-value]


@dataclass(frozen=True)
class RowModeProfile:
    """Typed applied/pending state for the firmware's atomic eight-row profile."""

    modes: tuple[RowMeasurementMode, ...]
    generation: int = 0
    requestId: int | None = None
    pendingModes: tuple[RowMeasurementMode, ...] | None = None
    state: str = "applied"
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "modes", normalize_row_modes(self.modes))
        if self.pendingModes is not None:
            object.__setattr__(self, "pendingModes", normalize_row_modes(self.pendingModes))
        if self.generation < 0:
            raise ValueError("row mode generation must be non-negative")
        if self.requestId is not None and self.requestId < 0:
            raise ValueError("row mode request ID must be non-negative")


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
class RowMeasurement:
    """One physical eight-cell row in a heterogeneous measurement frame.

    ``physicalValues`` is expressed in ``unit``.  CAP rows deliberately use
    the same circuit-offset-corrected pF semantics as ``CapacitanceFrame``;
    ``rawFixedValues`` retains the unmodified pf6 integers from the wire.
    Invalid ``Xhh`` cells remain NaN and retain their firmware error code.
    """

    row: int
    mode: RowMeasurementMode
    unit: str
    scale: int
    rawFixedValues: np.ndarray
    physicalValues: np.ndarray
    validMask: np.ndarray
    freshMask: np.ndarray
    errorMask: np.ndarray
    errorCodes: np.ndarray
    errorReasons: tuple[str, ...]
    pgaValues: np.ndarray | None = None
    pgaBypassMask: np.ndarray | None = None
    reference: str | None = None
    railValid: bool | None = None
    railAgeFrames: int | None = None
    rawFields: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (1 <= int(self.row) <= 8):
            raise ValueError(f"mixed row identity out of range: {self.row}")
        if self.mode not in {"CAP", "VOLT", "RES"}:
            raise ValueError(f"unsupported row measurement mode: {self.mode}")
        for name in ("rawFixedValues", "physicalValues", "validMask", "freshMask", "errorMask", "errorCodes"):
            values = np.asarray(getattr(self, name))
            if values.shape != (8,):
                raise ValueError(f"{name} must contain exactly 8 cells")
        if len(self.errorReasons) != 8:
            raise ValueError("errorReasons must contain exactly 8 cells")
        for name in ("pgaValues", "pgaBypassMask"):
            values = getattr(self, name)
            if values is not None and np.asarray(values).shape != (8,):
                raise ValueError(f"{name} must contain exactly 8 cells")
        expectedUnitScale = {
            "CAP": ("pF", -6),
            "VOLT": ("V", -6),
            "RES": ("ohm", -3),
        }[self.mode]
        if (self.unit, self.scale) != expectedUnitScale:
            raise ValueError(
                f"{self.mode} row requires unit={expectedUnitScale[0]},scale={expectedUnitScale[1]}"
            )
        rawFinite = np.isfinite(np.asarray(self.rawFixedValues, dtype=np.float64))
        validMask = np.asarray(self.validMask, dtype=bool)
        errorMask = np.asarray(self.errorMask, dtype=bool)
        errorCodes = np.asarray(self.errorCodes, dtype=np.uint8)
        if not np.array_equal(validMask, rawFinite):
            raise ValueError("validMask must match finite raw fixed values")
        if not np.array_equal(errorMask, ~rawFinite):
            raise ValueError("errorMask must match invalid raw fixed values")
        if np.any(errorCodes[rawFinite] != 0):
            raise ValueError("valid cells cannot carry a firmware error code")
        if self.mode == "CAP" and (self.pgaValues is not None or self.pgaBypassMask is not None):
            raise ValueError("CAP rows must not carry PGA metadata")
        if self.mode in {"VOLT", "RES"}:
            if (self.pgaValues is None) != (self.pgaBypassMask is None):
                raise ValueError("pgaValues and pgaBypassMask must be supplied together")
            if self.pgaValues is not None and not np.array_equal(
                np.asarray(self.pgaBypassMask, dtype=bool),
                np.asarray(self.pgaValues, dtype=np.uint8) == 0,
            ):
                raise ValueError("pgaBypassMask must match zero-valued PGA literals")


@dataclass(frozen=True)
class MixedMeasurementFrame:
    """One CRC-verified atomic mixed-row frame.

    A frame is created only after the protocol layer has received one unique
    ``MR`` record for every active physical row and has verified profile
    identity plus the shared ``K`` CRC.  The backing geometry remains Nx8;
    row-specific units never collapse into a synthetic ``unit='mixed'``.
    """

    seq: int
    timestampUs: int
    rows: int
    cells: int
    rowsGeneration: int
    rowsRequestId: int
    profileGeneration: int
    profileRequestId: int
    profile: tuple[RowMeasurementMode, ...]
    rowFrames: tuple[RowMeasurement, ...]
    sourceTransport: str
    sessionGeneration: int
    receivedTime: float
    receivedMonotonicNs: int
    deviceId: str = ""
    rawHeader: str = ""
    rawTrailer: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile", normalize_row_modes(self.profile))
        object.__setattr__(self, "rowFrames", tuple(self.rowFrames))
        if not (1 <= int(self.rows) <= 8):
            raise ValueError(f"mixed frame rows out of range: {self.rows}")
        if self.cells != self.rows * 8:
            raise ValueError(f"mixed frame cells {self.cells} != rows*8 {self.rows * 8}")
        if min(self.rowsGeneration, self.rowsRequestId, self.profileGeneration, self.profileRequestId) < 0:
            raise ValueError("mixed frame generation and request IDs must be non-negative")
        rowIdentities = tuple(rowFrame.row for rowFrame in self.rowFrames)
        if len(self.rowFrames) != self.rows or set(rowIdentities) != set(range(1, self.rows + 1)):
            raise ValueError("mixed frame must contain exactly one record for every active row")
        # Firmware 331c445 chooses the mixed grammar from the complete saved
        # eight-row profile.  Inactive configured rows may therefore make a
        # ROWS=1..N frame mixed even when its active prefix is homogeneous.
        if len(set(self.profile)) < 2:
            raise ValueError("mixed frame requires a heterogeneous saved row profile")
        for rowFrame in self.rowFrames:
            if rowFrame.mode != self.profile[rowFrame.row - 1]:
                raise ValueError(f"mixed row {rowFrame.row} mode does not match profile")


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
    lastGoodBatteryMv: int | None = None
    lastGoodValid: bool | None = None
    lastGoodFresh: bool | None = None
    lastGoodAgeMs: int | None = None
    lastGoodFrame: int | None = None
    lastGoodSource: str | None = None
    lastGoodReason: str | None = None


@dataclass(frozen=True)
class RailTelemetry:
    """Typed read-only ADS analogue rail-span telemetry."""

    railSpanUv: int | None
    valid: bool | None
    fresh: bool | None
    age: int | None
    ageMs: int | None
    source: str
    reason: str
    timestamp: float
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
    | MixedMeasurementFrame
    | VoltageFrame
    | ResistanceFrame
    | BatteryTelemetry
    | RailTelemetry
    | LogRecord
    | CommandAccepted
    | CommandApplied
    | CommandTransactionEvent
    | AdsDiagnosticEvent
    | TransportStateEvent
    | ParserErrorEvent
    | DiagnosticSummary
)
