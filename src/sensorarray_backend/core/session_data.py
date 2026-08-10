from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

SESSION_DATA_FORMATS = {"csv", "xlsx", "mat", "h5"}
SESSION_SCHEMA_VERSION = 2

CSV_COLUMNS = [
    "schemaVersion",
    "frameIndex",
    "measurementMode",
    "unit",
    "scale",
    "seq",
    "timeSeconds",
    "rows",
    "cell",
    "row",
    "col",
    "rawFixed",
    "physicalValue",
    "valid",
    "fresh",
    "errorCode",
    "pga",
    "generation",
    "requestId",
    "source",
    # Quantity-specific aliases keep files self-describing and preserve the
    # established CAP column for existing analysis scripts.
    "correctedPf",
    "valueUv",
    "valueV",
    "valueMilliOhm",
    "valueOhm",
]


@dataclass(frozen=True)
class SessionFrame:
    seq: int
    timeSeconds: float
    rows: int
    valuesPf: np.ndarray
    valid: np.ndarray
    source: str = "history"
    measurementMode: str = "CAP"
    unit: str = "pF"
    scale: int = -6
    physicalValues: np.ndarray | None = None
    rawFixed: np.ndarray | None = None
    fresh: np.ndarray | None = None
    errorCodes: np.ndarray | None = None
    pga: np.ndarray | None = None
    generation: int | None = None
    requestId: int | None = None

    @property
    def mode(self) -> str:
        return _mode_value(self.measurementMode)

    def physical_values(self) -> np.ndarray:
        source = self.physicalValues if self.physicalValues is not None else self.valuesPf
        return np.asarray(source, dtype=np.float64).reshape(64)

    def values_matrix(self) -> np.ndarray:
        return self.physical_values().reshape(8, 8)

    def valid_matrix(self) -> np.ndarray:
        return np.asarray(self.valid, dtype=bool).reshape(8, 8)

    def fresh_values(self) -> np.ndarray:
        return (
            np.asarray(self.fresh, dtype=bool).reshape(64)
            if self.fresh is not None
            else np.asarray(self.valid, dtype=bool).reshape(64)
        )

    def raw_fixed_values(self) -> np.ndarray:
        if self.rawFixed is not None:
            return np.asarray(self.rawFixed, dtype=np.float64).reshape(64)
        values = self.physical_values()
        factor = 10.0 ** int(self.scale)
        return np.where(np.isfinite(values), np.rint(values / factor), np.nan)

    def error_code_values(self) -> np.ndarray:
        if self.errorCodes is not None:
            return np.asarray(self.errorCodes, dtype=np.int16).reshape(64)
        return np.where(np.asarray(self.valid, dtype=bool).reshape(64), -1, 20).astype(np.int16)

    def pga_values(self) -> np.ndarray:
        return np.asarray(self.pga, dtype=np.int16).reshape(64) if self.pga is not None else np.full(64, -1, dtype=np.int16)


@dataclass(frozen=True)
class SessionModel:
    metadata: dict[str, Any]
    display: dict[str, Any]
    offsetsPf: np.ndarray
    currentMatrix: np.ndarray
    currentValidMask: np.ndarray
    frames: list[SessionFrame]
    rawLogs: list[dict[str, Any]]

    @property
    def currentMatrixPf(self) -> np.ndarray:
        return self.currentMatrix


def normalise_session_format(value: str | None) -> str:
    fmt = str(value or "h5").strip().lower().lstrip(".")
    if fmt not in SESSION_DATA_FORMATS:
        raise ValueError("session export format must be csv, xlsx, mat, or h5")
    return fmt


def session_model_from_payload(payload: dict[str, Any]) -> SessionModel:
    current = payload.get("currentMatrix") if isinstance(payload.get("currentMatrix"), dict) else {}
    current_values = _matrix_from_json(current.get("values") if current.get("values") is not None else current.get("correctedPf"))
    current_valid = _bool_matrix_from_json(current.get("valid") if current.get("valid") is not None else current.get("validMask"))
    offsets = _matrix_from_json(payload.get("offsetsPf"), default=0.0)
    frames = _frames_from_payload(payload, current_values, current_valid)
    return SessionModel(
        metadata={"schemaVersion": SESSION_SCHEMA_VERSION, **dict(payload.get("metadata") or {})},
        display=dict(payload.get("display") or {}),
        offsetsPf=offsets,
        currentMatrix=current_values,
        currentValidMask=current_valid,
        frames=frames,
        rawLogs=[row for row in payload.get("rawLogs", []) if isinstance(row, dict)],
    )


