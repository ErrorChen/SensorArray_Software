from __future__ import annotations

from dataclasses import dataclass

import numpy as np


_SIGNED_INT_MISSING = np.iinfo(np.int64).min


@dataclass
class HistorySlice:
    seq: np.ndarray
    timeSeconds: np.ndarray
    values: np.ndarray
    valid: np.ndarray
    fresh: np.ndarray
    expected: np.ndarray
    acquired: np.ndarray
    expectedKnown: np.ndarray
    acquiredKnown: np.ndarray
    freshKnown: np.ndarray
    error: np.ndarray
    rows: np.ndarray
    modes: np.ndarray
    units: np.ndarray
    sources: np.ndarray
    scales: np.ndarray
    rawFixed: np.ndarray
    errorCodes: np.ndarray
    errorReasons: np.ndarray
    pga: np.ndarray
    pgaBypass: np.ndarray
    generations: np.ndarray
    requestIds: np.ndarray
    rowModes: np.ndarray
    rowUnits: np.ndarray
    rowScales: np.ndarray
    connectionGenerations: np.ndarray
    bootIds: np.ndarray
    deviceTimestampUs: np.ndarray
    hostWallTimes: np.ndarray
    hostMonotonicNs: np.ndarray
    rowsGenerations: np.ndarray
    rowsRequestIds: np.ndarray
    modeGenerations: np.ndarray
    modeRequestIds: np.ndarray
    profileGenerations: np.ndarray
    profileRequestIds: np.ndarray
    wireProfiles: np.ndarray
    revision: int


