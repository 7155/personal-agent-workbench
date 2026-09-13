import { LoaderCircle } from 'lucide-react';
import { lazy, Suspense } from 'react';
import type { PawOsWindowTarget } from '@/features/paw-os/model/desktop';
import type { PawAppId } from '../runtime/app-registry';
import { pawApp } from '../runtime/app-registry';
import { isPawExtensionAppId } from '../extensions/registry';
import { PawExtensionAppHost } from '../extensions/ExtensionAppHost';
import { PawAppIcon } from '../shell/PawAppIcon';
import './paw-apps.css';

type PawNativeAppId = Extract<PawAppId,
  | 'project-workbench'
  | 'memory'
  | 'knowledge'
  | 'input-studio'
  | 'app-center'
  | 'system-monitor'
  | 'eval-lab'
  | 'system-settings'>;

const loadPawAgentApp = () => import('./entries/PawAgentAppEntry');
const loadPawAgentCapsuleApp = () => import('./entries/PawAgentCapsuleAppEntry');
const loadPawBrowserApp = () => import('./entries/PawBrowserAppEntry');
const loadPawNativeApp = () => import('./entries/PawNativeAppEntry');
const loadTraceAgentApp = () => import('./entries/PawTraceAgentAppEntry');
const loadPawOsSatelliteHost = () => import('./entries/PawSatelliteEntry');
const loadPawResultWindow = () => import('./entries/PawResultWindowEntry');
const loadFilesApp = async () => ({
  default: (await import('@/features/files/PawOsFilesApp')).PawOsFilesApp,
});
const loadTerminalApp = async () => ({
  default: (await import('@/features/terminal/PawOsTerminalApp')).PawOsTerminalApp,
});

const PawAgentApp = lazy(loadPawAgentApp);
const PawAgentCapsuleApp = lazy(loadPawAgentCapsuleApp);
const PawBrowserApp = lazy(loadPawBrowserApp);
const PawNativeApp = lazy(loadPawNativeApp);
const TraceAgentApp = lazy(loadTraceAgentApp);
const PawOsSatelliteHost = lazy(loadPawOsSatelliteHost);
const PawResultWindow = lazy(loadPawResultWindow);
const FilesApp = lazy(loadFilesApp);
const TerminalApp = lazy(loadTerminalApp);
const SchedulesApp = lazy(async () => ({ default: (await import('@/features/schedules/SchedulesApp')).SchedulesApp }));

export function warmPawAppBody(appId: PawAppId): void {
  const load = appId === 'agent'
    ? loadPawAgentApp
    : appId === 'agent-capsule'
    ? loadPawAgentCapsuleApp
    : appId === 'browser'
    ? loadPawBrowserApp
    : appId === 'files'
    ? loadFilesApp
    : appId === 'terminal'
    ? loadTerminalApp
    : appId === 'trace-agent'
    ? loadTraceAgentApp
    : isPawExtensionAppId(appId)
    ? undefined
    : loadPawNativeApp;
  if (load) void load().catch(() => undefined);
}

export function PawAppBody({
  appId,
  entityId,
  initialRoute,
  target,
}: {
  appId: PawAppId;
  entityId?: string;
  initialRoute?: string;
  target?: PawOsWindowTarget;
}) {
  return (
    <Suspense fallback={<AppLoading appId={appId} />}>
      {renderApp(appId, entityId, initialRoute, target)}
    </Suspense>
  );
}

function renderApp(appId: PawAppId, entityId?: string, initialRoute?: string, target?: PawOsWindowTarget) {
  if (isPawExtensionAppId(appId)) {
    return <PawExtensionAppHost appId={appId} entityId={entityId} initialRoute={initialRoute} target={target} />;
  }
  if (target?.kind === 'result') return <PawResultWindow target={target} />;
  if (target?.kind === 'process-terminal') return <PawOsSatelliteHost target={target} />;
  if (target?.kind === 'browser-target' && target.backgroundObserver) return <PawOsSatelliteHost target={target} />;
  /* A Room participant is a planet observation window, not its full Session
   * workspace. Keep the participant target for Room grouping and the
   * Earth/Mars titleplate; the compact host retains the public timeline and
   * exposes Trace/full-Session navigation when intervention is needed. */
  if (
    (target?.kind === 'room' && Boolean(target.panel))
    || target?.kind === 'participant'
    || target?.kind === 'subagent'
    || target?.kind === 'work-document'
    || target?.kind === 'project'
    || target?.kind === 'task'
    || target?.kind === 'package'
  ) return <PawOsSatelliteHost target={target} />;
  switch (appId) {
    case 'schedules':
      return <SchedulesApp initialRoute={initialRoute} />;
    case 'agent':
      return <PawAgentApp initialRoute={initialRoute} target={target ?? (entityId ? { kind: 'session', id: entityId, title: entityId } : undefined)} />;
    case 'agent-capsule':
      return <PawAgentCapsuleApp />;
    case 'browser':
      return <PawBrowserApp target={target?.kind === 'browser-target' ? target : undefined} />;
    case 'files':
      return <FilesApp initialRoute={initialRoute} />;
    case 'terminal':
      return <TerminalApp />;
    case 'trace-agent':
      return <TraceAgentApp initialRoute={initialRoute} />;
    case 'project-workbench':
    case 'memory':
    case 'knowledge':
    case 'input-studio':
    case 'app-center':
    case 'system-monitor':
    case 'eval-lab':
    case 'system-settings':
      return <PawNativeApp appId={appId as PawNativeAppId} initialRoute={initialRoute} />;
  }
}

function AppLoading({ appId }: { appId: PawAppId }) {
  return (
    <div className="paw-app-loading" role="status">
      <PawAppIcon appId={appId} size={28} />
      <LoaderCircle className="ui-spin" size={18} />
      <span>正在打开 {pawApp(appId).label}</span>
    </div>
  );
}
