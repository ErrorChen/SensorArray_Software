import type { BackendSnapshotPayload, SelectionSnapshot } from "../api/types";
import { appliedMeasurementMode, measurementQuantity, quantityForMode } from "./measurement";

export function selectionTitle(selection: Partial<SelectionSnapshot> | undefined): string {
  if (!selection) {
    return "S1 Primary FDC D1-D4";
  }
  if (selection.title) {
    return selection.title;
  }
  const rowLabel = selection.rowLabel ?? `S${(selection.rowIndex ?? 0) + 1}`;
  const group = selection.fdcGroup ?? "primary";
  const label = group === "primary" ? "Primary FDC" : "Secondary FDC";
  const start = selection.detectorStart ?? (group === "primary" ? 1 : 5);
  const end = selection.detectorEnd ?? (group === "primary" ? 4 : 8);
  return `${rowLabel} ${label} D${start}-D${end}`;
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
    return frozenVisualWithCurrentStatus(incoming, currentVisual);
  }
  if (currentVisual && !measurementMatrixIsCurrent(incoming, currentVisual)) {
    return frozenVisualWithCurrentStatus(incoming, currentVisual);
  }
  return incoming;
}

export function measurementMatrixIsCurrent(incoming: BackendSnapshotPayload, currentVisual: BackendSnapshotPayload | null): boolean {
  const appliedMode = appliedMeasurementMode(incoming);
  if (measurementQuantity(incoming) !== quantityForMode(appliedMode)) {
    return false;
  }
  if (appliedMode === "CAP") {
    // CAP frame gen/rid belongs to ROWS, not to the MODE transaction.  The
    // MAPP sequence is the only authoritative mode boundary for CAP.
    const boundarySeq = incoming.measurement?.frameSeq;
    if (typeof boundarySeq === "number" && typeof incoming.frame.seq === "number" && incoming.frame.seq < boundarySeq) {
      return false;
    }
    const currentSeq = currentVisual?.frame.seq;
    return !(
      currentVisual &&
      appliedMeasurementMode(currentVisual) === "CAP" &&
      typeof currentSeq === "number" &&
      typeof incoming.frame.seq === "number" &&
      incoming.frame.seq < currentSeq
    );
  }
  const appliedGeneration = incoming.measurement?.generation;
  const matrixGeneration = incoming.matrix.generation;
  if (typeof appliedGeneration === "number" && matrixGeneration !== appliedGeneration) {
    return false;
  }
  const currentGeneration = currentVisual?.matrix.generation;
  if (
    currentVisual &&
    appliedMeasurementMode(currentVisual) === appliedMode &&
    typeof currentGeneration === "number" &&
    typeof matrixGeneration === "number" &&
    matrixGeneration < currentGeneration
  ) {
    return false;
  }
  return true;
}

function frozenVisualWithCurrentStatus(incoming: BackendSnapshotPayload, currentVisual: BackendSnapshotPayload): BackendSnapshotPayload {
  return {
    ...currentVisual,
    connection: incoming.connection,
    logs: incoming.logs,
    discovery: incoming.discovery,
    display: incoming.display,
    commands: incoming.commands,
    diagnostics: incoming.diagnostics,
    battery: incoming.battery,
    ads: incoming.ads,
    rates: incoming.rates
  };
}
