import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MotionActivityBoundary, MotionProvider } from '@/design/motion';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { MemoryReferenceDialog, type MemoryReferenceSelection } from './MemoryReferenceDialog';
import { memoryQueryKeys } from './api';

afterEach(() => { cleanup(); localStorage.clear(); delete document.documentElement.dataset.reduceMotion; });

function reference(kind = 'atom', id = 'atom:root') {
  return {
    schemaVersion: 'rag-ime.memory-reference.v1', settingsRevision: 'settings:fixture', runtimeRevision: 1,
    ok: true, kind, referenceId: id,
    item: { id, title: kind === 'atom' ? '原记忆' : '原来源', text: '原正文不随动效变化。', status: 'approved', updatedAtMs: 1 },
    source: { kind: 'agent_memory_evidence', id },
    ref: { kind, id, referenceKind: kind, referenceId: id },
    evidenceRefs: kind === 'atom' ? [{ kind: 'evidence', id: 'evidence:child', referenceKind: 'evidence', referenceId: 'evidence:child', label: '原来源' }] : [],
  };
}

function Controller({ referenceId = 'atom:root', kind = 'atom', label }: Partial<MemoryReferenceSelection>) {
  const [open, setOpen] = useState(false);
  return <><button onClick={() => setOpen(true)}>查看原记忆</button>{open ? <MemoryReferenceDialog kind={kind} label={label} referenceId={referenceId} onOpenChange={setOpen} /> : null}</>;
}
function renderDialog(transport: MockControlTransport, active = true, selection: Partial<MemoryReferenceSelection> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  const wrapper = (enabled: boolean, referenceId = 'atom:root') => <ControlTransportProvider transport={transport}><QueryClientProvider client={client}>
    <MotionProvider><MotionActivityBoundary active={enabled}><Controller {...selection} referenceId={selection.referenceId ?? referenceId} /></MotionActivityBoundary></MotionProvider>
  </QueryClientProvider></ControlTransportProvider>;
  return { ...render(wrapper(active)), wrapper, client };
}

it('returns to the same original source control and reading position after a deeper reference', async () => {
  const user = userEvent.setup();
  const transport = new MockControlTransport({ routes: { 'memory.reference.get': (request: ControlRequest) => reference(String(request.params?.kind), String(request.params?.referenceId)) } });
  renderDialog(transport);
  const opener = screen.getByRole('button', { name: '查看原记忆' });
  await user.click(opener);
  const dialog = await screen.findByRole('dialog', { name: '原记忆' });
  expect(within(dialog).getByRole('heading', { name: '原记忆' })).toHaveFocus();
  const body = dialog.querySelector('.memory-reference-view') as HTMLElement;
  body.scrollTop = 132;
  await user.click(within(dialog).getByRole('button', { name: /原来源/ }));
  const heading = await screen.findByRole('heading', { name: '原来源' });
  expect(heading).toHaveFocus();
  expect(screen.getByRole('navigation', { name: '记忆来源路径' })).toHaveTextContent('记忆');
  await user.click(within(dialog).getByRole('button', { name: '返回 记忆' }));
  const source = await within(dialog).findByRole('button', { name: /原来源/ });
  expect(source).toHaveFocus();
  expect((dialog.querySelector('.memory-reference-view') as HTMLElement).scrollTop).toBe(132);
  expect(dialog).toHaveTextContent('原正文不随动效变化。');
  await user.keyboard('{Escape}');
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  expect(opener).toHaveFocus();
  expect(transport.requests.every(({ request }) => request.pathId === 'memory.reference.get')).toBe(true);
});

it('quietens the existing mounted reference without refetching or losing focus and content', async () => {
  const user = userEvent.setup();
  const transport = new MockControlTransport({ routes: { 'memory.reference.get': reference() } });
  const view = renderDialog(transport);
  await user.click(screen.getByRole('button', { name: '查看原记忆' }));
  const dialog = await screen.findByRole('dialog', { name: '原记忆' });
  const source = within(dialog).getByRole('button', { name: /原来源/ });
  source.focus();
  const body = dialog.querySelector('.memory-reference-view') as HTMLElement;
  body.scrollTop = 80;
  const reads = transport.requests.length;
  view.rerender(view.wrapper(false));
  expect(dialog).toHaveAttribute('data-motion-active', 'false');
  expect(source).toHaveFocus();
  expect(dialog.querySelector('.memory-reference-view')).toBe(body);
  expect(body.scrollTop).toBe(80);
  view.rerender(view.wrapper(true));
  expect(dialog).toHaveAttribute('data-motion-active', 'true');
  expect(source).toHaveFocus();
  expect(body).toHaveTextContent('原正文不随动效变化。');
  expect(transport.requests).toHaveLength(reads);
});

