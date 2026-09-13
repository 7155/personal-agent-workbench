import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

function Harness({ surface, importer, busy = false, onSend = vi.fn(), onPick = vi.fn() }: {
  surface: 'Session' | 'Room'; importer: ComposerFileImporter; busy?: boolean; onSend?: (...args: string[]) => void; onPick?: () => void;
}) {
  const [draft, setDraft] = useState('请核对附件');
  const common = { draft, onDraftChange: setDraft, sending: false, onPasteImages: importer, onPickAttachments: onPick, onSend, onAttachmentsChange: vi.fn() };
  return <TooltipProvider>{surface === 'Room'
    ? <RoomComposer {...common} room={{ id: 'room-a', status: 'active', participants: [participant] }} attachments={[]} personas={[]} taskBusyState={busy ? 'running' : undefined} onPasteFromClipboard={vi.fn()} />
    : <AgentComposer {...common} attachments={[]} session={previewSessions[0]} minimal busy={busy} commands={[]} tools={[]} toolCatalogStatus="ready" onToolSelect={vi.fn()} onProductCommand={vi.fn()} onStop={vi.fn()} onPermissionChange={vi.fn()} onWorkspaceRootsChange={vi.fn()} onModelChange={vi.fn()} />}</TooltipProvider>;
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
