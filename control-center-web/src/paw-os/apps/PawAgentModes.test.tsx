import { useEffect, useState } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { MockControlTransport } from '@/test/mock-transport';
import type { ControlRequest } from '@/platform/transport';
import { agentModeStore } from '@/features/semantic-workspace/agent-mode-store';
import { rememberRoomEntryMode } from '@/features/semantic-workspace/room-entry-mode';
import { PawAgentApp } from './PawAgentApp';

const lifetime = vi.hoisted(() => ({ mounts: 0, unmounts: 0 }));
function StatefulWorkspace({ interfaceMode = 'session', onJevEvents }: { interfaceMode?: string; onJevEvents?: (events: readonly unknown[]) => void }) {
  const [draft, setDraft] = useState('');
  useEffect(() => { lifetime.mounts++; return () => { lifetime.unmounts++; }; }, []);
  return <><textarea aria-label="测试中的工作草稿" data-interface-mode={interfaceMode} value={draft} onChange={event => setDraft(event.target.value)} />{onJevEvents ? <button onClick={() => onJevEvents([{ roomId: 'room-one', eventType: 'participant_status', payload: { status: 'jev_updated', graphId: 'existing-graph' } }])}>测试收到 JEV 执行事件</button> : null}</>;
}
vi.mock('./PawSessionWorkspace', () => ({ PawSessionWorkspace: () => <StatefulWorkspace /> }));
vi.mock('./PawRoomWorkspace', () => ({ PawRoomWorkspace: (props: { interfaceMode: string; onJevEvents?: (events: readonly unknown[]) => void }) => <StatefulWorkspace {...props} /> }));

afterEach(() => { cleanup(); localStorage.removeItem('paw.agent.interface-mode.v1'); lifetime.mounts = 0; lifetime.unmounts = 0; });

function mount(route = '/agent?session=one', { hasGraph = false, entryMode, roomKind = 'collaboration' }: { hasGraph?: boolean; entryMode?: 'traditional' | 'jev'; roomKind?: string } = {}) {
  const transport = new MockControlTransport({ routes: {
    'agent.sessions.list': { ok: true, items: [{ id: 'one', title: '当前工作', mode: 'assistant', status: 'idle', updatedAtMs: 1, workspaceRoots: [] }] },
    'agent.rooms.list': { ok: true, items: [] }, 'agent.roles.list': { ok: true, items: [] },
    'agent.role.models': { providers: [], selected: {} },
    'configuration.settings': { ok: true, settings: {} },
    'agent.organization.read': { ok: true, items: [], receipts: [], unavailable: [] },
    'agent.room.get': (request: ControlRequest) => ({ ok: true, room: { id: request.params?.roomId, roomKind, routingPolicy: 'jev' } }),
    'agent.jev.get': (request: ControlRequest) => ({ ok: true, mode: 'jev', items: hasGraph ? [{ graph_id: 'existing-graph', room_id: request.params?.roomId }] : [] }),
  } });
  if (entryMode) rememberRoomEntryMode(transport, 'room-one', entryMode);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><TooltipProvider><PawAgentApp initialRoute={route} /></TooltipProvider></ControlTransportProvider></QueryClientProvider>);
  return { ...view, transport, setHasGraph: (value: boolean) => { hasGraph = value; } };
}

