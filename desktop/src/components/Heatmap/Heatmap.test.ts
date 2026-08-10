import { describe, expect, it, vi } from "vitest";

import { createBackendSnapshot } from "../../testUtils/snapshot";
import {
  buildDynamicHeatmapOption,
  buildHeatmapData,
  formatHeatmapCellLabel,
  formatHeatmapTooltip,
  invalidCellTargets,
  pointToCell,
  showHeatmapTooltipForCell
} from "./Heatmap";

describe("Heatmap data helpers", () => {
  it("keeps invalid cells addressable while fixed series ids preserve chart instance state", () => {
    const snapshot = createBackendSnapshot();
    snapshot.matrix.displayValues[0][0] = 10;
    snapshot.matrix.displayValues[0][1] = 20;
    snapshot.matrix.valid[0][1] = false;

    const data = buildHeatmapData(snapshot);
    expect(data).toHaveLength(64);
    expect(data.find((item) => item[3] === "S1D2")).toEqual([1, 0, null, "S1D2", false]);

    const selected = data[Math.floor(data.length / 8)];
    const option = buildDynamicHeatmapOption(snapshot, new Set([String(selected[3])])) as {
      series: Array<{ id: string; type: string; data: unknown[]; silent?: boolean }>;
    };

    expect(option.series[0].id).toBe("heatmap-values");
    expect(option.series[1].id).toBe("invalid-cells");
    expect(option.series[1].data).toHaveLength(1);
    expect(option.series[2].id).toBe("selected-cells");
    expect(option.series[2].silent).toBe(true);
    expect(option.series[2].data).toEqual([[selected[0], selected[1]]]);
  });

  it("replaces the colour-scale unit when the measurement quantity changes", () => {
    const scaleUnit = (mode: "CAP" | "VOLT" | "RES"): string => {
      const option = buildDynamicHeatmapOption(createBackendSnapshot({ mode }), new Set()) as {
        visualMap: Array<{ id: string; text?: string[] }>;
      };
      expect(option.visualMap[0].id).toBe("measurement-scale");
      return option.visualMap[0].text?.[0] ?? "";
    };

    expect(scaleUnit("CAP")).toBe("pF");
    expect(scaleUnit("VOLT")).toBe("V");
    expect(scaleUnit("RES")).toBe("Ω");
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

  it("explicitly targets invalid-series tooltips beneath the silent selection overlay", () => {
    const dispatchAction = vi.fn();
    const chart = {
      getOption: vi.fn(() => ({
        series: [
          { data: [{ value: [0, 0, 1, "S1D1", 1] }] },
          { data: [{ value: [4, 0, 0, "S1D5", 0] }] },
          { data: [[0, 0], [1, 0], [2, 0], [3, 0]] }
        ]
      })),
      dispatchAction
    } as unknown as Parameters<typeof showHeatmapTooltipForCell>[0];

    expect(showHeatmapTooltipForCell(chart, { row: 0, col: 4 })).toBe(true);
    expect(dispatchAction).toHaveBeenCalledWith({ type: "showTip", seriesIndex: 1, dataIndex: 0 });
  });

  it("formats tooltip content from the latest snapshot values", () => {
    const snapshot = createBackendSnapshot();
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
    expect(tooltip).toContain("Raw pF: 101.234");
    expect(tooltip).toContain("Corrected pF: 99.500");
    expect(tooltip).toContain("Seq: 123");
  });

  it("excludes stale voltage cells and reports negative voltage, PGA bypass, and firmware errors", () => {
    const snapshot = createBackendSnapshot({ mode: "VOLT" });
    snapshot.matrix.values[0][0] = -0.00125;
    snapshot.matrix.displayValues[0][0] = -0.00125;
    snapshot.matrix.rawFixed[0][0] = -1250;
    snapshot.matrix.pga[0][0] = 0;
    snapshot.matrix.pgaBypass[0][0] = true;
    snapshot.matrix.valid[0][1] = false;
    snapshot.matrix.errorCodes[0][1] = 3;
    snapshot.matrix.errorReasons[0][1] = "ADC timeout";
    snapshot.matrix.fresh[0][2] = false;

    const data = buildHeatmapData(snapshot);
    expect(data.find((item) => item[3] === "S1D2")?.[2]).toBeNull();
    expect(data.find((item) => item[3] === "S1D3")?.[2]).toBeNull();

    const voltageTooltip = formatHeatmapTooltip({ value: [0, 0, -0.00125, "S1D1", 1] } as never, snapshot);
    expect(voltageTooltip).toContain("Value: -1.250 mV");
    expect(voltageTooltip).toContain("Raw integer µV: -1250");
    expect(voltageTooltip).toContain("PGA bypass");

    const errorTooltip = formatHeatmapTooltip({ value: [1, 0, null, "S1D2", 0] } as never, snapshot);
    expect(errorTooltip).toContain("0x03");
    expect(errorTooltip).toContain("ADC timeout");
  });

  it("leaves inactive cells blank and gives Xhh precedence over stale", () => {
    const snapshot = createBackendSnapshot({ mode: "RES" });
    snapshot.frame.rows = 1;
    snapshot.matrix.valid[0][1] = false;
    snapshot.matrix.fresh[0][1] = false;
    snapshot.matrix.errorCodes[0][1] = 3;
    snapshot.matrix.valid[0][2] = true;
    snapshot.matrix.fresh[0][2] = false;

    expect(formatHeatmapCellLabel(snapshot, 0, 1, null)).toBe("X03");
    expect(formatHeatmapCellLabel(snapshot, 0, 2, null)).toBe("stale");
    expect(formatHeatmapCellLabel(snapshot, 1, 0, null)).toBe("");

    const option = buildDynamicHeatmapOption(snapshot, new Set()) as { series: Array<{ data: unknown[] }> };
    expect(option.series[1].data).toHaveLength(2);
    expect(invalidCellTargets(snapshot)).toEqual([
      { row: 0, col: 1, label: "S1D2" },
      { row: 0, col: 2, label: "S1D3" }
    ]);
  });

  it("does not paint placeholder cells before the first valid frame", () => {
    const snapshot = createBackendSnapshot();
    snapshot.frame.valid = false;
    snapshot.frame.rows = 8;
    snapshot.matrix.valid = snapshot.matrix.valid.map((row) => row.map(() => false));
    snapshot.matrix.fresh = snapshot.matrix.fresh.map((row) => row.map(() => false));

    const option = buildDynamicHeatmapOption(snapshot, new Set()) as { series: Array<{ data: unknown[] }> };
    expect(option.series[0].data).toHaveLength(0);
    expect(option.series[1].data).toHaveLength(0);
    expect(formatHeatmapCellLabel(snapshot, 0, 0, null)).toBe("");
  });
});
