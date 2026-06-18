from __future__ import annotations

import csv
from pathlib import Path

from sensorarray_app.constants import CELL_NAMES
from sensorarray_app.store.history_store import MatrixHistoryStore


def export_history_csv(history: MatrixHistoryStore, path: str | Path, metadata: dict | None = None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    indices = history.ordered_indices()
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if metadata:
            for key, value in metadata.items():
                writer.writerow([f"# {key}", value])
        writer.writerow(["seq", "time_s", "rows", *CELL_NAMES])
        for idx in indices:
            writer.writerow([history.seq[idx], history.timeSeconds[idx], history.rows[idx], *history.values[idx, :].tolist()])
