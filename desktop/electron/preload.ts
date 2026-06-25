import { createRequire } from "node:module";

const nodeRequire = createRequire(import.meta.url);
const { contextBridge, ipcRenderer } = nodeRequire("electron") as typeof import("electron");

contextBridge.exposeInMainWorld("sensorarrayDesktop", {
  getBackendUrl: () => ipcRenderer.invoke("backend:url"),
  selectReplayFile: () => ipcRenderer.invoke("dialog:selectReplayFile")
});
