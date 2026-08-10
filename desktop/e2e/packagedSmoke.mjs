import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { _electron as electron } from "@playwright/test";

const e2eDirectory = path.dirname(fileURLToPath(import.meta.url));
const desktopRoot = path.resolve(e2eDirectory, "..");
const repoRoot = path.resolve(desktopRoot, "..");
const executablePath = path.join(desktopRoot, "release", "win-unpacked", "SensorArray.exe");
const artifactRoot = path.join(repoRoot, "validation_artifacts", "package");
const userDataRoot = path.join(artifactRoot, "electron-user-data");
const screenshotPath = path.join(artifactRoot, "win_unpacked_electron.png");
const evidencePath = path.join(artifactRoot, "win_unpacked_smoke.json");

if (!existsSync(executablePath)) {
  throw new Error(`Packaged Electron executable is missing: ${executablePath}`);
}

mkdirSync(userDataRoot, { recursive: true });
const userDataDirectory = mkdtempSync(path.join(userDataRoot, "smoke-"));
const environment = Object.fromEntries(
  Object.entries(process.env).filter(([, value]) => typeof value === "string")
);
delete environment.SENSORARRAY_FRONTEND_URL;
delete environment.ELECTRON_RUN_AS_NODE;

const evidence = {
  status: "FAIL",
  startedAt: new Date().toISOString(),
  executablePath,
  rendererUrl: "",
  backendUrl: "",
  runtimeDirectory: "",
  preloadBridgePresent: false,
  health: null,
  workspace: {},
  consoleErrors: [],
  pageErrors: []
};

let electronApplication;
try {
  electronApplication = await electron.launch({
    executablePath,
    args: [`--user-data-dir=${userDataDirectory}`],
    cwd: path.dirname(executablePath),
    env: environment
  });
  const page = await electronApplication.firstWindow();
  page.on("console", (message) => {
    if (message.type() === "error") {
      evidence.consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => evidence.pageErrors.push(String(error)));

  await page.getByTestId("measurement-mode-control").waitFor({ state: "visible", timeout: 45_000 });
  evidence.rendererUrl = page.url();
  if (!evidence.rendererUrl.startsWith("file:")) {
    throw new Error(`Packaged renderer did not load from a local file URL: ${evidence.rendererUrl}`);
  }

  const bridge = await page.evaluate(async () => {
    const desktopWindow = globalThis;
    const api = desktopWindow.sensorarrayDesktop;
    return {
      present: Boolean(api),
      backendUrl: await api?.getBackendUrl?.(),
      runtimeDirectory: await api?.getRuntimeDirectory?.()
    };
  });
  evidence.preloadBridgePresent = bridge.present;
  evidence.backendUrl = String(bridge.backendUrl || "");
  evidence.runtimeDirectory = String(bridge.runtimeDirectory || "");
  if (!evidence.preloadBridgePresent) {
    throw new Error("window.sensorarrayDesktop preload bridge is missing in the packaged application");
  }
  if (!/^http:\/\/127\.0\.0\.1:8(?:8\d\d|9[0-8]\d)$/.test(evidence.backendUrl)) {
    throw new Error(`Packaged backend URL is not a local dynamic sidecar port: ${evidence.backendUrl}`);
  }

  const healthResponse = await page.request.get(`${evidence.backendUrl}/health`);
  if (!healthResponse.ok()) {
    throw new Error(`Packaged backend health returned HTTP ${healthResponse.status()}`);
  }
  evidence.health = await healthResponse.json();
  evidence.workspace = {
    heatmap: await page.getByText(/8x8 .* Heatmap/).first().isVisible(),
    setup: await page.getByRole("tab", { name: "Setup" }).isVisible(),
    advanced: await page.getByRole("tab", { name: "Advanced" }).isVisible(),
    command: await page.getByText("Write / Command", { exact: true }).isVisible(),
    rawLog: await page.getByRole("button", { name: "Raw Log", exact: true }).isVisible(),
    status: await page.getByRole("button", { name: "Status", exact: true }).isVisible()
  };
  if (Object.values(evidence.workspace).some((visible) => !visible)) {
    throw new Error(`Packaged workspace is incomplete: ${JSON.stringify(evidence.workspace)}`);
  }

  await page.screenshot({ path: screenshotPath, fullPage: true });
  await page.waitForTimeout(1_000);
  if (evidence.consoleErrors.length || evidence.pageErrors.length) {
    throw new Error(`Packaged renderer errors: ${JSON.stringify({ console: evidence.consoleErrors, page: evidence.pageErrors })}`);
  }
  evidence.status = "PASS";
} catch (error) {
  evidence.error = String(error instanceof Error ? error.stack || error.message : error);
  throw error;
} finally {
  evidence.finishedAt = new Date().toISOString();
  mkdirSync(artifactRoot, { recursive: true });
  writeFileSync(evidencePath, `${JSON.stringify(evidence, null, 2)}\n`, "utf8");
  if (electronApplication) {
    await electronApplication.close();
  }
  if (path.dirname(userDataDirectory) === userDataRoot) {
    rmSync(userDataDirectory, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  }
}

console.log(`PACKAGED_ELECTRON_SMOKE,status=${evidence.status},renderer=${evidence.rendererUrl},backend=${evidence.backendUrl}`);
