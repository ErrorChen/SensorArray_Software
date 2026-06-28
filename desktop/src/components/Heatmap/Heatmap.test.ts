import { describe, expect, it, vi } from "vitest";

import type { BackendSnapshotPayload } from "../../api/types";
import { buildDynamicHeatmapOption, buildHeatmapData, formatHeatmapTooltip, pointToCell } from "./Heatmap";

describe("Heatmap data helpers", () => {
  it("keeps invalid cells addressable while fixed series ids preserve chart instance state", () => {
    const snapshot = makeSnapshot();
    snapshot.matrix.displayValues[0][0] = 10;
    snapshot.matrix.displayValues[0][1] = 20;
    snapshot.matrix.validMask[0][1] = false;

    const data = buildHeatmapData(snapshot);
    expect(data).toHaveLength(64);
    expect(data.find((item) => item[3] === "S1D2")).toEqual([1, 0, null, "S1D2", false]);

    const selected = data[Math.floor(data.length / 8)];
    const option = buildDynamicHeatmapOption(snapshot, new Set([String(selected[3])])) as {
      series: Array<{ id: string; type: string; data: unknown[]; silent?: boolean }>;
    };

    expect(option.series[0].id).toBe("heatmap-values");
    expect(option.series[1].id).toBe("selected-cells");
    expect(option.series[1].silent).toBe(true);
    expect(option.series[1].data).toEqual([[selected[0], selected[1]]]);
  });

  it("maps zrender pointer coordinates to SxDy cells through the chart grid", () => {
    const chart = {
      containPixel: vi.fn(() => true),
      convertFromPixel: vi.fn(() => [4.2, 0.1])
    } as unknown as Parameters<typeof pointToCell>[0];

    expect(pointToCell(chart, 12, 34)).toEqual({ row: 0, col: 4 });
    expect(chart.containPixel).toHaveBeenCalledWith({ gridIndex: 0 }, [12, 34]);
    expect(chart.convertFromPixel).toHaveBeenCalledWith({ gridIndex: 0 }, [12, 34]);
  });

  it("rejects pointer coordinates outside the heatmap grid or 8x8 bounds", () => {
    const outsideGrid = {
      containPixel: vi.fn(() => false),
      convertFromPixel: vi.fn()
    } as unknown as Parameters<typeof pointToCell>[0];
    expect(pointToCell(outsideGrid, 1, 2)).toBeNull();

    const outsideCell = {
      containPixel: vi.fn(() => true),
      convertFromPixel: vi.fn(() => [8, 0])
    } as unknown as Parameters<typeof pointToCell>[0];
    expect(pointToCell(outsideCell, 1, 2)).toBeNull();
  });

  it("formats tooltip content from the latest snapshot values", () => {
    const snapshot = makeSnapshot();
    const rowIndex = 0;
    const colIndex = Math.floor(snapshot.matrix.cols.length / 2);
    const label = `${snapshot.matrix.rows[rowIndex]}${snapshot.matrix.cols[colIndex]}`;
    snapshot.matrix.rawPf[rowIndex][colIndex] = 101.2345;
    snapshot.matrix.correctedPf[rowIndex][colIndex] = 99.5;
    snapshot.matrix.userOffsetPf[rowIndex][colIndex] = 1.25;
    snapshot.matrix.displayValues[rowIndex][colIndex] = 99.5;
    snapshot.frame.seq = 123;

    const tooltip = formatHeatmapTooltip({ value: [colIndex, rowIndex, 99.5, label, 1] } as never, snapshot);

    expect(tooltip).toContain(`<strong>${label}</strong>`);
    expect(tooltip).toContain("raw pF: 101.234");
    expect(tooltip).toContain("corrected pF: 99.500");
    expect(tooltip).toContain("seq: 123");
  });
});

function makeSnapshot(): BackendSnapshotPayload {
  const numbers = Array.from({ length: 8 }, () => Array.from({ length: 8 }, () => 0));
  return {
    connection: { mode: "serial", state: "connected", deviceLabel: "SERIAL_TEST_PORT", generation: 1 },
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
