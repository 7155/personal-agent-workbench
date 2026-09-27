import { useCallback, useEffect, useRef } from 'react';
import type { ControlTransport } from '@/platform/transport';
import { readCompleteFile } from '@/features/files/WorkspaceTextEditor';
import type { EarthMapState } from './pi-package/view-contract';
import { createCoalescingWriter, isMissingWorkspaceFile, workspaceFilePath } from './workspace-io';

export function useMapStatePersistence(transport: ControlTransport, sessionId: string | undefined, root: string) {
  const writerRef = useRef<ReturnType<typeof createCoalescingWriter<EarthMapState>> | undefined>(undefined);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => {
    if (!sessionId || !root) return;
    let active = true;
    const path = workspaceFilePath(root, '.earth/map-state.json');
    const writer = createCoalescingWriter<EarthMapState>(async state => {
      let resourceRevision: string | undefined;
      try { resourceRevision = (await readCompleteFile(transport, { sessionId, path, name: 'map-state.json' })).resourceRevision; }
      catch (error) { if (!isMissingWorkspaceFile(error)) throw error; }
      if (!active) return;
      await transport.request({ pathId: 'agent.session.workspace.save', params: { sessionId }, body: {
        path, content: JSON.stringify(state), ...(resourceRevision ? { resourceRevision } : {}),
      } });
    });
    writerRef.current = writer;
    return () => { active = false; clearTimeout(timer.current); writer.dispose(); if (writerRef.current === writer) writerRef.current = undefined; };
  }, [transport, sessionId, root]);
  return useCallback((state: EarthMapState) => {
    clearTimeout(timer.current);
    const writer = writerRef.current;
    timer.current = setTimeout(() => { if (writer === writerRef.current) void writer?.push(state); }, 250);
  }, []);
}
