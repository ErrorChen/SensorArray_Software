import { execFile as execFileCallback } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";

import { _electron as electron } from "@playwright/test";

const execFile = promisify(execFileCallback);

let backendUrl = "";
const serialPort = process.env.SENSORARRAY_HIL_SERIAL_PORT || "COM12";
const minimumRunMs = Number(process.env.SENSORARRAY_HIL_RUN_MS || 120_000);
const enduranceRunMs = Number(process.env.SENSORARRAY_HIL_ENDURANCE_MS || 450_000);
const wireProbeMs = Number(process.env.SENSORARRAY_HIL_WIRE_PROBE_MS || 120_000);
const switchingCycles = Number(process.env.SENSORARRAY_HIL_SWITCH_CYCLES || 10);
if (!Number.isFinite(minimumRunMs) || minimumRunMs < 120_000) {
  throw new Error(`Hardware stability evidence requires at least 120000 ms, got ${minimumRunMs}`);
}
if (!Number.isFinite(enduranceRunMs) || enduranceRunMs < 450_000) {
  throw new Error(`Hardware endurance evidence requires at least 450000 ms (7.5 min), got ${enduranceRunMs}`);
}
if (!Number.isFinite(wireProbeMs) || wireProbeMs < 60_000) {
  throw new Error(`Hardware wire diagnostic requires at least 60000 ms, got ${wireProbeMs}`);
}
if (!Number.isInteger(switchingCycles) || switchingCycles < 10) {
  throw new Error(`Hardware switching evidence requires at least 10 cycles, got ${switchingCycles}`);
}
const requestedPhase = process.argv.find((argument) => argument.startsWith("--phase="))?.slice("--phase=".length) || "all";
if (!new Set(["all", "serial", "serial-wire", "serial-switching", "lifecycle", "gui-stress", "ble", "ble-reconnect", "mixed", "wifi"]).has(requestedPhase)) {
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
  firmwareValidationBaseline: "8045e9e9ec9599533c52c15dfcb6002f79fd15f1",
  firmwareIdentityCaveat: "BUILD? reports build metadata, not a cryptographic Git SHA; exact identity comes from the verified flash checkout/process.",
  renderer: "local built renderer (no browser/Vite URL)",
  serialPort,
  minimumRunMs,
  enduranceRunMs,
  wireProbeMs,
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
  resistanceFixture: { status: "NOT_RUN", observations: [], errors: [] },
  mixed: { status: "NOT_RUN", observations: [], errors: [] },
  recording: { status: "NOT_RUN", observations: [], errors: [] },
  deviceLifecycle: { status: "NOT_RUN", observations: [], errors: [] },
  fdcIsolation: { status: "NOT_RUN", observations: [], errors: [] },
  guiStress: { status: "NOT_RUN", observations: [], errors: [] },
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
  if (requestedPhase === "serial-wire") {
    await runSerialWireProbe(page);
  } else if (["all", "serial"].includes(requestedPhase)) {
    await runSerialAcceptance(page);
  } else if (requestedPhase === "lifecycle") {
    await runSerialLifecycleAcceptance(page);
  } else if (requestedPhase === "gui-stress") {
    await runGuiStressAcceptance(page);
  } else if (requestedPhase === "serial-switching") {
    await runSerialSwitchingAcceptance(page);
  }
  if (["all", "ble", "ble-reconnect", "mixed"].includes(requestedPhase)) {
    await runBleAcceptance(page);
  }
  if (["all", "wifi"].includes(requestedPhase)) {
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

if (
  report.fatalError ||
  report.serial.status === "FAIL" ||
  report.ble.status === "FAIL" ||
  report.deviceLifecycle.status === "FAIL" ||
  report.fdcIsolation.status === "FAIL" ||
  report.guiStress.status === "FAIL" ||
  report.switching.status === "FAIL" ||
  report.resistanceFixture.status === "FAIL"
) {
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
    snapshot = await waitForSnapshot(
      currentPage,
      (candidate) =>
        ["connected", "streaming"].includes(candidate.connection.state) &&
        candidate.measurement?.syncState === "synced" &&
        candidate.measurement?.appliedMode === "CAP" &&
        candidate.usbStream?.mode === "DEBUG" &&
        Number(candidate.usbStream?.dataEvery) > 1,
      90_000,
      "serial bootstrap and DEBUG stream state"
    );
    if (snapshot.usbStream?.mode !== "DEBUG" || Number(snapshot.usbStream?.dataEvery) <= 1) {
      throw new Error(`Serial default USB stream was not DEBUG/decimated: ${JSON.stringify(snapshot.usbStream)}`);
    }
    result.observations.push({ action: "connectedDebug", snapshot: compactSnapshot(snapshot), usbStream: snapshot.usbStream });
    const debugRun = await observeSparseDebugRun(currentPage, minimumRunMs);
    result.observations.push(debugRun);
    result.observations.push(await exerciseRawLogAndStatus(currentPage, "serial"));
    result.observations.push(await forceUncappedFirmwarePipeline(currentPage));

    const fullStream = await setUsbStreamViaGui(currentPage, "FULL");
    result.observations.push(fullStream);
    snapshot = await waitForSnapshot(currentPage, isCompleteFreshCapAnyRows, 60_000, "complete fresh CAP frame after USBSTREAM=FULL");
    if (snapshot.frame.rows !== 8) {
      await selectRows(currentPage, 8);
      snapshot = await waitForSnapshot(currentPage, isCompleteFreshCap, 30_000, "ROWS=8 CAP setup for serial HIL");
    }
    result.observations.push({ action: "connectedFull", snapshot: compactSnapshot(snapshot), cellHealth: cellHealth(snapshot) });
    result.observations.push(await exerciseCellInspection(currentPage, "CAP", 0, 4));
    await screenshot(currentPage, "HIL_S01_serial_cap_full.png");

    const fullRecording = await runFullRateRecording(currentPage);
    result.observations.push(fullRecording);
    report.recording.status = "PASS";
    report.recording.observations.push(fullRecording);

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

    const voltTransition = await setGlobalMode(currentPage, "VOLT");
    result.observations.push(voltTransition);
    result.observations.push(await exerciseCellInspection(currentPage, "VOLT", 0, 4));
    await screenshot(currentPage, "HIL_S04_serial_volt.png");
    result.observations.push(
      await observeSustainedRun(currentPage, "serial VOLT", minimumRunMs, (candidate) => isCompleteFreshMode(candidate, "VOLT"))
    );

    const mixedTransition = await applyRowProfile(currentPage, "RVRCCVVR");
    result.observations.push(mixedTransition);
    await screenshot(currentPage, "HIL_S05_serial_mixed_rvrccvvr.png");
    const serialMixedRun = await observeSustainedRun(
      currentPage,
      "serial mixed RVRCCVVR",
      minimumRunMs,
      (candidate) => isCompleteFreshMixed(candidate, "RVRCCVVR")
    );
    result.observations.push(serialMixedRun);
    report.mixed.observations.push({ transport: "serial", transition: mixedTransition, sustainedRun: serialMixedRun });
    await selectRows(currentPage, 1);
    const mixedRowsOne = await waitForSnapshot(
      currentPage,
      (candidate) => candidate.frame.rows === 1 && isCompleteFreshMixed(candidate, "RVRCCVVR"),
      30_000,
      "serial ROWS=1 mixed frame with trailing N profile"
    );
    result.observations.push({ action: "mixedRowsOne", snapshot: compactSnapshot(mixedRowsOne), cellHealth: cellHealth(mixedRowsOne) });
    await selectRows(currentPage, 8);
    await waitForSnapshot(currentPage, (candidate) => candidate.frame.rows === 8 && isCompleteFreshMixed(candidate, "RVRCCVVR"), 30_000, "serial mixed ROWS=8 restore");
    report.mixed.status = "PASS";

    const capReturn = await setGlobalMode(currentPage, "CAP");
    result.observations.push(capReturn);
    await screenshot(currentPage, "HIL_S06_serial_cap_return.png");

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
    await screenshot(currentPage, "HIL_S07_serial_reconnect.png");
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
    result.diagnosticExport = await exportDiagnosticSession(currentPage, "hardware_gui_hil_serial_failure_session.zip");
    result.failureEvidence = await collectFailureEvidence(currentPage, "serial", error);
    await screenshot(currentPage, "HIL_S99_serial_failure.png");
  } finally {
    await restoreCapIfPossible(currentPage);
    await disconnectIfConnected(currentPage);
  }
}

async function runSerialWireProbe(currentPage) {
  const result = report.serial;
  const capturedParserErrors = new Map();
  try {
    await clickTransport(currentPage, "Serial");
    await currentPage.getByTitle("Refresh ports").click();
    await poll(async () => {
      const text = await currentPage.locator(".scanState").first().textContent();
      return Boolean(text && !text.includes("Scanning"));
    }, 15_000, "serial wire-probe port refresh");
    const discoveredPorts = await currentPage.locator("#serial-port-options option").evaluateAll((options) =>
      options.map((option) => option.getAttribute("value") || "").filter(Boolean)
    );
    if (!discoveredPorts.some((port) => port.toUpperCase() === serialPort.toUpperCase())) {
      result.status = "BLOCKED";
      result.errors.push(`Wire probe did not find ${serialPort}`);
      return;
    }
    await currentPage.getByPlaceholder("Enter or select a serial port").fill(serialPort);
    await currentPage.locator(".modePanel input[type='number']").first().fill("115200");
    await currentPage.getByRole("button", { name: "Connect", exact: true }).click();
    await waitForSnapshot(
      currentPage,
      (candidate) =>
        candidate.bootstrap?.state === "SYNCED" &&
        candidate.measurement?.syncState === "synced" &&
        candidate.usbStream?.mode === "DEBUG",
      90_000,
      "serial wire-probe bootstrap"
    );
    result.observations.push(await forceUncappedFirmwarePipeline(currentPage));
    result.observations.push(await setUsbStreamViaGui(currentPage, "FULL"));
    await waitForSnapshot(currentPage, isCompleteFreshCapAnyRows, 60_000, "serial wire-probe FULL CAP");
    const performanceStart = await sendGuiCommandWithSnapshot(currentPage, "PERF?", "PERF");
    const first = performanceStart.snapshot;
    const startedAt = new Date().toISOString();
    const started = Date.now();
    let last = first;
    while (Date.now() - started < wireProbeMs) {
      await wait(Math.min(1_000, wireProbeMs - (Date.now() - started)));
      last = await backendStatus(currentPage);
      if (
        !["connected", "streaming"].includes(last.connection?.state) ||
        last.connection?.mode !== "serial" ||
        last.usbStream?.mode !== "FULL"
      ) {
        throw new Error(`Serial wire probe lost FULL streaming state: ${JSON.stringify(compactSnapshot(last))}`);
      }
      for (const row of last.logs?.rows || []) {
        if (row.tag === "PARSER") {
          capturedParserErrors.set(`${row.timestamp}:${row.rawText}`, compactLog(row));
        }
      }
    }
    const performanceEnd = await sendCausalPerformanceSnapshot(currentPage, "Serial FULL wire probe");
    last = performanceEnd.snapshot;
    for (const row of last.logs?.rows || []) {
      if (row.tag === "PARSER") {
        capturedParserErrors.set(`${row.timestamp}:${row.rawText}`, compactLog(row));
      }
    }
    result.diagnosticExport = await exportDiagnosticSession(currentPage, "hardware_gui_hil_serial_wire_session.zip");
    const observation = {
      action: "serialWireProbe",
      startedAt,
      finishedAt: new Date().toISOString(),
      elapsedMs: Date.now() - started,
      firmwarePerformance: { before: performanceStart.response, after: performanceEnd.response },
      causalWindow: causalPerformanceWindow(
        performanceStart.response,
        performanceEnd,
        "Serial FULL wire probe"
      ),
      parserErrors: [...capturedParserErrors.values()],
      startDiagnostics: first.diagnostics,
      endDiagnostics: last.diagnostics,
      parserEvidence: assertNoParserCorruption(first.diagnostics, last.diagnostics, "Serial FULL wire probe"),
      sequenceEvidence: assertNoUnexplainedSequenceLoss(first.diagnostics, last.diagnostics, "Serial FULL wire probe")
    };
    result.observations.push(observation);
    result.status = "PASS";
  } catch (error) {
    result.status = "FAIL";
    result.errors.push(errorText(error));
    result.capturedParserErrors = [...capturedParserErrors.values()];
    result.diagnosticExport = result.diagnosticExport || await exportDiagnosticSession(currentPage, "hardware_gui_hil_serial_wire_failure_session.zip");
    result.failureEvidence = await collectFailureEvidence(currentPage, "serial-wire", error);
    await screenshot(currentPage, "HIL_SW99_serial_wire_failure.png");
  } finally {
    await disconnectIfConnected(currentPage);
  }
}

async function runSerialLifecycleAcceptance(currentPage) {
  const lifecycle = report.deviceLifecycle;
  const fdc = report.fdcIsolation;
  try {
    let snapshot = await connectSerialForPhase(currentPage, "lifecycle", true);
    if (snapshot === null) {
      lifecycle.status = "BLOCKED";
      fdc.status = "BLOCKED";
      const reason = `GUI serial refresh did not find ${serialPort}`;
      lifecycle.errors.push(reason);
      fdc.errors.push(reason);
      return;
    }
    const attachedBootId = Number(snapshot.device?.bootId);
    if (snapshot.measurement?.syncState !== "synced") {
      const restartStarted = Date.now();
      const attachedConnectionGeneration = Number(snapshot.connection?.connectionGeneration || 0);
      await currentPage.getByRole("tab", { name: "Advanced" }).click();
      currentPage.once("dialog", (dialog) => void dialog.accept());
      await currentPage.getByRole("button", { name: "Restart", exact: true }).click();
      const restarted = await waitForSnapshot(
        currentPage,
        (candidate) =>
          candidate.bootstrap?.state === "SYNCED" &&
          candidate.measurement?.syncState === "synced" &&
          Number(candidate.device?.bootId) !== attachedBootId &&
          Number(candidate.connection?.connectionGeneration || 0) > attachedConnectionGeneration &&
          isCompleteFreshCap(candidate),
        150_000,
        "restart recovery from pre-existing degraded device state"
      );
      lifecycle.observations.push({
        action: "preExistingDegradedRestartRecovery",
        oldBootId: attachedBootId,
        newBootId: restarted.device?.bootId,
        restartEvidence: deferredCommandEvidence(restarted, restartStarted, "RACK", "RAPP", "RESTART"),
        snapshot: compactSnapshot(restarted)
      });
      snapshot = restarted;
    }
    const initialBootId = Number(snapshot.device?.bootId);
    if (!Number.isInteger(initialBootId)) {
      throw new Error(`Lifecycle HIL requires an authoritative bootId: ${JSON.stringify(snapshot.device)}`);
    }
    lifecycle.observations.push({ action: "bootstrap", snapshot: compactSnapshot(snapshot) });

    const recoverStarted = Date.now();
    await currentPage.getByRole("tab", { name: "Advanced" }).click();
    const advancedAuthorityText = (await currentPage.locator(".advancedPanel").textContent()) || "";
    const expectedCalibrationLabel = snapshot.calibration?.valid ? "Calibrated" : "Uncalibrated/default";
    for (const expectedText of [
      `boot ${initialBootId}`,
      `READY ${String(snapshot.device?.ready)}`,
      `protocol ${String(snapshot.device?.protocol?.version ?? "unknown")}`,
      `build ${String(snapshot.device?.build?.project ?? "unknown")}`,
      expectedCalibrationLabel
    ]) {
      if (!advancedAuthorityText.includes(expectedText)) {
        throw new Error(`Advanced device authority omitted ${expectedText}: ${compactText(advancedAuthorityText, 1_200)}`);
      }
    }
    lifecycle.observations.push({
      action: "advancedAuthority",
      protocol: snapshot.device?.protocol,
      build: snapshot.device?.build,
      calibration: snapshot.calibration,
      expectedCalibrationLabel
    });
    await currentPage.getByRole("button", { name: "Recover", exact: true }).click();
    const recovered = await waitForSnapshot(
      currentPage,
      (candidate) => hasDeferredCommand(candidate, recoverStarted, "RACK", "RAPP", "RECOVER", ["applied", "safe"]),
      45_000,
      "RECOVER RACK/RAPP applied"
    );
    const recoverEvidence = deferredCommandEvidence(recovered, recoverStarted, "RACK", "RAPP", "RECOVER");
    lifecycle.observations.push({ action: "recover", ...recoverEvidence, snapshot: compactSnapshot(recovered) });

    snapshot = await backendStatus(currentPage);
    if (snapshot.measurement?.appliedMode !== "VOLT") {
      lifecycle.observations.push(await setGlobalMode(currentPage, "VOLT"));
    }
    const voltageSnapshot = await waitForSnapshot(
      currentPage,
      (candidate) =>
        isCompleteFreshMode(candidate, "VOLT") &&
        candidate.rail?.valid === true &&
        candidate.rail?.fresh === true &&
        Number.isFinite(Number(candidate.rail?.avddUv)) &&
        Number.isFinite(Number(candidate.rail?.avssUv)) &&
        Number.isFinite(Number(candidate.rail?.spanUv)) &&
        candidate.voltage?.derivedValid === true &&
        candidate.voltage?.railBootMatchesFrame === true,
      60_000,
      "fresh same-boot rail and VSS-derived voltage"
    );
    const editableRailInputs = await currentPage.locator("input").evaluateAll((inputs) =>
      inputs.filter((input) => /AVDD|AVSS/i.test([
        input.getAttribute("name"),
        input.getAttribute("placeholder"),
        input.getAttribute("aria-label")
      ].filter(Boolean).join(" "))).length
    );
    if (editableRailInputs !== 0) {
      throw new Error(`Production GUI exposed ${editableRailInputs} editable AVDD/AVSS inputs`);
    }
    report.rail.status = "PASS";
    report.rail.observations.push({
      action: "serialInternalRail",
      rail: voltageSnapshot.rail,
      voltage: voltageSnapshot.voltage,
      editableRailInputs
    });
    snapshot = voltageSnapshot;
    if (snapshot.measurement?.rowProfile?.appliedModes?.join("") !== "RRRRRRRR") {
      lifecycle.observations.push(await applyRowProfile(currentPage, "RRRRRRRR"));
    }
    snapshot = await waitForSnapshot(
      currentPage,
      (candidate) =>
        candidate.measurement?.appliedMode === "RES" &&
        candidate.measurement?.rowProfile?.appliedModes?.join("") === "RESRESRESRESRESRESRESRES" &&
        isCompleteFreshMode(candidate, "RES"),
      45_000,
      "homogeneous RES precondition for FDC isolation"
    );

    const fdcStarted = Date.now();
    await currentPage.getByRole("tab", { name: "Advanced" }).click();
    await currentPage.getByRole("button", { name: "Enable", exact: true }).click();
    const isolated = await waitForSnapshot(
      currentPage,
      (candidate) =>
        candidate.fdcIsolation?.sd === "high" &&
        candidate.fdcIsolation?.verified === true &&
        candidate.fdcIsolation?.restartRequired === true &&
        hasDeferredCommand(candidate, fdcStarted, "FACK", "FAPP", "FDCISO", ["applied"]),
      45_000,
      "FDCISO FACK/FAPP and restart-required state"
    );
    const fdcApplyEvidence = deferredCommandEvidence(isolated, fdcStarted, "FACK", "FAPP", "FDCISO");
    const disableGuarded = await currentPage.getByRole("button", { name: "Disable", exact: true }).isDisabled();
    await currentPage.getByRole("tab", { name: "Setup" }).click();
    const capGuarded = await currentPage.getByTestId("measurement-mode-control").getByRole("button", { name: "CAP", exact: true }).isDisabled();
    if (!disableGuarded || !capGuarded) {
      throw new Error(`FDC restart-required UI guards were incomplete: disable=${disableGuarded}, CAP=${capGuarded}`);
    }
    fdc.observations.push({
      action: "enable",
      ...fdcApplyEvidence,
      state: isolated.fdcIsolation,
      capGuarded,
      disableGuarded
    });
    await screenshot(currentPage, "HIL_L01_fdc_restart_required.png");

    const offResponse = await sendGuiCommand(currentPage, "FDCISO=OFF", "FERR");
    if (
      offResponse.parsedFields?.reason !== "restart_required" ||
      offResponse.parsedFields?.restartRequired !== "1"
    ) {
      throw new Error(`FDCISO=OFF did not return the expected restart_required FERR: ${JSON.stringify(offResponse)}`);
    }
    fdc.observations.push({ action: "offRejectedOnce", response: offResponse });

    const restartStarted = Date.now();
    const connectionGenerationBefore = Number(isolated.connection?.connectionGeneration || 0);
    await currentPage.getByRole("tab", { name: "Advanced" }).click();
    currentPage.once("dialog", (dialog) => void dialog.accept());
    await currentPage.getByRole("button", { name: "Restart", exact: true }).click();
    const restarting = await waitForSnapshot(
      currentPage,
      (candidate) => hasDeferredCommand(candidate, restartStarted, "RACK", "RAPP", "RESTART", ["restarting"]),
      30_000,
      "RESTART RACK/RAPP restarting"
    );
    const restartEvidence = deferredCommandEvidence(restarting, restartStarted, "RACK", "RAPP", "RESTART");
    lifecycle.observations.push({ action: "restartAccepted", ...restartEvidence, snapshot: compactSnapshot(restarting) });

    const rebooted = await waitForSnapshot(
      currentPage,
      (candidate) =>
        Number(candidate.device?.bootId) !== initialBootId &&
        Number(candidate.connection?.connectionGeneration) > connectionGenerationBefore &&
        candidate.bootstrap?.state === "SYNCED" &&
        candidate.measurement?.syncState === "synced" &&
        candidate.fdcIsolation?.sd === "low" &&
        candidate.fdcIsolation?.restartRequired === false &&
        isCompleteFreshCap(candidate),
      150_000,
      "automatic Serial reconnect and bootstrap after RESTART"
    );
    const lifecycleEvent = [...(rebooted.device?.lifecycleEvents || [])].reverse().find(
      (event) => event.kind === "DEVICE_REBOOT" && Number(event.newBootId) === Number(rebooted.device?.bootId)
    );
    const latestCommand = rebooted.commands?.latestCommand;
    if (!lifecycleEvent?.expected || lifecycleEvent.resetCategory !== "manual_restart") {
      throw new Error(`RESTART reboot was not classified as expected/manual: ${JSON.stringify(lifecycleEvent)}`);
    }
    if (latestCommand?.commandType !== "restart" || latestCommand?.state !== "COMPLETED_AFTER_REBOOT") {
      throw new Error(`RESTART transaction did not complete after new boot: ${JSON.stringify(latestCommand)}`);
    }
    lifecycle.observations.push({
      action: "restartRecovered",
      oldBootId: initialBootId,
      newBootId: rebooted.device.bootId,
      oldConnectionGeneration: connectionGenerationBefore,
      newConnectionGeneration: rebooted.connection.connectionGeneration,
      lifecycleEvent,
      latestCommand,
      snapshot: compactSnapshot(rebooted)
    });
    fdc.observations.push({ action: "restartCleared", state: rebooted.fdcIsolation, capAvailable: true });
    await screenshot(currentPage, "HIL_L02_restart_recovered_cap.png");
    lifecycle.status = "PASS";
    fdc.status = "PASS";
  } catch (error) {
    lifecycle.status = "FAIL";
    fdc.status = fdc.observations.length ? "FAIL" : "BLOCKED";
    lifecycle.errors.push(errorText(error));
    fdc.errors.push(errorText(error));
    lifecycle.failureEvidence = await collectFailureEvidence(currentPage, "lifecycle", error);
    await screenshot(currentPage, "HIL_L99_lifecycle_failure.png");
  } finally {
    const recovery = await ensureFdcRestartRecovered(currentPage);
    if (recovery) {
      fdc.observations.push(recovery);
    }
    await disconnectIfConnected(currentPage);
  }
}

async function runGuiStressAcceptance(currentPage) {
  const result = report.guiStress;
  let recordingStarted = false;
  try {
    let snapshot = await connectSerialForPhase(currentPage, "GUI stress");
    if (snapshot === null) {
      result.status = "BLOCKED";
      result.errors.push(`GUI serial refresh did not find ${serialPort}`);
      return;
    }
    if (snapshot.measurement?.appliedMode !== "CAP") {
      await setGlobalMode(currentPage, "CAP");
    }
    if (Number(snapshot.frame?.rows) !== 8) {
      await selectRows(currentPage, 8);
    }
    await waitForSnapshot(currentPage, isCompleteFreshCap, 60_000, "GUI stress CAP ROWS=8 precondition");
    result.observations.push(await forceUncappedFirmwarePipeline(currentPage));
    result.observations.push(await setUsbStreamViaGui(currentPage, "FULL"));
    const performanceStart = await sendGuiCommandWithSnapshot(currentPage, "PERF?", "PERF");
    const diagnosticsBefore = performanceStart.snapshot.diagnostics;
    const memoryBefore = await rendererMemorySample();
    const presentationBefore = await presentationDiagnostics(currentPage);

    const startResponse = await currentPage.request.post(`${backendUrl}/api/recording/start`, {
      data: { directory: artifactRoot, allowReducedStream: false }
    });
    const startPayload = await startResponse.json();
    if (!startResponse.ok() || startPayload?.ok !== true) {
      throw new Error(`GUI stress recording start failed: ${startResponse.status()} ${JSON.stringify(startPayload)}`);
    }
    recordingStarted = true;

    const startedAt = new Date().toISOString();
    for (let cycle = 1; cycle <= 100; cycle += 1) {
      for (const tab of ["Advanced", "Setup", "Advanced", "Setup"]) {
        await currentPage.getByRole("tab", { name: tab, exact: true }).click();
      }
      await currentPage.getByRole("button", { name: "Raw Log", exact: true }).click();
      await currentPage.getByRole("button", { name: "Status", exact: true }).click();
      await currentPage.setViewportSize({ width: cycle % 2 ? 1220 : 1460, height: cycle % 3 ? 780 : 920 });
      await minimizeAndRestoreWindow();
      if (cycle % 10 === 0) {
        const current = await backendStatus(currentPage);
        if (!["connected", "streaming"].includes(current.connection?.state)) {
          throw new Error(`GUI stress lost transport at cycle ${cycle}: ${JSON.stringify(current.connection)}`);
        }
      }
    }
    await wait(5_000);
    const performanceEnd = await sendCausalPerformanceSnapshot(currentPage, "GUI 100-cycle stress");
    const memoryAfter = await rendererMemorySample();
    const presentationAfter = await presentationDiagnostics(currentPage);
    const stopResponse = await currentPage.request.post(`${backendUrl}/api/recording/stop`, { data: {} });
    const stopPayload = await stopResponse.json();
    recordingStarted = false;
    const recording = stopPayload?.recording || {};
    if (
      !stopResponse.ok() ||
      numeric(recording.receivedFrames) <= 0 ||
      numeric(recording.writtenFrames) !== numeric(recording.receivedFrames) ||
      numeric(recording.droppedFrames) !== 0
    ) {
      throw new Error(`GUI stress recorder continuity failed: ${JSON.stringify(stopPayload)}`);
    }
    assertNoParserCorruption(diagnosticsBefore, performanceEnd.snapshot.diagnostics, "GUI 100-cycle stress");
    assertNoUnexplainedSequenceLoss(diagnosticsBefore, performanceEnd.snapshot.diagnostics, "GUI 100-cycle stress");
    if (presentationBefore.counts !== presentationAfter.counts || !presentationAfter.text.includes("WebSocket client/server 1/1")) {
      throw new Error(`GUI resources did not plateau: ${JSON.stringify({ presentationBefore, presentationAfter })}`);
    }
    const memoryGrowthKb = numeric(memoryAfter?.workingSetSize) - numeric(memoryBefore?.workingSetSize);
    if (memoryGrowthKb > 256 * 1024) {
      throw new Error(`GUI stress renderer working set grew by ${memoryGrowthKb} KiB`);
    }
    if (report.electron.consoleErrors.length || report.electron.pageErrors.length) {
      throw new Error(`GUI errors observed during stress: ${JSON.stringify(report.electron)}`);
    }
    result.observations.push({
      action: "guiStress",
      startedAt,
      finishedAt: new Date().toISOString(),
      cycles: 100,
      tabSwitches: 400,
      logViewSwitches: 200,
      resizes: 100,
      minimiseRestoreCycles: 100,
      presentationBefore,
      presentationAfter,
      memoryBefore,
      memoryAfter,
      memoryGrowthKb,
      recording,
      causalWindow: causalPerformanceWindow(
        performanceStart.response,
        performanceEnd,
        "GUI 100-cycle stress"
      ),
      parserEvidence: assertNoParserCorruption(diagnosticsBefore, performanceEnd.snapshot.diagnostics, "GUI 100-cycle stress"),
      sequenceEvidence: assertNoUnexplainedSequenceLoss(diagnosticsBefore, performanceEnd.snapshot.diagnostics, "GUI 100-cycle stress")
    });
    await screenshot(currentPage, "HIL_G01_gui_100_cycle_stress.png");
    result.status = "PASS";
  } catch (error) {
    result.status = "FAIL";
    result.errors.push(errorText(error));
    result.failureEvidence = await collectFailureEvidence(currentPage, "gui-stress", error);
    await screenshot(currentPage, "HIL_G99_gui_stress_failure.png");
  } finally {
    if (recordingStarted) {
      try {
        await currentPage.request.post(`${backendUrl}/api/recording/stop`, { data: {} });
      } catch {
        // Failure evidence above remains authoritative.
      }
    }
    await restoreCapIfPossible(currentPage);
    await disconnectIfConnected(currentPage);
  }
}

async function runSerialSwitchingAcceptance(currentPage) {
  const result = report.switching;
  const transitions = [];
  try {
    let snapshot = await connectSerialForPhase(currentPage, "Serial switching");
    if (snapshot === null) {
      result.status = "BLOCKED";
      result.errors.push(`GUI serial refresh did not find ${serialPort}`);
      return;
    }
    if (Number(snapshot.frame?.rows) !== 8) {
      await selectRows(currentPage, 8);
      snapshot = await waitForSnapshot(
        currentPage,
        (candidate) => Number(candidate.frame?.rows) === 8 && activeCellsFresh(candidate) === 64,
        45_000,
        "Serial switching ROWS=8 precondition"
      );
    }
    result.observations.push(await forceUncappedFirmwarePipeline(currentPage));
    result.observations.push(await setUsbStreamViaGui(currentPage, "FULL"));
    const performanceStart = await sendGuiCommandWithSnapshot(currentPage, "PERF?", "PERF");
    if (performanceStart.snapshot.measurement?.appliedMode === "CAP") {
      transitions.push({ cycle: 0, precondition: true, ...(await setGlobalMode(currentPage, "RES")) });
    }
    try {
      await waitForSnapshot(
        currentPage,
        resistanceFixtureAnchorsInRange,
        30_000,
        "stable resistance fixture anchors"
      );
      const fixture = await verifyResistanceFixture(currentPage);
      report.resistanceFixture.status = "PASS";
      report.resistanceFixture.observations.push(fixture);
      result.observations.push(fixture);
    } catch (error) {
      const fixtureFailure = {
        action: "resistanceFixture",
        status: "FAIL",
        error: errorText(error),
        snapshot: compactSnapshot(await backendStatus(currentPage))
      };
      report.resistanceFixture.status = "FAIL";
      report.resistanceFixture.errors.push(errorText(error));
      report.resistanceFixture.observations.push(fixtureFailure);
      result.observations.push(fixtureFailure);
    }
    for (let cycle = 1; cycle <= switchingCycles; cycle += 1) {
      transitions.push({ cycle, ...(await setGlobalMode(currentPage, "CAP")) });
      transitions.push({ cycle, ...(await setGlobalMode(currentPage, "RES")) });
      transitions.push({ cycle, ...(await setGlobalMode(currentPage, "VOLT")) });
      transitions.push({ cycle, ...(await applyRowProfile(currentPage, "RVRCCVVR")) });
      transitions.push({ cycle, ...(await applyRowProfile(currentPage, "CCCCCCCC")) });
      transitions.push({ cycle, ...(await applyRowProfile(currentPage, "RRRRRRRR")) });
      transitions.push({ cycle, ...(await applyRowProfile(currentPage, "VVVVVVVV")) });
    }
    const performanceEnd = await sendCausalPerformanceSnapshot(currentPage, "Serial 10-cycle switching");
    const parserEvidence = assertNoParserCorruption(
      performanceStart.snapshot.diagnostics,
      performanceEnd.snapshot.diagnostics,
      "Serial 10-cycle switching"
    );
    const sequenceEvidence = assertNoUnexplainedSequenceLoss(
      performanceStart.snapshot.diagnostics,
      performanceEnd.snapshot.diagnostics,
      "Serial 10-cycle switching"
    );
    result.observations.push({
      action: "serialSwitchingStress",
      cycles: switchingCycles,
      requiredTransactions: switchingCycles * 7,
      actualTransactions: transitions.filter((item) => !item.precondition).length,
      transitions,
      firmwarePerformance: { before: performanceStart.response, after: performanceEnd.response },
      causalWindow: causalPerformanceWindow(
        performanceStart.response,
        performanceEnd,
        "Serial 10-cycle switching"
      ),
      parserEvidence,
      sequenceEvidence
    });
    await screenshot(currentPage, "HIL_T01_serial_10_cycle_switching.png");
    result.status = "PASS";
  } catch (error) {
    result.status = "FAIL";
    result.observations.push({ action: "partialSerialSwitching", completedTransitions: transitions.length, transitions });
    result.errors.push(errorText(error));
    result.failureEvidence = await collectFailureEvidence(currentPage, "serial-switching", error);
    await screenshot(currentPage, "HIL_T99_serial_switching_failure.png");
  } finally {
    await restoreCapIfPossible(currentPage);
    await disconnectIfConnected(currentPage);
  }
}

async function connectSerialForPhase(currentPage, label, allowDegraded = false) {
  await clickTransport(currentPage, "Serial");
  await currentPage.getByTitle("Refresh ports").click();
  await poll(async () => {
    const text = await currentPage.locator(".scanState").first().textContent();
    return Boolean(text && !text.includes("Scanning"));
  }, 15_000, `${label} serial port refresh`);
  const discoveredPorts = await currentPage.locator("#serial-port-options option").evaluateAll((options) =>
    options.map((option) => option.getAttribute("value") || "").filter(Boolean)
  );
  if (!discoveredPorts.some((port) => port.toUpperCase() === serialPort.toUpperCase())) {
    return null;
  }
  await currentPage.getByPlaceholder("Enter or select a serial port").fill(serialPort);
  await currentPage.locator(".modePanel input[type='number']").first().fill("115200");
  await currentPage.getByRole("button", { name: "Connect", exact: true }).click();
  return waitForSnapshot(
    currentPage,
    (candidate) =>
      candidate.connection?.mode === "serial" &&
      ["connected", "streaming"].includes(candidate.connection?.state) &&
      candidate.bootstrap?.state === "SYNCED" &&
      (
        candidate.measurement?.syncState === "synced" ||
        (allowDegraded && candidate.measurement?.syncState === "device_degraded")
      ) &&
      candidate.device?.ready === true &&
      candidate.device?.protocol?.compatible === true,
    100_000,
    `${label} serial bootstrap`
  );
}

function hasDeferredCommand(snapshot, startedAt, acceptedTag, terminalTag, command, terminalStates) {
  const rows = logsSince(snapshot, startedAt, [acceptedTag, terminalTag]);
  const accepted = rows.find((row) => row.tag === acceptedTag && (!row.parsedFields?.cmd || row.parsedFields.cmd === command));
  const terminal = rows.find(
    (row) => row.tag === terminalTag &&
      (!row.parsedFields?.cmd || row.parsedFields.cmd === command) &&
      terminalStates.includes(row.parsedFields?.state)
  );
  if (accepted && terminal) {
    return true;
  }
  const record = deferredCommandRecord(snapshot, startedAt, command);
  return Boolean(
    record &&
    record.acceptedMessage?.startsWith(`${acceptedTag},`) &&
    record.terminalMessage?.startsWith(`${terminalTag},`) &&
    terminalStates.includes(record.terminalRawFields?.state)
  );
}

function deferredCommandEvidence(snapshot, startedAt, acceptedTag, terminalTag, command) {
  const rows = logsSince(snapshot, startedAt, [acceptedTag, terminalTag]);
  let accepted = rows.find((row) => row.tag === acceptedTag && (!row.parsedFields?.cmd || row.parsedFields.cmd === command));
  let terminal = rows.find((row) => row.tag === terminalTag && (!row.parsedFields?.cmd || row.parsedFields.cmd === command));
  const record = deferredCommandRecord(snapshot, startedAt, command);
  accepted ||= commandRecordLog(record, acceptedTag, "accepted");
  terminal ||= commandRecordLog(record, terminalTag, "terminal");
  if (!accepted || !terminal) {
    throw new Error(`${command} did not expose ${acceptedTag}/${terminalTag}: ${JSON.stringify(rows)}`);
  }
  const acceptedId = Number(accepted.parsedFields?.id);
  const terminalId = Number(terminal.parsedFields?.id);
  if (!Number.isInteger(acceptedId) || acceptedId !== terminalId) {
    throw new Error(`${command} request ID mismatch: ${acceptedId} != ${terminalId}`);
  }
  return { accepted, terminal, requestId: acceptedId };
}

function deferredCommandRecord(snapshot, startedAt, command) {
  const commandType = ({ ROWMODES: "row_modes", FDCISO: "fdc_isolation" })[command] || command.toLowerCase();
  const sinceSeconds = startedAt / 1000;
  return [...(snapshot.commands?.transactions || [])].reverse().find(
    (record) => record.commandType === commandType && Number(record.sentTime) >= sinceSeconds
  );
}

function commandRecordLog(record, tag, phase) {
  if (!record) {
    return null;
  }
  const accepted = phase === "accepted";
  const rawText = accepted ? record.acceptedMessage : record.terminalMessage;
  const parsedFields = accepted ? record.acceptedRawFields : record.terminalRawFields;
  if (!rawText?.startsWith(`${tag},`) || !parsedFields) {
    return null;
  }
  return {
    tag,
    timestamp: accepted ? record.acceptedTime : record.appliedTime,
    parsedFields,
    rawText,
    source: "serial",
    channel: "transaction-audit"
  };
}

async function ensureFdcRestartRecovered(currentPage) {
  const snapshot = await safeStatus(currentPage);
  if (
    !snapshot?.fdcIsolation?.restartRequired ||
    !["connected", "streaming"].includes(snapshot.connection?.state)
  ) {
    return null;
  }
  const oldBootId = Number(snapshot.device?.bootId);
  const response = await currentPage.request.post(`${backendUrl}/api/device/restart`, { data: {} });
  if (!response.ok()) {
    throw new Error(`Cleanup RESTART failed: ${response.status()} ${await response.text()}`);
  }
  const recovered = await waitForSnapshot(
    currentPage,
    (candidate) =>
      Number(candidate.device?.bootId) !== oldBootId &&
      candidate.bootstrap?.state === "SYNCED" &&
      candidate.fdcIsolation?.restartRequired === false,
    150_000,
    "cleanup restart after FDC isolation"
  );
  return { action: "cleanupRestart", oldBootId, newBootId: recovered.device?.bootId };
}

async function presentationDiagnostics(currentPage) {
  await currentPage.getByRole("tab", { name: "Advanced" }).click();
  const group = currentPage.locator(".advancedPanel .controlGroup").filter({
    has: currentPage.locator(".panelHeader.small", { hasText: "Presentation Diagnostics" })
  });
  const text = ((await group.textContent()) || "").replace(/\s+/g, " ").trim();
  const match = text.match(/charts (\d+) · resize observers (\d+).*WebSocket client\/server (\d+)\/(\d+)/);
  if (!match) {
    throw new Error(`Could not parse presentation diagnostics: ${text}`);
  }
  return { text, counts: match.slice(1).join("/") };
}

async function minimizeAndRestoreWindow() {
  await electronApp.evaluate(async ({ BrowserWindow }) => {
    BrowserWindow.getAllWindows()[0]?.minimize();
  });
  await wait(20);
  await electronApp.evaluate(async ({ BrowserWindow }) => {
    const window = BrowserWindow.getAllWindows()[0];
    window?.restore();
    window?.show();
  });
  await wait(20);
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
    if (requestedPhase === "ble-reconnect") {
      const automaticReconnect = await runBleUnexpectedReconnectStress(currentPage, selected.address, 30, "rows");
      result.observations.push(automaticReconnect);
      const finalReconnect = await backendStatus(currentPage);
      result.observations.push(
        assertNoParserCorruption(initialCap.diagnostics, finalReconnect.diagnostics, "BLE reconnect-only phase")
      );
      result.observations.push(
        assertReconnectBoundaryIntegrity(initialCap.diagnostics, finalReconnect.diagnostics, 30)
      );
      result.status = "PASS";
      await screenshot(currentPage, "HIL_B06_ble_reconnect.png");
      return;
    }
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

    const automaticReconnect = await runBleUnexpectedReconnectStress(currentPage, selected.address, 30);
    result.observations.push(automaticReconnect);

    const beforeManualReconnect = await backendStatus(currentPage);
    await currentPage.getByRole("button", { name: "Disconnect", exact: true }).click();
    await waitForSnapshot(currentPage, (candidate) => candidate.connection.state === "disconnected", 20_000, "BLE disconnect");
    await currentPage.getByRole("button", { name: "Connect", exact: true }).click();
    const reconnected = await waitForSnapshot(
      currentPage,
      (candidate) =>
        candidate.connection.mode === "ble" &&
        candidate.connection.state === "streaming" &&
        candidate.connection.generation > firstGeneration &&
        candidate.bootstrap?.state === "SYNCED" &&
        Number(candidate.device?.bootId) === Number(beforeManualReconnect.device?.bootId),
      60_000,
      "BLE manual stop/start reconnect and same-boot bootstrap"
    );
    const reconnectedFrame = await waitForSnapshot(
      currentPage,
      (candidate) => matchesMeasurementState(candidate, beforeManualReconnect),
      45_000,
      "BLE authoritative measurement state after manual reconnect"
    );
    await wait(5_000);
    const finalBle = await backendStatus(currentPage);
    result.observations.push(
      assertNoParserCorruption(initialCap.diagnostics, finalBle.diagnostics, "BLE full acceptance")
    );
    result.observations.push({
      action: "reconnect",
      firstGeneration,
      newGeneration: reconnected.connection.generation,
      sameBoot: Number(finalBle.device?.bootId) === Number(beforeManualReconnect.device?.bootId),
      snapshot: compactSnapshot(reconnectedFrame),
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

async function runBleUnexpectedReconnectStress(currentPage, address, cycles, probe = "mode") {
  const observations = [];
  let snapshot = await backendStatus(currentPage);
  const sessionGeneration = Number(snapshot.connection?.generation);
  const bootId = Number(snapshot.device?.bootId);
  if (!Number.isInteger(sessionGeneration) || !Number.isInteger(bootId)) {
    throw new Error(`BLE reconnect stress requires session/boot identity: ${JSON.stringify(compactSnapshot(snapshot))}`);
  }
  for (let cycle = 1; cycle <= cycles; cycle += 1) {
    const connectionGeneration = Number(snapshot.connection?.connectionGeneration);
    const expectedState = snapshot;
    const forced = await execFile("/usr/bin/bluetoothctl", ["disconnect", address], { timeout: 15_000 });
    const streaming = await waitForSnapshot(
      currentPage,
      (candidate) =>
        candidate.connection?.mode === "ble" &&
        candidate.connection?.state === "streaming" &&
        Number(candidate.connection?.generation) === sessionGeneration &&
        Number(candidate.connection?.connectionGeneration) > connectionGeneration,
      60_000,
      `BLE automatic reconnect cycle ${cycle}`
    );
    if (Number(streaming.device?.bootId) !== bootId) {
      throw new Error(`BLE link-only cycle ${cycle} changed bootId ${bootId} -> ${streaming.device?.bootId}`);
    }
    const resynchronised = await waitForSnapshot(
      currentPage,
      (candidate) =>
        candidate.bootstrap?.state === "SYNCED" &&
        candidate.measurement?.syncState === "synced" &&
        matchesMeasurementState(candidate, expectedState),
      60_000,
      `BLE same-boot state resynchronisation cycle ${cycle}`
    );
    const controlPath = probe === "rows"
      ? await setRowsWithEvidence(currentPage, cycle % 2 === 1 ? 7 : 8)
      : await setGlobalMode(currentPage, cycle % 2 === 1 ? "CAP" : "RES");
    snapshot = await backendStatus(currentPage);
    observations.push({
      cycle,
      forcedDisconnect: { stdout: forced.stdout?.trim() || "", stderr: forced.stderr?.trim() || "" },
      oldConnectionGeneration: connectionGeneration,
      newConnectionGeneration: snapshot.connection?.connectionGeneration,
      sameSessionGeneration: Number(snapshot.connection?.generation) === sessionGeneration,
      sameBoot: Number(snapshot.device?.bootId) === bootId,
      resynchronised: compactSnapshot(resynchronised),
      controlPath
    });
  }
  const final = await backendStatus(currentPage);
  const counters = final.diagnostics?.transport?.queueCounters || {};
  for (const key of ["controlDrops", "lifecycleDrops", "faultDrops"]) {
    if (numeric(counters[key]) !== 0) {
      throw new Error(`BLE reconnect stress observed ${key}=${counters[key]}`);
    }
  }
  return {
    action: "automaticReconnectStress",
    probe,
    requestedCycles: cycles,
    completedCycles: observations.length,
    sessionGeneration,
    bootId,
    queueCounters: counters,
    cycles: observations
  };
}

async function runMixedAcceptance(currentPage, result) {
  let mixedCoreComplete = false;
  let mixedStabilityStarted = false;
  let switchingStarted = false;
  let batteryEvaluated = false;
  const switchResults = [];
  try {
    const mixedTransition = await applyRowProfile(currentPage, "RVRCCVVR");
    result.observations.push(mixedTransition);
    report.mixed.observations.push(mixedTransition);
    await screenshot(currentPage, "HIL_B07_ble_mixed_rvrccvvr.png");
    mixedStabilityStarted = true;
    const mixedStability = await observeSustainedRun(
      currentPage,
      "BLE mixed RVRCCVVR",
      minimumRunMs,
      (candidate) => isCompleteFreshMixed(candidate, "RVRCCVVR")
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
      for (const profile of ["CCCCCCCC", "RVRCCVVR", "VVVVVVVV", "CRVCRVCR", "RRRRRRRR"]) {
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
  await currentPage.getByRole("tab", { name: "Setup" }).click();
  const group = currentPage.locator(".controlGroup").filter({ has: currentPage.locator(".panelHeader", { hasText: "Rows" }) });
  await group.locator("select").selectOption(String(rows));
}

async function setRowsWithEvidence(currentPage, rows) {
  const startedAt = Date.now();
  await selectRows(currentPage, rows);
  const snapshot = await waitForSnapshot(
    currentPage,
    (candidate) =>
      candidate.frame?.rows === rows &&
      candidate.commands?.pendingRows === null &&
      candidate.commands?.activeRows === rows &&
      activeCellsFresh(candidate) === rows * 8,
    45_000,
    `BLE reconnect probe ROWS=${rows}`
  );
  const logs = logsSince(snapshot, startedAt, ["RCMD", "RAPP"]);
  const accepted = logs.find((row) => row.tag === "RCMD");
  const applied = logs.find((row) => row.tag === "RAPP");
  if (!accepted || !applied) {
    throw new Error(`BLE ROWS=${rows} reconnect probe requires RCMD and RAPP: ${JSON.stringify(logs)}`);
  }
  if (accepted.channel !== "ctrl" || applied.channel !== "log") {
    throw new Error(`BLE ROWS=${rows} reconnect probe requires FF11/ctrl RCMD and FF30/log RAPP: ${JSON.stringify(logs)}`);
  }
  assertAppliedIdentity(snapshot, accepted, applied, {
    generation: snapshot.commands?.rowsGeneration,
    requestId: snapshot.commands?.rowsAppliedRequestId,
    frameSeq: snapshot.commands?.rowsFrameSeq,
    dataGeneration: snapshot.frame?.generation,
    dataRequestId: snapshot.frame?.requestId
  }, `ROWS=${rows}`);
  return { action: "rowsReconnectProbe", transport: "ble", rows, accepted, applied, snapshot: compactSnapshot(snapshot) };
}

async function setGlobalMode(currentPage, mode) {
  const activeTransport = (await backendStatus(currentPage)).connection.mode;
  const startedAt = Date.now();
  await currentPage.getByRole("tab", { name: "Setup" }).click();
  await currentPage.getByTestId("measurement-mode-control").getByRole("button", { name: mode, exact: true }).click();
  const snapshot = await waitForSnapshot(
    currentPage,
    (candidate) => isCompleteFreshMode(candidate, mode),
    120_000,
    `${activeTransport} ${mode} MAPP and complete fresh frame`
  );
  const logs = logsSince(snapshot, startedAt, ["MACK", "MAPP"]);
  const record = activeTransport === "serial" ? deferredCommandRecord(snapshot, startedAt, "MODE") : null;
  const accepted = logs.find((row) => row.tag === "MACK") || commandRecordLog(record, "MACK", "accepted");
  const applied = logs.find((row) => row.tag === "MAPP") || commandRecordLog(record, "MAPP", "terminal");
  if (!accepted || !applied) {
    throw new Error(`${activeTransport} ${mode} requires MACK and MAPP: ${JSON.stringify(logs)}`);
  }
  if (activeTransport === "ble" && (accepted.channel !== "ctrl" || applied.channel !== "log")) {
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
  return { action: "globalMode", transport: activeTransport, mode, accepted, applied, snapshot: compactSnapshot(snapshot) };
}

async function applyRowProfile(currentPage, profile) {
  const activeTransport = (await backendStatus(currentPage)).connection.mode;
  const modes = [...profile].map((mode) => mode === "C" ? "CAP" : mode === "V" ? "VOLT" : "RES");
  await currentPage.getByRole("tab", { name: "Setup" }).click();
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
    120_000,
    `${activeTransport} ROWMODES=${profile} RMAPP and fresh frame`
  );
  const logs = logsSince(snapshot, startedAt, ["RMACK", "RMAPP"]);
  const record = activeTransport === "serial" ? deferredCommandRecord(snapshot, startedAt, "ROWMODES") : null;
  const accepted = logs.find((row) => row.tag === "RMACK") || commandRecordLog(record, "RMACK", "accepted");
  const applied = logs.find((row) => row.tag === "RMAPP") || commandRecordLog(record, "RMAPP", "terminal");
  if (!accepted || !applied) {
    throw new Error(`${activeTransport} ${profile} requires RMACK and RMAPP: ${JSON.stringify(logs)}`);
  }
  if (activeTransport === "ble" && (accepted.channel !== "ctrl" || applied.channel !== "log")) {
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
  return { action: "rowProfile", transport: activeTransport, profile, accepted, applied, snapshot: compactSnapshot(snapshot) };
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

async function setUsbStreamViaGui(currentPage, mode) {
  const startedAt = Date.now();
  await currentPage.getByRole("tab", { name: "Advanced" }).click();
  const group = currentPage.locator(".advancedPanel .controlGroup").filter({
    has: currentPage.locator(".panelHeader.small", { hasText: "USB Stream" })
  });
  await group.getByRole("button", { name: mode, exact: true }).click();
  const snapshot = await waitForSnapshot(
    currentPage,
    (candidate) => candidate.usbStream?.mode === mode && candidate.usbStream?.state === "applied",
    15_000,
    `USBSTREAM=${mode} applied`
  );
  await currentPage.getByRole("tab", { name: "Setup" }).click();
  return {
    action: "usbStream",
    mode,
    dataEvery: snapshot.usbStream?.dataEvery,
    diagEvery: snapshot.usbStream?.diagEvery,
    logs: logsSince(snapshot, startedAt, ["USBSTREAM", "ACK"])
  };
}

async function runFullRateRecording(currentPage) {
  const startResponse = await currentPage.request.post(`${backendUrl}/api/recording/start`, {
    data: { directory: artifactRoot, allowReducedStream: false }
  });
  if (!startResponse.ok()) {
    throw new Error(`FULL recording start failed: ${startResponse.status()} ${await startResponse.text()}`);
  }
  const started = await startResponse.json();
  let run;
  let stopPayload;
  try {
    run = await observeSustainedRun(currentPage, "serial CAP FULL recording/endurance", enduranceRunMs, isCompleteFreshCap);
  } finally {
    const stopResponse = await currentPage.request.post(`${backendUrl}/api/recording/stop`, { data: {} });
    if (!stopResponse.ok()) {
      throw new Error(`FULL recording stop failed: ${stopResponse.status()} ${await stopResponse.text()}`);
    }
    stopPayload = await stopResponse.json();
  }
  const recording = stopPayload?.recording || {};
  if (
    numeric(recording.receivedFrames) <= 0 ||
    numeric(recording.writtenFrames) !== numeric(recording.receivedFrames) ||
    numeric(recording.droppedFrames) !== 0
  ) {
    throw new Error(`FULL recorder was not lossless: ${JSON.stringify(recording)}`);
  }
  if (numeric(run?.sequenceEvidence?.intentionalDelta) !== 0) {
    throw new Error(`FULL stream was incorrectly classified as intentional DEBUG decimation: ${JSON.stringify(run?.sequenceEvidence)}`);
  }
  return { action: "fullRateRecording", started: started.recording, finished: recording, sustainedRun: run };
}

async function observeSparseDebugRun(currentPage, durationMs) {
  const performanceStart = await sendGuiCommandWithSnapshot(currentPage, "PERF?", "PERF");
  const performanceBefore = performanceStart.response;
  const startedAt = new Date().toISOString();
  const started = Date.now();
  const first = performanceStart.snapshot;
  const firstPackets = numeric(first.diagnostics?.transportPackets);
  const firstParserFrames = numeric(first.diagnostics?.parserFrames);
  const revisions = new Set();
  const sequences = new Set();
  let last = first;
  let maximumCaptureFps = numeric(first.rates?.captureFps);
  let authoritativeFrames = 0;
  let lastProgressRevision = Number(first.frame?.revision);
  let lastProgressAt = Date.now();

  while (Date.now() - started < durationMs) {
    await wait(Math.min(1_000, durationMs - (Date.now() - started)));
    last = await backendStatus(currentPage);
    if (
      !["connected", "streaming"].includes(last.connection?.state) ||
      last.connection?.mode !== "serial" ||
      last.measurement?.appliedMode !== "CAP" ||
      last.usbStream?.mode !== "DEBUG" ||
      Number(last.usbStream?.dataEvery) <= 1
    ) {
      throw new Error(`serial CAP DEBUG lifecycle/configuration changed: ${JSON.stringify(compactSnapshot(last))}`);
    }
    const fatalLogs = logsSince(last, started, ["MFAULT", "APP_FATAL"]);
    if (fatalLogs.length) {
      throw new Error(`serial CAP DEBUG observed a firmware runtime fault: ${JSON.stringify(fatalLogs.at(-1))}`);
    }
    const currentRevision = Number(last.frame?.revision);
    if (Number.isFinite(currentRevision) && currentRevision > lastProgressRevision) {
      lastProgressRevision = currentRevision;
      lastProgressAt = Date.now();
    } else if (Date.now() - lastProgressAt > 15_000) {
      throw new Error(`serial CAP DEBUG frame stream stalled at revision ${lastProgressRevision}`);
    }
    maximumCaptureFps = Math.max(maximumCaptureFps, numeric(last.rates?.captureFps));
    if (last.frame?.seq !== null && last.frame?.seq !== undefined && isCurrentAuthoritativeFrame(last)) {
      if (!isCompleteFreshCapAnyRows(last)) {
        throw new Error(`serial CAP DEBUG emitted a non-authoritative/incomplete measurement: ${JSON.stringify(compactSnapshot(last))}`);
      }
      authoritativeFrames += 1;
      revisions.add(last.frame.revision);
      sequences.add(last.frame.seq);
    }
  }

  const performanceEnd = await sendCausalPerformanceSnapshot(currentPage, "serial CAP DEBUG");
  const performanceAfter = performanceEnd.response;
  last = performanceEnd.snapshot;
  const packetDelta = numeric(last.diagnostics?.transportPackets) - firstPackets;
  const parserFrameDelta = numeric(last.diagnostics?.parserFrames) - firstParserFrames;
  if (packetDelta <= 0 || maximumCaptureFps <= 0 || revisions.size < 2) {
    throw new Error(
      `serial CAP DEBUG produced no continuous transport/capture evidence: packets=${packetDelta}, captureFps=${maximumCaptureFps}, revisions=${revisions.size}`
    );
  }
  const parserEvidence = assertNoParserCorruption(first.diagnostics, last.diagnostics, "serial CAP DEBUG");
  const sequenceEvidence = assertNoUnexplainedSequenceLoss(first.diagnostics, last.diagnostics, "serial CAP DEBUG");
  return {
    action: "sparseDebugRun",
    label: "serial CAP DEBUG",
    startedAt,
    finishedAt: new Date().toISOString(),
    elapsedMs: Date.now() - started,
    configuredDataEvery: first.usbStream?.dataEvery,
    configuredDiagEvery: first.usbStream?.diagEvery,
    transportPacketDelta: packetDelta,
    parserFrameDelta,
    maximumCaptureFps,
    authoritativeSamples: authoritativeFrames,
    observedRevisionCount: revisions.size,
    observedSequenceCount: sequences.size,
    firmwarePerformance: { before: performanceBefore, after: performanceAfter },
    causalWindow: causalPerformanceWindow(performanceBefore, performanceEnd, "serial CAP DEBUG"),
    parserEvidence,
    sequenceEvidence,
    startDiagnostics: first.diagnostics,
    endDiagnostics: last.diagnostics
  };
}

async function sendGuiCommand(currentPage, command, expectedTag) {
  return (await sendGuiCommandWithSnapshot(currentPage, command, expectedTag)).response;
}

async function sendGuiCommandWithSnapshot(currentPage, command, expectedTag) {
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
  return {
    response: logsSince(responseSnapshot, sentAt, [expectedTag]).at(-1),
    snapshot: responseSnapshot
  };
}

async function sendCausalPerformanceSnapshot(currentPage, label) {
  const result = await sendGuiCommandWithSnapshot(currentPage, "PERF?", "PERF");
  const causalSequenceEnd = requiredLogInteger(result.response, "frames", `${label} PERF`);
  const latestObservedSequence = Number(result.snapshot?.frame?.seq);
  return {
    ...result,
    causalSequenceEnd,
    excludedLiveTail: {
      startSequence: causalSequenceEnd + 1,
      endSequence: Number.isInteger(latestObservedSequence) ? latestObservedSequence : null,
      pendingGapCount: numeric(result.snapshot?.diagnostics?.pendingFirmwareEvidenceGap)
    }
  };
}

function causalPerformanceWindow(openingResponse, closingResult, label) {
  const startExclusive = requiredLogInteger(openingResponse, "frames", `${label} opening PERF`);
  const endInclusive = Number(closingResult?.causalSequenceEnd);
  if (!Number.isInteger(endInclusive) || endInclusive < startExclusive) {
    throw new Error(
      `${label} invalid PERF causal window: start=${startExclusive}, end=${endInclusive}`
    );
  }
  return {
    startExclusive,
    endInclusive,
    excludedLiveTail: closingResult.excludedLiveTail
  };
}

async function submitGuiCommand(currentPage, command) {
  const input = currentPage.getByPlaceholder("Enter command text");
  await input.fill(command);
  await currentPage.getByRole("button", { name: "Send", exact: true }).click();
  await currentPage.locator(".commandRecords").waitFor({ state: "visible" });
}

async function forceUncappedFirmwarePipeline(currentPage) {
  await submitGuiCommand(currentPage, "FPSCAP=OFF");
  await submitGuiCommand(currentPage, "OUTCAP=OFF");
  const deadline = Date.now() + 12_000;
  let response = null;
  while (Date.now() < deadline) {
    response = await sendGuiCommand(currentPage, "FPS?", "FPS");
    if (Number(response?.parsedFields?.cfcap) === 0 && Number(response?.parsedFields?.ofcap) === 0) {
      return { action: "uncappedFirmwarePipeline", captureFpsCap: 0, outputFpsCap: 0, response };
    }
    await wait(250);
  }
  throw new Error(`Firmware did not apply FPSCAP=OFF and OUTCAP=OFF: ${JSON.stringify(response)}`);
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
  await heatmap.scrollIntoViewIfNeeded();
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
  await poll(async () => {
    // A scheduled ECharts setOption can hide an existing tooltip. Re-enter
    // the same cell on every bounded poll so a live high-rate renderer is
    // tested without relying on one race-prone mousemove.
    await currentPage.mouse.move(box.x + 2, box.y + 2);
    await currentPage.mouse.move(x, y);
    const bodyText = (await currentPage.locator("body").textContent()) || "";
    const modeSpecificText = mode === "RES" ? "Raw integer m\u03A9" : mode === "VOLT" ? "Raw integer \u00B5V" : "Raw pF";
    return bodyText.includes(cell) && bodyText.includes(`Mode: ${mode}`) && bodyText.includes(modeSpecificText);
  }, 10_000, `${mode} ${cell} tooltip`);
  await currentPage.mouse.click(x, y);
  const selected = await waitForSnapshot(
    currentPage,
    (candidate) => Array.isArray(candidate.selection?.cells) && candidate.selection.cells.includes(cell),
    8_000,
    `${mode} ${cell} selection`
  );
  const trendHeader = ((await currentPage.locator(".trendPanel .panelHeader").first().textContent()) || "").trim();
  const expectedQuantity = mode === "RES" ? "Resistance" : mode === "VOLT" ? "Voltage" : "Capacitance";
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

async function verifyResistanceFixture(currentPage) {
  const current = await backendStatus(currentPage);
  if (!isCompleteFreshMode(current, "RES") || Number(current.frame?.rows) !== 8) {
    throw new Error(`Resistance fixture requires a complete fresh 8x8 RES frame: ${JSON.stringify(compactSnapshot(current))}`);
  }
  const anchors = [
    { cell: "S1D1", row: 0, col: 0, minimum: 5_000, maximum: 20_000 },
    { cell: "S8D8", row: 7, col: 7, minimum: 5_000, maximum: 20_000 },
    { cell: "S3D3", row: 2, col: 2, minimum: 800, maximum: 3_500 }
  ].map((anchor) => ({ ...anchor, value: Number(current.matrix?.values?.[anchor.row]?.[anchor.col]) }));
  for (const anchor of anchors) {
    if (
      !Number.isFinite(anchor.value) ||
      anchor.value < anchor.minimum ||
      anchor.value > anchor.maximum ||
      current.matrix?.expected?.[anchor.row]?.[anchor.col] !== true ||
      current.matrix?.acquired?.[anchor.row]?.[anchor.col] !== true ||
      current.matrix?.fresh?.[anchor.row]?.[anchor.col] !== true ||
      current.matrix?.valid?.[anchor.row]?.[anchor.col] !== true ||
      current.matrix?.error?.[anchor.row]?.[anchor.col] === true
    ) {
      throw new Error(`Resistance fixture anchor failed: ${JSON.stringify(anchor)}`);
    }
  }
  const invalidCells = [];
  for (let row = 0; row < 8; row += 1) {
    for (let col = 0; col < 8; col += 1) {
      const errorCode = Number(current.matrix?.errorCodes?.[row]?.[col]);
      if (errorCode !== 0x08 && errorCode !== 0x0d) continue;
      const evidence = {
        cell: `S${row + 1}D${col + 1}`,
        row,
        col,
        errorCode,
        value: current.matrix?.values?.[row]?.[col],
        expected: current.matrix?.expected?.[row]?.[col],
        acquired: current.matrix?.acquired?.[row]?.[col],
        fresh: current.matrix?.fresh?.[row]?.[col],
        valid: current.matrix?.valid?.[row]?.[col],
        error: current.matrix?.error?.[row]?.[col]
      };
      if (
        evidence.value !== null ||
        evidence.expected !== true ||
        evidence.acquired !== true ||
        evidence.fresh !== true ||
        evidence.valid !== false ||
        evidence.error !== true
      ) {
        throw new Error(`OPEN fixture cell lost acquired/fresh/invalid semantics: ${JSON.stringify(evidence)}`);
      }
      invalidCells.push({
        ...evidence,
        classification: errorCode === 0x0d ? "OPEN" : "SATURATED"
      });
    }
  }
  const openCells = invalidCells.filter((cell) => cell.errorCode === 0x0d);
  if (!openCells.length) {
    throw new Error("Resistance fixture exposed no authoritative X0D OPEN cells");
  }

  const invalid = invalidCells[0];
  const heatmap = currentPage.locator(".heatmapCanvas");
  await heatmap.scrollIntoViewIfNeeded();
  const box = await heatmap.boundingBox();
  if (!box) throw new Error("Resistance fixture heatmap has no visible bounding box");
  const x = box.x + 64 + ((invalid.col + 0.5) * (box.width - 64 - 28)) / 8;
  const y = box.y + 28 + ((invalid.row + 0.5) * (box.height - 28 - 72)) / 8;
  const expectedError = `X${invalid.errorCode.toString(16).toUpperCase().padStart(2, "0")}`;
  await poll(async () => {
    await currentPage.mouse.move(box.x + 2, box.y + 2);
    await currentPage.mouse.move(x, y);
    const bodyText = (await currentPage.locator("body").textContent()) || "";
    return bodyText.includes(invalid.cell) && bodyText.includes(expectedError) && bodyText.includes("Acquired: yes") && bodyText.includes("Fresh: yes") && bodyText.includes("Valid: no");
  }, 10_000, `${invalid.cell} ${invalid.classification} tooltip semantics`);
  await screenshot(currentPage, "HIL_T00_resistance_fixture_open.png");
  return {
    action: "resistanceFixture",
    anchors,
    openCount: openCells.length,
    saturatedCount: invalidCells.filter((cell) => cell.errorCode === 0x08).length,
    invalidSample: invalid,
    tooltipError: expectedError
  };
}

function resistanceFixtureAnchorsInRange(snapshot) {
  if (!isCompleteFreshMode(snapshot, "RES") || Number(snapshot.frame?.rows) !== 8) {
    return false;
  }
  return [
    { row: 0, col: 0, minimum: 5_000, maximum: 20_000 },
    { row: 7, col: 7, minimum: 5_000, maximum: 20_000 },
    { row: 2, col: 2, minimum: 800, maximum: 3_500 }
  ].every(({ row, col, minimum, maximum }) => {
    const value = Number(snapshot.matrix?.values?.[row]?.[col]);
    return Number.isFinite(value) && value >= minimum && value <= maximum &&
      snapshot.matrix?.expected?.[row]?.[col] === true &&
      snapshot.matrix?.acquired?.[row]?.[col] === true &&
      snapshot.matrix?.fresh?.[row]?.[col] === true &&
      snapshot.matrix?.valid?.[row]?.[col] === true &&
      snapshot.matrix?.error?.[row]?.[col] !== true;
  });
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
    parserErrors: rows.filter((row) => row.tag === "PARSER").slice(-100).map(compactLog),
    criticalLogs: rows.filter((row) =>
      row.severity === "error" ||
      ["MFAULT", "ADSFAULT", "FERR", "BATERR", "MERR", "RMERR", "RERR"].includes(row.tag)
    ).slice(-100).map(compactLog),
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
  // PERF is cumulative. A snapshot on each side lets the backend reconcile
  // non-fresh frames in the unfinished SF50 window before judging sequence
  // integrity.
  const performanceStart = await sendGuiCommandWithSnapshot(currentPage, "PERF?", "PERF");
  const performanceBefore = performanceStart.response;
  const startedAt = new Date().toISOString();
  const started = Date.now();
  const first = performanceStart.snapshot;
  const revisions = new Set([first.frame.revision]);
  const sequences = new Set([first.frame.seq]);
  let minimumValid = activeCellsValid(first);
  let minimumFresh = activeCellsFresh(first);
  let last = first;
  let lastProgressRevision = Number(first.frame?.revision);
  let lastProgressAt = Date.now();
  const memorySamples = [await rendererMemorySample()];
  while (Date.now() - started < durationMs) {
    await wait(Math.min(1_000, durationMs - (Date.now() - started)));
    last = await backendStatus(currentPage);
    if (!predicate(last)) {
      throw new Error(`${label} violated sustained-run predicate at ${new Date().toISOString()}: ${JSON.stringify(compactSnapshot(last))}`);
    }
    const fatalLogs = logsSince(last, started, ["MFAULT", "APP_FATAL", "ADSFAULT"]);
    if (fatalLogs.length) {
      throw new Error(`${label} observed a firmware runtime fault: ${JSON.stringify(fatalLogs.at(-1))}`);
    }
    const currentRevision = Number(last.frame?.revision);
    if (Number.isFinite(currentRevision) && currentRevision > lastProgressRevision) {
      lastProgressRevision = currentRevision;
      lastProgressAt = Date.now();
    } else if (Date.now() - lastProgressAt > 10_000) {
      throw new Error(
        `${label} frame stream stalled for more than 10 seconds at revision ${lastProgressRevision}: ${JSON.stringify(compactStallEvidence(last))}`
      );
    }
    revisions.add(last.frame.revision);
    sequences.add(last.frame.seq);
    minimumValid = Math.min(minimumValid, activeCellsValid(last));
    minimumFresh = Math.min(minimumFresh, activeCellsFresh(last));
    memorySamples.push(await rendererMemorySample());
  }
  const performanceEnd = await sendCausalPerformanceSnapshot(currentPage, label);
  const performanceAfter = performanceEnd.response;
  last = performanceEnd.snapshot;
  if (last.frame.revision <= first.frame.revision || sequences.size < 2) {
    throw new Error(`${label} did not continuously update frames`);
  }
  const parserEvidence = assertNoParserCorruption(first.diagnostics, last.diagnostics, label);
  const sequenceEvidence = assertNoUnexplainedSequenceLoss(first.diagnostics, last.diagnostics, label);
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
    firmwarePerformance: { before: performanceBefore, after: performanceAfter },
    causalWindow: causalPerformanceWindow(performanceBefore, performanceEnd, label),
    memoryEvidence,
    parserEvidence,
    sequenceEvidence,
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
  ) && isCurrentAuthoritativeFrame(snapshot) && snapshot.measurement.appliedMode === "CAP" && snapshot.matrix.quantity === "capacitance" && snapshot.frame.rows === 8 && snapshot.frame.valid && activeCellsValid(snapshot) === 64 && activeCellsFresh(snapshot) === 64;
}

function isCompleteFreshCapAnyRows(snapshot) {
  const active = Math.max(1, Math.min(8, Number(snapshot.frame?.rows) || 0)) * 8;
  return (
    ["serial", "ble"].includes(snapshot.connection.mode) &&
    isCurrentAuthoritativeFrame(snapshot) &&
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
    isCurrentAuthoritativeFrame(snapshot) &&
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
    ["serial", "ble"].includes(snapshot.connection.mode) &&
    isCurrentAuthoritativeFrame(snapshot) &&
    snapshot.frame.layout === "MIXED" &&
    snapshot.frame.rowModes?.join("") === modes.join("") &&
    snapshot.measurement?.rowProfile?.pendingModes === null &&
    snapshot.measurement?.rowProfile?.appliedModes?.join("") === modes.join("") &&
    snapshot.frame.valid &&
    activeCellsFresh(snapshot) === snapshot.frame.rows * 8
  );
}

function matchesMeasurementState(snapshot, expectedSnapshot) {
  if (Number(snapshot.frame?.rows) !== Number(expectedSnapshot.frame?.rows)) {
    return false;
  }
  const expectedModes = expectedSnapshot.measurement?.rowProfile?.appliedModes || [];
  if (expectedSnapshot.frame?.layout === "MIXED") {
    const profile = expectedModes.map((mode) => mode === "CAP" ? "C" : mode === "VOLT" ? "V" : "R").join("");
    return profile.length === 8 && isCompleteFreshMixed(snapshot, profile);
  }
  const mode = expectedSnapshot.measurement?.appliedMode;
  return ["CAP", "VOLT", "RES"].includes(mode) && isCompleteFreshMode(snapshot, mode);
}

function isCurrentAuthoritativeFrame(snapshot) {
  const connectionGeneration = Number(snapshot.connection?.connectionGeneration);
  const frameConnectionGeneration = Number(snapshot.matrix?.connectionGeneration);
  return (
    ["connected", "streaming"].includes(snapshot.connection?.state) &&
    snapshot.matrix?.sourceTransport === snapshot.connection?.mode &&
    snapshot.frame?.quarantinedReason === "" &&
    snapshot.matrix?.quarantinedReason === "" &&
    Number.isInteger(connectionGeneration) &&
    connectionGeneration > 0 &&
    frameConnectionGeneration === connectionGeneration
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
    device: snapshot.device,
    bootstrap: snapshot.bootstrap,
    usbStream: snapshot.usbStream,
    fdcIsolation: snapshot.fdcIsolation,
    calibration: snapshot.calibration,
    battery: snapshot.battery,
    rail: snapshot.rail,
    voltage: snapshot.voltage,
    recording: snapshot.recording,
    performance: snapshot.performance,
    commands: snapshot.commands,
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

function compactStallEvidence(snapshot) {
  return {
    connection: snapshot.connection,
    device: {
      bootId: snapshot.device?.bootId,
      ready: snapshot.device?.ready,
      stage: snapshot.device?.stage,
      lastError: snapshot.device?.lastError
    },
    frame: {
      seq: snapshot.frame?.seq,
      revision: snapshot.frame?.revision,
      layout: snapshot.frame?.layout,
      rowModes: snapshot.frame?.rowModes,
      valid: snapshot.frame?.valid,
      quarantinedReason: snapshot.frame?.quarantinedReason
    },
    latestCommand: snapshot.commands?.latestCommand,
    diagnostics: {
      parserFrames: snapshot.diagnostics?.parserFrames,
      parserRejects: snapshot.diagnostics?.parserRejects,
      crcFailures: snapshot.diagnostics?.crcFailures,
      hostTransportDrop: snapshot.diagnostics?.hostTransportDrop,
      hostUnexplainedSequenceGap: snapshot.diagnostics?.hostUnexplainedSequenceGap
    }
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
  // Sequence accounting shares rejectsByReason for backwards-compatible UI
  // visibility, but does not increment parserRejects. Keep parser integrity
  // tied to CRC and actual parser rejects. A final stale callback during a
  // tested disconnect/reconnect is an intentional safety rejection.
  const accountingReasons = new Set([
    "unknown_sequence_gap",
    "debug_unexplained_sequence_gap",
    "non_monotonic_sequence",
    "firmware_suppressed_non_fresh",
    "firmware_transport_drop_gap"
  ]);
  const disallowedRejects = Object.fromEntries(
    Object.entries(rejectDeltas).filter(
      ([reason]) => reason !== "stale_session_generation" && !accountingReasons.has(reason)
    )
  );
  const allowedStaleDelta = numeric(rejectDeltas.stale_session_generation);
  const disallowedParserRejectDelta = Math.max(0, parserRejectDelta - allowedStaleDelta);
  if (crcDelta !== 0 || disallowedParserRejectDelta !== 0 || Object.keys(disallowedRejects).length) {
    throw new Error(
      `${label} parser corruption counters changed: crc=${crcDelta}, parserRejects=${disallowedParserRejectDelta}, rejects=${JSON.stringify(disallowedRejects)}`
    );
  }
  return {
    action: "parserIntegrity",
    label,
    crcDelta,
    parserRejectDelta,
    disallowedParserRejectDelta,
    rejectDeltas,
    disallowedRejects
  };
}

function assertReconnectBoundaryIntegrity(before, after, requiredReconnects) {
  const evidence = {
    action: "reconnectBoundaryIntegrity",
    requiredReconnects,
    reconnectDelta: numeric(after?.reconnectCount) - numeric(before?.reconnectCount),
    observedSequenceGapDelta: numeric(after?.observedSequenceGapFrames) - numeric(before?.observedSequenceGapFrames),
    firmwareSuppressedDelta: numeric(after?.firmwareSuppressedNonFresh) - numeric(before?.firmwareSuppressedNonFresh),
    firmwareDropDelta: numeric(after?.firmwareTransportDrop) - numeric(before?.firmwareTransportDrop),
    boundaryUnexplainedDelta: numeric(after?.hostUnexplainedSequenceGap) - numeric(before?.hostUnexplainedSequenceGap),
    hostTransportDropDelta: numeric(after?.hostTransportDrop) - numeric(before?.hostTransportDrop),
    hostQueueDropDelta: numeric(after?.hostQueueDrops) - numeric(before?.hostQueueDrops),
    fragmentDropDelta: numeric(after?.fragmentDrops) - numeric(before?.fragmentDrops),
    note: "Sequence jumps inside deliberate disconnect/bootstrap/configuration windows are boundary evidence, not a lossless sustained-run interval. Every connection epoch separately requires bootstrap, FF11/FF30 ROWS completion, and a fresh FF20 frame."
  };
  if (
    evidence.reconnectDelta < requiredReconnects ||
    evidence.hostTransportDropDelta !== 0 ||
    evidence.hostQueueDropDelta !== 0 ||
    evidence.fragmentDropDelta !== 0
  ) {
    throw new Error(`BLE reconnect boundary integrity failed: ${JSON.stringify(evidence)}`);
  }
  return evidence;
}

function assertNoUnexplainedSequenceLoss(before, after, label) {
  const unknownDelta =
    numeric(after?.hostUnexplainedSequenceGap) - numeric(before?.hostUnexplainedSequenceGap);
  const expectedOutputDelta =
    numeric(after?.expectedOutputDecimation) - numeric(before?.expectedOutputDecimation);
  const intentionalDelta =
    numeric(after?.intentionalFirmwareDecimation) - numeric(before?.intentionalFirmwareDecimation);
  const firmwareSuppressedDelta =
    numeric(after?.firmwareSuppressedNonFresh) - numeric(before?.firmwareSuppressedNonFresh);
  const firmwareDropDelta =
    numeric(after?.firmwareReportedDrop) - numeric(before?.firmwareReportedDrop);
  const hostDropDelta = numeric(after?.hostTransportDrop) - numeric(before?.hostTransportDrop);
  const pendingFirmwareEvidenceDelta =
    numeric(after?.pendingFirmwareEvidenceGap) - numeric(before?.pendingFirmwareEvidenceGap);
  if (unknownDelta > 0) {
    throw new Error(
      `${label} retained ${unknownDelta} host-unexplained sequence gaps after SF50/PERF reconciliation`
    );
  }
  return {
    action: "sequenceIntegrity",
    label,
    unknownDelta,
    expectedOutputDelta,
    intentionalDelta,
    firmwareSuppressedDelta,
    firmwareDropDelta,
    hostDropDelta,
    pendingFirmwareEvidenceDelta
  };
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

async function exportDiagnosticSession(currentPage, name) {
  try {
    const response = await currentPage.request.get(`${backendUrl}/api/export/session?format=zip`);
    if (!response.ok()) {
      return { ok: false, error: `${response.status()} ${await response.text()}` };
    }
    const outputPath = path.join(artifactRoot, name);
    writeFileSync(outputPath, await response.body());
    return { ok: true, path: outputPath };
  } catch (error) {
    return { ok: false, error: errorText(error) };
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
