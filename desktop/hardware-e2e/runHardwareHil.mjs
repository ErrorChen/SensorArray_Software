import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { _electron as electron } from "@playwright/test";

let backendUrl = "";
const serialPort = process.env.SENSORARRAY_HIL_SERIAL_PORT || "COM12";
const minimumRunMs = Number(process.env.SENSORARRAY_HIL_RUN_MS || 30_500);
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
  headedElectron: true,
  voltage: {
    status: "BLOCKED",
    reason: "No AVDD/AVSS values measured by the user with a DMM in this validation run; no historical ADS rail values were used."
  },
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
  await runSerialAcceptance(page);
  await runBleAcceptance(page);
  await runWifiSmoke(page);
} catch (error) {
  report.fatalError = errorText(error);
} finally {
  report.finishedAt = new Date().toISOString();
  const finalStatus = await safeStatus(page);
  if (finalStatus) {
    report.finalSnapshot = compactSnapshot(finalStatus);
  }
  writeFileSync(path.join(artifactRoot, "hardware_gui_hil.json"), JSON.stringify(report, null, 2), "utf8");
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
    snapshot = await waitForSnapshot(currentPage, isCompleteFreshCap, 40_000, "complete fresh CAP frame after serial attach");
    result.observations.push({ action: "connected", snapshot: compactSnapshot(snapshot), cellHealth: cellHealth(snapshot) });
    result.observations.push(await exerciseCellInspection(currentPage, "CAP", 0, 4));
    await screenshot(currentPage, "HIL_S01_serial_cap_initial.png");

    result.observations.push(await observeSustainedRun(currentPage, "serial CAP initial", minimumRunMs, isCompleteFreshCap));
    result.observations.push(await exerciseRawLogAndStatus(currentPage, "serial"));

    for (const rows of [1, 2, 4, 8]) {
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
    await screenshot(currentPage, "HIL_S02_serial_rows_1_2_4_8.png");

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
    if (crcDelta !== 0) {
      throw new Error(`Serial produced ${crcDelta} CRC failure(s)`);
    }
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
    const initialCap = await waitForSnapshot(currentPage, isCompleteFreshCap, 45_000, "BLE complete fresh CAP frame");
    result.observations.push({ action: "connected", snapshot: compactSnapshot(initialCap), cellHealth: cellHealth(initialCap), gatt: recentLogs(initialCap, ["Transport"], 8) });
    result.observations.push(await exerciseCellInspection(currentPage, "CAP", 0, 4));
    await screenshot(currentPage, "HIL_B02_ble_cap.png");
    result.observations.push(await observeSustainedRun(currentPage, "BLE CAP", minimumRunMs, isCompleteFreshCap));
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

    const resRequestStart = Date.now();
    const dropsBefore = numeric((await backendStatus(currentPage)).diagnostics.wrongModeDrops);
    await currentPage.getByTestId("measurement-mode-control").getByRole("button", { name: "RES", exact: true }).click();
    const pendingOrTimeout = await waitForSnapshot(
      currentPage,
      (candidate) => ["accepted", "timeout", "error"].includes(candidate.measurement.transitionState),
      15_000,
      "BLE mode transaction response"
    );
    await screenshot(currentPage, "HIL_B04_ble_res_pending.png");
    await wait(minimumRunMs);
    const afterResRun = await backendStatus(currentPage);
    const modeReplies = logsSince(afterResRun, resRequestStart, ["MACK", "MAPP", "MODE", "FRAME_DROP", "BLE_RX50", "BLE_FRAG50", "PROTO50"]);
    result.observations.push({
      action: "bleResAttempt",
      initialTransition: compactSnapshot(pendingOrTimeout),
      afterMinimumRun: compactSnapshot(afterResRun),
      wrongModeDropDelta: numeric(afterResRun.diagnostics.wrongModeDrops) - dropsBefore,
      relevantLogs: modeReplies
    });

    // Pure BLE currently receives MACK over FF11 but firmware emits the
    // asynchronous MAPP terminal event only on Serial stdout. Strict host
    // semantics therefore keep RES pending/timeout and reject R frames. This
    // is recorded as a real cross-repository blocker, never as a GUI PASS.
    const mappSeen = modeReplies.some((row) => row.tag === "MAPP");
    if (!mappSeen || afterResRun.measurement.appliedMode !== "RES") {
      result.status = "BLOCKED";
      result.errors.push("BLE CAP/data/control worked, but strict RES GUI acceptance is blocked because no matching MAPP terminal event arrived on FF11/FF30.");
    }

    // Restore physical firmware to CAP through FF10. The UI may retain a
    // timeout because the matching MAPP is likewise unavailable on pure BLE.
    await currentPage.getByTestId("measurement-mode-control").getByRole("button", { name: "CAP", exact: true }).click();
    await waitForSnapshot(currentPage, (candidate) => candidate.matrix.quantity === "capacitance" && candidate.frame.rows === 8, 40_000, "BLE physical CAP return");
    const capState = await sendGuiCommand(currentPage, "STATE?", "MODE");
    result.observations.push({ action: "bleCapReturn", stateResponse: capState, snapshot: compactSnapshot(await backendStatus(currentPage)) });
    await screenshot(currentPage, "HIL_B05_ble_cap_return.png");
    result.observations.push(await observeSustainedRun(currentPage, "BLE CAP return", minimumRunMs, isCompleteFreshCap));

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
    result.status = result.observations.length ? "FAIL" : "BLOCKED";
    result.errors.push(errorText(error));
    result.failureEvidence = await collectFailureEvidence(currentPage, "ble", error);
    await screenshot(currentPage, "HIL_B99_ble_failure.png");
  } finally {
    await disconnectIfConnected(currentPage);
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
  const gridHeight = box.height - 28 - 52;
  const x = box.x + 64 + ((col + 0.5) * gridWidth) / 8;
  const y = box.y + 28 + ((row + 0.5) * gridHeight) / 8;
  const cell = `S${row + 1}D${col + 1}`;
  await currentPage.mouse.move(x, y);
  await poll(async () => {
    const bodyText = (await currentPage.locator("body").textContent()) || "";
    const modeSpecificText = mode === "RES" ? "Raw integer mΩ" : "Raw pF";
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
  }
  if (last.frame.revision <= first.frame.revision || sequences.size < 2) {
    throw new Error(`${label} did not continuously update frames`);
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
    startDiagnostics: first.diagnostics,
    endDiagnostics: last.diagnostics
  };
}

function isCompleteFreshCap(snapshot) {
  return (
    snapshot.connection.mode === "serial" || snapshot.connection.mode === "ble"
  ) && ["connected", "streaming"].includes(snapshot.connection.state) && snapshot.measurement.appliedMode === "CAP" && snapshot.matrix.quantity === "capacitance" && snapshot.frame.rows === 8 && snapshot.frame.valid && activeCellsValid(snapshot) === 64 && activeCellsFresh(snapshot) === 64;
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
