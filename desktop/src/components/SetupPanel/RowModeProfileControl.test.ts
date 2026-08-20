import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { defaultSetupProfile } from "../../state/setupProfile";
import { createBackendSnapshot } from "../../testUtils/snapshot";
import { profileCode, RowModeProfileControl, rowModeProfileView } from "./RowModeProfileControl";

describe("RowModeProfileControl", () => {
  it("renders eight draft controls and marks rows outside active geometry inactive", () => {
    const snapshot = createBackendSnapshot();
    snapshot.frame.rows = 4;
    const html = renderToStaticMarkup(createElement(RowModeProfileControl, {
      client: null,
      snapshot,
      setupProfile: defaultSetupProfile("."),
      onSetupProfileChange: () => undefined,
      onError: () => undefined
    }));

    for (let row = 1; row <= 8; row += 1) {
      expect(html).toContain(`S${row} measurement mode`);
    }
    expect((html.match(/Inactive with current ROWS setting/g) ?? [])).toHaveLength(4);
    expect(html).toContain("Apply row modes");
  });

  it("keeps applied and pending profiles separate until matching RMAPP", () => {
    const snapshot = createBackendSnapshot();
    snapshot.measurement.rowProfile = {
      appliedModes: Array.from({ length: 8 }, () => "CAP"),
      pendingModes: ["RES", "VOLT", "VOLT", "CAP", "CAP", "VOLT", "VOLT", "RES"],
      transitionState: "accepted",
      requestId: 71,
      generation: 9,
      frameSeq: 125,
      error: ""
    };
    const view = rowModeProfileView(snapshot);
    expect(profileCode(view.appliedModes)).toBe("CCCCCCCC");
    expect(profileCode(view.pendingModes!)).toBe("RVVCCVVR");
    expect(view.transitionState).toBe("accepted");
    expect(view.requestId).toBe(71);
    expect(view.busy).toBe(true);
  });

  it("surfaces RMERR/timeout without fabricating an applied profile", () => {
    const snapshot = createBackendSnapshot();
    snapshot.measurement.rowProfile = {
      appliedModes: Array.from({ length: 8 }, () => "CAP"),
      pendingModes: null,
      transitionState: "timeout",
      requestId: 72,
      generation: 9,
      frameSeq: 125,
      error: "RMAPP timeout"
    };
    const view = rowModeProfileView(snapshot);
    expect(profileCode(view.appliedModes)).toBe("CCCCCCCC");
    expect(view.error).toBe("RMAPP timeout");
  });

  it("blocks ROWMODES while a global MODE transaction is requested or accepted", () => {
    const snapshot = createBackendSnapshot();
    snapshot.measurement.pendingMode = "RES";
    snapshot.measurement.transitionState = "accepted";

    expect(rowModeProfileView(snapshot).busy).toBe(true);
    const html = renderToStaticMarkup(createElement(RowModeProfileControl, {
      client: {} as never,
      snapshot,
      setupProfile: defaultSetupProfile("."),
      onSetupProfileChange: () => undefined,
      onError: () => undefined
    }));
    expect(html).toContain("Applying row modes");
    expect((html.match(/disabled=""/g) ?? []).length).toBeGreaterThanOrEqual(9);
  });

  it("blocks applying a draft containing CAP while FDC restart is required", () => {
    const snapshot = createBackendSnapshot({ mode: "RES" });
    snapshot.fdcIsolation = { sd: "high", verified: true, restartRequired: true };
    const profile = defaultSetupProfile(".");
    const html = renderToStaticMarkup(createElement(RowModeProfileControl, {
      client: {} as never,
      snapshot,
      setupProfile: profile,
      onSetupProfileChange: () => undefined,
      onError: () => undefined
    }));
    expect(html).toContain("This profile contains CAP, which is unavailable until the device is restarted.");
    expect(html).toContain('<button class="primary" disabled="">Apply row modes</button>');
  });
});
