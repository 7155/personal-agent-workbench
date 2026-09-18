import { createContext, createElement, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useOptionalControlTransport } from '@/app/control-transport';
import type { PawAppId } from '../runtime/app-registry';
import { extensionAppForPackage, isPawExtensionAppId, registerLabExtensionApps } from './registry';
import type {
  PawExtensionAppId,
  PawExtensionAppInstallationEvidence,
  PawExtensionAppManifest,
} from './types';

export const PAW_EXTENSION_INSTALLATION_CHANGED_EVENT = 'pawos:extension-installation-changed';

export type PawExtensionInstallationStatus = 'loading' | 'ready' | 'unavailable';

export type PawExtensionInstallationProjection = {
  installedExtensionIds: ReadonlySet<PawExtensionAppId>;
  enabledExtensionIds: ReadonlySet<PawExtensionAppId>;
  availableExtensionIds: ReadonlySet<PawExtensionAppId>;
  /** Installed package is present, but its signed App evidence no longer
   * matches the source manifest. It must be updated before the App can open. */
  updateRequiredExtensionIds: ReadonlySet<PawExtensionAppId>;
};

export type PawExtensionInstallation = PawExtensionInstallationProjection & {
  status: PawExtensionInstallationStatus;
  loading: boolean;
  unavailable: boolean;
  ready: boolean;
  isInstalled: (appId: PawAppId) => boolean;
  isEnabled: (appId: PawAppId) => boolean;
  isAvailable: (appId: PawAppId) => boolean;
  isUpdateRequired: (appId: PawAppId) => boolean;
  refresh: () => void;
};

const EMPTY_IDS: ReadonlySet<PawExtensionAppId> = new Set<PawExtensionAppId>();
const EXTENSION_BINDING_TOKEN = /^pawos\.extension\.binding\.[0-9a-f]{40}$/;
const INSTALLATION_CONTEXT = createContext<PawExtensionInstallation | null>(null);
const DEFAULT_RECONCILIATION_INTERVAL_MS = 60_000;

/**
 * Reduce the Runtime inventory to the registered Extension App identities.
 * Package inventory ids are intentionally resolved through each manifest's
 * packageId; Runtime display names and resource lists cannot grant a desktop
 * surface. A Runtime that explicitly reports unavailable contributes no
 * identities, even if a stale items array accompanies the response.
 */
export function projectPawExtensionInstallation(payload: unknown): PawExtensionInstallationProjection {
  const source = asRecord(payload);
  if (source.runtimeAvailable === false) return emptyProjection();
  const installedExtensionIds = new Set<PawExtensionAppId>();
  const enabledExtensionIds = new Set<PawExtensionAppId>();
  const availableExtensionIds = new Set<PawExtensionAppId>();
  const updateRequiredExtensionIds = new Set<PawExtensionAppId>();
  const items = Array.isArray(source.items) ? source.items : [];
  for (const candidate of items) {
    const item = asRecord(candidate);
    const itemId = text(item.id);
    const explicitPackageId = text(item.packageId);
    if (itemId && explicitPackageId && itemId !== explicitPackageId) continue;
    const packageId = explicitPackageId || itemId;
    const app = packageId ? extensionAppForPackage(packageId) : null;
    if (!app || item.installed !== true) continue;
    installedExtensionIds.add(app.id);
    const matches = extensionAppInstallationMatches(app, item);
    if (!matches) updateRequiredExtensionIds.add(app.id);
    if (item.enabled === true && matches) {
      enabledExtensionIds.add(app.id);
      availableExtensionIds.add(app.id);
    }
  }
  return { installedExtensionIds, enabledExtensionIds, availableExtensionIds, updateRequiredExtensionIds };
}

