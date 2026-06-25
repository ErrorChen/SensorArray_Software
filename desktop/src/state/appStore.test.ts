import { describe, expect, it } from "vitest";

import { cellLabel, selectionTitle } from "./appStore";

describe("appStore helpers", () => {
  it("generates a defensive selection title", () => {
    expect(selectionTitle({ rowLabel: "S5", fdcGroup: "secondary", detectorStart: 5, detectorEnd: 8 })).toBe(
      "S5 · Secondary FDC · D5-D8"
    );
  });

  it("labels 8x8 cells", () => {
    expect(cellLabel(0, 0)).toBe("S1D1");
    expect(cellLabel(7, 7)).toBe("S8D8");
  });
});

