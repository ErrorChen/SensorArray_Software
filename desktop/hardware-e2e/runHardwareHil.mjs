import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { _electron as electron } from "@playwright/test";

let backendUrl = "";
const serialPort = process.env.SENSORARRAY_HIL_SERIAL_PORT || "COM12";
const minimumRunMs = Number(process.env.SENSORARRAY_HIL_RUN_MS || 120_000);
const switchingCycles = Number(process.env.SENSORARRAY_HIL_SWITCH_CYCLES || 10);
if (!Number.isFinite(minimumRunMs) || minimumRunMs < 120_000) {
  throw new Error(`Hardware stability evidence requires at least 120000 ms, got ${minimumRunMs}`);
}
if (!Number.isInteger(switchingCycles) || switchingCycles < 10) {
  throw new Error(`Hardware switching evidence requires at least 10 cycles, got ${switchingCycles}`);
}
const requestedPhase = process.argv.find((argument) => argument.startsWith("--phase="))?.slice("--phase=".length) || "all";
if (!new Set(["all", "mixed"]).has(requestedPhase)) {
  throw new Error(`Unsupported hardware HIL phase: ${requestedPhase}`);
}
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const desktopRoot = path.join(repoRoot, "desktop");
const artifactRoot = path.join(repoRoot, "validation_artifacts", "hardware");

mkdirSync(artifactRoot, { recursive: true });

const report = {
  schemaVersion: 1,
  startedAt: new Date().toISOString(),
  application: "Electron desktop + Python sidecar",
  renderer: "local built renderer (no browser/Vite URL)",
  serialPort,
  minimumRunMs,
  switchingCycles,
  requestedPhase,
  headedElectron: true,
  voltage: {
    status: "NOT_RUN",
    reason: "Firmware internal rail telemetry is authoritative; the production GUI has no AVDD/AVSS input dependency."
  },
  rail: { status: "NOT_RUN", observations: [], errors: [] },
  battery: { status: "NOT_RUN", observations: [], errors: [] },
  stability: { status: "NOT_RUN", observations: [], errors: [] },
  switching: { status: "NOT_RUN", observations: [], errors: [] },
  mixed: { status: "NOT_RUN", observations: [], errors: [] },
  serial: { status: "NOT_RUN", observations: [], errors: [] },
  ble: { status: "NOT_RUN", observations: [], errors: [] },
  wifi: { status: "NOT_RUN", observations: [], errors: [] },
  electron: { preloadBridgePresent: false, backendUrl: "", consoleErrors: [], pageErrors: [] }
};

const environment = {};
for (const [key, value] of Object.entries(process.env)) {
  if (typeof value === "string") {
    environment[key] = value;
  }
}
delete environment.ELECTRON_RUN_AS_NODE;
delete environment.SENSORARRAY_FRONTEND_URL;

const electronApp = await electron.launch({
  args: [path.join(desktopRoot, "dist-electron", "main.js")],
  cwd: desktopRoot,
  env: environment
});
const page = await electronApp.firstWindow();

page.on("console", (message) => {
  if (message.type() === "error") {
    report.electron.consoleErrors.push(message.text());
  }
});
page.on("pageerror", (error) => report.electron.pageErrors.push(error.message));

try {
  await openGui(page);
  if (requestedPhase === "all") {
    await runSerialAcceptance(page);
  }
  await runBleAcceptance(page);
  if (requestedPhase === "all") {
    await runWifiSmoke(page);
  }
} catch (error) {
  report.fatalError = errorText(error);
} finally {
  report.finishedAt = new Date().toISOString();
  const finalStatus = await safeStatus(page);
  if (finalStatus) {
    report.finalSnapshot = compactSnapshot(finalStatus);
  }
  const reportName = requestedPhase === "all" ? "hardware_gui_hil.json" : `hardware_gui_hil_${requestedPhase}.json`;
  writeFileSync(path.join(artifactRoot, reportName), JSON.stringify(report, null, 2), "utf8");
  await electronApp.close();
}

if (report.fatalError || report.serial.status === "FAIL" || report.ble.status === "FAIL") {
  process.exitCode = 1;
}

async function openGui(currentPage) {
  await currentPage.getByTestId("measurement-mode-control").waitFor({ state: "visible", timeout: 30_000 });
  const rendererUrl = currentPage.url();
  report.electron.rendererUrl = rendererUrl;
  if (!rendererUrl.startsWith("file:")) {
    throw new Error(`Hardware HIL requires Electron's local built renderer, got ${rendererUrl}`);
  }
  const bridge = await currentPage.evaluate(async () => {
    const desktopBridge = globalThis.sensorarrayDesktop;
    return {
      present: Boolean(desktopBridge),
      backendUrl: await desktopBridge?.getBackendUrl?.(),
      runtimeDirectory: await desktopBridge?.getRuntimeDirectory?.()
    };
  });
  if (!bridge.present || typeof bridge.backendUrl !== "string" || !/^http:\/\/127\.0\.0\.1:\d+$/.test(bridge.backendUrl)) {
    throw new Error(`Electron preload/backend bridge is unavailable: ${JSON.stringify(bridge)}`);
  }
  backendUrl = bridge.backendUrl;
  report.backendUrl = backendUrl;
  report.electron.preloadBridgePresent = true;
  report.electron.backendUrl = backendUrl;
  report.electron.runtimeDirectory = bridge.runtimeDirectory || "";
  const health = await currentPage.request.get(`${backendUrl}/health`);
  if (!health.ok()) {
    throw new Error(`Backend health failed: ${health.status()} ${await health.text()}`);
  }
  await currentPage.locator(".socketState").waitFor({ state: "visible", timeout: 20_000 });
  await poll(async () => (await currentPage.locator(".socketState").textContent())?.trim() === "connected", 20_000, "WebSocket connected");
  await currentPage.getByRole("tab", { name: "Setup" }).click();
  await screenshot(currentPage, "HIL_00_gui_boot.png");
}

