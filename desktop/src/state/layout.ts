export type SplitLimits = {
  minLeftRatio?: number;
  minLeftPx?: number;
  minRightPx: number;
};

export function clampSplitRatio(rawRatio: number, containerWidth: number, limits: SplitLimits): number {
  if (!Number.isFinite(containerWidth) || containerWidth <= 0) {
    return rawRatio;
  }
  let minimum = limits.minLeftRatio ?? (limits.minLeftPx ?? 0) / containerWidth;
  let maximum = 1 - limits.minRightPx / containerWidth;
  if (maximum < minimum) {
    const constrained = Math.min(Math.max(minimum, 0.1), 0.9);
    minimum = constrained;
    maximum = constrained;
  }
  return Math.min(Math.max(rawRatio, minimum), maximum);
}