export function PawExtensionInstallationProvider({
  children,
  pollIntervalMs = DEFAULT_RECONCILIATION_INTERVAL_MS,
}: {
  children: ReactNode;
  pollIntervalMs?: number;
}) {
  const transport = useOptionalControlTransport();
  const [projection, setProjection] = useState<PawExtensionInstallationProjection>(() => emptyProjection());
  const [status, setStatus] = useState<PawExtensionInstallationStatus>('loading');
  const activeRequest = useRef<AbortController | null>(null);
  const scheduledRefresh = useRef<() => void>(() => undefined);

  const load = useCallback(async () => {
    activeRequest.current?.abort();
    if (!transport) {
      activeRequest.current = null;
      setProjection(emptyProjection());
      setStatus('unavailable');
      return;
    }
    const controller = new AbortController();
    activeRequest.current = controller;
    setStatus('loading');
    try {
      const [native, lab] = await Promise.allSettled([
        transport.request({ pathId: 'agent.extensions.list', signal: controller.signal }),
        transport.request({ pathId: 'agent.eval-lab.apps.get', signal: controller.signal }),
      ]);
      if (controller.signal.aborted || activeRequest.current !== controller) return;
      const payload = native.status === 'fulfilled' ? native.value : { runtimeAvailable: false };
      const labIds = registerLabExtensionApps(lab.status === 'fulfilled' ? lab.value : undefined);
      const legacy = projectPawExtensionInstallation(payload);
      const next = { installedExtensionIds: new Set([...legacy.installedExtensionIds, ...labIds]),
        enabledExtensionIds: new Set([...legacy.enabledExtensionIds, ...labIds]),
        availableExtensionIds: new Set([...legacy.availableExtensionIds, ...labIds]),
        updateRequiredExtensionIds: new Set(legacy.updateRequiredExtensionIds) };
      const runtimeUnavailable = asRecord(payload).runtimeAvailable === false
        && !(lab.status === 'fulfilled' && asRecord(lab.value).ok === true);
      setProjection((current) => sameProjection(current, next) ? current : next);
      setStatus(runtimeUnavailable ? 'unavailable' : 'ready');
    } catch {
      if (controller.signal.aborted || activeRequest.current !== controller) return;
      setProjection(emptyProjection());
      setStatus('unavailable');
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null;
    }
  }, [transport]);

  const refresh = useCallback(() => scheduledRefresh.current(), []);

  useEffect(() => {
    let disposed = false;
    let visible = document.visibilityState !== 'hidden';
    let generation = 0;
    let timer: number | null = null;

    const clearTimer = () => {
      if (timer === null) return;
      window.clearTimeout(timer);
      timer = null;
    };
    const run = () => {
      if (disposed || !visible) return;
      const owner = ++generation;
      clearTimer();
      void load().finally(() => {
        if (disposed || !visible || generation !== owner || pollIntervalMs <= 0) return;
        timer = window.setTimeout(() => {
          timer = null;
          if (generation === owner) run();
        }, pollIntervalMs);
      });
    };
    const requestRefresh = () => {
      if (visible) run();
    };
    const handleVisibilityChange = () => {
      const nextVisible = document.visibilityState !== 'hidden';
      if (nextVisible === visible) return;
      visible = nextVisible;
      generation += 1;
      clearTimer();
      if (!visible) {
        activeRequest.current?.abort();
        return;
      }
      run();
    };

    scheduledRefresh.current = requestRefresh;
    window.addEventListener(PAW_EXTENSION_INSTALLATION_CHANGED_EVENT, requestRefresh);
    document.addEventListener('visibilitychange', handleVisibilityChange);
    if (visible) run();

    return () => {
      disposed = true;
      generation += 1;
      scheduledRefresh.current = () => undefined;
      clearTimer();
      window.removeEventListener(PAW_EXTENSION_INSTALLATION_CHANGED_EVENT, requestRefresh);
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      activeRequest.current?.abort();
      activeRequest.current = null;
    };
  }, [load, pollIntervalMs]);

  const value = useMemo<PawExtensionInstallation>(() => ({
    installedExtensionIds: projection.installedExtensionIds,
    enabledExtensionIds: projection.enabledExtensionIds,
    availableExtensionIds: projection.availableExtensionIds,
    updateRequiredExtensionIds: projection.updateRequiredExtensionIds,
    status,
    loading: status === 'loading',
    unavailable: status === 'unavailable',
    ready: status === 'ready',
    isInstalled: (appId) => !isPawExtensionAppId(appId) || projection.installedExtensionIds.has(appId),
    isEnabled: (appId) => !isPawExtensionAppId(appId) || projection.enabledExtensionIds.has(appId),
    isAvailable: (appId) => !isPawExtensionAppId(appId) || projection.availableExtensionIds.has(appId),
    isUpdateRequired: (appId) => isPawExtensionAppId(appId) && projection.updateRequiredExtensionIds.has(appId),
    refresh,
  }), [projection, refresh, status]);

  return createElement(INSTALLATION_CONTEXT.Provider, { value }, children);
}

export function usePawExtensionInstallation(): PawExtensionInstallation {
  const value = useContext(INSTALLATION_CONTEXT);
  if (!value) throw new Error('usePawExtensionInstallation must be used inside PawExtensionInstallationProvider');
  return value;
}

function emptyProjection(): PawExtensionInstallationProjection {
  return {
    installedExtensionIds: EMPTY_IDS,
    enabledExtensionIds: EMPTY_IDS,
    availableExtensionIds: EMPTY_IDS,
    updateRequiredExtensionIds: EMPTY_IDS,
  };
}