async function runSerialAcceptance(currentPage) {
  const result = report.serial;
  try {
    await clickTransport(currentPage, "Serial");
    await currentPage.getByTitle("Refresh ports").click();
    await poll(async () => {
      const text = await currentPage.locator(".scanState").first().textContent();
      return Boolean(text && !text.includes("Scanning"));
    }, 15_000, "serial port refresh");
    const discoveredPorts = await currentPage.locator("#serial-port-options option").evaluateAll((options) =>
      options.map((option) => option.getAttribute("value") || "").filter(Boolean)
    );
    result.observations.push({ action: "serialPortDiscovery", ports: discoveredPorts });
    if (!discoveredPorts.some((port) => port.toUpperCase() === serialPort.toUpperCase())) {
      result.status = "BLOCKED";
      result.errors.push(`GUI serial refresh did not find ${serialPort}; no missing device was counted as a hardware failure or PASS.`);
      await screenshot(currentPage, "HIL_S00_serial_port_missing.png");
      return;
    }
    const portInput = currentPage.getByPlaceholder("Enter or select a serial port");
    await portInput.fill(serialPort);
    const baudInput = currentPage.locator(".modePanel input[type='number']").first();
    await baudInput.fill("115200");
    result.observations.push({ action: "guiSerialSelection", port: await portInput.inputValue(), baud: Number(await baudInput.inputValue()) });

    const diagnosticsBefore = (await backendStatus(currentPage)).diagnostics;
    await currentPage.getByRole("button", { name: "Connect", exact: true }).click();
    let snapshot = await waitForSnapshot(
      currentPage,
      (candidate) => ["connected", "streaming"].includes(candidate.connection.state),
      20_000,
      "serial connection"
    );
    const firstGeneration = snapshot.connection.generation;
    snapshot = await waitForSnapshot(currentPage, isCompleteFreshCapAnyRows, 40_000, "complete fresh CAP frame after serial attach");
    if (snapshot.frame.rows !== 8) {
      await selectRows(currentPage, 8);
      snapshot = await waitForSnapshot(currentPage, isCompleteFreshCap, 30_000, "ROWS=8 CAP setup for serial HIL");
    }
    result.observations.push({ action: "connected", snapshot: compactSnapshot(snapshot), cellHealth: cellHealth(snapshot) });
    result.observations.push(await exerciseCellInspection(currentPage, "CAP", 0, 4));
    await screenshot(currentPage, "HIL_S01_serial_cap_initial.png");

    result.observations.push(await observeSustainedRun(currentPage, "serial CAP initial", minimumRunMs, isCompleteFreshCap));
    result.observations.push(await exerciseRawLogAndStatus(currentPage, "serial"));

    for (const rows of [1, 2, 3, 4, 5, 6, 7, 8]) {
      await selectRows(currentPage, rows);
      let rowSnapshot = await waitForSnapshot(
        currentPage,
        (candidate) => candidate.frame.rows === rows && activeCellsFresh(candidate) === rows * 8,
        25_000,
        `ROWS=${rows} fresh frame`
      );
      if (rowSnapshot.commands?.pendingRows !== null && rowSnapshot.commands?.pendingRows !== undefined) {
        rowSnapshot = await waitForSnapshot(
          currentPage,
          (candidate) =>
            candidate.frame.rows === rows &&
            activeCellsFresh(candidate) === rows * 8 &&
            (candidate.commands?.pendingRows === null || candidate.commands?.pendingRows === undefined),
          10_000,
          `ROWS=${rows} RAPP transaction completion`
        );
      }
      const commandState = rowSnapshot.commands || {};
      result.observations.push({ action: "rows", rows, snapshot: compactSnapshot(rowSnapshot), cellHealth: cellHealth(rowSnapshot), commands: commandState });
    }
    await screenshot(currentPage, "HIL_S02_serial_rows_1_through_8.png");

    const modeQuery = await sendGuiCommand(currentPage, "MODE?", "MODE");
    const stateQuery = await sendGuiCommand(currentPage, "STATE?", "MODE");
    result.observations.push({ action: "commandPanel", modeQuery, stateQuery });

    const transitionStart = Date.now();
    await currentPage.getByTestId("measurement-mode-control").getByRole("button", { name: "RES", exact: true }).click();
    const resSnapshot = await waitForSnapshot(
      currentPage,
      (candidate) =>
        candidate.measurement.appliedMode === "RES" &&
        candidate.matrix.quantity === "resistance" &&
        candidate.frame.rows === 8 &&
        candidate.frame.valid &&
        activeCellsFresh(candidate) === 64,
      30_000,
      "serial RES MAPP and fully fresh frame"
    );
    const transactionLogs = logsSince(resSnapshot, transitionStart, ["MACK", "MAPP"]);
    if (!transactionLogs.some((row) => row.tag === "MACK") || !transactionLogs.some((row) => row.tag === "MAPP")) {
      throw new Error(`Serial RES transaction did not expose both MACK and MAPP: ${JSON.stringify(transactionLogs)}`);
    }
    result.observations.push({ action: "modeToRes", transitionLogs: transactionLogs, snapshot: compactSnapshot(resSnapshot), cellHealth: cellHealth(resSnapshot) });
    result.observations.push(await exerciseCellInspection(currentPage, "RES", 0, 4));
    await screenshot(currentPage, "HIL_S03_serial_res.png");
    result.observations.push(
      await observeSustainedRun(
        currentPage,
        "serial RES",
        minimumRunMs,
        (candidate) => candidate.measurement.appliedMode === "RES" && candidate.matrix.quantity === "resistance" && activeCellsFresh(candidate) === 64
      )
    );

    const returnStart = Date.now();
    await currentPage.getByTestId("measurement-mode-control").getByRole("button", { name: "CAP", exact: true }).click();
    const capReturn = await waitForSnapshot(currentPage, isCompleteFreshCap, 30_000, "serial CAP return MAPP and full frame");
    const returnLogs = logsSince(capReturn, returnStart, ["MACK", "MAPP"]);
    if (!returnLogs.some((row) => row.tag === "MACK") || !returnLogs.some((row) => row.tag === "MAPP")) {
      throw new Error(`Serial CAP return did not expose both MACK and MAPP: ${JSON.stringify(returnLogs)}`);
    }
    result.observations.push({ action: "modeReturnCap", transitionLogs: returnLogs, snapshot: compactSnapshot(capReturn), cellHealth: cellHealth(capReturn) });
    await screenshot(currentPage, "HIL_S04_serial_cap_return.png");
    result.observations.push(await observeSustainedRun(currentPage, "serial CAP return", minimumRunMs, isCompleteFreshCap));

    await currentPage.getByRole("button", { name: "Disconnect", exact: true }).click();
    await waitForSnapshot(currentPage, (candidate) => candidate.connection.state === "disconnected", 15_000, "serial disconnect");
    await currentPage.getByRole("button", { name: "Connect", exact: true }).click();
    const reconnected = await waitForSnapshot(
      currentPage,
      (candidate) => isCompleteFreshCap(candidate) && candidate.connection.generation > firstGeneration,
      40_000,
      "serial reconnect with new generation"
    );
    result.observations.push({ action: "reconnect", firstGeneration, newGeneration: reconnected.connection.generation, snapshot: compactSnapshot(reconnected) });
    await screenshot(currentPage, "HIL_S05_serial_reconnect.png");
    await wait(5_000);

    const finalSnapshot = await backendStatus(currentPage);
    const diagnosticsAfter = finalSnapshot.diagnostics;
    const crcDelta = numeric(diagnosticsAfter.crcFailures) - numeric(diagnosticsBefore.crcFailures);
    const parserRejectDelta = numeric(diagnosticsAfter.parserRejects) - numeric(diagnosticsBefore.parserRejects);
    result.observations.push({ action: "diagnostics", crcDelta, parserRejectDelta, before: diagnosticsBefore, after: diagnosticsAfter });
    result.observations.push(assertNoParserCorruption(diagnosticsBefore, diagnosticsAfter, "Serial acceptance"));
    if (report.electron.consoleErrors.length || report.electron.pageErrors.length) {
      throw new Error(`GUI errors observed: ${JSON.stringify(report.electron)}`);
    }
    result.status = "PASS";
  } catch (error) {
    result.status = "FAIL";
    result.errors.push(errorText(error));
    await probeSerialWithoutCap(currentPage, result);
    result.failureEvidence = await collectFailureEvidence(currentPage, "serial", error);
    await screenshot(currentPage, "HIL_S99_serial_failure.png");
  } finally {
    await restoreCapIfPossible(currentPage);
    await disconnectIfConnected(currentPage);
  }
}

