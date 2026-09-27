export type AgentInterfaceMode = 'traditional' | 'jev';
export const AGENT_MODE_KEY = 'paw.agent.interface-mode.v1';
export interface ModeHost {
  read(): string | null; write(value: AgentInterfaceMode): void;
  subscribe(callback: () => void): () => void;
}
/** Small external store; mode is device presentation, not execution authority. */
export function createAgentModeStore(host?: ModeHost) {
  let mode: AgentInterfaceMode = 'jev'; let initialized = false; let volatile = false;
  const listeners = new Set<() => void>(); let unsubscribe: (() => void) | undefined;
  const read = () => { try { return host?.read() === 'traditional' ? 'traditional' : 'jev'; } catch { return mode; } };
  function update(next: AgentInterfaceMode) { if (mode !== next) { mode = next; for (const fn of listeners) fn(); } }
  return {
    getSnapshot: (): AgentInterfaceMode => { if (!initialized) { initialized = true; mode = read(); } return mode; },
    getServerSnapshot: (): AgentInterfaceMode => 'jev',
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      if (listeners.size === 1) {
        unsubscribe = host?.subscribe(() => { volatile = false; update(read()); });
        // Cover changes while every Agent window was closed.
        initialized = true; if (!volatile) update(read());
      }
      return () => { listeners.delete(listener); if (!listeners.size) { unsubscribe?.(); unsubscribe = undefined; } };
    },
    select: (next: AgentInterfaceMode) => {
      if (next !== 'jev' && next !== 'traditional') return;
      initialized = true;
      try { host?.write(next); volatile = !host; } catch { volatile = true; /* Keep choice in this page. */ }
      update(next);
    },
  };
}
export const agentModeStore = createAgentModeStore(typeof window === 'undefined' ? undefined : {
  read: () => window.localStorage.getItem(AGENT_MODE_KEY),
  write: value => window.localStorage.setItem(AGENT_MODE_KEY, value),
  subscribe: callback => {
    const handler = (event: StorageEvent) => {
      if (event.key !== null && event.key !== AGENT_MODE_KEY) return;
      try { if (event.storageArea !== null && event.storageArea !== window.localStorage) return; } catch { /* Restricted storage: read() keeps in-memory view. */ }
      callback();
    };
    window.addEventListener('storage', handler); return () => window.removeEventListener('storage', handler);
  },
});
