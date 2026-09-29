import { useSyncExternalStore } from 'react';
import { agentModeStore, type AgentInterfaceMode } from './agent-mode-store';
export type { AgentInterfaceMode } from './agent-mode-store';

/** Keep the existing component and hook API, so PawAgentApp need not remount. */
export function useAgentInterfaceMode() {
  const mode = useSyncExternalStore(agentModeStore.subscribe, agentModeStore.getSnapshot, agentModeStore.getServerSnapshot);
  return [mode, agentModeStore.select] as const;
}
export function AgentModeSwitch({ mode, onChange }: {
  mode: AgentInterfaceMode; onChange: (mode: AgentInterfaceMode) => void;
}) {
  return <div aria-label="Agent 界面模式" className="agent-mode-switch" role="group">
    <button aria-pressed={mode === 'traditional'} onClick={() => onChange('traditional')} type="button">传统</button>
    <button aria-pressed={mode === 'jev'} onClick={() => onChange('jev')} type="button">Jev</button>
  </div>;
}