async function runBleAcceptance(currentPage) {
  const result = report.ble;
  let stabilityAttempted = false;
  let voltageAttempted = false;
  try {
    await clickTransport(currentPage, "Bluetooth LE");
    const scanStartedAt = Date.now();
    await currentPage.getByRole("button", { name: "Scan", exact: true }).click();
    await poll(async () => {
      const label = await currentPage.getByRole("button", { name: /Scan/ }).textContent();
      return !label?.includes("Scanning");
    }, 30_000, "BLE GUI scan");
    const scanSnapshot = await backendStatus(currentPage);
    result.observations.push({ action: "scan", durationMs: Date.now() - scanStartedAt, state: scanSnapshot.discovery.bleState, devices: scanSnapshot.discovery.bleResults });
    await screenshot(currentPage, "HIL_B01_ble_scan.png");

    const candidates = scanSnapshot.discovery.bleResults.filter((device) => device.address && (device.verified || /^CscArray_/i.test(device.name || "")));
    if (!candidates.length) {
      result.status = "BLOCKED";
      result.errors.push("BLE adapter scan completed through the GUI, but no verified/name-matched SensorArray device was visible.");
      return;
    }
    const selected = candidates.find((device) => device.verified) || candidates[0];
    const deviceSelect = currentPage.locator(".modePanel select").first();
    if ((await deviceSelect.locator(`option[value="${cssEscape(selected.address)}"]`).count()) === 0) {
      const advanced = currentPage.getByLabel("Advanced devices");
      if (await advanced.count()) {
        await advanced.check();
      }
    }
    await deviceSelect.selectOption(selected.address);
    result.observations.push({ action: "selectedDevice", device: selected });
    await currentPage.getByRole("button", { name: "Connect", exact: true }).click();
    const connected = await waitForSnapshot(
      currentPage,
      (candidate) => candidate.connection.mode === "ble" && candidate.connection.state === "streaming",
      35_000,
      "BLE streaming"
    );
    const firstGeneration = connected.connection.generation;
    let initialCap = await waitForSnapshot(currentPage, isCompleteFreshCapAnyRows, 45_000, "BLE complete fresh CAP frame");
    if (initialCap.frame.rows !== 8) {
      await selectRows(currentPage, 8);
      initialCap = await waitForSnapshot(currentPage, isCompleteFreshCap, 30_000, "ROWS=8 CAP setup for BLE HIL");
    }
    result.observations.push({ action: "connected", snapshot: compactSnapshot(initialCap), cellHealth: cellHealth(initialCap), gatt: recentLogs(initialCap, ["Transport"], 8) });
    result.observations.push(await exerciseCellInspection(currentPage, "CAP", 0, 4));
    await screenshot(currentPage, "HIL_B02_ble_cap.png");
    if (requestedPhase === "mixed") {
      result.observations.push({ action: "phase", phase: "mixed", note: "CAP attach is only the prerequisite for an independent mixed/profile HIL run." });
      await runMixedAcceptance(currentPage, result);
      const finalMixedPhase = await backendStatus(currentPage);
      result.observations.push(
        assertNoParserCorruption(initialCap.diagnostics, finalMixedPhase.diagnostics, "BLE mixed phase")
      );
      result.status = "PASS";
      return;
    }
    stabilityAttempted = true;
    const capStability = await observeSustainedRun(currentPage, "BLE CAP", minimumRunMs, isCompleteFreshCap);
    result.observations.push(capStability);
    report.stability.observations.push(capStability);
    result.observations.push(await exerciseRawLogAndStatus(currentPage, "ble"));

    const stateResponse = await sendGuiCommand(currentPage, "STATE?", "MODE");
    result.observations.push({ action: "ff10Ff11StateQuery", response: stateResponse });

    for (const rows of [1, 8]) {
      await selectRows(currentPage, rows);
      const rowFrame = await waitForSnapshot(
        currentPage,
        (candidate) => candidate.frame.rows === rows && activeCellsFresh(candidate) === rows * 8,
        30_000,
        `BLE ROWS=${rows} physical frame`
      );
      result.observations.push({ action: "bleRows", rows, snapshot: compactSnapshot(rowFrame), commands: rowFrame.commands, cellHealth: cellHealth(rowFrame) });
    }
    await screenshot(currentPage, "HIL_B03_ble_rows.png");

    // The current firmware contract publishes MACK over FF11 (ctrl) and the
    // terminal MAPP over FF30 (log). Data frames never complete a transaction.
    const resTransition = await setGlobalMode(currentPage, "RES");
    result.observations.push(resTransition);
    await screenshot(currentPage, "HIL_B04_ble_res_applied.png");
    const resStability = await observeSustainedRun(currentPage, "BLE RES", minimumRunMs, (candidate) => isCompleteFreshMode(candidate, "RES"));
    result.observations.push(resStability);
    report.stability.observations.push(resStability);

    const capReturn = await setGlobalMode(currentPage, "CAP");
    result.observations.push(capReturn);
    await screenshot(currentPage, "HIL_B05_ble_cap_return.png");

    voltageAttempted = true;
    const voltTransition = await setGlobalMode(currentPage, "VOLT");
    result.observations.push(voltTransition);
    const voltSnapshot = await backendStatus(currentPage);
    if (await currentPage.getByLabel("Measured AVDD to GND").count() || await currentPage.getByLabel("Measured AVSS to GND").count()) {
      throw new Error("Production GUI still exposed AVDD/AVSS inputs during BLE VOLT acceptance");
    }
    const rail = voltSnapshot.measurement?.railTelemetry;
    if (!rail?.valid || rail.fresh !== true || typeof rail.railSpanUv !== "number" || rail.source !== "internal_monitor") {
      report.rail.status = "FAIL";
      report.rail.errors.push(`Internal read-only rail telemetry unavailable in VOLT: ${JSON.stringify(rail)}`);
      throw new Error(report.rail.errors.at(-1));
    }
    report.rail.status = "PASS";
    report.rail.observations.push({ snapshot: rail, avddInputs: 0, avssInputs: 0 });
    await screenshot(currentPage, "HIL_B06_ble_volt_rail_readonly.png");
    const voltStability = await observeSustainedRun(currentPage, "BLE VOLT", minimumRunMs, (candidate) => isCompleteFreshMode(candidate, "VOLT"));
    result.observations.push(voltStability);
    report.stability.observations.push(voltStability);
    report.voltage.status = "PASS";

    await runMixedAcceptance(currentPage, result);
    report.stability.status = "PASS";

    await currentPage.getByRole("button", { name: "Disconnect", exact: true }).click();
    await waitForSnapshot(currentPage, (candidate) => candidate.connection.state === "disconnected", 20_000, "BLE disconnect");
    await currentPage.getByRole("button", { name: "Connect", exact: true }).click();
    const reconnected = await waitForSnapshot(
      currentPage,
      (candidate) => candidate.connection.mode === "ble" && candidate.connection.state === "streaming" && candidate.connection.generation > firstGeneration,
      40_000,
      "BLE reconnect"
    );
    const reconnectedCap = await waitForSnapshot(currentPage, isCompleteFreshCap, 45_000, "BLE CAP after reconnect");
    await wait(5_000);
    const finalBle = await backendStatus(currentPage);
    result.observations.push(
      assertNoParserCorruption(initialCap.diagnostics, finalBle.diagnostics, "BLE full acceptance")
    );
    result.observations.push({
      action: "reconnect",
      firstGeneration,
      newGeneration: reconnected.connection.generation,
      snapshot: compactSnapshot(reconnectedCap),
      diagnostics: finalBle.diagnostics,
      transportEvidence: recentLogs(finalBle, ["Transport", "BLE_RX50", "BLE_FRAG50", "PROTO50"], 30)
    });
    await screenshot(currentPage, "HIL_B06_ble_reconnect.png");

    if (result.status === "NOT_RUN") {
      result.status = "PASS";
    }
  } catch (error) {
    if (voltageAttempted && report.voltage.status !== "PASS") {
      report.voltage.status = "FAIL";
      report.voltage.reason = `VOLT/internal-monitor acceptance did not complete: ${errorText(error)}`;
    }
    if (voltageAttempted && report.rail.status === "NOT_RUN") {
      report.rail.status = "FAIL";
      report.rail.errors.push(`Internal rail acceptance did not complete after MODE=VOLT was attempted: ${errorText(error)}`);
    }
    if (report.stability.status === "NOT_RUN" && stabilityAttempted) {
      report.stability.status = "FAIL";
      report.stability.errors.push(
        `${report.stability.observations.length ? "Only part of" : "The first state in"} the required BLE stability matrix completed: ${errorText(error)}`
      );
    }
    result.status = result.observations.length ? "FAIL" : "BLOCKED";
    result.errors.push(errorText(error));
    result.failureEvidence = await collectFailureEvidence(currentPage, "ble", error);
    await screenshot(currentPage, "HIL_B99_ble_failure.png");
  } finally {
    await disconnectIfConnected(currentPage);
  }
}

