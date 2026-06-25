import type { BackendSnapshotPayload } from "../api/types";

export type HeatmapDatum = [number, number, number | null, string, boolean];

export function resolveColourRange(data: HeatmapDatum[], snapshot: BackendSnapshotPayload): [number, number] {
  const range = snapshot.display.colorRange;
  if (typeof range.min === "number" && typeof range.max === "number" && range.max > range.min) {
    return [range.min, range.max];
  }
  const values = data.map((item) => item[2]).filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  if (!values.length) {
    return snapshot.matrix.unit === "%" ? [-0.5, 0.5] : [0, 1];
  }
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  if (snapshot.matrix.unit === "%") {
    const extent = Math.max(Math.abs(minimum), Math.abs(maximum), 0.5);
    return [-extent, extent];
  }
  const span = maximum - minimum;
  const padding = span === 0 ? Math.max(Math.abs(minimum) * 0.02, 0.5) : span * 0.02;
  return [minimum - padding, maximum + padding];
}
