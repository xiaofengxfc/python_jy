/*
 * The renderer runs with contextIsolation on, so window controls go through a
 * deliberately tiny surface: three commands and the platform name.
 */

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("bitrader", {
  platform: process.platform,
  minimize: () => ipcRenderer.invoke("window:minimize"),
  toggleMaximize: () => ipcRenderer.invoke("window:toggle-maximize"),
  close: () => ipcRenderer.invoke("window:close"),
});