async function runMixedAcceptance(currentPage, result) {
  let mixedCoreComplete = false;
  let mixedStabilityStarted = false;
  let switchingStarted = false;
  let batteryEvaluated = false;
  const switchResults = [];
  try {
    const mixedTransition = await applyRowProfile(currentPage, "RVVCCVVR");
    result.observations.push(mixedTransition);
    report.mixed.observations.push(mixedTransition);
    await screenshot(currentPage, "HIL_B07_ble_mixed_rvvccvvr.png");
    mixedStabilityStarted = true;
    const mixedStability = await observeSustainedRun(
      currentPage,
      "BLE mixed RVVCCVVR",
      minimumRunMs,
      (candidate) => isCompleteFreshMixed(candidate, "RVVCCVVR")
    );
    result.observations.push(mixedStability);
    report.mixed.observations.push(mixedStability);
    report.stability.observations.push(mixedStability);
    // The long-run gate is independent from the subsequent transaction stress.
    // Preserve completed 120-second evidence if a later profile switch fails.
    report.stability.status = "PASS";

    const secondMixed = await applyRowProfile(currentPage, "CRVCRVCR");
    result.observations.push(secondMixed);
    report.mixed.observations.push(secondMixed);
    await screenshot(currentPage, "HIL_B08_ble_mixed_crvcrvcr.png");
    mixedCoreComplete = true;
    report.mixed.status = "PASS";

    switchingStarted = true;
    for (let cycle = 1; cycle <= switchingCycles; cycle += 1) {
      for (const mode of ["CAP", "RES", "CAP", "VOLT", "CAP"]) {
        switchResults.push({ cycle, ...(await setGlobalMode(currentPage, mode)) });
      }
      // The required global sequence ends at CAP. Move to RES so the first
      // CCCCCCCC profile is a real GUI Apply, not an unchanged disabled draft.
      switchResults.push({ cycle, precondition: "rowProfileStress", ...(await setGlobalMode(currentPage, "RES")) });
      for (const profile of ["CCCCCCCC", "RVVCCVVR", "VVVVVVVV", "CRVCRVCR", "RRRRRRRR"]) {
        switchResults.push({ cycle, ...(await applyRowProfile(currentPage, profile)) });
      }
    }
    report.switching.status = "PASS";
    report.switching.observations = switchResults;
    result.observations.push({ action: "switchingStress", cycles: switchingCycles, transactions: switchResults.length });
    await evaluateBatteryLastGood(currentPage);
    batteryEvaluated = true;
  } catch (error) {
    if (mixedStabilityStarted && report.stability.status !== "PASS") {
      report.stability.status = "FAIL";
      report.stability.errors.push(errorText(error));
    }
    if (!mixedCoreComplete) {
      report.mixed.status = report.mixed.observations.length ? "FAIL" : "BLOCKED";
      report.mixed.errors.push(errorText(error));
    }
    if (switchingStarted) {
      report.switching.status = "FAIL";
      report.switching.observations = switchResults;
      report.switching.errors.push(errorText(error));
    }
    if (!batteryEvaluated && report.battery.status === "NOT_RUN") {
      report.battery.errors.push("Battery last-good acceptance was not reached because an earlier mixed/switching gate failed.");
    }
    throw error;
  }
}

async function runWifiSmoke(currentPage) {
  const result = report.wifi;
  try {
    await clickTransport(currentPage, "Wi-Fi UDP");
    await currentPage.getByRole("button", { name: "Discover", exact: true }).click();
    await poll(async () => {
      const label = await currentPage.getByRole("button", { name: /Discover/ }).textContent();
      return !label?.includes("Discovering");
    }, 20_000, "Wi-Fi GUI discovery");
    const snapshot = await backendStatus(currentPage);
    const devices = snapshot.discovery.wifiResults || [];
    result.observations.push({ action: "discover", state: snapshot.discovery.wifiState, devices });
    await screenshot(currentPage, "HIL_W01_wifi_discovery.png");
    if (!devices.some((device) => device.confirmed)) {
      result.status = "BLOCKED";
      result.errors.push("GUI Wi-Fi discovery found no confirmed SensorArray AP/UDP control endpoint; no unconfirmed fallback was counted as hardware PASS.");
      return;
    }
    result.status = "PASS";
  } catch (error) {
    result.status = "BLOCKED";
    result.errors.push(errorText(error));
    await screenshot(currentPage, "HIL_W99_wifi_failure.png");
  }
}