def export_session_bytes(payload: dict[str, Any], fmt: str) -> tuple[bytes, str, str]:
    selected = normalise_session_format(fmt)
    model = session_model_from_payload(payload)
    if selected == "csv":
        return _export_csv(model), "text/csv; charset=utf-8", "csv"
    if selected == "xlsx":
        return _export_xlsx(model), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
    if selected == "mat":
        return _export_mat(model), "application/octet-stream", "mat"
    return _export_h5(model), "application/x-hdf5", "h5"


def load_session_frames(path: str | Path) -> list[SessionFrame]:
    source = Path(path)
    suffix = source.suffix.lower().lstrip(".")
    if suffix == "csv":
        return _load_csv(source)
    if suffix == "xlsx":
        return _load_xlsx(source)
    if suffix == "mat":
        return _load_mat(source)
    if suffix in {"h5", "hdf5"}:
        return _load_h5(source)
    raise ValueError("session import format must be csv, xlsx, mat, or h5")


def frames_to_measurement_ascii_bytes(frames: list[SessionFrame]) -> bytes:
    from sensorarray_app.constants import CAP_FIXED_SCALE, CAP_INVALID_SENTINEL, FDC_CIRCUIT_OFFSET_PF
    from sensorarray_app.protocol.crc import crc32_reflected

    out = bytearray()
    previous_mode: str | None = None
    for frame in frames:
        rows = max(1, min(8, int(frame.rows)))
        cells = rows * 8
        mode = frame.mode
        values = frame.physical_values()
        valid = np.asarray(frame.valid, dtype=bool).reshape(64)
        fresh = frame.fresh_values()
        raw_fixed = frame.raw_fixed_values()
        error_codes = frame.error_code_values()
        generation = int(frame.generation if frame.generation is not None else 1)
        request_id = int(frame.requestId if frame.requestId is not None else 1)
        timestamp_us = int(float(frame.timeSeconds) * 1_000_000)
        if previous_mode is not None and mode != previous_mode:
            # Exported measurement frames do not carry the asynchronous mode
            # events that originally surrounded them.  Recreate an explicit
            # offline transaction so Replay follows the same MACK -> MAPP ->
            # generation-gated store path as a live session.  CAP's frame
            # gen/rid remain ROWS metadata; the store intentionally gates CAP
            # by tag and boundary sequence only.
            out.extend(
                f"MACK,id={request_id},old={previous_mode},new={mode},state=accepted\n".encode("ascii")
            )
            out.extend(
                f"MAPP,id={request_id},gen={generation},old={previous_mode},new={mode},"
                f"seq={int(frame.seq)},state=applied,transitionUs=0\n".encode("ascii")
            )
        if mode == "CAP":
            fixed: list[int] = []
            for index in range(cells):
                value = values[index]
                if not bool(valid[index]) or not np.isfinite(value):
                    fixed.append(CAP_INVALID_SENTINEL)
                elif np.isfinite(raw_fixed[index]):
                    fixed.append(int(raw_fixed[index]))
                else:
                    fixed.append(int(round((float(value) + FDC_CIRCUIT_OFFSET_PF) * CAP_FIXED_SCALE)))
            # CAP freshness is a row/device acquisition property independent
            # of whether a particular cell converted to a valid capacitance.
            # Folding validity into this mask would incorrectly make every
            # cell in a row stale after round-tripping one invalid cell.
            primary_mask = _device_row_mask(fresh, rows, 0, 4)
            secondary_mask = _device_row_mask(fresh, rows, 4, 8)
            row_mask = primary_mask | secondary_mask
            header = (
                f"C,seq={int(frame.seq)},ts={timestamp_us},rows={rows},cells={cells},gen={generation},rid={request_id},"
                f"rf={row_mask:02X},pf={primary_mask:02X},sf={secondary_mask:02X},"
                f"bad=0/0/{int((~valid[:cells]).sum())},fmt=pf6,n={cells}\n"
            ).encode("ascii")
            body = bytearray(header)
            for line_index, start in enumerate(range(0, cells, 16)):
                body.extend(f"D{line_index},{','.join(str(value) for value in fixed[start:start + 16])}\n".encode("ascii"))
        else:
            unit = "V" if mode == "VOLT" else "ohm"
            scale = -6 if mode == "VOLT" else -3
            format_name = "uv-x" if mode == "VOLT" else "mohm-x"
            valid_mask = _bit_mask(valid, cells)
            fresh_mask = _bit_mask(fresh, cells)
            error_mask = _bit_mask(error_codes >= 0, cells)
            reference = "AVDD_AVSS" if mode == "VOLT" else "INTREF"
            header = (
                f"{'V' if mode == 'VOLT' else 'R'},seq={int(frame.seq)},ts={timestamp_us},rows={rows},cells={cells},"
                f"gen={generation},rid={request_id},mode={mode},unit={unit},scale={scale},valid={valid_mask:016X},"
                f"fresh={fresh_mask:016X},error={error_mask:016X},ref={reference},rail=0,age=0,avdd=0,avss=0,"
                f"vexc=0,rref=0,dur=0,tr=0,gc=0,ov=0,aa=0,fb=0,ir=0,to=0,st=0,spi=0,fmt={format_name},"
                f"n={cells},bad={int((~valid[:cells]).sum())}\n"
            ).encode("ascii")
            body = bytearray(header)
            for line_index, start in enumerate(range(0, cells, 16)):
                tokens: list[str] = []
                for index in range(start, min(start + 16, cells)):
                    if not bool(valid[index]) or not np.isfinite(raw_fixed[index]):
                        code = int(error_codes[index]) if int(error_codes[index]) >= 0 else 20
                        tokens.append(f"X{code & 0xFF:02X}")
                    else:
                        tokens.append(str(int(raw_fixed[index])))
                body.extend(f"D{line_index},{','.join(tokens)}\n".encode("ascii"))
            pga = frame.pga_values()
            for line_index, start in enumerate(range(0, cells, 16)):
                packed = "".join(f"{(int(pga[index]) if int(pga[index]) in {0, 1, 2, 4, 8, 16, 32} else 1):02X}" for index in range(start, min(start + 16, cells)))
                body.extend(f"P{line_index},{packed}\n".encode("ascii"))
        crc = crc32_reflected(bytes(body))
        body.extend(f"K,seq={int(frame.seq)},gen={generation},rid={request_id},crc={crc:08X}\n".encode("ascii"))
        out.extend(body)
        previous_mode = mode
    return bytes(out)


