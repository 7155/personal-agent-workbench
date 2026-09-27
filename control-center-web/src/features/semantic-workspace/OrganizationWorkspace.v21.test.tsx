/** Run in PAW's real Vitest/React environment after merging.
 * These DOM tests are delivered but not counted in the standalone test report.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { OrganizationWorkspace } from './OrganizationWorkspace';
import type { OrganizationRequest, Space } from './organization-model';

const fixture = vi.hoisted(() => ({ connectionIdentity: 'organization-dom', request: vi.fn() }));
vi.mock('@/app/control-transport', () => ({ useControlTransport: () => fixture }));
let instance = 0;
const makeSpace = (i: number): Space => ({ key: `session:${i}`, title: `项目 ${i}`, category: 'unknown',
  group: '', placement: 'desk', pinned: false, revision: 0, sourceRevision: 's1', updatedAtMs: i });
function setupServer(items: Space[], dropFirstWrite = false) {
  const rows = new Map(items.map(row => [row.key, { ...row }]));
  const commands = new Map<string, unknown>();
  fixture.request.mockImplementation(async (request: OrganizationRequest) => {
    if (request.pathId === 'agent.organization.read') return { ok: true, items: request.body.keys.map(key => ({ ...rows.get(key)! })), receipts: [], unavailable: [] };
    if (request.pathId === 'agent.organization.command') {
      const body = request.body;
      if (commands.has(body.commandId)) return commands.get(body.commandId);
      const row = rows.get(body.spaceKey)!;
      if (body.operation === 'group') row.group = body.value;
      if (body.operation === 'category') row.category = body.value;
      row.revision++;
      const result = { ok: true, receiptId: body.commandId, replayed: false };
      commands.set(body.commandId, result);
      if (dropFirstWrite) { dropFirstWrite = false; throw new Error('fixture lost response'); }
      return result;
    }
    return { ok: true, proposal: null, message: '依据不足' };
  });
  return { rows, commands };
}
beforeEach(() => {
  fixture.connectionIdentity = `organization-dom-${++instance}`;
  fixture.request.mockReset();
  // The DOM suite exercises the wide non-modal presentation. Native narrow
  // modality/focus restoration is separately checked in the manual runbook.
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    x: 0, y: 0, top: 0, left: 0, bottom: 800, right: 1200, width: 1200, height: 800, toJSON: () => ({}),
  });
  // jsdom has no native dialog close; this suite verifies non-modal layout.
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value: function (this: HTMLDialogElement) { this.removeAttribute('open'); } });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('OrganizationWorkspace v2.1 integration', () => {
  it('searches across backend batches before frontend pagination without querying Jev', async () => {
    const items = Array.from({ length: 250 }, (_, i) => makeSpace(i)); items[0].title = '旧项目中的唯一需求';
    setupServer(items);
    render(<OrganizationWorkspace spaceKeys={items.map(row => row.key)} selectedKey="" onOpen={vi.fn()} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/已读取 250\/250 项/)).toBeTruthy());
    fireEvent.change(screen.getByLabelText('搜索已载入的全部空间'), { target: { value: '唯一需求' } });
    expect(await screen.findByRole('button', { name: '旧项目中的唯一需求' })).toBeTruthy();
    expect(fixture.request.mock.calls.every(([request]) => (request as OrganizationRequest).pathId === 'agent.organization.read')).toBe(true);
  });
  it('edits group metadata through the existing command route', async () => {
    const { rows } = setupServer([makeSpace(1)]);
    render(<OrganizationWorkspace spaceKeys={['session:1']} selectedKey="" onOpen={vi.fn()} onClose={vi.fn()} />);
    await screen.findByRole('button', { name: '项目 1' });
    fireEvent.click(screen.getByText('设置分组'));
    fireEvent.change(screen.getByLabelText('项目 1的分组'), { target: { value: 'RAG 实验' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(rows.get('session:1')!.group).toBe('RAG 实验'));
    expect(await screen.findByText('分组：RAG 实验')).toBeTruthy();
  });
  it('remounting the panel preserves an uncertain command for explicit same-ID reconciliation', async () => {
    const { rows, commands } = setupServer([makeSpace(1)], true);
    const props = { spaceKeys: ['session:1'], selectedKey: '', onOpen: vi.fn(), onClose: vi.fn() };
    const first = render(<OrganizationWorkspace {...props} />);
    await screen.findByRole('button', { name: '项目 1' });
    fireEvent.change(screen.getByLabelText('项目 1的用途'), { target: { value: 'waiting' } });
    await screen.findByText('有一项整理结果待确认'); first.unmount();
    render(<OrganizationWorkspace {...props} />);
    fireEvent.click(await screen.findByRole('button', { name: '核实上次操作' }));
    await waitFor(() => expect(screen.queryByText('有一项整理结果待确认')).toBeNull());
    expect(commands.size).toBe(1); expect(rows.get('session:1')!.revision).toBe(1);
  });
  it('opening a space only delegates navigation, never prompts or resumes', async () => {
    setupServer([makeSpace(1)]); const onOpen = vi.fn();
    render(<OrganizationWorkspace spaceKeys={['session:1']} selectedKey="" onOpen={onOpen} onClose={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: '项目 1' }));
    expect(onOpen).toHaveBeenCalledWith('session:1');
    expect(fixture.request.mock.calls.every(([request]) => (request as OrganizationRequest).pathId === 'agent.organization.read')).toBe(true);
  });
});
