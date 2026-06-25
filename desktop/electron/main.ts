import type { BrowserWindow as BrowserWindowType, OpenDialogOptions } from "electron";
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { BackendProcess, findAvailablePort, startBackend, stopBackend, waitForHealth } from "./backendProcess.js";

const nodeRequire = createRequire(import.meta.url);
const { app, BrowserWindow, dialog, ipcMain } = nodeRequire("electron") as typeof import("electron");
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const projectRoot = path.resolve(__dirname, "..", "..");
const appId = "au.edu.sydney.sensorarray";

let backend: BackendProcess | null = null;
let mainWindow: BrowserWindowType | null = null;

app.setAppUserModelId(appId);

ipcMain.handle("backend:url", () => backend?.url ?? "http://127.0.0.1:8765");
ipcMain.handle("dialog:selectReplayFile", async () => {
  const options: OpenDialogOptions = {
    title: "Open replay file",
    properties: ["openFile"],
    filters: [
      { name: "SensorArray logs", extensions: ["txt", "log", "json", "bin"] },
      { name: "All files", extensions: ["*"] }
    ]
  };
  const result = mainWindow ? await dialog.showOpenDialog(mainWindow, options) : await dialog.showOpenDialog(options);
  if (result.canceled || result.filePaths.length === 0) {
    return null;
  }
  return result.filePaths[0];
});

async function createWindow(): Promise<void> {
  const port = await findAvailablePort(8765);
  backend = startBackend(projectRoot, port);
  try {
    await waitForHealth(backend.url);
  } catch (error) {
    await showBackendError(error);
    return;
  }

  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1180,
    minHeight: 760,
    title: "SensorArray",
    icon: resolveIconPath(),
    backgroundColor: "#f7f8fa",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  const devUrl = process.env.SENSORARRAY_FRONTEND_URL;
  if (devUrl) {
    await mainWindow.loadURL(`${devUrl}?backendUrl=${encodeURIComponent(backend.url)}`);
  } else {
    await mainWindow.loadFile(path.join(projectRoot, "desktop", "dist", "index.html"), {
      query: { backendUrl: backend.url }
    });
  }
}

async function showBackendError(error: unknown): Promise<void> {
  const detail = error instanceof Error ? error.message : String(error);
  const stderr = backend?.stderr.join("").slice(-4000) ?? "";
  mainWindow = new BrowserWindow({
    width: 900,
    height: 560,
    title: "SensorArray backend error",
    icon: resolveIconPath()
  });
  const body = escapeHtml(`${detail}\n\n${stderr}`);
  await mainWindow.loadURL(
    `data:text/html;charset=utf-8,${encodeURIComponent(`<pre style="white-space:pre-wrap;font:13px Consolas;padding:24px">${body}</pre>`)}`
  );
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => {
    const map: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
    return map[char];
  });
}

function resolveIconPath(): string | undefined {
  const candidates = app.isPackaged
    ? [
        path.join(process.resourcesPath, "assets", "icons", "sensorarray-icon.ico"),
        path.join(process.resourcesPath, "sensorarray-icon.ico")
      ]
    : [
        path.join(projectRoot, "desktop", "assets", "icons", "sensorarray-icon.ico"),
        path.join(projectRoot, "desktop", "public", "favicon.ico")
      ];
  return candidates.find((candidate) => fs.existsSync(candidate));
}

app.whenReady().then(createWindow).catch((error) => {
  dialog.showErrorBox("SensorArray failed to start", error instanceof Error ? error.message : String(error));
});

app.on("window-all-closed", () => {
  stopBackend(backend);
  backend = null;
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  stopBackend(backend);
  backend = null;
});
