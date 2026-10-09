import { act, cleanup, render, waitFor } from '@testing-library/react';
import { StrictMode, type ReactNode } from 'react';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { recoveryScope } from '@/features/semantic-workspace/workspace-recovery';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TooltipProvider } from '@/components/primitives';
import { previewAgentSnapshot } from '../preview-data';
import { agentSessionAddress, selectAgentProjection, useAgentLiveStore } from '../state/live-store';
import { AgentTimeline } from './AgentTimeline';

const virtuosoMock = vi.hoisted(() => ({
  atBottomStateChange: undefined as ((atBottom: boolean) => void) | undefined,
  followOutput: undefined as (() => 'auto' | 'smooth' | false) | undefined,
  scroller: undefined as HTMLDivElement | undefined,
}));

vi.mock('react-virtuoso', async () => {
  const React = await import('react');
  return {
    Virtuoso: React.forwardRef(({
      atBottomStateChange,
      data,
      followOutput,
      itemContent,
      scrollerRef,
    }: {
      atBottomStateChange?: (atBottom: boolean) => void;
      data: string[];
      followOutput?: () => 'auto' | 'smooth' | false;
      itemContent: (index: number, item: string) => ReactNode;
      scrollerRef?: (scroller: HTMLElement | Window | null) => void;
    }, ref) => {
      const localScrollerRef = React.useRef<HTMLDivElement>(null);
      React.useImperativeHandle(ref, () => ({ scrollToIndex: vi.fn() }));
      React.useLayoutEffect(() => {
        virtuosoMock.scroller = localScrollerRef.current ?? undefined;
        scrollerRef?.(localScrollerRef.current);
        return () => scrollerRef?.(null);
      }, [scrollerRef]);
      virtuosoMock.atBottomStateChange = atBottomStateChange;
      virtuosoMock.followOutput = followOutput;
      return (
        <div ref={localScrollerRef} data-testid="agent-virtuoso">
          {data.map((item, index) => <div key={item}>{itemContent(index, item)}</div>)}
        </div>
      );
    }),
  };
});

const SESSION_ID = 'session-follow-intent';

afterEach(() => {
  cleanup();
  useAgentLiveStore.setState({ projections: {} });
  virtuosoMock.atBottomStateChange = undefined;
  virtuosoMock.followOutput = undefined;
  virtuosoMock.scroller = undefined;
});

