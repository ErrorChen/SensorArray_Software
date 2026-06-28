from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

SESSION_DATA_FORMATS = {"csv", "xlsx", "mat", "h5"}

CSV_COLUMNS = [
    "frameIndex",
    "seq",
    "timeSeconds",
    "rows",
    "cell",
    "row",
    "col",
    "correctedPf",
    "valid",
    "source",
]


@dataclass(frozen=True)
class SessionFrame:
    seq: int
    timeSeconds: float
    rows: int
    valuesPf: np.ndarray
    valid: np.ndarray
    source: str = "history"

    def values_matrix(self) -> np.ndarray:
        return np.asarray(self.valuesPf, dtype=np.float64).reshape(8, 8)

    def valid_matrix(self) -> np.ndarray:
        return np.asarray(self.valid, dtype=bool).reshape(8, 8)


@dataclass(frozen=True)
class SessionModel:
    metadata: dict[str, Any]
    display: dict[str, Any]
    offsetsPf: np.ndarray
    currentMatrixPf: np.ndarray
    currentValidMask: np.ndarray
    frames: list[SessionFrame]
    rawLogs: list[dict[str, Any]]


def normalise_session_format(value: str | None) -> str:
    fmt = str(value or "h5").strip().lower().lstrip(".")
    if fmt not in SESSION_DATA_FORMATS:
        raise ValueError("session export format must be csv, xlsx, mat, or h5")
    return fmt


