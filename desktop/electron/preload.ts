import electron from "electron";

const { contextBridge, ipcRenderer } = electron;

contextBridge.exposeInMainWorld("sensorarrayDesktop", {
  getBackendUrl: () => ipcRenderer.invoke("backend:url"),
  selectReplayFile: () => ipcRenderer.invoke("dialog:selectReplayFile"),
  onImportReplayData: (callback: (path: string) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, path: string) => callback(path);
    ipcRenderer.on("menu:importReplayData", listener);
    return () => ipcRenderer.off("menu:importReplayData", listener);
  },
  onExportSessionData: (callback: () => void) => {
    const listener = () => callback();
    ipcRenderer.on("menu:exportSessionData", listener);
    return () => ipcRenderer.off("menu:exportSessionData", listener);
  },
  saveExportedSession: (defaultName: string, data: string) => ipcRenderer.invoke("dialog:saveExportedSession", defaultName, data)
});