async function clickTransport(currentPage, accessibleName) {
  await currentPage.getByRole("tab", { name: "Setup" }).click();
  await currentPage.getByRole("button", { name: accessibleName, exact: true }).click();
  await wait(250);
}

async function selectRows(currentPage, rows) {
  const group = currentPage.locator(".controlGroup").filter({ has: currentPage.locator(".panelHeader", { hasText: "Rows" }) });
  await group.locator("select").selectOption(String(rows));
}

async function setGlobalMode(currentPage, mode) {
  const startedAt = Date.now();
  await currentPage.getByTestId("measurement-mode-control").getByRole("button", { name: mode, exact: true }).click();
  const snapshot = await waitForSnapshot(
    currentPage,
    (candidate) => isCompleteFreshMode(candidate, mode),
    45_000,
    `BLE ${mode} MAPP and complete fresh frame`
  );
  const logs = logsSince(snapshot, startedAt, ["MACK", "MAPP"]);
  const accepted = logs.find((row) => row.tag === "MACK");
  const applied = logs.find((row) => row.tag === "MAPP");
  if (!accepted || !applied || accepted.channel !== "ctrl" || applied.channel !== "log") {
    throw new Error(`BLE ${mode} requires FF11/ctrl MACK and FF30/log MAPP: ${JSON.stringify(logs)}`);
  }
  if (accepted.parsedFields?.new !== mode || applied.parsedFields?.new !== mode) {
    throw new Error(`BLE ${mode} MACK/MAPP payload mismatch: ${JSON.stringify(logs)}`);
  }
  const identity = {
    generation: snapshot.measurement?.generation,
    requestId: snapshot.measurement?.requestId,
    frameSeq: snapshot.measurement?.frameSeq
  };
  if (mode !== "CAP") {
    // V/R headers carry MODE gen/rid. Legacy C/D/K carries the independent
    // ROWS identity, so CAP proves MAPP only through its sequence boundary.
    identity.dataGeneration = snapshot.frame?.generation;
    identity.dataRequestId = snapshot.frame?.requestId;
  }
  assertAppliedIdentity(snapshot, accepted, applied, identity, `MODE=${mode}`);
  return { action: "globalMode", mode, accepted, applied, snapshot: compactSnapshot(snapshot) };
}

async function applyRowProfile(currentPage, profile) {
  const modes = [...profile].map((mode) => mode === "C" ? "CAP" : mode === "V" ? "VOLT" : "RES");
  const control = currentPage.getByTestId("row-mode-profile-control");
  const startedAt = Date.now();
  for (let row = 0; row < 8; row += 1) {
    await control.getByLabel(`S${row + 1} measurement mode`).selectOption(modes[row]);
  }
  await control.getByRole("button", { name: "Apply row modes", exact: true }).click();
  const snapshot = await waitForSnapshot(
    currentPage,
    (candidate) =>
      candidate.frame.layout === (new Set(modes).size > 1 ? "MIXED" : "HOMOGENEOUS") &&
      candidate.measurement?.rowProfile?.pendingModes === null &&
      candidate.measurement?.rowProfile?.appliedModes?.join("") === modes.join("") &&
      activeCellsFresh(candidate) === candidate.frame.rows * 8,
    45_000,
    `BLE ROWMODES=${profile} RMAPP and fresh frame`
  );
  const logs = logsSince(snapshot, startedAt, ["RMACK", "RMAPP"]);
  const accepted = logs.find((row) => row.tag === "RMACK");
  const applied = logs.find((row) => row.tag === "RMAPP");
  if (!accepted || !applied || accepted.channel !== "ctrl" || applied.channel !== "log") {
    throw new Error(`BLE ${profile} requires FF11/ctrl RMACK and FF30/log RMAPP: ${JSON.stringify(logs)}`);
  }
  if (accepted.parsedFields?.new !== profile || applied.parsedFields?.profile !== profile) {
    throw new Error(`BLE ${profile} RMACK/RMAPP payload mismatch: ${JSON.stringify(logs)}`);
  }
  assertAppliedIdentity(snapshot, accepted, applied, {
    generation: snapshot.measurement?.rowProfile?.generation,
    requestId: snapshot.measurement?.rowProfile?.requestId,
    frameSeq: snapshot.measurement?.rowProfile?.frameSeq,
    dataGeneration: snapshot.frame?.profileGeneration,
    dataRequestId: snapshot.frame?.profileRequestId
  }, `ROWMODES=${profile}`);
  return { action: "rowProfile", profile, accepted, applied, snapshot: compactSnapshot(snapshot) };
}

async function evaluateBatteryLastGood(currentPage) {
  let snapshot;
  try {
    snapshot = await waitForSnapshot(
      currentPage,
      (candidate) =>
        candidate.battery?.latestAttempt?.valid === true &&
        candidate.battery?.latestAttempt?.fresh === true &&
        typeof candidate.battery?.lastGood?.batteryMv === "number",
      30_000,
      "BLE first valid battery last-good"
    );
  } catch (error) {
    report.battery.status = "BLOCKED";
    report.battery.errors.push(`No valid battery telemetry observed: ${errorText(error)}`);
    return;
  }
  const firstMv = snapshot.battery.lastGood.batteryMv;
  report.battery.observations.push({ action: "firstValid", battery: snapshot.battery });
  try {
    const invalid = await waitForSnapshot(
      currentPage,
      (candidate) => candidate.battery?.latestAttempt?.valid === false,
      30_000,
      "BLE invalid/stale battery attempt"
    );
    if (invalid.battery?.lastGood?.batteryMv !== firstMv) {
      throw new Error(`Battery last-good changed after invalid attempt: ${firstMv} -> ${invalid.battery?.lastGood?.batteryMv}`);
    }
    const statusText = (await currentPage.locator(".statusItems").textContent()) || "";
    if (!statusText.includes((firstMv / 1000).toFixed(3)) || !statusText.includes("last known")) {
      throw new Error(`StatusBar did not retain last-good battery: ${statusText}`);
    }
    report.battery.status = "PASS";
    report.battery.observations.push({ action: "invalidSticky", battery: invalid.battery, statusText });
    await screenshot(currentPage, "HIL_B09_battery_stale.png");
  } catch (error) {
    report.battery.status = "BLOCKED";
    report.battery.errors.push(`Valid battery observed, but no later invalid attempt arrived in the bounded run: ${errorText(error)}`);
  }
}

async function sendGuiCommand(currentPage, command, expectedTag) {
  const sentAt = Date.now();
  const input = currentPage.getByPlaceholder("Enter command text");
  await input.fill(command);
  await currentPage.getByRole("button", { name: "Send", exact: true }).click();
  await currentPage.locator(".commandRecords").waitFor({ state: "visible" });
  const responseSnapshot = await waitForSnapshot(
    currentPage,
    (candidate) => logsSince(candidate, sentAt, [expectedTag]).length > 0,
    12_000,
    `${command} ${expectedTag} response`
  );
  return logsSince(responseSnapshot, sentAt, [expectedTag]).at(-1);
}

