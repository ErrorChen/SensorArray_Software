import { describe, expect, it } from "vitest";

import type { HistoryPayload, HistorySeries } from "../../api/types";
import { buildTrendPoints, historyForMode } from "./TrendGrid";

describe("TrendGrid measurement isolation", () => {
  it("does not expose history from a different applied measurement mode", () => {
    const history = makeHistory("CAP", "capacitance", "pF");
    expect(historyForMode(history, "VOLT")).toBeNull();
    expect(historyForMode(history, "CAP")).toBe(history);
  });

  it("retains invalid, stale, and mode-mismatch points as null discontinuities", () => {
    const series: HistorySeries = {
      cell: "S1D1",
      points: [
        { seq: 1, timeSeconds: 1, value: 1, valid: true, fresh: true },
        { seq: 2, timeSeconds: 2, value: 2, valid: false, fresh: true },
        { seq: 3, timeSeconds: 3, value: 3, valid: true, fresh: false },
        { seq: 4, timeSeconds: 4, value: null, valid: true, fresh: true }
      ]
    };
    expect(buildTrendPoints(series).map((point) => point.seq)).toEqual([1, 2, 3, 4]);
    expect(buildTrendPoints(series).map((point) => point.value[1])).toEqual([1, null, null, null]);
  });

  it("does not reconnect values across a RES to CAP history gap", () => {
    const series: HistorySeries = {
      cell: "S1D1",
      points: [
        { seq: 10, timeSeconds: 10, value: 6.2, valid: true, fresh: true },
        // Backend emits null while this cell belongs to another row mode.
        { seq: 11, timeSeconds: 11, value: null, valid: false, fresh: false },
        { seq: 12, timeSeconds: 12, value: 6.4, valid: true, fresh: true }
      ]
    };
    expect(buildTrendPoints(series).map((point) => point.value)).toEqual([
      [0, 6.2],
      [1, null],
      [2, 6.4]
    ]);
  });
});

function makeHistory(mode: HistoryPayload["mode"], quantity: HistoryPayload["quantity"], unit: string): HistoryPayload {
  return {
    mode,
    quantity,
    selectionRevision: 1,
    title: "S1 Primary D1-D4",
    unit,
    revision: 1,
    series: []
  };
}
