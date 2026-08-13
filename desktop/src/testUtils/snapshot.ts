import type {
  BackendSnapshotPayload,
  DisplayMode,
  MeasurementMode,
  MeasurementQuantity,
  MeasurementUnit
} from "../api/types";

type SnapshotOptions = {
  mode?: MeasurementMode;
  quantity?: MeasurementQuantity;
  unit?: MeasurementUnit | "%";
  displayMode?: DisplayMode;
};

export function createBackendSnapshot(options: SnapshotOptions = {}): BackendSnapshotPayload {
  const mode = options.mode ?? "CAP";
  const quantity = options.quantity ?? (mode === "VOLT" ? "voltage" : mode === "RES" ? "resistance" : "capacitance");
  const unit = options.unit ?? (mode === "VOLT" ? "V" : mode === "RES" ? "ohm" : "pF");
  const numbers = numberMatrix(0);
  const booleans = booleanMatrix(true);
  return {
    connection: { mode: "serial", state: "connected", deviceLabel: "SERIAL_TEST_PORT", generation: 1 },
    measurement: {
      appliedMode: mode,
      pendingMode: null,
      transitionState: "applied",
      requestId: null,
      generation: 1,
      frameSeq: 1,
      error: "",
      rail: {
        configured: false,
        state: "unconfigured",
        requestId: null,
        measuredAvddV: null,
        measuredAvssV: null
      },
      railTelemetry: {
        railSpanUv: null,
        valid: false,
        fresh: false,
        age: null,
        source: "internal_monitor",
        reason: "not_measured",
        timestamp: null
      },
      rowProfile: {
        appliedModes: Array.from({ length: 8 }, () => mode),
        pendingModes: null,
        transitionState: "applied",
        requestId: null,
        generation: 1,
        frameSeq: 1,
        error: ""
      }
    },
    frame: {
      seq: 1,
      fps: 20,
      rows: 8,
      valid: true,
      timestampUs: 1000,
      revision: 1,
      layout: "HOMOGENEOUS",
      rowModes: Array.from({ length: 8 }, () => mode),
      profileGeneration: 1,
      profileRequestId: null
    },
    matrix: {
      rows: Array.from({ length: 8 }, (_, index) => `S${index + 1}`),
      cols: Array.from({ length: 8 }, (_, index) => `D${index + 1}`),
      quantity,
      unit,
      scale: mode === "RES" ? -3 : -6,
      values: numbers.map((row) => [...row]),
      displayValues: numbers.map((row) => [...row]),
      rawFixed: numbers.map((row) => [...row]),
      valid: booleans.map((row) => [...row]),
      fresh: booleans.map((row) => [...row]),
      error: booleanMatrix(false),
      errorCodes: nullableNumberMatrix(),
      errorReasons: nullableStringMatrix(),
      pga: nullableNumberMatrix(),
      pgaBypass: booleanMatrix(false),
      sourceTransport: "serial",
      generation: 1,
      requestId: null,
      diagnostics: {},
      correctedPf: numbers.map((row) => [...row]),
      rawPf: numbers.map((row) => [...row]),
      userOffsetPf: numbers.map((row) => [...row]),
      validMask: booleans.map((row) => [...row]),
      domain: quantity,
      modeByRow: Array.from({ length: 8 }, () => mode),
      unitByRow: Array.from({ length: 8 }, () => unit),
      scaleByRow: Array.from({ length: 8 }, () => mode === "RES" ? -3 : -6)
    },
    selection: {
      rowIndex: 0,
      rowLabel: "S1",
      fdcGroup: "primary",
      detectorStart: 1,
      detectorEnd: 4,
      cells: ["S1D1", "S1D2", "S1D3", "S1D4"],
      title: "S1 Primary FDC D1-D4",
      selectionRevision: 1
    },
    display: {
      displayMode: options.displayMode ?? (unit === "%" ? "delta_percent" : "absolute_pf"),
      measurementDomain: "auto",
      showCellText: true,
      pauseDisplay: false,
      freezeColor: false,
      unitMode: "auto",
      circuitOffsetPf: 33,
      trendLatestN: 600,
      colorRange: { min: null, max: null, frozen: false, quantity }
    },
    baseline: {},
    commands: {},
    logs: { revision: 0, totalRecords: 0, overwrites: 0, rows: [] },
    discovery: { bleState: "idle", bleResults: [], wifiState: "idle", wifiResults: [] },
    diagnostics: {}
  };
}

function numberMatrix(value: number): number[][] {
  return Array.from({ length: 8 }, () => Array.from({ length: 8 }, () => value));
}

function booleanMatrix(value: boolean): boolean[][] {
  return Array.from({ length: 8 }, () => Array.from({ length: 8 }, () => value));
}

function nullableNumberMatrix(): (number | null)[][] {
  return Array.from({ length: 8 }, () => Array.from({ length: 8 }, () => null));
}

function nullableStringMatrix(): (string | null)[][] {
  return Array.from({ length: 8 }, () => Array.from({ length: 8 }, () => null));
}
