import { describe, expect, it } from "vitest";

import { createBackendSnapshot } from "../../testUtils/snapshot";
import { cellValues, formatOffsetInput, formatPf, parseOffsetInput } from "./OffsetPanel";

describe("OffsetPanel helpers", () => {
  it("reads 1-based selected cell values from the 8x8 matrix", () => {
    const snapshot = createBackendSnapshot();
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
