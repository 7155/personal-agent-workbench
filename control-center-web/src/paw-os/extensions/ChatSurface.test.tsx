import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { agentProjection, agentSessionAddress, useAgentLiveStore } from '@/features/agent/state/live-store';
import type { SessionSummary } from '@/features/agent/types';
import { recoveryScope } from '@/features/semantic-workspace/workspace-recovery';
import { StubControlTransport } from '@/test/stub-control-transport';
import { chatMessageWithContextV1, chatSurfaceCompatibility, PawChatSurface } from './ChatSurface';

afterEach(() => { cleanup(); useAgentLiveStore.setState({ projections: {} }); });
const session: SessionSummary = { id: 'vertical-v1', title: '业务对话', mode: 'assistant', status: 'idle', roleId: '', roleVersion: '', roleBookRevisionId: '', updatedAtMs: 1, workspaceRoots: [], executionMode: 'read_only' };

describe('OS ChatSurface V1 contract', () => {
  it('accepts only an implemented major and its real UI features', () => {
    expect(chatSurfaceCompatibility({ major: 1, features: ['embedded', 'composer-context', 'draft-intent', 'composer-header'] })).toEqual({ supported: true });
    expect(chatSurfaceCompatibility({ major: 2 }).supported).toBe(false);
    expect(chatSurfaceCompatibility({ major: 1, features: ['grant-tools'] }).supported).toBe(false);
    expect(chatSurfaceCompatibility(JSON.parse('{"major":1,"features":null}')).supported).toBe(false);
  });

  it('rejects an unsupported major without opening, mutating, or deleting its Session projection', () => {
    const transport = new StubControlTransport('mock', {});
    const address = agentSessionAddress(transport, session.id);
    useAgentLiveStore.getState().appendOptimistic(address, { clientMessageId: 'kept', text: '尚未确认的输入', nowMs: 1 });
    const before = agentProjection(address);
    render(<ControlTransportProvider transport={transport}><PawChatSurface api={{ major: 2 }} session={session} onNewConversation={vi.fn()} onSessionCreated={vi.fn()} onSessionUpdated={vi.fn()} /></ControlTransportProvider>);
    expect(screen.getByRole('alert')).toHaveTextContent('当前工作台支持 v1');
    expect(screen.queryByRole('textbox', { name: '消息' })).not.toBeInTheDocument();
    expect(transport.requests).toHaveLength(0);
    expect(agentProjection(address)).toBe(before);
  });

  it('preserves the actual composer draft across an incompatible view and return, without injecting primary-assistant navigation', async () => {
    const transport = new StubControlTransport('mock', {
      'agent.session.snapshot': { sessionId: session.id, messages: [], liveEvents: [], lastSequence: 0, status: 'idle', partial: true, snapshotScope: 'recent', runtimeQuiescent: true },
      'agent.session.models': {}, 'agent.session.commands': {}, 'agent.runtime.get': {},
      'agent.tools.list': { schemaVersion: 'rag-ime.capability-catalog.v1', ok: true, revision: 'v1', effectiveAtMs: 1, projectScope: { supported: false },
        sessionPolicy: { sessionId: session.id, policyRevision: 1, effectiveAtMs: 1, disclosurePreferences: {} }, items: [] },
    });
    Object.defineProperty(transport, 'connectionIdentity', { value: `chat-surface-${crypto.randomUUID()}` });
    const tree = (major: number) => <ControlTransportProvider transport={transport}><TooltipProvider><PawChatSurface
      api={{ major }} session={session} view="embedded" composer={{ placeholder: '继续这个业务问题', header: view => <small>{view.session.title} · 垂直场景</small> }}
      onNewConversation={vi.fn()} onSessionCreated={vi.fn()} onSessionUpdated={vi.fn()} /></TooltipProvider></ControlTransportProvider>;
    const view = render(tree(1));
    const input = await screen.findByRole('textbox', { name: '消息' });
    expect(input).toHaveAttribute('placeholder', '继续这个业务问题');
    fireEvent.change(input, { target: { value: '未发送的业务草稿' } });
    await waitFor(() => expect(JSON.parse(localStorage.getItem(recoveryScope(transport, `session:${session.id}`)) ?? '{}').draft).toBe('未发送的业务草稿'));
    view.rerender(tree(2));
    expect(screen.getByRole('alert')).toHaveTextContent('当前工作台支持 v1');
    view.rerender(tree(1));
    expect(await screen.findByRole('textbox', { name: '消息' })).toHaveValue('未发送的业务草稿');
    expect(screen.getByText('业务对话 · 垂直场景')).toBeVisible();
    expect(screen.queryByRole('button', { name: /交给助手做|返回我的助手/ })).not.toBeInTheDocument();
    expect(transport.requests.some(request => ['agent.sessions.create', 'agent.session.prompt', 'agent.session.mode.update', 'agent.session.delete'].includes(request.pathId))).toBe(false);
    view.unmount();
    localStorage.removeItem(recoveryScope(transport, `session:${session.id}`));
  });

  it('keeps business context in the existing user-message formatting contract', () => {
    const text = chatMessageWithContextV1('分析所选范围', { label: '地图选择', detail: '1个地块', text: '{"type":"FeatureCollection","features":[]}', onClear() {} });
    expect(text).toContain('分析所选范围');
    expect(text).toContain('地图上下文：地图选择 · 1个地块');
    expect(chatMessageWithContextV1('/help', { label: '忽略', detail: '', text: '不得覆盖产品命令', onClear() {} })).toBe('/help');
  });
});
