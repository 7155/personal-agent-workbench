import { useCallback, useReducer, type SetStateAction } from 'react';
import type { DockTab } from './EarthDataDock';
import type { WorkbenchPanel } from './WorkbenchToolbar';
import { reduceWorkbenchSurface, INITIAL_SURFACE } from './workbench-surface';

export function useWorkbenchSurface() {
  const [state, dispatch] = useReducer(reduceWorkbenchSurface, INITIAL_SURFACE);
  const setAgentVisible = useCallback((value: SetStateAction<boolean>) => dispatch({ type: 'agent', value }), []);
  const setDrawer = useCallback((value: SetStateAction<WorkbenchPanel | null>) => dispatch({ type: 'drawer', value }), []);
  const setDockView = useCallback((value: SetStateAction<DockTab | null>) => dispatch({ type: 'dock', value }), []);
  const resetSurface = useCallback(() => dispatch({ type: 'reset' }), []);
  return { ...state, setAgentVisible, setDrawer, setDockView, resetSurface };
}
