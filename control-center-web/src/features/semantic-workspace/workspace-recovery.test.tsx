import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { recoveryScope, useWorkspaceRecovery, WorkspaceRecoveryNotice } from './workspace-recovery';

beforeEach(() => localStorage.clear());
afterEach(cleanup);
function Harness({ owner = 'session:one' }: { owner?: string }) {
  const recovery = useWorkspaceRecovery<{ id: string; sha256: string; name: string }>(owner);
  return <><input aria-label="草稿" value={recovery.draft} onChange={e => recovery.setDraft(e.target.value)} />
    <button onClick={() => recovery.setAttachments([{ id: 'media-one', sha256: 'hash', name: '材料.pdf' }])}>加入附件引用</button>
    <span>附件数 {recovery.attachments.length}</span><WorkspaceRecoveryNotice recovery={recovery} /></>;
}
function transport(identity: string, available = true) {
  const t = new MockControlTransport({ routes: { 'agent.continuity.media': { ok: true, items: [{ id: 'media-one', available }] } } });
  Object.defineProperty(t, 'connectionIdentity', { value: identity });
  return t;
}
it('restores drafts and durable references after remount and verifies references', async () => {
  const t = transport('local-project');
  const tree = () => <ControlTransportProvider transport={t}><Harness /></ControlTransportProvider>;
  const first = render(tree());
  fireEvent.change(screen.getByLabelText('草稿'), { target: { value: '保留未发送的要求' } });
  fireEvent.click(screen.getByText('加入附件引用'));
  first.unmount(); render(tree());
  expect(screen.getByLabelText('草稿')).toHaveValue('保留未发送的要求');
  expect(screen.getByText('附件数 1')).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByText('正在核实恢复的附件…')).not.toBeInTheDocument());
  expect(t.requests[0].request.pathId).toBe('agent.continuity.media');
});
it('retains an inaccessible attachment visibly until the user removes its reference', async () => {
  const t = transport('local-project', false);
  const tree = () => <ControlTransportProvider transport={t}><Harness /></ControlTransportProvider>;
  const first = render(tree()); fireEvent.click(screen.getByText('加入附件引用')); first.unmount(); render(tree());
  await screen.findByText(/1 项附件已失效/);
  expect(screen.getByText('附件数 1')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '移除这些附件引用' }));
  expect(screen.getByText('附件数 0')).toBeInTheDocument();
});
it('does not treat a newly imported receipt as an old reference after restoring a text draft', async () => {
  const t = transport('draft-only', false);
  localStorage.setItem(recoveryScope(t, 'session:one'), JSON.stringify({draft:'植被面积分析', attachments:[], savedAtMs:Date.now()}));
  render(<ControlTransportProvider transport={t}><Harness /></ControlTransportProvider>);
  fireEvent.click(screen.getByText('加入附件引用'));
  expect(screen.getByText('附件数 1')).toBeInTheDocument();
  await waitFor(() => expect(screen.queryByText(/正在核实|附件已失效/)).not.toBeInTheDocument());
  expect(t.requests).toHaveLength(0);
  expect(screen.getByLabelText('草稿')).toHaveValue('植被面积分析');
});
it('does not restore a draft into another backend or owner', () => {
  const first = render(<ControlTransportProvider transport={transport('a')}><Harness /></ControlTransportProvider>);
  fireEvent.change(screen.getByLabelText('草稿'), { target: { value: '项目 A 私有草稿' } }); first.unmount();
  render(<ControlTransportProvider transport={transport('b')}><Harness /></ControlTransportProvider>);
  expect(screen.getByLabelText('草稿')).toHaveValue('');
});

it('keeps an unreadable saved draft instead of overwriting it with the mount fallback', async () => {
  const t = transport('corrupt-record');
  const key = recoveryScope(t, 'session:one');
  localStorage.setItem(key, '{incomplete saved draft');
  render(<ControlTransportProvider transport={t}><Harness /></ControlTransportProvider>);
  await screen.findByText(/本机草稿记录无法读取/);
  expect(localStorage.getItem(key)).toBe('{incomplete saved draft');
  fireEvent.change(screen.getByLabelText('草稿'), { target: { value: '新的工作要求' } });
  expect(localStorage.getItem(key + ':unreadable')).toBe('{incomplete saved draft');
  expect(JSON.parse(localStorage.getItem(key)!).draft).toBe('新的工作要求');
});
