from __future__ import annotations

from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from sensorarray_app.constants import RAW_LOG_DEFAULT_LINES
from sensorarray_app.domain.models import LogRecord


class RawLogStore:
    def __init__(self, max_lines: int = RAW_LOG_DEFAULT_LINES):
        self.maxLines = max(1, int(max_lines))
        self._records: deque[LogRecord] = deque(maxlen=self.maxLines)
        self.totalRecords = 0
        self.overwrites = 0
        self.revision = 0

    def add(self, record: LogRecord) -> None:
        if len(self._records) == self.maxLines:
            self.overwrites += 1
        self._records.append(record)
        self.totalRecords += 1
        self.revision += 1

    def extend(self, records: Iterable[LogRecord]) -> None:
        for record in records:
            self.add(record)

    def snapshot(
        self,
        show_data: bool = False,
        search: str = "",
        tag: str = "",
        severity: str = "",
        source: str = "",
        channel: str = "",
        limit: int = 500,
    ) -> dict:
        rows = list(self._records)
        if not show_data:
            rows = [row for row in rows if row.tag not in {"C", "D0", "D1", "D2", "D3", "D4", "K"}]
        if tag:
            rows = [row for row in rows if row.tag == tag]
        if severity:
            rows = [row for row in rows if row.severity == severity]
        if source:
            rows = [row for row in rows if row.source == source]
        if channel:
            rows = [row for row in rows if row.channel == channel]
        if search:
            rows = [row for row in rows if search in row.rawText]
        rows = rows[-max(1, int(limit)) :]
        return {
            "revision": self.revision,
            "totalRecords": self.totalRecords,
            "overwrites": self.overwrites,
            "rows": [asdict(row) for row in rows],
        }

    def save_all(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            for record in self._records:
                handle.write(record.rawText + "\n")

    def clear_view(self) -> None:
        self._records.clear()
        self.revision += 1
