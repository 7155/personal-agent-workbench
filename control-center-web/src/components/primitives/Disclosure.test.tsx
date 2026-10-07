import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Disclosure } from './Disclosure';
import { MotionActivityBoundary } from '@/design/motion';

afterEach(() => {
  cleanup();
  delete document.documentElement.dataset.reduceMotion;
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('Disclosure', () => {
  it('settles a keyboard close immediately without leaving a hidden exit timer', () => {
    vi.useFakeTimers();
    render(<Disclosure defaultOpen summary="键盘详情"><p>键盘内容</p></Disclosure>);
    const baselineTimers = vi.getTimerCount(); // Native details schedules its own toggle notification.
    fireEvent.keyDown(screen.getByText('键盘详情'), { key: 'Enter' });
    expect(screen.queryByText('键盘内容')).not.toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(baselineTimers);
  });

  it('stops an in-flight exit when its host becomes inactive', () => {
    vi.useFakeTimers();
    const surface = (active: boolean) => <MotionActivityBoundary active={active}>
      <Disclosure defaultOpen summary="活动详情"><p>活动内容</p></Disclosure>
    </MotionActivityBoundary>;
    const view = render(surface(true));
    const baselineTimers = vi.getTimerCount();
    fireEvent.click(screen.getByText('活动详情'), { detail: 1 });
    expect(screen.getByText('活动内容')).toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(baselineTimers + 1);
    view.rerender(surface(false));
    expect(screen.queryByText('活动内容')).not.toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(baselineTimers);
  });

  it('uses one toggle path for pointer, Enter, and Space while exposing truthful ARIA state', async () => {
    const user = userEvent.setup();
    const onOpenChange = vi.fn();
    render(
      <Disclosure className="feature-disclosure" exitDurationMs={0} onOpenChange={onOpenChange} summary="查看详情">
        <p>完整内容</p>
      </Disclosure>,
    );

    const { details, reveal, summary } = disclosureElements('查看详情');
    expect(details).toHaveClass('ui-disclosure', 'feature-disclosure');
    expect(details).not.toHaveAttribute('open');
    expect(summary).toHaveAttribute('aria-expanded', 'false');
    expect(summary).toHaveAttribute('aria-controls', reveal.id);
    expect(reveal).toHaveAttribute('aria-hidden', 'true');
    expect(reveal).toHaveAttribute('inert');

    await user.click(summary);
    expect(details).toHaveAttribute('open');
    expect(summary).toHaveAttribute('aria-expanded', 'true');
    expect(reveal).toHaveAttribute('aria-hidden', 'false');
    expect(reveal).not.toHaveAttribute('inert');
    expect(onOpenChange).toHaveBeenNthCalledWith(1, true);

    summary.focus();
    await user.keyboard('{Enter}');
    expect(details).not.toHaveAttribute('open');
    expect(summary).toHaveAttribute('aria-expanded', 'false');
    expect(onOpenChange).toHaveBeenNthCalledWith(2, false);

    await user.keyboard(' ');
    expect(details).toHaveAttribute('open');
    expect(summary).toHaveAttribute('aria-expanded', 'true');
    expect(onOpenChange).toHaveBeenNthCalledWith(3, true);
    expect(onOpenChange).toHaveBeenCalledTimes(3);
  });

  it('honors defaultOpen and isolates nested disclosure state', async () => {
    const user = userEvent.setup();
    render(
      <Disclosure defaultOpen exitDurationMs={0} summary="外层">
        <Disclosure exitDurationMs={0} summary="内层">
          <p>内层内容</p>
        </Disclosure>
      </Disclosure>,
    );

    const outer = disclosureElements('外层');
    expect(outer.details).toHaveAttribute('open');
    expect(outer.summary).toHaveAttribute('aria-expanded', 'true');

    const inner = disclosureElements('内层');
    expect(inner.details).not.toHaveAttribute('open');
    await user.click(inner.summary);
    expect(inner.details).toHaveAttribute('open');
    expect(outer.details).toHaveAttribute('open');

    await user.click(outer.summary);
    expect(outer.details).not.toHaveAttribute('open');
    expect(screen.queryByText('内层内容')).not.toBeInTheDocument();

    await user.click(outer.summary);
    const remountedInner = disclosureElements('内层');
    expect(remountedInner.details).not.toHaveAttribute('open');
    expect(remountedInner.reveal).toHaveAttribute('inert');
  });

  it('cancels an exit timer when a close is immediately reversed', async () => {
    vi.useFakeTimers();
    render(
      <Disclosure defaultOpen exitDurationMs={20} summary="查看详情">
        <p>完整内容</p>
      </Disclosure>,
    );

    const { details, summary } = disclosureElements('查看详情');
    fireEvent.click(summary, { detail: 1 });
    expect(details).toHaveAttribute('open');
    expect(details).not.toHaveAttribute('data-expanded');
    expect(summary).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText('完整内容')).toBeInTheDocument();

    fireEvent.click(summary, { detail: 1 });
    expect(details).toHaveAttribute('open');
    await act(async () => {
      vi.advanceTimersByTime(25);
    });
    expect(screen.getByText('完整内容')).toBeInTheDocument();
  });

  it('does not toggle when a summary contains an interactive target', async () => {
    const user = userEvent.setup();
    const action = vi.fn();
    const onOpenChange = vi.fn();
    render(
      <Disclosure
        exitDurationMs={0}
        onOpenChange={onOpenChange}
        summary={(
          <>
            <span>可展开</span>
            <button onClick={action} type="button">执行动作</button>
          </>
        )}
      >
        <p>完整内容</p>
      </Disclosure>,
    );

    const { details } = disclosureElements('可展开');
    await user.click(screen.getByRole('button', { name: '执行动作' }));
    expect(action).toHaveBeenCalledTimes(1);
    expect(details).not.toHaveAttribute('open');
    expect(onOpenChange).not.toHaveBeenCalled();

    await user.click(screen.getByText('可展开'));
    expect(details).toHaveAttribute('open');
  });

  it('ignores interactive ancestors outside the summary', async () => {
    const user = userEvent.setup();
    render(
      <section role="tabpanel" tabIndex={0}>
        <Disclosure exitDurationMs={0} summary="查看面板内详情">
          <p>面板内完整内容</p>
        </Disclosure>
      </section>,
    );

    const { details, summary } = disclosureElements('查看面板内详情');
    await user.click(summary);
    expect(details).toHaveAttribute('open');
    expect(screen.getByText('面板内完整内容')).toBeInTheDocument();
  });

  it.each([
    ['system preference', () => vi.stubGlobal('matchMedia', () => ({ matches: true }))],
    ['data preference', () => {
      vi.stubGlobal('matchMedia', () => ({ matches: false }));
      document.documentElement.dataset.reduceMotion = 'true';
    }],
  ])('unmounts exit content immediately for %s reduced motion', async (_label, configure) => {
    configure();
    const user = userEvent.setup();
    render(
      <Disclosure defaultOpen exitDurationMs={1_000} summary="查看详情">
        <p>完整内容</p>
      </Disclosure>,
    );

    const { summary, reveal } = disclosureElements('查看详情');
    await user.click(summary);
    expect(reveal).toHaveAttribute('inert');
    expect(screen.queryByText('完整内容')).not.toBeInTheDocument();
  });

  it('keeps exiting content mounted until the close transition can finish', async () => {
    const user = userEvent.setup();
    render(<Disclosure exitDurationMs={20} summary="查看详情"><p>完整内容</p></Disclosure>);

    const { details, reveal, summary } = disclosureElements('查看详情');
    expect(details).not.toHaveAttribute('open');
    expect(screen.queryByText('完整内容')).not.toBeInTheDocument();

    await user.click(summary);
    expect(details).toHaveAttribute('open');
    expect(reveal).toHaveAttribute('aria-hidden', 'false');
    expect(reveal).not.toHaveAttribute('inert');
    expect(screen.getByText('完整内容')).toBeInTheDocument();

    await user.click(summary);
    expect(details).toHaveAttribute('open');
    expect(details).not.toHaveAttribute('data-expanded');
    expect(reveal).toHaveAttribute('aria-hidden', 'true');
    expect(reveal).toHaveAttribute('inert');
    expect(screen.getByText('完整内容')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText('完整内容')).not.toBeInTheDocument();
      expect(details).not.toHaveAttribute('open');
    });
  });
});

function disclosureElements(label: string) {
  const summary = screen.getByText(label);
  const details = summary.closest('details');
  if (!details) throw new Error(`Missing details for ${label}`);
  const reveal = details.querySelector<HTMLElement>('.ui-disclosure__reveal');
  if (!reveal) throw new Error(`Missing reveal for ${label}`);
  return { details, reveal, summary };
}