def frames_to_cap_ascii_bytes(frames: list[SessionFrame]) -> bytes:
    """Compatibility name retained; modern frames are emitted as C/V/R."""

    return frames_to_measurement_ascii_bytes(frames)


def _frames_from_payload(payload: dict[str, Any], current_values: np.ndarray, current_valid: np.ndarray) -> list[SessionFrame]:
    history_frames = payload.get("historyFrames")
    frames = [
        _frame_from_history_dict(index, frame)
        for index, frame in enumerate(history_frames or [])
        if isinstance(frame, dict)
    ]
    if frames:
        return frames
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    current = payload.get("currentMatrix") if isinstance(payload.get("currentMatrix"), dict) else {}
    mode = _mode_value(current.get("mode") or metadata.get("measurementMode") or "CAP")
    unit, scale = _mode_unit_scale(mode)
    return [
        SessionFrame(
            seq=_int_value(metadata.get("frameSeq"), 1),
            timeSeconds=0.0,
            rows=_rows_value(metadata.get("rows"), 8),
            valuesPf=current_values.reshape(64) if mode == "CAP" else np.full(64, np.nan),
            valid=current_valid.reshape(64),
            source="current",
            measurementMode=mode,
            unit=unit,
            scale=scale,
            physicalValues=current_values.reshape(64),
            rawFixed=_flat_numeric_matrix(current.get("rawFixed")),
            fresh=_flat_bool_matrix(current.get("fresh"), default=current_valid.reshape(64)),
            errorCodes=_flat_int_matrix(current.get("errorCodes"), -1),
            pga=_flat_int_matrix(current.get("pga"), -1),
            generation=_optional_int(current.get("generation")),
            requestId=_optional_int(current.get("requestId")),
        )
    ]


def _frame_from_history_dict(index: int, frame: dict[str, Any]) -> SessionFrame:
    mode = _mode_value(frame.get("measurementMode") or frame.get("mode") or "CAP")
    unit, default_scale = _mode_unit_scale(mode)
    raw_values = frame.get("physicalValues")
    if raw_values is None:
        raw_values = frame.get("values")
    if raw_values is None:
        raw_values = frame.get("valuesPf")
    values = _flat_numeric_list(raw_values)
    valid = _flat_bool_list(frame.get("valid"), default=np.isfinite(values))
    return SessionFrame(
        seq=_int_value(frame.get("seq"), index + 1),
        timeSeconds=_float_value(frame.get("timeSeconds"), float(index)),
        rows=_rows_value(frame.get("rows"), 8),
        valuesPf=values if mode == "CAP" else np.full(64, np.nan),
        valid=valid,
        source=str(frame.get("source") or "history"),
        measurementMode=mode,
        unit=str(frame.get("unit") or unit),
        scale=_int_value(frame.get("scale"), default_scale),
        physicalValues=values,
        rawFixed=_flat_numeric_list(frame.get("rawFixed")),
        fresh=_flat_bool_list(frame.get("fresh"), default=valid),
        errorCodes=_flat_int_list(frame.get("errorCodes"), -1),
        pga=_flat_int_list(frame.get("pga"), -1),
        generation=_optional_int(frame.get("generation")),
        requestId=_optional_int(frame.get("requestId")),
    )