it('keeps pending reads while hidden or reduced and reveals the original receipt when it resolves', async () => {
  const user = userEvent.setup();
  let resolve!: (value: unknown) => void;
  const pending = new Promise(res => { resolve = res; });
  const transport = new MockControlTransport({ routes: { 'memory.reference.get': () => pending } });
  let visibility: DocumentVisibilityState = 'visible';
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
  try {
    const view = renderDialog(transport);
    await user.click(screen.getByRole('button', { name: '查看原记忆' }));
    const dialog = screen.getByRole('dialog');
    expect(await screen.findByRole('status')).toHaveTextContent('正在读取引用详情');
    expect(dialog).toHaveAttribute('data-motion-active', 'true');
    visibility = 'hidden'; fireEvent(document, new Event('visibilitychange'));
    expect(dialog).toHaveAttribute('data-motion-active', 'false');
    expect(screen.getByRole('status')).toHaveTextContent('正在读取引用详情');
    visibility = 'visible'; fireEvent(document, new Event('visibilitychange'));
    localStorage.setItem('rag-ime-control-motion', 'reduce');
    fireEvent(window, new StorageEvent('storage', { key: 'rag-ime-control-motion', newValue: 'reduce', storageArea: localStorage }));
    expect(dialog).toHaveAttribute('data-motion-active', 'false');
    resolve(reference());
    await screen.findByRole('heading', { name: '原记忆' });
    expect(dialog).toHaveTextContent('原正文不随动效变化。');
    expect(transport.requests).toHaveLength(1);
    view.unmount();
  } finally { Reflect.deleteProperty(document, 'visibilityState'); }
});

it('reads an identical title and body once while retaining kind, original status and technical reference', async () => {
  const user = userEvent.setup();
  const data = reference();
  const original = '单条项目事实，仅作为来源展示。';
  data.item.title = original; data.item.text = original;
  const transport = new MockControlTransport({ routes: { 'memory.reference.get': data } });
  renderDialog(transport);
  await user.click(screen.getByRole('button', { name: '查看原记忆' }));
  const dialog = await screen.findByRole('dialog', { name: '已整理记忆' });
  expect(within(dialog).getAllByText(original)).toHaveLength(1);
  expect(dialog).toHaveTextContent('已确认');
  expect(within(dialog).getByRole('navigation', { name: '记忆来源路径' })).toHaveTextContent('记忆');
  await user.click(within(dialog).getByText('高级：引用详情', { selector: 'summary' }));
  expect(within(within(dialog).getByText('引用编号').closest('div')!).getByText('atom:root')).toBeInTheDocument();
  expect(dialog).toHaveTextContent('当前层');
  expect(dialog).toHaveTextContent('agent_memory_evidence');
});

it('keeps a compact long heading fully inspectable through its original details', async () => {
  const user = userEvent.setup();
  const data = reference();
  data.item.title = '演示项目的长期来源记录与每次用户更正依据'.repeat(4);
  data.item.text = '正文与原标题不同，完整标题也应能在窄屏查阅。';
  expect(data.item.title.length).toBeLessThan(120);
  const transport = new MockControlTransport({ routes: { 'memory.reference.get': data } });
  renderDialog(transport);
  await user.click(screen.getByRole('button', { name: '查看原记忆' }));
  const dialog = await screen.findByRole('dialog', { name: data.item.title });
  await user.click(within(dialog).getByText('高级：引用详情', { selector: 'summary' }));
  const fullTitle = within(dialog).getByText('完整标题').closest('div')!;
  expect(fullTitle).toHaveTextContent(data.item.title);
  expect(dialog).toHaveTextContent(data.item.text);
  expect(transport.requests).toHaveLength(1);
});

