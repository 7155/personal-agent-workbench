import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { createPreviewTransport } from '@/app/preview-control-transport';
import type { SessionSummary } from '@/features/agent/types';
import type { ControlTransport, PickedFile } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { PawPrimaryAssistantHome, type PrimaryAssistantHomeDraft } from './PawPrimaryAssistantHome';

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const primary: SessionSummary = {
  id: 'directory-primary', title: '我的助手', status: 'idle', mode: 'assistant',
  roleId: '', roleVersion: '', roleBookRevisionId: '', workspaceRoots: [], updatedAtMs: 1,
  executionMode: 'read_only', metadata: { primaryAssistant: true },
};
const initialForm: PrimaryAssistantHomeDraft = {
  draft: '检查项目入口，并保留这些要求', execute: true, workspace: '/work/existing',
  contextWorkspace: '/work/existing', acceptance: '测试通过\n说明修改', scopeConfirmed: true,
};
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}
function directory(path: string): PickedFile {
  return { id: `workspace:${path}`, name: path.split('/').at(-1)!, path, mimeType: 'inode/directory', byteSize: 0 };
}
function fixtureTransport() {
  return new MockControlTransport({ routes: {
    'agent.primary.ensure': { ok: true, session: primary, tasks: [] },
    'agent.primary.tasks.create': { ok: true, session: {
      ...primary, id: 'directory-task', executionMode: 'workspace_managed', metadata: { primaryTask: true },
    } },
  } });
}
function setup(transport: ControlTransport, form = initialForm) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onOpen = vi.fn();
  const tree = (owner: ControlTransport) => <QueryClientProvider client={client}>
    <ControlTransportProvider transport={owner}>
      <PawPrimaryAssistantHome initialForm={form} onOpen={onOpen} onAdvanced={vi.fn()} />
    </ControlTransportProvider>
  </QueryClientProvider>;
  const view = render(tree(transport));
  return { onOpen, ...view, replaceTransport: (owner: ControlTransport) => view.rerender(tree(owner)) };
}
async function ready() {
  await waitFor(() => expect(screen.getByRole('button', { name: /进入对话/ })).toBeEnabled());
}
function expectExecutionDraft(path = initialForm.workspace) {
  expect(screen.getByRole('textbox', { name: '和我的助手聊聊' })).toHaveValue(initialForm.draft);
  expect(screen.getByRole('textbox', { name: '本次工作目录' })).toHaveValue(path);
  expect(screen.getByRole('textbox', { name: '完成标准' })).toHaveValue(initialForm.acceptance);
}
function expectNoDispatch(transport: MockControlTransport) {
  expect(transport.requests.some(({ request }) => [
    'agent.primary.tasks.create', 'agent.session.prompt', 'agent.sessions.create', 'agent.rooms.create',
  ].includes(request.pathId))).toBe(false);
}

