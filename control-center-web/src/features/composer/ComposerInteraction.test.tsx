import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TooltipProvider } from '@/components/primitives';
import { AgentComposer } from '@/features/agent/composer/AgentComposer';
import { previewSessions } from '@/features/agent/preview-data';
import { RoomComposer } from '@/features/rooms/composer/RoomComposer';
import type { ComposerFileImporter } from './pasted-text';

afterEach(cleanup);
const text = '完整需求：保留换行与中文。\r\n'.repeat(600);
const participant = { id: 'earth', sessionId: 'session-earth', roleId: 'worker', roleVersion: '1', displayName: 'Earth', ordinal: 0, status: 'active' };

function Harness({ surface, importer, inputOwnerId, busy = false, onSend = vi.fn(), onPick = vi.fn() }: {
  surface: 'Session' | 'Room'; importer: ComposerFileImporter; inputOwnerId?: string; busy?: boolean; onSend?: (...args: string[]) => void; onPick?: () => void;
}) {
  const [draft, setDraft] = useState('请核对附件');
  const common = { draft, onDraftChange: setDraft, sending: false, onPasteImages: importer, onPickAttachments: onPick, onSend, onAttachmentsChange: vi.fn() };
  return <TooltipProvider>{surface === 'Room'
    ? <RoomComposer {...common} room={{ id: 'room-a', status: 'active', participants: [participant] }} attachments={[]} personas={[]} taskBusyState={busy ? 'running' : undefined} onPasteFromClipboard={vi.fn()} />
    : <AgentComposer {...common} inputOwnerId={inputOwnerId} attachments={[]} session={previewSessions[0]} minimal busy={busy} commands={[]} tools={[]} toolCatalogStatus="ready" onToolSelect={vi.fn()} onProductCommand={vi.fn()} onStop={vi.fn()} onPermissionChange={vi.fn()} onWorkspaceRootsChange={vi.fn()} onModelChange={vi.fn()} />}</TooltipProvider>;
}

describe.each(['Session', 'Room'] as const)('%s shared composer', (surface) => {
  it('converts a long paste into one intact UTF-8 file without replacing the draft', async () => {
    const importer = vi.fn().mockResolvedValue(true);
    render(<Harness surface={surface} importer={importer} />);
    const editor = screen.getByRole('textbox');
    expect(fireEvent.paste(editor, { clipboardData: { files: [], items: [], getData: () => text } })).toBe(false);
    await waitFor(() => expect(screen.queryByLabelText('长文本附件')).not.toBeInTheDocument());
    expect(importer).toHaveBeenCalledTimes(1);
    const file = importer.mock.calls[0]![0][0] as File;
    expect(file.name).toMatch(/\.txt$/u);
    expect(file.type).toBe('text/plain');
    const reader = new FileReader();
    const content = new Promise((resolve) => { reader.onload = () => resolve(reader.result); });
    reader.readAsText(file);
    expect(await content).toBe(text);
    expect(editor).toHaveValue('请核对附件');
  });

  it('retains failed text, prevents a partial send and retries the original file', async () => {
    const importer = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const onSend = vi.fn();
    render(<Harness surface={surface} importer={importer} onSend={onSend} />);
    const editor = screen.getByRole('textbox');
    fireEvent.paste(editor, { clipboardData: { files: [], items: [], getData: () => text } });
    await screen.findByText('文本附件未导入，内容已保留。');
    fireEvent.keyDown(editor, { key: 'Enter' });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('查看保留的原文'));
    expect(screen.getByRole('textbox', { name: '未导入的长文本' })).toHaveValue(text.replace(/\r\n/gu, '\n'));
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await waitFor(() => expect(screen.queryByLabelText('长文本附件')).not.toBeInTheDocument());
    expect(importer.mock.calls[0]![0][0]).toBe(importer.mock.calls[1]![0][0]);
    expect(editor).toHaveValue('请核对附件');
  });

  it('provides one add trigger, keyboard dismissal and an independent editor expansion', async () => {
    const user = userEvent.setup(); const onPick = vi.fn();
    const { container } = render(<Harness surface={surface} importer={vi.fn()} onPick={onPick} />);
    const controls = container.querySelector('.agent-composer__controls') as HTMLElement;
    expect(within(controls).getAllByRole('button')).toHaveLength(1);
    const add = screen.getByRole('button', { name: '添加内容' });
    await user.click(add);
    await user.click(screen.getByRole('menuitem', { name: /选择附件/ }));
    expect(onPick).toHaveBeenCalledTimes(1);
    await user.click(add); await user.keyboard('{Escape}');
    expect(add).toHaveFocus();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '展开长文本编辑' }));
    expect(screen.getByRole('textbox')).toHaveFocus();
    expect(screen.getByRole('button', { name: '收起长文本编辑' })).toHaveAttribute('aria-expanded', 'true');
    await user.keyboard('{Escape}');
    expect(screen.getByRole('button', { name: '展开长文本编辑' })).toHaveAttribute('aria-expanded', 'false');
  });
});