describe('Agent traditional / Jev modes', () => {
  it('follows newly received graph evidence without replacing the open workspace or its draft', async () => {
    const user = userEvent.setup();
    const { transport, setHasGraph } = mount('/agent?room=room-one');
    const draft = await screen.findByRole('textbox', { name: '测试中的工作草稿' });
    expect(draft).toHaveAttribute('data-interface-mode', 'traditional');
    await user.type(draft, '等待中的补充');
    setHasGraph(true);
    await user.click(screen.getByRole('button', { name: '测试收到 JEV 执行事件' }));
    await waitFor(() => expect(draft).toHaveAttribute('data-interface-mode', 'jev'));
    expect(screen.getByRole('textbox', { name: '测试中的工作草稿' })).toBe(draft);
    expect(draft).toHaveValue('等待中的补充');
    expect(lifetime.mounts).toBe(1); expect(lifetime.unmounts).toBe(0);
    expect(transport.requests.some(({ request }) => /command|prompt|create|abort/.test(request.pathId))).toBe(false);
  });
  it('opens a new Agent in Jev by default while keeping an ordinary Session selectable', async () => {
    const { transport } = mount('/agent');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Jev' })).toHaveAttribute('aria-pressed', 'true'));
    expect(screen.getByRole('radio', { name: 'Room' })).toBeChecked();
    await userEvent.setup().click(screen.getByRole('radio', { name: 'Session' }));
    expect(screen.getByRole('radio', { name: 'Session' })).toBeChecked();
    expect(screen.getByRole('button', { name: '开始 Session' })).toBeInTheDocument();
    expect(transport.requests.some(call => /create|command|prompt/.test(call.request.pathId))).toBe(false);
  });
  it('respects an explicit traditional preference for a new Agent', () => {
    agentModeStore.select('traditional');
    mount('/agent');
    expect(screen.getByRole('button', { name: '传统' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('radio', { name: 'Session' })).toBeChecked();
  });
  it.each(['/agent?session=one', '/agent?room=room-one'])('keeps the same workspace and draft across switches: %s', async route => {
    const user = userEvent.setup();
    const { transport } = mount(route);
    const draft = await screen.findByRole('textbox', { name: '测试中的工作草稿' });
    await user.type(draft, '尚未发送的修正');
    await user.click(screen.getByRole('button', { name: '工作台选项' }));
    const switchToJev = screen.queryByRole('menuitem', { name: '切换到Jev界面' });
    if (switchToJev) {
      await user.click(switchToJev);
      await user.click(screen.getByRole('button', { name: '工作台选项' }));
    }
    await user.click(screen.getByRole('menuitem', { name: '工作空间' }));
    await screen.findByRole('complementary', { name: 'Jev 工作空间目录' });
    await user.click(screen.getByRole('button', { name: '工作台选项' }));
    await user.click(screen.getByRole('menuitem', { name: '切换到传统界面' }));
    expect(screen.getByRole('textbox', { name: '测试中的工作草稿' })).toBe(draft);
    expect(draft).toHaveValue('尚未发送的修正');
    expect(lifetime.mounts).toBe(1); expect(lifetime.unmounts).toBe(0);
    expect(draft).toHaveAttribute('data-interface-mode', route.includes('room=') ? 'traditional' : 'session');
    expect(transport.requests.some(({ request }) => /prompt|message|abort|create|command|suggest|undo/.test(request.pathId))).toBe(false);
  });

  it('keeps an empty historical Room traditional despite a global Jev preference and legacy Jev routing policy', async () => {
    agentModeStore.select('jev');
    const { transport } = mount('/agent?room=room-one');
    expect(await screen.findByRole('textbox', { name: '测试中的工作草稿' })).toHaveAttribute('data-interface-mode', 'traditional');
    expect(transport.requests.some(call => call.request.pathId === 'agent.jev.command' || call.request.pathId === 'agent.room.message')).toBe(false);
  });
  it('opens an existing Jev graph independently of a global traditional preference', async () => {
    agentModeStore.select('traditional');
    mount('/agent?room=room-one', { hasGraph: true });
    expect(await screen.findByRole('textbox', { name: '测试中的工作草稿' })).toHaveAttribute('data-interface-mode', 'jev');
  });
  it('retains the explicit new Jev Room entry while its first graph is still pending', async () => {
    agentModeStore.select('traditional');
    mount('/agent?room=room-one', { entryMode: 'jev' });
    expect(await screen.findByRole('textbox', { name: '测试中的工作草稿' })).toHaveAttribute('data-interface-mode', 'jev');
  });
  it('keeps roleplay on its original Room surface', async () => {
    agentModeStore.select('jev');
    const { transport } = mount('/agent?room=room-one', { hasGraph: true, roomKind: 'roleplay' });
    expect(await screen.findByRole('textbox', { name: '测试中的工作草稿' })).toHaveAttribute('data-interface-mode', 'traditional');
    expect(transport.requests.some(call => call.request.pathId === 'agent.jev.get')).toBe(false);
  });

  it('preserves the real home composer text and remembers the mode on reopening', async () => {
    const user = userEvent.setup(); const view = mount('/agent');
    const input = screen.getAllByRole('textbox').find(node => node.tagName === 'TEXTAREA')!;
    await user.type(input, '先不要开始，保留草稿');
    await user.click(screen.getByRole('button', { name: 'Jev' }));
    expect(screen.getByRole('heading', { name: '今天想完成什么？' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '继续工作' })).toBeInTheDocument();
    expect(input.compareDocumentPosition(screen.getByRole('region', { name: '项目复工' })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(input).toHaveValue('先不要开始，保留草稿'); expect(input.isConnected).toBe(true);
    view.unmount(); mount('/agent');
    await waitFor(() => expect(within(screen.getByRole('group', { name: 'Agent 界面模式' })).getByRole('button', { name: 'Jev' })).toHaveAttribute('aria-pressed', 'true'));
  });
});