describe('Agent timeline follow intent', () => {
  it('keeps a focused transcript action clear when a later snapshot appends content', async () => {
    useAgentLiveStore.getState().hydrateSnapshot(SESSION_ID, previewAgentSnapshot(SESSION_ID));
    const onFollowStateChange = vi.fn();
    render(<TooltipProvider><AgentTimeline modelSelectionAvailable onApprovalDecision={() => {}}
      onRetryTurn={() => false} onSwitchModel={() => {}} sessionId={SESSION_ID}
      onFollowStateChange={onFollowStateChange} /></TooltipProvider>);
    await waitFor(() => expect(virtuosoMock.scroller).toBeTruthy());
    const scroller = virtuosoMock.scroller!;
    Object.defineProperty(scroller, 'scrollHeight', { configurable: true, value: 900 });
    Object.defineProperty(scroller, 'clientHeight', { configurable: true, value: 400 });
    scroller.scrollTop = 215;
    expect(virtuosoMock.followOutput?.()).toBe('auto');
    const action = scroller.querySelector<HTMLButtonElement>('button')!;
    expect(action).toBeTruthy();
    act(() => action.focus());
    expect(action).toHaveFocus();
    expect(virtuosoMock.followOutput?.()).toBe(false);
    act(() => useAgentLiveStore.getState().appendOptimistic(SESSION_ID, {
      clientMessageId: 'later-snapshot-content', text: 'Later original snapshot content', nowMs: 200,
    }));
    await waitFor(() => expect(onFollowStateChange).toHaveBeenLastCalledWith({ following: false, unseenUpdates: 1 }));
    await act(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    expect(scroller.scrollTop).toBe(215);
    expect(action).toHaveFocus();
  });

  it('keeps live follow when the scrollport itself receives focus', async () => {
    useAgentLiveStore.getState().hydrateSnapshot(SESSION_ID, previewAgentSnapshot(SESSION_ID));
    render(<TooltipProvider><AgentTimeline modelSelectionAvailable onApprovalDecision={() => {}}
      onRetryTurn={() => false} onSwitchModel={() => {}} sessionId={SESSION_ID} /></TooltipProvider>);
    await waitFor(() => expect(virtuosoMock.scroller).toBeTruthy());
    const scroller = virtuosoMock.scroller!;
    scroller.tabIndex = 0;
    act(() => scroller.focus());
    expect(virtuosoMock.followOutput?.()).toBe('auto');
  });

  it('counts only the owning transport and resets follow state when the provider changes', async () => {
    const a = new MockControlTransport();
    const b = new MockControlTransport();
    const addressA = agentSessionAddress(a, SESSION_ID);
    const addressB = agentSessionAddress(b, SESSION_ID);
    useAgentLiveStore.getState().hydrateSnapshot(addressA, previewAgentSnapshot(SESSION_ID));
    useAgentLiveStore.getState().hydrateSnapshot(addressB, previewAgentSnapshot(SESSION_ID));
    const onFollowStateChange = vi.fn();
    const element = (transport: MockControlTransport) => (
      <ControlTransportProvider transport={transport}><TooltipProvider>
        <AgentTimeline modelSelectionAvailable onApprovalDecision={() => {}}
          onRetryTurn={() => false} onSwitchModel={() => {}} sessionId={SESSION_ID}
          onFollowStateChange={onFollowStateChange} />
      </TooltipProvider></ControlTransportProvider>
    );
    const view = render(element(a));
    await waitFor(() => expect(virtuosoMock.scroller).toBeTruthy());
    act(() => virtuosoMock.scroller!.dispatchEvent(new WheelEvent('wheel', { deltaY: -24 })));
    onFollowStateChange.mockClear();
    act(() => useAgentLiveStore.getState().appendOptimistic(addressB, {
      clientMessageId: 'other-transport', text: '乙的新消息', nowMs: 200,
    }));
    expect(onFollowStateChange).not.toHaveBeenCalled();
    act(() => useAgentLiveStore.getState().appendOptimistic(addressA, {
      clientMessageId: 'own-transport', text: '甲的新消息', nowMs: 200,
    }));
    expect(onFollowStateChange).toHaveBeenLastCalledWith({ following: false, unseenUpdates: 1 });

    view.rerender(element(b));
    expect(virtuosoMock.followOutput?.()).toBe('auto');
    expect(onFollowStateChange).toHaveBeenLastCalledWith({ following: true, unseenUpdates: 0 });
    act(() => virtuosoMock.scroller!.dispatchEvent(new WheelEvent('wheel', { deltaY: -24 })));
    onFollowStateChange.mockClear();
    act(() => useAgentLiveStore.getState().appendOptimistic(addressA, {
      clientMessageId: 'old-owner', text: '旧连接消息', nowMs: 300,
    }));
    expect(onFollowStateChange).not.toHaveBeenCalled();
  });

  it('focuses a jump inside its own transcript when both transports use the same message IDs', async () => {
    const a = new MockControlTransport();
    const b = new MockControlTransport();
    const snapshot = previewAgentSnapshot(SESSION_ID);
    const addressA = agentSessionAddress(a, SESSION_ID);
    useAgentLiveStore.getState().hydrateSnapshot(addressA, snapshot);
    useAgentLiveStore.getState().hydrateSnapshot(agentSessionAddress(b, SESSION_ID), snapshot);
    const messageId = selectAgentProjection(useAgentLiveStore.getState(), addressA)?.messageOrder[0];
    if (!messageId) throw new Error('The hydrated transcript fixture must contain a message');
    const view = render(<TooltipProvider>
      <ControlTransportProvider transport={a}>
        <AgentTimeline modelSelectionAvailable onApprovalDecision={() => {}}
          onRetryTurn={() => false} onSwitchModel={() => {}} sessionId={SESSION_ID} />
      </ControlTransportProvider>
      <ControlTransportProvider transport={b}>
        <AgentTimeline modelSelectionAvailable onApprovalDecision={() => {}}
          onRetryTurn={() => false} onSwitchModel={() => {}} sessionId={SESSION_ID}
          jumpRequest={{ messageId, requestId: 1 }} />
      </ControlTransportProvider>
    </TooltipProvider>);
    const messages = view.container.querySelectorAll<HTMLElement>(`[data-agent-message-id="${messageId}"]`);
    expect(messages).toHaveLength(2);
    await waitFor(() => expect(document.activeElement).toBe(messages[1]));
  });

  it('lets wheel, touch and keyboard reading detach until the user actually returns', async () => {
    useAgentLiveStore.getState().hydrateSnapshot(
      SESSION_ID,
      previewAgentSnapshot(SESSION_ID),
    );
    render(
      <TooltipProvider>
        <AgentTimeline
          modelSelectionAvailable
          onApprovalDecision={() => {}}
          onRetryTurn={() => false}
          onSwitchModel={() => {}}
          sessionId={SESSION_ID}
        />
      </TooltipProvider>,
    );

    await waitFor(() => expect(virtuosoMock.scroller).toBeTruthy());
    const scroller = virtuosoMock.scroller!;
    Object.defineProperty(scroller, 'scrollHeight', { configurable: true, value: 900 });
    Object.defineProperty(scroller, 'clientHeight', { configurable: true, value: 400 });
    scroller.scrollTop = 500;
    expect(virtuosoMock.followOutput?.()).toBe('auto');

    // Trackpads can report either sign depending on the platform/gesture. Any
    // real wheel movement is reader intent and must beat a simultaneous stream.
    act(() => scroller.dispatchEvent(new WheelEvent('wheel', { deltaY: 24 })));
    expect(virtuosoMock.followOutput?.()).toBe(false);

    // A virtualizer can report `atBottom` while reconciling a growing row.
    // That passive layout signal must not steal ownership back from the reader.
    act(() => virtuosoMock.atBottomStateChange?.(true));
    expect(virtuosoMock.followOutput?.()).toBe(false);

    scroller.scrollTop = 500;
    act(() => scroller.dispatchEvent(new Event('scroll')));
    expect(virtuosoMock.followOutput?.()).toBe('auto');

    scroller.scrollTop = 200;
    act(() => scroller.dispatchEvent(new Event('touchmove')));
    expect(virtuosoMock.followOutput?.()).toBe(false);
    act(() => virtuosoMock.atBottomStateChange?.(true));
    expect(virtuosoMock.followOutput?.()).toBe(false);
    scroller.scrollTop = 500;
    act(() => scroller.dispatchEvent(new Event('scroll')));
    expect(virtuosoMock.followOutput?.()).toBe('auto');

    scroller.scrollTop = 200;
    act(() => scroller.dispatchEvent(new KeyboardEvent('keydown', { key: 'PageUp' })));
    expect(virtuosoMock.followOutput?.()).toBe(false);
    act(() => virtuosoMock.atBottomStateChange?.(true));
    expect(virtuosoMock.followOutput?.()).toBe(false);
  });
  it('keeps a persisted position during StrictMode cleanup before messages load', () => {
    const transport = new MockControlTransport();
    Object.defineProperty(transport, 'connectionIdentity', { value: 'reading-recovery-test' });
    const key = recoveryScope(transport, `session:${SESSION_ID}`) + ':anchor';
    const saved = JSON.stringify({ conversationId: SESSION_ID, rowKey: 'old-turn', rowIndex: 1, offsetFromViewportTopPx: -30, fallbackScrollTop: 200 });
    localStorage.setItem(key, saved);
    const view = render(<StrictMode><ControlTransportProvider transport={transport}><TooltipProvider>
      <AgentTimeline modelSelectionAvailable onApprovalDecision={() => {}} onRetryTurn={() => false} onSwitchModel={() => {}} sessionId={SESSION_ID} />
    </TooltipProvider></ControlTransportProvider></StrictMode>);
    view.unmount();
    expect(localStorage.getItem(key)).toBe(saved);
    localStorage.removeItem(key);
  });

});
