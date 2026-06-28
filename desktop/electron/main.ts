import type {
  BrowserWindow as BrowserWindowType,
  MenuItemConstructorOptions,
  OpenDialogOptions,
  OpenDialogReturnValue,
  SaveDialogOptions,
  SaveDialogReturnValue
} from "electron";
import electron from "electron";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import type { BackendProcess } from "./backendProcess.js";
import { startBackendWithFirstHealthyPort, stopBackend } from "./backendProcess.js";
import { buildBackendPortCandidates, defaultBackendHost } from "./backendPortPolicy.js";

const { app, BrowserWindow, Menu, dialog, ipcMain } = electron;
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "..", "..");
const appId = "au.edu.sydney.sensorarray";
const backendPortCandidates = buildBackendPortCandidates();

let backend: BackendProcess | null = null;
let mainWindow: BrowserWindowType | null = null;
let defaultSaveDirectory = resolveRuntimeDirectory();

app.setAppUserModelId(appId);
app.commandLine.appendSwitch("explicitly-allowed-ports", backendPortCandidates.join(","));

ipcMain.handle("backend:url", () => {
  if (!backend) {
    throw new Error("Backend is not ready");
  }
  return backend.url;
});
ipcMain.handle("runtime:directory", () => resolveRuntimeDirectory());
ipcMain.handle("paths:getDefaultSaveDirectory", () => defaultSaveDirectory);
ipcMain.handle("paths:setDefaultSaveDirectory", async (_event, directory: string) => {
  const check = await checkDirectoryWritable(directory);
  defaultSaveDirectory = check.path || String(directory || "");
  return check;
});
ipcMain.handle("paths:selectDefaultSaveDirectory", async () => {
  const result = await showOpenDialog({
    title: "Choose default save directory",
    defaultPath: defaultSaveDirectory,
    properties: ["openDirectory", "createDirectory"]
  });
  if (!result || result.canceled || result.filePaths.length === 0) {
    return { ok: false, canceled: true };
  }
  const check = await checkDirectoryWritable(result.filePaths[0]);
  if (check.ok) {
    defaultSaveDirectory = check.path;
  }
  return check;
});
ipcMain.handle("paths:openDefaultSaveDirectory", async () => {
  const check = await checkDirectoryWritable(defaultSaveDirectory);
  if (!check.ok) {
    return check;
  }
  const error = await electron.shell.openPath(check.path);
  return error ? { ok: false, path: check.path, error } : { ok: true, path: check.path };
});
ipcMain.handle("dialog:selectReplayFile", async () => {
  const options: OpenDialogOptions = {
    title: "Open replay file",
    properties: ["openFile"],
    filters: [
      { name: "SensorArray replay data", extensions: ["json", "jsonl", "log", "txt", "csv", "bin"] },
      { name: "All files", extensions: ["*"] }
    ]
  };
  const result = await showOpenDialog(options);
  if (result.canceled || result.filePaths.length === 0) {
    return null;
  }
  return result.filePaths[0];
});
ipcMain.handle("dialog:selectSessionDataFile", async () => {
  const result = await showOpenDialog({
    title: "Import session data",
    defaultPath: defaultSaveDirectory,
    properties: ["openFile"],
    filters: [
      { name: "SensorArray session data", extensions: ["csv", "xlsx", "mat", "h5"] },
      { name: "All files", extensions: ["*"] }
    ]
  });
  if (result.canceled || result.filePaths.length === 0) {
    return null;
  }
  return result.filePaths[0];
});
ipcMain.handle("dialog:saveExportedSession", async (_event, defaultName: string, data: ArrayBuffer | Uint8Array | string) => {
  const options: SaveDialogOptions = {
    title: "Export current session data",
    defaultPath: path.join(defaultSaveDirectory, defaultName),
    filters: [
      { name: "SensorArray CSV", extensions: ["csv"] },
      { name: "SensorArray Excel workbook", extensions: ["xlsx"] },
      { name: "SensorArray MATLAB file", extensions: ["mat"] },
      { name: "SensorArray HDF5 file", extensions: ["h5"] },
      { name: "All files", extensions: ["*"] }
    ]
  };
  const result = mainWindow ? await dialog.showSaveDialog(mainWindow, options) : await dialog.showSaveDialog(options);
  if (result.canceled || !result.filePath) {
    return { ok: false, canceled: true };
  }
  try {
    const buffer = bufferFromIpcData(data);
    await fs.promises.writeFile(result.filePath, buffer);
    return { ok: true, path: result.filePath };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
});
ipcMain.handle("dialog:chooseSessionExportPath", async (_event, defaultName: string) => {
  const result = await showSaveDialog({
    title: "Export current session data",
    defaultPath: path.join(defaultSaveDirectory, defaultName),
    filters: [
      { name: "SensorArray CSV", extensions: ["csv"] },
      { name: "SensorArray Excel workbook", extensions: ["xlsx"] },
      { name: "SensorArray MATLAB file", extensions: ["mat"] },
      { name: "SensorArray HDF5 file", extensions: ["h5"] },
      { name: "All files", extensions: ["*"] }
    ]
  });
  if (result.canceled || !result.filePath) {
    return { ok: false, canceled: true };
  }
  return { ok: true, path: result.filePath };
});
ipcMain.handle("file:writeBinary", async (_event, filePath: string, data: ArrayBuffer | Uint8Array | string) => {
  try {
    const buffer = bufferFromIpcData(data);
    await fs.promises.writeFile(filePath, buffer);
    return { ok: true, path: filePath };
  } catch (error) {
    return { ok: false, path: filePath, error: error instanceof Error ? error.message : String(error) };
  }
});
ipcMain.handle("dialog:saveSetupProfile", async (_event, defaultName: string, data: string) => {
  const result = await showSaveDialog({
    title: "Export setup profile",
    defaultPath: path.join(defaultSaveDirectory, defaultName),
    filters: [
      { name: "SensorArray setup JSON", extensions: ["json"] },
      { name: "All files", extensions: ["*"] }
    ]
  });
  if (result.canceled || !result.filePath) {
    return { ok: false, canceled: true };
  }
  try {
    await fs.promises.writeFile(result.filePath, String(data), "utf-8");
    return { ok: true, path: result.filePath };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
});
ipcMain.handle("dialog:selectSetupProfile", async () => {
  const result = await showOpenDialog({
    title: "Import setup profile",
    defaultPath: defaultSaveDirectory,
    properties: ["openFile"],
    filters: [
      { name: "SensorArray setup JSON", extensions: ["json"] },
      { name: "All files", extensions: ["*"] }
    ]
  });
  if (result.canceled || result.filePaths.length === 0) {
    return null;
  }
  return result.filePaths[0];
});
ipcMain.handle("file:readText", async (_event, filePath: string) => fs.promises.readFile(filePath, "utf-8"));
ipcMain.handle("screenshot:capture", async () => captureScreenshot());

async function createWindow(): Promise<void> {
  try {
    backend = await startBackendWithFirstHealthyPort({
      projectRoot: repoRoot,
      host: defaultBackendHost,
      ports: backendPortCandidates,
      isPackaged: app.isPackaged,
      resourcesPath: process.resourcesPath
    });
  } catch (error) {
    await showBackendError(error);
    return;
  }

  const preloadPath = resolvePreloadPath();
  if (!fs.existsSync(preloadPath)) {
    const error = new Error(`Electron preload file was not found: ${preloadPath}`);
    stopBackend(backend);
    backend = null;
    await showBackendError(error);
    return;
  }

  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1000,
    minHeight: 680,
    title: "SensorArray",
    icon: resolveIconPath(),
    backgroundColor: "#f7f8fa",
    webPreferences: {
      preload: preloadPath,
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  const devUrl = app.isPackaged ? undefined : process.env.SENSORARRAY_FRONTEND_URL;
  try {
    if (devUrl) {
      await mainWindow.loadURL(`${devUrl}?backendUrl=${encodeURIComponent(backend.url)}`);
    } else {
      const indexPath = resolveRendererIndexPath();
      if (!fs.existsSync(indexPath)) {
        throw new Error(`Renderer index.html was not found: ${indexPath}`);
      }
      await mainWindow.loadFile(indexPath, {
        query: { backendUrl: backend.url }
      });
    }
  } catch (error) {
    stopBackend(backend);
    backend = null;
    throw error;
  }
  installApplicationMenu();
}

function installApplicationMenu(): void {
  const template: MenuItemConstructorOptions[] = [
    {
      label: "File",
      submenu: [
        {
          label: "Import Replay Data...",
          click: () => void importReplayDataFromMenu()
        },
        {
          label: "Import Session Data...",
          accelerator: "CmdOrCtrl+I",
          click: () => mainWindow?.webContents.send("menu:importSessionData")
        },
        {
          label: "Export Current Session Data...",
          accelerator: "CmdOrCtrl+E",
          click: () => mainWindow?.webContents.send("menu:exportSessionData")
        },
        {
          label: "Import Setup...",
          click: () => mainWindow?.webContents.send("menu:importSetupProfile")
        },
        {
          label: "Export Setup...",
          click: () => mainWindow?.webContents.send("menu:exportSetupProfile")
        },
        { type: "separator" },
        {
          label: "Screenshot / Capture",
          click: () => void captureScreenshotAndNotify()
        },
        { type: "separator" },
        process.platform === "darwin" ? { role: "close" } : { role: "quit", label: "Exit" }
      ]
    },
    {
      label: "Screenshot / Capture",
      submenu: [
        {
          label: "Capture Screenshot",
          accelerator: "CmdOrCtrl+Shift+S",
          click: () => void captureScreenshotAndNotify()
        }
      ]
    }
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

async function captureScreenshotAndNotify(): Promise<void> {
  const result = await captureScreenshot();
  mainWindow?.webContents.send("screenshot:result", result);
}

async function importReplayDataFromMenu(): Promise<void> {
  const options: OpenDialogOptions = {
    title: "Import replay data",
    properties: ["openFile"],
    filters: [
      { name: "SensorArray replay data", extensions: ["json", "jsonl", "log", "txt", "csv"] },
      { name: "All files", extensions: ["*"] }
    ]
  };
  const result = await showOpenDialog(options);
  if (!result.canceled && result.filePaths[0]) {
    mainWindow?.webContents.send("menu:importReplayData", result.filePaths[0]);
  }
}

async function captureScreenshot(): Promise<{ ok: boolean; path?: string; error?: string }> {
  if (!mainWindow) {
    return { ok: false, error: "No application window is available" };
  }
  const directory = await checkDirectoryWritable(defaultSaveDirectory);
  if (!directory.ok) {
    return { ok: false, error: directory.error };
  }
  try {
    const image = await mainWindow.webContents.capturePage();
    const filePath = await nextScreenshotPath(directory.path);
    await fs.promises.writeFile(filePath, image.toPNG());
    return { ok: true, path: filePath };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

async function nextScreenshotPath(directory: string): Promise<string> {
  const now = new Date();
  const baseName = `Screenshot_CscArray__${timestampForScreenshot(now)}`;
  const firstPath = path.join(directory, `${baseName}.png`);
  if (!fs.existsSync(firstPath)) {
    return firstPath;
  }
  for (let index = 1; index < 1000; index += 1) {
    const filePath = path.join(directory, `${baseName}_${String(index).padStart(3, "0")}.png`);
    if (!fs.existsSync(filePath)) {
      return filePath;
    }
  }
  throw new Error("Could not allocate a screenshot filename");
}

function timestampForScreenshot(date: Date): string {
  return `${date.getFullYear()}${pad2(date.getMonth() + 1)}${pad2(date.getDate())}_${pad2(date.getHours())}${pad2(date.getMinutes())}${pad2(date.getSeconds())}`;
}

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

function resolveRuntimeDirectory(): string {
  return app.isPackaged ? path.dirname(process.execPath) : repoRoot;
}

async function checkDirectoryWritable(directory: string): Promise<{ ok: true; path: string } | { ok: false; path: string; error: string }> {
  const target = path.resolve(String(directory || resolveRuntimeDirectory()));
  try {
    const stat = await fs.promises.stat(target);
    if (!stat.isDirectory()) {
      return { ok: false, path: target, error: "Path is not a directory" };
    }
    await fs.promises.access(target, fs.constants.W_OK);
    return { ok: true, path: target };
  } catch (error) {
    return { ok: false, path: target, error: error instanceof Error ? error.message : String(error) };
  }
}

async function showOpenDialog(options: OpenDialogOptions): Promise<OpenDialogReturnValue> {
  return mainWindow ? dialog.showOpenDialog(mainWindow, options) : dialog.showOpenDialog(options);
}

async function showSaveDialog(options: SaveDialogOptions): Promise<SaveDialogReturnValue> {
  return mainWindow ? dialog.showSaveDialog(mainWindow, options) : dialog.showSaveDialog(options);
}

async function showBackendError(error: unknown): Promise<void> {
  const detail = error instanceof Error ? error.message : String(error);
  const stderr = backend?.stderr.join("").slice(-4000) ?? "";
  const stdout = backend?.stdout.join("").slice(-2000) ?? "";
  mainWindow = new BrowserWindow({
    width: 900,
    height: 560,
    title: "SensorArray backend error",
    icon: resolveIconPath()
  });
  const body = escapeHtml(`${detail}\n\nSTDERR:\n${stderr || "(empty)"}\n\nSTDOUT:\n${stdout || "(empty)"}`);
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

function bufferFromIpcData(data: ArrayBuffer | Uint8Array | string): Buffer {
  if (typeof data === "string") {
    return Buffer.from(data, "utf-8");
  }
  if (data instanceof ArrayBuffer) {
    return Buffer.from(new Uint8Array(data));
  }
  return Buffer.from(data);
}

function resolveRendererIndexPath(): string {
  if (app.isPackaged) {
    return path.join(app.getAppPath(), "dist", "index.html");
  }
  return path.join(repoRoot, "desktop", "dist", "index.html");
}

function resolvePreloadPath(): string {
  return path.join(__dirname, "preload.js");
}

function resolveIconPath(): string | undefined {
  const candidates = app.isPackaged
    ? [
        path.join(process.resourcesPath, "assets", "icons", "sensorarray-icon.ico"),
        path.join(process.resourcesPath, "sensorarray-icon.ico")
      ]
    : [
        path.join(repoRoot, "desktop", "assets", "icons", "sensorarray-icon.ico"),
        path.join(repoRoot, "desktop", "public", "favicon.ico")
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
