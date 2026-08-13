import type { BackendSnapshotPayload, CommandLineEnding, MeasurementMode, RowMeasurementMode, SetupProfile, TransportMode } from "../api/types";

export const setupProfileSchemaVersion = 3;

export function defaultSetupProfile(runtimeDirectory: string): SetupProfile {
  return {
    schemaVersion: setupProfileSchemaVersion,
    appVersion: "1.0.0",
    transport: {
      mode: "serial",
      serial: { port: "", baud: 115200 },
      wifi: { host: "192.168.4.1", fallbackHost: "192.168.4.1" },
      ble: { address: "", deviceId: "" },
      replay: { path: "", speed: 1 }
    },
    acquisition: { rows: 8, measurementMode: "CAP", rowModes: allRows("CAP") },
    voltageRail: {
      measuredAvddV: null,
      measuredAvssV: null
    },
    display: {
      displayMode: "absolute_pf",
      measurementDomain: "auto",
      showCellText: true,
      pauseDisplay: false,
      freezeColor: false,
      unitMode: "auto",
      circuitOffsetPf: 33,
      trendLatestN: 600
    },
    offsetsPf: zeroOffsets(),
    command: { lineEnding: "lf" },
    paths: { defaultSaveDirectory: runtimeDirectory }
  };
}

export function setupProfileFromSnapshot(snapshot: BackendSnapshotPayload | null, current: SetupProfile): SetupProfile {
  if (!snapshot) {
    return current;
  }
  return {
    ...current,
    transport: {
      ...current.transport,
      mode: snapshot.connection.mode,
      serial: { ...current.transport.serial },
      wifi: { ...current.transport.wifi },
      ble: { ...current.transport.ble },
      replay: { ...current.transport.replay }
    },
    acquisition: {
      rows: snapshot.frame.rows,
      measurementMode: snapshot.measurement?.appliedMode ?? current.acquisition.measurementMode,
      rowModes: normaliseRowModes(
        snapshot.measurement?.rowProfile?.appliedModes ?? snapshot.frame.rowModes ?? snapshot.matrix.modeByRow,
        current.acquisition.rowModes
      )
    },
    voltageRail: {
      measuredAvddV: finiteOrExisting(snapshot.measurement?.rail.measuredAvddV, current.voltageRail.measuredAvddV),
      measuredAvssV: finiteOrExisting(snapshot.measurement?.rail.measuredAvssV, current.voltageRail.measuredAvssV)
    },
    display: {
      displayMode: snapshot.display.displayMode,
      measurementDomain: snapshot.display.measurementDomain,
      showCellText: snapshot.display.showCellText,
      pauseDisplay: snapshot.display.pauseDisplay,
      freezeColor: snapshot.display.freezeColor,
      unitMode: snapshot.display.unitMode,
      circuitOffsetPf: snapshot.display.circuitOffsetPf,
      trendLatestN: snapshot.display.trendLatestN
    },
    offsetsPf: normaliseOffsets(snapshot.matrix.userOffsetPf, current.offsetsPf)
  };
}

export function normaliseSetupProfile(value: unknown, runtimeDirectory: string): SetupProfile {
  const fallback = defaultSetupProfile(runtimeDirectory);
  if (!value || typeof value !== "object") {
    return fallback;
  }
  const payload = value as Partial<SetupProfile>;
  if (
    payload.schemaVersion !== undefined &&
    payload.schemaVersion !== 1 &&
    payload.schemaVersion !== 2 &&
    payload.schemaVersion !== setupProfileSchemaVersion
  ) {
    throw new Error("Unsupported setup profile schemaVersion");
  }
  const transport = objectValue<SetupProfile["transport"]>(payload.transport);
  const serial = objectValue<SetupProfile["transport"]["serial"]>(transport.serial);
  const wifi = objectValue<SetupProfile["transport"]["wifi"]>(transport.wifi);
  const ble = objectValue<SetupProfile["transport"]["ble"]>(transport.ble);
  const replay = objectValue<SetupProfile["transport"]["replay"]>(transport.replay);
  const display = objectValue<SetupProfile["display"]>(payload.display);
  const command = objectValue<SetupProfile["command"]>(payload.command);
  const paths = objectValue<SetupProfile["paths"]>(payload.paths);
  const acquisition = objectValue<SetupProfile["acquisition"]>(payload.acquisition);
  const voltageRail = objectValue<SetupProfile["voltageRail"]>(payload.voltageRail);
  const baud = finitePositive(serial.baud ?? fallback.transport.serial.baud, "transport.serial.baud");
  const rows = supportedRows(acquisition.rows ?? fallback.acquisition.rows);
  const lineEnding = normaliseLineEnding(command.lineEnding ?? fallback.command.lineEnding);
  const defaultSaveDirectory = String(paths.defaultSaveDirectory || runtimeDirectory).trim();
  if (!defaultSaveDirectory) {
    throw new Error("paths.defaultSaveDirectory must not be empty");
  }
  const measurementMode = normaliseMeasurementMode(acquisition.measurementMode ?? fallback.acquisition.measurementMode);
  return {
    schemaVersion: setupProfileSchemaVersion,
    appVersion: typeof payload.appVersion === "string" ? payload.appVersion : fallback.appVersion,
    transport: {
      mode: normaliseTransportMode(transport.mode ?? fallback.transport.mode),
      serial: { port: String(serial.port ?? fallback.transport.serial.port ?? ""), baud },
      wifi: {
        host: String(wifi.host ?? fallback.transport.wifi.host),
        fallbackHost: String(wifi.fallbackHost ?? fallback.transport.wifi.fallbackHost)
      },
      ble: { address: String(ble.address ?? fallback.transport.ble.address ?? ""), deviceId: String(ble.deviceId ?? fallback.transport.ble.deviceId ?? "") },
      replay: { path: String(replay.path ?? fallback.transport.replay.path ?? ""), speed: finitePositive(replay.speed ?? fallback.transport.replay.speed, "transport.replay.speed") }
    },
    acquisition: {
      rows,
      measurementMode,
      rowModes: normaliseRowModes(acquisition.rowModes, allRows(measurementMode))
    },
    voltageRail: {
      measuredAvddV: optionalFiniteNumber(voltageRail.measuredAvddV ?? fallback.voltageRail.measuredAvddV, "voltageRail.measuredAvddV"),
      measuredAvssV: optionalFiniteNumber(voltageRail.measuredAvssV ?? fallback.voltageRail.measuredAvssV, "voltageRail.measuredAvssV")
    },
    display: {
      displayMode: display.displayMode === "delta_percent" ? "delta_percent" : fallback.display.displayMode,
      measurementDomain: String(display.measurementDomain ?? fallback.display.measurementDomain),
      showCellText: Boolean(display.showCellText ?? fallback.display.showCellText),
      pauseDisplay: Boolean(display.pauseDisplay ?? fallback.display.pauseDisplay),
      freezeColor: Boolean(display.freezeColor ?? fallback.display.freezeColor),
      unitMode: String(display.unitMode ?? fallback.display.unitMode),
      circuitOffsetPf: finiteNumber(display.circuitOffsetPf ?? fallback.display.circuitOffsetPf, "display.circuitOffsetPf"),
      trendLatestN: integerRange(display.trendLatestN ?? fallback.display.trendLatestN, 1, 18000, "display.trendLatestN")
    },
    offsetsPf: normaliseOffsets(payload.offsetsPf, fallback.offsetsPf),
    command: { lineEnding },
    paths: { defaultSaveDirectory }
  };
}

