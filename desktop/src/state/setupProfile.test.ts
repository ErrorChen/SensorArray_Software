import { describe, expect, it } from "vitest";

import { defaultSetupProfile, normaliseSetupProfile, setupProfileFromSnapshot } from "./setupProfile";
import type { BackendSnapshotPayload } from "../api/types";

describe("setup profile", () => {
  it("uses the runtime directory as the default save directory", () => {
    const profile = defaultSetupProfile("C:/SensorArray");

    expect(profile.schemaVersion).toBe(1);
    expect(profile.appVersion).toBe("1.0.0");
    expect(profile.paths.defaultSaveDirectory).toBe("C:/SensorArray");
    expect(profile.offsetsPf).toHaveLength(8);
    expect(profile.offsetsPf[0]).toHaveLength(8);
  });

  it("fills missing optional fields while preserving imported preferences", () => {
    const profile = normaliseSetupProfile(
      {
        transport: {
          mode: "wifi",
          serial: { baud: 230400 },
          wifi: { host: "10.0.0.2" }
        },
        acquisition: { rows: 4 },
        command: { lineEnding: "crlf" },
        paths: { defaultSaveDirectory: "D:/captures" }
      },
      "C:/runtime"
    );

    expect(profile.transport.mode).toBe("wifi");
    expect(profile.transport.serial.baud).toBe(230400);
    expect(profile.transport.wifi.host).toBe("10.0.0.2");
    expect(profile.transport.wifi.fallbackHost).toBe("192.168.4.1");
    expect(profile.transport.replay.speed).toBe(1);
    expect(profile.acquisition.rows).toBe(4);
    expect(profile.command.lineEnding).toBe("crlf");
    expect(profile.paths.defaultSaveDirectory).toBe("D:/captures");
  });

  it("rejects invalid rows, baud, schema version, default save directory, and offset shape", () => {
    expect(() => normaliseSetupProfile({ schemaVersion: 2 }, "C:/runtime")).toThrow("schemaVersion");
    expect(() => normaliseSetupProfile({ transport: { serial: { baud: 0 } } }, "C:/runtime")).toThrow("baud");
    expect(() => normaliseSetupProfile({ acquisition: { rows: 9 } }, "C:/runtime")).toThrow("rows");
    expect(() => normaliseSetupProfile({ paths: { defaultSaveDirectory: " " } }, "C:/runtime")).toThrow("defaultSaveDirectory");
    expect(() => normaliseSetupProfile({ offsetsPf: [[1, 2]] }, "C:/runtime")).toThrow("offsetsPf");
  });

  it("exports display and offsets from the current snapshot without losing transport preferences", () => {
    const current = defaultSetupProfile("C:/runtime");
    current.transport.serial = { port: "COM12", baud: 921600 };
    const snapshot = makeSnapshot();
    snapshot.frame.rows = 2;
    snapshot.display.displayMode = "delta_percent";
    snapshot.matrix.userOffsetPf[0][0] = 7.5;

    const profile = setupProfileFromSnapshot(snapshot, current);

    expect(profile.transport.serial).toEqual({ port: "COM12", baud: 921600 });
    expect(profile.acquisition.rows).toBe(2);
    expect(profile.display.displayMode).toBe("delta_percent");
    expect(profile.offsetsPf[0][0]).toBe(7.5);
  });
});

function makeSnapshot(): BackendSnapshotPayload {
  const matrix = Array.from({ length: 8 }, () => Array.from({ length: 8 }, () => 0));
  return {
    connection: { mode: "serial", state: "disconnected", deviceLabel: "", generation: 0 },
    frame: { seq: null, fps: 0, rows: 8, valid: false, timestampUs: null, revision: 0 },
    matrix: {
      rows: Array.from({ length: 8 }, (_, index) => `S${index + 1}`),
      cols: Array.from({ length: 8 }, (_, index) => `D${index + 1}`),
      correctedPf: matrix,
      rawPf: matrix,
      rawFixed: matrix,
      userOffsetPf: matrix.map((row) => [...row]),
      displayValues: matrix,
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
      selectionRevision: 0
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
