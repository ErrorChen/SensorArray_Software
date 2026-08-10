import { describe, expect, it } from "vitest";

import type { HistoryPayload, HistorySeries } from "../../api/types";
import { buildTrendPoints, historyForMode } from "./TrendGrid";

describe("TrendGrid measurement isolation", () => {
  it("does not expose history from a different applied measurement mode", () => {
    const history = makeHistory("CAP", "capacitance", "pF");
    expect(historyForMode(history, "VOLT")).toBeNull();
    expect(historyForMode(history, "CAP")).toBe(history);
  });

  it("drops invalid and stale history points instead of plotting them as values", () => {
    const series: HistorySeries = {
      cell: "S1D1",
      points: [
        { seq: 1, timeSeconds: 1, value: 1, valid: true, fresh: true },
        { seq: 2, timeSeconds: 2, value: 2, valid: false, fresh: true },
        { seq: 3, timeSeconds: 3, value: 3, valid: true, fresh: false },
        { seq: 4, timeSeconds: 4, value: null, valid: true, fresh: true }
      ]
    };
    expect(buildTrendPoints(series).map((point) => point.seq)).toEqual([1]);
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
