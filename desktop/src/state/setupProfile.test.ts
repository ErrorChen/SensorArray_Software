import { describe, expect, it } from "vitest";

import { defaultSetupProfile, normaliseSetupProfile, setupProfileFromSnapshot } from "./setupProfile";
import { createBackendSnapshot } from "../testUtils/snapshot";

describe("setup profile", () => {
  it("uses the runtime directory as the default save directory", () => {
    const profile = defaultSetupProfile("C:/SensorArray");

    expect(profile.schemaVersion).toBe(2);
    expect(profile.appVersion).toBe("1.0.0");
    expect(profile.paths.defaultSaveDirectory).toBe("C:/SensorArray");
    expect(profile.offsetsPf).toHaveLength(8);
    expect(profile.offsetsPf[0]).toHaveLength(8);
    expect(profile.acquisition.measurementMode).toBe("CAP");
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

  it("rejects invalid rows, baud, schema version, default save directory, and offset shape", () => {
    expect(() => normaliseSetupProfile({ schemaVersion: 3 }, "C:/runtime")).toThrow("schemaVersion");
    expect(() => normaliseSetupProfile({ transport: { serial: { baud: 0 } } }, "C:/runtime")).toThrow("baud");
    expect(() => normaliseSetupProfile({ acquisition: { rows: 3 } }, "C:/runtime")).toThrow("rows");
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

  it("loads old profiles as CAP and preserves measured voltage rails in new profiles", () => {
    const oldProfile = normaliseSetupProfile({ schemaVersion: 1, acquisition: { rows: 2 } }, "C:/runtime");
    expect(oldProfile.acquisition.measurementMode).toBe("CAP");
    expect(oldProfile.schemaVersion).toBe(2);

    const profile = normaliseSetupProfile(
      {
        acquisition: { rows: 4, measurementMode: "VOLT" },
        voltageRail: { measuredAvddV: 3.391, measuredAvssV: -2.5 }
      },
      "C:/runtime"
    );
    expect(profile.acquisition.measurementMode).toBe("VOLT");
    expect(profile.voltageRail).toEqual({ measuredAvddV: 3.391, measuredAvssV: -2.5 });
  });
});
