from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from sensorarray_app.domain.capacitance import expand_rows_to_matrix
from sensorarray_app.domain.models import CapacitanceFrame, MeasurementFrame, MixedMeasurementFrame, ResistanceFrame, VoltageFrame
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
    layout: str = "HOMOGENEOUS"
    rowModes: tuple[str, ...] = ("CAP",) * 8
    rowUnits: tuple[str, ...] = ("pF",) * 8
    rowScales: tuple[int, ...] = (-6,) * 8
    profileGeneration: int | None = None
    profileRequestId: int | None = None
    capValues: np.ndarray = field(default_factory=lambda: np.full((8, 8), np.nan))
    voltValues: np.ndarray = field(default_factory=lambda: np.full((8, 8), np.nan))
    resValues: np.ndarray = field(default_factory=lambda: np.full((8, 8), np.nan))


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
        self._geometry_known = False
        self._rows_generation: int | None = None
        self._rows_request_id: int | None = None
        self._rows_frame_seq: int | None = None
        self._applied_mode = "CAP"
        self._gate_generation: int | None = None
        self._gate_request_id: int | None = None
        self._gate_frame_seq: int | None = None
        self._row_modes: tuple[str, ...] = ("CAP",) * 8
        self._profile_generation: int | None = None
        self._profile_request_id: int | None = None
        self._profile_frame_seq: int | None = None
        self._domain_values = {
            "CAP": np.full((8, 8), np.nan, dtype=np.float64),
            "VOLT": np.full((8, 8), np.nan, dtype=np.float64),
            "RES": np.full((8, 8), np.nan, dtype=np.float64),
        }
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
            self._row_modes = (normalized,) * 8
            # MAPP.gen is the MeasurementMode generation. Firmware also
            # advances RowModeProfile for MODE=set-all, but does not publish
            # that independent counter in MAPP. Preserve it as unknown.
            self._profile_generation = None
            self._profile_request_id = request_id
            self._profile_frame_seq = frame_seq
            self._revision += 1
            # Never expose values or a colour domain from the previous
            # physical quantity while waiting for the first target-mode frame.
            if changed:
                self._latest = None

    def apply_row_modes(
        self,
        modes: Any,
        generation: int | None,
        request_id: int | None,
        frame_seq: int | None,
    ) -> None:
        normalized = _normalize_row_modes(modes)
        with self._lock:
            changed = normalized != self._row_modes
            self._row_modes = normalized
            self._profile_generation = generation
            self._profile_request_id = request_id
            self._profile_frame_seq = frame_seq

            # Firmware 331c445 selects M/MR/K from the complete persisted
            # eight-row profile, including inactive rows.  Only a profile
            # homogeneous across all eight rows uses the legacy C/V/R family.
            # RMAPP is therefore
            # also the authoritative boundary for that legacy-frame gate: a
            # following V/R frame must match the profile generation/request
            # ID, while CAP retains its established sequence-only gate because
            # C/D/K gen/rid identify the independent ROWS transaction.
            homogeneous_mode = normalized[0] if len(set(normalized)) == 1 else None
            if homogeneous_mode is not None:
                changed = changed or homogeneous_mode != self._applied_mode
                self._applied_mode = homogeneous_mode
                self._gate_generation = generation
                self._gate_request_id = request_id
                self._gate_frame_seq = frame_seq
            self._revision += 1
            # Do not relabel values captured under the previous row profile.
            # Wait for one complete CRC-valid mixed/homogeneous frame.
            if changed:
                self._latest = None

    def apply_rows(
        self,
        rows: int,
        generation: int | None,
        request_id: int | None,
        frame_seq: int | None,
    ) -> None:
        """Arm the authoritative geometry identity after a matching RAPP."""

        if not 1 <= int(rows) <= 8:
            raise ValueError("rows must be 1..8")
        with self._lock:
            changed = int(rows) != self._active_rows
            self._active_rows = int(rows)
            self._geometry_known = True
            self._rows_generation = generation
            self._rows_request_id = request_id
            self._rows_frame_seq = frame_seq

            homogeneous_mode = self._row_modes[0] if len(set(self._row_modes)) == 1 else None
            if homogeneous_mode is not None:
                changed = changed or homogeneous_mode != self._applied_mode
                self._applied_mode = homogeneous_mode
                # RAPP owns geometry identity only.  In particular, it must
                # not replace the independent MODE/RMAPP frame gate: doing so
                # after MODE=VOLT/RES would erase that transaction's strict
                # generation boundary and admit a stale V/R frame.
            self._revision += 1
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
        if not self._accept(
            "CAP",
            frame.rows,
            frame.seq,
            frame.generation,
            frame.requestId,
            modern=False,
        ):
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
        if not self._accept(
            frame.mode,
            frame.rows,
            frame.seq,
            frame.generation,
            frame.requestId,
            modern=True,
        ):
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

    def add_mixed(self, frame: MixedMeasurementFrame) -> bool:
        """Atomically store one complete typed MixedMeasurementFrame."""

        profile = _normalize_row_modes(frame.profile)
        if not self._accept_mixed(
            int(frame.seq),
            int(frame.rows),
            int(frame.rowsGeneration),
            int(frame.rowsRequestId),
            profile,
            int(frame.profileGeneration),
            int(frame.profileRequestId),
        ):
            return False
        rows = int(frame.rows)
        values64 = np.full(64, np.nan, dtype=np.float64)
        raw_fixed64 = np.full(64, np.nan, dtype=np.float64)
        raw_pf64 = np.full(64, np.nan, dtype=np.float64)
        valid64 = np.zeros(64, dtype=bool)
        fresh64 = np.zeros(64, dtype=bool)
        error64 = np.zeros(64, dtype=bool)
        error_codes64 = np.full(64, -1, dtype=np.int16)
        error_reasons64 = np.full(64, "", dtype=object)
        pga64 = np.full(64, -1, dtype=np.int16)
        row_units = list(_row_units(profile))
        row_scales = list(_row_scales(profile))

        for row_frame in frame.rowFrames:
            row_index = int(row_frame.row) - 1
            if not 0 <= row_index < rows:
                raise ValueError("mixed row identity is outside active ROWS")
            start = row_index * 8
            stop = start + 8
            physical = np.asarray(row_frame.physicalValues, dtype=np.float64).reshape(8)
            raw_fixed = np.asarray(row_frame.rawFixedValues, dtype=np.float64).reshape(8)
            valid = np.asarray(row_frame.validMask, dtype=bool).reshape(8) & np.isfinite(physical)
            fresh = np.asarray(row_frame.freshMask, dtype=bool).reshape(8)
            errors = np.asarray(row_frame.errorMask, dtype=bool).reshape(8)
            values64[start:stop] = np.where(valid, physical, np.nan)
            raw_fixed64[start:stop] = np.where(valid, raw_fixed, np.nan)
            valid64[start:stop] = valid
            fresh64[start:stop] = fresh
            error64[start:stop] = errors
            codes = np.asarray(row_frame.errorCodes, dtype=np.int16).reshape(8)
            error_codes64[start:stop] = np.where(errors, codes, -1)
            error_reasons64[start:stop] = np.where(
                errors,
                np.asarray(row_frame.errorReasons, dtype=object).reshape(8),
                "",
            )
            if row_frame.pgaValues is not None:
                pga64[start:stop] = np.asarray(row_frame.pgaValues, dtype=np.int16).reshape(8)
            if profile[row_index] == "CAP":
                raw_pf64[start:stop] = np.where(valid, raw_fixed * 1e-6, np.nan)
            row_units[row_index] = str(row_frame.unit)
            row_scales[row_index] = int(row_frame.scale)

        snapshot = MatrixSnapshot(
            revision=0,
            mode="MIXED",
            # Legacy global presentation fields use the first physical row as
            # a compatibility hint only. ``layout=MIXED`` plus per-row fields
            # are authoritative; importantly, no synthetic unit="mixed" is
            # ever exposed.
            quantity=MODE_PRESENTATION[profile[0]][0],
            domain="row_specific",
            activeRows=rows,
            seq=int(frame.seq),
            timestampUs=int(frame.timestampUs),
            matrix=expand_rows_to_matrix(values64, rows),
            rawPf=expand_rows_to_matrix(raw_pf64, rows),
            correctedPf=np.where(
                np.asarray([mode == "CAP" for mode in profile], dtype=bool).reshape(8, 1),
                expand_rows_to_matrix(values64, rows),
                np.nan,
            ),
            rawFixed=expand_rows_to_matrix(raw_fixed64, rows),
            valid=_bool_matrix(valid64, rows),
            fresh=_bool_matrix(fresh64, rows),
            error=_bool_matrix(error64, rows),
            errorCodes=_int_matrix(error_codes64, rows, -1),
            errorReasons=_object_matrix(error_reasons64, rows, ""),
            pga=_int_matrix(pga64, rows, -1),
            pgaBypass=_bool_matrix(pga64 == 0, rows),
            unit="",
            scale=0,
            format="",
            sessionGeneration=int(frame.sessionGeneration),
            firmwareGeneration=int(frame.rowsGeneration),
            requestId=int(frame.rowsRequestId),
            sourceTransport=str(frame.sourceTransport),
            rawHeader=str(frame.rawHeader),
            rawTrailer=str(frame.rawTrailer),
            diagnostics={},
            layout="MIXED",
            rowModes=profile,
            rowUnits=tuple(row_units),
            rowScales=tuple(row_scales),
            profileGeneration=int(frame.profileGeneration),
            profileRequestId=int(frame.profileRequestId),
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
                layout=source.layout,
                rowModes=tuple(source.rowModes),
                rowUnits=tuple(source.rowUnits),
                rowScales=tuple(source.rowScales),
                profileGeneration=source.profileGeneration,
                profileRequestId=source.profileRequestId,
                capValues=source.capValues.copy(),
                voltValues=source.voltValues.copy(),
                resValues=source.resValues.copy(),
            )

    def clear(self) -> None:
        with self._lock:
            self._revision += 1
            self._latest = None
            self._history.clear()
            for values in self._domain_values.values():
                values.fill(np.nan)

    def reset_session(self) -> None:
        """Clear data and all device-scoped frame identities."""

        with self._lock:
            self._revision += 1
            self._latest = None
            self._history.clear()
            self._active_rows = 8
            self._geometry_known = False
            self._rows_generation = None
            self._rows_request_id = None
            self._rows_frame_seq = None
            self._applied_mode = "CAP"
            self._gate_generation = None
            self._gate_request_id = None
            self._gate_frame_seq = None
            self._row_modes = ("CAP",) * 8
            self._profile_generation = None
            self._profile_request_id = None
            self._profile_frame_seq = None
            for values in self._domain_values.values():
                values.fill(np.nan)

    def set_active_rows_for_display(self, rows: int) -> None:
        if not (1 <= int(rows) <= 8):
            raise ValueError("rows must be 1..8")
        with self._lock:
            self._active_rows = int(rows)
            self._geometry_known = True
            # Display-only/replay geometry has no live RAPP identity.
            self._rows_generation = None
            self._rows_request_id = None
            self._rows_frame_seq = None
            self._revision += 1

    def _accept(
        self,
        mode: str,
        rows: int,
        seq: int,
        generation: int | None,
        request_id: int | None,
        *,
        modern: bool,
    ) -> bool:
        normalized = _normalize_mode(mode)
        with self._lock:
            # A heterogeneous full-profile RMAPP disables every homogeneous
            # frame path, even when the active ROWS prefix is one mode.
            # In-flight legacy data from the previous profile must not replace
            # per-row semantics after the atomic profile boundary.
            homogeneous_mode = self._row_modes[0] if len(set(self._row_modes)) == 1 else None
            if homogeneous_mode is None or normalized != homogeneous_mode or normalized != self._applied_mode:
                self.rejectedWrongMode += 1
                return False
            if self._geometry_known and int(rows) != self._active_rows:
                self.rejectedWrongMode += 1
                return False
            if self._rows_frame_seq is not None and int(seq) < self._rows_frame_seq:
                self.rejectedBeforeBoundary += 1
                return False
            if self._gate_frame_seq is not None and int(seq) < self._gate_frame_seq:
                self.rejectedBeforeBoundary += 1
                return False
            if not modern and self._rows_generation is not None and generation != self._rows_generation:
                self.rejectedStaleGeneration += 1
                return False
            if not modern and self._rows_request_id is not None and request_id != self._rows_request_id:
                self.rejectedStaleGeneration += 1
                return False
            if modern and self._gate_generation is not None and generation != self._gate_generation:
                self.rejectedStaleGeneration += 1
                return False
            if modern and self._gate_request_id is not None and request_id != self._gate_request_id:
                self.rejectedStaleGeneration += 1
                return False
            return True

    def _accept_mixed(
        self,
        seq: int,
        rows: int,
        rows_generation: int,
        rows_request_id: int,
        profile: tuple[str, ...],
        generation: int,
        request_id: int,
    ) -> bool:
        with self._lock:
            if self._geometry_known and rows != self._active_rows:
                self.rejectedWrongMode += 1
                return False
            if self._rows_frame_seq is not None and seq < self._rows_frame_seq:
                self.rejectedBeforeBoundary += 1
                return False
            if self._rows_generation is not None and rows_generation != self._rows_generation:
                self.rejectedStaleGeneration += 1
                return False
            if self._rows_request_id is not None and rows_request_id != self._rows_request_id:
                self.rejectedStaleGeneration += 1
                return False
            if profile != self._row_modes:
                self.rejectedWrongMode += 1
                return False
            if self._profile_frame_seq is not None and int(seq) < self._profile_frame_seq:
                self.rejectedBeforeBoundary += 1
                return False
            if self._profile_generation is not None and generation != self._profile_generation:
                self.rejectedStaleGeneration += 1
                return False
            if self._profile_request_id is not None and request_id != self._profile_request_id:
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
            layout="HOMOGENEOUS",
            rowModes=self._row_modes,
            rowUnits=_row_units(self._row_modes),
            rowScales=_row_scales(self._row_modes),
            profileGeneration=self._profile_generation,
            profileRequestId=self._profile_request_id,
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
            self._geometry_known = True
            if isinstance(frame, MixedMeasurementFrame):
                self._rows_generation = int(frame.rowsGeneration)
                self._rows_request_id = int(frame.rowsRequestId)
            elif isinstance(frame, CapacitanceFrame):
                self._rows_generation = int(frame.generation)
                self._rows_request_id = int(frame.requestId)
            current_matrix = np.asarray(snapshot.matrix, dtype=np.float64)
            for row_index in range(snapshot.activeRows):
                row_mode = snapshot.rowModes[row_index]
                self._domain_values[row_mode][row_index, :] = current_matrix[row_index, :]
            self._row_modes = tuple(snapshot.rowModes)
            self._profile_generation = snapshot.profileGeneration
            self._profile_request_id = snapshot.profileRequestId
            committed = {
                **snapshot.__dict__,
                "revision": self._revision,
                "capValues": self._domain_values["CAP"].copy(),
                "voltValues": self._domain_values["VOLT"].copy(),
                "resValues": self._domain_values["RES"].copy(),
            }
            self._latest = MatrixSnapshot(**committed)
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
                row_modes=snapshot.rowModes,
                row_units=snapshot.rowUnits,
                row_scales=snapshot.rowScales,
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
        mixed = len(set(self._row_modes)) > 1
        compatibility_mode = self._row_modes[0] if mixed else self._applied_mode
        quantity, unit, scale, format_name = MODE_PRESENTATION[compatibility_mode]
        return MatrixSnapshot(
            revision=self._revision,
            mode="MIXED" if mixed else self._applied_mode,
            quantity=quantity,
            domain="row_specific" if mixed else quantity,
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
            unit="" if mixed else unit,
            scale=0 if mixed else scale,
            format="" if mixed else format_name,
            sessionGeneration=0,
            firmwareGeneration=self._gate_generation,
            requestId=self._gate_request_id,
            sourceTransport="",
            diagnostics={},
            layout="MIXED" if mixed else "HOMOGENEOUS",
            rowModes=self._row_modes,
            rowUnits=_row_units(self._row_modes),
            rowScales=_row_scales(self._row_modes),
            profileGeneration=self._profile_generation,
            profileRequestId=self._profile_request_id,
            capValues=self._domain_values["CAP"].copy(),
            voltValues=self._domain_values["VOLT"].copy(),
            resValues=self._domain_values["RES"].copy(),
        )