async function disconnectIfConnected(currentPage) {
  try {
    const snapshot = await backendStatus(currentPage);
    if (["connected", "streaming", "connecting", "reconnecting"].includes(snapshot.connection.state)) {
      const disconnect = currentPage.getByRole("button", { name: "Disconnect", exact: true });
      if (await disconnect.count()) {
        await disconnect.click({ timeout: 5_000 });
      } else {
        await currentPage.request.post(`${backendUrl}/api/transport/disconnect`, { data: {} });
      }
      await waitForSnapshot(currentPage, (candidate) => candidate.connection.state === "disconnected", 15_000, "disconnect cleanup");
    }
  } catch {
    // Best-effort cleanup. The concrete failure was already captured above.
  }
}

async function restoreCapIfPossible(currentPage) {
  try {
    const snapshot = await backendStatus(currentPage);
    if (
      !["connected", "streaming"].includes(snapshot.connection.state) ||
      (snapshot.measurement.appliedMode === "CAP" && snapshot.matrix.quantity === "capacitance")
    ) {
      return;
    }
    const button = currentPage.getByTestId("measurement-mode-control").getByRole("button", { name: "CAP", exact: true });
    if (await button.isEnabled()) {
      await button.click();
      await waitForSnapshot(currentPage, isCompleteFreshCap, 30_000, "best-effort CAP restore");
    }
  } catch {
    // The primary HIL error and final snapshot retain the failure evidence.
  }
}

async function probeSerialWithoutCap(currentPage, result) {
  const snapshot = await safeStatus(currentPage);
  if (
    !snapshot ||
    snapshot.connection.mode !== "serial" ||
    !["connected", "streaming"].includes(snapshot.connection.state) ||
    isCompleteFreshCap(snapshot)
  ) {
    return;
  }

  const probe = {
    action: "serialNoCapCommandProbe",
    reason: "Serial connected but no complete fresh CAP frame was available; MODE?/STATE? were attempted through the GUI Command panel.",
    modeQuery: null,
    stateQuery: null,
    errors: []
  };
  for (const [field, command] of [["modeQuery", "MODE?"], ["stateQuery", "STATE?"]]) {
    try {
      probe[field] = await sendGuiCommand(currentPage, command, "MODE");
    } catch (error) {
      probe.errors.push({ command, error: errorText(error) });
    }
  }
  result.observations.push(probe);
}

async function exerciseCellInspection(currentPage, mode, row, col) {
  const heatmap = currentPage.locator(".heatmapCanvas");
  const box = await heatmap.boundingBox();
  if (!box) {
    throw new Error(`Cannot inspect ${mode} cell because the heatmap has no visible bounding box`);
  }
  const gridWidth = box.width - 64 - 28;
  const snapshot = await backendStatus(currentPage);
  const gridHeight = box.height - 28 - 72;
  const x = box.x + 64 + ((col + 0.5) * gridWidth) / 8;
  const y = box.y + 28 + ((row + 0.5) * gridHeight) / Math.max(1, Math.min(8, snapshot.frame.rows));
  const cell = `S${row + 1}D${col + 1}`;
  await currentPage.mouse.move(x, y);
  await poll(async () => {
    const bodyText = (await currentPage.locator("body").textContent()) || "";
    const modeSpecificText = mode === "RES" ? "Raw integer m\u03A9" : mode === "VOLT" ? "Raw integer \u00B5V" : "Raw pF";
    return bodyText.includes(cell) && bodyText.includes(`Mode: ${mode}`) && bodyText.includes(modeSpecificText);
  }, 5_000, `${mode} ${cell} tooltip`);
  await currentPage.mouse.click(x, y);
  const selected = await waitForSnapshot(
    currentPage,
    (candidate) => Array.isArray(candidate.selection?.cells) && candidate.selection.cells.includes(cell),
    8_000,
    `${mode} ${cell} selection`
  );
  const trendHeader = ((await currentPage.locator(".trendPanel .panelHeader").first().textContent()) || "").trim();
  const expectedQuantity = mode === "RES" ? "Resistance" : "Capacitance";
  if (!trendHeader.includes(expectedQuantity)) {
    throw new Error(`${mode} trend header did not identify ${expectedQuantity}: ${trendHeader}`);
  }
  return {
    action: "cellInspection",
    mode,
    cell,
    selection: selected.selection,
    trendHeader,
    tooltipVerified: true
  };
}

async function exerciseRawLogAndStatus(currentPage, transport) {
  await currentPage.getByRole("button", { name: "Status", exact: true }).click();
  await currentPage.locator(".statusList").waitFor({ state: "visible", timeout: 5_000 });
  const statusCardCount = await currentPage.locator(".statusCard").count();
  const statusText = compactText(((await currentPage.locator(".statusList").textContent()) || "").trim(), 1_000);
  await currentPage.getByRole("button", { name: "Raw Log", exact: true }).click();
  const terminal = currentPage.locator(".logTerminal");
  await terminal.waitFor({ state: "visible", timeout: 5_000 });
  const rawLogText = ((await terminal.textContent()) || "").trim();
  if (!rawLogText) {
    throw new Error(`${transport} Raw Log was empty during connected hardware acceptance`);
  }
  return {
    action: "rawLogAndStatus",
    transport,
    statusCardCount,
    statusText,
    rawLogCharacters: rawLogText.length,
    rawLogTail: compactText(rawLogText.slice(-1_000), 1_000)
  };
}

async function collectFailureEvidence(currentPage, scope, error) {
  const snapshot = await safeStatus(currentPage);
  if (!snapshot) {
    return {
      scope,
      capturedAt: new Date().toISOString(),
      error: errorText(error),
      backendStatusAvailable: false
    };
  }

  const rows = Array.isArray(snapshot.logs?.rows) ? snapshot.logs.rows : [];
  const transportRows = rows.filter((row) =>
    row.tag === "Transport" ||
    row.source === "host" ||
    ["BLE_RX50", "BLE_FRAG50", "PROTO50", "FRAME_DROP", "PARSER"].includes(row.tag)
  );
  return {
    scope,
    capturedAt: new Date().toISOString(),
    error: errorText(error),
    backendStatusAvailable: true,
    connection: snapshot.connection,
    snapshot: compactSnapshot(snapshot),
    discovery: snapshot.discovery || {},
    diagnostics: {
      host: snapshot.diagnostics || {},
      matrix: snapshot.matrix?.diagnostics || {},
      rates: snapshot.rates || {}
    },
    logCounts: {
      totalVisible: rows.length,
      byTag: countBy(rows, (row) => row.tag || "UNKNOWN"),
      bySource: countBy(rows, (row) => row.source || "unknown"),
      byChannel: countBy(rows, (row) => row.channel || "unknown"),
      transportEvidenceByTag: countBy(transportRows, (row) => row.tag || "UNKNOWN")
    },
    payloadBoundary: {
      firstPayload: compactText(snapshot.matrix?.rawHeader),
      lastPayload: compactText(snapshot.matrix?.rawTrailer),
      firstRawText: boundaryLog(rows.at(0)),
      lastRawText: boundaryLog(rows.at(-1)),
      firstTransportRawText: boundaryLog(transportRows.at(0)),
      lastTransportRawText: boundaryLog(transportRows.at(-1)),
      currentFrameHeader: compactText(snapshot.matrix?.rawHeader),
      currentFrameTrailer: compactText(snapshot.matrix?.rawTrailer)
    }
  };
}

