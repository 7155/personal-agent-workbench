import { skipToken, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { useControlTransport } from '@/app/control-transport';
import type { ControlTransport } from '@/platform/transport';
import { labConnectionKey, requestLabControl } from '../control-request';
import { isArtifact, isProject, object, parseProjectRead, projectActions, type LabArtifact, type LabProject, type ProjectAction, type ProjectCommand, type ProjectRead, type ProjectReceipt } from './types';

type Pending = { command: ProjectCommand; outcome: 'sending' | 'unknown' };
export async function readLabProject(transport: ControlTransport, projectId = '', artifactId = '', revision?: number, signal?: AbortSignal): Promise<ProjectRead> {
  const raw = await requestLabControl(transport, { pathId: 'agent.eval-lab.projects.get',
    query: { ...(projectId ? { projectId } : {}), ...(artifactId ? { artifactId } : {}), ...(revision ? { artifactRevision: revision } : {}) }, signal });
  return parseProjectRead(raw, projectId, artifactId);
}
export async function commandLabProject(transport: ControlTransport, command: ProjectCommand): Promise<ProjectReceipt> {
  const raw = object(await requestLabControl(transport, { pathId: 'agent.eval-lab.projects.command', body: command }));
  if (raw.ok === false) throw Object.assign(new Error(projectError({ payload: raw })), { payload: raw });
  if (raw.ok !== true || raw.clientRequestId !== command.clientRequestId || typeof raw.replayed !== 'boolean'
      || !isProject(raw.project) || (command.projectId && raw.project.projectId !== command.projectId)
      || (raw.artifact !== undefined && !isArtifact(raw.artifact))) throw new Error('项目回执与本次操作不匹配，请核对原操作。');
  return raw as ProjectReceipt;
}
export function projectError(error: unknown, fallback = '项目服务暂时不可用，请重新读取。'): string {
  const value = object(error); const details = object(value.details); const payload = object(value.payload ?? details.payload ?? details.body ?? value.details);
  const message = typeof payload.message === 'string' ? payload.message : typeof payload.error === 'string' ? payload.error : error instanceof Error ? error.message : '';
  return !message || /^(?:failed to fetch|fetch failed|load failed|network request failed|NetworkError when attempting to fetch resource\.?)$/iu.test(message) ? fallback : message;
}
export function projectCommandRejected(error: unknown): boolean {
  const value = object(error); const details = object(value.details); const status = value.status ?? details.status ?? details.statusCode;
  const payload = object(value.payload ?? details.payload ?? details.body ?? value.details);
  return status === 404 || status === 409 || status === 422 || /(?:CONFLICT|INVALID_REQUEST|NOT_FOUND)$/u.test(String(payload.code ?? ''));
}
export function useLabProjects(projectId: string) {
  const transport = useControlTransport(); const client = useQueryClient(); const connection = labConnectionKey(transport);
  const prefix = ['lab-projects', connection] as const; const catalogKey = [...prefix, 'catalog'] as const;
  const projectKey = [...prefix, 'project', projectId] as const; const pendingKey = [...prefix, 'command'] as const;
  const catalog = useQuery({ queryKey: catalogKey, queryFn: ({ signal }) => readLabProject(transport, '', '', undefined, signal),
    retry: false, refetchOnWindowFocus: false, refetchInterval: (query) => projectId || query.state.status === 'error' ? 3000 : false, refetchIntervalInBackground: false });
  const project = useQuery({ queryKey: projectKey, queryFn: projectId ? ({ signal }) => readLabProject(transport, projectId, '', undefined, signal) : skipToken,
    retry: false, refetchOnWindowFocus: false,
    // A recovered catalog can have the same revision: retry failed reads independently.
    // While a Golden/Pi job is queued or running, refresh the read-only
    // execution projection. This observes the real owner without starting a
    // second worker or replaying a command.
    refetchInterval: (query) => {
      const data = query.state.data; const current = data?.project;
      const active = current?.bindings.some((binding) => ['queued', 'running'].includes(binding.execution?.status ?? ''))
        || Boolean(current?.workflow?.counts.running || current?.workflow?.counts.queued)
        || data?.knowledge?.jobs.some((job) => ['queued', 'running', 'cancelling'].includes(job.state));
      // Jobs can advance or start without changing the project revision. Keep
      // the visible project observable even when its Guide is idle or closed.
      return active ? 2000 : projectId ? 3000 : false;
    }, refetchIntervalInBackground: false });
  const observedRevision = catalog.data?.items.find((item) => item.projectId === projectId)?.revision;
  const savedRevision = project.data?.project?.revision;
  useEffect(() => {
    if (observedRevision !== undefined && savedRevision !== undefined && observedRevision > savedRevision) void client.invalidateQueries({ queryKey: [...prefix, 'project', projectId] });
  }, [client, connection, observedRevision, savedRevision, projectId]);
  const pending = useQuery<Pending | null>({ queryKey: pendingKey, queryFn: skipToken, gcTime: Infinity, initialData: () => restore(transport) });
  const mutation = useMutation({ retry: false, mutationFn: async (command: ProjectCommand) => {
    client.setQueryData<Pending>(pendingKey, { command, outcome: 'sending' }); persist(transport, command);
    try {
      const receipt = await commandLabProject(transport, command);
      const key = [...prefix, 'project', receipt.project.projectId]; const existing = client.getQueryData<ProjectRead>(key);
      const accepted = existing?.project && existing.project.revision > receipt.project.revision ? existing.project : receipt.project;
      const list = client.getQueryData<ProjectRead>(catalogKey);
      const items = [...(list?.items ?? []).filter((item) => item.projectId !== accepted.projectId), accepted].sort((a, b) => b.updatedAtMs - a.updatedAtMs);
      client.setQueryData<ProjectRead>(key, { ok: true, supportedViews: list?.supportedViews ?? [], items, project: accepted });
      client.setQueryData<ProjectRead>(catalogKey, { ...list, ok: true, supportedViews: list?.supportedViews ?? [], items, project: null });
      if (receipt.artifact) client.setQueryData<LabArtifact>([...prefix, 'artifact', receipt.project.projectId, receipt.artifact.artifactId, receipt.artifact.revision], receipt.artifact);
      client.setQueryData(pendingKey, null); persist(transport, null);
      return receipt;
    } catch (error) {
      const definite = projectCommandRejected(error); client.setQueryData(pendingKey, definite ? null : { command, outcome: 'unknown' });
      if (definite) persist(transport, null);
      throw error;
    }
  }, onSettled: (_value, error) => {
    if (!error || projectCommandRejected(error)) {
      void client.invalidateQueries({ queryKey: catalogKey });
      if (projectId) void client.invalidateQueries({ queryKey: projectKey });
    }
  } });
  return { connection, catalog, project, mutation, pending: pending.data ?? null,
    async submit(action: ProjectAction, input: ProjectCommand['input'], target?: LabProject | null) {
      if (client.getQueryData<Pending | null>(pendingKey)) return undefined;
      return mutation.mutateAsync({ action, ...(target ? { projectId: target.projectId } : {}), expectedRevision: target?.revision ?? 0,
        clientRequestId: `lab-project:${crypto.randomUUID()}`, input: structuredClone(input) }).catch(() => undefined);
    },
    async reconcile() { const value = client.getQueryData<Pending | null>(pendingKey); return value?.outcome === 'unknown' ? mutation.mutateAsync(value.command).catch(() => undefined) : undefined; },
  };
}
export function useLabArtifact(projectId: string, artifactId: string, revision?: number) {
  const transport = useControlTransport(); const connection = labConnectionKey(transport);
  return useQuery({ queryKey: ['lab-projects', connection, 'artifact', projectId, artifactId, revision],
    queryFn: projectId && artifactId && revision ? async ({ signal }) => (await readLabProject(transport, projectId, artifactId, revision, signal)).artifact! : skipToken,
    retry: false, refetchOnWindowFocus: false, staleTime: Infinity,
    refetchInterval: (query) => query.state.status === 'error' ? 3000 : false, refetchIntervalInBackground: false });
}
function storageKey(transport: ControlTransport) { return transport.connectionIdentity ? `paw.lab.project-command.v1:${labConnectionKey(transport)}` : ''; }
function persist(transport: ControlTransport, value: ProjectCommand | null) {
  const key = storageKey(transport); if (!key) return;
  try { if (value) sessionStorage.setItem(key, JSON.stringify(value)); else sessionStorage.removeItem(key); } catch { /* Query cache retains the original command. */ }
}
function restore(transport: ControlTransport): Pending | null {
  const key = storageKey(transport); if (!key) return null;
  try { const value = object(JSON.parse(sessionStorage.getItem(key) ?? 'null'));
    return projectActions.includes(value.action as ProjectAction) && Number.isSafeInteger(value.expectedRevision) && Number(value.expectedRevision) >= 0
      && typeof value.clientRequestId === 'string' && value.clientRequestId.startsWith('lab-project:') && value.clientRequestId.length <= 240
      && (value.projectId === undefined || typeof value.projectId === 'string') && value.input !== null && typeof value.input === 'object' && !Array.isArray(value.input)
      ? { command: value as ProjectCommand, outcome: 'unknown' } : null;
  } catch { return null; }
}
