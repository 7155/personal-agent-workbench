import { useEffect, useMemo } from 'react';
import { usePawOsAppearance } from '@/design/paw-os-themes';
import { pawAppForPath } from './runtime/app-registry';
import { PawDesktopProvider, usePawDesktopApi } from './runtime/desktop-context';
import type { PawDesktopStore } from './runtime/desktop-store';
import { PawDesktop } from './shell/PawDesktop';
import './styles/paw-os.css';
import './styles/paw-os-motion.css';
import './styles/paw-os-webmodel-v1.css';
import './styles/paw-os-shell-migrated-v1.css';
import './styles/paw-os-controls.css';
import './styles/paw-os-stellar.css';
import './styles/paw-os-stellar-dark.css';

export function PawOsApp({ desktopStorageKey }: { desktopStorageKey?: string } = {}) {
  const { theme } = usePawOsAppearance();
  const initialRoute = useMemo(() => currentHashRoute(), []);
  const initialApp = useMemo(() => pawAppForPath(initialRoute), [initialRoute]);
  return (
    <PawDesktopProvider
      initialAppId={initialApp?.id}
      initialRoute={initialRoute}
      storageKey={desktopStorageKey}
    >
      <PawOsRouteBridge />
      <div className="paw-desktop-root" data-paw-theme={theme} data-paw-visual="stellar" data-testid="paw-os-product-root">
        <PawDesktop />
      </div>
    </PawDesktopProvider>
  );
}

function PawOsRouteBridge() {
  const api = usePawDesktopApi();
  useEffect(() => {
    const sync = () => syncPawOsRoute(api);
    sync();
    window.addEventListener('hashchange', sync);
    return () => window.removeEventListener('hashchange', sync);
  }, [api]);
  return null;
}

export function syncPawOsRoute(api: PawDesktopStore) {
  const route = currentHashRoute();
  const app = pawAppForPath(route);
  if (!app) {
    api.getState().showWayfinder();
    return;
  }
  if (app.id === 'agent') {
    const currentTarget = api.getState().windows.agent?.target;
    const requestedTarget = agentRouteTarget(route);
    if (
      (currentTarget?.kind === 'session' || currentTarget?.kind === 'room')
      && (
        !requestedTarget
        || requestedTarget.kind !== currentTarget.kind
        || requestedTarget.id !== currentTarget.id
      )
    ) {
      // A bound target is stronger than initialRoute inside PawAgentApp. Drop
      // that stale binding before applying a different deep link; the App will
      // bind the newly loaded Session or Room back after catalog resolution.
      api.getState().bindAgentMain('agent');
    }
  }
  api.getState().openApp(app.id, { initialRoute: route });
}

function agentRouteTarget(route: string): { kind: 'session' | 'room'; id: string } | null {
  const query = new URLSearchParams(route.split('?', 2)[1] ?? '');
  const roomId = query.get('room')?.trim();
  if (roomId) return { kind: 'room', id: roomId };
  const sessionId = (query.get('session') || query.get('sessionId'))?.trim();
  return sessionId ? { kind: 'session', id: sessionId } : null;
}

function currentHashRoute(): string {
  if (typeof window === 'undefined') return '/project-field';
  return window.location.hash.replace(/^#/, '') || '/project-field';
}
