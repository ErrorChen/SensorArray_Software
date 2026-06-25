from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FourPointSelection:
    rowIndex: int
    rowLabel: str
    fdcGroup: str
    detectorStart: int
    detectorEnd: int
    cells: tuple[str, str, str, str]
    selectionRevision: int

    @property
    def title(self) -> str:
        label = "Primary FDC" if self.fdcGroup == "primary" else "Secondary FDC"
        return f"{self.rowLabel} 路 {label} 路 D{self.detectorStart}-D{self.detectorEnd}"

    def to_payload(self) -> dict:
        return {
            "rowIndex": self.rowIndex,
            "rowLabel": self.rowLabel,
            "fdcGroup": self.fdcGroup,
            "detectorStart": self.detectorStart,
            "detectorEnd": self.detectorEnd,
            "cells": list(self.cells),
            "title": self.title,
            "selectionRevision": self.selectionRevision,
        }


def default_selection(active_rows: int = 8, revision: int = 0) -> FourPointSelection:
    row = 1 if active_rows >= 1 else 0
    if row <= 0:
        row = 1
    return select_group(row, 1, active_rows=max(1, active_rows), revision=revision)


def select_group(row: int, detector: int, active_rows: int, revision: int = 0) -> FourPointSelection:
    if not (1 <= row <= 8):
        raise ValueError("row must be 1..8")
    if row > active_rows:
        raise ValueError("inactive row cannot be selected")
    if not (1 <= detector <= 8):
        raise ValueError("detector must be 1..8")
    if detector <= 4:
        group = "primary"
        start = 1
        end = 4
    else:
        group = "secondary"
        start = 5
        end = 8
    cells = tuple(f"S{row}D{det}" for det in range(start, end + 1))
    return FourPointSelection(
        rowIndex=row - 1,
        rowLabel=f"S{row}",
        fdcGroup=group,
        detectorStart=start,
        detectorEnd=end,
        cells=cells,  # type: ignore[arg-type]
        selectionRevision=int(revision),
    )


def correct_selection(selection: FourPointSelection, active_rows: int, revision: int) -> tuple[FourPointSelection, bool]:
    if selection.rowIndex < active_rows:
        return selection, False
    corrected = select_group(1, selection.detectorStart, max(1, active_rows), revision=revision)
    return corrected, True


def fallback_title(selection: dict) -> str:
    row_label = selection.get("rowLabel") or f"S{int(selection.get('rowIndex', 0)) + 1}"
    group = selection.get("fdcGroup") or "primary"
    label = "Primary FDC" if group == "primary" else "Secondary FDC"
    start = int(selection.get("detectorStart") or (1 if group == "primary" else 5))
    end = int(selection.get("detectorEnd") or (4 if group == "primary" else 8))
    return f"{row_label} 路 {label} 路 D{start}-D{end}"

