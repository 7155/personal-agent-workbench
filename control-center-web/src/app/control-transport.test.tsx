import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  ControlTransportProvider,
  createConfiguredControlTransport,
  useControlTransport,
} from '@/app/control-transport';
import { createPreviewTransport } from '@/app/preview-control-transport';

function Probe() {
  const transport = useControlTransport();
  return <output>{transport.kind}</output>;
}

describe('ControlTransportProvider', () => {
  it('does not reuse primary demo identities or expose sample history after a renderer reload', async () => {
    const first = createPreviewTransport();
    const second = createPreviewTransport();
    const before = await first.request<{ session: { id: string } }>({ pathId: 'agent.primary.ensure', body: {} });
    const after = await second.request<{ session: { id: string } }>({ pathId: 'agent.primary.ensure', body: {} });
    expect(after.session.id).not.toBe(before.session.id);
    const stale = await second.request<{ messages: unknown[]; liveEvents: unknown[] }>({ pathId: 'agent.session.snapshot', params: { sessionId: before.session.id } });
    expect(stale.messages).toEqual([]);
    expect(stale.liveEvents).toEqual([]);
    await expect(second.request({ pathId: 'agent.session.prompt', params: { sessionId: before.session.id }, body: { message: 'stale', clientMessageId: 'stale' } })).rejects.toThrow('演示会话已重置');
  });
  const originalUrl = window.location.href;

  beforeEach(() => {
    window.history.replaceState({}, '', '/?controlTransport=mock#/control-transport-tests');
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    window.history.replaceState({}, '', originalUrl);
  });

  it('provides the preview transport when no native bridge is present', () => {
    render(
      <ControlTransportProvider>
        <Probe />
      </ControlTransportProvider>,
    );
    expect(screen.getByText('mock')).toBeInTheDocument();
  });

  it('allows an explicit same-origin HTTP transport in development previews', async () => {
    const originalUrl = window.location.href;
    window.history.replaceState({}, '', '/?controlTransport=http#/context-debug');
    const fetchMock = vi.fn(async () => new Response(
      JSON.stringify({ sessions: [] }),
      { headers: { 'Content-Type': 'application/json' }, status: 200 },
    ));
    vi.stubGlobal('fetch', fetchMock);
    try {
      const transport = createConfiguredControlTransport();
      expect(transport.kind).toBe('http');
      await transport.request({ pathId: 'agent.sessions.list' });
      expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
        `${window.location.origin}/api/agent/sessions`,
      );
    } finally {
      vi.unstubAllGlobals();
      window.history.replaceState({}, '', originalUrl);
    }
  });

  it('uses the same-origin HTTP transport by default for PAWOS development', () => {
    const originalUrl = window.location.href;
    window.history.replaceState({}, '', '/?frontend=paw-os#/project-field');
    try {
      expect(createConfiguredControlTransport().kind).toBe('http');
    } finally {
      window.history.replaceState({}, '', originalUrl);
    }
  });

  it('honors an explicit mock build transport when the development URL has no override', () => {
    window.history.replaceState({}, '', '/?frontend=paw-os#/project-field');
    vi.stubEnv('VITE_CONTROL_TRANSPORT', 'mock');

    expect(createConfiguredControlTransport().kind).toBe('mock');
  });
  it('keeps a built mock renderer synthetic even when a native bridge and Electron host query exist', async () => {
    const previous = window.webkit;
    const postMessage = vi.fn();
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    window.history.replaceState({}, '', '/?pawHost=electron#/agent');
    vi.stubEnv('DEV', false);
    vi.stubEnv('VITE_CONTROL_TRANSPORT', 'mock');
    window.webkit = { messageHandlers: { ragImeNativeBridge: { postMessage } } };
    try {
      const transport = createConfiguredControlTransport();
      expect(transport.kind).toBe('mock');
      await transport.request({ pathId: 'agent.primary.ensure', body: {} });
      await transport.request({ pathId: 'memory.profile' });
      expect(postMessage).not.toHaveBeenCalled();
      expect(fetchSpy).not.toHaveBeenCalled();
    } finally { window.webkit = previous; fetchSpy.mockRestore(); }
  });

  it('keeps the preview memory surface representative and contract-valid', async () => {
    const transport = createConfiguredControlTransport();
    const summary = await transport.request<Record<string, unknown>>({ pathId: 'memory.summary' });
    const apps = await transport.request<Record<string, unknown>>({
      pathId: 'memory.pages',
      params: { kind: 'apps' },
      query: { limit: 50, cursor: '' },
    });
    const graph = await transport.request<Record<string, unknown>>({
      pathId: 'memory.graph.get',
      query: { plane: 'tags' },
    });
    const entity = await transport.request<Record<string, unknown>>({
      pathId: 'memory.entity.get',
      params: { kind: 'tag', entityId: 'agent-runtime' },
      query: { connectionsLimit: 40, membersLimit: 40 },
    });
    const status = await transport.request<Record<string, unknown>>({
      pathId: 'agent.memoryMaintenance.run',
      query: { limit: 12 },
    });

    expect(summary).toMatchObject({ appCount: 4, blockedFragmentCount: 4_887 });
    expect(apps.items).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'com.openai.codex' }),
    ]));
    expect(JSON.stringify(apps.items)).not.toContain('rawText');
    expect(graph).toMatchObject({ schemaVersion: 'rag-ime.memory-graph.v1', plane: 'tags' });
    expect(entity).toMatchObject({ schemaVersion: 'rag-ime.memory-entity.v1', entityId: 'agent-runtime' });
    expect(status).toMatchObject({ pendingDraftCount: 1 });
  });

  it('serves a contract-shaped Agent Lab matrix in mock/preview mode', async () => {
    const transport = createConfiguredControlTransport();
    const result = await transport.request<Record<string, unknown>>({ pathId: 'agent.eval-lab.runs' });

    expect(result).toMatchObject({
      schemaVersion: 'rag-ime.eval-lab-run-list.v1',
      ok: true,
      experimentTotal: 22,
      pathSearchTotal: 1,
    });
    expect(result.experiments).toEqual(expect.arrayContaining([
      expect.objectContaining({ experimentId: 'memory.personal-shadow-evaluation.v1', evaluationKind: 'memory' }),
      expect.objectContaining({ experimentId: 'agent-lab.model-cost.luna-max-validation.v1', evaluationKind: 'model_cost' }),
    ]));
    expect(result.items).toEqual(expect.arrayContaining([
      expect.objectContaining({ runId: 'enterpriseops-csm-baseline-validation-20260901-v5' }),
    ]));
  });

  it('provides interactive workflow, plugin, lifecycle and subagent preview fixtures', async () => {
    const transport = createConfiguredControlTransport();
    const workflow = await transport.request<Record<string, unknown>>({
      pathId: 'agent.session.workflow.get',
      params: { sessionId: 'session-preview' },
    });
    const pausedGoal = await transport.request<Record<string, unknown>>({
      pathId: 'agent.session.goal.mutate',
      params: { sessionId: 'session-preview' },
      body: { action: 'pause', expectedRevision: 1 },
    });
    const installed = await transport.request<Record<string, unknown>>({
      pathId: 'agent.extensions.list',
    });
    const catalog = await transport.request<Record<string, unknown>>({
      pathId: 'agent.extensions.catalog',
    });
    const proposals = await transport.request<Record<string, unknown>>({
      pathId: 'agent.extensions.proposals',
    });
    const hooks = await transport.request<Record<string, unknown>>({
      pathId: 'agent.lifecycleHooks.get',
      query: { limit: 20 },
    });
    await transport.request<Record<string, unknown>>({
      pathId: 'agent.lifecycleHooks.update',
      body: { eventType: 'project_complete', enabled: false },
    });
    const updatedHooks = await transport.request<Record<string, unknown>>({
      pathId: 'agent.lifecycleHooks.get',
      query: { limit: 20 },
    });
    const validation = await transport.request<Record<string, unknown>>({
      pathId: 'agent.extensions.validate',
      body: { catalogId: 'session-review', catalogVersion: '1.1.0' },
    });
    const extensionPreview = await transport.request<Record<string, unknown>>({
      pathId: 'agent.extensions.preview',
      body: {
        action: 'install',
        validationToken: String(validation.validationToken),
        enable: true,
      },
    });
    await transport.request<Record<string, unknown>>({
      pathId: 'agent.extensions.apply',
      body: {
        previewToken: String(extensionPreview.previewToken),
        payloadSha256: String(extensionPreview.payloadSha256),
        confirmText: 'apply',
      },
    });
    const installedAfterApply = await transport.request<Record<string, unknown>>({
      pathId: 'agent.extensions.list',
    });
    const consoleSnapshot = await transport.request<Record<string, unknown>>({
      pathId: 'agent.subagent.console',
      params: { runId: 'subagent-run:research' },
      query: { sessionId: 'session-preview' },
    });
    const controlReceipt = await transport.request<Record<string, unknown>>({
      pathId: 'agent.subagent.control',
      params: { runId: 'subagent-run:research' },
      body: {
        sessionId: 'session-preview',
        action: 'steer',
        clientActionId: 'preview-control-1',
        message: '继续核对前端',
      },
    });

    expect(workflow).toMatchObject({
      schemaVersion: 'rag-ime.agent-workflow-state.v1',
      todo: {
        phases: expect.arrayContaining([
          expect.objectContaining({ name: '实现' }),
          expect.objectContaining({ name: '验证' }),
        ]),
        counts: { total: 3, pending: 0, inProgress: 1, blocked: 1, completed: 1, abandoned: 0 },
      },
      goal: { configured: true },
    });
    expect(pausedGoal).toMatchObject({
      goal: { status: 'paused' },
      actGate: { allowed: false, reason: 'goal_paused' },
    });
    expect(installed.items).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'timeline-inspector' }),
    ]));
    expect(catalog.items).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'session-review', actionable: true }),
    ]));
    expect(proposals.items).toHaveLength(1);
    expect(hooks.policies).toHaveLength(6);
    expect(updatedHooks.policies).toEqual(expect.arrayContaining([
      expect.objectContaining({ eventType: 'project_complete', enabled: false }),
    ]));
    expect(installedAfterApply.items).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'session-review', enabled: true }),
    ]));
    expect(consoleSnapshot).toMatchObject({
      schemaVersion: 'rag-ime.agent-subagent-console.v1',
      capabilities: { steer: { available: true } },
    });
    expect(controlReceipt).toMatchObject({
      ok: true,
      replayed: false,
      clientActionId: 'preview-control-1',
    });
  });
});