def session_model_from_payload(payload: dict[str, Any]) -> SessionModel:
    current_matrix = payload.get("currentMatrix") if isinstance(payload.get("currentMatrix"), dict) else {}
    current_values = _matrix_from_json(current_matrix.get("correctedPf"))
    current_valid = _bool_matrix_from_json(current_matrix.get("validMask"))
    offsets = _matrix_from_json(payload.get("offsetsPf"), default=0.0)
    frames = _frames_from_payload(payload, current_values, current_valid)
    return SessionModel(
        metadata=dict(payload.get("metadata") or {}),
        display=dict(payload.get("display") or {}),
        offsetsPf=offsets,
        currentMatrixPf=current_values,
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


def frames_to_cap_ascii_bytes(frames: list[SessionFrame]) -> bytes:
    from sensorarray_app.constants import CAP_FIXED_SCALE, CAP_INVALID_SENTINEL, FDC_CIRCUIT_OFFSET_PF
    from sensorarray_app.protocol.crc import crc32_reflected

    out = bytearray()
    for frame in frames:
        rows = max(1, min(8, int(frame.rows)))
        cells = rows * 8
        values = np.asarray(frame.valuesPf, dtype=np.float64).reshape(64)
        valid = np.asarray(frame.valid, dtype=bool).reshape(64)
        raw_fixed: list[int] = []
        for index in range(cells):
            value = values[index]
            if not bool(valid[index]) or not np.isfinite(value):
                raw_fixed.append(CAP_INVALID_SENTINEL)
            else:
                raw_fixed.append(int(round((float(value) + FDC_CIRCUIT_OFFSET_PF) * CAP_FIXED_SCALE)))
        header = (
            f"C,seq={int(frame.seq)},ts={int(float(frame.timeSeconds) * 1_000_000)},rows={rows},cells={cells},"
            f"gen=1,rid=1,rf=FF,pf=FF,sf=FF,bad=0/0/0,fmt=pf6,n={cells}\n"
        ).encode("ascii")
        body = bytearray(header)
        for line_index, start in enumerate(range(0, cells, 16)):
            values_text = ",".join(str(value) for value in raw_fixed[start : start + 16])
            body.extend(f"D{line_index},{values_text}\n".encode("ascii"))
        crc = crc32_reflected(bytes(body))
        body.extend(f"K,seq={int(frame.seq)},gen=1,rid=1,crc={crc:08X}\n".encode("ascii"))
        out.extend(body)
    return bytes(out)


def _frames_from_payload(payload: dict[str, Any], current_values: np.ndarray, current_valid: np.ndarray) -> list[SessionFrame]:
    frames: list[SessionFrame] = []
    history_frames = payload.get("historyFrames")
    if isinstance(history_frames, list):
        for index, frame in enumerate(history_frames):
            if isinstance(frame, dict):
                frames.append(_frame_from_history_dict(index, frame))
    if frames:
        return frames
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    rows = _rows_value(metadata.get("rows"), 8)
    seq = _int_value(metadata.get("frameSeq"), 1)
    return [
        SessionFrame(
            seq=seq,
            timeSeconds=0.0,
            rows=rows,
            valuesPf=current_values.reshape(64),
            valid=current_valid.reshape(64),
            source="current",
        )
    ]


def _frame_from_history_dict(index: int, frame: dict[str, Any]) -> SessionFrame:
    values = np.full(64, np.nan, dtype=np.float64)
    valid = np.zeros(64, dtype=bool)
    raw_values = frame.get("valuesPf")
    raw_valid = frame.get("valid")
    if isinstance(raw_values, list):
        for cell_index, value in enumerate(raw_values[:64]):
            number = _json_number(value)
            if number is not None:
                values[cell_index] = number
    if isinstance(raw_valid, list):
        for cell_index, value in enumerate(raw_valid[:64]):
            valid[cell_index] = bool(value)
    else:
        valid = np.isfinite(values)
    return SessionFrame(
        seq=_int_value(frame.get("seq"), index + 1),
        timeSeconds=_float_value(frame.get("timeSeconds"), float(index)),
        rows=_rows_value(frame.get("rows"), 8),
        valuesPf=values,
        valid=valid,
    )


def _export_csv(model: SessionModel) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for row in _iter_frame_rows(model.frames):
        writer.writerow(row)
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

    current_sheet = workbook.create_sheet("current_matrix")
    current_sheet.append(["cell", "row", "col", "correctedPf", "valid"])
    for row_index in range(8):
        for col_index in range(8):
            current_sheet.append(
                [
                    _cell_label(row_index, col_index),
                    row_index + 1,
                    col_index + 1,
                    _blank_nan(model.currentMatrixPf[row_index, col_index]),
                    bool(model.currentValidMask[row_index, col_index]),
                ]
            )

    offsets_sheet = workbook.create_sheet("offsets")
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

    output = io.BytesIO()
    savemat(
        output,
        {
            "current_matrix_pf": model.currentMatrixPf,
            "current_valid_mask": model.currentValidMask.astype(np.uint8),
            "offsets_pf": model.offsetsPf,
            "frames_values_pf": np.stack([frame.values_matrix() for frame in model.frames], axis=0),
            "frames_valid_mask": np.stack([frame.valid_matrix().astype(np.uint8) for frame in model.frames], axis=0),
            "frame_seq": np.asarray([frame.seq for frame in model.frames], dtype=np.int64),
            "frame_time_seconds": np.asarray([frame.timeSeconds for frame in model.frames], dtype=np.float64),
            "frame_rows": np.asarray([frame.rows for frame in model.frames], dtype=np.int16),
        },
    )
    return output.getvalue()


def _export_h5(model: SessionModel) -> bytes:
    try:
        import h5py
    except ImportError as exc:
        raise RuntimeError("h5py is required for HDF5 export") from exc

    output = io.BytesIO()
    with h5py.File(output, "w") as handle:
        metadata = handle.create_group("metadata")
        for key, value in model.metadata.items():
            metadata.attrs[str(key)] = _string_value(value)
        current = handle.create_group("current")
        current.create_dataset("matrix_pf", data=model.currentMatrixPf)
        current.create_dataset("valid_mask", data=model.currentValidMask.astype(np.uint8))
        frames = handle.create_group("frames")
        frames.create_dataset("values_pf", data=np.stack([frame.values_matrix() for frame in model.frames], axis=0))
        frames.create_dataset("valid_mask", data=np.stack([frame.valid_matrix().astype(np.uint8) for frame in model.frames], axis=0))
        frames.create_dataset("seq", data=np.asarray([frame.seq for frame in model.frames], dtype=np.int64))
        frames.create_dataset("time_seconds", data=np.asarray([frame.timeSeconds for frame in model.frames], dtype=np.float64))
        frames.create_dataset("rows", data=np.asarray([frame.rows for frame in model.frames], dtype=np.int16))
        offsets = handle.create_group("offsets")
        offsets.create_dataset("offsets_pf", data=model.offsetsPf)
    return output.getvalue()


def _load_csv(path: Path) -> list[SessionFrame]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in CSV_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"CSV missing required columns: {', '.join(missing)}")
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
    missing = [column for column in CSV_COLUMNS if column not in header]
    if missing:
        raise ValueError(f"XLSX frames sheet missing required columns: {', '.join(missing)}")
    dict_rows = [dict(zip(header, row, strict=False)) for row in rows[1:]]
    return _rows_to_frames(dict_rows)


def _load_mat(path: Path) -> list[SessionFrame]:
    try:
        from scipy.io import loadmat
    except ImportError as exc:
        raise RuntimeError("scipy is required for MAT import") from exc

    payload = loadmat(path)
    required = ["frames_values_pf", "frames_valid_mask", "frame_seq", "frame_time_seconds", "frame_rows"]
    missing = [name for name in required if name not in payload]
    if missing:
        raise ValueError(f"MAT missing required variables: {', '.join(missing)}")
    return _arrays_to_frames(
        payload["frames_values_pf"],
        payload["frames_valid_mask"],
        np.ravel(payload["frame_seq"]),
        np.ravel(payload["frame_time_seconds"]),
        np.ravel(payload["frame_rows"]),
    )


def _load_h5(path: Path) -> list[SessionFrame]:
    try:
        import h5py
    except ImportError as exc:
        raise RuntimeError("h5py is required for HDF5 import") from exc

    with h5py.File(path, "r") as handle:
        required = ["frames/values_pf", "frames/valid_mask", "frames/seq", "frames/time_seconds", "frames/rows"]
        missing = [name for name in required if name not in handle]
        if missing:
            raise ValueError(f"HDF5 missing required datasets: {', '.join(missing)}")
        return _arrays_to_frames(
            handle["frames/values_pf"][()],
            handle["frames/valid_mask"][()],
            handle["frames/seq"][()],
            handle["frames/time_seconds"][()],
            handle["frames/rows"][()],
        )


