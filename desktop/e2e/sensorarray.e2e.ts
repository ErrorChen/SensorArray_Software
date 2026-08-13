import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";

import type { BackendSnapshotPayload } from "../src/api/types";
import { prepareGuiReplayFixtures, type GuiReplayFixtures } from "./fixtureFactory";

const e2eDirectory = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(e2eDirectory, "..", "..");
const desktopRoot = path.join(repoRoot, "desktop");
const screenshotRoot = path.join(repoRoot, "validation_artifacts", "gui");
const userDataRoot = path.join(repoRoot, "validation_artifacts", "electron-user-data");

let fixtures: GuiReplayFixtures;
let electronApp: ElectronApplication | null = null;
let page: Page;
let backendUrl = "";
let electronUserDataDir = "";

test.beforeAll(() => {
  mkdirSync(screenshotRoot, { recursive: true });
  mkdirSync(userDataRoot, { recursive: true });
  fixtures = prepareGuiReplayFixtures(repoRoot);
});

test.beforeEach(async () => {
  const environment: Record<string, string> = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (typeof value === "string") {
      environment[key] = value;
    }
  }
  // The formal GUI suite must exercise Electron's local-file renderer and the
  // Python sidecar selected by electron/main.ts. A stale developer URL would
  // silently turn this into a Vite/Chromium test, so remove it explicitly.
  delete environment.SENSORARRAY_FRONTEND_URL;
  delete environment.ELECTRON_RUN_AS_NODE;
  electronUserDataDir = mkdtempSync(path.join(userDataRoot, "case-"));
  electronApp = await electron.launch({
    args: [`--user-data-dir=${electronUserDataDir}`, path.join(desktopRoot, "dist-electron", "main.js")],
    cwd: desktopRoot,
    env: environment
  });
  page = await electronApp.firstWindow();
  await expect(page.getByTestId("measurement-mode-control")).toBeVisible({ timeout: 30_000 });
  const bridgeBackendUrl = await page.evaluate(async () => {
    const desktopWindow = globalThis as typeof globalThis & {
      sensorarrayDesktop?: { getBackendUrl?: () => Promise<string> };
    };
    return desktopWindow.sensorarrayDesktop?.getBackendUrl?.();
  });
  expect(bridgeBackendUrl).toMatch(/^http:\/\/127\.0\.0\.1:8(?:8\d\d|9[0-8]\d)$/);
  backendUrl = String(bridgeBackendUrl);
});

