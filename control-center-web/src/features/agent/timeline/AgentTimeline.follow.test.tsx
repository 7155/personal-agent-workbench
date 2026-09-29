import { act, cleanup, render, waitFor } from '@testing-library/react';
import { StrictMode, type ReactNode } from 'react';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { recoveryScope } from '@/features/semantic-workspace/workspace-recovery';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TooltipProvider } from '@/components/primitives';
import { previewAgentSnapshot } from '../preview-data';
import { useAgentLiveStore } from '../state/live-store';
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
  useAgentLiveStore.getState().clear(SESSION_ID);
  virtuosoMock.atBottomStateChange = undefined;
  virtuosoMock.followOutput = undefined;
  virtuosoMock.scroller = undefined;
});

describe('Agent timeline follow intent', () => {
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