def _export_csv(model: SessionModel) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    writer.writerows(_iter_frame_rows(model.frames))
    return output.getvalue().encode("utf-8")


def _export_xlsx(model: SessionModel) -> bytes:
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise RuntimeError("openpyxl is required for XLSX export") from exc
    workbook = Workbook()
    metadata_sheet = workbook.active
    metadata_sheet.title = "metadata"
    metadata_sheet.append(["key", "value"])
    for key, value in model.metadata.items():
        metadata_sheet.append([key, _string_value(value)])
    frames_sheet = workbook.create_sheet("frames")
    frames_sheet.append(CSV_COLUMNS)
    for row in _iter_frame_rows(model.frames):
        frames_sheet.append([row[column] for column in CSV_COLUMNS])
    offsets_sheet = workbook.create_sheet("cap_offsets")
    offsets_sheet.append(["row", "col", "offsetPf"])
    for row_index in range(8):
        for col_index in range(8):
            offsets_sheet.append([row_index + 1, col_index + 1, float(model.offsetsPf[row_index, col_index])])
    logs_sheet = workbook.create_sheet("raw_logs")
    logs_sheet.append(["timestamp", "source", "channel", "tag", "severity", "rawText"])
    for row in model.rawLogs:
        logs_sheet.append([row.get("timestamp"), row.get("source"), row.get("channel"), row.get("tag"), row.get("severity"), row.get("rawText")])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _export_mat(model: SessionModel) -> bytes:
    try:
        from scipy.io import savemat
    except ImportError as exc:
        raise RuntimeError("scipy is required for MAT export") from exc
    arrays = _frame_arrays(model.frames)
    payload: dict[str, Any] = {
        "session_schema_version": np.asarray([SESSION_SCHEMA_VERSION], dtype=np.int16),
        "current_matrix": model.currentMatrix,
        "current_valid_mask": model.currentValidMask.astype(np.uint8),
        "offsets_pf": model.offsetsPf,
        **arrays,
    }
    if all(frame.mode == "CAP" for frame in model.frames):
        payload["current_matrix_pf"] = model.currentMatrix
        payload["frames_values_pf"] = arrays["frames_physical_values"]
    output = io.BytesIO()
    savemat(output, payload)
    return output.getvalue()


def _export_h5(model: SessionModel) -> bytes:
    try:
        import h5py
    except ImportError as exc:
        raise RuntimeError("h5py is required for HDF5 export") from exc
    arrays = _frame_arrays(model.frames)
    output = io.BytesIO()
    with h5py.File(output, "w") as handle:
        metadata = handle.create_group("metadata")
        metadata.attrs["schema_version"] = SESSION_SCHEMA_VERSION
        for key, value in model.metadata.items():
            metadata.attrs[str(key)] = _string_value(value)
        current = handle.create_group("current")
        current.create_dataset("matrix", data=model.currentMatrix)
        current.create_dataset("valid_mask", data=model.currentValidMask.astype(np.uint8))
        frames = handle.create_group("frames")
        for name, value in arrays.items():
            if name in {"frame_mode", "frame_unit", "frame_source"}:
                dataset_name = name.removeprefix("frame_")
                frames.create_dataset(dataset_name, data=np.asarray(value, dtype="S32"))
            elif name.startswith("frame_"):
                frames.create_dataset(name.removeprefix("frame_"), data=value)
            elif name.startswith("frames_"):
                frames.create_dataset(name.removeprefix("frames_"), data=value)
        offsets = handle.create_group("capacitance")
        offsets.create_dataset("offsets_pf", data=model.offsetsPf)
        if all(frame.mode == "CAP" for frame in model.frames):
            current.create_dataset("matrix_pf", data=model.currentMatrix)
            frames.create_dataset("values_pf", data=arrays["frames_physical_values"])
    return output.getvalue()


def _load_csv(path: Path) -> list[SessionFrame]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        required = {"frameIndex", "seq", "rows", "row", "col", "valid"}
        missing = sorted(required - fields)
        if missing or not ({"physicalValue", "correctedPf"} & fields):
            detail = missing or ["physicalValue or correctedPf"]
            raise ValueError(f"CSV missing required columns: {', '.join(detail)}")
        return _rows_to_frames(reader)


