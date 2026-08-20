import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { defaultSetupProfile } from "../../state/setupProfile";
import { createBackendSnapshot } from "../../testUtils/snapshot";
import { AdvancedPanel } from "./AdvancedPanel";

beforeAll(() => {
  vi.stubGlobal("window", {});
  vi.stubGlobal("navigator", { userAgent: "vitest" });
});

function render(snapshot = createBackendSnapshot({ mode: "RES" })): string {
  return renderToStaticMarkup(createElement(AdvancedPanel, {
    client: {} as never,
    snapshot,
    setupProfile: defaultSetupProfile("."),
    runtimeDirectory: ".",
    onSetupProfileChange: () => undefined,
    onError: () => undefined,
    onNotice: () => undefined
  }));
}

describe("AdvancedPanel FDC isolation", () => {
  it("enables isolation only for authoritative homogeneous VOLT/RES", () => {
    const snapshot = createBackendSnapshot({ mode: "RES" });
    snapshot.measurement.authoritativeStateKnown = true;
    snapshot.fdcIsolation = { sd: "low", verified: true, restartRequired: false };
    const html = render(snapshot);
    expect(html).toContain("FDC SD command/readback verified.");
    expect(html).toContain(">Enable</button>");
    expect(html).not.toContain("only after bootstrap confirms");
  });

  it("explains restart-required semantics and does not claim electrical verification", () => {
    const snapshot = createBackendSnapshot({ mode: "RES" });
    snapshot.measurement.authoritativeStateKnown = true;
    snapshot.fdcIsolation = { sd: "high", verified: true, restartRequired: true };
    const html = render(snapshot);
    expect(html).toContain("FDC shutdown is active. CAP is unavailable until device restart.");
    expect(html).toContain("FDC SD command/readback verified.");
    expect(html).not.toContain("electrically isolated verified");
  });

  it("keeps output policy, firmware drops, host loss, parser and CRC counters distinct", () => {
    const snapshot = createBackendSnapshot({ mode: "CAP" });
    snapshot.diagnostics = {
      expectedOutputDecimation: 147,
      firmwareSuppressedNonFresh: 3,
      firmwareReportedDrop: 2,
      firmwareAttributedSequenceGap: 2,
      wireInterleaveRecoveries: 7,
      wireInterleaveDroppedFrames: 1,
      hostTransportDrop: 1,
      parserRejects: 4,
      pendingFirmwareEvidenceGap: 8,
      hostUnexplainedSequenceGap: 5,
      crcFailures: 6,
      parserFrames: 700
    };
    const html = render(snapshot);
    expect(html).toContain("expected output decimation 147");
    expect(html).toContain("firmware non-fresh suppression 3");
    expect(html).toContain("firmware reported drops 2");
    expect(html).toContain("firmware-attributed seq gaps 2");
    expect(html).toContain("host ingress drops 1");
    expect(html).toContain("parser rejects 4");
    expect(html).toContain("awaiting firmware evidence 8");
    expect(html).toContain("unexplained sequence gaps 5");
    expect(html).toContain("CRC errors 6");
    expect(html).toContain("wire interleave recoveries 7");
    expect(html).toContain("pending frames discarded by recovery 1");
  });

  it("shows authoritative calibration metadata without inventing factory calibration", () => {
    const snapshot = createBackendSnapshot({ mode: "CAP" });
    snapshot.calibration = {
      source: "0",
      schema: 0,
      valid: false,
      boardId: "00000000",
      hardwareRev: 0,
      payloadLength: 0,
      state: "default",
      rawFields: { reason: "no_blob" }
    };
    const html = render(snapshot);
    expect(html).toContain("Uncalibrated/default");
    expect(html).toContain("source 0");
    expect(html).toContain("schema 0");
    expect(html).toContain("board 00000000");
    expect(html).toContain("reason no_blob");
    expect(html).not.toContain("Factory calibrated");
  });

  it("shows BOOT, READY, PROTO, and BUILD authority in the advanced device view", () => {
    const snapshot = createBackendSnapshot({ mode: "CAP" });
    snapshot.device = {
      bootId: 17,
      bootCount: 4,
      resetReason: "software",
      ready: true,
      stage: "frame_read",
      lastError: "0x0",
      protocol: { version: "1", wires: "ascii", compatible: true },
      build: { project: "SensorArray", idf: "v5.5.5", target: "esp32s3", proto: "1" },
      lifecycleEvents: []
    };
    const html = render(snapshot);
    expect(html).toContain("boot 17");
    expect(html).toContain("READY true");
    expect(html).toContain("protocol 1 (ascii)");
    expect(html).toContain("build SensorArray");
    expect(html).toContain("IDF v5.5.5");
  });
});
