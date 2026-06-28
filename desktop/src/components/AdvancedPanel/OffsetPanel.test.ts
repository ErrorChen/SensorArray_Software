import { describe, expect, it } from "vitest";

import type { BackendSnapshotPayload } from "../../api/types";
import { cellValues, formatOffsetInput, formatPf, parseOffsetInput } from "./OffsetPanel";

describe("OffsetPanel helpers", () => {
  it("reads 1-based selected cell values from the 8x8 matrix", () => {
    const snapshot = makeSnapshot();
    snapshot.matrix.rawPf[1][4] = 10.125;
    snapshot.matrix.correctedPf[1][4] = 9.5;
    snapshot.matrix.userOffsetPf[1][4] = 0.625;
    snapshot.matrix.displayValues[1][4] = 8.875;

    expect(cellValues(snapshot, 2, 5)).toEqual({
      raw: 10.125,
      corrected: 9.5,
      offset: 0.625,
      displayed: 8.875
    });
  });

  it("formats and parses offset values without accepting non-finite input", () => {
    expect(formatPf(1.23456)).toBe("1.235");
    expect(formatPf(null)).toBe("NA");
    expect(formatOffsetInput(0.25)).toBe("0.25");
    expect(formatOffsetInput(Number.NaN)).toBe("0");
    expect(parseOffsetInput(" -1.5 ")).toBe(-1.5);
    expect(parseOffsetInput("Infinity")).toBeNull();
    expect(parseOffsetInput("not a number")).toBeNull();
  });
});

function makeSnapshot(): BackendSnapshotPayload {
  const numbers = Array.from({ length: 8 }, () => Array.from({ length: 8 }, () => 0));
  return {
    connection: { mode: "serial", state: "connected", deviceLabel: "COM12", generation: 1 },
    frame: { seq: 1, fps: 20, rows: 8, valid: true, timestampUs: 1000, revision: 1 },
    matrix: {
      rows: Array.from({ length: 8 }, (_, index) => `S${index + 1}`),
      cols: Array.from({ length: 8 }, (_, index) => `D${index + 1}`),
      correctedPf: numbers.map((row) => [...row]),
      rawPf: numbers.map((row) => [...row]),
      rawFixed: numbers.map((row) => [...row]),
      userOffsetPf: numbers.map((row) => [...row]),
      displayValues: numbers.map((row) => [...row]),
      validMask: Array.from({ length: 8 }, () => Array.from({ length: 8 }, () => true)),
      unit: "pF",
      domain: "capacitance"
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
      displayMode: "absolute_pf",
      measurementDomain: "auto",
      showCellText: true,
      pauseDisplay: false,
      freezeColor: false,
      unitMode: "auto",
      circuitOffsetPf: 33,
      trendLatestN: 600,
      colorRange: { min: null, max: null, frozen: false }
    },
    baseline: {},
    commands: {},
    logs: { revision: 0, totalRecords: 0, overwrites: 0, rows: [] },
    discovery: { bleState: "idle", bleResults: [], wifiState: "idle", wifiResults: [] },
    diagnostics: {}
  };
}
