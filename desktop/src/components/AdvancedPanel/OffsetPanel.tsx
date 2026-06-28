import { Eraser, LocateFixed, RotateCcw, Save } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import type { BackendHttpClient } from "../../api/httpClient";
import type { BackendSnapshotPayload, OffsetResponse, OffsetScope } from "../../api/types";
import { cellLabel } from "../../state/appStore";

type Props = {
  client: BackendHttpClient | null;
  snapshot: BackendSnapshotPayload | null;
  onError: (message: string) => void;
};

type OffsetCellValues = {
  raw: number | null | undefined;
  corrected: number | null | undefined;
  offset: number | null | undefined;
  displayed: number | null | undefined;
};

type SelectedCell = { row: number; col: number };

export function OffsetPanel({ client, snapshot, onError }: Props): JSX.Element {
  const [selectedCell, setSelectedCell] = useState<SelectedCell>({ row: 1, col: 1 });
  const [pending, setPending] = useState(false);
  const [status, setStatus] = useState("");
  const [offsetText, setOffsetText] = useState("0");
  const [offsetDirty, setOffsetDirty] = useState(false);
  const offsetInputRef = useRef<HTMLInputElement | null>(null);

  const values = useMemo(() => cellValues(snapshot, selectedCell.row, selectedCell.col), [selectedCell.col, selectedCell.row, snapshot]);
  const finiteCorrectedCount = useMemo(() => countFiniteCorrected(snapshot), [snapshot]);
  const finiteSelectedRowCount = useMemo(() => countFiniteCorrected(snapshot, selectedCell.row), [selectedCell.row, snapshot]);
  const selectedLabel = cellLabel(selectedCell.row - 1, selectedCell.col - 1);

  useEffect(() => {
    if (!offsetDirty) {
      setOffsetText(formatOffsetInput(values.offset));
    }
  }, [offsetDirty, values.offset]);

  function selectCell(nextRow: number, nextCol: number, focusInput = false): void {
    if (nextRow === selectedCell.row && nextCol === selectedCell.col && !focusInput) {
      return;
    }
    if (offsetDirty && (nextRow !== selectedCell.row || nextCol !== selectedCell.col)) {
      setStatus("Unsaved offset input discarded");
    }
    setSelectedCell({ row: nextRow, col: nextCol });
    setOffsetText(formatOffsetInput(cellValues(snapshot, nextRow, nextCol).offset));
    setOffsetDirty(false);
    if (focusInput) {
      window.setTimeout(() => {
        offsetInputRef.current?.focus();
        offsetInputRef.current?.select();
      }, 0);
    }
  }

  async function run(action: () => Promise<OffsetResponse>, success: (response: OffsetResponse) => string): Promise<void> {
    if (!client) {
      return;
    }
    setPending(true);
    setStatus("");
    try {
      const response = await action();
      setOffsetDirty(false);
      setOffsetText(formatOffsetInput(response.offsetsPf[selectedCell.row - 1]?.[selectedCell.col - 1]));
      setStatus(success(response));
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setPending(false);
    }
  }

  async function saveSelectedOffset(): Promise<void> {
    const offsetPf = parseOffsetInput(offsetText);
    if (offsetPf === null) {
      onError("Offset must be a finite pF value");
      return;
    }
    await run(
      () => client!.setOffsetCell(selectedCell.row, selectedCell.col, offsetPf),
      () => `Offset saved for ${selectedLabel}; baseline invalidated`
    );
  }

  async function zeroSelectedCell(): Promise<void> {
    if (!isFiniteNumber(values.corrected)) {
      onError("Cannot zero this cell because current corrected pF is unavailable");
      return;
    }
    await run(
      () => client!.zeroCurrentOffsets("cell", selectedCell.row, selectedCell.col),
      () => `Zeroed ${selectedLabel}; baseline invalidated`
    );
  }

  async function clearSelectedCell(): Promise<void> {
    await run(
      () => client!.clearOffsets("cell", selectedCell.row, selectedCell.col),
      () => `Cleared ${selectedLabel}; baseline invalidated`
    );
  }

  async function zeroScope(scope: OffsetScope): Promise<void> {
    const targetCount = scope === "all" ? 64 : scope === "row" ? 8 : 1;
    const finiteCount = scope === "all" ? finiteCorrectedCount : scope === "row" ? finiteSelectedRowCount : isFiniteNumber(values.corrected) ? 1 : 0;
    if (finiteCount === 0) {
      onError(scope === "row" ? "Cannot zero this row because it has no finite corrected pF values" : "Cannot zero because no finite corrected pF values are available");
      return;
    }
    await run(
      () => client!.zeroCurrentOffsets(scope, scope === "all" ? undefined : selectedCell.row, scope === "cell" ? selectedCell.col : undefined),
      (response) => {
        const applied = response.changedCells ?? finiteCount;
        return `Zero applied to ${applied} cell${applied === 1 ? "" : "s"}; skipped ${targetCount - applied}; baseline invalidated`;
      }
    );
  }

  async function clearScope(scope: OffsetScope): Promise<void> {
    if (scope === "all" && !window.confirm("Clear all offsets?")) {
      return;
    }
    await run(
      () => client!.clearOffsets(scope, scope === "all" ? undefined : selectedCell.row, scope === "cell" ? selectedCell.col : undefined),
      () => (scope === "all" ? "Cleared all offsets; baseline invalidated" : scope === "row" ? `Cleared row S${selectedCell.row}; baseline invalidated` : `Cleared ${selectedLabel}; baseline invalidated`)
    );
  }

  return (
    <div className="offsetPanel">
      <OffsetGrid
        snapshot={snapshot}
        selectedRow={selectedCell.row}
        selectedCol={selectedCell.col}
        onSelectCell={(row, col) => selectCell(row, col)}
        onEditCell={(row, col) => selectCell(row, col, true)}
      />

      <div className="offsetSide">
        <dl className="offsetReadout">
          <div>
            <dt>Selected</dt>
            <dd>{selectedLabel}</dd>
          </div>
          <div>
            <dt>Raw pF</dt>
            <dd>{formatPf(values.raw)}</dd>
          </div>
          <div>
            <dt>Corrected pF</dt>
            <dd>{formatPf(values.corrected)}</dd>
          </div>
          <div>
            <dt>User offset pF</dt>
            <dd>{formatPf(values.offset)}</dd>
          </div>
          <div>
            <dt>Displayed pF</dt>
            <dd>{snapshot?.matrix.unit === "pF" ? formatPf(values.displayed) : "NA"}</dd>
          </div>
          <div>
            <dt>Displayed %</dt>
            <dd>{snapshot?.matrix.unit === "%" ? formatPf(values.displayed, 2) : "NA"}</dd>
          </div>
        </dl>

        <label className="offsetEditorField">
          Offset pF
          <input
            ref={offsetInputRef}
            value={offsetText}
            inputMode="decimal"
            onChange={(event) => {
              setOffsetText(event.target.value);
              setOffsetDirty(true);
            }}
          />
        </label>

        <div className="buttonRow">
          <button className="primary" disabled={!client || pending} onClick={() => void saveSelectedOffset()}>
            <Save size={16} /> Save selected offset
          </button>
          <button disabled={!client || pending || !isFiniteNumber(values.corrected)} title={isFiniteNumber(values.corrected) ? "" : "Current corrected pF is unavailable"} onClick={() => void zeroSelectedCell()}>
            <LocateFixed size={16} /> Zero selected cell
          </button>
          <button disabled={!client || pending} onClick={() => void clearSelectedCell()}>
            <Eraser size={16} /> Clear selected cell
          </button>
        </div>
      </div>

      <div className="offsetButtons">
        <button disabled={!client || pending || !isFiniteNumber(values.corrected)} onClick={() => void zeroScope("cell")}>
          <LocateFixed size={16} /> Zero selected cell
        </button>
        <button disabled={!client || pending} onClick={() => void clearScope("cell")}>
          <Eraser size={16} /> Clear selected cell
        </button>
        <button disabled={!client || pending || finiteSelectedRowCount === 0} onClick={() => void zeroScope("row")}>
          <LocateFixed size={16} /> Zero current row
        </button>
        <button disabled={!client || pending} onClick={() => void clearScope("row")}>
          <Eraser size={16} /> Clear current row
        </button>
        <button disabled={!client || pending || finiteCorrectedCount === 0} onClick={() => void zeroScope("all")}>
          <LocateFixed size={16} /> Zero all cells
        </button>
        <button disabled={!client || pending} onClick={() => void clearScope("all")}>
          <RotateCcw size={16} /> Clear all offsets
        </button>
      </div>
      {status ? <div className="inlineNotice">{status}</div> : null}
    </div>
  );
}

