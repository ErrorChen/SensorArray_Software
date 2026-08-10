import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { createBackendSnapshot } from "../../testUtils/snapshot";
import { defaultSetupProfile } from "../../state/setupProfile";
import { MeasurementModeControl, measurementControlView, validateVoltageRailInputs } from "./MeasurementModeControl";

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

  it("requires measured rails for a new VOLT transition and validates their signs", () => {
    expect(validateVoltageRailInputs("", "", false)).toEqual({
      ok: false,
      error: "Voltage mode requires measured AVDD/AVSS rail configuration."
    });
    expect(validateVoltageRailInputs("3.391", "-2.500", false)).toEqual({
      ok: true,
      measuredAvddV: 3.391,
      measuredAvssV: -2.5
    });
    expect(validateVoltageRailInputs("-3.3", "-2.5", false)).toEqual({ ok: false, error: "Measured AVDD must be greater than 0 V." });
    expect(validateVoltageRailInputs("3.3", "2.5", false)).toEqual({ ok: false, error: "Measured AVSS must be less than 0 V." });
    expect(validateVoltageRailInputs("1.8", "-1.0", false)).toEqual({
      ok: false,
      error: "Measured AVDD-AVSS span must be between 3.5 V and 6.0 V."
    });
  });

  it("allows firmware-configured rails to be reused without inventing host values", () => {
    expect(validateVoltageRailInputs("", "", true)).toEqual({ ok: true });
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
    expect(html).toContain("→ VOLT");
    expect(html).toContain("Waiting for firmware apply (MAPP #42)");
  });

  it("does not present voltage rail inputs as resistance controls", () => {
    const snapshot = createBackendSnapshot({ mode: "RES" });
    const html = renderToStaticMarkup(
      createElement(MeasurementModeControl, {
        client: null,
        snapshot,
        setupProfile: defaultSetupProfile("."),
        onSetupProfileChange: () => undefined,
        onError: () => undefined
      })
    );
    expect(html).not.toContain("Voltage measurement rails");
    expect(html).not.toContain("Measured AVDD to GND");
  });
});
