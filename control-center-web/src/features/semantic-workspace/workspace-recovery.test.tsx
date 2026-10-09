import { Activity, StrictMode, type ReactNode } from 'react';
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { appendWorkspaceRecoveryDraft, recoveryScope, useWorkspaceRecovery, WorkspaceRecoveryNotice } from './workspace-recovery';

beforeEach(() => localStorage.clear());
afterEach(cleanup);
function Harness({ owner = 'session:one', label = '草稿', initialDraft = '' }: { owner?: string; label?: string; initialDraft?: string }) {
  const recovery = useWorkspaceRecovery<{ id: string; sha256: string; name: string }>(owner, initialDraft);
  return <><input aria-label={label} value={recovery.draft} onChange={e => recovery.setDraft(e.target.value)} />
    <button onClick={() => recovery.setAttachments([{ id: 'media-one', sha256: 'hash', name: '材料.pdf' }])}>加入附件引用</button>
    <span>附件数 {recovery.attachments.length}</span><WorkspaceRecoveryNotice recovery={recovery} /></>;
}
function transport(identity: string, available = true) {
  const t = new MockControlTransport({ routes: { 'agent.continuity.media': { ok: true, items: [{ id: 'media-one', available }] } } });
  Object.defineProperty(t, 'connectionIdentity', { value: identity });
  return t;
}

it.each([false, true])('rejoins the latest draft owner after resubscription (newer view closed: %s)', async closed => {
  const t = transport(`resubscribe-${closed}`);
  const tree = (hidden: boolean, second: boolean) => <ControlTransportProvider transport={t}>
    <Activity mode={hidden ? 'hidden' : 'visible'}><Harness label="A 草稿" initialDraft="入口种子" /></Activity>
    {second ? <Harness label="B 草稿" /> : null}
  </ControlTransportProvider>;
  const view = render(tree(false, false));
  expect(screen.getByRole('textbox', { name: 'A 草稿' })).toHaveValue('入口种子');
  fireEvent.change(screen.getByRole('textbox', { name: 'A 草稿' }), { target: { value: 'A 旧草稿' } });
  view.rerender(tree(true, false));
  view.rerender(tree(true, true));
  fireEvent.change(screen.getByRole('textbox', { name: 'B 草稿' }), { target: { value: 'B 新草稿' } });
  fireEvent.click(screen.getByRole('button', { name: '加入附件引用' }));
  if (closed) view.rerender(tree(true, false));
  view.rerender(tree(false, !closed));
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'A 草稿' })).toHaveValue('B 新草稿'));
  expect(JSON.parse(localStorage.getItem(recoveryScope(t, 'session:one'))!)).toMatchObject({
    draft: 'B 新草稿', attachments: [{ id: 'media-one', sha256: 'hash', name: '材料.pdf' }],
  });
  fireEvent.change(screen.getByRole('textbox', { name: 'A 草稿' }), { target: { value: 'A 接着编辑' } });
  if (!closed) {
    expect(screen.getByRole('textbox', { name: 'B 草稿' })).toHaveValue('A 接着编辑');
    view.rerender(tree(true, true));
    fireEvent.change(screen.getByRole('textbox', { name: 'B 草稿' }), { target: { value: 'B 再次编辑' } });
    view.rerender(tree(false, true));
    expect(screen.getByRole('textbox', { name: 'A 草稿' })).toHaveValue('B 再次编辑');
    fireEvent.change(screen.getByRole('textbox', { name: 'A 草稿' }), { target: { value: '共同的最终草稿' } });
    expect(screen.getByRole('textbox', { name: 'B 草稿' })).toHaveValue('共同的最终草稿');
    fireEvent.change(screen.getByRole('textbox', { name: 'B 草稿' }), { target: { value: 'B 共同编辑' } });
    expect(screen.getByRole('textbox', { name: 'A 草稿' })).toHaveValue('B 共同编辑');
    view.unmount();
    render(<ControlTransportProvider transport={t}><Harness label="重开草稿" initialDraft="新的显式入口" /></ControlTransportProvider>);
    expect(screen.getByRole('textbox', { name: '重开草稿' })).toHaveValue('新的显式入口');
  } else {
    await waitFor(() => expect(t.requests.some(({ request }) => request.pathId === 'agent.continuity.media')).toBe(true));
  }
});

