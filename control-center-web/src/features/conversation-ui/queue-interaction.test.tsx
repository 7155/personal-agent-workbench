import { StrictMode, useState } from 'react';
import { act, cleanup, render, renderHook, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TooltipProvider } from '@/components/primitives';
import { RoomComposer } from '@/features/rooms/composer/RoomComposer';
import { QueueTray } from './components/QueueTray';
import { useConversationQueue } from './use-conversation-queue';

afterEach(cleanup);

describe('queued composer interaction', () => {
  it.each([false, true])('clears each successfully queued draft without reporting a full queue (StrictMode=%s)', async (strict) => {
    function Harness() {
      const [draft, setDraft] = useState('');
      const queue = useConversationQueue({ busy: true, conversationId: 'room-a', send: vi.fn() });
      return <TooltipProvider><QueueTray controller={queue} busy /><RoomComposer
        room={{ id: 'room-a', status: 'active', participants: [] }} personas={[]}
        draft={draft} onDraftChange={setDraft} attachments={[]} sending={false}
        taskBusyState="running" queueDepth={queue.queue.length} onQueue={queue.enqueue}
        onAttachmentsChange={vi.fn()} onPasteImages={vi.fn()} onPasteFromClipboard={vi.fn()}
        onPickAttachments={vi.fn()} onSend={vi.fn()}
      /></TooltipProvider>;
    }
    const user = userEvent.setup();
    render(strict ? <StrictMode><Harness /></StrictMode> : <Harness />);
    const editor = screen.getByRole('textbox', { name: '协作消息' });
    for (const text of ['接着检查', '然后总结']) {
      await user.type(editor, text);
      await user.click(screen.getByRole('button', { name: /排到当前回合之后/ }));
      expect(editor).toHaveValue('');
      expect(screen.queryByText(/排队已满/)).not.toBeInTheDocument();
    }
    expect(screen.getByRole('button', { name: /2 条排队中/ })).toBeInTheDocument();
  });

  it('bounds synchronous admissions and consumes a repeated send action once', () => {
    const send = vi.fn();
    const { result } = renderHook(() => useConversationQueue({ conversationId: 'room-a', busy: true, send }));
    act(() => {
      for (let index = 0; index < 8; index += 1) expect(result.current.enqueue(`消息 ${index}`)).toBe(true);
      expect(result.current.enqueue('第九条留在输入框')).toBe(false);
    });
    expect(result.current.queue).toHaveLength(8);
    expect(result.current.capReached).toBe(true);
    const first = result.current.queue[0]!.id;
    act(() => { result.current.sendNow(first); result.current.sendNow(first); });
    expect(send).toHaveBeenCalledExactlyOnceWith('消息 0');
    expect(result.current.queue).toHaveLength(7);
    let restored = '';
    act(() => { restored = result.current.restoreToDraft('新草稿'); });
    expect(restored.split('\n\n')).toEqual(['新草稿', ...Array.from({ length: 7 }, (_, index) => `消息 ${index + 1}`)]);
    expect(result.current.queue).toHaveLength(0);
  });

  it('ignores callbacks retained by a previous conversation', () => {
    const { result, rerender } = renderHook(({ conversationId }) => useConversationQueue({ conversationId, busy: true, send: vi.fn() }), {
      initialProps: { conversationId: 'room-a' },
    });
    const stale = result.current;
    rerender({ conversationId: 'room-b' });
    act(() => { result.current.enqueue('B 的草稿'); });
    const id = result.current.queue[0]!.id;
    act(() => {
      expect(stale.enqueue('A 的草稿')).toBe(false);
      stale.remove(id); stale.edit(id, '错误修改'); stale.reorder(id, id); stale.clear(); stale.sendNow(id);
      expect(stale.restoreToDraft('A 当前输入')).toBe('A 当前输入');
    });
    expect(result.current.queue.map(item => item.text)).toEqual(['B 的草稿']);
  });

  it('does not dispatch the previous room queue on an identity switch', () => {
    const send = vi.fn();
    const { result, rerender } = renderHook(({ conversationId, busy }) => useConversationQueue({ conversationId, busy, send }), {
      initialProps: { conversationId: 'room-a', busy: true },
    });
    act(() => { result.current.enqueue('只发到 A'); });
    rerender({ conversationId: 'room-b', busy: false });
    expect(send).not.toHaveBeenCalled();
    expect(result.current.queue).toHaveLength(0);
  });
});