function zeroOffsets(): number[][] {
  return Array.from({ length: 8 }, () => Array.from({ length: 8 }, () => 0));
}

function objectValue<T>(value: unknown): Partial<T> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Partial<T>) : {};
}

function normaliseOffsets(value: unknown, fallback: number[][]): number[][] {
  if (value === undefined || value === null) {
    return fallback.map((row) => [...row]);
  }
  if (!Array.isArray(value) || value.length !== 8) {
    throw new Error("offsetsPf must be an 8x8 matrix");
  }
  return value.map((row, rowIndex) => {
    if (!Array.isArray(row) || row.length !== 8) {
      throw new Error("offsetsPf must be an 8x8 matrix");
    }
    return row.map((item, colIndex) => finiteNumber(item, `offsetsPf[${rowIndex}][${colIndex}]`));
  });
}

function finiteNumber(value: unknown, name: string): number {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    throw new Error(`${name} must be finite`);
  }
  return number;
}

function finitePositive(value: unknown, name: string): number {
  const number = finiteNumber(value, name);
  if (number <= 0) {
    throw new Error(`${name} must be positive`);
  }
  return number;
}

function integerRange(value: unknown, min: number, max: number, name: string): number {
  const number = Math.trunc(finiteNumber(value, name));
  if (number < min || number > max) {
    throw new Error(`${name} must be ${min}..${max}`);
  }
  return number;
}

function supportedRows(value: unknown): number {
  const rows = finiteNumber(value, "acquisition.rows");
  if (!Number.isInteger(rows) || rows < 1 || rows > 8) {
    throw new Error("acquisition.rows must be an integer from 1 through 8");
  }
  return rows;
}

function normaliseTransportMode(value: unknown): TransportMode {
  return value === "ble" || value === "wifi" || value === "replay" ? value : "serial";
}

function normaliseMeasurementMode(value: unknown): MeasurementMode {
  const mode = String(value ?? "").trim().toUpperCase();
  return mode === "VOLT" || mode === "RES" ? mode : "CAP";
}

export function normaliseRowModes(value: unknown, fallback: RowMeasurementMode[] = allRows("CAP")): RowMeasurementMode[] {
  if (value === undefined || value === null) {
    return [...fallback];
  }
  if (!Array.isArray(value) || value.length !== 8) {
    throw new Error("acquisition.rowModes must contain exactly 8 modes");
  }
  return value.map((mode, index) => {
    const normalised = String(mode ?? "").trim().toUpperCase();
    if (normalised !== "CAP" && normalised !== "VOLT" && normalised !== "RES") {
      throw new Error(`acquisition.rowModes[${index}] must be CAP, VOLT, or RES`);
    }
    return normalised;
  });
}

function allRows(mode: RowMeasurementMode): RowMeasurementMode[] {
  return Array.from({ length: 8 }, () => mode);
}

function optionalFiniteNumber(value: unknown, name: string): number | null {
  if (value === undefined || value === null || value === "") {
    return null;
  }
  return finiteNumber(value, name);
}

function finiteOrExisting(value: number | null | undefined, existing: number | null): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : existing;
}

function normaliseLineEnding(value: unknown): CommandLineEnding {
  if (value === "crlf" || value === "none") {
    return value;
  }
  return "lf";
}
