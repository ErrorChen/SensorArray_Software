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
    expect(html).not.toContain("Battery N/A (fresh)");
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
    expect(html).toContain("Battery 4.012 V (stale)");
    expect(html).toContain("ADS identity unconfirmed");
  });
});
