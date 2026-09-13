import { QueryClient, QueryClientProvider, type UseQueryResult } from '@tanstack/react-query';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import type { ControlRequest } from '@/platform/transport';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { useLabArtifact, useLabProjects } from './api';
import type { LabProject, ProjectCommand } from './types';

const project = (patch: Partial<LabProject> = {}): LabProject => ({
  schemaVersion: 'rag-ime.agent-lab-project.v1', projectId: 'project-1', revision: 1, title: '售后助手', description: '让客服依据新的规则完成任务', briefVersion: 1,
  materialCount: 0, artifactCount: 0, guideSessionId: '', createdAtMs: 1, updatedAtMs: 1, materialSetId: '',
  materialSet: { materialSetId: '', version: 0, materials: [], createdAtMs: null }, materialVersions: [],
  intake: { state: 'needs_materials', requestedPath: '', resolvedPath: '', readCount: 0, readBytes: 0, skippedCount: 0, partial: false, issues: [], checkedAtMs: null },
  artifacts: [], bindings: [], workspace: { artifactOrder: [], primaryArtifactId: '', layout: 'split' }, workspaceBinding: null, ...patch,
});

const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); });
it.each(['knowledge', 'workflow'] as const)('observes same-revision %s work completing without Guide activity or commands', async (owner) => {
  vi.useFakeTimers();
  try {
    let running = true;
    const transport = new MockControlTransport({ routes: { 'agent.eval-lab.projects.get': ({ query }: ControlRequest) => {
      const current = project(owner === 'workflow' ? { workflow: { schemaVersion: 'paw.lab-project-workflow.v1', observedAtMs: 1, nodes: [], edges: [], currentNodeId: null,
        counts: { running: running ? 1 : 0, queued: 0, completed: running ? 0 : 1, failed: 0 } } } : {});
      return { ok: true, items: [current], project: query?.projectId ? current : null, supportedViews: [],
        ...(owner === 'knowledge' ? { knowledge: { schemaVersion: 'paw.lab-knowledge-resource.v1', jobs: [{ jobId: 'knowledge-job', state: running ? 'running' : 'completed' }], corpora: [], indexes: [], datasets: [], evaluations: [], embedding: { provider: 'none', model: '' } } } : {}) };
    } } });
    const client = new QueryClient(); clients.push(client);
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}><ControlTransportProvider transport={transport}>{children}</ControlTransportProvider></QueryClientProvider>;
    const { result } = renderHook(() => useLabProjects('project-1'), { wrapper });
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    running = false;
    await act(async () => { await vi.advanceTimersByTimeAsync(2001); });
    expect(result.current.project.data?.project?.revision).toBe(1);
    if (owner === 'knowledge') expect(result.current.project.data?.knowledge?.jobs[0]?.state).toBe('completed');
    else expect(result.current.project.data?.project?.workflow?.counts.completed).toBe(1);
    expect(transport.requests.every(({ request }) => request.pathId === 'agent.eval-lab.projects.get')).toBe(true);
  } finally { vi.useRealTimers(); }
});
it.each(['catalog', 'artifact'] as const)('recovers the initial %s read automatically and stops polling after success', async (kind) => {
  let offline = true;
  const artifact = { artifactId: 'artifact-1', revision: 1, title: '观察', kind: 'investigation', view: 'markdown', content: '已恢复的成果', summary: '', templateRef: null, actions: [], createdAtMs: 1, updatedAtMs: 1 };
  const transport = new MockControlTransport({ routes: { 'agent.eval-lab.projects.get': () => {
    if (offline) throw new Error('Failed to fetch');
    return { ok: true, items: [], project: kind === 'artifact' ? project() : null, supportedViews: ['markdown'], ...(kind === 'artifact' ? { artifact } : {}) };
  } } });
  const client = new QueryClient(); clients.push(client);
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}><ControlTransportProvider transport={transport}>{children}</ControlTransportProvider></QueryClientProvider>;
  const useRead: () => UseQueryResult<unknown, Error> = kind === 'artifact' ? () => useLabArtifact('project-1', 'artifact-1', 1) : () => useLabProjects('').catalog;
  const { result } = renderHook(useRead, { wrapper });
  await waitFor(() => expect(result.current.isError).toBe(true));
  offline = false;
  await waitFor(() => expect(result.current.isSuccess).toBe(true), { timeout: 4500 });
  const settledReads = transport.requests.length;
  await new Promise((resolve) => setTimeout(resolve, 3300));
  expect(transport.requests).toHaveLength(settledReads);
  expect(transport.requests.every(({ request }) => request.pathId === 'agent.eval-lab.projects.get')).toBe(true);
});


it('preserves an expired prepare command and reconciles the same identity without a second preparation', async () => {
  vi.useFakeTimers();
  try {
    const commands: ProjectCommand[] = [];
    const current = project();
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.projects.get': () => ({ ok: true, items: [current], project: current, supportedViews: [] }),
      'agent.eval-lab.projects.command': ({ body }: ControlRequest) => {
        commands.push(body as ProjectCommand);
        if (commands.length === 1) return new Promise(() => {});
        return { ok: true, project: project({ revision: 2 }), clientRequestId: (body as ProjectCommand).clientRequestId, replayed: true };
      },
    } });
    const client = new QueryClient(); clients.push(client);
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}><ControlTransportProvider transport={transport}>{children}</ControlTransportProvider></QueryClientProvider>;
    const { result } = renderHook(() => useLabProjects(current.projectId), { wrapper });
    let submitted!: ReturnType<typeof result.current.submit>;
    await act(async () => { submitted = result.current.submit('prepare_app', { directory: 'polar-research-app' }, current); });
    await act(async () => { await vi.advanceTimersByTimeAsync(202_000); });
    expect(result.current.pending?.outcome).toBe('sending');
    expect(await result.current.submit('prepare_app', { directory: 'polar-research-app' }, current)).toBeUndefined();
    expect(commands).toHaveLength(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(98_000); await submitted; await vi.advanceTimersByTimeAsync(1); });
    expect(result.current.pending?.outcome).toBe('unknown');
    expect(result.current.pending?.command).toEqual(commands[0]);
    await act(async () => { await result.current.reconcile(); await vi.advanceTimersByTimeAsync(0); });
    expect(commands).toHaveLength(2); expect(commands[1]).toEqual(commands[0]);
    expect(result.current.pending).toBeNull();
  } finally { vi.useRealTimers(); }
});
