import { useQuery } from '@tanstack/react-query';
import { useControlTransport } from '@/app/control-transport';
import { labConnectionKey, requestLabControl } from '../control-request';
import { object, type JsonValue } from './types';
import type { ControlTransport } from '@/platform/transport';
import { projectCommandRejected, projectError } from './api';

export type LabAppAction = { id: string; title: string; prompt: string; kind?: 'completion' | 'retrieval'; inputSchema: Record<string, JsonValue> };
export type LabEvaluationSelection = { suiteId: string; jobId: string; variant: 'baseline' | 'candidate' };
export type LabAppSpec = { title: string; description: string; html: string; skill: string; context: string[];
  model: { provider: string; model: string; thinkingLevel: string }; actions: LabAppAction[];
  externalWorkspace?: { title: string; url: string; presentation?: 'tabs' | 'split' };
  appearance?: { accent: string; icon: { symbol: string; background: string }; colorScheme?: 'inherit' | 'light' | 'dark' };
  evaluationSelection?: { suiteId: string; jobId: string; variant: 'baseline' | 'candidate'; snapshotId?: string; configurationSha256?: string; scope?: string;
    applicationMethod?: { title: string; sha256: string; source?: { kind: string; projectId?: string; artifactId?: string; artifactRevision?: number } } | null };
  knowledge?: { documentCount: number; sourceCount: number; chunkCount: number; profile: { mode: string; topK: number; contextChars: number }; sourceIndexId: string; snapshotSha256: string } };
export type LabApp = { appId: string; projectId: string; title: string; description: string; revision: number;
  latestVersion: number; activeVersion: number | null; createdAtMs: number; updatedAtMs: number; installation?: unknown };
export type LabAppVersion = { appId: string; version: number; spec: LabAppSpec; contentHash: string; html: string;
  fileCount: number; byteSize: number; createdAtMs: number; sourceFiles: { path: string; byteSize: number; sha256: string }[] };
export type LabAppCall = { callId: string; appId: string; version: number; actionId: string; input: Record<string, JsonValue>; model?: LabAppSpec['model'];
  state: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'interrupted'; sessionId: string;
  result: { text?: string; usage?: Record<string, JsonValue>; receipt?: Record<string, JsonValue> }; error: string;
  progress?: { stage?: string; events?: { stage: string; atMs: number }[]; text?: string; sources?: JsonValue[]; knowledge?: Record<string, JsonValue>; streamPartial?: boolean; startedAtMs?: number; updatedAtMs?: number };
  cancelRequested: boolean; createdAtMs: number; updatedAtMs: number };
export type LabAppRead = { ok: true; items: LabApp[]; app: LabApp | null; version?: LabAppVersion; versions?: LabAppVersion[]; calls?: LabAppCall[]; call?: LabAppCall };
export type LabAppCommand = { appId: string; expectedRevision: number; clientRequestId: string;
  action: 'activate' | 'deactivate' | 'invoke' | 'cancel' | 'resume'; input: Record<string, JsonValue> };
export type LabAppReceipt = { ok: true; app: LabApp; call?: LabAppCall; replayed: boolean; clientRequestId: string };
const natural = (value: unknown) => Number.isSafeInteger(value) && Number(value) > 0;
export function isLabApp(raw: unknown): raw is LabApp {
  const value = object(raw);
  return typeof value.appId === 'string' && /^extension:lab-[a-f0-9]{32}$/u.test(value.appId)
    && typeof value.projectId === 'string' && typeof value.title === 'string' && typeof value.description === 'string'
    && natural(value.revision) && natural(value.latestVersion) && (value.activeVersion === null || natural(value.activeVersion));
}
export function parseLabAppRead(raw: unknown, appId = '', callId = '', expectedVersion?: number): LabAppRead {
  const value = object(raw);
  if (value.ok !== true || !Array.isArray(value.items) || !value.items.every(isLabApp)
      || !(value.app === null || isLabApp(value.app)) || (appId && (!isLabApp(value.app) || value.app.appId !== appId))) throw new Error('应用数据未完整返回。');
  if (appId) {
    const version = object(value.version); const spec = object(version.spec);
    if (version.appId !== appId || !natural(version.version) || typeof version.html !== 'string' || typeof version.contentHash !== 'string'
        || typeof spec.title !== 'string' || !Array.isArray(spec.actions)
        || !spec.actions.every((raw) => { const action = object(raw); return typeof action.id === 'string' && typeof action.title === 'string' && typeof action.prompt === 'string'; })) throw new Error('应用版本未完整返回。');
    if (expectedVersion !== undefined && version.version !== expectedVersion) throw new Error('返回版本与选中的原应用版本不匹配。');
    if (callId) {
      const call = object(value.call);
      if (call.callId !== callId || call.appId !== appId || call.version !== version.version || typeof call.state !== 'string' || !call.result || typeof call.result !== 'object' || Array.isArray(call.result)) throw new Error('原应用调用未完整返回，未替换为其他调用。');
    }
    if (spec.externalWorkspace !== undefined) {
      const workspace = object(spec.externalWorkspace);
      if (workspace.presentation !== undefined && !['tabs', 'split'].includes(String(workspace.presentation))) throw new Error('应用工作台布局无效。');
      if (typeof workspace.title !== 'string' || !workspace.title.trim() || workspace.title.length > 100 || !externalWorkspaceUrl(workspace.url)) throw new Error('应用工作台地址未完整返回。');
    }
    if (spec.evaluationSelection !== undefined) {
      const selection = object(spec.evaluationSelection); const method = object(selection.applicationMethod);
      if (typeof selection.suiteId !== 'string' || typeof selection.jobId !== 'string' || !['baseline', 'candidate'].includes(String(selection.variant))
        || !['snapshotId', 'configurationSha256', 'scope'].every((key) => selection[key] === undefined || typeof selection[key] === 'string')
        || !(selection.applicationMethod === undefined || selection.applicationMethod === null || (typeof method.title === 'string' && typeof method.sha256 === 'string'))) throw new Error('应用的评测来源未完整返回。');
    }
  }
  return value as LabAppRead;
}