function sameProjection(left: PawExtensionInstallationProjection, right: PawExtensionInstallationProjection): boolean {
  return sameSet(left.installedExtensionIds, right.installedExtensionIds)
    && sameSet(left.enabledExtensionIds, right.enabledExtensionIds)
    && sameSet(left.availableExtensionIds, right.availableExtensionIds)
    && sameSet(left.updateRequiredExtensionIds, right.updateRequiredExtensionIds);
}

function sameSet(left: ReadonlySet<PawExtensionAppId>, right: ReadonlySet<PawExtensionAppId>): boolean {
  if (left.size !== right.size) return false;
  for (const value of right) if (!left.has(value)) return false;
  return true;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

export function extensionAppInstallationMatches(
  app: PawExtensionAppManifest,
  item: Record<string, unknown>,
): boolean {
  const itemId = text(item.id);
  const explicitPackageId = text(item.packageId);
  if ((itemId && explicitPackageId && itemId !== explicitPackageId)
    || (explicitPackageId || itemId) !== app.packageId) return false;
  const evidence = extensionAppInstallationEvidence(item);
  if (!evidence) return false;
  const installedVersion = text(item.version);
  return installedVersion === app.version
    && evidence.id === app.id
    && evidence.packageId === app.packageId
    && evidence.version === app.version
    && evidence.bindingSha256 === app.bindingSha256
    && evidence.bindingCapability === extensionBindingCapability(app.bindingSha256)
    && evidence.skillRef === app.skillRef
    && evidence.skillSha256 === app.skillSha256
    && evidence.verticalSuiteId === app.verticalSuiteId
    && evidence.verticalSuiteRevision === app.verticalSuiteRevision
    && sameSandboxContract(evidence.sandbox, app.sandbox);
}

function extensionAppInstallationEvidence(
  item: Record<string, unknown>,
): PawExtensionAppInstallationEvidence | null {
  const raw = asRecord(item.extensionApp);
  const rawCapability = text(raw.bindingCapability);
  const listedCapability = extensionBindingCapabilityFromList(item.capabilities);
  if (!rawCapability || !listedCapability || rawCapability !== listedCapability) return null;
  const capability = listedCapability;
  const id = text(raw.id);
  const packageId = text(raw.packageId);
  const version = text(raw.version);
  const bindingSha256 = text(raw.bindingSha256);
  const skillRef = text(raw.skillRef);
  const skillSha256 = text(raw.skillSha256);
  const verticalSuiteId = text(raw.verticalSuiteId);
  const verticalSuiteRevision = text(raw.verticalSuiteRevision);
  const sandbox = extensionSandboxContract(raw.sandbox);
  if (!isPawExtensionAppId(id)
    || !packageId
    || !version
    || !/^[0-9a-f]{64}$/.test(bindingSha256)
    || !capability
    || !EXTENSION_BINDING_TOKEN.test(capability)
    || !skillRef
    || !/^[0-9a-f]{64}$/.test(skillSha256)
    || !verticalSuiteId
    || !verticalSuiteRevision) return null;
  return {
    id,
    packageId,
    version,
    bindingSha256,
    bindingCapability: capability,
    skillRef,
    skillSha256,
    verticalSuiteId,
    verticalSuiteRevision,
    ...(sandbox ? { sandbox } : {}),
  };
}

function extensionSandboxContract(value: unknown): PawExtensionAppInstallationEvidence['sandbox'] | undefined {
  const raw = asRecord(value);
  return raw.default === 'required' || raw.default === 'optional' || raw.default === 'disabled'
    ? raw.connectorPackageId === 'vertical-agent-sandbox' && raw.policyId === 'vertical-readonly-v1'
      ? {
          default: raw.default,
          connectorPackageId: 'vertical-agent-sandbox',
          policyId: 'vertical-readonly-v1',
        }
      : undefined
    : undefined;
}

function sameSandboxContract(
  left: PawExtensionAppInstallationEvidence['sandbox'],
  right: PawExtensionAppManifest['sandbox'],
): boolean {
  if (!left || !right) return left === right;
  return left.default === right.default
    && left.connectorPackageId === right.connectorPackageId
    && left.policyId === right.policyId;
}

function extensionBindingCapabilityFromList(value: unknown): string {
  if (!Array.isArray(value)) return '';
  const matches = value.filter((candidate): candidate is string => (
    typeof candidate === 'string' && EXTENSION_BINDING_TOKEN.test(candidate)
  ));
  return matches.length === 1 ? matches[0]! : '';
}

function extensionBindingCapability(bindingSha256: string): string {
  return `pawos.extension.binding.${bindingSha256.slice(0, 40)}`;
}
