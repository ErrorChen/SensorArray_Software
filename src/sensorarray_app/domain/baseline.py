from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from sensorarray_app.constants import BASELINE_DURATION_SECONDS, BASELINE_EPSILON_PF, BASELINE_MIN_SAMPLES
from sensorarray_app.domain.models import CapacitanceFrame


@dataclass
class BaselineResult:
    valuesPf: np.ndarray
    validMask: np.ndarray
    sampleCounts: np.ndarray
    invalidReasons: list[str]
    frameCount: int
    rejectedFrameCount: int
    startMonotonicNs: int
    endMonotonicNs: int


@dataclass
class BaselineSession:
    sessionGeneration: int
    transport: str
    deviceId: str
    activeRows: int
    firmwareGeneration: int
    requestId: int
    measurementDomain: str
    circuitOffsetPf: float
    startMonotonicNs: int
    durationSeconds: float = BASELINE_DURATION_SECONDS
    minSamples: int = BASELINE_MIN_SAMPLES
    epsilonPf: float = BASELINE_EPSILON_PF
    frameCount: int = 0
    rejectedFrameCount: int = 0
    _samples: list[list[float]] = field(default_factory=lambda: [[] for _ in range(64)])
    cancelled: bool = False

    @property
    def endMonotonicNs(self) -> int:
        return self.startMonotonicNs + int(self.durationSeconds * 1_000_000_000)

    def progress(self, now_monotonic_ns: int) -> float:
        span = max(1, self.endMonotonicNs - self.startMonotonicNs)
        return max(0.0, min(1.0, (now_monotonic_ns - self.startMonotonicNs) / span))

    def add_frame(self, frame: CapacitanceFrame) -> bool:
        if self.cancelled:
            return False
        if frame.receivedMonotonicNs < self.startMonotonicNs:
            return False
        if frame.receivedMonotonicNs >= self.endMonotonicNs:
            return False
        if not self._frame_matches(frame):
            self.rejectedFrameCount += 1
            return False
        self.frameCount += 1
        values = frame.correctedPfValues
        valid = frame.validMask
        for index in range(min(len(values), self.activeRows * 8)):
            value = float(values[index])
            if bool(valid[index]) and np.isfinite(value):
                self._samples[index].append(value)
        return True

    def complete(self) -> BaselineResult:
        values = np.full(64, np.nan, dtype=np.float64)
        valid = np.zeros(64, dtype=bool)
        counts = np.zeros(64, dtype=np.int32)
        reasons = ["inactive" for _ in range(64)]
        for index in range(self.activeRows * 8):
            samples = np.asarray(self._samples[index], dtype=np.float64)
            counts[index] = len(samples)
            if samples.size < self.minSamples:
                reasons[index] = "sample_count"
                continue
            median = float(np.nanmedian(samples))
            if not np.isfinite(median):
                reasons[index] = "nan"
                continue
            if abs(median) < self.epsilonPf:
                reasons[index] = "denominator_epsilon"
                continue
            values[index] = median
            valid[index] = True
            reasons[index] = "valid"
        return BaselineResult(
            valuesPf=values,
            validMask=valid,
            sampleCounts=counts,
            invalidReasons=reasons,
            frameCount=self.frameCount,
            rejectedFrameCount=self.rejectedFrameCount,
            startMonotonicNs=self.startMonotonicNs,
            endMonotonicNs=self.endMonotonicNs,
        )

    def _frame_matches(self, frame: CapacitanceFrame) -> bool:
        return (
            frame.sessionGeneration == self.sessionGeneration
            and frame.sourceTransport == self.transport
            and (not self.deviceId or frame.deviceId == self.deviceId)
            and frame.rows == self.activeRows
            and frame.generation == self.firmwareGeneration
            and frame.requestId == self.requestId
        )


def delta_percent(frame_values_pf: np.ndarray, baseline: BaselineResult) -> np.ndarray:
    current = np.asarray(frame_values_pf, dtype=np.float64)
    out = np.full(64, np.nan, dtype=np.float64)
    valid = baseline.validMask & np.isfinite(current) & np.isfinite(baseline.valuesPf)
    out[valid] = ((current[valid] - baseline.valuesPf[valid]) / baseline.valuesPf[valid]) * 100.0
    return out