/** A browser destination does not grant access to the App invocation bridge. */
export function externalWorkspaceUrl(value: unknown): string | null {
  if (typeof value !== 'string' || value.length > 2000 || /[\s<>"'\\]/u.test(value)) return null;
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash
      || (url.protocol === 'http:' && !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname))) return null;
    return url.href;
  } catch { return null; }
}
export function useLabApps(projectId = '', appId = '', version?: number, callId = '') {
  const transport = useControlTransport();
  return useQuery<LabAppRead>({ queryKey: ['lab-apps', labConnectionKey(transport), projectId, appId, version ?? 0, callId], retry: false,
    refetchInterval: (query) => query.state.data?.calls?.some((call) => ['queued', 'running'].includes(call.state)) ? 1000 : 3000, refetchOnWindowFocus: false,
    queryFn: async ({ signal }) => parseLabAppRead(await requestLabControl(transport, { pathId: 'agent.eval-lab.apps.get', signal,
      query: { ...(projectId ? { projectId } : {}), ...(appId ? { appId } : {}), ...(version ? { version } : {}), ...(callId ? { callId } : {}) } }), appId, callId, version),
  });
}
export async function commandLabApp(transport: ControlTransport, command: LabAppCommand): Promise<LabAppReceipt> {
  persistAppCommand(transport, command);
  try {
    const value = object(await requestLabControl(transport, { pathId: 'agent.eval-lab.apps.command', body: command }));
    if (value.ok === false) throw Object.assign(new Error(projectError({ payload: value })), { payload: value });
    if (value.ok !== true || !isLabApp(value.app) || value.app.appId !== command.appId || value.clientRequestId !== command.clientRequestId) throw new Error('应用操作回执未完整返回，请核对原操作。');
    persistAppCommand(transport, command, true);
    return value as LabAppReceipt;
  } catch (error) {
    if (projectCommandRejected(error)) persistAppCommand(transport, command, true);
    throw error;
  }
}
const appPendingKey = (transport: ControlTransport) => `paw.lab.app-commands.v1:${labConnectionKey(transport)}`;
export function pendingLabAppCommands(transport: ControlTransport, appId: string): LabAppCommand[] {
  try {
    const values: unknown = JSON.parse(sessionStorage.getItem(appPendingKey(transport)) ?? '[]');
    if (!Array.isArray(values)) return [];
    return values.filter((value): value is LabAppCommand => {
      const row = object(value);
      return row.appId === appId && natural(row.expectedRevision) && typeof row.clientRequestId === 'string'
        && ['activate', 'deactivate', 'invoke', 'cancel', 'resume'].includes(String(row.action))
        && row.input !== null && typeof row.input === 'object' && !Array.isArray(row.input);
    });
  } catch { return []; }
}
function persistAppCommand(transport: ControlTransport, command: LabAppCommand, remove = false) {
  try {
    const raw: unknown = JSON.parse(sessionStorage.getItem(appPendingKey(transport)) ?? '[]');
    const values = (Array.isArray(raw) ? raw : []).filter((value) => object(value).clientRequestId !== command.clientRequestId);
    if (!remove) values.push(command);
    sessionStorage.setItem(appPendingKey(transport), JSON.stringify(values));
  } catch { /* The mounted surface retains its original operation too. */ }
}
export async function downloadLabApp(transport: ControlTransport, appId: string, version: number, target: 'paw' | 'standalone'): Promise<void> {
  const value = object(await requestLabControl(transport, { pathId: 'agent.eval-lab.apps.download', query: { appId, version, target } }));
  if (value.ok !== true || value.appId !== appId || value.version !== version || value.target !== target
      || typeof value.filename !== 'string' || typeof value.base64 !== 'string' || value.base64.length > 128_000_000
      || !natural(value.byteSize) || typeof value.sha256 !== 'string') throw new Error('应用包未完整返回。');
  const bytes = Uint8Array.from(atob(value.base64), (character) => character.charCodeAt(0));
  if (bytes.byteLength !== value.byteSize) throw new Error('应用包大小不一致，请重新下载。');
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  if (Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('') !== value.sha256) throw new Error('应用包内容不完整，请重新下载。');
  const url = URL.createObjectURL(new Blob([bytes], { type: 'application/zip' }));
  const link = document.createElement('a'); link.href = url; link.download = value.filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}
