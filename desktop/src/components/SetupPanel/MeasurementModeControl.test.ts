import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { createBackendSnapshot } from "../../testUtils/snapshot";
import { defaultSetupProfile } from "../../state/setupProfile";
import { MeasurementModeControl, measurementControlView } from "./MeasurementModeControl";

describe("MeasurementModeControl state", () => {
  it("shows requested VOLT separately while CAP remains applied", () => {
    const snapshot = createBackendSnapshot();
    snapshot.measurement.pendingMode = "VOLT";
    snapshot.measurement.transitionState = "accepted";
    snapshot.measurement.requestId = 42;

    expect(measurementControlView(snapshot)).toEqual({
      appliedMode: "CAP",
      pendingMode: "VOLT",
      transitionState: "accepted",
      status: "Waiting for firmware apply (MAPP #42)",
      requestId: 42,
      busy: true,
      error: ""
    });
  });

  it("surfaces timeout/error state without committing the pending mode", () => {
    const snapshot = createBackendSnapshot();
    snapshot.measurement.pendingMode = "RES";
    snapshot.measurement.transitionState = "timeout";
    snapshot.measurement.error = "MAPP timeout";

    const view = measurementControlView(snapshot);
    expect(view.appliedMode).toBe("CAP");
    expect(view.pendingMode).toBe("RES");
    expect(view.busy).toBe(false);
    expect(view.error).toBe("MAPP timeout");
  });

  it("renders applied and pending modes as separate user-visible state", () => {
    const snapshot = createBackendSnapshot();
    snapshot.measurement.pendingMode = "VOLT";
    snapshot.measurement.transitionState = "accepted";
    snapshot.measurement.requestId = 42;
    const html = renderToStaticMarkup(
      createElement(MeasurementModeControl, {
        client: null,
        snapshot,
        setupProfile: defaultSetupProfile("."),
        onSetupProfileChange: () => undefined,
        onError: () => undefined
      })
    );
    expect(html).toContain("Applied mode");
    expect(html).toContain("CAP");
    expect(html).toContain("\u2192 VOLT");
    expect(html).toContain("Waiting for firmware apply (MAPP #42)");
  });

  it("removes AVDD/AVSS inputs and presents internal rail telemetry read-only", () => {
    const snapshot = createBackendSnapshot({ mode: "VOLT" });
    snapshot.measurement.railTelemetry = {
      railSpanUv: 5_126_000,
      valid: true,
      fresh: true,
      age: 1.8,
      source: "internal_monitor",
      reason: "",
      timestamp: 123
    };
    const html = renderToStaticMarkup(
      createElement(MeasurementModeControl, {
        client: null,
        snapshot,
        setupProfile: defaultSetupProfile("."),
        onSetupProfileChange: () => undefined,
        onError: () => undefined
      })
    );
    expect(html).toContain("ADS analogue rail span");
    expect(html).toContain("AVDD \u2212 AVSS: 5.126 V");
    expect(html).toContain("age: 1.8 s");
    expect(html).toContain("source: internal monitor");
    expect(html).not.toContain("Measured AVDD to GND");
    expect(html).not.toContain("Measured AVSS to GND");
    expect(html).not.toContain("<input");
  });

  it("keeps a retained rail span visible with stale age", () => {
    const snapshot = createBackendSnapshot({ mode: "VOLT" });
    snapshot.measurement.railTelemetry = {
      railSpanUv: 5_126_000,
      valid: false,
      fresh: false,
      age: 12,
      source: "internal_monitor",
      reason: "connection_stale",
      timestamp: 123
    };
    const html = renderToStaticMarkup(createElement(MeasurementModeControl, {
      client: null,
      snapshot,
      setupProfile: defaultSetupProfile("."),
      onSetupProfileChange: () => undefined,
      onError: () => undefined
    }));
    expect(html).toContain("AVDD − AVSS: 5.126 V");
    expect(html).toContain("stale 12 s");
  });

  it("blocks global MODE while a row-profile transaction is requested or accepted", () => {
    const snapshot = createBackendSnapshot();
    snapshot.measurement.rowProfile!.transitionState = "accepted";
    snapshot.measurement.rowProfile!.pendingModes = ["RES", "VOLT", "VOLT", "CAP", "CAP", "VOLT", "VOLT", "RES"];

    expect(measurementControlView(snapshot).busy).toBe(true);
    const html = renderToStaticMarkup(
      createElement(MeasurementModeControl, {
        client: {} as never,
        snapshot,
        setupProfile: defaultSetupProfile("."),
        onSetupProfileChange: () => undefined,
        onError: () => undefined
      })
    );
    expect((html.match(/disabled=""/g) ?? [])).toHaveLength(3);
  });
});
