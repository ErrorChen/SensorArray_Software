import { Eraser, LocateFixed, RotateCcw, Save } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type { BackendHttpClient } from "../../api/httpClient";
import type { BackendSnapshotPayload, OffsetScope } from "../../api/types";
import { cellLabel } from "../../state/appStore";

type Props = {
  client: BackendHttpClient | null;
  snapshot: BackendSnapshotPayload | null;
  onError: (message: string) => void;
};

export function OffsetPanel({ client, snapshot, onError }: Props): JSX.Element {
  const [row, setRow] = useState(1);
  const [col, setCol] = useState(1);
  const [offsetText, setOffsetText] = useState("0");
  const [pending, setPending] = useState(false);
  const [status, setStatus] = useState("");

  const values = useMemo(() => cellValues(snapshot, row, col), [col, row, snapshot]);

  useEffect(() => {
    setOffsetText(formatInput(values.offset));
  }, [values.offset]);

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

  async function setOffset(): Promise<void> {
    const offsetPf = Number(offsetText);
    if (!Number.isFinite(offsetPf)) {
      onError("Offset must be a finite pF value");
      return;
    }
    await run("Offset saved; baseline was invalidated", () => client!.setOffsetCell(row, col, offsetPf).then(() => undefined));
  }

  async function zeroCurrent(scope: OffsetScope): Promise<void> {
    await run("Current value zeroed; baseline was invalidated", () =>
      client!.zeroCurrentOffsets(scope, scope === "all" ? undefined : row, scope === "cell" ? col : undefined).then(() => undefined)
    );
  }

  async function clear(scope: OffsetScope): Promise<void> {
    await run("Offset cleared; baseline was invalidated", () =>
      client!.clearOffsets(scope, scope === "all" ? undefined : row, scope === "cell" ? col : undefined).then(() => undefined)
    );
  }

  return (
    <div className="offsetPanel">
      <div className="offsetGrid" role="grid" aria-label="Offset cell grid">
        {Array.from({ length: 8 }, (_, rowIndex) =>
          Array.from({ length: 8 }, (_, colIndex) => {
            const selected = rowIndex + 1 === row && colIndex + 1 === col;
            const offset = snapshot?.matrix.userOffsetPf[rowIndex]?.[colIndex];
            return (
              <button
                key={`${rowIndex}-${colIndex}`}
                className={selected ? "offsetCell active" : "offsetCell"}
                title={cellLabel(rowIndex, colIndex)}
                onClick={() => {
                  setRow(rowIndex + 1);
                  setCol(colIndex + 1);
                }}
              >
                <span>{cellLabel(rowIndex, colIndex)}</span>
                <small>{formatPf(offset)}</small>
              </button>
            );
          })
        )}
      </div>

      <div className="offsetControls">
        <label>
          Row
          <select value={row} onChange={(event) => setRow(Number(event.target.value))}>
            {Array.from({ length: 8 }, (_, index) => index + 1).map((value) => (
              <option key={value} value={value}>
                S{value}
              </option>
            ))}
          </select>
        </label>
        <label>
          Detector
          <select value={col} onChange={(event) => setCol(Number(event.target.value))}>
            {Array.from({ length: 8 }, (_, index) => index + 1).map((value) => (
              <option key={value} value={value}>
                D{value}
              </option>
            ))}
          </select>
        </label>
        <label>
          Offset pF
          <input value={offsetText} inputMode="decimal" onChange={(event) => setOffsetText(event.target.value)} />
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

      <div className="offsetButtons">
        <button disabled={!client || pending} onClick={() => void setOffset()}>
          <Save size={16} /> Set offset
        </button>
        <button disabled={!client || pending || !snapshot?.frame.valid} onClick={() => void zeroCurrent("cell")}>
          <LocateFixed size={16} /> Zero selected
        </button>
        <button disabled={!client || pending || !snapshot?.frame.valid} onClick={() => void zeroCurrent("row")}>
          <LocateFixed size={16} /> Zero row
        </button>
        <button disabled={!client || pending || !snapshot?.frame.valid} onClick={() => void zeroCurrent("all")}>
          <LocateFixed size={16} /> Zero all
        </button>
        <button disabled={!client || pending} onClick={() => void clear("cell")}>
          <Eraser size={16} /> Clear selected
        </button>
        <button disabled={!client || pending} onClick={() => void clear("row")}>
          <Eraser size={16} /> Clear row
        </button>
        <button disabled={!client || pending} onClick={() => void clear("all")}>
          <RotateCcw size={16} /> Clear all
        </button>
      </div>
      {status ? <div className="inlineNotice">{status}</div> : null}
    </div>
  );
}

function cellValues(snapshot: BackendSnapshotPayload | null, row: number, col: number) {
  const rowIndex = row - 1;
  const colIndex = col - 1;
  return {
    raw: snapshot?.matrix.rawPf[rowIndex]?.[colIndex],
    corrected: snapshot?.matrix.correctedPf[rowIndex]?.[colIndex],
    offset: snapshot?.matrix.userOffsetPf[rowIndex]?.[colIndex],
    displayed: snapshot?.matrix.displayValues[rowIndex]?.[colIndex]
  };
}

function formatPf(value: number | null | undefined, digits = 3): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "NA";
}

function formatInput(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "0";
}
