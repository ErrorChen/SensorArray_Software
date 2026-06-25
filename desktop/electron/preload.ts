import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("sensorarrayDesktop", {
  getBackendUrl: () => ipcRenderer.invoke("backend:url"),
  selectReplayFile: () => ipcRenderer.invoke("dialog:selectReplayFile")
});

