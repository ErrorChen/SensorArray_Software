from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from sensorarray_app.domain.capacitance import expand_rows_to_matrix
from sensorarray_app.domain.models import CapacitanceFrame, MeasurementFrame, ResistanceFrame, VoltageFrame
from sensorarray_app.store.history_store import MatrixHistoryStore


MODE_PRESENTATION: dict[str, tuple[str, str, int, str]] = {
    "CAP": ("capacitance", "pF", -6, "pf6"),
    "VOLT": ("voltage", "V", -6, "uv-x"),
    "RES": ("resistance", "ohm", -3, "mohm-x"),
}


@dataclass(frozen=True)
class MatrixSnapshot:
    revision: int
    mode: str
    quantity: str
    domain: str
    activeRows: int
    seq: int | None
    timestampUs: int | None
    matrix: np.ndarray
    rawPf: np.ndarray
    correctedPf: np.ndarray
    rawFixed: np.ndarray
    valid: np.ndarray
    fresh: np.ndarray
    error: np.ndarray
    errorCodes: np.ndarray
    errorReasons: np.ndarray
    pga: np.ndarray
    pgaBypass: np.ndarray
    unit: str
    scale: int
    format: str
    sessionGeneration: int
    firmwareGeneration: int | None
    requestId: int | None
    sourceTransport: str
    rawHeader: str = ""
    rawTrailer: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


