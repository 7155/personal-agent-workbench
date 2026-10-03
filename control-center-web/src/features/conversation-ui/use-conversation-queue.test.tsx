import { StrictMode, type ReactNode } from 'react';
import { act, renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { useConversationQueue } from './use-conversation-queue';

describe('useConversationQueue', () => {
  it.each(['drain', 'sendNow'] as const)('retains a rejected edited draft without retrying on render in StrictMode (%s)', mode => {
    const send = vi.fn(() => false);
    const wrapper = ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>;
    const { result, rerender } = renderHook(({ busy }) => useConversationQueue({
      busy, conversationId: 'session-rejected-queue', send,
    }), { wrapper, initialProps: { busy: true } });
    act(() => { result.current.enqueue('原本允许的消息'); });
    const id = result.current.queue[0]!.id;
    act(() => { result.current.edit(id, '/branch'); });
    const held = result.current.queue;
    if (mode === 'drain') rerender({ busy: false });
    else act(() => { result.current.sendNow(id); });
    expect(send).toHaveBeenCalledExactlyOnceWith('/branch');
    expect(result.current.queue).toEqual(held);
    rerender({ busy: mode === 'sendNow' });
    expect(send).toHaveBeenCalledTimes(1);
    expect(result.current.queue[0]).toMatchObject({ id, text: '/branch' });
    send.mockImplementation(() => true);
    act(() => { result.current.sendNow(id); });
    expect(send).toHaveBeenCalledTimes(2);
    expect(result.current.queue).toHaveLength(0);
  });

  it.each([undefined, true])('consumes an accepted automatic send once in StrictMode (admission=%s)', admission => {
    const send = vi.fn(() => admission);
    const wrapper = ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>;
    const { result, rerender } = renderHook(({ busy }) => useConversationQueue({
      busy, conversationId: 'session-accepted-queue', send,
    }), { wrapper, initialProps: { busy: true } });
    act(() => { result.current.enqueue('允许的消息'); });
    rerender({ busy: false });
    expect(send).toHaveBeenCalledExactlyOnceWith('允许的消息');
    expect(result.current.queue).toHaveLength(0);
    rerender({ busy: false });
    expect(send).toHaveBeenCalledTimes(1);
  });

  it('sends a queued draft exactly once when sendNow runs in StrictMode', () => {
    const send = vi.fn();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <StrictMode>{children}</StrictMode>
    );
    const { result } = renderHook(
      () => useConversationQueue({
        busy: true,
        conversationId: 'session-strict-queue',
        send,
      }),
      { wrapper },
    );

    act(() => {
      result.current.enqueue('调整当前执行方向');
    });
    const queuedId = result.current.queue[0]?.id;
    expect(queuedId).toBeTruthy();

    act(() => result.current.sendNow(queuedId!));

    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith('调整当前执行方向');
    expect(result.current.queue).toHaveLength(0);
  });
});