def _load_xlsx(path: Path) -> list[SessionFrame]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("openpyxl is required for XLSX import") from exc
    workbook = load_workbook(path, read_only=True, data_only=True)
    if "frames" not in workbook.sheetnames:
        raise ValueError("XLSX missing required sheet: frames")
    rows = list(workbook["frames"].iter_rows(values_only=True))
    if not rows:
        raise ValueError("XLSX frames sheet is empty")
    header = [str(value or "") for value in rows[0]]
    return _rows_to_frames(dict(zip(header, row, strict=False)) for row in rows[1:])


def _load_mat(path: Path) -> list[SessionFrame]:
    try:
        from scipy.io import loadmat
    except ImportError as exc:
        raise RuntimeError("scipy is required for MAT import") from exc
    payload = loadmat(path)
    if "frames_physical_values" in payload:
        return _arrays_to_frames(
            payload["frames_physical_values"], payload["frames_valid_mask"], payload.get("frames_fresh_mask"),
            payload.get("frames_raw_fixed"), payload.get("frames_error_codes"), payload.get("frames_pga"),
            payload["frame_seq"].ravel(), payload["frame_time_seconds"].ravel(), payload["frame_rows"].ravel(),
            payload.get("frame_mode"), payload.get("frame_unit"), payload.get("frame_scale"), payload.get("frame_source"),
            payload.get("frame_generation"), payload.get("frame_request_id"),
        )
    required = ["frames_values_pf", "frames_valid_mask", "frame_seq", "frame_time_seconds", "frame_rows"]
    missing = [name for name in required if name not in payload]
    if missing:
        raise ValueError(f"MAT missing required variables: {', '.join(missing)}")
    return _arrays_to_frames(
        payload["frames_values_pf"], payload["frames_valid_mask"], None, None, None, None,
        payload["frame_seq"].ravel(), payload["frame_time_seconds"].ravel(), payload["frame_rows"].ravel(),
        None, None, None, None, None, None,
    )


def _load_h5(path: Path) -> list[SessionFrame]:
    try:
        import h5py
    except ImportError as exc:
        raise RuntimeError("h5py is required for HDF5 import") from exc
    with h5py.File(path, "r") as handle:
        if "frames/physical_values" in handle:
            return _arrays_to_frames(
                handle["frames/physical_values"][()], handle["frames/valid_mask"][()],
                _h5_optional(handle, "frames/fresh_mask"), _h5_optional(handle, "frames/raw_fixed"),
                _h5_optional(handle, "frames/error_codes"), _h5_optional(handle, "frames/pga"),
                handle["frames/seq"][()], handle["frames/time_seconds"][()], handle["frames/rows"][()],
                _h5_optional(handle, "frames/mode"), _h5_optional(handle, "frames/unit"),
                _h5_optional(handle, "frames/scale"), _h5_optional(handle, "frames/source"),
                _h5_optional(handle, "frames/generation"), _h5_optional(handle, "frames/request_id"),
            )
        required = ["frames/values_pf", "frames/valid_mask", "frames/seq", "frames/time_seconds", "frames/rows"]
        missing = [name for name in required if name not in handle]
        if missing:
            raise ValueError(f"HDF5 missing required datasets: {', '.join(missing)}")
        return _arrays_to_frames(
            handle["frames/values_pf"][()], handle["frames/valid_mask"][()], None, None, None, None,
            handle["frames/seq"][()], handle["frames/time_seconds"][()], handle["frames/rows"][()],
            None, None, None, None, None, None,
        )


def _frame_arrays(frames: list[SessionFrame]) -> dict[str, np.ndarray]:
    return {
        "frames_physical_values": np.stack([frame.values_matrix() for frame in frames], axis=0),
        "frames_raw_fixed": np.stack([frame.raw_fixed_values().reshape(8, 8) for frame in frames], axis=0),
        "frames_valid_mask": np.stack([frame.valid_matrix().astype(np.uint8) for frame in frames], axis=0),
        "frames_fresh_mask": np.stack([frame.fresh_values().reshape(8, 8).astype(np.uint8) for frame in frames], axis=0),
        "frames_error_codes": np.stack([frame.error_code_values().reshape(8, 8) for frame in frames], axis=0),
        "frames_pga": np.stack([frame.pga_values().reshape(8, 8) for frame in frames], axis=0),
        "frame_seq": np.asarray([frame.seq for frame in frames], dtype=np.int64),
        "frame_time_seconds": np.asarray([frame.timeSeconds for frame in frames], dtype=np.float64),
        "frame_rows": np.asarray([frame.rows for frame in frames], dtype=np.int16),
        "frame_mode": np.asarray([frame.mode for frame in frames], dtype="U4"),
        "frame_unit": np.asarray([frame.unit for frame in frames], dtype="U8"),
        "frame_scale": np.asarray([frame.scale for frame in frames], dtype=np.int16),
        "frame_source": np.asarray([frame.source for frame in frames], dtype="U32"),
        "frame_generation": np.asarray([-1 if frame.generation is None else frame.generation for frame in frames], dtype=np.int64),
        "frame_request_id": np.asarray([-1 if frame.requestId is None else frame.requestId for frame in frames], dtype=np.int64),
    }


