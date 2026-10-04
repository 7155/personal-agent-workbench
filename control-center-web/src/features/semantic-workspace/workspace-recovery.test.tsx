import { StrictMode, type ReactNode } from 'react';
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
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

function recoveryWrapper(t: ReturnType<typeof transport>) {
  return ({ children }: { children: ReactNode }) => <StrictMode><ControlTransportProvider transport={t}>{children}</ControlTransportProvider></StrictMode>;
}

it('ignores ordinary setters from a disposed view after the same owner reopens', () => {
  const t = transport('disposed-setters');
  const first = renderHook(() => useWorkspaceRecovery<{ id: string }>('session:one'), { wrapper: recoveryWrapper(t) });
  const { setDraft, setAttachments } = first.result.current;
  first.unmount();
  const reopened = renderHook(() => useWorkspaceRecovery<{ id: string }>('session:one'), { wrapper: recoveryWrapper(t) });
  act(() => { reopened.result.current.setDraft('新的要求'); reopened.result.current.setAttachments([{ id: 'new-media' }]); });
  act(() => { setDraft(''); setAttachments([]); });
  expect(reopened.result.current.draft).toBe('新的要求');
  expect(reopened.result.current.attachments).toEqual([{ id: 'new-media' }]);
});

it('recovers closed input into a reopened owner and verifies restored attachment references', async () => {
  const t = transport('late-attachment');
  const first = renderHook(() => useWorkspaceRecovery<{ id: string; name: string }>('session:one'), { wrapper: recoveryWrapper(t) });
  const recover = first.result.current.recoverInput;
  first.unmount();
  const reopenedTransport = transport('late-attachment', false);
  const reopened = renderHook(() => useWorkspaceRecovery<{ id: string; name: string }>('session:one'), { wrapper: recoveryWrapper(reopenedTransport) });
  act(() => recover(current => ({
    draft: current.draft || '被拒绝的输入', attachments: current.attachments.length ? current.attachments : [{ id: 'media-one', name: '材料.pdf' }],
  })));
  expect(reopened.result.current.draft).toBe('被拒绝的输入');
  expect(reopened.result.current.attachments).toEqual([{ id: 'media-one', name: '材料.pdf' }]);
  await waitFor(() => expect(reopened.result.current.issues).toEqual(['media-one']));
  expect(reopenedTransport.requests[0].request).toMatchObject({ pathId: 'agent.continuity.media', body: { spaceKey: 'session:one', attachments: [{ id: 'media-one', sha256: '' }] } });
});

it.each([false, true])('preserves newer draft and attachments when an older input recovers (newer owner closed: %s)', closed => {
  const t = transport(`newer-input-${closed}`);
  const first = renderHook(() => useWorkspaceRecovery<{ id: string }>('session:one'), { wrapper: recoveryWrapper(t) });
  const recover = first.result.current.recoverInput;
  first.unmount();
  const next = renderHook(() => useWorkspaceRecovery<{ id: string }>('session:one'), { wrapper: recoveryWrapper(transport(`newer-input-${closed}`)) });
  act(() => { next.result.current.setDraft('较新的要求'); next.result.current.setAttachments([{ id: 'new-media' }]); });
  if (closed) next.unmount();
  act(() => recover(current => ({
    draft: current.draft.trim() ? current.draft : '旧的请求', attachments: current.attachments.length ? current.attachments : [{ id: 'old-media' }],
  })));
  expect(JSON.parse(localStorage.getItem(recoveryScope(t, 'session:one'))!)).toMatchObject({ draft: '较新的要求', attachments: [{ id: 'new-media' }] });
  if (!closed) {
    expect(next.result.current.draft).toBe('较新的要求');
    expect(next.result.current.attachments).toEqual([{ id: 'new-media' }]);
    expect(next.result.current.issues).toEqual([]);
  }
});

it('keeps late recovery with its original connection and Session after the hook changes owners', () => {
  const t = transport('original-connection');
  const first = renderHook(({ owner }) => useWorkspaceRecovery<{ id: string }>(owner), {
    wrapper: recoveryWrapper(t), initialProps: { owner: 'session:one' },
  });
  const recover = first.result.current.recoverInput;
  first.rerender({ owner: 'session:two' });
  act(() => first.result.current.setDraft('另一个 Session 的要求'));
  const other = renderHook(() => useWorkspaceRecovery<{ id: string }>('session:one'), { wrapper: recoveryWrapper(transport('other-connection')) });
  act(() => recover(() => ({ draft: '原 Session 的要求', attachments: [{ id: 'old-media' }] })));
  expect(first.result.current.draft).toBe('另一个 Session 的要求');
  expect(other.result.current.draft).toBe('');
  expect(JSON.parse(localStorage.getItem(recoveryScope(t, 'session:one'))!)).toMatchObject({ draft: '原 Session 的要求', attachments: [{ id: 'old-media' }] });
});

it('keeps explicit initial input and anonymous owners isolated through StrictMode replay', () => {
  const t = transport('initial-input');
  localStorage.setItem(recoveryScope(t, 'session:one'), JSON.stringify({ draft: '旧草稿', attachments: [{ id: 'old-media' }], savedAtMs: 1 }));
  const first = renderHook(() => useWorkspaceRecovery('session:one', '指定的草稿', [{ id: 'new-media' }]), { wrapper: recoveryWrapper(t) });
  expect(first.result.current.draft).toBe('指定的草稿');
  expect(first.result.current.attachments).toEqual([{ id: 'new-media' }]);
  act(() => { first.result.current.setDraft(''); first.result.current.setAttachments([]); });
  first.rerender();
  expect(first.result.current.draft).toBe('');
  expect(first.result.current.attachments).toEqual([]);
  const anonymous = transport('');
  const a = renderHook(() => useWorkspaceRecovery('session:one'), { wrapper: recoveryWrapper(anonymous) });
  const b = renderHook(() => useWorkspaceRecovery('session:one'), { wrapper: recoveryWrapper(anonymous) });
  act(() => a.result.current.setDraft('匿名草稿'));
  expect(b.result.current.draft).toBe('');
});
