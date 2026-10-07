import { act, cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MotionActivityBoundary } from '@/design/motion';
import { ChatPresentationProvider } from '../reading/chat-presentation';
import type { ToolStatus } from '../model/types';
import { ToolStatusMark } from './ToolStatusMark';

afterEach(() => { cleanup(); vi.restoreAllMocks(); delete document.documentElement.dataset.reduceMotion; });

function view(status: ToolStatus, active = true, version: 'v1' | 'v2' = 'v2') {
  return <ChatPresentationProvider ownerKey={`status-motion-${version}`} defaultVersion={version}>
    <MotionActivityBoundary active={active}><ToolStatusMark status={status}/></MotionActivityBoundary>
  </ChatPresentationProvider>;
}

describe('receipt feedback, never execution authority', () => {
  it('keeps cold outcomes quiet and gives one mounted change feedback without replay on content renders', () => {
    const { container, rerender, unmount } = render(view('success'));
    const mark = () => container.querySelector('.ccui-execution-mark')!;
    expect(mark()).not.toHaveAttribute('data-changed');
    rerender(view('running'));
    expect(mark()).toHaveAttribute('data-changed', 'true');
    rerender(view('cancelled'));
    expect(mark()).toHaveAttribute('data-state', 'cancelled');
    expect(mark()).toHaveAttribute('data-changed', 'true');
    const glyph = mark().firstElementChild;
    rerender(view('cancelled'));
    expect(mark().firstElementChild).toBe(glyph);
    unmount();
    const cold = render(view('cancelled'));
    expect(cold.container.querySelector('.ccui-execution-mark')).not.toHaveAttribute('data-changed');
  });

  it('consumes inactive transitions without replay and clears feedback immediately on deactivation', () => {
    const { container, rerender } = render(view('running'));
    const mark = () => container.querySelector('.ccui-execution-mark')!;
    rerender(view('success'));
    expect(mark()).toHaveAttribute('data-changed', 'true');
    rerender(view('success', false));
    expect(mark()).toHaveAttribute('data-active', 'false');
    expect(mark()).not.toHaveAttribute('data-changed');
    rerender(view('error', false));
    expect(mark()).toHaveAttribute('data-state', 'error');
    rerender(view('error', true));
    expect(mark()).toHaveAttribute('data-active', 'true');
    expect(mark()).not.toHaveAttribute('data-changed');
  });

  it('settles hidden and reduced presentation while retaining the actual stopped state', () => {
    const visible = vi.spyOn(document, 'visibilityState', 'get');
    const { container, rerender } = render(view('running'));
    visible.mockReturnValue('hidden');
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    rerender(view('cancelled'));
    const mark = () => container.querySelector('.ccui-execution-mark')!;
    expect(mark()).toHaveAttribute('data-active', 'false');
    expect(mark()).toHaveAttribute('data-state', 'cancelled');
    visible.mockReturnValue('visible');
    act(() => document.dispatchEvent(new Event('visibilitychange')));
    expect(mark()).not.toHaveAttribute('data-changed');
    document.documentElement.dataset.reduceMotion = 'true';
    rerender(view('success'));
    expect(mark()).toHaveAttribute('data-active', 'false');
    expect(mark()).not.toHaveAttribute('data-changed');
  });

  it('keeps the v1 markup contract when rolling presentation back', () => {
    const { container, rerender } = render(view('success'));
    rerender(view('success', true, 'v1'));
    expect(container.querySelector('.ccui-execution-mark')).not.toHaveAttribute('data-feedback');
    expect(container.querySelector('.ccui-execution-mark')).not.toHaveAttribute('data-changed');
  });
});
