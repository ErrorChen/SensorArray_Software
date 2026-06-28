import electron from "electron";

const { contextBridge, ipcRenderer } = electron;

function onMenu(channel: string, callback: () => void): () => void {
  const listener = () => callback();
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.off(channel, listener);
}

contextBridge.exposeInMainWorld("sensorarrayDesktop", {
  getBackendUrl: () => ipcRenderer.invoke("backend:url"),
  getRuntimeDirectory: () => ipcRenderer.invoke("runtime:directory"),
  getDefaultSaveDirectory: () => ipcRenderer.invoke("paths:getDefaultSaveDirectory"),
  setDefaultSaveDirectory: (directory: string) => ipcRenderer.invoke("paths:setDefaultSaveDirectory", directory),
  selectDefaultSaveDirectory: () => ipcRenderer.invoke("paths:selectDefaultSaveDirectory"),
  openDefaultSaveDirectory: () => ipcRenderer.invoke("paths:openDefaultSaveDirectory"),
  selectReplayFile: () => ipcRenderer.invoke("dialog:selectReplayFile"),
  selectSessionDataFile: () => ipcRenderer.invoke("dialog:selectSessionDataFile"),
  selectSetupProfile: () => ipcRenderer.invoke("dialog:selectSetupProfile"),
  readTextFile: (path: string) => ipcRenderer.invoke("file:readText", path),
  onImportReplayData: (callback: (path: string) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, path: string) => callback(path);
    ipcRenderer.on("menu:importReplayData", listener);
    return () => ipcRenderer.off("menu:importReplayData", listener);
  },
  onImportSessionData: (callback: () => void) => {
    return onMenu("menu:importSessionData", callback);
  },
  onExportSessionData: (callback: () => void) => {
    return onMenu("menu:exportSessionData", callback);
  },
  onImportSetupProfile: (callback: () => void) => {
    return onMenu("menu:importSetupProfile", callback);
  },
  onExportSetupProfile: (callback: () => void) => {
    return onMenu("menu:exportSetupProfile", callback);
  },
  onCaptureScreenshot: (callback: () => void) => {
    return onMenu("menu:captureScreenshot", callback);
  },
  onMenuImportSetup: (callback: () => void) => onMenu("menu:importSetupProfile", callback),
  onMenuExportSetup: (callback: () => void) => onMenu("menu:exportSetupProfile", callback),
  onMenuImportData: (callback: () => void) => onMenu("menu:importSessionData", callback),
  onMenuExportData: (callback: () => void) => onMenu("menu:exportSessionData", callback),
  onMenuCaptureScreenshot: (callback: () => void) => onMenu("menu:captureScreenshot", callback),
  onScreenshotResult: (callback: (result: { ok: boolean; path?: string; error?: string; canceled?: boolean }) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, result: { ok: boolean; path?: string; error?: string; canceled?: boolean }) => callback(result);
    ipcRenderer.on("screenshot:result", listener);
    return () => ipcRenderer.off("screenshot:result", listener);
  },
  chooseSessionExportPath: (defaultName: string) => ipcRenderer.invoke("dialog:chooseSessionExportPath", defaultName),
  saveExportedSession: (defaultName: string, data: ArrayBuffer | Uint8Array | string) => ipcRenderer.invoke("dialog:saveExportedSession", defaultName, data),
  writeBinaryFile: (path: string, data: ArrayBuffer | Uint8Array | string) => ipcRenderer.invoke("file:writeBinary", path, data),
  saveSetupProfile: (defaultName: string, data: string) => ipcRenderer.invoke("dialog:saveSetupProfile", defaultName, data),
  captureScreenshot: () => ipcRenderer.invoke("screenshot:capture")
});