it('reads a user message once when its original title is a shortened body preview', async () => {
  const user = userEvent.setup();
  const data = { ...reference('evidence', 'evidence:child'), item: {
    ...reference('evidence', 'evidence:child').item, sourceKind: 'user_message',
    text: '用户明确更正普通项目的报告标签，并要求保留原始来源与审批结果。'.repeat(6),
    title: '',
  } };
  data.item.title = data.item.text.slice(0, 160);
  const transport = new MockControlTransport({ routes: { 'memory.reference.get': (request: ControlRequest) =>
    request.params?.referenceId === 'evidence:child' ? data : reference() } });
  renderDialog(transport);
  await user.click(screen.getByRole('button', { name: '查看原记忆' }));
  const rootDialog = await screen.findByRole('dialog', { name: '原记忆' });
  await user.click(within(rootDialog).getByRole('button', { name: /原来源/ }));
  const dialog = await screen.findByRole('dialog', { name: '来源记录' });
  expect(within(dialog).getByRole('heading')).toHaveTextContent('来源记录');
  expect(dialog).toHaveTextContent(data.item.text);
  expect(within(dialog).queryByText(data.item.title, { selector: 'h2' })).toBeNull();
  await user.click(within(dialog).getByText('高级：引用详情', { selector: 'summary' }));
  const originalTitle = within(dialog).getByText('完整标题').closest('div')!;
  expect(originalTitle).toHaveTextContent(data.item.title);
  expect(transport.requests).toHaveLength(2);
});

it.each(['full', 'reduce'])('keeps the retry action stable and prevents a second read while pending (%s motion)', async (preference) => {
  localStorage.setItem('rag-ime-control-motion', preference);
  const user = userEvent.setup();
  let attempts = 0;
  let resolve!: (value: unknown) => void;
  const pending = new Promise(res => { resolve = res; });
  const transport = new MockControlTransport({ routes: { 'memory.reference.get': () => {
    attempts++;
    if (attempts === 1) throw new Error('original read unavailable');
    return pending;
  } } });
  renderDialog(transport);
  await user.click(screen.getByRole('button', { name: '查看原记忆' }));
  const retry = await screen.findByRole('button', { name: '重试读取' });
  await user.click(retry);
  expect(screen.getByRole('button', { name: '重试读取' })).toBe(retry);
  expect(retry).toHaveAttribute('aria-busy', 'true');
  expect(retry).toBeDisabled();
  await user.click(retry);
  expect(attempts).toBe(2);
  resolve(reference());
  await screen.findByRole('heading', { name: '原记忆' });
  expect(screen.getByRole('heading', { name: '原记忆' })).toHaveFocus();
  expect(screen.getByRole('dialog')).toHaveTextContent('原正文不随动效变化。');
  expect(attempts).toBe(2);
});

it('binds retry feedback to its exact source and ignores a late retry after returning or changing the root', async () => {
  const user = userEvent.setup();
  let childReads = 0;
  let resolve!: (value: unknown) => void;
  const pending = new Promise(res => { resolve = res; });
  const transport = new MockControlTransport({ routes: { 'memory.reference.get': (request: ControlRequest) => {
    const id = String(request.params?.referenceId);
    if (id === 'evidence:child') {
      childReads++;
      if (childReads < 3) throw new Error('仅此原来源暂时不可读取');
      return pending;
    }
    const data = reference('atom', id);
    if (id === 'atom:new-root') { data.item.title = '新的原记忆'; data.item.text = '新的原正文。'; }
    return data;
  } } });
  const view = renderDialog(transport);
  await user.click(screen.getByRole('button', { name: '查看原记忆' }));
  const dialog = await screen.findByRole('dialog', { name: '原记忆' });
  await user.click(within(dialog).getByRole('button', { name: /原来源/ }));
  await screen.findByRole('button', { name: '重试读取' });
  expect(dialog).toHaveTextContent('仅此原来源暂时不可读取');
  await user.click(within(dialog).getByRole('button', { name: '返回 记忆' }));
  await screen.findByRole('heading', { name: '原记忆' });
  expect(dialog).not.toHaveTextContent('仅此原来源暂时不可读取');
  expect(within(dialog).getByRole('button', { name: /原来源/ })).toHaveFocus();
  await user.click(within(dialog).getByRole('button', { name: /原来源/ }));
  await user.click(await screen.findByRole('button', { name: '重试读取' }));
  expect(screen.getByRole('button', { name: '重试读取' })).toBeDisabled();
  await user.click(within(dialog).getByRole('button', { name: '返回 记忆' }));
  await screen.findByRole('heading', { name: '原记忆' });
  expect(dialog).not.toHaveTextContent('仅此原来源暂时不可读取');
  view.rerender(view.wrapper(true, 'atom:new-root'));
  const title = await screen.findByRole('heading', { name: '新的原记忆' });
  expect(title).toHaveFocus();
  expect(dialog).toHaveTextContent('新的原正文。');
  await act(async () => { resolve(reference('evidence', 'evidence:child')); });
  await waitFor(() => expect(title).toHaveFocus());
  expect(dialog).not.toHaveTextContent('仅此原来源暂时不可读取');
  expect(dialog).not.toHaveTextContent('原正文不随动效变化。');
  expect(dialog).not.toHaveTextContent('原来源记录');
  expect(childReads).toBe(3);
  expect(transport.requests.every(({ request }) => request.pathId === 'memory.reference.get')).toBe(true);
});

