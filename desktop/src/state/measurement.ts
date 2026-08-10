import type {
  BackendSnapshotPayload,
  MatrixSnapshot,
  MeasurementMode,
  MeasurementQuantity,
  MeasurementTransitionState
} from "../api/types";

export type CellMeasurementState = {
  value: number | null;
  rawFixed: number | null;
  valid: boolean;
  fresh: boolean;
  errorCode: number | null;
  errorReason: string | null;
  pga: number | null;
  pgaBypass: boolean;
};

const modeQuantity: Record<MeasurementMode, MeasurementQuantity> = {
  CAP: "capacitance",
  VOLT: "voltage",
  RES: "resistance"
};

const quantityLabels: Record<MeasurementQuantity, string> = {
  capacitance: "Capacitance",
  voltage: "Voltage",
  resistance: "Resistance"
};

export function appliedMeasurementMode(snapshot: BackendSnapshotPayload | null | undefined): MeasurementMode {
  return snapshot?.measurement?.appliedMode ?? modeFromQuantity(snapshot?.matrix?.quantity) ?? "CAP";
}

export function pendingMeasurementMode(snapshot: BackendSnapshotPayload | null | undefined): MeasurementMode | null {
  return snapshot?.measurement?.pendingMode ?? null;
}

export function measurementQuantity(snapshot: BackendSnapshotPayload | null | undefined): MeasurementQuantity {
  return snapshot?.matrix?.quantity ?? modeQuantity[appliedMeasurementMode(snapshot)];
}

export function quantityForMode(mode: MeasurementMode): MeasurementQuantity {
  return modeQuantity[mode];
}

export function quantityLabel(quantity: MeasurementQuantity): string {
  return quantityLabels[quantity];
}

export function isCapacitanceMode(snapshot: BackendSnapshotPayload | null | undefined): boolean {
  return appliedMeasurementMode(snapshot) === "CAP";
}

export function isTransitionPending(state: MeasurementTransitionState | undefined): boolean {
  return state === "requested" || state === "accepted" || state === "configuring_rail";
}

export function transitionDescription(snapshot: BackendSnapshotPayload | null | undefined): string {
  const measurement = snapshot?.measurement;
  if (!measurement) {
    return `Applied: ${appliedMeasurementMode(snapshot)}`;
  }
  const request = measurement.requestId === null ? "" : ` #${measurement.requestId}`;
  if (measurement.transitionState === "configuring_rail") {
    return `Configuring measured voltage rails${request}`;
  }
  if (measurement.transitionState === "requested") {
    return `Requesting ${measurement.pendingMode ?? measurement.appliedMode}${request}`;
  }
  if (measurement.transitionState === "accepted") {
    return `Waiting for firmware apply (MAPP${request})`;
  }
  if (measurement.transitionState === "timeout") {
    return `Mode transition timed out${request}`;
  }
  if (measurement.transitionState === "error") {
    return measurement.error || `Mode transition failed${request}`;
  }
  return `Applied: ${measurement.appliedMode}`;
}

export function cellMeasurementState(matrix: MatrixSnapshot, row: number, col: number): CellMeasurementState {
  const value = finiteOrNull(matrix.displayValues?.[row]?.[col] ?? matrix.values?.[row]?.[col]);
  const legacyValid = matrix.validMask?.[row]?.[col];
  const valid = matrix.valid?.[row]?.[col] ?? legacyValid ?? false;
  const fresh = matrix.fresh?.[row]?.[col] ?? true;
  const errorCode = finiteIntegerOrNull(matrix.errorCodes?.[row]?.[col]);
  const providedReason = matrix.errorReasons?.[row]?.[col];
  return {
    value,
    rawFixed: finiteOrNull(matrix.rawFixed?.[row]?.[col]),
    valid,
    fresh,
    errorCode,
    errorReason: errorReason(errorCode, providedReason),
    pga: finiteNonNegativeOrNull(matrix.pga?.[row]?.[col]),
    pgaBypass: Boolean(matrix.pgaBypass?.[row]?.[col])
  };
}

