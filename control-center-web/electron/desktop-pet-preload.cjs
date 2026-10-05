const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('pawDesktopPet', Object.freeze({
  ready: () => ipcRenderer.invoke('paw-pet:ready'),
  onSnapshot: (listener) => {
    const handler = (_event, snapshot) => listener(snapshot);
    ipcRenderer.on('paw-pet:state', handler);
    return () => ipcRenderer.removeListener('paw-pet:state', handler);
  },
  hide: () => ipcRenderer.invoke('paw-pet:hide'),
  openAssistant: () => ipcRenderer.invoke('paw-pet:open-assistant'),
  openConversation: (target) => ipcRenderer.invoke('paw-pet:open-conversation', target),
  setExpanded: (expanded) => ipcRenderer.invoke('paw-pet:expand', expanded),
  drag: (phase) => ipcRenderer.invoke('paw-pet:drag', phase),
}));