function OffsetGrid({
  snapshot,
  selectedRow,
  selectedCol,
  onSelectCell,
  onEditCell
}: {
  snapshot: BackendSnapshotPayload | null;
  selectedRow: number;
  selectedCol: number;
  onSelectCell: (row: number, col: number) => void;
  onEditCell: (row: number, col: number) => void;
}): JSX.Element {
  return (
    <div className="offsetGrid" role="grid" aria-label="Offset cell grid">
      {Array.from({ length: 8 }, (_, rowIndex) =>
        Array.from({ length: 8 }, (_, colIndex) => (
          <OffsetCellButton
            key={`${rowIndex}-${colIndex}`}
            row={rowIndex + 1}
            col={colIndex + 1}
            offset={snapshot?.matrix.userOffsetPf[rowIndex]?.[colIndex]}
            selected={rowIndex + 1 === selectedRow && colIndex + 1 === selectedCol}
            onSelectCell={onSelectCell}
            onEditCell={onEditCell}
          />
        ))
      )}
    </div>
  );
}

function OffsetCellButton({
  row,
  col,
  offset,
  selected,
  onSelectCell,
  onEditCell
}: {
  row: number;
  col: number;
  offset: number | null | undefined;
  selected: boolean;
  onSelectCell: (row: number, col: number) => void;
  onEditCell: (row: number, col: number) => void;
}): JSX.Element {
  const label = cellLabel(row - 1, col - 1);
  return (
    <button
      className={selected ? "offsetCell active" : "offsetCell"}
      title={label}
      data-row={row}
      data-col={col}
      aria-label={label}
      aria-pressed={selected}
      onClick={() => onSelectCell(row, col)}
      onDoubleClick={() => onEditCell(row, col)}
    >
      <span>{label}</span>
      <small>{formatPf(offset)}</small>
    </button>
  );
}

export function cellValues(snapshot: BackendSnapshotPayload | null, row: number, col: number): OffsetCellValues {
  const rowIndex = row - 1;
  const colIndex = col - 1;
  return {
    raw: snapshot?.matrix.rawPf[rowIndex]?.[colIndex],
    corrected: snapshot?.matrix.correctedPf[rowIndex]?.[colIndex],
    offset: snapshot?.matrix.userOffsetPf[rowIndex]?.[colIndex],
    displayed: snapshot?.matrix.displayValues[rowIndex]?.[colIndex]
  };
}

export function formatPf(value: number | null | undefined, digits = 3): string {
  return isFiniteNumber(value) ? value.toFixed(digits) : "NA";
}

export function formatOffsetInput(value: number | null | undefined): string {
  return isFiniteNumber(value) ? String(value) : "0";
}

export function parseOffsetInput(text: string): number | null {
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

function countFiniteCorrected(snapshot: BackendSnapshotPayload | null, row?: number): number {
  const rows = row ? [snapshot?.matrix.correctedPf[row - 1] ?? []] : snapshot?.matrix.correctedPf ?? [];
  return rows.flat().filter(isFiniteNumber).length;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}