async function observeSustainedRun(currentPage, label, durationMs, predicate) {
  const startedAt = new Date().toISOString();
  const started = Date.now();
  const first = await backendStatus(currentPage);
  const revisions = new Set([first.frame.revision]);
  const sequences = new Set([first.frame.seq]);
  let minimumValid = activeCellsValid(first);
  let minimumFresh = activeCellsFresh(first);
  let last = first;
  const memorySamples = [await rendererMemorySample()];
  while (Date.now() - started < durationMs) {
    await wait(Math.min(1_000, durationMs - (Date.now() - started)));
    last = await backendStatus(currentPage);
    if (!predicate(last)) {
      throw new Error(`${label} violated sustained-run predicate at ${new Date().toISOString()}: ${JSON.stringify(compactSnapshot(last))}`);
    }
    revisions.add(last.frame.revision);
    sequences.add(last.frame.seq);
    minimumValid = Math.min(minimumValid, activeCellsValid(last));
    minimumFresh = Math.min(minimumFresh, activeCellsFresh(last));
    memorySamples.push(await rendererMemorySample());
  }
  if (last.frame.revision <= first.frame.revision || sequences.size < 2) {
    throw new Error(`${label} did not continuously update frames`);
  }
  const parserEvidence = assertNoParserCorruption(first.diagnostics, last.diagnostics, label);
  const validMemory = memorySamples.filter(
    (sample) => sample !== null && Number.isFinite(Number(sample.workingSetSize))
  );
  if (validMemory.length < 2) {
    throw new Error(`${label} could not obtain two renderer working-set samples`);
  }
  const memoryEvidence = validMemory.length
    ? {
        startWorkingSetKb: validMemory[0].workingSetSize,
        endWorkingSetKb: validMemory.at(-1).workingSetSize,
        maximumWorkingSetKb: Math.max(...validMemory.map((sample) => sample.workingSetSize)),
        growthKb: validMemory.at(-1).workingSetSize - validMemory[0].workingSetSize,
        samples: validMemory.length
      }
    : { unavailable: true };
  // A 256 MiB increase over one two-minute window is conservative enough to
  // tolerate ECharts/V8 warm-up while still detecting obvious runaway churn.
  if (!("unavailable" in memoryEvidence) && memoryEvidence.growthKb > 256 * 1024) {
    throw new Error(`${label} renderer working-set growth was unbounded: ${JSON.stringify(memoryEvidence)}`);
  }
  return {
    action: "sustainedRun",
    label,
    startedAt,
    finishedAt: new Date().toISOString(),
    elapsedMs: Date.now() - started,
    startRevision: first.frame.revision,
    endRevision: last.frame.revision,
    startSeq: first.frame.seq,
    endSeq: last.frame.seq,
    observedRevisionCount: revisions.size,
    observedSequenceCount: sequences.size,
    minimumValid,
    minimumFresh,
    memoryEvidence,
    parserEvidence,
    startDiagnostics: first.diagnostics,
    endDiagnostics: last.diagnostics
  };
}

async function rendererMemorySample() {
  try {
    return await electronApp.evaluate(async ({ app, BrowserWindow }) => {
      const rendererPid = BrowserWindow.getAllWindows()[0]?.webContents.getOSProcessId();
      if (!rendererPid) {
        return null;
      }
      return app.getAppMetrics().find((metric) => metric.pid === rendererPid)?.memory ?? null;
    });
  } catch {
    return null;
  }
}

function isCompleteFreshCap(snapshot) {
  return (
    snapshot.connection.mode === "serial" || snapshot.connection.mode === "ble"
  ) && ["connected", "streaming"].includes(snapshot.connection.state) && snapshot.measurement.appliedMode === "CAP" && snapshot.matrix.quantity === "capacitance" && snapshot.frame.rows === 8 && snapshot.frame.valid && activeCellsValid(snapshot) === 64 && activeCellsFresh(snapshot) === 64;
}

function isCompleteFreshCapAnyRows(snapshot) {
  const active = Math.max(1, Math.min(8, Number(snapshot.frame?.rows) || 0)) * 8;
  return (
    ["serial", "ble"].includes(snapshot.connection.mode) &&
    ["connected", "streaming"].includes(snapshot.connection.state) &&
    snapshot.measurement.appliedMode === "CAP" &&
    snapshot.matrix.quantity === "capacitance" &&
    snapshot.frame.valid &&
    activeCellsValid(snapshot) === active &&
    activeCellsFresh(snapshot) === active
  );
}

function isCompleteFreshMode(snapshot, mode) {
  const quantity = mode === "CAP" ? "capacitance" : mode === "VOLT" ? "voltage" : "resistance";
  // A fresh frame can legitimately contain per-cell measurement errors (for
  // example, an open RES channel).  Transaction/stability acceptance therefore
  // requires every active cell to have been attempted freshly, while cellHealth
  // records valid/error counts separately instead of misreporting a completed
  // MAPP as a timeout.
  return (
    ["serial", "ble"].includes(snapshot.connection.mode) &&
    ["connected", "streaming"].includes(snapshot.connection.state) &&
    snapshot.measurement.appliedMode === mode &&
    snapshot.measurement.pendingMode === null &&
    snapshot.matrix.quantity === quantity &&
    snapshot.frame.valid &&
    activeCellsFresh(snapshot) === snapshot.frame.rows * 8
  );
}

function isCompleteFreshMixed(snapshot, profile) {
  const modes = [...profile].map((mode) => mode === "C" ? "CAP" : mode === "V" ? "VOLT" : "RES");
  // Mixed frames use the same attempted-vs-valid distinction as homogeneous
  // V/R frames.  CRC/profile/identity checks are covered by frame.valid and the
  // authoritative profile metadata; invalid cells remain explicit diagnostics.
  return (
    snapshot.connection.mode === "ble" &&
    ["connected", "streaming"].includes(snapshot.connection.state) &&
    snapshot.frame.layout === "MIXED" &&
    snapshot.frame.rowModes?.join("") === modes.join("") &&
    snapshot.measurement?.rowProfile?.pendingModes === null &&
    snapshot.measurement?.rowProfile?.appliedModes?.join("") === modes.join("") &&
    snapshot.frame.valid &&
    activeCellsFresh(snapshot) === snapshot.frame.rows * 8
  );
}

function activeCellsValid(snapshot) {
  return snapshot.matrix.valid.slice(0, snapshot.frame.rows).flat().filter(Boolean).length;
}

function activeCellsFresh(snapshot) {
  return snapshot.matrix.fresh.slice(0, snapshot.frame.rows).flat().filter(Boolean).length;
}

