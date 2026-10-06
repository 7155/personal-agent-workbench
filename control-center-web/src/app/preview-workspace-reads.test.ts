import { describe, expect, it } from 'vitest';
import { readSessionCatalog } from '@/paw-os/apps/session-catalog';
import type { SpaceFacts } from '@/features/semantic-workspace/continuity-model';
import type { ControlRequest } from '@/platform/transport';
import { createPreviewTransport } from './preview-control-transport';

describe('public preview workspace reads', () => {
  it('supports the real paginated Session reader while retaining the existing sessions projection', async () => {
    const transport = createPreviewTransport();
    const catalog = await readSessionCatalog(transport, false);
    expect(catalog.items).toContainEqual(expect.objectContaining({ id: 'session-preview' }));
    const direct = await transport.request<{ items: unknown[]; sessions: unknown[]; hasMore: boolean }>({ pathId: 'agent.sessions.list' });
    expect(direct.sessions).toEqual(catalog.items);
    expect(direct.items).toEqual(direct.sessions);
    expect(direct.hasMore).toBe(false);
    await transport.request({ pathId: 'agent.session.archive', params: { sessionId: 'session-preview' }, body: { archived: true } });
    expect((await readSessionCatalog(transport, false)).items).not.toContainEqual(expect.objectContaining({ id: 'session-preview' }));
    expect((await readSessionCatalog(transport, true)).items).toContainEqual(expect.objectContaining({ id: 'session-preview', status: 'archived' }));
  });

  it('discloses only known preview spaces and never fabricates execution or model receipts', async () => {
    const transport = createPreviewTransport();
    const input = { keys: ['session:session-preview', 'room:room-preview', 'session:missing'] };
    const value = await transport.request<{ ok: boolean; items: SpaceFacts[]; failures: { key: string; error: string }[] }>({ pathId: 'agent.continuity.read', body: input });
    expect(value.ok).toBe(true);
    expect(value.items.map(item => item.key)).toEqual(input.keys.slice(0, 2));
    expect(value.failures).toEqual([{ key: 'session:missing', error: expect.stringContaining('不存在') }]);
    for (const item of value.items) {
      expect(item).toMatchObject({ running: null, goal: null, executionAllowed: false, requests: [], decisions: [], candidates: [], deliveries: [], pendingDecisions: [], contextPack: {} });
      expect(item.missing).toContainEqual(expect.stringContaining('公开演示'));
      expect(item.revision).toMatch(/^preview:/);
    }
    const unavailableRequests: ControlRequest[] = [
      { pathId: 'agent.continuity.analyze', body: { spaceKey: input.keys[0], expectedRevision: value.items[0].revision } },
      { pathId: 'agent.continuity.suggest', body: { spaceKey: input.keys[0], expectedRevision: value.items[0].revision } },
      { pathId: 'agent.continuity.decision', body: { spaceKey: input.keys[0], expectedRevision: value.items[0].revision, id: 'preview-decision', text: '不能保存', supersedesId: '' } },
      { pathId: 'agent.continuity.resume', body: { spaceKey: input.keys[0], proposalId: 'preview-proposal', commandId: 'preview-command' } },
    ];
    for (const request of unavailableRequests) {
      await expect(transport.request(request)).rejects.toMatchObject({ status: 422, message: expect.stringContaining('本机服务') });
    }
    expect(await transport.request({ pathId: 'agent.continuity.read', body: input })).toMatchObject({ items: value.items.map(item => ({ ...item, observedAtMs: expect.any(Number) })), failures: value.failures });
  });
});
