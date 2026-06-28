import { Eraser, LocateFixed, RotateCcw, Save, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type { BackendHttpClient } from "../../api/httpClient";
import type { BackendSnapshotPayload, OffsetScope } from "../../api/types";
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

type EditingCell = { row: number; col: number };

export function OffsetPanel({ client, snapshot, onError }: Props): JSX.Element {
  const [row, setRow] = useState(1);
  const [col, setCol] = useState(1);
  const [pending, setPending] = useState(false);
  const [status, setStatus] = useState("");
  const [editingCell, setEditingCell] = useState<EditingCell | null>(null);
  const [offsetText, setOffsetText] = useState("0");
  const [offsetDirty, setOffsetDirty] = useState(false);

  const values = useMemo(() => cellValues(snapshot, row, col), [col, row, snapshot]);
  const editingValues = useMemo(() => (editingCell ? cellValues(snapshot, editingCell.row, editingCell.col) : null), [editingCell, snapshot]);

  useEffect(() => {
    if (editingCell && editingValues && !offsetDirty) {
      setOffsetText(formatOffsetInput(editingValues.offset));
    }
  }, [editingCell, editingValues, offsetDirty]);

  function selectCell(nextRow: number, nextCol: number): void {
    setRow(nextRow);
    setCol(nextCol);
  }

  function openEditor(nextRow: number, nextCol: number): void {
    selectCell(nextRow, nextCol);
    setEditingCell({ row: nextRow, col: nextCol });
    setOffsetText(formatOffsetInput(cellValues(snapshot, nextRow, nextCol).offset));
    setOffsetDirty(false);
  }

  async function run(label: string, action: () => Promise<void>): Promise<void> {
    if (!client) {
      return;
    }
    setPending(true);
    setStatus("");
    try {
      await action();
      setStatus(label);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setStatus("");
      onError(message);
    } finally {
      setPending(false);
    }
  }

  async function commitOffsetCell(targetRow: number, targetCol: number, offsetPf: number): Promise<void> {
    await run("Offset saved; baseline invalidated", async () => {
      await client!.setOffsetCell(targetRow, targetCol, offsetPf);
      closeEditor();
    });
  }

  async function zeroOffsetCellToCurrent(targetRow: number, targetCol: number): Promise<void> {
    const corrected = cellValues(snapshot, targetRow, targetCol).corrected;
    if (typeof corrected !== "number" || !Number.isFinite(corrected)) {
      onError("Cannot zero this cell because current corrected pF is unavailable");
      return;
    }
    await run("Current cell zeroed; baseline invalidated", async () => {
      await client!.zeroCurrentOffsets("cell", targetRow, targetCol);
      closeEditor();
    });
  }

  async function clearOffsetCell(targetRow: number, targetCol: number): Promise<void> {
    await run("Cell offset cleared; baseline invalidated", async () => {
      await client!.clearOffsets("cell", targetRow, targetCol);
      closeEditor();
    });
  }

  async function zeroCurrent(scope: OffsetScope): Promise<void> {
    await run("Current value zeroed; baseline invalidated", () =>
      client!.zeroCurrentOffsets(scope, scope === "all" ? undefined : row, scope === "cell" ? col : undefined).then(() => undefined)
    );
  }

  async function clear(scope: OffsetScope): Promise<void> {
    await run("Offset cleared; baseline invalidated", () =>
      client!.clearOffsets(scope, scope === "all" ? undefined : row, scope === "cell" ? col : undefined).then(() => undefined)
    );
  }

  function closeEditor(): void {
    setEditingCell(null);
    setOffsetDirty(false);
  }

  return (
    <div className="offsetPanel">
      <OffsetGrid snapshot={snapshot} selectedRow={row} selectedCol={col} onSelectCell={selectCell} onEditCell={openEditor} />

      <div className="offsetSide">
        <div className="offsetControls">
          <label>
            Row
            <select value={row} onChange={(event) => selectCell(Number(event.target.value), col)}>
              {Array.from({ length: 8 }, (_, index) => index + 1).map((value) => (
                <option key={value} value={value}>
                  S{value}
                </option>
              ))}
            </select>
          </label>
          <label>
            Detector
            <select value={col} onChange={(event) => selectCell(row, Number(event.target.value))}>
              {Array.from({ length: 8 }, (_, index) => index + 1).map((value) => (
                <option key={value} value={value}>
                  D{value}
                </option>
              ))}
            </select>
          </label>
        </div>

        <dl className="offsetReadout">
          <div>
            <dt>Selected</dt>
            <dd>{cellLabel(row - 1, col - 1)}</dd>
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
            <dt>Displayed {snapshot?.matrix.unit ?? "pF"}</dt>
            <dd>{formatPf(values.displayed, snapshot?.matrix.unit === "%" ? 2 : 3)}</dd>
          </div>
        </dl>
      </div>

      <div className="offsetButtons">
        <button disabled={!client || pending} onClick={() => openEditor(row, col)}>
          <Save size={16} /> Edit selected cell
        </button>
        <button disabled={!client || pending || !snapshot?.frame.valid} onClick={() => void zeroCurrent("cell")}>
          <LocateFixed size={16} /> Zero selected cell
        </button>
        <button disabled={!client || pending || !snapshot?.frame.valid} onClick={() => void zeroCurrent("row")}>
          <LocateFixed size={16} /> Zero current row
        </button>
        <button disabled={!client || pending || !snapshot?.frame.valid} onClick={() => void zeroCurrent("all")}>
          <LocateFixed size={16} /> Zero all cells
        </button>
        <button disabled={!client || pending} onClick={() => void clear("cell")}>
          <Eraser size={16} /> Clear selected cell
        </button>
        <button disabled={!client || pending} onClick={() => void clear("row")}>
          <Eraser size={16} /> Clear current row
        </button>
        <button disabled={!client || pending} onClick={() => void clear("all")}>
          <RotateCcw size={16} /> Clear all offsets
        </button>
      </div>
      {status ? <div className="inlineNotice">{status}</div> : null}

      {editingCell && editingValues ? (
        <OffsetEditDialog
          cell={editingCell}
          values={editingValues}
          unit={snapshot?.matrix.unit ?? "pF"}
          offsetText={offsetText}
          pending={pending}
          onOffsetTextChange={(text) => {
            setOffsetText(text);
            setOffsetDirty(true);
          }}
          onSave={() => {
            const offsetPf = parseOffsetInput(offsetText);
            if (offsetPf === null) {
              onError("Offset must be a finite pF value");
              return;
            }
            void commitOffsetCell(editingCell.row, editingCell.col, offsetPf);
          }}
          onZero={() => void zeroOffsetCellToCurrent(editingCell.row, editingCell.col)}
          onClear={() => void clearOffsetCell(editingCell.row, editingCell.col)}
          onCancel={closeEditor}
        />
      ) : null}
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
  return (
    <button
      className={selected ? "offsetCell active" : "offsetCell"}
      title={cellLabel(row - 1, col - 1)}
      onClick={() => onSelectCell(row, col)}
      onDoubleClick={() => onEditCell(row, col)}
    >
      <span>{cellLabel(row - 1, col - 1)}</span>
      <small>{formatPf(offset)}</small>
    </button>
  );
}

function OffsetEditDialog({
  cell,
  values,
  unit,
  offsetText,
  pending,
  onOffsetTextChange,
  onSave,
  onZero,
  onClear,
  onCancel
}: {
  cell: EditingCell;
  values: OffsetCellValues;
  unit: string;
  offsetText: string;
  pending: boolean;
  onOffsetTextChange: (text: string) => void;
  onSave: () => void;
  onZero: () => void;
  onClear: () => void;
  onCancel: () => void;
}): JSX.Element {
  return (
    <div className="modalBackdrop" role="presentation">
      <div
        className="offsetDialog"
        role="dialog"
        aria-modal="true"
        aria-label={`Edit offset for ${cellLabel(cell.row - 1, cell.col - 1)}`}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            onCancel();
          }
          if (event.key === "Enter") {
            event.preventDefault();
            onSave();
          }
        }}
      >
        <div className="dialogHeader">
          <strong>Edit offset for {cellLabel(cell.row - 1, cell.col - 1)}</strong>
          <button title="Cancel" onClick={onCancel}>
            <X size={16} />
          </button>
        </div>
        <dl className="offsetReadout">
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
            <dt>Displayed value</dt>
            <dd>{formatPf(values.displayed, unit === "%" ? 2 : 3)}</dd>
          </div>
        </dl>
        <label>
          Offset pF
          <input autoFocus value={offsetText} inputMode="decimal" onChange={(event) => onOffsetTextChange(event.target.value)} />
        </label>
        <div className="buttonRow">
          <button className="primary" disabled={pending} onClick={onSave}>
            <Save size={16} /> Save offset
          </button>
          <button disabled={pending} onClick={onZero}>
            <LocateFixed size={16} /> Zero this cell to current corrected value
          </button>
          <button disabled={pending} onClick={onClear}>
            <Eraser size={16} /> Clear this cell
          </button>
          <button disabled={pending} onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
    </div>
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
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "NA";
}

export function formatOffsetInput(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "0";
}

export function parseOffsetInput(text: string): number | null {
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}
