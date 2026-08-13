import { describe, expect, it } from "vitest";

import { defaultSetupProfile, normaliseSetupProfile, setupProfileFromSnapshot } from "./setupProfile";
import { createBackendSnapshot } from "../testUtils/snapshot";

describe("setup profile", () => {
  it("uses the runtime directory as the default save directory", () => {
    const profile = defaultSetupProfile("C:/SensorArray");

    expect(profile.schemaVersion).toBe(3);
    expect(profile.appVersion).toBe("1.0.0");
    expect(profile.paths.defaultSaveDirectory).toBe("C:/SensorArray");
    expect(profile.offsetsPf).toHaveLength(8);
    expect(profile.offsetsPf[0]).toHaveLength(8);
    expect(profile.acquisition.measurementMode).toBe("CAP");
    expect(profile.acquisition.rowModes).toEqual(Array.from({ length: 8 }, () => "CAP"));
    expect(profile.voltageRail).toEqual({ measuredAvddV: null, measuredAvssV: null });
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

  it("round-trips every supported row count from 1 through 8", () => {
    for (let rows = 1; rows <= 8; rows += 1) {
      expect(normaliseSetupProfile({ acquisition: { rows } }, "C:/runtime").acquisition.rows).toBe(rows);
    }
  });

  it("rejects invalid rows, baud, schema version, default save directory, and offset shape", () => {
    expect(() => normaliseSetupProfile({ schemaVersion: 4 }, "C:/runtime")).toThrow("schemaVersion");
    expect(() => normaliseSetupProfile({ transport: { serial: { baud: 0 } } }, "C:/runtime")).toThrow("baud");
    expect(() => normaliseSetupProfile({ acquisition: { rows: 0 } }, "C:/runtime")).toThrow("rows");
    expect(() => normaliseSetupProfile({ acquisition: { rows: 8.5 } }, "C:/runtime")).toThrow("rows");
    expect(() => normaliseSetupProfile({ paths: { defaultSaveDirectory: " " } }, "C:/runtime")).toThrow("defaultSaveDirectory");
    expect(() => normaliseSetupProfile({ offsetsPf: [[1, 2]] }, "C:/runtime")).toThrow("offsetsPf");
  });

  it("exports display and offsets from the current snapshot without losing transport preferences", () => {
    const current = defaultSetupProfile("C:/runtime");
    current.transport.serial = { port: "SERIAL_TEST_PORT", baud: 921600 };
    const snapshot = createBackendSnapshot();
    snapshot.frame.rows = 2;
    snapshot.display.displayMode = "delta_percent";
    snapshot.matrix.userOffsetPf[0][0] = 7.5;

    const profile = setupProfileFromSnapshot(snapshot, current);

    expect(profile.transport.serial).toEqual({ port: "SERIAL_TEST_PORT", baud: 921600 });
    expect(profile.acquisition.rows).toBe(2);
    expect(profile.display.displayMode).toBe("delta_percent");
    expect(profile.offsetsPf[0][0]).toBe(7.5);
  });

  it("migrates old profiles to a fixed eight-row profile and preserves legacy voltage rail data", () => {
    const oldProfile = normaliseSetupProfile({ schemaVersion: 1, acquisition: { rows: 2 } }, "C:/runtime");
    expect(oldProfile.acquisition.measurementMode).toBe("CAP");
    expect(oldProfile.acquisition.rowModes).toEqual(Array.from({ length: 8 }, () => "CAP"));
    expect(oldProfile.schemaVersion).toBe(3);

    const profile = normaliseSetupProfile(
      {
        acquisition: { rows: 4, measurementMode: "VOLT" },
        voltageRail: { measuredAvddV: 3.391, measuredAvssV: -2.5 }
      },
      "C:/runtime"
    );
    expect(profile.acquisition.measurementMode).toBe("VOLT");
    expect(profile.acquisition.rowModes).toEqual(Array.from({ length: 8 }, () => "VOLT"));
    expect(profile.voltageRail).toEqual({ measuredAvddV: 3.391, measuredAvssV: -2.5 });
  });

  it("round-trips all eight heterogeneous row modes", () => {
    const rowModes = ["RES", "VOLT", "VOLT", "CAP", "CAP", "VOLT", "VOLT", "RES"] as const;
    const profile = normaliseSetupProfile({ acquisition: { rows: 5, measurementMode: "CAP", rowModes } }, "C:/runtime");
    expect(profile.acquisition.rowModes).toEqual(rowModes);
    expect(() => normaliseSetupProfile({ acquisition: { rowModes: ["CAP"] } }, "C:/runtime")).toThrow("exactly 8");
  });
});
