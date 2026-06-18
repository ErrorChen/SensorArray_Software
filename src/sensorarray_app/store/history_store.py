from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class HistorySlice:
    seq: np.ndarray
    timeSeconds: np.ndarray
    values: np.ndarray
    valid: np.ndarray
    rows: np.ndarray
    revision: int


class MatrixHistoryStore:
    def __init__(self, capacity_frames: int = 18_000):
        self.capacity = max(1, int(capacity_frames))
        self.seq = np.full(self.capacity, -1, dtype=np.int64)
        self.timeSeconds = np.full(self.capacity, np.nan, dtype=np.float64)
        self.values = np.full((self.capacity, 64), np.nan, dtype=np.float64)
        self.valid = np.zeros((self.capacity, 64), dtype=bool)
        self.rows = np.zeros(self.capacity, dtype=np.int16)
        self.writeIndex = 0
        self.frameCount = 0
        self.totalFrames = 0
        self.overwrites = 0
        self.revision = 0

    def append(self, seq: int, time_seconds: float, values: np.ndarray, valid: np.ndarray, rows: int) -> None:
        if self.frameCount == self.capacity:
            self.overwrites += 1
        index = self.writeIndex
        self.seq[index] = int(seq)
        self.timeSeconds[index] = float(time_seconds)
        self.values[index, :] = np.asarray(values, dtype=np.float64).reshape(64)
        self.valid[index, :] = np.asarray(valid, dtype=bool).reshape(64)
        self.rows[index] = int(rows)
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

    def slice(self, cell_indices: list[int], window_seconds: float | None = None, latest_n: int | None = None) -> HistorySlice:
        indices = self.ordered_indices()
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
            rows=self.rows[indices].copy(),
            revision=self.revision,
        )

    def clear(self) -> None:
        self.seq.fill(-1)
        self.timeSeconds.fill(np.nan)
        self.values.fill(np.nan)
        self.valid.fill(False)
        self.rows.fill(0)
        self.writeIndex = 0
        self.frameCount = 0
        self.totalFrames = 0
        self.revision += 1