export function isCellDisplayable(cell: CellMeasurementState): boolean {
  return cell.valid && cell.fresh && typeof cell.value === "number" && Number.isFinite(cell.value);
}

export function matrixDisplayUnit(snapshot: BackendSnapshotPayload): string {
  if (measurementQuantity(snapshot) === "capacitance" && snapshot.display.displayMode === "delta_percent") {
    return "%";
  }
  return snapshot.matrix.unit === "ohm" ? "Ω" : snapshot.matrix.unit;
}

export function formatMeasurementValue(
  value: number | null | undefined,
  quantity: MeasurementQuantity,
  options: { compact?: boolean; percent?: boolean } = {}
): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "NA";
  }
  if (options.percent) {
    return `${formatNumber(value, options.compact ? 2 : 3)} %`;
  }
  if (quantity === "voltage") {
    return formatEngineering(value, [
      { threshold: 1, multiplier: 1, unit: "V" },
      { threshold: 1e-3, multiplier: 1e3, unit: "mV" },
      { threshold: 0, multiplier: 1e6, unit: "µV" }
    ], options.compact);
  }
  if (quantity === "resistance") {
    const absolute = Math.abs(value);
    if (absolute >= 1e6) {
      return `${formatNumber(value / 1e6, options.compact ? 2 : 3)} MΩ`;
    }
    if (absolute >= 1e3) {
      return `${formatNumber(value / 1e3, options.compact ? 2 : 3)} kΩ`;
    }
    if (absolute > 0 && absolute < 1) {
      return `${formatNumber(value * 1e3, options.compact ? 1 : 3)} mΩ`;
    }
    return `${formatNumber(value, options.compact ? 2 : 3)} Ω`;
  }
  return `${formatNumber(value, options.compact ? 2 : 3)} pF`;
}

export function pgaLabel(pga: number | null | undefined, bypass: boolean): string {
  if (bypass || pga === 0) {
    return "PGA bypass";
  }
  if (typeof pga !== "number" || !Number.isFinite(pga) || pga < 0) {
    return "PGA unavailable";
  }
  return `PGA ×${pga}`;
}

export function formatErrorCode(code: number | null | undefined): string {
  if (typeof code !== "number" || !Number.isInteger(code) || code < 0) {
    return "none";
  }
  return `0x${code.toString(16).toUpperCase().padStart(2, "0")}`;
}

export function errorReason(code: number | null | undefined, provided: string | null | undefined): string | null {
  if (provided && provided.trim()) {
    return provided.trim();
  }
  if (typeof code !== "number" || !Number.isInteger(code) || code < 0) {
    return null;
  }
  return `Unknown firmware cell error ${formatErrorCode(code)}`;
}

export function rawFixedLabel(mode: MeasurementMode): string {
  if (mode === "VOLT") {
    return "Raw integer µV";
  }
  if (mode === "RES") {
    return "Raw integer mΩ";
  }
  return "Raw fixed value";
}

function modeFromQuantity(quantity: MeasurementQuantity | undefined): MeasurementMode | null {
  if (quantity === "voltage") {
    return "VOLT";
  }
  if (quantity === "resistance") {
    return "RES";
  }
  if (quantity === "capacitance") {
    return "CAP";
  }
  return null;
}

function finiteOrNull(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function finiteIntegerOrNull(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

function finiteNonNegativeOrNull(value: number | null | undefined): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

function formatEngineering(
  value: number,
  ranges: Array<{ threshold: number; multiplier: number; unit: string }>,
  compact = false
): string {
  const absolute = Math.abs(value);
  const range = ranges.find((candidate) => absolute >= candidate.threshold) ?? ranges[ranges.length - 1];
  return `${formatNumber(value * range.multiplier, compact ? 2 : 3)} ${range.unit}`;
}

function formatNumber(value: number, digits: number): string {
  if (value === 0) {
    return (0).toFixed(digits);
  }
  return value.toFixed(digits);
}
