import { describe, expect, it } from "vitest";

import { createBackendSnapshot } from "../testUtils/snapshot";
import {
  appliedMeasurementMode,
  cellMeasurementState,
  errorReason,
  formatErrorCode,
  formatMeasurementValue,
  isCellDisplayable,
  pgaLabel,
  transitionDescription
} from "./measurement";

describe("measurement presentation", () => {
  it("formats signed voltage and resistance with engineering units without changing quantity", () => {
    expect(formatMeasurementValue(-0.00000125, "voltage")).toBe("-1.250 µV");
    expect(formatMeasurementValue(-0.00125, "voltage")).toBe("-1.250 mV");
    expect(formatMeasurementValue(2.5, "voltage")).toBe("2.500 V");
    expect(formatMeasurementValue(0.125, "resistance")).toBe("125.000 mΩ");
    expect(formatMeasurementValue(12_500, "resistance")).toBe("12.500 kΩ");
    expect(formatMeasurementValue(2_000_000, "resistance")).toBe("2.000 MΩ");
  });

  it("treats PGA zero as verified bypass and preserves unknown error codes", () => {
    expect(pgaLabel(0, false)).toBe("PGA bypass");
    expect(pgaLabel(32, false)).toBe("PGA ×32");
    expect(pgaLabel(-1, false)).toBe("PGA unavailable");
    expect(formatErrorCode(3)).toBe("0x03");
    expect(errorReason(0xab, null)).toBe("Unknown firmware cell error 0xAB");
  });

  it("requires both validity and freshness before a cell is displayable", () => {
    const snapshot = createBackendSnapshot({ mode: "RES" });
    snapshot.matrix.displayValues[0][0] = 10;
    let cell = cellMeasurementState(snapshot.matrix, 0, 0);
    expect(isCellDisplayable(cell)).toBe(true);

    snapshot.matrix.fresh[0][0] = false;
    cell = cellMeasurementState(snapshot.matrix, 0, 0);
    expect(isCellDisplayable(cell)).toBe(false);
  });

  it("keeps the applied mode unchanged while waiting for MAPP", () => {
    const snapshot = createBackendSnapshot();
    snapshot.measurement.pendingMode = "VOLT";
    snapshot.measurement.transitionState = "accepted";
    snapshot.measurement.requestId = 42;

    expect(appliedMeasurementMode(snapshot)).toBe("CAP");
    expect(transitionDescription(snapshot)).toBe("Waiting for firmware apply (MAPP #42)");
  });
});
