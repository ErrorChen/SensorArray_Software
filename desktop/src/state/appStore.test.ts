import { describe, expect, it } from "vitest";

import type { BackendSnapshotPayload } from "../api/types";
import { cellLabel, selectionTitle } from "./appStore";
import { isCommandSendDisabled, updateCommandHistory } from "./commandPanel";
import { resolveColourRange, type HeatmapDatum } from "./heatmap";
import { clampSplitRatio } from "./layout";
import { isBleScanDisabled } from "./transportUi";

describe("appStore helpers", () => {
  it("generates a defensive selection title", () => {
    expect(selectionTitle({ rowLabel: "S5", fdcGroup: "secondary", detectorStart: 5, detectorEnd: 8 })).toBe(
      "S5 Secondary FDC D5-D8"
    );
  });

  it("labels 8x8 cells", () => {
    expect(cellLabel(0, 0)).toBe("S1D1");
    expect(cellLabel(7, 7)).toBe("S8D8");
  });
});

describe("heatmap colour range", () => {
  it("uses only valid finite values for absolute range", () => {
    const data: HeatmapDatum[] = [
      [0, 0, 10, "S1D1", true],
      [1, 0, null, "S1D2", false],
      [2, 0, 20, "S1D3", true]
    ];
    expect(resolveColourRange(data, snapshot("pF"))).toEqual([9.8, 20.2]);
  });

  it("keeps delta percent range symmetric around zero", () => {
    const data: HeatmapDatum[] = [
      [0, 0, -0.1, "S1D1", true],
      [1, 0, 0.2, "S1D2", true]
    ];
    expect(resolveColourRange(data, snapshot("%"))).toEqual([-0.5, 0.5]);
  });

  it("does not override a frozen backend colour range", () => {
    const frozen = snapshot("pF");
    frozen.display.colorRange = { min: 1, max: 2, frozen: true };
    expect(resolveColourRange([[0, 0, 10, "S1D1", true]], frozen)).toEqual([1, 2]);
  });
});

describe("layout and command UI rules", () => {
  it("clamps the main splitter to left and right constraints", () => {
    expect(clampSplitRatio(0.2, 1000, { minLeftRatio: 0.45, minRightPx: 300 })).toBe(0.45);
    expect(clampSplitRatio(0.9, 1000, { minLeftRatio: 0.45, minRightPx: 300 })).toBe(0.7);
  });

  it("disables command send unless connected with non-empty input", () => {
    expect(isCommandSendDisabled({ hasClient: true, pending: false, busy: false, connected: false, commandText: "PING" })).toBe(true);
    expect(isCommandSendDisabled({ hasClient: true, pending: false, busy: false, connected: true, commandText: "PING" })).toBe(false);
    expect(isCommandSendDisabled({ hasClient: true, pending: false, busy: false, connected: true, commandText: "   " })).toBe(true);
  });

  it("deduplicates and limits command history", () => {
    expect(updateCommandHistory(["OLD", "PING"], "PING", 3)).toEqual(["PING", "OLD"]);
    expect(updateCommandHistory(["A", "B", "C"], "D", 3)).toEqual(["D", "A", "B"]);
  });

  it("disables BLE scan while BLE is active", () => {
    expect(isBleScanDisabled("ble", "streaming")).toBe(true);
    expect(isBleScanDisabled("ble", "connected")).toBe(true);
    expect(isBleScanDisabled("serial", "streaming")).toBe(false);
  });
});

function snapshot(unit: "pF" | "%"): BackendSnapshotPayload {
  return {
    connection: { mode: "serial", state: "disconnected", deviceLabel: "", generation: 0 },
    frame: { seq: null, fps: 0, rows: 8, valid: false, timestampUs: null, revision: 0 },
    matrix: {
      rows: [],
      cols: [],
      correctedPf: [],
      rawPf: [],
      rawFixed: [],
      displayValues: [],
      validMask: [],
      unit,
      domain: "capacitance"
    },
    selection: {
      rowIndex: 0,
      rowLabel: "S1",
      fdcGroup: "primary",
      detectorStart: 1,
      detectorEnd: 4,
      cells: [],
      title: "S1 Primary FDC D1-D4",
      selectionRevision: 0
    },
    display: {
      displayMode: unit === "%" ? "delta_percent" : "absolute_pf",
      measurementDomain: "auto",
      showCellText: true,
      pauseDisplay: false,
      freezeColor: false,
      unitMode: "raw",
      circuitOffsetPf: 33,
      colorRange: { min: null, max: null, frozen: false }
    },
    baseline: {},
    commands: {},
    logs: { revision: 0, totalRecords: 0, overwrites: 0, rows: [] },
    discovery: { bleState: "idle", bleResults: [], wifiState: "idle", wifiResults: [] },
    diagnostics: {}
  };
}