class MatrixHistoryStore:
    def __init__(self, capacity_frames: int = 18_000):
        self.capacity = max(1, int(capacity_frames))
        self.seq = np.full(self.capacity, -1, dtype=np.int64)
        self.timeSeconds = np.full(self.capacity, np.nan, dtype=np.float64)
        self.values = np.full((self.capacity, 64), np.nan, dtype=np.float64)
        self.valid = np.zeros((self.capacity, 64), dtype=bool)
        self.fresh = np.zeros((self.capacity, 64), dtype=bool)
        self.expected = np.zeros((self.capacity, 64), dtype=bool)
        self.acquired = np.zeros((self.capacity, 64), dtype=bool)
        self.expectedKnown = np.zeros((self.capacity, 64), dtype=bool)
        self.acquiredKnown = np.zeros((self.capacity, 64), dtype=bool)
        self.freshKnown = np.zeros((self.capacity, 64), dtype=bool)
        self.error = np.zeros((self.capacity, 64), dtype=bool)
        self.rows = np.zeros(self.capacity, dtype=np.int16)
        self.modes = np.full(self.capacity, "CAP", dtype="U8")
        self.units = np.full(self.capacity, "pF", dtype="U8")
        self.sources = np.full(self.capacity, "", dtype="U16")
        self.scales = np.full(self.capacity, -6, dtype=np.int16)
        self.rawFixed = np.full((self.capacity, 64), np.nan, dtype=np.float64)
        self.errorCodes = np.full((self.capacity, 64), -1, dtype=np.int16)
        self.errorReasons = np.full((self.capacity, 64), "", dtype="U96")
        self.pga = np.full((self.capacity, 64), -1, dtype=np.int16)
        self.pgaBypass = np.zeros((self.capacity, 64), dtype=bool)
        self.generations = np.full(self.capacity, -1, dtype=np.int64)
        self.requestIds = np.full(self.capacity, -1, dtype=np.int64)
        self.rowModes = np.full((self.capacity, 8), "CAP", dtype="U4")
        self.rowUnits = np.full((self.capacity, 8), "pF", dtype="U8")
        self.rowScales = np.full((self.capacity, 8), -6, dtype=np.int16)
        self.connectionGenerations = np.zeros(self.capacity, dtype=np.int64)
        self.bootIds = np.full(self.capacity, -1, dtype=np.int64)
        self.deviceTimestampUs = np.full(self.capacity, -1, dtype=np.int64)
        self.hostWallTimes = np.full(self.capacity, np.nan, dtype=np.float64)
        self.hostMonotonicNs = np.full(self.capacity, -1, dtype=np.int64)
        self.rowsGenerations = np.full(self.capacity, -1, dtype=np.int64)
        self.rowsRequestIds = np.full(self.capacity, -1, dtype=np.int64)
        self.modeGenerations = np.full(self.capacity, -1, dtype=np.int64)
        self.modeRequestIds = np.full(self.capacity, -1, dtype=np.int64)
        self.profileGenerations = np.full(self.capacity, -1, dtype=np.int64)
        self.profileRequestIds = np.full(self.capacity, -1, dtype=np.int64)
        self.wireProfiles = np.full(self.capacity, "", dtype="U8")
        self.railValid = np.zeros(self.capacity, dtype=bool)
        self.railFresh = np.zeros(self.capacity, dtype=bool)
        self.railAge = np.full(self.capacity, -1, dtype=np.int64)
        self.avddUv = np.full(self.capacity, -1, dtype=np.int64)
        # AVSS is legitimately negative, so -1 cannot be used as a missing
        # sentinel without corrupting scientific export.
        self.avssUv = np.full(self.capacity, _SIGNED_INT_MISSING, dtype=np.int64)
        self.railSpanUv = np.full(self.capacity, -1, dtype=np.int64)
        self.railSource = np.full(self.capacity, "", dtype="U32")
        self.railReason = np.full(self.capacity, "", dtype="U96")
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
        expected: np.ndarray | None = None,
        acquired: np.ndarray | None = None,
        expected_known: np.ndarray | bool | None = None,
        acquired_known: np.ndarray | bool | None = None,
        fresh_known: np.ndarray | bool | None = None,
        error: np.ndarray | None = None,
        raw_fixed: np.ndarray | None = None,
        error_codes: np.ndarray | None = None,
        error_reasons: np.ndarray | None = None,
        pga: np.ndarray | None = None,
        pga_bypass: np.ndarray | None = None,
        generation: int | None = None,
        request_id: int | None = None,
        row_modes: tuple[str, ...] | None = None,
        row_units: tuple[str, ...] | None = None,
        row_scales: tuple[int, ...] | None = None,
        connection_generation: int = 0,
        boot_id: int | None = None,
        device_timestamp_us: int | None = None,
        host_wall_time: float | None = None,
        host_monotonic_ns: int | None = None,
        rows_generation: int | None = None,
        rows_request_id: int | None = None,
        mode_generation: int | None = None,
        mode_request_id: int | None = None,
        profile_generation: int | None = None,
        profile_request_id: int | None = None,
        wire_profile: str | None = None,
        rail_valid: bool = False,
        rail_fresh: bool = False,
        rail_age: int | None = None,
        avdd_uv: int | None = None,
        avss_uv: int | None = None,
        rail_span_uv: int | None = None,
        rail_source: str = "",
        rail_reason: str = "",
    ) -> None:
        if self.frameCount == self.capacity:
            self.overwrites += 1
        index = self.writeIndex
        self.seq[index] = int(seq)
        self.timeSeconds[index] = float(time_seconds)
        self.values[index, :] = np.asarray(values, dtype=np.float64).reshape(64)
        self.valid[index, :] = np.asarray(valid, dtype=bool).reshape(64)
        self.fresh[index, :] = np.asarray(fresh, dtype=bool).reshape(64) if fresh is not None else False
        self.expected[index, :] = np.asarray(expected, dtype=bool).reshape(64) if expected is not None else False
        self.acquired[index, :] = np.asarray(acquired, dtype=bool).reshape(64) if acquired is not None else False
        self.expectedKnown[index, :] = _known_mask(expected_known, expected is not None)
        self.acquiredKnown[index, :] = _known_mask(acquired_known, acquired is not None)
        self.freshKnown[index, :] = _known_mask(fresh_known, fresh is not None)
        self.error[index, :] = np.asarray(error, dtype=bool).reshape(64) if error is not None else False
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
        self.errorReasons[index, :] = (
            np.asarray(error_reasons, dtype=str).reshape(64) if error_reasons is not None else ""
        )
        self.pga[index, :] = np.asarray(pga, dtype=np.int16).reshape(64) if pga is not None else -1
        self.pgaBypass[index, :] = (
            np.asarray(pga_bypass, dtype=bool).reshape(64) if pga_bypass is not None else False
        )
        self.generations[index] = -1 if generation is None else int(generation)
        self.requestIds[index] = -1 if request_id is None else int(request_id)
        self.rowModes[index, :] = tuple(row_modes) if row_modes is not None else (str(mode).upper(),) * 8
        self.rowUnits[index, :] = tuple(row_units) if row_units is not None else (str(unit),) * 8
        self.rowScales[index, :] = tuple(row_scales) if row_scales is not None else (int(scale),) * 8
        self.connectionGenerations[index] = int(connection_generation)
        self.bootIds[index] = -1 if boot_id is None else int(boot_id)
        self.deviceTimestampUs[index] = -1 if device_timestamp_us is None else int(device_timestamp_us)
        self.hostWallTimes[index] = np.nan if host_wall_time is None else float(host_wall_time)
        self.hostMonotonicNs[index] = -1 if host_monotonic_ns is None else int(host_monotonic_ns)
        self.rowsGenerations[index] = -1 if rows_generation is None else int(rows_generation)
        self.rowsRequestIds[index] = -1 if rows_request_id is None else int(rows_request_id)
        self.modeGenerations[index] = -1 if mode_generation is None else int(mode_generation)
        self.modeRequestIds[index] = -1 if mode_request_id is None else int(mode_request_id)
        self.profileGenerations[index] = -1 if profile_generation is None else int(profile_generation)
        self.profileRequestIds[index] = -1 if profile_request_id is None else int(profile_request_id)
        self.wireProfiles[index] = str(wire_profile or "")
        self.railValid[index] = bool(rail_valid)
        self.railFresh[index] = bool(rail_fresh)
        self.railAge[index] = -1 if rail_age is None else int(rail_age)
        self.avddUv[index] = -1 if avdd_uv is None else int(avdd_uv)
        self.avssUv[index] = _SIGNED_INT_MISSING if avss_uv is None else int(avss_uv)
        self.railSpanUv[index] = -1 if rail_span_uv is None else int(rail_span_uv)
        self.railSource[index] = str(rail_source)
        self.railReason[index] = str(rail_reason)
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
        if latest_n is not None and latest_n > 0:
            indices = indices[-int(latest_n) :]
        elif window_seconds is not None and indices.size:
            latest = self.timeSeconds[indices][-1]
            indices = indices[self.timeSeconds[indices] >= latest - float(window_seconds)]
        values = self.values[np.ix_(indices, cell_indices)].copy()
        valid = self.valid[np.ix_(indices, cell_indices)].copy()
        fresh = self.fresh[np.ix_(indices, cell_indices)].copy()
        expected = self.expected[np.ix_(indices, cell_indices)].copy()
        acquired = self.acquired[np.ix_(indices, cell_indices)].copy()
        expected_known = self.expectedKnown[np.ix_(indices, cell_indices)].copy()
        acquired_known = self.acquiredKnown[np.ix_(indices, cell_indices)].copy()
        fresh_known = self.freshKnown[np.ix_(indices, cell_indices)].copy()
        error = self.error[np.ix_(indices, cell_indices)].copy()
        raw_fixed = self.rawFixed[np.ix_(indices, cell_indices)].copy()
        error_codes = self.errorCodes[np.ix_(indices, cell_indices)].copy()
        error_reasons = self.errorReasons[np.ix_(indices, cell_indices)].copy()
        pga = self.pga[np.ix_(indices, cell_indices)].copy()
        pga_bypass = self.pgaBypass[np.ix_(indices, cell_indices)].copy()
        if measurement_mode is not None:
            selected_rows = np.asarray([int(cell_index) // 8 for cell_index in cell_indices], dtype=np.int64)
            mode_match = self.rowModes[np.ix_(indices, selected_rows)] == str(measurement_mode).upper()
            # A mode mismatch is an intentional history discontinuity. Values
            # from ohms, volts and pF must never be joined by a trend line.
            values[~mode_match] = np.nan
            valid[~mode_match] = False
            fresh[~mode_match] = False
            expected[~mode_match] = False
            acquired[~mode_match] = False
            error[~mode_match] = False
            raw_fixed[~mode_match] = np.nan
            error_codes[~mode_match] = -1
            error_reasons[~mode_match] = ""
            pga[~mode_match] = -1
            pga_bypass[~mode_match] = False
        return HistorySlice(
            seq=self.seq[indices].copy(),
            timeSeconds=self.timeSeconds[indices].copy(),
            values=values,
            valid=valid,
            fresh=fresh,
            expected=expected,
            acquired=acquired,
            expectedKnown=expected_known,
            acquiredKnown=acquired_known,
            freshKnown=fresh_known,
            error=error,
            rows=self.rows[indices].copy(),
            modes=self.modes[indices].copy(),
            units=self.units[indices].copy(),
            sources=self.sources[indices].copy(),
            scales=self.scales[indices].copy(),
            rawFixed=raw_fixed,
            errorCodes=error_codes,
            errorReasons=error_reasons,
            pga=pga,
            pgaBypass=pga_bypass,
            generations=self.generations[indices].copy(),
            requestIds=self.requestIds[indices].copy(),
            rowModes=self.rowModes[indices, :].copy(),
            rowUnits=self.rowUnits[indices, :].copy(),
            rowScales=self.rowScales[indices, :].copy(),
            connectionGenerations=self.connectionGenerations[indices].copy(),
            bootIds=self.bootIds[indices].copy(),
            deviceTimestampUs=self.deviceTimestampUs[indices].copy(),
            hostWallTimes=self.hostWallTimes[indices].copy(),
            hostMonotonicNs=self.hostMonotonicNs[indices].copy(),
            rowsGenerations=self.rowsGenerations[indices].copy(),
            rowsRequestIds=self.rowsRequestIds[indices].copy(),
            modeGenerations=self.modeGenerations[indices].copy(),
            modeRequestIds=self.modeRequestIds[indices].copy(),
            profileGenerations=self.profileGenerations[indices].copy(),
            profileRequestIds=self.profileRequestIds[indices].copy(),
            wireProfiles=self.wireProfiles[indices].copy(),
            revision=self.revision,
        )

    def clear(self) -> None:
        self.seq.fill(-1)
        self.timeSeconds.fill(np.nan)
        self.values.fill(np.nan)
        self.valid.fill(False)
        self.fresh.fill(False)
        self.expected.fill(False)
        self.acquired.fill(False)
        self.expectedKnown.fill(False)
        self.acquiredKnown.fill(False)
        self.freshKnown.fill(False)
        self.error.fill(False)
        self.rows.fill(0)
        self.modes.fill("CAP")
        self.units.fill("pF")
        self.sources.fill("")
        self.scales.fill(-6)
        self.rawFixed.fill(np.nan)
        self.errorCodes.fill(-1)
        self.errorReasons.fill("")
        self.pga.fill(-1)
        self.pgaBypass.fill(False)
        self.generations.fill(-1)
        self.requestIds.fill(-1)
        self.rowModes.fill("CAP")
        self.rowUnits.fill("pF")
        self.rowScales.fill(-6)
        self.connectionGenerations.fill(0)
        self.bootIds.fill(-1)
        self.deviceTimestampUs.fill(-1)
        self.hostWallTimes.fill(np.nan)
        self.hostMonotonicNs.fill(-1)
        self.rowsGenerations.fill(-1)
        self.rowsRequestIds.fill(-1)
        self.modeGenerations.fill(-1)
        self.modeRequestIds.fill(-1)
        self.profileGenerations.fill(-1)
        self.profileRequestIds.fill(-1)
        self.wireProfiles.fill("")
        self.railValid.fill(False)
        self.railFresh.fill(False)
        self.railAge.fill(-1)
        self.avddUv.fill(-1)
        self.avssUv.fill(_SIGNED_INT_MISSING)
        self.railSpanUv.fill(-1)
        self.railSource.fill("")
        self.railReason.fill("")
        self.writeIndex = 0
        self.frameCount = 0
        self.totalFrames = 0
        self.revision += 1


def _known_mask(value: np.ndarray | bool | None, default: bool) -> np.ndarray:
    if value is None:
        return np.full(64, bool(default), dtype=bool)
    if isinstance(value, (bool, np.bool_)):
        return np.full(64, bool(value), dtype=bool)
    return np.asarray(value, dtype=bool).reshape(64)