it('appends a carried draft to the current persisted input once, preserving references and StrictMode edits', () => {
  const t = transport('append-initial-draft');
  const key = recoveryScope(t, 'session:one');
  localStorage.setItem(key, JSON.stringify({ draft: '原草稿 A', attachments: [{ id: 'old-media' }], savedAtMs: 1 }));
  const view = renderHook(() => useWorkspaceRecovery<{ id: string }>('session:one', '带入草稿 B', [], 'append'), { wrapper: recoveryWrapper(t) });
  expect(view.result.current.draft).toBe('原草稿 A\n\n带入草稿 B');
  expect(view.result.current.attachments).toEqual([{ id: 'old-media' }]);
  expect(view.result.current.warning).toContain('已保留原草稿');
  expect(JSON.parse(localStorage.getItem(key)!)).toMatchObject({ draft: '原草稿 A\n\n带入草稿 B' });
  act(() => view.result.current.setDraft('编辑后的 A 与 B'));
  view.rerender();
  expect(view.result.current.draft).toBe('编辑后的 A 与 B');
  view.unmount();
  const same = renderHook(() => useWorkspaceRecovery('session:one', '编辑后的 A 与 B', [], 'append'), { wrapper: recoveryWrapper(t) });
  expect(same.result.current.draft).toBe('编辑后的 A 与 B');
});

it('resubscribes anonymous views without sharing their independent drafts', () => {
  const t = transport('');
  const tree = (hidden: boolean, second: boolean) => <ControlTransportProvider transport={t}>
    <Activity mode={hidden ? 'hidden' : 'visible'}><Harness label="A 匿名草稿" /></Activity>
    {second ? <Harness label="B 匿名草稿" /> : null}
  </ControlTransportProvider>;
  const view = render(tree(false, false));
  fireEvent.change(screen.getByRole('textbox', { name: 'A 匿名草稿' }), { target: { value: 'A 私有草稿' } });
  view.rerender(tree(true, true));
  fireEvent.change(screen.getByRole('textbox', { name: 'B 匿名草稿' }), { target: { value: 'B 私有草稿' } });
  view.rerender(tree(false, true));
  expect(screen.getByRole('textbox', { name: 'A 匿名草稿' })).toHaveValue('A 私有草稿');
  expect(screen.getByRole('textbox', { name: 'B 匿名草稿' })).toHaveValue('B 私有草稿');
  expect(localStorage.length).toBe(0);
});

it('retains unsaved input across resubscription when storage writes fail', () => {
  const t = transport('failed-storage-resubscribe');
  const key = recoveryScope(t, 'session:one');
  localStorage.setItem(key, JSON.stringify({ draft: '已保存的旧草稿', attachments: [], savedAtMs: 1 }));
  const writes = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage unavailable'); });
  const tree = (hidden: boolean) => <StrictMode><ControlTransportProvider transport={t}>
    <Activity mode={hidden ? 'hidden' : 'visible'}><Harness initialDraft="指定的首次草稿" /></Activity>
  </ControlTransportProvider></StrictMode>;
  try {
    const view = render(tree(false));
    expect(screen.getByRole('textbox', { name: '草稿' })).toHaveValue('指定的首次草稿');
    fireEvent.change(screen.getByRole('textbox', { name: '草稿' }), { target: { value: '尚未保存的新输入' } });
    view.rerender(tree(true));
    view.rerender(tree(false));
    expect(screen.getByRole('textbox', { name: '草稿' })).toHaveValue('尚未保存的新输入');
    expect(screen.getByRole('status')).toHaveTextContent('草稿暂时无法保存');
    expect(JSON.parse(localStorage.getItem(key)!).draft).toBe('已保存的旧草稿');
  } finally { writes.mockRestore(); }
});

