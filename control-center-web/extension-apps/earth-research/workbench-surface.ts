import type { SetStateAction } from 'react';
import type { DockTab } from './EarthDataDock';
import type { WorkbenchPanel } from './WorkbenchToolbar';

export type WorkbenchSurface = { agentVisible: boolean; drawer: WorkbenchPanel | null; dockView: DockTab | null };
export const INITIAL_SURFACE: WorkbenchSurface = { agentVisible: false, drawer: null, dockView: null };
type Action =
  | { type: 'agent'; value: SetStateAction<boolean> }
  | { type: 'drawer'; value: SetStateAction<WorkbenchPanel | null> }
  | { type: 'dock'; value: SetStateAction<DockTab | null> }
  | { type: 'reset' };
function resolve<T>(next: SetStateAction<T>, current: T): T {
  return typeof next === 'function' ? (next as (value: T) => T)(current) : next;
}
/** Only visibility changes. Owners stay mounted, so drafts and running work survive. */
export function reduceWorkbenchSurface(state: WorkbenchSurface, action: Action): WorkbenchSurface {
  if (action.type === 'reset') return INITIAL_SURFACE;
  if (action.type === 'agent') {
    const agentVisible = resolve(action.value, state.agentVisible);
    return agentVisible ? { ...INITIAL_SURFACE, agentVisible } : { ...state, agentVisible };
  }
  if (action.type === 'drawer') {
    const drawer = resolve(action.value, state.drawer);
    return drawer ? { ...INITIAL_SURFACE, drawer } : { ...state, drawer };
  }
  const dockView = resolve(action.value, state.dockView);
  return dockView ? { ...INITIAL_SURFACE, dockView } : { ...state, dockView };
}
