import type { BackendSnapshotPayload, SelectionSnapshot } from "../api/types";

export function selectionTitle(selection: Partial<SelectionSnapshot> | undefined): string {
  if (!selection) {
    return "S1 · Primary FDC · D1-D4";
  }
  if (selection.title) {
    return selection.title;
  }
  const rowLabel = selection.rowLabel ?? `S${(selection.rowIndex ?? 0) + 1}`;
  const group = selection.fdcGroup ?? "primary";
  const label = group === "primary" ? "Primary FDC" : "Secondary FDC";
  const start = selection.detectorStart ?? (group === "primary" ? 1 : 5);
  const end = selection.detectorEnd ?? (group === "primary" ? 4 : 8);
  return `${rowLabel} · ${label} · D${start}-D${end}`;
}

export function cellLabel(rowIndex: number, colIndex: number): string {
  return `S${rowIndex + 1}D${colIndex + 1}`;
}

export function selectedCells(selection: SelectionSnapshot | undefined): Set<string> {
  return new Set(selection?.cells ?? []);
}

export function snapshotForDisplay(
  incoming: BackendSnapshotPayload,
  currentVisual: BackendSnapshotPayload | null
): BackendSnapshotPayload {
  if (incoming.display.pauseDisplay && currentVisual) {
    return {
      ...currentVisual,
      connection: incoming.connection,
      logs: incoming.logs,
      discovery: incoming.discovery,
      display: incoming.display
    };
  }
  return incoming;
}

