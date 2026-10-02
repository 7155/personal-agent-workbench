import { lazy, memo, Suspense } from 'react';
import type { PawOsWindowTarget } from '@/features/paw-os/model/desktop';
import type { PawAppId } from '../runtime/app-registry';
import { pawApp } from '../runtime/app-registry';
import { PawAppIcon } from '../shell/PawAppIcon';
import { PawAppErrorBoundary } from './PawAppErrorBoundary';
import { agentWorkspaceIntent, warmAgentWorkspace, type AgentWorkspaceKind } from './agent-workspace-loader';
// Eager: the boot state below is what the window shows while the App chunk —
// and everything it imports, including paw-apps.css — is still loading.
import './paw-app-boot.css';

const loadPawAppsRuntime = () => import('./PawAppsRuntime');
const PawAppBody = lazy(async () => ({
  default: (await loadPawAppsRuntime()).PawAppBody,
}));

/** Start both dynamic boundaries while the pointer is approaching an App (or
 * the desktop is idle), so the launch click only commits window state. Native
 * import caching makes repeated hover/focus calls share the result. An exact
 * Room/Session intent warms its module in parallel, without mounting it. */
export function warmPawAppProcess(appId: PawAppId, workspaceKind?: AgentWorkspaceKind): void {
  if (appId === 'agent' && workspaceKind) warmAgentWorkspace(workspaceKind);
  void loadPawAppsRuntime()
    .then((runtime) => runtime.warmPawAppBody(appId))
    .catch(() => undefined);
}

export const PawAppProcess = memo(function PawAppProcess({ appId, entityId, initialRoute, target }: { appId: PawAppId; entityId?: string; initialRoute?: string; target?: PawOsWindowTarget }) {
  const workspaceKind = agentWorkspaceIntent({ appId, entityId, initialRoute, target });
  if (workspaceKind) warmAgentWorkspace(workspaceKind);
  const resetKey = JSON.stringify([appId, entityId, initialRoute, target?.kind, target?.id, target && 'panel' in target ? target.panel : undefined]);
  return (
    <PawAppErrorBoundary appId={appId} resetKey={resetKey}>
      <Suspense fallback={<PawAppBoot appId={appId} />}>
        <PawAppBody appId={appId} entityId={entityId} initialRoute={initialRoute} target={target} />
      </Suspense>
    </PawAppErrorBoundary>
  );
});

function PawAppBoot({ appId }: { appId: PawAppId }) {
  const app = pawApp(appId);
  return (
    <div className="paw-app-boot" role="status">
      <PawAppIcon appId={appId} size={32} />
      <span><small>正在打开</small><strong>{app.label}</strong></span>
    </div>
  );
}
