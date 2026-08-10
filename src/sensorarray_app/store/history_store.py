from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class HistorySlice:
    seq: np.ndarray
    timeSeconds: np.ndarray
    values: np.ndarray
    valid: np.ndarray
    fresh: np.ndarray
    rows: np.ndarray
    modes: np.ndarray
    units: np.ndarray
    sources: np.ndarray
    scales: np.ndarray
    rawFixed: np.ndarray
    errorCodes: np.ndarray
    pga: np.ndarray
    generations: np.ndarray
    requestIds: np.ndarray
    revision: int


class MatrixHistoryStore:
    def __init__(self, capacity_frames: int = 18_000):
        self.capacity = max(1, int(capacity_frames))
        self.seq = np.full(self.capacity, -1, dtype=np.int64)
        self.timeSeconds = np.full(self.capacity, np.nan, dtype=np.float64)
        self.values = np.full((self.capacity, 64), np.nan, dtype=np.float64)
        self.valid = np.zeros((self.capacity, 64), dtype=bool)
        self.fresh = np.zeros((self.capacity, 64), dtype=bool)
        self.rows = np.zeros(self.capacity, dtype=np.int16)
        self.modes = np.full(self.capacity, "CAP", dtype="U4")
        self.units = np.full(self.capacity, "pF", dtype="U8")
        self.sources = np.full(self.capacity, "", dtype="U16")
        self.scales = np.full(self.capacity, -6, dtype=np.int16)
        self.rawFixed = np.full((self.capacity, 64), np.nan, dtype=np.float64)
        self.errorCodes = np.full((self.capacity, 64), -1, dtype=np.int16)
        self.pga = np.full((self.capacity, 64), -1, dtype=np.int16)
        self.generations = np.full(self.capacity, -1, dtype=np.int64)
        self.requestIds = np.full(self.capacity, -1, dtype=np.int64)
        self.writeIndex = 0
        self.frameCount = 0
        self.totalFrames = 0
        self.overwrites = 0
        self.revision = 0

    def append(
        self,
        seq: int,
        time_seconds: float,
        values: np.ndarray,
        valid: np.ndarray,
        rows: int,
        *,
        mode: str = "CAP",
        unit: str = "pF",
        source: str = "",
        scale: int = -6,
        fresh: np.ndarray | None = None,
        raw_fixed: np.ndarray | None = None,
        error_codes: np.ndarray | None = None,
        pga: np.ndarray | None = None,
        generation: int | None = None,
        request_id: int | None = None,
    ) -> None:
        if self.frameCount == self.capacity:
            self.overwrites += 1
        index = self.writeIndex
        self.seq[index] = int(seq)
        self.timeSeconds[index] = float(time_seconds)
        self.values[index, :] = np.asarray(values, dtype=np.float64).reshape(64)
        self.valid[index, :] = np.asarray(valid, dtype=bool).reshape(64)
        self.fresh[index, :] = (
            np.asarray(fresh, dtype=bool).reshape(64) if fresh is not None else self.valid[index, :]
        )
        self.rows[index] = int(rows)
        self.modes[index] = str(mode).upper()
        self.units[index] = str(unit)
        self.sources[index] = str(source)
        self.scales[index] = int(scale)
        self.rawFixed[index, :] = (
            np.asarray(raw_fixed, dtype=np.float64).reshape(64) if raw_fixed is not None else np.nan
        )
        self.errorCodes[index, :] = (
            np.asarray(error_codes, dtype=np.int16).reshape(64) if error_codes is not None else -1
        )
        self.pga[index, :] = np.asarray(pga, dtype=np.int16).reshape(64) if pga is not None else -1
        self.generations[index] = -1 if generation is None else int(generation)
        self.requestIds[index] = -1 if request_id is None else int(request_id)
        self.writeIndex = (self.writeIndex + 1) % self.capacity
        self.frameCount = min(self.frameCount + 1, self.capacity)
        self.totalFrames += 1
        self.revision += 1

    def ordered_indices(self) -> np.ndarray:
        if self.frameCount < self.capacity:
            return np.arange(self.frameCount, dtype=np.int64)
        return np.concatenate(
            (
                np.arange(self.writeIndex, self.capacity, dtype=np.int64),
                np.arange(0, self.writeIndex, dtype=np.int64),
            )
        )

    def slice(
        self,
        cell_indices: list[int],
        window_seconds: float | None = None,
        latest_n: int | None = None,
        measurement_mode: str | None = None,
    ) -> HistorySlice:
        indices = self.ordered_indices()
        if measurement_mode is not None:
            indices = indices[self.modes[indices] == str(measurement_mode).upper()]
        if latest_n is not None and latest_n > 0:
            indices = indices[-int(latest_n) :]
        elif window_seconds is not None and indices.size:
            latest = self.timeSeconds[indices][-1]
            indices = indices[self.timeSeconds[indices] >= latest - float(window_seconds)]
        return HistorySlice(
            seq=self.seq[indices].copy(),
            timeSeconds=self.timeSeconds[indices].copy(),
            values=self.values[np.ix_(indices, cell_indices)].copy(),
            valid=self.valid[np.ix_(indices, cell_indices)].copy(),
            fresh=self.fresh[np.ix_(indices, cell_indices)].copy(),
            rows=self.rows[indices].copy(),
            modes=self.modes[indices].copy(),
            units=self.units[indices].copy(),
            sources=self.sources[indices].copy(),
            scales=self.scales[indices].copy(),
            rawFixed=self.rawFixed[np.ix_(indices, cell_indices)].copy(),
            errorCodes=self.errorCodes[np.ix_(indices, cell_indices)].copy(),
            pga=self.pga[np.ix_(indices, cell_indices)].copy(),
            generations=self.generations[indices].copy(),
            requestIds=self.requestIds[indices].copy(),
            revision=self.revision,
        )

    def clear(self) -> None:
        self.seq.fill(-1)
        self.timeSeconds.fill(np.nan)
        self.values.fill(np.nan)
        self.valid.fill(False)
        self.fresh.fill(False)
        self.rows.fill(0)
        self.modes.fill("CAP")
        self.units.fill("pF")
        self.sources.fill("")
        self.scales.fill(-6)
        self.rawFixed.fill(np.nan)
        self.errorCodes.fill(-1)
        self.pga.fill(-1)
        self.generations.fill(-1)
        self.requestIds.fill(-1)
        self.writeIndex = 0
        self.frameCount = 0
        self.totalFrames = 0
        self.revision += 1
