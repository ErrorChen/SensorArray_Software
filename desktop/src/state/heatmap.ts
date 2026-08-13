import type { BackendSnapshotPayload, ColourDomain, DisplayMode, MeasurementMode } from "../api/types";

export type HeatmapDatum = [number, number, number | null, string, boolean, MeasurementMode?];

const minimumExtents: Record<ColourDomain, number> = {
  cap_absolute: 1,
  cap_delta: 0.5,
  voltage: 0.001,
  resistance: 1
};

export function colourDomainForMode(mode: MeasurementMode, displayMode: DisplayMode): ColourDomain {
  if (mode === "VOLT") return "voltage";
  if (mode === "RES") return "resistance";
  return displayMode === "delta_percent" ? "cap_delta" : "cap_absolute";
}

export function resolveColourRange(
  data: HeatmapDatum[],
  snapshot: BackendSnapshotPayload,
  domain: ColourDomain = colourDomainForMode(snapshot.measurement?.appliedMode ?? "CAP", snapshot.display.displayMode)
): [number, number] {
  // New snapshots are authoritative and maintain frozen/last-good ranges per
  // physical domain. The singular legacy field is only safe for homogeneous
  // replay snapshots where it cannot mix pF, V, and ohms.
  const authoritative = snapshot.display.colourRanges?.[domain];
  if (isNondegenerateRange(authoritative)) {
    return [authoritative.min, authoritative.max];
  }
  const legacy = snapshot.display.colorRange;
  const hasTypedRanges = Boolean(snapshot.display.colourRanges);
  // Firmware 331c445 selects frame layout from all eight saved modes, even
  // when inactive configured rows are the only source of heterogeneity.
  const savedModes = snapshot.frame.rowModes ?? snapshot.matrix.modeByRow ?? [];
  const homogeneous = snapshot.frame.layout !== "MIXED" && savedModes.length === 8 && new Set(savedModes).size === 1;
  if (!hasTypedRanges && homogeneous && isNondegenerateRange(legacy)) {
    return [legacy.min, legacy.max];
  }

  const values = data
    .filter((item) => item[4] && item[1] < Math.max(1, Math.min(8, snapshot.frame.rows)))
    .map((item) => item[2])
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  if (!values.length) {
    return coldStartRange(domain);
  }
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  if (domain === "voltage" || domain === "cap_delta") {
    const extent = Math.max(Math.abs(minimum), Math.abs(maximum), minimumExtents[domain]) * 1.05;
    return [-extent, extent];
  }
  if (maximum > minimum) {
    const padding = (maximum - minimum) * 0.02;
    return [minimum - padding, maximum + padding];
  }
  if (maximum > 0) {
    return [0, Math.max(maximum * 1.05, minimumExtents[domain])];
  }
  const extent = Math.max(Math.abs(maximum) * 1.05, minimumExtents[domain]);
  return [-extent, extent];
}

function coldStartRange(domain: ColourDomain): [number, number] {
  const extent = minimumExtents[domain];
  return domain === "voltage" || domain === "cap_delta" ? [-extent, extent] : [0, extent];
}

function isNondegenerateRange(value: { min: number | null; max: number | null } | undefined): value is { min: number; max: number } {
  return typeof value?.min === "number" && Number.isFinite(value.min)
    && typeof value.max === "number" && Number.isFinite(value.max)
    && value.max > value.min;
}