it('keeps a fresh attachment preview through resubscription without re-verifying its receipt', () => {
  const t = transport('live-preview-resubscribe', false);
  const file = new File(['image bytes'], 'photo.png', { type: 'image/png' });
  function PreviewProbe() {
    const recovery = useWorkspaceRecovery<{ id: string; previewFile?: File }>('session:one');
    return <><button onClick={() => recovery.setAttachments([{ id: 'media-one', previewFile: file }])}>添加新图片</button>
      <output aria-label="图片来源">{recovery.attachments[0]?.previewFile === file ? '本次导入' : '无原始图片'}</output></>;
  }
  const tree = (hidden: boolean) => <StrictMode><ControlTransportProvider transport={t}>
    <Activity mode={hidden ? 'hidden' : 'visible'}><PreviewProbe /></Activity>
  </ControlTransportProvider></StrictMode>;
  const view = render(tree(false));
  fireEvent.click(screen.getByRole('button', { name: '添加新图片' }));
  view.rerender(tree(true));
  view.rerender(tree(false));
  expect(screen.getByLabelText('图片来源')).toHaveTextContent('本次导入');
  expect(t.requests).toHaveLength(0);
  expect(JSON.parse(localStorage.getItem(recoveryScope(t, 'session:one'))!).attachments).toEqual([{ id: 'media-one' }]);
});

it('retains edits after adopting newer disk input even when subsequent saves fail', () => {
  const t = transport('adopt-then-storage-fails');
  const tree = (hidden: boolean, second: boolean) => <ControlTransportProvider transport={t}>
    <Activity mode={hidden ? 'hidden' : 'visible'}><Harness label="A 草稿" /></Activity>
    {second ? <Harness label="B 草稿" /> : null}
  </ControlTransportProvider>;
  const view = render(tree(false, false));
  fireEvent.change(screen.getByRole('textbox', { name: 'A 草稿' }), { target: { value: 'A 原来的输入' } });
  view.rerender(tree(true, false));
  view.rerender(tree(true, true));
  fireEvent.change(screen.getByRole('textbox', { name: 'B 草稿' }), { target: { value: 'B 保存的输入' } });
  view.rerender(tree(true, false));
  const writes = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('storage unavailable'); });
  try {
    view.rerender(tree(false, false));
    expect(screen.getByRole('textbox', { name: 'A 草稿' })).toHaveValue('B 保存的输入');
    fireEvent.change(screen.getByRole('textbox', { name: 'A 草稿' }), { target: { value: '恢复后尚未保存的编辑' } });
    view.rerender(tree(true, false));
    view.rerender(tree(false, false));
    expect(screen.getByRole('textbox', { name: 'A 草稿' })).toHaveValue('恢复后尚未保存的编辑');
    expect(JSON.parse(localStorage.getItem(recoveryScope(t, 'session:one'))!).draft).toBe('B 保存的输入');
  } finally { writes.mockRestore(); }
});
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

it('hands an explicitly created fork draft to the existing owner without replacing mounted input or attachments', async () => {
  const t=transport('explicit-fork-draft');
  render(<ControlTransportProvider transport={t}><Harness owner="session:fork"/></ControlTransportProvider>);
  fireEvent.change(screen.getByRole('textbox',{name:'草稿'}),{target:{value:'保留原草稿'}});
  fireEvent.click(screen.getByRole('button',{name:'加入附件引用'}));
  act(()=>expect(appendWorkspaceRecoveryDraft(t,'session:fork','分支选中文本')).toBe(true));
  // The fixture uses a single-line input, which strips line breaks; stored
  // draft below retains the multiline editor's exact appended value.
  expect(screen.getByRole('textbox',{name:'草稿'})).toHaveValue('保留原草稿分支选中文本');
  expect(JSON.parse(localStorage.getItem(recoveryScope(t,'session:fork'))!)).toMatchObject({draft:'保留原草稿\n\n分支选中文本',attachments:[{id:'media-one'}]});
  expect(appendWorkspaceRecoveryDraft(t,'session:unmounted','尚未打开分支的文本')).toBe(true);
  expect(JSON.parse(localStorage.getItem(recoveryScope(t,'session:unmounted'))!)).toMatchObject({draft:'尚未打开分支的文本'});
});
