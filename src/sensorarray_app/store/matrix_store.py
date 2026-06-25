from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from sensorarray_app.domain.capacitance import expand_rows_to_matrix
from sensorarray_app.domain.models import CapacitanceFrame, ResistanceFrame, VoltageFrame
from sensorarray_app.store.history_store import MatrixHistoryStore


@dataclass(frozen=True)
class MatrixSnapshot:
    revision: int
    domain: str
    activeRows: int
    seq: int | None
    timestampUs: int | None
    matrix: np.ndarray
    rawPf: np.ndarray
    rawFixed: np.ndarray
    valid: np.ndarray
    unit: str
    sessionGeneration: int
    firmwareGeneration: int | None
    requestId: int | None


class MatrixStore:
    def __init__(self, history_capacity_frames: int = 18_000):
        self._lock = threading.RLock()
        self._revision = 0
        self._latest: MatrixSnapshot | None = None
        self._history = MatrixHistoryStore(history_capacity_frames)
        self._active_rows = 8

    @property
    def history(self) -> MatrixHistoryStore:
        return self._history

    def add_capacitance(self, frame: CapacitanceFrame) -> None:
        values64 = np.full(64, np.nan, dtype=np.float64)
        raw_pf64 = np.full(64, np.nan, dtype=np.float64)
        raw_fixed64 = np.full(64, np.nan, dtype=np.float64)
        valid64 = np.zeros(64, dtype=bool)
        values64[: frame.cells] = frame.correctedPfValues
        raw_pf64[: frame.cells] = frame.rawPfValues
        valid64[: frame.cells] = frame.validMask
        raw_fixed_values = np.asarray(frame.rawFixedValues, dtype=np.float64)
        raw_fixed64[: frame.cells] = np.where(frame.validMask, raw_fixed_values, np.nan)
        matrix = expand_rows_to_matrix(values64, frame.rows)
        raw_pf_matrix = expand_rows_to_matrix(raw_pf64, frame.rows)
        raw_fixed_matrix = expand_rows_to_matrix(raw_fixed64, frame.rows)
        valid_matrix = np.zeros((8, 8), dtype=bool)
        valid_matrix[: frame.rows, :] = valid64[: frame.cells].reshape(frame.rows, 8)
        with self._lock:
            self._revision += 1
            self._active_rows = frame.rows
            self._latest = MatrixSnapshot(
                revision=self._revision,
                domain="capacitance",
                activeRows=frame.rows,
                seq=frame.seq,
                timestampUs=frame.timestampUs,
                matrix=matrix,
                rawPf=raw_pf_matrix,
                rawFixed=raw_fixed_matrix,
                valid=valid_matrix,
                unit="pF",
                sessionGeneration=frame.sessionGeneration,
                firmwareGeneration=frame.generation,
                requestId=frame.requestId,
            )
            self._history.append(frame.seq, frame.timestampUs / 1_000_000.0, values64, valid64, frame.rows)

    def add_voltage(self, frame: VoltageFrame) -> None:
        values = np.asarray(frame.valuesUv, dtype=np.float64).reshape(64)
        valid = np.asarray(frame.validMask, dtype=bool).reshape(64)
        self._add_flat("voltage", frame.seq, frame.timestampUs, values, valid, "uV", frame.sessionGeneration, 8, None, None)

    def add_resistance(self, frame: ResistanceFrame) -> None:
        values = np.asarray(frame.valuesOhm, dtype=np.float64).reshape(64)
        valid = np.asarray(frame.validMask, dtype=bool).reshape(64)
        self._add_flat("resistance", frame.seq, frame.timestampUs, values, valid, "ohm", frame.sessionGeneration, 8, None, None)

    def snapshot(self) -> MatrixSnapshot:
        with self._lock:
            if self._latest is not None:
                return MatrixSnapshot(
                    revision=self._latest.revision,
                    domain=self._latest.domain,
                    activeRows=self._latest.activeRows,
                    seq=self._latest.seq,
                    timestampUs=self._latest.timestampUs,
                    matrix=self._latest.matrix.copy(),
                    rawPf=self._latest.rawPf.copy(),
                    rawFixed=self._latest.rawFixed.copy(),
                    valid=self._latest.valid.copy(),
                    unit=self._latest.unit,
                    sessionGeneration=self._latest.sessionGeneration,
                    firmwareGeneration=self._latest.firmwareGeneration,
                    requestId=self._latest.requestId,
                )
            return MatrixSnapshot(
                revision=self._revision,
                domain="capacitance",
                activeRows=self._active_rows,
                seq=None,
                timestampUs=None,
                matrix=np.full((8, 8), np.nan, dtype=np.float64),
                rawPf=np.full((8, 8), np.nan, dtype=np.float64),
                rawFixed=np.full((8, 8), np.nan, dtype=np.float64),
                valid=np.zeros((8, 8), dtype=bool),
                unit="pF",
                sessionGeneration=0,
                firmwareGeneration=None,
                requestId=None,
            )

    def clear(self) -> None:
        with self._lock:
            self._revision += 1
            self._latest = None
            self._history.clear()

    def set_active_rows_for_display(self, rows: int) -> None:
        if not (1 <= int(rows) <= 8):
            raise ValueError("rows must be 1..8")
        with self._lock:
            self._active_rows = int(rows)
            self._revision += 1

    def _add_flat(
        self,
        domain: str,
        seq: int,
        timestamp_us: int,
        values: np.ndarray,
        valid: np.ndarray,
        unit: str,
        session_generation: int,
        rows: int,
        firmware_generation: int | None,
        request_id: int | None,
    ) -> None:
        matrix = values.reshape(8, 8).copy()
        raw_pf = np.full((8, 8), np.nan, dtype=np.float64)
        raw_fixed = np.full((8, 8), np.nan, dtype=np.float64)
        valid_matrix = valid.reshape(8, 8).copy()
        with self._lock:
            self._revision += 1
            self._latest = MatrixSnapshot(
                revision=self._revision,
                domain=domain,
                activeRows=rows,
                seq=seq,
                timestampUs=timestamp_us,
                matrix=matrix,
                rawPf=raw_pf,
                rawFixed=raw_fixed,
                valid=valid_matrix,
                unit=unit,
                sessionGeneration=session_generation,
                firmwareGeneration=firmware_generation,
                requestId=request_id,
            )
            self._history.append(seq, timestamp_us / 1_000_000.0, values, valid, rows)
