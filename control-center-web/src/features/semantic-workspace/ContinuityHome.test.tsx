import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { ContinuityHome } from './ContinuityHome';
import type { SpaceFacts } from './continuity-model';

const facts: SpaceFacts = {
  key: 'session:one', title: '知识库实验', revision: 'v1', observedAtMs: 1000, running: false,
  goal: { configured: true, objective: '核实切片实验', successCriteria: '不修改正式知识库', status: 'active' },
  requests: [{ text: '等数据再验证，不改正式库', source: { kind: 'message', id: 'u1', revision: 'r1' } }],
  candidates: [{ id: 'goal', text: '核实切片实验', workItemId: '', source: { kind: 'goal', id: 'g1', revision: 'g1r' } }],
  decisions: [], blockers: [], sources: [{ kind: 'goal', id: 'g1', revision: 'g1r', label: '当前目标' }],
  missing: ['更早要求尚未完整核实'], pendingDecisions: [], deliveries: [],
  organization: { pinned: false, placement: 'desk' }, executionAllowed: true, contextPack: {},
};
let sequence = 0;
function setup(lose = false, rejection = false, onOpenIntent?: (key: string) => void) {
  const calls: string[] = [];
  const transport = new MockControlTransport({ routes: {
    'agent.continuity.read': { ok: true, items: [facts], failures: [] },
    'agent.continuity.suggest': { ok: true, proposal: { id: 'p1', spaceKey: facts.key, text: '在实验副本验证', revision: 'v1', expiresAtMs: Date.now() + 300_000, model: 'fixture' } },
    'agent.continuity.resume': (request: ControlRequest) => {
      const body = request.body as { commandId: string };
      calls.push(body.commandId);
      if (rejection) throw Object.assign(new Error('要求已变化，未执行旧计划'), { status: 409 });
      if (lose) { lose = false; throw new Error('回复丢失'); }
      return { ok: true, accepted: true, receipt: { turnId: 'owner-turn' } };
    },
  } });
  Object.defineProperty(transport, 'connectionIdentity', { value: `continuity-test-${++sequence}` });
  const onOpen = vi.fn();
  const tree = () => <ControlTransportProvider transport={transport}><ContinuityHome spaceKeys={[facts.key]} onOpen={onOpen} onOpenIntent={onOpenIntent} /></ControlTransportProvider>;
  return { transport, calls, onOpen, tree, view: render(tree()) };
}
beforeEach(() => localStorage.clear());
afterEach(cleanup);

describe('source-backed resumption', () => {
  it('warms only the exact open target on hover, focus and pointer intent without opening or resuming it', async () => {
    const intent = vi.fn();
    const { onOpen, transport } = setup(false, false, intent);
    const target = await screen.findByRole('button', { name: /^知识库实验/ });
    fireEvent.pointerEnter(target);
    fireEvent.focus(target);
    fireEvent.pointerDown(target);
    expect(intent.mock.calls).toEqual([[facts.key], [facts.key], [facts.key]]);
    expect(onOpen).not.toHaveBeenCalled();
    expect(transport.requests.every(call => call.request.pathId === 'agent.continuity.read')).toBe(true);
  });

  it('opens without executing and separates current facts from missing evidence', async () => {
    const { onOpen, transport } = setup();
    fireEvent.click(await screen.findByRole('button', { name: '查看进度：知识库实验' }));
    fireEvent.click(screen.getByText('待决定与最近交付'));
    await screen.findAllByText('等数据再验证，不改正式库');
    expect(screen.getByRole('heading', { name: '需要你决定' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '最近交付' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '打开工作空间' }));
    expect(onOpen).toHaveBeenCalledWith(facts.key);
    expect(transport.requests.every(call => call.request.pathId === 'agent.continuity.read')).toBe(true);
  });
  it('keeps the same execution key after a lost response and remount', async () => {
    const { tree, view, calls, onOpen } = setup(true);
    fireEvent.click(await screen.findByRole('button', { name: '查看进度：知识库实验' }));
    fireEvent.click(await screen.findByRole('button', { name: '梳理下一步' }));
    fireEvent.click(await screen.findByRole('button', { name: '继续这一步' }));
    await screen.findByText(/执行结果尚待核实/);
    view.unmount(); render(tree());
    fireEvent.click(await screen.findByRole('button', { name: '核实继续操作' }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(facts.key));
    expect(calls).toHaveLength(2); expect(calls[0]).toBe(calls[1]);
  });
  it('a definite stale-plan rejection does not leave an endless uncertain operation', async () => {
    setup(false, true);
    fireEvent.click(await screen.findByRole('button', { name: '查看进度：知识库实验' }));
    fireEvent.click(await screen.findByRole('button', { name: '梳理下一步' }));
    fireEvent.click(await screen.findByRole('button', { name: '继续这一步' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '核实继续操作' })).not.toBeInTheDocument());
    expect(screen.queryByRole('button', { name: '继续这一步' })).not.toBeInTheDocument();
  });
  it('lets the user select a source-backed step without requesting a model judgment', async () => {
    const { transport } = setup();
    fireEvent.click(await screen.findByRole('button', { name: '查看进度：知识库实验' }));
    fireEvent.click(await screen.findByText('我来选择下一步'));
    fireEvent.click(screen.getByRole('button', { name: '准备这一步' }));
    await screen.findByRole('button', { name: '继续这一步' });
    const call = transport.requests.find(entry => entry.request.pathId === 'agent.continuity.suggest');
    expect(call?.request.body).toEqual({ spaceKey: facts.key, expectedRevision: facts.revision, candidateId: 'goal' });
    expect(transport.requests.some(entry => entry.request.pathId === 'agent.continuity.resume')).toBe(false);
  });

});
