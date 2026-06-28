import electron from "electron";

const { contextBridge, ipcRenderer } = electron;

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
    const listener = () => callback();
    ipcRenderer.on("menu:importSessionData", listener);
    return () => ipcRenderer.off("menu:importSessionData", listener);
  },
  onExportSessionData: (callback: () => void) => {
    const listener = () => callback();
    ipcRenderer.on("menu:exportSessionData", listener);
    return () => ipcRenderer.off("menu:exportSessionData", listener);
  },
  onImportSetupProfile: (callback: () => void) => {
    const listener = () => callback();
    ipcRenderer.on("menu:importSetupProfile", listener);
    return () => ipcRenderer.off("menu:importSetupProfile", listener);
  },
  onExportSetupProfile: (callback: () => void) => {
    const listener = () => callback();
    ipcRenderer.on("menu:exportSetupProfile", listener);
    return () => ipcRenderer.off("menu:exportSetupProfile", listener);
  },
  onCaptureScreenshot: (callback: () => void) => {
    const listener = () => callback();
    ipcRenderer.on("menu:captureScreenshot", listener);
    return () => ipcRenderer.off("menu:captureScreenshot", listener);
  },
  chooseSessionExportPath: (defaultName: string) => ipcRenderer.invoke("dialog:chooseSessionExportPath", defaultName),
  saveExportedSession: (defaultName: string, data: ArrayBuffer | Uint8Array | string) => ipcRenderer.invoke("dialog:saveExportedSession", defaultName, data),
  writeBinaryFile: (path: string, data: ArrayBuffer | Uint8Array | string) => ipcRenderer.invoke("file:writeBinary", path, data),
  saveSetupProfile: (defaultName: string, data: string) => ipcRenderer.invoke("dialog:saveSetupProfile", defaultName, data),
  captureScreenshot: () => ipcRenderer.invoke("screenshot:capture")
});
