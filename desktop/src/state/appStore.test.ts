import { describe, expect, it } from "vitest";

import type { BackendSnapshotPayload } from "../api/types";
import { cellLabel, measurementMatrixIsCurrent, selectionTitle, snapshotForDisplay } from "./appStore";
import { isCommandSendDisabled, updateCommandHistory } from "./commandPanel";
import { resolveColourRange, type HeatmapDatum } from "./heatmap";
import { clampSplitRatio } from "./layout";
import { parseKeyValueText, parseLogStatusRows } from "./logStatus";
import { isBleScanDisabled } from "./transportUi";
import { createBackendSnapshot } from "../testUtils/snapshot";

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

  it("keeps the last coherent matrix when an old-generation frame follows MAPP", () => {
    const current = createBackendSnapshot({ mode: "VOLT" });
    current.measurement.generation = 7;
    current.matrix.generation = 7;
    current.matrix.displayValues[0][0] = 0.25;

    const stale = createBackendSnapshot({ mode: "VOLT" });
    stale.measurement.generation = 7;
    stale.matrix.generation = 6;
    stale.matrix.displayValues[0][0] = 99;

    expect(measurementMatrixIsCurrent(stale, current)).toBe(false);
    expect(snapshotForDisplay(stale, current).matrix.displayValues[0][0]).toBe(0.25);
  });

  it("uses the MAPP sequence boundary for CAP instead of comparing ROWS generation", () => {
    const current = createBackendSnapshot({ mode: "CAP" });
    current.measurement.generation = 21;
    current.measurement.frameSeq = 100;
    current.frame.seq = 101;
    current.matrix.generation = 2;

    const next = createBackendSnapshot({ mode: "CAP" });
    next.measurement.generation = 21;
    next.measurement.frameSeq = 100;
    next.frame.seq = 102;
    next.matrix.generation = 3;
    expect(measurementMatrixIsCurrent(next, current)).toBe(true);

    next.frame.seq = 99;
    expect(measurementMatrixIsCurrent(next, current)).toBe(false);
  });

  it("uses homogeneous ROWMODES authority when global appliedMode is still different", () => {
    const current = createBackendSnapshot({ mode: "CAP" });
    const next = createBackendSnapshot({ mode: "VOLT" });
    next.measurement.appliedMode = "CAP";
    next.measurement.generation = 3;
    next.measurement.rowProfile = {
      appliedModes: Array.from({ length: 8 }, () => "VOLT"),
      pendingModes: null,
      transitionState: "applied",
      requestId: 81,
      generation: 12,
      frameSeq: 200,
      error: ""
    };
    next.frame.rowModes = Array.from({ length: 8 }, () => "VOLT");
    next.frame.profileGeneration = 12;
    next.frame.profileRequestId = 81;
    next.frame.seq = 201;
    next.matrix.generation = 12;

    expect(measurementMatrixIsCurrent(next, current)).toBe(true);
    next.frame.profileRequestId = 82;
    expect(measurementMatrixIsCurrent(next, current)).toBe(false);
  });

  it("does not treat a homogeneous active prefix as a homogeneous saved profile", () => {
    const next = createBackendSnapshot({ mode: "CAP" });
    const profile = ["CAP", "CAP", "CAP", "CAP", "RES", "VOLT", "VOLT", "RES"] as const;
    next.frame.rows = 4;
    next.measurement.appliedMode = "VOLT";
    next.measurement.rowProfile = {
      appliedModes: [...profile],
      pendingModes: null,
      transitionState: "applied",
      requestId: 91,
      generation: 13,
      frameSeq: 300,
      error: ""
    };
    next.frame.rowModes = [...profile];
    next.matrix.modeByRow = [...profile];
    next.frame.profileGeneration = 13;
    next.frame.profileRequestId = 91;
    next.frame.seq = 301;

    expect(measurementMatrixIsCurrent(next, null)).toBe(false);

    next.frame.layout = "MIXED";
    expect(measurementMatrixIsCurrent(next, null)).toBe(true);
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
    expect(resolveColourRange(data, snapshot("%"))).toEqual([-0.525, 0.525]);
  });

  it("does not override a frozen backend colour range", () => {
    const frozen = snapshot("pF");
    frozen.display.colorRange = { min: 1, max: 2, frozen: true };
    expect(resolveColourRange([[0, 0, 10, "S1D1", true]], frozen)).toEqual([1, 2]);
  });

  it("does not reuse a legacy single range when inactive saved rows make the profile mixed", () => {
    const legacy = snapshot("pF");
    legacy.frame.rows = 1;
    legacy.frame.rowModes = ["CAP", "RES", "CAP", "CAP", "CAP", "CAP", "CAP", "CAP"];
    legacy.matrix.modeByRow = [...legacy.frame.rowModes];
    legacy.display.colourRanges = undefined;
    legacy.display.colorRange = { min: 1, max: 2, frozen: true };
    expect(resolveColourRange([[0, 0, 10, "S1D1", true]], legacy)).toEqual([0, 10.5]);
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

describe("log status parser", () => {
  it("parses command and BLE status rows", () => {
    const items = parseLogStatusRows([
      logRow("CMD_TX", "CMD_TX,mode=ble,bytes=8,ending=lf", "info"),
      logRow("BL50", "BL50,conn=1,sub=1,mtu=247,phy=1/1,mq=12,xx=9", "info"),
      logRow("BLE_FRAG50", "BLE_FRAG50,reassembled=10,duplicate=0,missing=1,timeout=0,crc=0,length=0", "info")
    ]);
    expect(items.some((item) => item.title === "Command sent" && item.severity === "ok")).toBe(true);
    const bluetooth = items.find((item) => item.title === "Bluetooth 50-frame summary");
    expect(bluetooth?.details["Connection state (conn)"]).toBe(1);
    expect(bluetooth?.details["Negotiated MTU bytes (mtu)"]).toBe(247);
    expect(bluetooth?.details["Unknown firmware field (xx)"]).toBeUndefined();
    expect(bluetooth?.details["Legacy/unknown field (xx)"]).toBe(9);
    expect(items.some((item) => item.title === "BLE fragment statistics" && item.severity === "warn")).toBe(true);
  });

  it("keeps unknown key value logs readable", () => {
    expect(parseKeyValueText("UNKNOWN,foo=1,bar=true,baz=text")).toEqual({ foo: 1, bar: true, baz: "text" });
    const [item] = parseLogStatusRows([logRow("UNKNOWN", "UNKNOWN,foo=1", "info")]);
    expect(item.category).toBe("Other");
    expect(item.title).toBe("Unknown firmware log (UNKNOWN)");
    expect(item.details["Unknown firmware field (foo)"]).toBe(1);
  });

  it("marks parser errors as error severity", () => {
    const [item] = parseLogStatusRows([logRow("PARSER", "crc: invalid", "error")]);
    expect(item.category).toBe("Parser");
    expect(item.severity).toBe("error");
  });

  it("does not invent an ADS1262 identity when firmware reports unknown", () => {
    const [item] = parseLogStatusRows([logRow("ADS", "ADS,chip=unknown,valid=0", "info")]);
    expect(item.title).toBe("ADS identity unconfirmed");
    expect(item.severity).toBe("warn");
  });

  it("understands measurement and ADS diagnostic transaction logs", () => {
    const items = parseLogStatusRows([
      logRow("MACK", "MACK,id=42,old=CAP,new=VOLT,state=accepted", "info"),
      logRow("MAPP", "MAPP,id=42,gen=7,old=CAP,new=VOLT,seq=9,state=applied", "info"),
      logRow("ADSCHKSTAT", "ADSCHKSTAT,id=5,state=completed,samples=20,fresh=20,restore=ok", "info")
    ]);
    expect(items.some((item) => item.title === "Measurement mode accepted")).toBe(true);
    expect(items.some((item) => item.title === "Measurement mode applied")).toBe(true);
    expect(items.some((item) => item.title === "ADS diagnostic status" && item.severity === "ok")).toBe(true);
  });

  it("distinguishes rail RAPP from rows RAPP and surfaces mode failures", () => {
    const items = parseLogStatusRows([
      logRow("RAPP", "RAPP,id=51,avdd=3391000,avss=-2500000,source=external,state=applied", "info"),
      logRow("MERR", "MERR,id=42,new=VOLT,state=SAFE,err=0x103", "error"),
      logRow("BAPP", "BAPP,id=8,cmd=BATNOW,seq=10,status=complete", "info")
    ]);
    expect(items.some((item) => item.title === "Voltage rail configuration applied" && item.category === "Measurement")).toBe(true);
    expect(items.some((item) => item.title === "Measurement mode failed" && item.severity === "error")).toBe(true);
    expect(items.some((item) => item.title === "Battery command completed")).toBe(true);
  });
});

function snapshot(unit: "pF" | "%"): BackendSnapshotPayload {
  return createBackendSnapshot({ unit, displayMode: unit === "%" ? "delta_percent" : "absolute_pf" });
}

function logRow(tag: string, rawText: string, severity: string) {
  return {
    timestamp: 1,
    monotonicTime: 1,
    source: "host",
    channel: "host",
    tag,
    severity,
    rawText,
    parsedFields: {},
    recognised: true,
    sessionGeneration: 1
  };
}