def _arrays_to_frames(
    values: Any, valid: Any, fresh: Any, raw_fixed: Any, error_codes: Any, pga: Any,
    seq: Any, time_seconds: Any, rows: Any, modes: Any, units: Any, scales: Any, sources: Any,
    generations: Any, request_ids: Any,
) -> list[SessionFrame]:
    values_array = np.asarray(values, dtype=np.float64)
    valid_array = np.asarray(valid, dtype=bool)
    if values_array.ndim != 3 or values_array.shape[1:] != (8, 8):
        raise ValueError("frames physical values must have shape Nx8x8")
    if valid_array.shape != values_array.shape:
        raise ValueError("frames valid mask must match physical values")
    count = values_array.shape[0]
    frames: list[SessionFrame] = []
    for index in range(count):
        mode = _array_mode(modes, index)
        unit, default_scale = _mode_unit_scale(mode)
        generation = _array_optional_int(generations, index)
        request_id = _array_optional_int(request_ids, index)
        frames.append(
            SessionFrame(
                seq=_int_value(seq[index] if index < len(seq) else index + 1, index + 1),
                timeSeconds=_float_value(time_seconds[index] if index < len(time_seconds) else index, float(index)),
                rows=_rows_value(rows[index] if index < len(rows) else 8, 8),
                valuesPf=values_array[index].reshape(64) if mode == "CAP" else np.full(64, np.nan),
                valid=valid_array[index].reshape(64),
                measurementMode=mode,
                unit=_array_string(units, index, unit),
                scale=_array_int(scales, index, default_scale),
                physicalValues=values_array[index].reshape(64),
                rawFixed=_array_matrix(raw_fixed, index, np.nan),
                fresh=_array_matrix(fresh, index, valid_array[index]).astype(bool),
                errorCodes=_array_matrix(error_codes, index, -1).astype(np.int16),
                pga=_array_matrix(pga, index, -1).astype(np.int16),
                source=_array_string(sources, index, "history"),
                generation=generation,
                requestId=request_id,
            )
        )
    if not frames:
        raise ValueError("session file contains no frames")
    return frames


def _rows_to_frames(rows: Iterable[dict[str, Any]]) -> list[SessionFrame]:
    groups: dict[int, dict[str, Any]] = {}
    for row in rows:
        frame_index = _int_value(row.get("frameIndex"), 0)
        mode = _mode_value(row.get("measurementMode") or "CAP")
        unit, default_scale = _mode_unit_scale(mode)
        group = groups.setdefault(
            frame_index,
            {
                "seq": _int_value(row.get("seq"), frame_index + 1), "time": _float_value(row.get("timeSeconds"), float(frame_index)),
                "rows": _rows_value(row.get("rows"), 8), "mode": mode, "unit": str(row.get("unit") or unit),
                "scale": _int_value(row.get("scale"), default_scale), "source": str(row.get("source") or "import"),
                "values": np.full(64, np.nan), "raw": np.full(64, np.nan), "valid": np.zeros(64, dtype=bool),
                "fresh": np.zeros(64, dtype=bool), "error": np.full(64, -1, dtype=np.int16), "pga": np.full(64, -1, dtype=np.int16),
                "generation": _optional_int(row.get("generation")), "requestId": _optional_int(row.get("requestId")),
            },
        )
        row_index, col_index = _row_index(row.get("row")), _col_index(row.get("col"))
        cell_index = row_index * 8 + col_index
        physical = _json_number(row.get("physicalValue"))
        if physical is None:
            physical = _json_number(row.get("correctedPf"))
        if physical is not None:
            group["values"][cell_index] = physical
        raw = _json_number(row.get("rawFixed"))
        if raw is not None:
            group["raw"][cell_index] = raw
        group["valid"][cell_index] = _bool_value(row.get("valid"))
        group["fresh"][cell_index] = _bool_value(row.get("fresh")) if row.get("fresh") not in {None, ""} else group["valid"][cell_index]
        group["error"][cell_index] = _int_value(row.get("errorCode"), -1)
        group["pga"][cell_index] = _int_value(row.get("pga"), -1)
    frames: list[SessionFrame] = []
    for _, group in sorted(groups.items()):
        mode = group["mode"]
        frames.append(
            SessionFrame(
                seq=group["seq"], timeSeconds=group["time"], rows=group["rows"],
                valuesPf=group["values"] if mode == "CAP" else np.full(64, np.nan), valid=group["valid"], source=group["source"],
                measurementMode=mode, unit=group["unit"], scale=group["scale"], physicalValues=group["values"], rawFixed=group["raw"],
                fresh=group["fresh"], errorCodes=group["error"], pga=group["pga"], generation=group["generation"], requestId=group["requestId"],
            )
        )
    if not frames:
        raise ValueError("session file contains no frames")
    return frames