function deferredImport() {
  let resolve!: (value: boolean) => void; let reject!: (reason: Error) => void;
  const promise = new Promise<boolean>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function pasteLongText(title: string) {
  fireEvent.paste(screen.getByRole('textbox', { name: '消息' }), {
    clipboardData: { files: [], items: [], getData: () => `${title}\n${'原文保持不变。'.repeat(600)}` },
  });
}

describe('Session long-text input owner', () => {
  it.each(['success', 'failure', 'refused'] as const)('lets a new connection import independently and ignores old %s/finally', async outcome => {
    const old = deferredImport(); const current = deferredImport();
    const oldImporter = vi.fn(() => old.promise); const currentImporter = vi.fn(() => current.promise); const onSend = vi.fn();
    const view = render(<Harness surface="Session" inputOwnerId="transport-a:same-session" importer={oldImporter} onSend={onSend} />);
    pasteLongText('旧连接文本'); expect(oldImporter).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText('长文本附件')).toHaveTextContent('旧连接文本');
    // session.id and the mounted Composer remain unchanged; only the actual
    // transport+Session owner changes, as in Workspace reconnection.
    view.rerender(<Harness surface="Session" inputOwnerId="transport-b:same-session" importer={currentImporter} onSend={onSend} />);
    expect(screen.queryByLabelText('长文本附件')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '发送' })).toBeEnabled();
    pasteLongText('新连接文本'); expect(currentImporter).toHaveBeenCalledTimes(1);
    await act(async () => {
      if (outcome === 'failure') old.reject(new Error('old import failed'));
      else old.resolve(outcome === 'success');
    });
    await waitFor(() => expect(screen.getByLabelText('长文本附件')).toHaveTextContent('新连接文本'));
    expect(screen.getByLabelText('长文本附件')).not.toHaveTextContent('旧连接文本');
    fireEvent.keyDown(screen.getByRole('textbox', { name: '消息' }), { key: 'Enter' });
    expect(onSend).not.toHaveBeenCalled();
    // An obsolete finally must not release the new owner's import lock.
    pasteLongText('重复尝试'); await screen.findByText('文本附件未导入，内容已保留。');
    expect(currentImporter).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: /移除未导入的 重复尝试/ }));
    await act(async () => current.resolve(true));
    await waitFor(() => expect(screen.queryByLabelText('长文本附件')).not.toBeInTheDocument());
    expect(screen.getByRole('textbox', { name: '消息' })).toHaveValue('请核对附件');
    fireEvent.keyDown(screen.getByRole('textbox', { name: '消息' }), { key: 'Enter' });
    expect(onSend).toHaveBeenCalledExactlyOnceWith('prompt', '请核对附件');
  });

  it('does not reuse an earlier import lifetime after navigating away and back to the same owner', async () => {
    const old = deferredImport(); const current = deferredImport();
    const oldImporter = vi.fn(() => old.promise); const currentImporter = vi.fn(() => current.promise);
    const view = render(<Harness surface="Session" inputOwnerId="owner-a" importer={oldImporter} />);
    pasteLongText('早先的文本');
    view.rerender(<Harness surface="Session" inputOwnerId="owner-b" importer={currentImporter} />);
    view.rerender(<Harness surface="Session" inputOwnerId="owner-a" importer={currentImporter} />);
    pasteLongText('返回后文本'); expect(currentImporter).toHaveBeenCalledTimes(1);
    await act(async () => old.resolve(true));
    await waitFor(() => expect(screen.getByLabelText('长文本附件')).toHaveTextContent('返回后文本'));
    pasteLongText('返回后重复尝试'); await screen.findByText('文本附件未导入，内容已保留。');
    expect(currentImporter).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: /移除未导入的 返回后重复尝试/ }));
    await act(async () => current.resolve(true)); await waitFor(() => expect(screen.queryByLabelText('长文本附件')).not.toBeInTheDocument());
  });
});
