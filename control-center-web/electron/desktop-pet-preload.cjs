const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('pawDesktopPet', Object.freeze({
  ready: () => ipcRenderer.invoke('paw-pet:ready'),
  hide: () => ipcRenderer.invoke('paw-pet:hide'),
  openAssistant: () => ipcRenderer.invoke('paw-pet:open-assistant'),
  drag: (phase) => ipcRenderer.invoke('paw-pet:drag', phase),
}));