def _iter_frame_rows(frames: list[SessionFrame]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for frame_index, frame in enumerate(frames):
        values, raw = frame.physical_values(), frame.raw_fixed_values()
        valid, fresh = np.asarray(frame.valid, dtype=bool).reshape(64), frame.fresh_values()
        errors, pga = frame.error_code_values(), frame.pga_values()
        for row_index in range(8):
            for col_index in range(8):
                index = row_index * 8 + col_index
                physical = _blank_nan(values[index])
                raw_value = _blank_nan(raw[index])
                output.append(
                    {
                        "schemaVersion": SESSION_SCHEMA_VERSION, "frameIndex": frame_index, "measurementMode": frame.mode,
                        "unit": frame.unit, "scale": frame.scale, "seq": frame.seq, "timeSeconds": frame.timeSeconds,
                        "rows": frame.rows, "cell": _cell_label(row_index, col_index), "row": row_index + 1, "col": col_index + 1,
                        "rawFixed": raw_value, "physicalValue": physical, "valid": bool(valid[index]), "fresh": bool(fresh[index]),
                        "errorCode": "" if int(errors[index]) < 0 else int(errors[index]), "pga": "" if int(pga[index]) < 0 else int(pga[index]),
                        "generation": "" if frame.generation is None else frame.generation, "requestId": "" if frame.requestId is None else frame.requestId,
                        "source": frame.source, "correctedPf": physical if frame.mode == "CAP" else "",
                        "valueUv": raw_value if frame.mode == "VOLT" else "", "valueV": physical if frame.mode == "VOLT" else "",
                        "valueMilliOhm": raw_value if frame.mode == "RES" else "", "valueOhm": physical if frame.mode == "RES" else "",
                    }
                )
    return output


def _mode_value(value: Any) -> str:
    mode = str(value or "CAP").strip().upper()
    mode = {"CAPACITANCE": "CAP", "VOLTAGE": "VOLT", "RESISTANCE": "RES"}.get(mode, mode)
    if mode not in {"CAP", "VOLT", "RES"}:
        raise ValueError(f"unsupported measurement mode: {value}")
    return mode


def _mode_unit_scale(mode: str) -> tuple[str, int]:
    return {"CAP": ("pF", -6), "VOLT": ("V", -6), "RES": ("ohm", -3)}[_mode_value(mode)]


def _bit_mask(values: np.ndarray, cells: int) -> int:
    mask = 0
    for index, value in enumerate(np.asarray(values, dtype=bool).reshape(64)[:cells]):
        if value:
            mask |= 1 << index
    return mask


def _device_row_mask(values: np.ndarray, rows: int, column_start: int, column_end: int) -> int:
    """Return a conservative CAP freshness mask for one four-channel FDC.

    Invalid values do not affect freshness.  Live CAP exports always contain
    one common bit for the four cells; requiring all four also avoids turning
    an unrepresentable hand-authored partial group into falsely fresh data.
    """

    mask = 0
    cells = np.asarray(values, dtype=bool).reshape(64)
    for row_index in range(rows):
        start = row_index * 8 + column_start
        end = row_index * 8 + column_end
        if bool(cells[start:end].all()):
            mask |= 1 << row_index
    return mask


def _flat_numeric_list(value: Any) -> np.ndarray:
    out = np.full(64, np.nan)
    if isinstance(value, (list, tuple, np.ndarray)):
        for index, item in enumerate(np.asarray(value, dtype=object).reshape(-1)[:64]):
            number = _json_number(item)
            if number is not None:
                out[index] = number
    return out


def _flat_bool_list(value: Any, default: np.ndarray) -> np.ndarray:
    if not isinstance(value, (list, tuple, np.ndarray)):
        return np.asarray(default, dtype=bool).reshape(64).copy()
    out = np.zeros(64, dtype=bool)
    flat = np.asarray(value, dtype=object).reshape(-1)
    for index, item in enumerate(flat[:64]):
        out[index] = bool(item)
    return out


def _flat_int_list(value: Any, fill: int) -> np.ndarray:
    out = np.full(64, fill, dtype=np.int16)
    if isinstance(value, (list, tuple, np.ndarray)):
        for index, item in enumerate(np.asarray(value, dtype=object).reshape(-1)[:64]):
            if item not in {None, ""}:
                out[index] = _int_value(item, fill)
    return out


def _matrix_from_json(value: Any, default: float = np.nan) -> np.ndarray:
    matrix = np.full((8, 8), default, dtype=np.float64)
    if not isinstance(value, list):
        return matrix
    for row_index, row in enumerate(value[:8]):
        if isinstance(row, list):
            for col_index, item in enumerate(row[:8]):
                number = _json_number(item)
                if number is not None:
                    matrix[row_index, col_index] = number
    return matrix


def _bool_matrix_from_json(value: Any) -> np.ndarray:
    matrix = np.zeros((8, 8), dtype=bool)
    if isinstance(value, list):
        for row_index, row in enumerate(value[:8]):
            if isinstance(row, list):
                for col_index, item in enumerate(row[:8]):
                    matrix[row_index, col_index] = bool(item)
    return matrix


def _flat_numeric_matrix(value: Any) -> np.ndarray:
    return _matrix_from_json(value).reshape(64)


def _flat_bool_matrix(value: Any, default: np.ndarray) -> np.ndarray:
    return _bool_matrix_from_json(value).reshape(64) if isinstance(value, list) else np.asarray(default, dtype=bool).reshape(64)


def _flat_int_matrix(value: Any, fill: int) -> np.ndarray:
    if not isinstance(value, list):
        return np.full(64, fill, dtype=np.int16)
    out = np.full((8, 8), fill, dtype=np.int16)
    for row_index, row in enumerate(value[:8]):
        if isinstance(row, list):
            for col_index, item in enumerate(row[:8]):
                if item is not None:
                    out[row_index, col_index] = _int_value(item, fill)
    return out.reshape(64)


def _h5_optional(handle: Any, name: str) -> Any:
    return handle[name][()] if name in handle else None


def _array_matrix(value: Any, index: int, default: Any) -> np.ndarray:
    if value is None:
        return np.asarray(default if np.asarray(default).shape == (8, 8) else np.full((8, 8), default)).reshape(64)
    array = np.asarray(value)
    return array[index].reshape(64)


def _array_mode(value: Any, index: int) -> str:
    if value is None:
        return "CAP"
    flat = np.asarray(value).reshape(-1)
    raw = flat[index] if index < flat.size else "CAP"
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", errors="ignore")
    raw_text = str(raw).strip("[]' ")
    return _mode_value(raw_text or "CAP")


def _array_string(value: Any, index: int, default: str) -> str:
    if value is None:
        return default
    flat = np.asarray(value).reshape(-1)
    raw = flat[index] if index < flat.size else default
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = str(raw).strip("[]' ")
    return text or default


def _array_int(value: Any, index: int, default: int) -> int:
    if value is None:
        return default
    flat = np.asarray(value).reshape(-1)
    return _int_value(flat[index] if index < flat.size else default, default)


def _array_optional_int(value: Any, index: int) -> int | None:
    parsed = _array_int(value, index, -1)
    return None if parsed < 0 else parsed


def _json_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _blank_nan(value: Any) -> float | str:
    number = _json_number(value)
    return "" if number is None else number


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    return _int_value(value, 0)


def _int_value(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float_value(value: Any, default: float) -> float:
    number = _json_number(value)
    return default if number is None else number


def _rows_value(value: Any, default: int) -> int:
    rows = _int_value(value, default)
    if not 1 <= rows <= 8:
        raise ValueError("rows must be 1..8")
    return rows


def _row_index(value: Any) -> int:
    index = _int_value(value, 0) - 1
    if not 0 <= index < 8:
        raise ValueError("row must be 1..8")
    return index


def _col_index(value: Any) -> int:
    index = _int_value(value, 0) - 1
    if not 0 <= index < 8:
        raise ValueError("col must be 1..8")
    return index


def _cell_label(row_index: int, col_index: int) -> str:
    return f"S{row_index + 1}D{col_index + 1}"


def _string_value(value: Any) -> str:
    return "" if value is None else str(value)