class MatrixStore:
    """Thread-safe current matrix plus quantity-isolated history.

    The store deliberately keeps firmware measurement mode separate from the
    transport mode. A MAPP boundary can arm a generation/request gate before
    the first frame of the new quantity arrives. Modern V/R frames must match
    that gate; CAP uses its independently generated ROWS gen/rid fields, so CAP
    is gated by mode and boundary sequence rather than by the V/R mode gen/rid.
    """

    def __init__(self, history_capacity_frames: int = 18_000):
        self._lock = threading.RLock()
        self._revision = 0
        self._latest: MatrixSnapshot | None = None
        self._history = MatrixHistoryStore(history_capacity_frames)
        self._active_rows = 8
        self._applied_mode = "CAP"
        self._gate_generation: int | None = None
        self._gate_request_id: int | None = None
        self._gate_frame_seq: int | None = None
        self.rejectedStaleGeneration = 0
        self.rejectedWrongMode = 0
        self.rejectedBeforeBoundary = 0

    @property
    def history(self) -> MatrixHistoryStore:
        return self._history

    @property
    def appliedMode(self) -> str:
        with self._lock:
            return self._applied_mode

    def apply_measurement_mode(
        self,
        mode: str,
        generation: int | None,
        request_id: int | None,
        frame_seq: int | None,
    ) -> None:
        normalized = _normalize_mode(mode)
        with self._lock:
            changed = normalized != self._applied_mode
            self._applied_mode = normalized
            self._gate_generation = generation
            self._gate_request_id = request_id
            self._gate_frame_seq = frame_seq
            self._revision += 1
            # Never expose values or a colour domain from the previous
            # physical quantity while waiting for the first target-mode frame.
            if changed:
                self._latest = None

    def sync_measurement_mode_from_frame(self, frame: MeasurementFrame) -> None:
        """Synchronise after attaching to a device already streaming V/R.

        This is not an optimistic UI transition: the complete CRC-valid frame
        itself is the observed device state. A pending MACK transition must be
        resolved by MAPP in the runtime before this method is used.
        """

        self.apply_measurement_mode(frame.mode, frame.generation, frame.requestId, frame.seq)

    def add_capacitance(self, frame: CapacitanceFrame) -> bool:
        if not self._accept("CAP", frame.seq, None, None, modern=False):
            return False
        values64 = np.full(64, np.nan, dtype=np.float64)
        raw_pf64 = np.full(64, np.nan, dtype=np.float64)
        raw_fixed64 = np.full(64, np.nan, dtype=np.float64)
        valid64 = np.zeros(64, dtype=bool)
        fresh64 = np.zeros(64, dtype=bool)
        values64[: frame.cells] = frame.correctedPfValues
        raw_pf64[: frame.cells] = frame.rawPfValues
        valid64[: frame.cells] = np.asarray(frame.validMask, dtype=bool)
        raw_fixed_values = np.asarray(frame.rawFixedValues, dtype=np.float64)
        raw_fixed64[: frame.cells] = np.where(valid64[: frame.cells], raw_fixed_values, np.nan)

        # CAP freshness is reported per row and per primary/secondary FDC.
        for row_index in range(frame.rows):
            row_bit = 1 << row_index
            row_fresh = bool(frame.rowFreshMask & row_bit)
            for col_index in range(8):
                device_mask = frame.primaryFreshMask if col_index < 4 else frame.secondaryFreshMask
                fresh64[row_index * 8 + col_index] = row_fresh and bool(device_mask & row_bit)

        error_codes64 = np.full(64, -1, dtype=np.int16)
        error_reasons64 = np.full(64, "", dtype=object)
        invalid_active = ~valid64[: frame.cells]
        invalid_indices = np.flatnonzero(invalid_active)
        error_codes64[invalid_indices] = 20
        error_reasons64[invalid_indices] = "Invalid capacitance value"
        snapshot = self._make_snapshot(
            mode="CAP",
            rows=frame.rows,
            seq=frame.seq,
            timestamp_us=frame.timestampUs,
            values=values64,
            raw_pf=raw_pf64,
            raw_fixed=raw_fixed64,
            valid=valid64,
            fresh=fresh64,
            error_codes=error_codes64,
            error_reasons=error_reasons64,
            pga=np.full(64, -1, dtype=np.int16),
            session_generation=frame.sessionGeneration,
            firmware_generation=frame.generation,
            request_id=frame.requestId,
            source_transport=frame.sourceTransport,
            raw_header=frame.rawHeader,
            raw_trailer=frame.rawTrailer,
            diagnostics={
                "rowFreshMask": frame.rowFreshMask,
                "primaryFreshMask": frame.primaryFreshMask,
                "secondaryFreshMask": frame.secondaryFreshMask,
                "badStaleCount": frame.badStaleCount,
                "badMixedCount": frame.badMixedCount,
                "badInvalidCount": frame.badInvalidCount,
            },
        )
        self._commit(snapshot, values64, valid64, fresh64, raw_fixed64, error_codes64, np.full(64, -1), frame)
        return True

    def add_measurement(self, frame: MeasurementFrame) -> bool:
        if not self._accept(frame.mode, frame.seq, frame.generation, frame.requestId, modern=True):
            return False
        cells = int(frame.cells)
        values64 = np.full(64, np.nan, dtype=np.float64)
        raw_fixed64 = np.full(64, np.nan, dtype=np.float64)
        valid64 = np.zeros(64, dtype=bool)
        fresh64 = np.zeros(64, dtype=bool)
        error_mask64 = np.zeros(64, dtype=bool)
        error_codes64 = np.full(64, -1, dtype=np.int16)
        error_reasons64 = np.full(64, "", dtype=object)
        pga64 = np.full(64, -1, dtype=np.int16)

        physical = np.asarray(frame.physicalValues, dtype=np.float64).reshape(cells)
        raw = np.asarray(frame.rawFixedValues, dtype=np.float64).reshape(cells)
        parsed_valid = np.asarray(frame.validMask, dtype=bool).reshape(cells) & np.isfinite(physical)
        values64[:cells] = np.where(parsed_valid, physical, np.nan)
        raw_fixed64[:cells] = np.where(parsed_valid, raw, np.nan)
        valid64[:cells] = parsed_valid
        fresh64[:cells] = np.asarray(frame.freshMask, dtype=bool).reshape(cells)
        parsed_error_mask = np.asarray(frame.errorMask, dtype=bool).reshape(cells)
        parsed_error_codes = np.asarray(frame.errorCodes, dtype=np.int16).reshape(cells)
        parsed_error_reasons = np.asarray(frame.errorReasons, dtype=object).reshape(cells)
        error_mask64[:cells] = parsed_error_mask
        # The parser retains firmware code 0 for ordinary valid cells.  Store
        # and export use -1 to mean "no cell error" so an error mask cannot be
        # reconstructed as every active bit merely because code 0 is present.
        error_codes64[:cells] = np.where(parsed_error_mask, parsed_error_codes, -1)
        error_reasons64[:cells] = np.where(parsed_error_mask, parsed_error_reasons, "")
        pga64[:cells] = np.asarray(frame.pgaValues, dtype=np.int16).reshape(cells)

        diagnostics = {
            "reference": frame.reference,
            "railValid": frame.railValid,
            "railAgeFrames": frame.railAgeFrames,
            "avddUv": frame.avddUv,
            "avssUv": frame.avssUv,
            "matrixReferenceUv": frame.matrixReferenceUv,
            "referenceResistorOhms": frame.referenceResistorOhms,
            "durationUs": frame.durationUs,
            "transitionDurationUs": frame.transitionDurationUs,
            "gainChangeCount": frame.gainChangeCount,
            "overrangeCount": frame.overrangeCount,
            "autorangeAttemptCount": frame.autorangeAttemptCount,
            "autorangeFallbackCount": frame.autorangeFallbackCount,
            "recoveredRetryCount": frame.recoveredRetryCount,
            "drdyTimeoutCount": frame.drdyTimeoutCount,
            "staleCount": frame.staleCount,
            "spiErrorCount": frame.spiErrorCount,
            "badCellCount": frame.badCellCount,
        }
        snapshot = self._make_snapshot(
            mode=frame.mode,
            rows=frame.rows,
            seq=frame.seq,
            timestamp_us=frame.timestampUs,
            values=values64,
            raw_pf=np.full(64, np.nan),
            raw_fixed=raw_fixed64,
            valid=valid64,
            fresh=fresh64,
            error_codes=error_codes64,
            error_reasons=error_reasons64,
            pga=pga64,
            session_generation=frame.sessionGeneration,
            firmware_generation=frame.generation,
            request_id=frame.requestId,
            source_transport=frame.sourceTransport,
            raw_header=frame.rawHeader,
            raw_trailer=frame.rawTrailer,
            diagnostics=diagnostics,
            explicit_error=error_mask64,
        )
        self._commit(snapshot, values64, valid64, fresh64, raw_fixed64, error_codes64, pga64, frame)
        return True

    def add_voltage(self, frame: VoltageFrame) -> None:
        values_uv = np.asarray(frame.valuesUv, dtype=np.float64).reshape(64)
        valid = np.asarray(frame.validMask, dtype=bool).reshape(64)
        self._add_legacy_flat("VOLT", frame.seq, frame.timestampUs, values_uv * 1e-6, values_uv, valid, frame.sessionGeneration)

    def add_resistance(self, frame: ResistanceFrame) -> None:
        values_ohm = np.asarray(frame.valuesOhm, dtype=np.float64).reshape(64)
        valid = np.asarray(frame.validMask, dtype=bool).reshape(64)
        self._add_legacy_flat("RES", frame.seq, frame.timestampUs, values_ohm, values_ohm, valid, frame.sessionGeneration)

    def snapshot(self) -> MatrixSnapshot:
        with self._lock:
            source = self._latest if self._latest is not None else self._empty_snapshot_locked()
            return MatrixSnapshot(
                revision=source.revision,
                mode=source.mode,
                quantity=source.quantity,
                domain=source.domain,
                activeRows=source.activeRows,
                seq=source.seq,
                timestampUs=source.timestampUs,
                matrix=source.matrix.copy(),
                rawPf=source.rawPf.copy(),
                correctedPf=source.correctedPf.copy(),
                rawFixed=source.rawFixed.copy(),
                valid=source.valid.copy(),
                fresh=source.fresh.copy(),
                error=source.error.copy(),
                errorCodes=source.errorCodes.copy(),
                errorReasons=source.errorReasons.copy(),
                pga=source.pga.copy(),
                pgaBypass=source.pgaBypass.copy(),
                unit=source.unit,
                scale=source.scale,
                format=source.format,
                sessionGeneration=source.sessionGeneration,
                firmwareGeneration=source.firmwareGeneration,
                requestId=source.requestId,
                sourceTransport=source.sourceTransport,
                rawHeader=source.rawHeader,
                rawTrailer=source.rawTrailer,
                diagnostics=dict(source.diagnostics),
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

    def _accept(
        self,
        mode: str,
        seq: int,
        generation: int | None,
        request_id: int | None,
        *,
        modern: bool,
    ) -> bool:
        normalized = _normalize_mode(mode)
        with self._lock:
            if normalized != self._applied_mode:
                self.rejectedWrongMode += 1
                return False
            if self._gate_frame_seq is not None and int(seq) < self._gate_frame_seq:
                self.rejectedBeforeBoundary += 1
                return False
            if modern and self._gate_generation is not None and generation != self._gate_generation:
                self.rejectedStaleGeneration += 1
                return False
            if modern and self._gate_request_id is not None and request_id != self._gate_request_id:
                self.rejectedStaleGeneration += 1
                return False
            return True

    def _make_snapshot(
        self,
        *,
        mode: str,
        rows: int,
        seq: int,
        timestamp_us: int,
        values: np.ndarray,
        raw_pf: np.ndarray,
        raw_fixed: np.ndarray,
        valid: np.ndarray,
        fresh: np.ndarray,
        error_codes: np.ndarray,
        error_reasons: np.ndarray,
        pga: np.ndarray,
        session_generation: int,
        firmware_generation: int | None,
        request_id: int | None,
        source_transport: str,
        raw_header: str,
        raw_trailer: str,
        diagnostics: dict[str, Any],
        explicit_error: np.ndarray | None = None,
    ) -> MatrixSnapshot:
        normalized = _normalize_mode(mode)
        quantity, unit, scale, format_name = MODE_PRESENTATION[normalized]
        error_flat = np.asarray(explicit_error, dtype=bool).reshape(64) if explicit_error is not None else error_codes >= 0
        pga_flat = np.asarray(pga, dtype=np.int16).reshape(64)
        return MatrixSnapshot(
            revision=0,
            mode=normalized,
            quantity=quantity,
            domain=quantity,
            activeRows=int(rows),
            seq=int(seq),
            timestampUs=int(timestamp_us),
            matrix=expand_rows_to_matrix(values, rows),
            rawPf=expand_rows_to_matrix(raw_pf, rows),
            correctedPf=expand_rows_to_matrix(values, rows) if normalized == "CAP" else np.full((8, 8), np.nan),
            rawFixed=expand_rows_to_matrix(raw_fixed, rows),
            valid=_bool_matrix(valid, rows),
            fresh=_bool_matrix(fresh, rows),
            error=_bool_matrix(error_flat, rows),
            errorCodes=_int_matrix(error_codes, rows, -1),
            errorReasons=_object_matrix(error_reasons, rows, ""),
            pga=_int_matrix(pga_flat, rows, -1),
            pgaBypass=_bool_matrix(pga_flat == 0, rows),
            unit=unit,
            scale=scale,
            format=format_name,
            sessionGeneration=int(session_generation),
            firmwareGeneration=firmware_generation,
            requestId=request_id,
            sourceTransport=str(source_transport),
            rawHeader=raw_header,
            rawTrailer=raw_trailer,
            diagnostics=dict(diagnostics),
        )

    def _commit(
        self,
        snapshot: MatrixSnapshot,
        values: np.ndarray,
        valid: np.ndarray,
        fresh: np.ndarray,
        raw_fixed: np.ndarray,
        error_codes: np.ndarray,
        pga: np.ndarray,
        frame: Any,
    ) -> None:
        with self._lock:
            self._revision += 1
            self._active_rows = snapshot.activeRows
            self._latest = MatrixSnapshot(**{**snapshot.__dict__, "revision": self._revision})
            self._history.append(
                snapshot.seq or 0,
                (snapshot.timestampUs or 0) / 1_000_000.0,
                values,
                valid,
                snapshot.activeRows,
                mode=snapshot.mode,
                unit=snapshot.unit,
                source=snapshot.sourceTransport,
                scale=snapshot.scale,
                fresh=fresh,
                raw_fixed=raw_fixed,
                error_codes=error_codes,
                pga=pga,
                generation=snapshot.firmwareGeneration,
                request_id=snapshot.requestId,
            )

    def _add_legacy_flat(
        self,
        mode: str,
        seq: int,
        timestamp_us: int,
        values: np.ndarray,
        raw_fixed: np.ndarray,
        valid: np.ndarray,
        session_generation: int,
    ) -> None:
        # Legacy imported voltage/resistance data has no MAPP generation. It is
        # retained for compatibility but is never used by the modern parser.
        self.apply_measurement_mode(mode, None, None, None)
        fresh = np.asarray(valid, dtype=bool).reshape(64)
        snapshot = self._make_snapshot(
            mode=mode,
            rows=8,
            seq=seq,
            timestamp_us=timestamp_us,
            values=values,
            raw_pf=np.full(64, np.nan),
            raw_fixed=raw_fixed,
            valid=valid,
            fresh=fresh,
            error_codes=np.full(64, -1),
            error_reasons=np.full(64, "", dtype=object),
            pga=np.full(64, -1),
            session_generation=session_generation,
            firmware_generation=None,
            request_id=None,
            source_transport="legacy",
            raw_header="",
            raw_trailer="",
            diagnostics={"legacy": True},
        )
        self._commit(snapshot, values, valid, fresh, raw_fixed, np.full(64, -1), np.full(64, -1), None)

    def _empty_snapshot_locked(self) -> MatrixSnapshot:
        quantity, unit, scale, format_name = MODE_PRESENTATION[self._applied_mode]
        return MatrixSnapshot(
            revision=self._revision,
            mode=self._applied_mode,
            quantity=quantity,
            domain=quantity,
            activeRows=self._active_rows,
            seq=None,
            timestampUs=None,
            matrix=np.full((8, 8), np.nan),
            rawPf=np.full((8, 8), np.nan),
            correctedPf=np.full((8, 8), np.nan),
            rawFixed=np.full((8, 8), np.nan),
            valid=np.zeros((8, 8), dtype=bool),
            fresh=np.zeros((8, 8), dtype=bool),
            error=np.zeros((8, 8), dtype=bool),
            errorCodes=np.full((8, 8), -1, dtype=np.int16),
            errorReasons=np.full((8, 8), "", dtype=object),
            pga=np.full((8, 8), -1, dtype=np.int16),
            pgaBypass=np.zeros((8, 8), dtype=bool),
            unit=unit,
            scale=scale,
            format=format_name,
            sessionGeneration=0,
            firmwareGeneration=self._gate_generation,
            requestId=self._gate_request_id,
            sourceTransport="",
            diagnostics={},
        )


def _normalize_mode(mode: str) -> str:
    normalized = str(mode).strip().upper()
    aliases = {"CAPACITANCE": "CAP", "VOLTAGE": "VOLT", "RESISTANCE": "RES"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in MODE_PRESENTATION:
        raise ValueError("measurement mode must be CAP, VOLT, or RES")
    return normalized


def _bool_matrix(values: np.ndarray, rows: int) -> np.ndarray:
    matrix = np.zeros((8, 8), dtype=bool)
    matrix[:rows, :] = np.asarray(values, dtype=bool).reshape(64)[: rows * 8].reshape(rows, 8)
    return matrix


def _int_matrix(values: np.ndarray, rows: int, fill: int) -> np.ndarray:
    matrix = np.full((8, 8), fill, dtype=np.int16)
    matrix[:rows, :] = np.asarray(values, dtype=np.int16).reshape(64)[: rows * 8].reshape(rows, 8)
    return matrix


def _object_matrix(values: np.ndarray, rows: int, fill: str) -> np.ndarray:
    matrix = np.full((8, 8), fill, dtype=object)
    matrix[:rows, :] = np.asarray(values, dtype=object).reshape(64)[: rows * 8].reshape(rows, 8)
    return matrix