function cellHealth(snapshot) {
  const active = snapshot.frame.rows * 8;
  return {
    active,
    valid: activeCellsValid(snapshot),
    fresh: activeCellsFresh(snapshot),
    errors: snapshot.matrix.error.slice(0, snapshot.frame.rows).flat().filter(Boolean).length,
    finite: snapshot.matrix.values.slice(0, snapshot.frame.rows).flat().filter((value) => typeof value === "number" && Number.isFinite(value)).length
  };
}

function compactSnapshot(snapshot) {
  return {
    connection: snapshot.connection,
    measurement: snapshot.measurement,
    frame: snapshot.frame,
    matrix: {
      quantity: snapshot.matrix.quantity,
      unit: snapshot.matrix.unit,
      wireUnit: snapshot.matrix.wireUnit,
      scale: snapshot.matrix.scale,
      sourceTransport: snapshot.matrix.sourceTransport,
      generation: snapshot.matrix.generation,
      requestId: snapshot.matrix.requestId,
      diagnostics: snapshot.matrix.diagnostics
    },
    cellHealth: cellHealth(snapshot),
    diagnostics: snapshot.diagnostics,
    rates: snapshot.rates
  };
}

function logsSince(snapshot, timeMs, tags) {
  const sinceSeconds = timeMs / 1000;
  return (snapshot.logs.rows || [])
    .filter((row) => row.timestamp >= sinceSeconds && tags.includes(row.tag))
    .map(compactLog);
}

function assertAppliedIdentity(snapshot, accepted, applied, identity, label) {
  const acceptedId = requiredLogInteger(accepted, "id", label);
  const appliedId = requiredLogInteger(applied, "id", label);
  const appliedGeneration = requiredLogInteger(applied, "gen", label);
  const appliedFrameSeq = requiredLogInteger(applied, "seq", label);
  if (acceptedId !== appliedId) {
    throw new Error(`${label} accepted/applied request ID mismatch: ${acceptedId} != ${appliedId}`);
  }
  const expected = {
    generation: appliedGeneration,
    requestId: appliedId,
    frameSeq: appliedFrameSeq
  };
  if (Object.hasOwn(identity, "dataGeneration")) {
    expected.dataGeneration = appliedGeneration;
  }
  if (Object.hasOwn(identity, "dataRequestId")) {
    expected.dataRequestId = appliedId;
  }
  for (const key of Object.keys(expected)) {
    if (Number(identity[key]) !== expected[key]) {
      throw new Error(`${label} ${key} mismatch: ${identity[key]} != ${expected[key]}`);
    }
  }
  if (Number(snapshot.frame?.seq) < appliedFrameSeq) {
    throw new Error(`${label} visible frame sequence ${snapshot.frame?.seq} precedes RM/MAPP seq ${appliedFrameSeq}`);
  }
}

function assertNoParserCorruption(before, after, label) {
  const crcDelta = numeric(after?.crcFailures) - numeric(before?.crcFailures);
  const parserRejectDelta = numeric(after?.parserRejects) - numeric(before?.parserRejects);
  const reasonNames = new Set([
    ...Object.keys(before?.rejectsByReason || {}),
    ...Object.keys(after?.rejectsByReason || {})
  ]);
  const rejectDeltas = {};
  for (const reason of reasonNames) {
    const delta = numeric(after?.rejectsByReason?.[reason]) - numeric(before?.rejectsByReason?.[reason]);
    if (delta > 0) {
      rejectDeltas[reason] = delta;
    }
  }
  // A final queued notification from the old worker may be deliberately
  // rejected while a tested disconnect/reconnect advances sessionGeneration.
  // That safety drop is not parser corruption; every protocol-level reject is.
  const disallowedRejects = Object.fromEntries(
    Object.entries(rejectDeltas).filter(([reason]) => reason !== "stale_session_generation")
  );
  if (crcDelta !== 0 || Object.keys(disallowedRejects).length) {
    throw new Error(
      `${label} parser corruption counters changed: crc=${crcDelta}, rejects=${JSON.stringify(disallowedRejects)}`
    );
  }
  return { action: "parserIntegrity", label, crcDelta, parserRejectDelta, rejectDeltas, disallowedRejects };
}

function requiredLogInteger(log, field, label) {
  const value = Number(log?.parsedFields?.[field]);
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(`${label} log lacks non-negative integer ${field}: ${JSON.stringify(log)}`);
  }
  return value;
}

function recentLogs(snapshot, tags, limit) {
  return (snapshot.logs.rows || []).filter((row) => tags.includes(row.tag)).slice(-limit).map(compactLog);
}

function compactLog(row) {
  return {
    timestamp: row.timestamp,
    source: row.source,
    channel: row.channel,
    tag: row.tag,
    severity: row.severity,
    rawText: row.rawText,
    parsedFields: row.parsedFields,
    recognised: row.recognised,
    sessionGeneration: row.sessionGeneration
  };
}

function countBy(rows, keyForRow) {
  const counts = {};
  for (const row of rows) {
    const key = String(keyForRow(row));
    counts[key] = (counts[key] || 0) + 1;
  }
  return counts;
}

function boundaryLog(row) {
  if (!row) {
    return null;
  }
  return {
    timestamp: row.timestamp,
    source: row.source,
    channel: row.channel,
    tag: row.tag,
    sessionGeneration: row.sessionGeneration,
    rawText: compactText(row.rawText)
  };
}

function compactText(value, maximumLength = 512) {
  if (typeof value !== "string" || !value) {
    return null;
  }
  return value.length <= maximumLength ? value : `${value.slice(0, maximumLength)}...`;
}

async function waitForSnapshot(currentPage, predicate, timeoutMs, label) {
  let last;
  await poll(async () => {
    last = await backendStatus(currentPage);
    return predicate(last);
  }, timeoutMs, label);
  return last;
}

async function backendStatus(currentPage) {
  const response = await currentPage.request.get(`${backendUrl}/api/status`);
  if (!response.ok()) {
    throw new Error(`GET /api/status failed: ${response.status()} ${await response.text()}`);
  }
  return response.json();
}

async function safeStatus(currentPage) {
  try {
    return await backendStatus(currentPage);
  } catch {
    return null;
  }
}

async function poll(action, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      if (await action()) {
        return;
      }
    } catch (error) {
      lastError = error;
    }
    await wait(200);
  }
  throw new Error(`Timed out waiting for ${label}${lastError ? `: ${errorText(lastError)}` : ""}`);
}

async function screenshot(currentPage, name) {
  try {
    await currentPage.screenshot({ path: path.join(artifactRoot, name) });
  } catch {
    // Evidence collection should not hide the primary hardware failure.
  }
}

function cssEscape(value) {
  return value.replaceAll("\\", "\\\\").replaceAll('"', '\\"');
}

function numeric(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

function errorText(error) {
  return error instanceof Error ? `${error.name}: ${error.message}` : String(error);
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, Math.max(0, milliseconds)));
}