it.each(['success', 'error'] as const)('keeps a short kind heading while a long-labelled original source read is pending and settles as %s', async (outcome) => {
  const user = userEvent.setup();
  const label = '原用户说明普通项目的标签并保留来源，不代表已经读取的正文。'.repeat(6).slice(0, 160);
  let resolve!: (value: unknown) => void;
  let reject!: (error: Error) => void;
  const pending = new Promise((res, rej) => { resolve = res; reject = rej; });
  const transport = new MockControlTransport({ routes: { 'memory.reference.get': () => pending } });
  renderDialog(transport, true, { kind: 'evidence', referenceId: 'evidence:original', label });
  await user.click(screen.getByRole('button', { name: '查看原记忆' }));
  const heading = screen.getByRole('heading', { name: '来源记录' });
  expect(heading).toHaveAttribute('title', label);
  expect(screen.getByRole('status')).toHaveTextContent('正在读取引用详情');
  expect(document.querySelector('.memory-reference-view')).toBeNull();
  if (outcome === 'error') {
    await act(async () => reject(new Error('此原引用确实暂不可读')));
    await screen.findByRole('button', { name: '重试读取' });
    expect(heading).toHaveTextContent('来源记录');
    expect(screen.getByRole('dialog')).toHaveTextContent('此原引用确实暂不可读');
    expect(document.querySelector('.memory-reference-view')).toBeNull();
  } else {
    const data = { ...reference('evidence', 'evidence:original'), item: {
      ...reference('evidence', 'evidence:original').item, title: label, text: `${label}已读原文的其余段落。`, sourceKind: 'user_message',
    } };
    await act(async () => resolve(data));
    const dialog = await screen.findByRole('dialog', { name: '来源记录' });
    expect(within(dialog).getByRole('heading')).toBe(heading);
    expect(dialog).toHaveTextContent(data.item.text);
    await user.click(within(dialog).getByText('高级：引用详情', { selector: 'summary' }));
    expect(within(dialog).getByText('完整标题').closest('div')).toHaveTextContent(label);
    expect(within(dialog).getByRole('navigation', { name: '记忆来源路径' })).toHaveTextContent('来源');
  }
  expect(transport.requests).toHaveLength(1);
  expect(transport.requests[0].request.params).toEqual({ kind: 'evidence', referenceId: 'evidence:original' });
});

it('uses the kind heading for an unresolved cached reference without inventing a body', async () => {
  const user = userEvent.setup();
  const transport = new MockControlTransport();
  const label = '不完整缓存保留原引用的长标签。'.repeat(10);
  const { client } = renderDialog(transport, true, { kind: 'evidence', referenceId: 'evidence:unresolved', label });
  // Deliberately incomplete cache fixture, not a schema-valid API response.
  client.setQueryData(memoryQueryKeys.reference('evidence', 'evidence:unresolved'), {});
  await user.click(screen.getByRole('button', { name: '查看原记忆' }));
  const heading = screen.getByRole('heading', { name: '来源记录' });
  expect(heading).toHaveAttribute('title', label);
  expect(screen.getByRole('dialog')).toHaveTextContent('没有可显示的引用');
  expect(screen.getByRole('button', { name: '重新读取' })).toBeInTheDocument();
  expect(document.querySelector('.memory-reference-view')).toBeNull();
  expect(transport.requests).toHaveLength(0);
});