test.afterEach(async () => {
  if (electronApp) {
    await electronApp.close();
    electronApp = null;
  }
  backendUrl = "";
  if (electronUserDataDir && path.dirname(electronUserDataDir) === userDataRoot) {
    rmSync(electronUserDataDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  }
  electronUserDataDir = "";
});

test.describe.serial("SensorArray real backend + Replay GUI acceptance", () => {
  test("G01 App boot", async () => {
    const fatalErrors = watchFatalErrors(page);
    await openApp(page);

    const health = await page.request.get(`${backendUrl}/health`);
    expect(health.ok()).toBeTruthy();
    expect(await health.json()).toMatchObject({ ok: true, service: "sensorarray_backend" });
    await expect(page.locator(".heatmapPanel")).toBeVisible();
    await expect(page.locator(".trendPanel")).toBeVisible();
    await expect(page.locator(".setupPanel")).toBeVisible();
    await expect(page.locator(".commandPane")).toBeVisible();
    await expect(page.getByRole("button", { name: "Raw Log", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Status", exact: true })).toBeVisible();
    await expect(page.locator(".commandPane")).toBeInViewport();
    await expect(page.getByRole("button", { name: "Raw Log", exact: true })).toBeInViewport();
    await expect(page.locator(".statusItems")).not.toContainText("Battery N/A (fresh)");
    await expect(page.locator(".statusItems")).not.toContainText("ADS identity unconfirmed");
    expect(await hasOuterPageOverflow(page)).toBe(false);
    await saveScreenshot(page, "G01_boot.png");
    expect(fatalErrors, fatalErrors.join("\n")).toEqual([]);
  });

  test("G02 CAP regression", async () => {
    const fatalErrors = watchFatalErrors(page);
    await openApp(page);
    await page.getByRole("button", { name: "Replay", exact: true }).click();
    await page.locator(".modePanel input").first().fill(fixtures.cap8);
    await page.getByRole("button", { name: "Connect", exact: true }).click();
    await waitForStatus(page, (status) => status.frame.seq === 9 && status.measurement.appliedMode === "CAP");

    const status = await backendStatus(page);
    expect(status.matrix.quantity).toBe("capacitance");
    expect(status.matrix.unit).toBe("pF");
    expect(status.frame.rows).toBe(8);
    expect(status.matrix.valid[0][0]).toBe(false);
    expect(status.matrix.values[0][0]).toBeNull();
    expect(status.matrix.displayValues[0][0]).toBeNull();
    expect(status.display.colorRange.min).not.toBe(0);
    expect(status.matrix.valid.flat().filter(Boolean)).toHaveLength(63);

    await expect(page.getByTestId("measurement-applied-mode")).toHaveText("CAP");
    await expect(page.getByText("8x8 Capacitance Heatmap")).toBeVisible();
    await expect(page.getByText("Capacitance display")).toBeVisible();
    await expect(page.getByRole("button", { name: "Set baseline" })).toBeVisible();
    await expect(page.locator(".trendPanel canvas").first()).toBeVisible();
    await page.getByRole("tab", { name: "Advanced" }).click();
    await expect(page.getByText("User offset pF", { exact: true })).toBeVisible();
    await page.getByRole("tab", { name: "Setup" }).click();
    await page.getByText("Capacitance display").scrollIntoViewIfNeeded();
    await saveScreenshot(page, "G02_cap.png");
    expect(fatalErrors, fatalErrors.join("\n")).toEqual([]);
  });

  test("G03 Measurement mode selector", async () => {
    await openApp(page);
    const control = page.getByTestId("measurement-mode-control");
    await expect(control).toBeVisible();
    for (const mode of ["CAP", "VOLT", "RES"]) {
      await expect(control.getByRole("button", { name: mode, exact: true })).toBeVisible();
    }
    await expect(page.getByTestId("measurement-applied-mode")).toHaveText("CAP");
  });

  test("G04 VOLT remains pending after MACK", async () => {
    const fatalErrors = watchFatalErrors(page);
    await openApp(page);
    await startReplay(page, fixtures.modeTimeline);
    await waitForStatus(
      page,
      (status) =>
        status.measurement.appliedMode === "CAP" &&
        status.measurement.pendingMode === "VOLT" &&
        status.measurement.transitionState === "accepted"
    );

    await expect(page.getByTestId("measurement-applied-mode")).toHaveText("CAP");
    await expect(page.getByTestId("measurement-pending-mode")).toContainText("VOLT");
    await expect(page.getByTestId("measurement-transition-state")).toContainText("Waiting for firmware apply (MAPP #42)");
    await expect(page.locator(".statusError")).toHaveCount(0);
    await page.getByTestId("measurement-mode-control").scrollIntoViewIfNeeded();
    await saveScreenshot(page, "G04_volt_pending.png");

    await waitForStatus(page, (status) => status.measurement.appliedMode === "VOLT" && status.frame.seq === 8);
    expect(fatalErrors, fatalErrors.join("\n")).toEqual([]);
  });

  test("G05 VOLT applied with negative physical values", async () => {
    const fatalErrors = watchFatalErrors(page);
    await openApp(page);
    await startReplay(page, fixtures.voltage);
    await waitForStatus(page, (status) => status.measurement.appliedMode === "VOLT" && status.frame.seq === 8);

    const status = await backendStatus(page);
    expect(status.measurement.pendingMode).toBeNull();
    expect(status.measurement.generation).toBe(7);
    expect(status.measurement.requestId).toBe(42);
    expect(status.matrix.quantity).toBe("voltage");
    expect(status.matrix.unit).toBe("V");
    expect(status.matrix.scale).toBe(-6);
    expect(status.matrix.rawFixed[0][0]).toBe(-1250);
    expect(status.matrix.values[0][0]).toBeCloseTo(-0.00125, 9);

    await expect(page.getByTestId("measurement-applied-mode")).toHaveText("VOLT");
    await expect(page.getByText("2x8 Voltage Heatmap")).toBeVisible();
    await expect(page.locator('[aria-label="Measurement heatmap; colour scale units V"]')).toBeVisible();
    await expect(page.locator(".trendPanel .panelHeader").first()).toContainText("Voltage");
    await expect(page.getByText("Baseline, Delta C/C0, and capacitance offsets are available for active CAP rows only.")).toBeVisible();
    await expect(page.getByText("ADS analogue rail span")).toBeVisible();
    await expect(page.getByTestId("rail-telemetry")).toContainText("AVDD \u2212 AVSS: 5.126 V");
    await expect(page.getByTestId("rail-telemetry")).toContainText("fresh");
    await expect(page.getByTestId("rail-telemetry")).toContainText("source: internal monitor");
    await expect(page.getByLabel("Measured AVDD to GND")).toHaveCount(0);
    await expect(page.getByLabel("Measured AVSS to GND")).toHaveCount(0);
    await page.getByText("ADS analogue rail span").scrollIntoViewIfNeeded();
    await saveScreenshot(page, "rail-readonly.png");
    await page.getByText("Baseline, Delta C/C0, and capacitance offsets are available for active CAP rows only.").scrollIntoViewIfNeeded();
    await saveScreenshot(page, "G05_volt.png");
    expect(fatalErrors, fatalErrors.join("\n")).toEqual([]);
  });

  test("G06 VOLT PGA gains and verified bypass tooltip", async () => {
    await openApp(page);
    await startReplay(page, fixtures.voltage);
    await waitForStatus(page, (status) => status.measurement.appliedMode === "VOLT" && status.frame.seq === 8);

    const expected = ["PGA ×1", "PGA ×2", "PGA ×4", "PGA ×8", "PGA ×16", "PGA ×32", "PGA bypass"];
    for (let col = 0; col < expected.length; col += 1) {
      await hoverHeatmapCell(page, 0, col);
      await expect(page.locator("body")).toContainText(expected[col]);
    }
    await saveScreenshot(page, "G06_volt_pga.png");
  });

  test("G07 Xhh cells remain null and expose known/unknown reasons", async () => {
    await openApp(page);
    await startReplay(page, fixtures.voltage);
    await waitForStatus(page, (status) => status.measurement.appliedMode === "VOLT" && status.frame.seq === 8);

    const status = await backendStatus(page);
    expect(status.matrix.values[0][4]).toBeNull();
    expect(status.matrix.valid[0][4]).toBe(false);
    expect(status.matrix.errorCodes[0][4]).toBe(3);
    expect(status.matrix.errorReasons[0][4]).toBe("ADS DRDY timeout");
    expect(status.matrix.values[1][0]).toBeNull();
    expect(status.matrix.errorCodes[1][0]).toBe(254);
    expect(status.matrix.errorReasons[1][0]).toBe("Unknown firmware cell error 0xFE");

    await hoverInvalidHeatmapCell(page, 0, 4);
    const invalidTooltip = page.getByTestId("heatmap-invalid-tooltip");
    await expect(invalidTooltip).toBeVisible();
    await expect(invalidTooltip).toContainText("0x03");
    await expect(invalidTooltip).toContainText("ADS DRDY timeout");
    await saveScreenshot(page, "G07_invalid.png");
    await hoverInvalidHeatmapCell(page, 1, 0);
    await expect(invalidTooltip).toContainText("Unknown firmware cell error 0xFE");
  });

  test("G08 old measurement generation is rejected", async () => {
    await openApp(page);
    const before = numericDiagnostic(await backendStatus(page), "staleGenerationDrops");
    await startReplay(page, fixtures.oldGeneration);
    await waitForStatus(page, (status) => numericDiagnostic(status, "staleGenerationDrops") > before);

    let status = await backendStatus(page);
    expect(status.frame.seq).toBe(8);
    expect(status.matrix.generation).toBe(7);
    expect(status.matrix.values[0][0]).toBeCloseTo(-0.00125, 9);

    await waitForStatus(page, (current) => current.frame.seq === 10);
    status = await backendStatus(page);
    expect(status.matrix.generation).toBe(7);
    expect(status.matrix.values[0][0]).toBeCloseTo(-0.0025, 9);
  });

  test("G09 CRC rejection does not mutate matrix and parser recovers", async () => {
    await openApp(page);
    const before = numericDiagnostic(await backendStatus(page), "crcFailures");
    await startReplay(page, fixtures.crcRecovery);
    await waitForStatus(page, (status) => numericDiagnostic(status, "crcFailures") > before);

    let status = await backendStatus(page);
    expect(status.frame.seq).toBe(8);
    expect(status.matrix.values[0][0]).toBeCloseTo(-0.00125, 9);
    expect(status.logs.rows.some((row) => row.tag === "PARSER" && row.rawText.toLowerCase().includes("crc"))).toBe(true);

    await waitForStatus(page, (current) => current.frame.seq === 10);
    status = await backendStatus(page);
    expect(status.matrix.values[0][0]).toBeCloseTo(-0.0025, 9);
    await page.getByRole("button", { name: "Status", exact: true }).click();
    await expect(page.getByText("Parser issue").first()).toBeVisible();
  });

  test("G10 RES presentation, raw mΩ and PGA", async () => {
    const fatalErrors = watchFatalErrors(page);
    await openApp(page);
    await startReplay(page, fixtures.resistance);
    await waitForStatus(page, (status) => status.measurement.appliedMode === "RES" && status.frame.seq === 9);

    const status = await backendStatus(page);
    expect(status.matrix.quantity).toBe("resistance");
    expect(status.matrix.unit).toBe("ohm");
    expect(status.matrix.scale).toBe(-3);
    expect(status.matrix.rawFixed[0][0]).toBe(1000);
    expect(status.matrix.values[0][0]).toBeCloseTo(1, 9);
    expect(status.matrix.pgaBypass[0][0]).toBe(true);
    await expect(page.getByTestId("measurement-applied-mode")).toHaveText("RES");
    await expect(page.getByText("1x8 Resistance Heatmap")).toBeVisible();
    await expect(page.locator('[aria-label="Measurement heatmap; colour scale units Ω"]')).toBeVisible();
    await expect(page.locator(".trendPanel .panelHeader").first()).toContainText("Resistance");
    await expect(page.getByText("Baseline, Delta C/C0, and capacitance offsets are available for active CAP rows only.")).toBeVisible();
    await expect(page.getByText("ADS analogue rail span")).toBeVisible();
    await page.getByTestId("measurement-mode-control").scrollIntoViewIfNeeded();
    await hoverHeatmapCell(page, 0, 0);
    await expect(page.locator("body")).toContainText("Raw integer mΩ: 1000");
    await expect(page.locator("body")).toContainText("Physical resistance: 1.000 Ω");
    await expect(page.locator("body")).toContainText("PGA bypass");
    await saveScreenshot(page, "G10_res.png");
    expect(fatalErrors, fatalErrors.join("\n")).toEqual([]);
  });

  test("G11 dynamic rows 1..8 have no phantom cells", async () => {
    await openApp(page);
    for (const rows of [1, 2, 3, 4, 5, 6, 7, 8] as const) {
      await startReplay(page, fixtures.rows[rows]);
      await waitForStatus(page, (status) => status.measurement.appliedMode === "CAP" && status.frame.rows === rows && status.frame.valid);
      const status = await backendStatus(page);
      expect(status.matrix.valid.flat().filter(Boolean)).toHaveLength(rows * 8);
      for (let row = rows; row < 8; row += 1) {
        expect(status.matrix.valid[row].some(Boolean)).toBe(false);
        expect(status.matrix.values[row].every((value) => value === null)).toBe(true);
      }
      await expect(page.locator(".rowsStatus")).toContainText(`applied ${rows}`);
      await expect(page.locator(".heatmapCanvas")).toHaveAttribute("aria-rowcount", String(rows));
      await expect(page.getByText(`${rows}x8 Capacitance Heatmap`)).toBeVisible();
    }
  });

  test("G12 RES to CAP restores capacitance controls", async () => {
    await openApp(page);
    await startReplay(page, fixtures.capReturn);
    await waitForStatus(page, (status) => status.measurement.appliedMode === "CAP" && status.frame.seq === 1);

    const status = await backendStatus(page);
    expect(status.matrix.quantity).toBe("capacitance");
    expect(status.matrix.unit).toBe("pF");
    expect(status.frame.rows).toBe(1);
    await expect(page.getByTestId("measurement-applied-mode")).toHaveText("CAP");
    await expect(page.getByText("Capacitance display")).toBeVisible();
    await expect(page.getByRole("option", { name: "Delta C/C0 %" })).toHaveCount(1);
    await expect(page.getByRole("button", { name: "Set baseline" })).toBeVisible();
    await page.getByRole("tab", { name: "Advanced" }).click();
    await expect(page.getByText("User offset pF")).toBeVisible();
    expect(await page.locator(".advancedPanel").evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
    expect(await page.locator(".offsetPanel").evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
    await expect(page.getByText("Desktop file APIs are available in the Electron app.")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Browse" })).toBeEnabled();
    await expect(page.getByRole("button", { name: "Check" })).toBeEnabled();
    await expect(page.locator(".savePathPanel .inlineError")).toHaveCount(0);
    await page.locator(".offsetPanel").scrollIntoViewIfNeeded();
    await saveScreenshot(page, "G12_cap_return.png");
  });

  test("G13 Battery and ADS diagnostics remain explicit", async () => {
    await openApp(page);
    await startReplay(page, fixtures.diagnostics);
    await waitForStatus(
      page,
      (status) => status.ads?.identityConfirmed === false && status.battery?.batteryMv === 4012 && status.ads?.state === "completed"
    );

    const status = await backendStatus(page);
    expect(status.ads?.label).toBe("ADS identity unconfirmed");
    expect(status.ads?.chip).toBe("unknown");
    expect(status.battery?.batteryText).toBe("4.012 V");
    // Replay has ended by the time the snapshot is asserted.  The typed store
    // retains the successful attempt, while the authoritative top-level state
    // correctly marks the displayed last-good value connection-stale.
    expect(status.battery?.latestAttempt?.fresh).toBe(true);
    expect(status.battery?.lastGood?.batteryMv).toBe(4012);
    expect(status.battery?.fresh).toBe(false);
    expect(status.battery?.reason).toBe("connection_stale");
    expect(status.battery?.retryCount).toBe(0);
    expect(status.battery?.unstableCount).toBe(1);
    expect(status.battery?.spreadRaw).toBe(5);
    expect(status.battery?.spreadMaximumRaw).toBe(9);
    await expect(page.getByText("ADS identity unconfirmed")).toBeVisible();
    await page.getByRole("button", { name: "Status", exact: true }).click();
    await expect(page.getByText("ADS diagnostic check")).toBeVisible();
    await expect(page.getByText("ADS diagnostic status", { exact: true })).toBeVisible();
    await expect(page.getByText("Battery measurement accepted")).toBeVisible();
  });

  test("G14 malformed frames and unknown logs recover to a valid frame", async () => {
    const fatalErrors = watchFatalErrors(page);
    await openApp(page);
    const rejectsBefore = numericDiagnostic(await backendStatus(page), "parserRejects");
    await startReplay(page, fixtures.malformedRecovery);
    await waitForStatus(
      page,
      (status) => status.measurement.appliedMode === "CAP" && status.frame.seq === 8 && numericDiagnostic(status, "parserRejects") >= rejectsBefore + 4
    );

    const status = await backendStatus(page);
    expect(status.matrix.quantity).toBe("capacitance");
    expect(status.matrix.valid.flat().filter(Boolean)).toHaveLength(64);
    expect(status.logs.rows.some((row) => row.tag === "FUTURE99" && row.recognised === false)).toBe(true);
    await page.getByRole("button", { name: "Status", exact: true }).click();
    await expect(page.getByText("Parser issue").first()).toBeVisible();
    await expect(page.getByText("Unknown firmware log (FUTURE99)")).toBeVisible();
    expect(fatalErrors, fatalErrors.join("\n")).toEqual([]);
  });

  test("G15 splitters and window resize keep charts usable", async () => {
    await openApp(page);
    await startReplay(page, fixtures.cap8);
    await waitForStatus(page, (status) => status.frame.seq === 9);
    const heatmap = page.locator(".heatmapCanvas");
    const trend = page.locator(".trendCanvas").first();
    const initialHeatmap = await requiredBox(heatmap);
    await requiredBox(trend);

    const mainSplitter = page.locator('[role="separator"]').first();
    const bottomSplitter = page.locator('[role="separator"]').nth(1);
    await dragSplitter(page, mainSplitter, 100);
    await dragSplitter(page, bottomSplitter, -80);
    await resizeElectronWindow(1100, 720);
    await expect(heatmap.locator("canvas")).toBeVisible();
    await expect(trend.locator("canvas")).toBeVisible();
    let resizedHeatmap = await requiredBox(heatmap);
    let resizedTrend = await requiredBox(trend);
    expect(resizedHeatmap.width).toBeGreaterThan(200);
    expect(resizedHeatmap.height).toBeGreaterThan(150);
    expect(resizedTrend.width).toBeGreaterThan(120);
    expect(resizedTrend.height).toBeGreaterThan(70);
    expect(resizedHeatmap.width).not.toBe(initialHeatmap.width);

    await resizeElectronWindow(1600, 1000);
    resizedHeatmap = await requiredBox(heatmap);
    resizedTrend = await requiredBox(trend);
    expect(resizedHeatmap.width).toBeGreaterThan(400);
    expect(resizedTrend.width).toBeGreaterThan(160);
  });

  test("G16 Electron preload and backend sidecar smoke", async () => {
    const fatalErrors = watchFatalErrors(page);
    const bridge = await page.evaluate(async () => {
      const desktopWindow = globalThis as typeof globalThis & {
        sensorarrayDesktop?: { getBackendUrl?: () => Promise<string>; getRuntimeDirectory?: () => Promise<string> };
      };
      return {
        present: Boolean(desktopWindow.sensorarrayDesktop),
        backendUrl: await desktopWindow.sensorarrayDesktop?.getBackendUrl?.(),
        runtimeDirectory: await desktopWindow.sensorarrayDesktop?.getRuntimeDirectory?.()
      };
    });
    expect(bridge.present).toBe(true);
    expect(bridge.backendUrl).toBe(backendUrl);
    expect(bridge.runtimeDirectory).toBeTruthy();
    expect(page.url()).toMatch(/^file:\/\/\/.+\/dist\/index\.html\?backendUrl=/);
    const health = await page.evaluate(async (url) => {
      const response = await fetch(`${url}/health`);
      return { ok: response.ok, payload: (await response.json()) as unknown };
    }, bridge.backendUrl);
    expect(health).toMatchObject({ ok: true, payload: { ok: true, service: "sensorarray_backend" } });
    await expect(page.locator(".commandPane")).toBeInViewport();
    await expect(page.getByRole("button", { name: "Raw Log", exact: true })).toBeInViewport();
    expect(await hasOuterPageOverflow(page)).toBe(false);
    await page.screenshot({ path: path.join(screenshotRoot, "G16_electron.png") });
    expect(fatalErrors, fatalErrors.join("\n")).toEqual([]);
  });

  test("G17 ROWS=1 RES has one visible row and a non-neutral single-value scale", async () => {
    await openApp(page);
    await startReplay(page, fixtures.rows1Res);
    await waitForStatus(page, (status) => status.frame.seq === 91 && status.frame.rows === 1 && status.matrix.modeByRow?.[0] === "RES");
    const status = await backendStatus(page);
    expect(status.matrix.displayValues[0][0]).toBeCloseTo(10_025, 6);
    expect(status.matrix.valid[0].filter(Boolean)).toHaveLength(1);
    expect(status.matrix.fresh[0].filter(Boolean)).toHaveLength(1);
    expect(usableActiveCells(status)).toBe(1);
    expect(status.display.colourRanges?.resistance?.max).toBeGreaterThan(status.display.colourRanges?.resistance?.min ?? 0);
    const range = status.display.colourRanges?.resistance;
    expect(range && range.min !== null && range.max !== null ? (10_025 - range.min) / (range.max - range.min) : 0).toBeGreaterThan(0.5);
    await expect(page.getByText("1x8 Resistance Heatmap")).toBeVisible();
    await expect(page.locator(".heatmapCanvas")).toHaveAttribute("aria-rowcount", "1");
    await expect(page.locator('[aria-label="Measurement heatmap; colour scale units Ω"]')).toBeVisible();
    await expect
      .poll(async () => (await sampleHeatmapCellColours(page, 0, 0, 1)).some(isRedDominant), {
        timeout: 5_000,
        intervals: [50, 100, 200]
      })
      .toBe(true);
    await saveScreenshot(page, "rows1-res.png");
  });

  test("G18 ROWS=3 CAP renders only S1..S3", async () => {
    await openApp(page);
    await startReplay(page, fixtures.rows[3]);
    await waitForStatus(page, (status) => status.frame.rows === 3 && status.frame.valid && status.matrix.modeByRow?.[0] === "CAP");
    await expect(page.getByText("3x8 Capacitance Heatmap")).toBeVisible();
    await expect(page.locator(".heatmapCanvas")).toHaveAttribute("aria-rowcount", "3");
    await saveScreenshot(page, "rows3-cap.png");
  });

  test("G19 ROWS=5 mixed replay isolates row semantics and colour domains", async () => {
    await openApp(page);
    await startReplay(page, fixtures.mixed5);
    await waitForStatus(page, (status) => status.frame.seq === 201 && status.frame.layout === "MIXED" && status.frame.rows === 5);
    const status = await backendStatus(page);
    expect(status.frame.rowModes).toEqual(["CAP", "RES", "VOLT", "CAP", "RES", "VOLT", "CAP", "RES"]);
    expect(status.matrix.modeByRow?.slice(0, 5)).toEqual(["CAP", "RES", "VOLT", "CAP", "RES"]);
    expect(status.matrix.unitByRow?.slice(0, 5)).toEqual(["pF", "ohm", "V", "pF", "ohm"]);
    expect(Object.keys(status.display.colourRanges ?? {})).toEqual(expect.arrayContaining(["cap_absolute", "voltage", "resistance"]));
    await expect(page.getByText("5x8 Mixed Measurement Heatmap")).toBeVisible();
    await expect(page.locator('[aria-label="Measurement heatmap; colour scale units pF, V, Ω"]')).toBeVisible();
    const profile = page.getByTestId("row-mode-status");
    await expect(profile).toContainText("CRVCRVCR");
    await expect(page.getByText("Inactive with current ROWS setting")).toHaveCount(3);
    await profile.scrollIntoViewIfNeeded();
    await saveScreenshot(page, "rows5-mixed.png");
  });

  test("G20 ROWS=8 mixed replay shows RVVCCVVR and three independent units", async () => {
    await openApp(page);
    await startReplay(page, fixtures.mixed8);
    await waitForStatus(page, (status) => status.frame.seq === 202 && status.frame.layout === "MIXED" && status.frame.rows === 8);
    const status = await backendStatus(page);
    expect(status.frame.rowModes).toEqual(["RES", "VOLT", "VOLT", "CAP", "CAP", "VOLT", "VOLT", "RES"]);
    expect(status.frame.profileGeneration).toBe(11);
    expect(status.frame.profileRequestId).toBe(62);
    await expect(page.getByText("8x8 Mixed Measurement Heatmap")).toBeVisible();
    await expect(page.locator('[aria-label="Measurement heatmap; colour scale units pF, V, Ω"]')).toBeVisible();
    const profile = page.getByTestId("row-mode-status");
    await expect(profile).toContainText("RVVCCVVR");
    await profile.scrollIntoViewIfNeeded();
    await saveScreenshot(page, "rows8-mixed.png");
  });

  test("G21 battery last-good remains visible after an invalid attempt", async () => {
    await openApp(page);
    await startReplay(page, fixtures.batteryStale);
    await waitForStatus(page, (status) =>
      status.battery?.latestAttempt?.valid === false &&
      status.battery?.lastGood?.batteryMv === 4092 &&
      status.battery?.lastGood?.firmwareAuthoritative === true
    );
    const status = await backendStatus(page);
    expect(status.battery?.lastGood?.source).toBe("firmware");
    await expect(page.locator(".statusItems")).toContainText("Battery 4.092 V (last known · adc_timeout)");
    await saveScreenshot(page, "battery-stale.png");
  });
});

async function openApp(page: Page): Promise<void> {
  expect(page.url()).toMatch(/^file:\/\/\/.+\/dist\/index\.html\?backendUrl=/);
  await expect(page.getByTestId("measurement-mode-control")).toBeVisible();
  await expect(page.locator(".socketState")).toHaveText("connected");
}

async function hasOuterPageOverflow(page: Page): Promise<boolean> {
  return page.evaluate(
    () =>
      document.documentElement.scrollHeight > window.innerHeight + 1 ||
      document.documentElement.scrollWidth > window.innerWidth + 1 ||
      document.body.scrollHeight > window.innerHeight + 1 ||
      document.body.scrollWidth > window.innerWidth + 1
  );
}

async function resizeElectronWindow(width: number, height: number): Promise<void> {
  if (!electronApp) {
    throw new Error("Electron application is not running");
  }
  await electronApp.evaluate(
    ({ BrowserWindow }, requestedSize) => {
      const window = BrowserWindow.getAllWindows()[0];
      if (!window) {
        throw new Error("Electron BrowserWindow is unavailable");
      }
      window.setSize(requestedSize.width, requestedSize.height);
    },
    { width, height }
  );
  await expect
    .poll(
      async () =>
        electronApp?.evaluate(({ BrowserWindow }) => {
          const bounds = BrowserWindow.getAllWindows()[0]?.getBounds();
          return bounds ? `${bounds.width}x${bounds.height}` : "missing";
        }),
      { timeout: 5_000 }
    )
    .toBe(`${width}x${height}`);
}

async function startReplay(page: Page, replayPath: string): Promise<void> {
  const opened = await page.request.post(`${backendUrl}/api/replay/open`, { data: { path: replayPath, speed: 1 } });
  expect(opened.ok(), await opened.text()).toBeTruthy();
  const started = await page.request.post(`${backendUrl}/api/replay/start`, { data: {} });
  expect(started.ok(), await started.text()).toBeTruthy();
}

async function backendStatus(page: Page): Promise<BackendSnapshotPayload> {
  const response = await page.request.get(`${backendUrl}/api/status`);
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()) as BackendSnapshotPayload;
}

async function waitForStatus(page: Page, predicate: (status: BackendSnapshotPayload) => boolean): Promise<void> {
  await expect.poll(async () => predicate(await backendStatus(page)), { timeout: 15_000, intervals: [50, 100, 200] }).toBe(true);
}

function numericDiagnostic(status: BackendSnapshotPayload, key: string): number {
  const value = status.diagnostics[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function usableActiveCells(status: BackendSnapshotPayload): number {
  let usable = 0;
  const rows = Math.max(1, Math.min(8, Math.trunc(status.frame.rows)));
  for (let row = 0; row < rows; row += 1) {
    for (let col = 0; col < 8; col += 1) {
      const value = status.matrix.displayValues?.[row]?.[col];
      if (
        status.matrix.valid?.[row]?.[col] &&
        status.matrix.fresh?.[row]?.[col] &&
        !status.matrix.error?.[row]?.[col] &&
        typeof value === "number" &&
        Number.isFinite(value)
      ) {
        usable += 1;
      }
    }
  }
  return usable;
}

async function sampleHeatmapCellColours(
  page: Page,
  row: number,
  col: number,
  rows: number
): Promise<Array<[number, number, number, number]>> {
  return page.locator(".heatmapCanvas").evaluate((host, coordinates) => {
    const canvases = Array.from(host.querySelectorAll("canvas"));
    const canvas = canvases.at(-1);
    const context = canvas?.getContext("2d");
    if (!canvas || !context || canvas.clientWidth <= 0 || canvas.clientHeight <= 0) {
      return [];
    }
    const gridWidth = canvas.clientWidth - 64 - 28;
    const gridHeight = canvas.clientHeight - 28 - 72;
    const cellWidth = gridWidth / 8;
    const cellHeight = gridHeight / coordinates.rows;
    const scaleX = canvas.width / canvas.clientWidth;
    const scaleY = canvas.height / canvas.clientHeight;
    const fractions = [[0.2, 0.2], [0.75, 0.2], [0.2, 0.75], [0.75, 0.75]];
    return fractions.map(([xFraction, yFraction]) => {
      const x = (64 + (coordinates.col + xFraction) * cellWidth) * scaleX;
      const y = (28 + (coordinates.row + yFraction) * cellHeight) * scaleY;
      return Array.from(context.getImageData(Math.floor(x), Math.floor(y), 1, 1).data) as [number, number, number, number];
    });
  }, { row, col, rows });
}

function isRedDominant([red, green, blue, alpha]: [number, number, number, number]): boolean {
  return alpha > 0 && red > green + 30 && red > blue + 30;
}

async function hoverHeatmapCell(page: Page, row: number, col: number): Promise<void> {
  // The selected-cell scatter overlay is intentionally silent and can cover
  // the underlying heatmap series. Move selection to the opposite FDC group
  // before hovering so Electron receives the real heatmap tooltip event.
  const selectionCell = `S${row + 1}D${col < 4 ? 5 : 1}`;
  const selected = await page.request.post(`${backendUrl}/api/selection`, { data: { cell: selectionCell } });
  expect(selected.ok(), await selected.text()).toBeTruthy();
  await expect
    .poll(async () => (await backendStatus(page)).selection.cells.includes(selectionCell), {
      timeout: 5_000,
      intervals: [50, 100, 200]
    })
    .toBe(true);
  await expect(page.locator(".trendPanel .panelHeader").first()).toContainText(col < 4 ? "D5-D8" : "D1-D4");
  const heatmap = page.locator(".heatmapCanvas");
  const box = await requiredBox(heatmap);
  const gridWidth = box.width - 64 - 28;
  const status = await backendStatus(page);
  const rows = Math.max(1, Math.min(8, status.frame.rows));
  const gridHeight = box.height - 28 - 72;
  const x = box.x + 64 + ((col + 0.5) * gridWidth) / 8;
  const y = box.y + 28 + ((row + 0.5) * gridHeight) / rows;
  await page.mouse.move(x, y);
}

async function hoverInvalidHeatmapCell(page: Page, row: number, col: number): Promise<void> {
  const cell = `S${row + 1}D${col + 1}`;
  const target = page.getByRole("button", { name: `${cell} measurement diagnostics`, exact: true });
  await expect(target).toBeAttached();
  await target.hover();
}

async function requiredBox(locator: ReturnType<Page["locator"]>): Promise<{ x: number; y: number; width: number; height: number }> {
  const box = await locator.boundingBox();
  if (!box) {
    throw new Error(`element has no visible bounding box: ${await locator.evaluate((element) => element.className)}`);
  }
  return box;
}

async function dragSplitter(page: Page, splitter: ReturnType<Page["locator"]>, deltaX: number): Promise<void> {
  const box = await requiredBox(splitter);
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + deltaX, y, { steps: 5 });
  await page.mouse.up();
}

async function saveScreenshot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: path.join(screenshotRoot, name) });
}

function watchFatalErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") {
      errors.push(`console: ${message.text()}`);
    }
  });
  return errors;
}
