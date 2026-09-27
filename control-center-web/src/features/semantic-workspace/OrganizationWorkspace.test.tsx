import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import type { ControlRequest } from '@/platform/transport';
import { OrganizationWorkspace } from './OrganizationWorkspace';

afterEach(cleanup);
const original = { key: 'session:one', title: 'Jev 原理', category: 'unknown', placement: 'desk', pinned: false, group: '', revision: 0 };

function setup(suggest?: () => Promise<unknown>) {
  let item = { ...original }; let receipts: { id: string; spaceKey: string }[] = [];
  const onOpen = vi.fn();
  const transport = new MockControlTransport({ routes: {
    'agent.organization.read': () => ({ ok: true, items: [{ ...item }], receipts, unavailable: [] }),
    'agent.organization.suggest': suggest ?? { ok: true, proposal: { id: 'p1', spaceKey: original.key, category: 'reference', basis: '仅依据当前标题，尚未读取对话正文', expiresAtMs: Date.now() + 300_000 } },
    'agent.organization.command': (request: ControlRequest) => {
      const body = request.body as Record<string, unknown>;
      if (body.expectedRevision !== item.revision) throw new Error('revision conflict');
      if (body.operation === 'proposal') item.category = 'reference';
      if (body.operation === 'placement') item.placement = body.value as string;
      item.revision++;
      receipts = [{ id: String(body.commandId), spaceKey: item.key }];
      return { ok: true, receiptId: body.commandId };
    },
    'agent.organization.undo': () => { item = { ...original, revision: item.revision + 1 }; receipts = []; return { ok: true }; },
  } });
  const tree = () => <ControlTransportProvider transport={transport}><OrganizationWorkspace spaceKeys={[original.key]} selectedKey="" onOpen={onOpen} onClose={() => {}} /></ControlTransportProvider>;
  return { transport, onOpen, tree, view: render(tree()) };
}

describe('Jev organization live port', () => {
  it('suggests, applies, shelves, reopens and undoes through owner routes', async () => {
    const user = userEvent.setup(); const { view, tree, transport, onOpen } = setup();
    await screen.findByRole('button', { name: original.title });
    await user.click(screen.getByRole('button', { name: 'Jev 分类建议' }));
    await screen.findByText('建议归为「参考资料」');
    await user.click(screen.getByRole('button', { name: '采用' }));
    await waitFor(() => expect(screen.getByLabelText('Jev 原理的用途')).toHaveValue('reference'));
    await user.click(screen.getByRole('button', { name: '收起入口' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: original.title })).not.toBeInTheDocument());
    view.unmount(); render(tree());
    await user.click(screen.getByRole('button', { name: /^已收起/ }));
    await screen.findByRole('button', { name: original.title });
    await user.click(screen.getByRole('button', { name: original.title }));
    expect(onOpen).toHaveBeenCalledWith(original.key);
    await user.click(screen.getByText(/可撤销的整理/));
    await user.click(screen.getByRole('button', { name: /撤销「Jev 原理」/ }));
    await user.click(screen.getByRole('button', { name: /^当前工作/ }));
    await screen.findByRole('button', { name: original.title });
    expect(transport.requests.every(({ request }) => request.pathId.startsWith('agent.organization.'))).toBe(true);
  });

  it('ignores late suggestions after closing and does not turn them into commands', async () => {
    let resolve!: (value: unknown) => void;
    const promise = new Promise(resolvePromise => { resolve = resolvePromise; });
    const user = userEvent.setup(); const { view, transport } = setup(() => promise);
    await screen.findByRole('button', { name: 'Jev 分类建议' });
    await user.click(screen.getByRole('button', { name: 'Jev 分类建议' }));
    view.unmount();
    await act(async () => { resolve({ ok: true, proposal: { id: 'late', spaceKey: original.key, category: 'reference', basis: '标题' } }); });
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.organization.command')).toBe(false);
  });
});