describe('primary assistant directory picker', () => {
  it('deduplicates a pending chooser and blocks both click and Enter submission', async () => {
    const transport = fixtureTransport();
    const pending = deferred<PickedFile[]>();
    const pick = vi.spyOn(transport, 'pickFiles').mockReturnValue(pending.promise);
    const { onOpen } = setup(transport);
    await ready();
    const picker = screen.getByRole('button', { name: '选择目录' });
    const submit = screen.getByRole('button', { name: '授权并开始任务' });
    expect(submit).toBeEnabled();
    fireEvent.click(picker);
    fireEvent.click(picker);
    expect(pick).toHaveBeenCalledTimes(1);
    expect(pick).toHaveBeenCalledWith(expect.objectContaining({
      purpose: 'workspace-root', selection: 'directory', multiple: false, maxFiles: 1,
    }));
    expect(picker).toBeDisabled();
    expect(submit).toBeDisabled();
    fireEvent.click(submit);
    fireEvent.keyDown(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { key: 'Enter' });
    expectNoDispatch(transport);
    expect(onOpen).not.toHaveBeenCalled();
    await act(async () => pending.resolve([]));
    expect(picker).toBeEnabled();
    expectExecutionDraft();
  });

  it('announces cancellation and keeps the previous path, goal, and acceptance criteria', async () => {
    const transport = fixtureTransport();
    vi.spyOn(transport, 'pickFiles').mockResolvedValue([]);
    const { onOpen } = setup(transport);
    await ready();
    fireEvent.click(screen.getByRole('button', { name: '选择目录' }));
    expect(await screen.findByText(/取消/)).toBeVisible();
    expectExecutionDraft();
    expect(screen.getByRole('button', { name: '选择目录' })).toBeEnabled();
    expectNoDispatch(transport);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('shows picker failure without losing the draft and allows a fresh chooser attempt', async () => {
    const transport = fixtureTransport();
    const pick = vi.spyOn(transport, 'pickFiles').mockRejectedValueOnce(new Error('系统目录选择器暂不可用'))
      .mockResolvedValueOnce([directory('/work/recovered')]);
    const { onOpen } = setup(transport);
    await ready();
    fireEvent.click(screen.getByRole('button', { name: '选择目录' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('系统目录选择器暂不可用');
    expectExecutionDraft();
    fireEvent.click(screen.getByRole('button', { name: '选择目录' }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: '本次工作目录' })).toHaveValue('/work/recovered'));
    expect(pick).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expectExecutionDraft('/work/recovered');
    expect(screen.getByRole('checkbox')).not.toBeChecked();
    expectNoDispatch(transport);
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('discards an old transport result and prevents its finally from releasing the new chooser', async () => {
    const original = fixtureTransport();
    const next = fixtureTransport();
    const oldResult = deferred<PickedFile[]>();
    const newResult = deferred<PickedFile[]>();
    vi.spyOn(original, 'pickFiles').mockReturnValue(oldResult.promise);
    const pickNext = vi.spyOn(next, 'pickFiles').mockReturnValue(newResult.promise);
    const { onOpen, replaceTransport } = setup(original);
    await ready();
    fireEvent.click(screen.getByRole('button', { name: '选择目录' }));
    replaceTransport(next);
    await ready();
    const nextPicker = screen.getByRole('button', { name: '选择目录' });
    expect(nextPicker).toBeEnabled();
    fireEvent.click(nextPicker);
    await act(async () => oldResult.resolve([directory('/work/stale-owner')]));
    expectExecutionDraft();
    expect(nextPicker).toBeDisabled();
    fireEvent.click(nextPicker);
    fireEvent.keyDown(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { key: 'Enter' });
    expect(pickNext).toHaveBeenCalledTimes(1);
    expectNoDispatch(original);
    expectNoDispatch(next);
    expect(onOpen).not.toHaveBeenCalled();
    await act(async () => newResult.resolve([directory('/work/current-owner')]));
    expectExecutionDraft('/work/current-owner');
    expect(nextPicker).toBeEnabled();
    expect(screen.getByRole('checkbox')).not.toBeChecked();
  });

  it('uses the native host through the actual preview factory without granting execution consent', async () => {
    const pickWorkspaceDirectory = vi.fn(async () => ({ name: 'native-project', path: '/work/native-project' }));
    vi.stubGlobal('pawBrowserHost', { kind: 'electron-webview', partition: 'persist:paw-browser', pickWorkspaceDirectory });
    const transport = createPreviewTransport();
    const { onOpen } = setup(transport);
    await ready();
    fireEvent.click(screen.getByRole('button', { name: '选择目录' }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: '本次工作目录' })).toHaveValue('/work/native-project'));
    expect(pickWorkspaceDirectory).toHaveBeenCalledTimes(1);
    expectExecutionDraft('/work/native-project');
    expect(screen.getByRole('checkbox')).not.toBeChecked();
    expect(screen.getByRole('button', { name: '授权并开始任务' })).toBeDisabled();
    expectNoDispatch(transport);
    expect(onOpen).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '授权并开始任务' }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledTimes(1));
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.primary.tasks.create')?.request.body).toMatchObject({
      objective: initialForm.draft, acceptanceCriteria: ['测试通过', '说明修改'],
      workspaceRoots: ['/work/native-project'], workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE',
    });
  });

  it('explains browser preview limitations and recovers through a manually entered path', async () => {
    vi.stubGlobal('pawBrowserHost', undefined);
    const transport = createPreviewTransport();
    const { onOpen } = setup(transport);
    await ready();
    fireEvent.click(screen.getByRole('button', { name: '选择目录' }));
    const notice = await screen.findByRole('alert');
    expect(notice).toHaveTextContent(/浏览器|网页/);
    expect(notice).toHaveTextContent(/不能|不支持|无法/);
    expect(notice).toHaveTextContent(/手动|填写|粘贴/);
    expectExecutionDraft();
    expectNoDispatch(transport);
    fireEvent.change(screen.getByRole('textbox', { name: '本次工作目录' }), { target: { value: '/work/manual-project' } });
    expect(screen.getByRole('checkbox')).not.toBeChecked();
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: '授权并开始任务' }));
    await waitFor(() => expect(onOpen).toHaveBeenCalledTimes(1));
    expect(transport.requests.find(({ request }) => request.pathId === 'agent.primary.tasks.create')?.request.body).toMatchObject({
      objective: initialForm.draft, workspaceRoots: ['/work/manual-project'],
    });
  });

  it('waits for the discussion picker, then ensures the selected roots without losing text or writing', async () => {
    const pending = deferred<{ name: string; path: string } | null>();
    const pickWorkspaceDirectory = vi.fn(() => pending.promise);
    vi.stubGlobal('pawBrowserHost', { kind: 'electron-webview', partition: 'persist:paw-browser', pickWorkspaceDirectory });
    const transport = createPreviewTransport();
    const { onOpen } = setup(transport, { ...initialForm, execute: false });
    await ready();
    const picker = screen.getByRole('button', { name: '选择项目' });
    fireEvent.click(picker);
    fireEvent.click(picker);
    expect(pickWorkspaceDirectory).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: '发送给我的助手' })).toBeDisabled();
    fireEvent.keyDown(screen.getByRole('textbox', { name: '和我的助手聊聊' }), { key: 'Enter' });
    expect(onOpen).not.toHaveBeenCalled();
    await act(async () => pending.resolve({ name: 'discussion-project', path: '/work/discussion-project' }));
    await waitFor(() => expect(screen.getByRole('combobox', { name: '讨论项目' })).toHaveValue('/work/discussion-project'));
    await ready();
    const ensures = transport.requests.filter(({ request }) => request.pathId === 'agent.primary.ensure');
    expect(ensures.map(({ request }) => request.body)).toEqual([
      { workspaceRoots: ['/work/existing'] }, { workspaceRoots: ['/work/discussion-project'] },
    ]);
    expect(screen.getByRole('textbox', { name: '和我的助手聊聊' })).toHaveValue(initialForm.draft);
    expectNoDispatch(transport);
    fireEvent.click(screen.getByRole('button', { name: '发送给我的助手' }));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ workspaceRoots: ['/work/discussion-project'], executionMode: 'read_only' }),
      expect.objectContaining({ message: initialForm.draft }));
  });
});
