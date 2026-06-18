from __future__ import annotations

import itertools
import time
from dataclasses import dataclass

from sensorarray_app.domain.models import CommandAccepted, CommandApplied


@dataclass
class CommandRecord:
    localId: int
    command: str
    requestedRows: int | None
    state: str
    sentTime: float
    firmwareId: int | None = None
    message: str = ""


class CommandService:
    def __init__(self):
        self._ids = itertools.count(1)
        self._commands: dict[int, CommandRecord] = {}
        self.requestedRows: int | None = None
        self.activeRows = 8
        self.pendingRows: int | None = None

    def request_rows(self, rows: int, sender) -> CommandRecord:
        if not (1 <= int(rows) <= 8):
            raise ValueError("ROWS must be 1..8")
        local_id = next(self._ids)
        command = f"ROWS={int(rows)}"
        record = CommandRecord(local_id, command, int(rows), "REQUESTED", time.time())
        self._commands[local_id] = record
        self.requestedRows = int(rows)
        self.pendingRows = int(rows)
        sender(command)
        return record

    def accept(self, event: CommandAccepted) -> None:
        for record in self._commands.values():
            if record.requestedRows == event.requestedRows and record.state == "REQUESTED":
                record.state = "ACCEPTED"
                record.firmwareId = event.commandId
                return

    def apply(self, event: CommandApplied) -> None:
        if event.newRows is not None:
            self.activeRows = event.newRows
            self.pendingRows = None
        for record in self._commands.values():
            if record.firmwareId == event.commandId or record.requestedRows == event.newRows:
                record.state = "APPLIED"
                record.message = event.rawText
                return

    def timeout_old(self, seconds: float = 5.0) -> None:
        now = time.time()
        for record in self._commands.values():
            if record.state in {"REQUESTED", "ACCEPTED"} and now - record.sentTime > seconds:
                record.state = "TIMEOUT"

    def snapshot(self) -> dict:
        self.timeout_old()
        latest = max(self._commands.values(), key=lambda item: item.localId, default=None)
        return {
            "requestedRows": self.requestedRows,
            "activeRows": self.activeRows,
            "pendingRows": self.pendingRows,
            "latestCommand": latest.__dict__ if latest else None,
        }