def _arrays_to_frames(values: Any, valid: Any, seq: Any, time_seconds: Any, rows: Any) -> list[SessionFrame]:
    values_array = np.asarray(values, dtype=np.float64)
    valid_array = np.asarray(valid, dtype=bool)
    if values_array.ndim != 3 or values_array.shape[1:] != (8, 8):
        raise ValueError("frames_values_pf must have shape Nx8x8")
    if valid_array.shape != values_array.shape:
        raise ValueError("frames_valid_mask must have the same shape as frames_values_pf")
    frames: list[SessionFrame] = []
    for index in range(values_array.shape[0]):
        frames.append(
            SessionFrame(
                seq=_int_value(seq[index] if index < len(seq) else index + 1, index + 1),
                timeSeconds=_float_value(time_seconds[index] if index < len(time_seconds) else index, float(index)),
                rows=_rows_value(rows[index] if index < len(rows) else 8, 8),
                valuesPf=values_array[index].reshape(64),
                valid=valid_array[index].reshape(64),
            )
        )
    if not frames:
        raise ValueError("session file contains no frames")
    return frames


def _rows_to_frames(rows: Any) -> list[SessionFrame]:
    groups: dict[int, dict[str, Any]] = {}
    for row in rows:
        frame_index = _int_value(row.get("frameIndex"), 0)
        group = groups.setdefault(
            frame_index,
            {
                "seq": _int_value(row.get("seq"), frame_index + 1),
                "timeSeconds": _float_value(row.get("timeSeconds"), float(frame_index)),
                "rows": _rows_value(row.get("rows"), 8),
                "values": np.full((8, 8), np.nan, dtype=np.float64),
                "valid": np.zeros((8, 8), dtype=bool),
            },
        )
        row_index = _row_index(row.get("row"))
        col_index = _col_index(row.get("col"))
        value = _json_number(row.get("correctedPf"))
        if value is not None:
            group["values"][row_index, col_index] = value
        group["valid"][row_index, col_index] = _bool_value(row.get("valid"))
    frames = [
        SessionFrame(
            seq=int(group["seq"]),
            timeSeconds=float(group["timeSeconds"]),
            rows=int(group["rows"]),
            valuesPf=np.asarray(group["values"], dtype=np.float64).reshape(64),
            valid=np.asarray(group["valid"], dtype=bool).reshape(64),
        )
        for _, group in sorted(groups.items())
    ]
    if not frames:
        raise ValueError("session file contains no frames")
    return frames


def _iter_frame_rows(frames: list[SessionFrame]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for frame_index, frame in enumerate(frames):
        values = frame.values_matrix()
        valid = frame.valid_matrix()
        for row_index in range(8):
            for col_index in range(8):
                rows.append(
                    {
                        "frameIndex": frame_index,
                        "seq": frame.seq,
                        "timeSeconds": frame.timeSeconds,
                        "rows": frame.rows,
                        "cell": _cell_label(row_index, col_index),
                        "row": row_index + 1,
                        "col": col_index + 1,
                        "correctedPf": _blank_nan(values[row_index, col_index]),
                        "valid": bool(valid[row_index, col_index]),
                        "source": frame.source,
                    }
                )
    return rows


def _matrix_from_json(value: Any, default: float = np.nan) -> np.ndarray:
    matrix = np.full((8, 8), default, dtype=np.float64)
    if not isinstance(value, list):
        return matrix
    for row_index, row in enumerate(value[:8]):
        if not isinstance(row, list):
            continue
        for col_index, item in enumerate(row[:8]):
            number = _json_number(item)
            if number is not None:
                matrix[row_index, col_index] = number
    return matrix


def _bool_matrix_from_json(value: Any) -> np.ndarray:
    matrix = np.zeros((8, 8), dtype=bool)
    if not isinstance(value, list):
        return matrix
    for row_index, row in enumerate(value[:8]):
        if not isinstance(row, list):
            continue
        for col_index, item in enumerate(row[:8]):
            matrix[row_index, col_index] = bool(item)
    return matrix


def _json_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def _blank_nan(value: Any) -> float | str:
    number = _json_number(value)
    return "" if number is None else number


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _int_value(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float_value(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if np.isfinite(number) else default


def _rows_value(value: Any, default: int) -> int:
    rows = _int_value(value, default)
    if not (1 <= rows <= 8):
        raise ValueError("rows must be 1..8")
    return rows


def _row_index(value: Any) -> int:
    index = _int_value(value, 0) - 1
    if not (0 <= index < 8):
        raise ValueError("row must be 1..8")
    return index


def _col_index(value: Any) -> int:
    index = _int_value(value, 0) - 1
    if not (0 <= index < 8):
        raise ValueError("col must be 1..8")
    return index


def _cell_label(row_index: int, col_index: int) -> str:
    return f"S{row_index + 1}D{col_index + 1}"


def _string_value(value: Any) -> str:
    return "" if value is None else str(value)
