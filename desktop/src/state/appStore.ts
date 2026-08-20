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
  if (incoming.frame.layout === "MIXED") {
    const profile = incoming.measurement?.rowProfile;
    if (
      typeof profile?.generation === "number" &&
      typeof incoming.frame.profileGeneration === "number" &&
      incoming.frame.profileGeneration !== profile.generation
    ) {
      return false;
    }
    if (
      typeof profile?.requestId === "number" &&
      typeof incoming.frame.profileRequestId === "number" &&
      incoming.frame.profileRequestId !== profile.requestId
    ) {
      return false;
    }
    if (
      typeof profile?.frameSeq === "number" &&
      typeof incoming.frame.seq === "number" &&
      incoming.frame.seq < profile.frameSeq
    ) {
      return false;
    }
    const currentGeneration = currentVisual?.frame.profileGeneration;
    if (
      currentVisual?.frame.layout === "MIXED" &&
      typeof currentGeneration === "number" &&
      typeof incoming.frame.profileGeneration === "number" &&
      incoming.frame.profileGeneration < currentGeneration
    ) {
      return false;
    }
    return true;
  }
  const profileAuthority = homogeneousProfileAuthority(incoming);
  const appliedMode = profileAuthority?.mode ?? appliedMeasurementMode(incoming);
  if (measurementQuantity(incoming) !== quantityForMode(appliedMode)) {
    return false;
  }
  if (profileAuthority) {
    if (
      typeof profileAuthority.generation === "number" &&
      typeof incoming.frame.profileGeneration === "number" &&
      incoming.frame.profileGeneration !== profileAuthority.generation
    ) {
      return false;
    }
    if (
      typeof profileAuthority.requestId === "number" &&
      typeof incoming.frame.profileRequestId === "number" &&
      incoming.frame.profileRequestId !== profileAuthority.requestId
    ) {
      return false;
    }
  }
  if (appliedMode === "CAP") {
    // CAP frame gen/rid belongs to ROWS, not to the MODE transaction.  The
    // MAPP sequence is the only authoritative mode boundary for CAP.
    const boundarySeq = profileAuthority?.frameSeq ?? incoming.measurement?.frameSeq;
    if (typeof boundarySeq === "number" && typeof incoming.frame.seq === "number" && incoming.frame.seq < boundarySeq) {
      return false;
    }
    const currentSeq = currentVisual?.frame.seq;
    return !(
      currentVisual &&
      effectiveHomogeneousMode(currentVisual) === "CAP" &&
      typeof currentSeq === "number" &&
      typeof incoming.frame.seq === "number" &&
      incoming.frame.seq < currentSeq
    );
  }
  const appliedGeneration = profileAuthority?.generation ?? incoming.measurement?.generation;
  const matrixGeneration = incoming.matrix.generation;
  if (typeof appliedGeneration === "number" && matrixGeneration !== appliedGeneration) {
    return false;
  }
  const currentGeneration = currentVisual?.matrix.generation;
  if (
    currentVisual &&
    effectiveHomogeneousMode(currentVisual) === appliedMode &&
    typeof currentGeneration === "number" &&
    typeof matrixGeneration === "number" &&
    matrixGeneration < currentGeneration
  ) {
    return false;
  }
  return true;
}

type HomogeneousProfileAuthority = {
  mode: "CAP" | "VOLT" | "RES";
  generation: number | null;
  requestId: number | null;
  frameSeq: number | null;
};

function homogeneousProfileAuthority(snapshot: BackendSnapshotPayload | null): HomogeneousProfileAuthority | null {
  const profile = snapshot?.measurement?.rowProfile;
  const modes = profile?.appliedModes;
  // Firmware 8045e9e9 chooses the legacy versus mixed frame grammar from the
  // complete persisted eight-row profile. Inactive configured rows therefore
  // remain semantically relevant: ROWS=4 + CCCCRVVR is mixed, not CAP.
  const savedModes = Array.isArray(modes) && modes.length === 8 ? modes : [];
  if (savedModes.length !== 8 || savedModes.some((mode) => mode !== savedModes[0])) {
    return null;
  }
  const mode = savedModes[0];
  if (mode !== "CAP" && mode !== "VOLT" && mode !== "RES") {
    return null;
  }
  if (!profile) {
    return null;
  }
  return {
    mode,
    generation: profile.generation,
    requestId: profile.requestId,
    frameSeq: profile.frameSeq
  };
}

function effectiveHomogeneousMode(snapshot: BackendSnapshotPayload | null): "CAP" | "VOLT" | "RES" {
  return homogeneousProfileAuthority(snapshot)?.mode ?? appliedMeasurementMode(snapshot);
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
