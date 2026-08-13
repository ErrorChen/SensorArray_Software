import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { createBackendSnapshot } from "../../testUtils/snapshot";
import { StatusBar } from "./StatusBar";

describe("StatusBar telemetry availability", () => {
  it("does not label absent battery or unqueried ADS data as fresh/warning", () => {
    const snapshot = createBackendSnapshot();
    snapshot.battery = { available: false, state: "Unknown", batteryText: "N/A" };
    snapshot.ads = {
      identity: {},
      identityAvailable: false,
      identityConfirmed: null,
      label: "ADS identity not queried",
      chip: "unknown",
      valid: false
    };

    const html = renderToStaticMarkup(createElement(StatusBar, { snapshot, socketState: "connected" }));
    expect(html).toContain("Battery —");
    expect(html).not.toContain("ADS identity unconfirmed");
  });

  it("warns for an explicit unknown ADS identity and reports real battery freshness", () => {
    const snapshot = createBackendSnapshot();
    snapshot.battery = { available: true, batteryText: "4.012 V", batteryMv: 4012, valid: true, fresh: false };
    snapshot.ads = {
      identity: { chip: "unknown", valid: 0 },
      identityAvailable: true,
      identityConfirmed: false,
      label: "ADS identity unconfirmed",
      chip: "unknown",
      valid: false
    };

    const html = renderToStaticMarkup(createElement(StatusBar, { snapshot, socketState: "connected" }));
    expect(html).toContain("Battery 4.012 V (last known · stale)");
    expect(html).toContain("ADS identity unconfirmed");
  });

  it("keeps last-good voltage visible after an invalid attempt and updates on the next fresh attempt", () => {
    const snapshot = createBackendSnapshot();
    snapshot.battery = {
      available: true,
      latestAttempt: { batteryMv: null, valid: false, fresh: false, reason: "adc_timeout" },
      lastGood: { batteryMv: 4092, batteryText: "4.092 V", valid: true, fresh: false }
    };
    let html = renderToStaticMarkup(createElement(StatusBar, { snapshot, socketState: "connected" }));
    expect(html).toContain("Battery 4.092 V (last known · adc_timeout)");

    snapshot.battery.latestAttempt = { batteryMv: 4088, batteryText: "4.088 V", valid: true, fresh: true };
    snapshot.battery.lastGood = snapshot.battery.latestAttempt;
    html = renderToStaticMarkup(createElement(StatusBar, { snapshot, socketState: "connected" }));
    expect(html).toContain("Battery 4.088 V (fresh)");
  });

  it("uses top-level connection staleness even when latestAttempt was fresh", () => {
    const snapshot = createBackendSnapshot();
    snapshot.battery = {
      available: true,
      state: "stale",
      batteryText: "4.092 V",
      batteryMv: 4092,
      valid: true,
      fresh: false,
      reason: "connection_stale",
      latestAttempt: { batteryMv: 4092, batteryText: "4.092 V", valid: true, fresh: true, reason: "ok" },
      lastGood: { batteryMv: 4092, batteryText: "4.092 V", valid: true, fresh: false }
    };

    const html = renderToStaticMarkup(createElement(StatusBar, { snapshot, socketState: "connected" }));
    expect(html).toContain("Battery 4.092 V (last known \u00B7 connection_stale)");
    expect(html).not.toContain("Battery 4.092 V (fresh)");
  });
});
