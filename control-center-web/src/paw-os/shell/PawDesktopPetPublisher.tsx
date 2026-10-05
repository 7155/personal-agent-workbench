import { useEffect, useMemo, useRef } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { createDesktopPetPublisher } from '@/features/agent/desktop-pet-publisher';
import { usePawWorkDirectory } from './PawWorkDirectory';
import { projectPetDirectory } from './desktop-pet-directory-projection';

/** Consumer of the existing shell directory, never another polling owner. */
export function PawDesktopPetPublisher() {
  const transport = useControlTransport();
  const directory = usePawWorkDirectory();
  const scopeId = useMemo(() => crypto.randomUUID(), [transport]);
  const publisher = useRef<ReturnType<typeof createDesktopPetPublisher> | undefined>(undefined);
  const snapshot = useMemo(() => projectPetDirectory(directory.sessions, directory.sessionStatusFresh, directory.loaded),
    [directory.sessions, directory.sessionStatusFresh, directory.loaded]);
  useEffect(() => {
    const bridge = window.pawDesktopPetState;
    if (!bridge) return;
    const lease = createDesktopPetPublisher(bridge, { schemaVersion: 1, sourceId: 'work-directory', scopeId });
    publisher.current = lease;
    return () => { publisher.current = undefined; lease.release(); };
  }, [scopeId]);
  useEffect(() => { publisher.current?.update(snapshot); }, [snapshot, scopeId]);
  return null;
}
