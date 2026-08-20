from __future__ import annotations

from collections import deque
from dataclasses import asdict
import json
from pathlib import Path
import queue
import shutil
import tempfile
import threading
import time
from typing import Iterable
import uuid

from sensorarray_app.constants import RAW_LOG_DEFAULT_LINES
from sensorarray_app.domain.models import LogRecord


class RawLogStore:
    def __init__(
        self,
        max_lines: int = RAW_LOG_DEFAULT_LINES,
        *,
        cache_root: str | Path | None = None,
        cache_quota_bytes: int = 64 * 1024 * 1024,
        cache_max_age_hours: float = 72.0,
        raw_wire_capture: bool = False,
    ):
        self.maxLines = max(1, int(max_lines))
        self._records: deque[LogRecord] = deque(maxlen=self.maxLines)
        self.totalRecords = 0
        self.overwrites = 0
        self.revision = 0
        self.rawWireCapture = bool(raw_wire_capture)
        self._cache = _TemporaryDiagnosticCache(
            root=cache_root,
            quota_bytes=cache_quota_bytes,
            max_age_hours=cache_max_age_hours,
        )

    def add(self, record: LogRecord) -> None:
        if len(self._records) == self.maxLines:
            self.overwrites += 1
        self._records.append(record)
        self.totalRecords += 1
        self.revision += 1
        if self.rawWireCapture or record.category != "MEASUREMENT":
            self._cache.append(record)

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
            rows = [row for row in rows if row.category != "MEASUREMENT"]
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
            "diskCache": self._cache.snapshot(),
            "rows": [asdict(row) for row in rows],
        }

    def save_all(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._cache.flush()
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            cached = self._cache.iter_records()
            if cached:
                for row in cached:
                    handle.write(str(row.get("rawText", "")) + "\n")
            else:
                for record in self._records:
                    handle.write(record.rawText + "\n")

    def export_recoverable(self, session: str, path: str | Path) -> int:
        return self._cache.export_recoverable(session, path)

    def clear_view(self) -> None:
        self._records.clear()
        self.revision += 1

    def close(self, *, clean: bool = True) -> None:
        self._cache.close(clean=clean)


class _TemporaryDiagnosticCache:
    """Bounded asynchronous cache preserving logs evicted from the UI ring."""

    def __init__(self, root: str | Path | None, quota_bytes: int, max_age_hours: float):
        base = Path(root) if root is not None else Path(tempfile.gettempdir()) / "sensorarray-diagnostic-cache"
        self.root = base
        # The same bound applies to the live session and to retained crash
        # sessions as a group.  Honour deliberately small values in tests and
        # embedded deployments instead of silently expanding the quota.
        self.quotaBytes = max(1024, int(quota_bytes))
        self.maxAgeSeconds = max(3600.0, float(max_age_hours) * 3600.0)
        self.sessionId = str(uuid.uuid4())
        self.sessionPath = self.root / f"session-{self.sessionId}"
        self.recordsPath = self.sessionPath / "records.jsonl"
        self.statePath = self.sessionPath / "crash_state.json"
        self.metadataPath = self.sessionPath / "metadata.json"
        self.queue: queue.Queue[dict | None] = queue.Queue(maxsize=4096)
        self.queued = 0
        self.written = 0
        self.dropped = 0
        self.bytesWritten = 0
        self.error = ""
        self.recoverableSessions: list[str] = []
        self._started = False
        self._closed = False
        self._thread: threading.Thread | None = None
        self._gc()

    def append(self, record: LogRecord) -> None:
        if self._closed:
            return
        self._ensure_started()
        try:
            self.queue.put_nowait(asdict(record))
            self.queued += 1
        except queue.Full:
            self.dropped += 1

    def flush(self, timeout: float = 2.0) -> bool:
        if not self._started:
            return True
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return self.queue.unfinished_tasks == 0

    def close(self, *, clean: bool) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._started:
            return
        try:
            self.queue.put(None, timeout=1.0)
        except queue.Full:
            self.dropped += 1
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._write_json(
            self.statePath,
            {
                "clean": bool(clean and self._thread is not None and not self._thread.is_alive()),
                "closedAt": time.time(),
                "written": self.written,
                "dropped": self.dropped,
                "error": self.error,
            },
        )

    def snapshot(self) -> dict:
        return {
            "enabled": True,
            "sessionId": self.sessionId,
            "path": str(self.sessionPath),
            "queued": self.queued,
            "written": self.written,
            "queueDepth": self.queue.qsize(),
            "dropped": self.dropped,
            "bytesWritten": self.bytesWritten,
            "quotaBytes": self.quotaBytes,
            "error": self.error,
            "recoverableSessions": list(self.recoverableSessions),
        }

    def iter_records(self) -> list[dict]:
        if not self.recordsPath.exists():
            return []
        rows: list[dict] = []
        try:
            with self.recordsPath.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        rows.append(value)
        except OSError as exc:
            self.error = str(exc)
        return rows

    def export_recoverable(self, session: str, path: str | Path) -> int:
        """Export one retained unclean session without trusting caller paths."""

        requested = str(session).strip()
        candidate_name = requested if requested.startswith("session-") else f"session-{requested}"
        candidate = self.root / candidate_name
        allowed = {Path(item).name for item in self.recoverableSessions}
        if candidate.name not in allowed or candidate.parent.resolve() != self.root.resolve():
            raise ValueError("unknown recoverable diagnostic session")
        records_path = candidate / "records.jsonl"
        if not records_path.is_file():
            raise FileNotFoundError(str(records_path))
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with records_path.open("r", encoding="utf-8") as source, target.open(
            "w", encoding="utf-8", newline="\n"
        ) as destination:
            for line in source:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                destination.write(str(row.get("rawText", "")) + "\n")
                written += 1
        return written

    def _ensure_started(self) -> None:
        if self._started:
            return
        self.sessionPath.mkdir(parents=True, exist_ok=True)
        self._write_json(self.metadataPath, {"sessionId": self.sessionId, "createdAt": time.time(), "schemaVersion": 1})
        self._write_json(self.statePath, {"clean": False, "createdAt": time.time()})
        self._thread = threading.Thread(target=self._run, name="SensorArrayDiagnosticCache", daemon=True)
        self._thread.start()
        self._started = True

    def _run(self) -> None:
        try:
            with self.recordsPath.open("a", encoding="utf-8", newline="\n") as handle:
                while True:
                    item = self.queue.get()
                    try:
                        if item is None:
                            handle.flush()
                            return
                        encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                        size = len(encoded.encode("utf-8"))
                        if self.bytesWritten + size > self.quotaBytes:
                            self.dropped += 1
                            self.error = "diagnostic cache quota reached"
                            continue
                        handle.write(encoded)
                        self.bytesWritten += size
                        self.written += 1
                        if self.written % 64 == 0:
                            handle.flush()
                    finally:
                        self.queue.task_done()
        except Exception as exc:  # pragma: no cover - filesystem failure path
            self.error = str(exc)

    def _gc(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            now = time.time()
            retained: list[tuple[Path, float, int]] = []
            for candidate in self.root.glob("session-*"):
                if not candidate.is_dir():
                    continue
                state_path = candidate / "crash_state.json"
                clean = False
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    clean = bool(state.get("clean"))
                except (OSError, json.JSONDecodeError, AttributeError):
                    state = {}
                age = max(0.0, now - candidate.stat().st_mtime)
                if age > self.maxAgeSeconds or clean:
                    shutil.rmtree(candidate, ignore_errors=True)
                else:
                    retained.append((candidate, candidate.stat().st_mtime, _directory_size(candidate)))

            # Crash logs are retained preferentially, but never without a
            # bound.  Reclaim the oldest sessions first until the aggregate
            # retained footprint fits the configured cache quota.
            total_bytes = sum(size for _path, _mtime, size in retained)
            retained.sort(key=lambda item: item[1])
            while retained and total_bytes > self.quotaBytes:
                candidate, _mtime, size = retained.pop(0)
                shutil.rmtree(candidate, ignore_errors=True)
                total_bytes = max(0, total_bytes - size)
            self.recoverableSessions = [str(path) for path, _mtime, _size in retained]
        except OSError as exc:
            self.error = str(exc)

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _directory_size(path: Path) -> int:
    total = 0
    try:
        for candidate in path.rglob("*"):
            if candidate.is_file():
                total += candidate.stat().st_size
    except OSError:
        return total
    return total
