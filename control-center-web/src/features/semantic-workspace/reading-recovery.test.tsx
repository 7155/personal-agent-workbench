import { useMemo, useRef } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { recoveryScope } from './workspace-recovery';
import { readRoomRoundReading, useRoomReadingRecovery, useRoomRoundReadingRecovery, useRoomViewRecovery } from './reading-recovery';

beforeEach(() => localStorage.clear());
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); localStorage.clear(); });

function Rounds({ scope = 'backend-a:room-a' }: { scope?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const initial = useMemo(() => readRoomRoundReading(scope), [scope]);
  useRoomRoundReadingRecovery(ref, scope, true, initial, () => ({
    followingLatest: initial?.followingLatest ?? false,
    historicalRoundIds: ['round-a'], processDisclosure: [['round-a', true]], expandedRowIds: ['row-a'],
  }));
  return <div ref={ref} aria-label="轮次记录"><article data-round-id="round-a">只显示的对话文本，不属于保存数据</article></div>;
}
function mockGeometry() {
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(800);
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(200);
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    const top = this.dataset.roundId ? 100 - (this.parentElement?.scrollTop ?? 0) : 100;
    return { top, bottom: top + 800, height: 800, left: 0, right: 700, width: 700, x: 0, y: top, toJSON: () => ({}) };
  });
}
it('coalesces scroll persistence and flushes the final UI-only position on pagehide/unmount', () => {
  vi.useFakeTimers(); mockGeometry();
  const writes = vi.spyOn(Storage.prototype, 'setItem');
  const rendered = render(<Rounds />);
  const node = screen.getByLabelText('轮次记录');
  for (const scrollTop of [100, 150, 200, 250, 300]) fireEvent.scroll(node, { target: { scrollTop } });
  expect(writes).not.toHaveBeenCalled();
  act(() => vi.advanceTimersByTime(250));
  expect(writes).toHaveBeenCalledTimes(1);
  fireEvent.scroll(node, { target: { scrollTop: 350 } });
  fireEvent(window, new Event('pagehide'));
  expect(writes).toHaveBeenCalledTimes(2);
  const saved = JSON.parse(localStorage.getItem('backend-a:room-a:reading:rounds:v2')!);
  expect(saved).toEqual({ rowId: 'round-a', offset: -350, scrollTop: 350, followingLatest: false,
    historicalRoundIds: ['round-a'], processDisclosure: [['round-a', true]], expandedRowIds: ['row-a'] });
  rendered.unmount();
  expect(writes).toHaveBeenCalledTimes(3);
  act(() => vi.advanceTimersByTime(1000));
  expect(writes).toHaveBeenCalledTimes(3);
});
it('clamps a cropped-away round to available history without requesting more records', () => {
  mockGeometry();
  localStorage.setItem('backend-a:room-a:reading:rounds:v2', JSON.stringify({ rowId: 'cropped-round', offset: -20,
    scrollTop: 900, followingLatest: false, historicalRoundIds: [], processDisclosure: [], expandedRowIds: [] }));
  const fetch = vi.spyOn(globalThis, 'fetch');
  render(<Rounds />);
  expect(screen.getByLabelText('轮次记录').scrollTop).toBe(600);
  expect(fetch).not.toHaveBeenCalled();
});
it.each(['malformed', 'denied'] as const)('keeps reading functional with %s storage', mode => {
  mockGeometry();
  if (mode === 'malformed') localStorage.setItem('backend-a:room-a:reading:rounds:v2', '{unfinished');
  else {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new DOMException('disabled', 'SecurityError'); });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('disabled', 'SecurityError'); });
  }
  const rendered = render(<Rounds />);
  const node = screen.getByLabelText('轮次记录');
  fireEvent.scroll(node, { target: { scrollTop: 200 } });
  fireEvent(window, new Event('pagehide'));
  expect(node.scrollTop).toBe(200);
  expect(node).toHaveTextContent('只显示的对话文本');
  expect(() => rendered.unmount()).not.toThrow();
});

function LegacyTranscript() {
  const ref = useRef<HTMLDivElement>(null);
  const [view, setView] = useRoomViewRecovery('room:a');
  useRoomReadingRecovery(ref, 'room:a', true, view);
  return <><button onClick={() => setView('messages')}>{view}</button><div ref={ref} aria-label="旧对话记录"><p data-room-message-id="message-a">原对话</p></div></>;
}
it('retains the existing connection-scoped view and transcript storage contract', () => {
  const transport = new MockControlTransport();
  Object.defineProperty(transport, 'connectionIdentity', { value: 'backend-a' });
  const scope = recoveryScope(transport, 'room:a');
  localStorage.setItem(scope + ':view', 'conversation');
  localStorage.setItem(scope + ':reading:conversation', JSON.stringify({ rowId: 'message-a', offset: -70, scrollTop: 500 }));
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    const top = this.dataset.roomMessageId ? 130 : 100;
    return { top, bottom: top + 600, height: 600, left: 0, right: 700, width: 700, x: 0, y: top, toJSON: () => ({}) };
  });
  render(<ControlTransportProvider transport={transport}><LegacyTranscript /></ControlTransportProvider>);
  expect(screen.getByRole('button', { name: 'conversation' })).toBeInTheDocument();
  expect(screen.getByLabelText('旧对话记录').scrollTop).toBe(100);
  fireEvent.click(screen.getByRole('button', { name: 'conversation' }));
  expect(localStorage.getItem(scope + ':view')).toBe('messages');
});
