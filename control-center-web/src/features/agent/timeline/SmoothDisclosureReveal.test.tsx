import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  DISCLOSURE_MOTION,
  SmoothDisclosureReveal,
} from './SmoothDisclosureReveal';

describe('SmoothDisclosureReveal', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => (
      window.setTimeout(() => callback(performance.now()), 16)
    ));
    vi.stubGlobal('cancelAnimationFrame', (handle: number) => window.clearTimeout(handle));
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function measuredRect(this: HTMLElement) {
      const height = this.classList.contains('agent-smooth-reveal')
        ? Number.parseFloat(this.style.height) || (this.dataset.state === 'open' ? 240 : 0)
        : 240;
      return {
        bottom: height,
        height,
        left: 0,
        right: 320,
        top: 0,
        width: 320,
        x: 0,
        y: 0,
        toJSON: () => ({}),
      };
    });
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
      configurable: true,
      get: () => 240,
    });
  });

  afterEach(() => {
    document.documentElement.removeAttribute('data-reduce-motion');
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('uses the bounded spring motion contract shared with the conversation baseline', () => {
    expect(DISCLOSURE_MOTION).toEqual({
      durationMs: 220,
      easing: 'cubic-bezier(0.34, 1.4, 0.64, 1)',
    });
  });

  it('finishes an opening before a descendant action receives native focus', () => {
    const content = <button type="button">Copy original evidence</button>;
    const view = render(<SmoothDisclosureReveal id="focus-opening" open={false}>{content}</SmoothDisclosureReveal>);
    view.rerender(<SmoothDisclosureReveal id="focus-opening" open>{content}</SmoothDisclosureReveal>);
    const region = document.getElementById('focus-opening')!;
    expect(region).toHaveAttribute('data-state', 'opening');
    act(() => screen.getByRole('button', { name: 'Copy original evidence' }).focus());
    expect(region).toHaveAttribute('data-state', 'open');
    expect(region).toHaveStyle({ height: 'auto' });
    expect(screen.getByRole('button', { name: 'Copy original evidence' })).toHaveFocus();
    act(() => vi.advanceTimersByTime(DISCLOSURE_MOTION.durationMs + 100));
    expect(region).toHaveStyle({ height: 'auto' });
  });

  it('does not reopen closing evidence when a synthetic focus event reaches it', () => {
    const content = <button type="button">Closing evidence</button>;
    const view = render(<SmoothDisclosureReveal id="focus-closing" open>{content}</SmoothDisclosureReveal>);
    view.rerender(<SmoothDisclosureReveal id="focus-closing" open={false}>{content}</SmoothDisclosureReveal>);
    const region = document.getElementById('focus-closing')!;
    fireEvent.focusIn(screen.getByText('Closing evidence'));
    expect(region).toHaveAttribute('data-state', 'closing');
    expect(region).toHaveAttribute('inert');
    act(() => vi.advanceTimersByTime(DISCLOSURE_MOTION.durationMs + 100));
    expect(region).toHaveAttribute('data-state', 'closed');
  });

  it('keeps closing content mounted, supports reversal, and removes it only after exit', async () => {
    const view = render(
      <SmoothDisclosureReveal id="evidence" open={false}>
        <p>真实工具证据</p>
      </SmoothDisclosureReveal>,
    );
    const region = document.getElementById('evidence')!;
    expect(region).toHaveAttribute('aria-hidden', 'true');
    expect(region).toHaveAttribute('data-state', 'closed');
    expect(screen.queryByText('真实工具证据')).not.toBeInTheDocument();

    view.rerender(
      <SmoothDisclosureReveal id="evidence" open>
        <p>真实工具证据</p>
      </SmoothDisclosureReveal>,
    );
    await act(async () => { vi.advanceTimersByTime(17); });
    expect(region).toHaveAttribute('aria-hidden', 'false');
    expect(region).toHaveAttribute('data-state', 'opening');
    expect(screen.getByText('真实工具证据')).toBeInTheDocument();
    fireEvent.transitionEnd(region, { propertyName: 'height' });
    expect(region).toHaveAttribute('data-state', 'open');

    view.rerender(
      <SmoothDisclosureReveal id="evidence" open={false}>
        <p>真实工具证据</p>
      </SmoothDisclosureReveal>,
    );
    await act(async () => { vi.advanceTimersByTime(17); });
    expect(region).toHaveAttribute('aria-hidden', 'true');
    expect(region).toHaveAttribute('data-state', 'closing');
    expect(screen.getByText('真实工具证据')).toBeInTheDocument();

    view.rerender(
      <SmoothDisclosureReveal id="evidence" open>
        <p>真实工具证据</p>
      </SmoothDisclosureReveal>,
    );
    await act(async () => { vi.advanceTimersByTime(17); });
    expect(region).toHaveAttribute('aria-hidden', 'false');
    expect(region).toHaveAttribute('data-state', 'opening');
    expect(screen.getByText('真实工具证据')).toBeInTheDocument();

    view.rerender(
      <SmoothDisclosureReveal id="evidence" open={false}>
        <p>真实工具证据</p>
      </SmoothDisclosureReveal>,
    );
    await act(async () => { vi.advanceTimersByTime(17); });
    fireEvent.transitionEnd(region, { propertyName: 'height' });
    expect(region).toHaveAttribute('data-state', 'closed');
    expect(screen.queryByText('真实工具证据')).not.toBeInTheDocument();
  });

  it('waits for close completion when layout temporarily measures zero height', async () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({
      bottom: 0,
      height: 0,
      left: 0,
      right: 0,
      top: 0,
      width: 0,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    }));
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
      configurable: true,
      get: () => 0,
    });
    const view = render(
      <SmoothDisclosureReveal id="zero-height" open>
        <p>仍在退出的证据</p>
      </SmoothDisclosureReveal>,
    );
    const region = document.getElementById('zero-height')!;

    view.rerender(
      <SmoothDisclosureReveal id="zero-height" open={false}>
        <p>仍在退出的证据</p>
      </SmoothDisclosureReveal>,
    );
    await act(async () => { vi.advanceTimersByTime(17); });
    expect(region).toHaveAttribute('data-state', 'closing');
    expect(screen.getByText('仍在退出的证据')).toBeInTheDocument();

    fireEvent.transitionEnd(region, { propertyName: 'height' });
    expect(region).toHaveAttribute('data-state', 'closed');
    expect(screen.queryByText('仍在退出的证据')).not.toBeInTheDocument();
  });

  it('keeps keepMounted children in the DOM while closed yet hidden from readers', async () => {
    const onPresenceChange = vi.fn();
    const view = render(
      <SmoothDisclosureReveal id="lane" keepMounted onPresenceChange={onPresenceChange} open={false}>
        <p>时间线记录</p>
      </SmoothDisclosureReveal>,
    );
    const region = document.getElementById('lane')!;
    expect(screen.getByText('时间线记录')).toBeInTheDocument();
    expect(region).toHaveAttribute('aria-hidden', 'true');
    expect(region).toHaveAttribute('inert');
    expect(region).toHaveAttribute('data-state', 'closed');
    expect(region).toHaveStyle({ height: '0px' });

    view.rerender(
      <SmoothDisclosureReveal id="lane" keepMounted onPresenceChange={onPresenceChange} open>
        <p>时间线记录</p>
      </SmoothDisclosureReveal>,
    );
    await act(async () => { vi.advanceTimersByTime(17); });
    fireEvent.transitionEnd(region, { propertyName: 'height' });
    expect(region).toHaveAttribute('data-state', 'open');
    expect(onPresenceChange).toHaveBeenLastCalledWith(true);

    view.rerender(
      <SmoothDisclosureReveal id="lane" keepMounted onPresenceChange={onPresenceChange} open={false}>
        <p>时间线记录</p>
      </SmoothDisclosureReveal>,
    );
    await act(async () => { vi.advanceTimersByTime(17); });
    fireEvent.transitionEnd(region, { propertyName: 'height' });
    expect(region).toHaveAttribute('data-state', 'closed');
    expect(onPresenceChange).toHaveBeenLastCalledWith(false);
    expect(screen.getByText('时间线记录')).toBeInTheDocument();
  });

  it('uses an immediate height path when motion is reduced', () => {
    document.documentElement.dataset.reduceMotion = 'true';
    const view = render(
      <SmoothDisclosureReveal id="reduced" open>
        <p>简化动画内容</p>
      </SmoothDisclosureReveal>,
    );
    const region = document.getElementById('reduced')!;
    expect(region).toHaveAttribute('data-state', 'open');
    expect(region).toHaveStyle({ height: 'auto' });

    view.rerender(
      <SmoothDisclosureReveal id="reduced" open={false}>
        <p>简化动画内容</p>
      </SmoothDisclosureReveal>,
    );
    expect(region).toHaveAttribute('data-state', 'closed');
    expect(screen.queryByText('简化动画内容')).not.toBeInTheDocument();
  });
});