def _normalize_mode(mode: str) -> str:
    normalized = str(mode).strip().upper()
    aliases = {"CAPACITANCE": "CAP", "VOLTAGE": "VOLT", "RESISTANCE": "RES"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in MODE_PRESENTATION:
        raise ValueError("measurement mode must be CAP, VOLT, or RES")
    return normalized


def _normalize_row_modes(modes: Any) -> tuple[str, ...]:
    if isinstance(modes, str):
        compact = modes.strip().upper()
        if len(compact) != 8 or not set(compact) <= {"C", "V", "R"}:
            raise ValueError("row modes must contain exactly 8 CAP, VOLT, or RES entries")
        modes = tuple({"C": "CAP", "V": "VOLT", "R": "RES"}[value] for value in compact)
    try:
        normalized = tuple(_normalize_mode(str(mode)) for mode in modes)
    except TypeError as exc:
        raise ValueError("row modes must contain exactly 8 CAP, VOLT, or RES entries") from exc
    if len(normalized) != 8:
        raise ValueError("row modes must contain exactly 8 CAP, VOLT, or RES entries")
    return normalized


def _row_units(modes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(MODE_PRESENTATION[mode][1] for mode in modes)


def _row_scales(modes: tuple[str, ...]) -> tuple[int, ...]:
    return tuple(MODE_PRESENTATION[mode][2] for mode in modes)


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
