import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AgentActivityProjection } from '@/contracts/agent-reducer';
import { PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import { ActivitySummary, FxActivityStack, ReasoningActivitySummary, resetActivityDisclosureOverrides } from './ActivitySummary';
import { inspectableRawResultText, publicToolResultView } from './public-tool-result';
import { parseTraceAgentHandoff } from '@/features/trace-agent/handoff';

afterEach(() => {
  cleanup();
  resetActivityDisclosureOverrides();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined });
});

describe('Agent tool activity details', () => {
  it.each(['PAW_GUARD_MODEL_REJECTED', 'PAW_PROVIDER_CALL_LIMIT'])('shows a safe concrete reason for %s', (code) => {
    const activity: AgentActivityProjection = {
      id: 'guard-failure', turnId: 'guard-turn', kind: 'turn_failed', status: 'failed',
      summary: '模型服务请求失败',
      payload: { error: `${code} Authorization: Bearer secret-material` },
      createdAtMs: 1, updatedAtMs: 2,
    };
    const { container } = render(<ActivitySummary activities={[activity]} />);
    expect(container).toHaveTextContent('模型请求被运行约束拦截');
    expect(container).not.toHaveTextContent('secret-material');
    openActivity(container);
    expect(screen.getByRole('dialog')).toHaveTextContent(code);
    expect(screen.getByRole('dialog')).not.toHaveTextContent('secret-material');
  });
  it('keeps an already visible stack open as real tool calls arrive', () => {
    const activities = Array.from({ length: 6 }, (_, index) => toolActivity('tool_finished', 'completed', {
      toolCallId: `stream-growth-${index}`,
      toolName: 'workspace_read',
      args: { path: `src/file-${index}.ts` },
    }));
    const { rerender } = render(<FxActivityStack activities={activities.slice(0, 4)} sessionId="growth" />);
    const toggle = screen.getByRole('button', { name: /全部收起工具与思考步骤/u });

    rerender(<FxActivityStack activities={activities} sessionId="growth" />);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getAllByRole('treeitem')).toHaveLength(6);
  });

  it('scopes a disclosure choice to the Session and restores it after a virtualized remount', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'same-runtime-id', toolName: 'workspace_read', args: { path: 'src/app.ts' },
    });
    const view = render(<FxActivityStack activities={[activity]} sessionId="first-session" />);
    const row = () => view.container.querySelector('.paw-activity')!;
    fireEvent.click(row());
    expect(row()).toHaveAttribute('aria-expanded', 'true');

    view.rerender(<FxActivityStack activities={[activity]} sessionId="second-session" />);
    expect(row()).toHaveAttribute('aria-expanded', 'false');
    view.unmount();
    const restored = render(<FxActivityStack activities={[activity]} sessionId="first-session" />);
    expect(restored.container.querySelector('.paw-activity')).toHaveAttribute('aria-expanded', 'true');
  });

  it('summarizes the public action and target without printing command output in the row', () => {
    const activities = [
      toolActivity('tool_finished', 'completed', {
        toolCallId: 'concise-read', toolName: 'workspace_read', args: { path: 'src/app.ts' },
      }),
      toolActivity('tool_progress', 'running', {
        toolCallId: 'concise-shell', toolName: 'workspace_shell', args: { command: 'pnpm test' },
        publicResult: { outputPreview: 'large log that belongs inside the disclosure' },
      }),
      toolActivity('tool_finished', 'failed', {
        toolCallId: 'concise-search', toolName: 'workspace_search',
        args: { path: 'src', query: 'loading' }, isError: true,
        result: { details: { error: '搜索范围不可用' } },
      }),
    ];
    const { container } = render(<FxActivityStack activities={activities} />);
    const rows = [...container.querySelectorAll('.paw-activity__row')];
    expect(rows[0]!.querySelector('.paw-activity__label')).toHaveTextContent('已读取');
    expect(rows[0]!.querySelector('.paw-activity__hint')).toHaveTextContent('src/app.ts');
    expect(rows[1]!.querySelector('.paw-activity__label')).toHaveTextContent('正在运行');
    expect(rows[1]!.querySelector('.paw-activity__hint')).toHaveTextContent('pnpm test');
    expect(rows[1]).not.toHaveTextContent('large log');
    expect(rows[2]!.querySelector('.paw-activity__label')).toHaveTextContent('搜索失败');
    expect(rows[2]!.querySelector('.paw-activity__hint')).toHaveTextContent('搜索范围不可用');
  });

  it('pauses the live indicator and clock while hidden, then resumes with real elapsed time', () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000);
    let hidden = false;
    const visibility = vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden);
    const activity = {
      ...toolActivity('tool_progress', 'running', {
        toolCallId: 'visibility-clock', toolName: 'workspace_shell', args: { command: 'pnpm test' },
      }),
      createdAtMs: 1_000, updatedAtMs: 1_000,
    };
    const view = render(<FxActivityStack activities={[activity]} />);
    const stack = view.container.querySelector('.paw-activity-stack')!;
    const signal = view.container.querySelector('.paw-activity__signal')!;
    expect(stack).toHaveAttribute('data-motion', 'active');
    expect(signal).toHaveAttribute('data-state', 'running');
    act(() => vi.advanceTimersByTime(2_000));
    const beforeHide = view.container.querySelector('.fx-meta')!.textContent;
    hidden = true;
    fireEvent(document, new Event('visibilitychange'));
    expect(stack).toHaveAttribute('data-motion', 'paused');
    act(() => vi.advanceTimersByTime(8_000));
    expect(view.container.querySelector('.fx-meta')).toHaveTextContent(beforeHide!);
    hidden = false;
    fireEvent(document, new Event('visibilitychange'));
    expect(stack).toHaveAttribute('data-motion', 'active');
    expect(view.container.querySelector('.fx-meta')).toHaveTextContent('10 秒');
    view.rerender(<FxActivityStack activities={[{ ...activity, status: 'completed', updatedAtMs: 11_000 }]} />);
    expect(view.container.querySelector('.paw-activity__signal')).toBe(signal);
    expect(signal).toHaveAttribute('data-state', 'completed');
    expect(stack).toHaveAttribute('data-motion', 'paused');
    visibility.mockRestore();
  });

  it('suspends motion outside the visible window without changing the Tool state', () => {
    let onIntersection: IntersectionObserverCallback = () => undefined;
    const disconnect = vi.fn();
    vi.stubGlobal('IntersectionObserver', class {
      constructor(callback: IntersectionObserverCallback) { onIntersection = callback; }
      observe() {}
      disconnect = disconnect;
    });
    const activity = toolActivity('tool_progress', 'running', {
      toolCallId: 'offscreen-tool', toolName: 'workspace_read', args: { path: 'src/app.ts' },
    });
    const view = render(<FxActivityStack activities={[activity]} />);
    const stack = view.container.querySelector('.paw-activity-stack')!;
    const notifyIntersection = (isIntersecting: boolean) => act(() => {
      onIntersection([{ isIntersecting } as IntersectionObserverEntry], {} as IntersectionObserver);
    });
    notifyIntersection(false);
    expect(stack).toHaveAttribute('data-motion', 'paused');
    expect(view.container.querySelector('.paw-activity')).toHaveAttribute('data-state', 'running');
    notifyIntersection(true);
    expect(stack).toHaveAttribute('data-motion', 'active');
    view.unmount();
    expect(disconnect).toHaveBeenCalledOnce();
  });

  it('estimates public reasoning but never invents Tool usage from visible result text', () => {
    const reasoning: AgentActivityProjection = {
      id: 'reasoning-receipt-estimate',
      turnId: 'turn-receipt-estimate',
      kind: 'reasoning_summary',
      status: 'completed',
      summary: '你好abcdefgh',
      payload: {
        source: 'provider_reasoning_summary',
        items: ['你好abcdefgh'],
        durationMs: 2_500,
      },
      createdAtMs: 1_000,
      updatedAtMs: 3_500,
    };
    const tool = {
      ...toolActivity('tool_finished', 'completed', {
        toolCallId: 'call-receipt-estimate',
        toolName: 'workspace_shell',
        durationMs: 1_200,
        publicResult: { outputPreview: 'abcdefghijkl' },
      }),
      createdAtMs: 1_000,
      updatedAtMs: 2_200,
    };

    const { container } = render(<FxActivityStack activities={[reasoning, tool]} />);
    const receipts = [...container.querySelectorAll<HTMLElement>('.fx-meta')];

    expect(receipts[0]).toHaveTextContent('2.5s · 约 4 token');
    expect(receipts[1]).toHaveTextContent('1.2s · 无独立统计');
    expect(receipts[1]).not.toHaveTextContent('约');
  });

  it('prefers activity-scoped real output usage over a visible-content estimate', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-real-usage',
      toolName: 'overview',
      durationMs: 800,
      usage: { input: 900, output: 37, totalTokens: 937 },
      result: { details: { ok: true, result: { summary: 'x'.repeat(400) } } },
    });

    const { container } = render(<FxActivityStack activities={[activity]} />);
    const receipt = container.querySelector('.fx-meta');

    expect(receipt).toHaveTextContent('0.8s · 937 token');
    expect(receipt).not.toHaveTextContent('约');
    expect(receipt).not.toHaveTextContent('900 token');
  });

  it('does not attribute an input-bearing Provider total to one Tool return', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-provider-input-usage',
      toolName: 'workspace_shell',
      durationMs: 800,
      usage: { input: 900, totalTokens: 937 },
      publicResult: { outputPreview: 'abcdefgh' },
    });

    const { container } = render(<FxActivityStack activities={[activity]} />);
    const receipt = container.querySelector('.fx-meta');

    expect(receipt).toHaveTextContent('0.8s · 937 token');
    expect(receipt).not.toHaveTextContent('约');
  });

  it('places the reasoning receipt in the compact status column', () => {
    const reasoning: AgentActivityProjection = {
      id: 'reasoning-compact-receipt',
      turnId: 'turn-reasoning-compact-receipt',
      kind: 'reasoning_summary',
      status: 'completed',
      summary: '已核对公开上下文',
      payload: {
        source: 'provider_reasoning_summary',
        items: ['已核对公开上下文'],
        durationMs: 1_500,
        usage: { totalTokens: 9 },
      },
      createdAtMs: 1_000,
      updatedAtMs: 2_500,
    };

    render(<ReasoningActivitySummary activities={[reasoning]} />);

    expect(screen.getByText('完成 · 1 项 · 1.5s · 9 token')).toHaveClass('agent-reasoning-feed__meta');
  });

  it('labels an old Tool receipt without usage instead of displaying zero', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-no-token-detail',
      toolName: 'overview',
      durationMs: 600,
    });

    const { container } = render(<FxActivityStack activities={[activity]} />);
    const receipt = container.querySelector('.fx-meta');

    expect(receipt).toHaveTextContent('0.6s');
    expect(receipt).toHaveTextContent('无独立统计');
    expect(receipt).not.toHaveTextContent('0 token');
  });

  it('keeps status before the non-shrinking receipt while the long hint owns flexible width', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-receipt-layout',
      toolName: 'overview',
      durationMs: 1_000,
      usage: { outputTokens: 12 },
      result: { details: { ok: true, result: { summary: '很长的可见结果'.repeat(80) } } },
    });

    const { container } = render(<FxActivityStack activities={[activity]} />);
    const row = container.querySelector('.paw-activity__row')!;
    const status = row.querySelector('.fx-pill')!;
    const receipt = row.querySelector('.fx-meta')!;

    expect(row.querySelector('.paw-activity__hint')).toBeInTheDocument();
    expect(status.compareDocumentPosition(receipt) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(receipt).toHaveTextContent('1.0s · 12 token');
  });

  it('keeps running, completed, and failed Tool receipts compact and folded', () => {
    const running = toolActivity('tool_progress', 'running', {
      toolCallId: 'call-running-receipt',
      toolName: 'workspace_shell',
      durationMs: 1_300,
      usage: { outputTokens: 5 },
      publicResult: { outputPreview: 'partial output' },
    });
    const completed = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-completed-receipt',
      toolName: 'workspace_shell',
      durationMs: 900,
      publicResult: { outputPreview: '已完成' },
    });
    const failed = toolActivity('tool_finished', 'failed', {
      toolCallId: 'call-failed-receipt',
      toolName: 'workspace_search',
      durationMs: 700,
      isError: true,
      result: { details: { error: '搜索参数超出允许范围' } },
    });

    const { container } = render(<FxActivityStack activities={[running, completed, failed]} />);
    const nodes = [...container.querySelectorAll<HTMLElement>('.paw-activity-node')];

    expect(nodes).toHaveLength(3);
    expect(nodes[0]!.querySelector('.fx-pill')).toHaveTextContent('进行中');
    expect(nodes[0]!.querySelector('.fx-meta')).toHaveTextContent('1.3s · 5 token');
    expect(nodes[1]!.querySelector('.fx-pill')).toHaveTextContent('完成');
    expect(nodes[1]!.querySelector('.fx-meta')).toHaveTextContent('0.9s · 无独立统计');
    expect(nodes[2]!.querySelector('.fx-pill')).toHaveTextContent('失败');
    expect(nodes[2]!.querySelector('.fx-meta')).toHaveTextContent('0.7s · 无独立统计');
    for (const node of nodes) {
      expect(node.querySelector('.paw-activity')).toHaveAttribute('aria-expanded', 'false');
    }
  });

  it('collapses and restores the entire FX activity list from its top control', () => {
    const first = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-collapse-all-first',
      toolName: 'workspace_shell',
      publicResult: { outputPreview: 'first result' },
    });
    const second = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-collapse-all-second',
      toolName: 'workspace_search',
      publicResult: { outputPreview: 'second result' },
    });

    const { container } = render(<FxActivityStack activities={[first, second]} />);
    const stack = screen.getByRole('group', { name: '工具与思考步骤' });
    const collapseAll = within(stack).getByRole('button', {
      name: /全部收起工具与思考步骤，共 2 项/,
    });
    const firstRow = container.querySelector('.paw-activity')!;
    const controlledId = collapseAll.getAttribute('aria-controls');
    const list = controlledId ? document.getElementById(controlledId) : null;

    expect(collapseAll).toHaveAttribute('aria-expanded', 'true');
    expect(collapseAll.compareDocumentPosition(firstRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(list).toHaveAttribute('role', 'tree');
    expect(list).toHaveAttribute('aria-hidden', 'false');

    fireEvent.click(collapseAll);

    expect(collapseAll).toHaveAttribute('aria-expanded', 'false');
    expect(collapseAll).toHaveTextContent('全部展开');
    expect(list).toHaveAttribute('aria-hidden', 'true');
    expect(list).toHaveAttribute('inert');

    fireEvent.click(collapseAll);

    expect(collapseAll).toHaveAttribute('aria-expanded', 'true');
    expect(collapseAll).toHaveTextContent('全部收起');
    expect(list).toHaveAttribute('aria-hidden', 'false');
    expect(list).not.toHaveAttribute('inert');
  });

  it('starts a long FX activity list compact without hiding its live or failure state', () => {
    const activities = Array.from({ length: 6 }, (_, index) => toolActivity(
      index === 0 ? 'tool_progress' : 'tool_finished',
      index === 0 ? 'running' : index === 1 ? 'failed' : 'completed',
      {
        toolCallId: `call-long-stack-${index}`,
        toolName: index % 2 ? 'workspace_search' : 'workspace_shell',
        ...(index === 1 ? { isError: true } : {}),
      },
    ));

    const { container } = render(<FxActivityStack activities={activities} sessionId="session-long-stack" />);
    const stack = screen.getByRole('group', { name: '工具与思考步骤' });
    const toggle = within(stack).getByRole('button', {
      name: /全部展开工具与思考步骤，共 6 项/,
    });
    const controlledId = toggle.getAttribute('aria-controls');
    const list = controlledId ? document.getElementById(controlledId) : null;

    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(within(stack).getByText('步骤进行中 · 1 项失败 · 6 个步骤')).toBeVisible();
    expect(list).toHaveAttribute('aria-hidden', 'true');
    expect(list).toHaveAttribute('inert');

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(list).toHaveAttribute('aria-hidden', 'false');
    expect(container.querySelectorAll('.paw-activity-node')).toHaveLength(6);
  });

  it('renders only authoritative bounded Tool progress as a compact meter', () => {
    const counted = {
      ...toolActivity('tool_progress', 'running', {
        toolCallId: 'call-counted-progress',
        toolName: 'knowledge',
        progress: 0.5,
      }),
      summary: '已扫描 24 / 48 段',
    };
    const invalid = {
      ...toolActivity('tool_progress', 'running', {
        toolCallId: 'call-invalid-progress',
        toolName: 'browser',
        progress: Number.NaN,
      }),
      summary: '正在检查页面',
    };
    const completedAtHalf = { ...counted, id: 'call-completed-half', status: 'completed' as const };

    const { rerender } = render(<FxActivityStack activities={[counted, invalid]} />);

    const meter = screen.getByRole('progressbar', { name: '知识库：24 / 48 段' });
    expect(meter).toHaveAttribute('aria-valuenow', '50');
    expect(meter).toHaveAttribute('aria-valuetext', '24 / 48 段');
    expect(meter).toHaveStyle({ '--paw-activity-progress': '0.5' });
    expect(screen.getByText('24 / 48 段')).toBeInTheDocument();
    expect(screen.getAllByRole('progressbar')).toHaveLength(1);

    rerender(<FxActivityStack activities={[completedAtHalf]} />);
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '50');
  });

  it('bounds an inspectable receipt before it can be copied or rendered', () => {
    const text = inspectableRawResultText({
      rows: Array.from({ length: 500 }, (_, index) => ({
        index,
        value: `large-result-${index}-${'X'.repeat(200)}`,
      })),
    }, 'json');

    expect(text.length).toBeLessThanOrEqual(24_000);
    expect(text).toContain('large-result-0');
    expect(text).toContain('[结果已截断');
    expect(text).not.toContain('large-result-499');
  });

  it('renders an interleaved tool group as an inline disclosure with the real result', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-inline-result',
      toolName: 'overview',
      result: { details: { ok: true, operation: 'status', result: { summary: '运行状态已读取' } } },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const group = container.querySelector<HTMLDetailsElement>('details.agent-activity--inline');
    expect(group).not.toBeNull();
    expect(group).not.toHaveAttribute('open');
    const summary = group!.querySelector('summary')!;
    expect(within(summary).getByText('1 项操作')).toBeInTheDocument();
    expect(within(summary).getByText('运行状态已读取')).toBeInTheDocument();
    expect(within(summary).getByText('完成')).toBeInTheDocument();
    expect(summary.querySelector('.agent-activity__inline-icon')).toBeInTheDocument();
    expect(summary.querySelector('.agent-activity__status')).not.toBeInTheDocument();
    expect(group!.querySelector('.agent-activity-row')).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: '操作记录' })).not.toBeInTheDocument();

    fireEvent.click(summary);
    expect(group).toHaveAttribute('open');
    const details = within(group!).getByRole('region', { name: '操作与思考过程' });
    expect(details).toHaveAttribute('data-bounded-scroll', 'true');
    expect(details).toHaveAttribute('tabindex', '0');
    const row = details.querySelector<HTMLDetailsElement>('.agent-activity-row');
    expect(row).not.toHaveAttribute('open');
    fireEvent.click(row!.querySelector('summary')!);
    expect(row).toHaveAttribute('open');
    expect(details).toHaveTextContent('运行状态已读取');
  });

  it('keeps the opened group and tool evidence visible across a virtualized remount', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-remounted-disclosure',
      toolName: 'overview',
      args: { section: 'runtime' },
      result: { details: { ok: true, result: { summary: '运行状态已读取' } } },
    });
    const first = render(<ActivitySummary activities={[activity]} inline />);
    const firstGroup = first.container.querySelector<HTMLDetailsElement>('details.agent-activity--inline')!;
    fireEvent.click(firstGroup.querySelector('summary')!);
    const firstRow = firstGroup.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(firstRow.querySelector('summary')!);
    expect(firstGroup).toHaveAttribute('open');
    expect(firstRow).toHaveAttribute('open');
    first.unmount();

    const second = render(<ActivitySummary activities={[activity]} inline />);
    const secondGroup = second.container.querySelector<HTMLDetailsElement>('details.agent-activity--inline')!;
    const secondRow = secondGroup.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    expect(secondGroup).toHaveAttribute('open');
    expect(secondRow).toHaveAttribute('open');
    expect(secondRow).toHaveTextContent('运行状态已读取');
    expect(secondRow).toHaveTextContent('完整返回');
  });

  it('toggles a controlled disclosure by Enter and Space without native page activation', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-keyboard-disclosure',
      toolName: 'overview',
      result: { details: { result: { summary: '运行状态已读取' } } },
    });
    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const group = container.querySelector<HTMLDetailsElement>('details.agent-activity--inline')!;
    const summary = group.querySelector<HTMLElement>('summary')!;

    fireEvent.keyDown(summary, { key: 'Enter' });
    expect(group).toHaveAttribute('open');
    fireEvent.keyDown(summary, { key: ' ' });
    expect(summary).toHaveAttribute('aria-expanded', 'false');
    const reveal = group.querySelector<HTMLElement>('.agent-smooth-reveal')!;
    expect(reveal).toHaveAttribute('aria-hidden', 'true');
    // The native details stays open for the 180ms exit so a multi-page body
    // can shrink instead of disappearing before the height transition.
    expect(group).toHaveAttribute('open');
    fireEvent.transitionEnd(reveal, { propertyName: 'height' });
    expect(group).not.toHaveAttribute('open');
  });

  it('preserves the scroll anchor and summary focus when an inline group changes height', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-anchored-disclosure',
      toolName: 'overview',
      result: { details: { result: { summary: '运行状态已读取' } } },
    });
    const { container } = render(
      <div data-testid="timeline-scrollport" style={{ maxHeight: 300, overflowY: 'auto' }}>
        <ActivitySummary activities={[activity]} inline />
      </div>,
    );
    const scrollport = screen.getByTestId('timeline-scrollport');
    Object.defineProperty(scrollport, 'scrollHeight', { configurable: true, value: 1_000 });
    Object.defineProperty(scrollport, 'clientHeight', { configurable: true, value: 300 });
    scrollport.scrollTop = 320;
    const group = container.querySelector<HTMLDetailsElement>('details.agent-activity--inline')!;
    const summary = group.querySelector<HTMLElement>('summary')!;
    expect(group).not.toHaveAttribute('open');
    vi.spyOn(summary, 'getBoundingClientRect').mockImplementation(() => {
      const top = group.hasAttribute('open')
        ? 130 + (320 - scrollport.scrollTop)
        : 180;
      return {
        bottom: top + 42,
        height: 42,
        left: 0,
        right: 600,
        top,
        width: 600,
        x: 0,
        y: top,
        toJSON: () => ({}),
      };
    });

    summary.focus();
    fireEvent.click(summary);

    expect(group).toHaveAttribute('open');
    expect(summary).toHaveAttribute('aria-expanded', 'true');
    expect(scrollport.scrollTop).toBe(270);
    expect(document.activeElement).toBe(summary);
  });

  it('keeps a large tool group compact until its summary is opened', () => {
    const activities = Array.from({ length: 34 }, (_, index) => toolActivity(
      'tool_finished',
      index === 17 ? 'failed' : 'completed',
      {
        toolCallId: `call-large-${index}`,
        toolName: index === 17 ? 'workspace_edit' : 'workspace_read',
        result: { details: index === 17
          ? { error: '当前 Todo 尚未进入执行中，写入未执行' }
          : { result: { summary: `已读取文件 ${index + 1}` } } },
      },
    ));
    const { container } = render(<ActivitySummary activities={activities} inline />);
    const group = container.querySelector<HTMLDetailsElement>('details.agent-activity--inline')!;

    expect(group).not.toHaveAttribute('open');
    expect(screen.queryByRole('dialog', { name: '操作记录' })).not.toBeInTheDocument();
    expect(group).toHaveAttribute('data-state', 'mixed');
    expect(group).toHaveTextContent('33 已完成 · 1 失败');
    fireEvent.click(group.querySelector('summary')!);
    const scrollRegion = within(group).getByRole('region', { name: '操作与思考过程' });
    expect(scrollRegion).toHaveAttribute('data-bounded-scroll', 'true');
    expect(scrollRegion.querySelectorAll('.agent-activity-row')).toHaveLength(34);
  });

  it('keeps a manually opened live group stable while status updates', () => {
    const activity = toolActivity('tool_progress', 'running', {
      toolCallId: 'call-running-disclosure',
      toolName: 'workspace_search',
      partialResult: { details: { summary: '正在检索项目内容' } },
    });
    const { container, rerender } = render(<ActivitySummary activities={[activity]} inline />);
    const group = container.querySelector<HTMLDetailsElement>('details.agent-activity--inline')!;

    expect(group).not.toHaveAttribute('open');
    fireEvent.click(group.querySelector('summary')!);
    expect(group).toHaveAttribute('open');

    rerender(
      <ActivitySummary
        activities={[{ ...activity, updatedAtMs: activity.updatedAtMs + 1_000 }]}
        inline
      />,
    );
    expect(group).toHaveAttribute('open');
    expect(within(group).getByRole('region', { name: '操作与思考过程' })).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: '正在处理' })).not.toBeInTheDocument();

    rerender(
      <ActivitySummary
        activities={[{ ...activity, status: 'completed', updatedAtMs: activity.updatedAtMs + 2_000 }]}
        inline
      />,
    );
    expect(group).toHaveAttribute('open');
    expect(within(group).getByRole('region', { name: '操作与思考过程' })).toBeInTheDocument();
  });

  it('summarizes a failed group while keeping its detailed receipt on demand', () => {
    const activity = toolActivity('tool_finished', 'failed', {
      toolCallId: 'call-failed-disclosure',
      toolName: 'workspace_search',
      isError: true,
      result: { details: { error: '搜索参数超出允许范围' } },
    });
    const { container, rerender } = render(<ActivitySummary activities={[activity]} inline />);
    const group = container.querySelector<HTMLDetailsElement>('details.agent-activity--inline')!;
    const summary = group.querySelector('summary')!;

    expect(group).not.toHaveAttribute('open');
    expect(summary).toHaveTextContent('搜索参数超出允许范围');
    expect(group).toHaveAttribute('data-state', 'mixed');
    fireEvent.click(summary);
    expect(group).toHaveAttribute('open');
    const failedToolSummary = screen.getByText('搜索项目内容').closest('summary')!;
    expect(failedToolSummary).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(failedToolSummary);
    expect(screen.getByLabelText('工具失败')).toHaveTextContent('搜索参数超出允许范围');
    expect(screen.getByLabelText('工具失败')).toHaveTextContent('失败原因');
    expect(screen.queryByRole('dialog', { name: '操作记录' })).not.toBeInTheDocument();

    rerender(<ActivitySummary activities={[{ ...activity, updatedAtMs: 3 }]} inline />);
    expect(group).toHaveAttribute('open');
  });

  it('can send an exact failed Tool receipt to Trace Agent', async () => {
    const user = userEvent.setup();
    const routes: string[] = [];
    const activity = toolActivity('tool_finished', 'failed', {
      toolCallId: 'call-trace-edit',
      toolName: 'edit',
      isError: true,
      result: { details: { error: 'resourceRevision must be sha256' } },
    });
    const { container } = render(
      <PawOsDesktopProvider openRoute={(route) => routes.push(route)} openWindow={() => undefined}>
        <ActivitySummary activities={[activity]} inline sessionId="session-trace" />
      </PawOsDesktopProvider>,
    );

    fireEvent.click(container.querySelector<HTMLDetailsElement>('details.agent-activity--inline > summary')!);
    fireEvent.click(screen.getByText('编辑文件').closest('summary')!);
    await user.click(screen.getByRole('button', { name: '交给 Trace Agent' }));

    const handoff = parseTraceAgentHandoff(routes[0].split('?', 2)[1] ?? '');
    expect(handoff).toMatchObject({
      kind: 'tool',
      sessionId: 'session-trace',
      error: 'resourceRevision must be sha256',
      refs: { toolCallId: 'call-trace-edit', toolName: 'edit' },
    });
  });

  it('shows a Pi validation message when the Tool receipt has content but empty details', () => {
    const view = publicToolResultView(toolActivity('tool_finished', 'failed', {
      toolCallId: 'call-validation-content',
      toolName: 'write',
      isError: true,
      result: {
        content: [{
          type: 'text',
          text: 'Validation failed: resourceRevision: must have required properties resourceRevision',
        }],
      },
    }));

    expect(view.error).toContain('resourceRevision');
    expect(view.error).not.toContain('没有可公开展示的错误明细');
  });

  it('renders an Act Gate refusal as an expected no-op rather than a Tool failure', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-goal-paused',
      toolName: 'edit',
      isError: false,
      governanceBlocked: true,
      summary: '工作区变更未执行：当前 Todo、Goal 或权限状态不允许执行。',
      result: {
        details: {
          ok: false,
          error: 'Act Gate blocked workspace mutation (goal_paused): 当前 Goal 已暂停，恢复后才能继续写入。',
        },
      },
    });
    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const group = container.querySelector<HTMLDetailsElement>('details.agent-activity--inline')!;
    expect(group).toHaveTextContent('1 项操作');
    expect(group).toHaveTextContent('工作区变更未执行');
    expect(group).not.toHaveTextContent('失败');
    const dialog = openInlineActivity(container);
    expect(dialog).not.toHaveTextContent('失败原因');
    expect(dialog).not.toHaveTextContent('未成功');
  });

  it('shows the exact safe coding-tool request and meaningful returned lines', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-grep-evidence',
      toolName: 'grep',
      args: {
        path: '[REDACTED_PATH]',
        pattern: 'rime_lexicon_review',
      },
      publicResult: {
        path: '…/project/rag_ime',
        pattern: 'rime_lexicon_review',
        glob: '*.py',
        limit: 100,
        context: 2,
        outputPreview: [
          'rag_ime/agent_tools.py:41:def rime_lexicon_review(...):',
          'tests/test_rime_lexicon_review.py:12:class ReviewTests:',
        ].join('\n'),
        outputTruncated: true,
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const dialog = openInlineActivity(container);
    const row = dialog.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    expect(row.querySelector('summary')).toHaveTextContent('在 …/project/rag_ime 搜索 “rime_lexicon_review”');
    expect(within(row.querySelector('summary')!).getByText('搜索文本', { selector: 'strong' })).toBeInTheDocument();
    fireEvent.click(row.querySelector('summary')!);

    const request = within(row).getByLabelText('工具调用参数');
    expect(request).toHaveTextContent('目标');
    expect(request).toHaveTextContent('…/project/rag_ime');
    expect(request).toHaveTextContent('rime_lexicon_review');
    expect(request).toHaveTextContent('*.py');
    expect(request).toHaveTextContent('100');
    expect(request).toHaveTextContent('2');
    const output = within(row).getByRole('region', { name: '搜索匹配结果' });
    expect(within(request).getByRole('button', { name: '复制参数' })).toBeInTheDocument();
    expect(output).toHaveTextContent('rag_ime/agent_tools.py:41');
    expect(output).toHaveTextContent('tests/test_rime_lexicon_review.py:12');
    expect(output).toHaveTextContent('完整结果仍由本机工具回执保留');
    expect(within(output).getByRole('button', { name: '复制结果' })).toBeInTheDocument();
  });

  it('renders Shell output as a terminal result instead of a generic result fragment', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-shell-renderer',
      toolName: 'workspace_shell',
      publicResult: {
        command: 'pnpm test',
        outputPreview: 'Tests: 12 passed\nDuration: 1.42s',
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const details = openInlineActivity(container);
    const row = details.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    const result = within(row).getByRole('region', { name: '命令输出' });
    expect(result).toHaveAttribute('data-result-kind', 'terminal');
    expect(result).toHaveTextContent('Tests: 12 passed');
  });

  it('renders historical Pi codemode source, nested calls, and final output together', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-codemode-history',
      toolName: 'codemode',
      args: { code: "const value = await read('README.md'); return value;" },
      result: {
        details: {
          calls: [
            { id: 'codemode/1', name: 'read', args: '{"path":"README.md"}', status: 'ok', durationMs: 16, cost: 0.001 },
            { id: 'codemode/2', name: 'bash', args: '{"command":"pnpm test"}', status: 'error', durationMs: 38, error: 'exit 1' },
          ],
          fullOutputPath: '/tmp/codemode-output.txt',
          nestedCallsComplete: false,
        },
        content: [{ type: 'text', text: 'Script completed\nWall time 0.12s\nOutput:\nFINAL_CODEMODE_OUTPUT' }],
      },
    });
    expect(publicToolResultView(activity).error).toBeUndefined();

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const details = openInlineActivity(container);
    const row = details.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    const result = within(row).getByRole('region', { name: '代码执行回执' });
    expect(result).toHaveAttribute('data-result-kind', 'codemode');
    expect(result).toHaveTextContent('部分恢复');
    expect(result).toHaveTextContent("const value = await read('README.md'); return value;");
    expect(result).toHaveTextContent('read');
    expect(result).toHaveTextContent('exit 1');
    expect(result).toHaveTextContent('FINAL_CODEMODE_OUTPUT');
    expect(result).toHaveTextContent('/tmp/codemode-output.txt');
  });

  it('projects the latest non-empty subagent return into the activity card', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-subagent-return',
      toolName: 'subagent',
      result: {
        details: {
          results: [
            { status: 'completed', output: '(no output)' },
            { status: 'completed', output: 'RETURN_CHECK_7F3A\n独立子 Session 已完成只读检查。' },
          ],
        },
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const group = container.querySelector<HTMLDetailsElement>('details.agent-activity--inline')!;
    expect(group).toHaveTextContent('RETURN_CHECK_7F3A');
    expect(group).not.toHaveTextContent('这条历史回执未包含可公开的调用参数或返回内容。');

    const details = openInlineActivity(container);
    const row = details.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);
    expect(within(row).getByLabelText('工具返回片段')).toHaveTextContent('RETURN_CHECK_7F3A');
    expect(within(row).getByLabelText('完整工具返回')).toBeInTheDocument();
  });

  it('keeps conversation planets and uses one fixed signal slot for compact Tool rows', () => {
    const running = toolActivity('tool_progress', 'running', {
      toolCallId: 'call-planet-running',
      toolName: 'workspace_search',
      summary: '正在检索仓库',
    });
    const settled = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-planet-settled',
      toolName: 'overview',
      result: { details: { ok: true, operation: 'status', result: { summary: '运行状态已读取' } } },
    });

    const live = render(<ActivitySummary activities={[running]} inline />);
    const liveSummary = live.container.querySelector('details.agent-activity--inline > summary')!;
    expect(liveSummary.querySelector('.paw-conv-planet[data-state="running"][data-size="md"]')).toBeInTheDocument();
    expect(
      liveSummary.querySelector('.agent-activity__inline-status .paw-conv-planet[data-state="running"]'),
    ).toBeInTheDocument();
    expect(liveSummary.querySelector('.agent-activity__inline-icon')).not.toBeInTheDocument();
    cleanup();

    const done = render(<ActivitySummary activities={[settled]} inline />);
    const doneSummary = done.container.querySelector('details.agent-activity--inline > summary')!;
    expect(doneSummary.querySelector('.agent-activity__inline-icon')).toBeInTheDocument();
    // A settled group keeps its Tool glyph in front and the same stable mark
    // tree in the pill; data-live, rather than DOM removal, stops the ring.
    const donePill = doneSummary.querySelector('.agent-activity__inline-status .paw-conv-planet[data-state="done"]')!;
    expect(donePill).toBeInTheDocument();
    expect(donePill).not.toHaveAttribute('data-live');
    expect(donePill.querySelector('.paw-conv-planet__orbit')).toBeInTheDocument();
    cleanup();

    const tree = render(<FxActivityStack activities={[running, settled]} />);
    const signals = [...tree.container.querySelectorAll('.paw-activity__signal')];
    expect(signals[0]).toHaveAttribute('data-state', 'running');
    expect(signals[1]).toHaveAttribute('data-state', 'completed');
    expect(signals[0]!.querySelector('.paw-conv-planet[data-state="running"]')).toBeInTheDocument();
    expect(signals[1]!.querySelector('.paw-conv-planet')).not.toBeInTheDocument();
    expect(signals[1]!.querySelector('.paw-activity__glyph')).toBeInTheDocument();
    expect(tree.container.querySelector('.fx-pill .paw-conv-planet')).not.toBeInTheDocument();
  });

  it('cleans wrapper tags from stored public summaries in both timeline presentations', () => {
    const activity: AgentActivityProjection = {
      id: 'stored-reasoning', turnId: 'turn-stored', kind: 'reasoning_summary', status: 'completed',
      summary: '<thinking>Preparing app</thinking>',
      payload: { source: 'provider_reasoning_summary', items: ['<thinking>Preparing app</thinking>'] },
      createdAtMs: 1, updatedAtMs: 2,
    };
    const compact = render(<ReasoningActivitySummary activities={[activity]} />);
    expect(compact.container.textContent).toContain('Preparing app');
    expect(compact.container.textContent).not.toContain('<thinking>');
    cleanup();
    const steps = render(<FxActivityStack activities={[activity]} />);
    expect(steps.container.textContent).toContain('Preparing app');
    expect(steps.container.textContent).not.toContain('<thinking>');
  });

  it('shows the thinking planet while the public reasoning summary is running', () => {
    const reasoning: AgentActivityProjection = {
      id: 'reasoning-planet-feed',
      turnId: 'turn-planet-feed',
      kind: 'reasoning_summary',
      status: 'running',
      summary: '正在核对事件顺序',
      payload: { source: 'provider_reasoning_summary', items: ['正在核对事件顺序'] },
      createdAtMs: 1,
      updatedAtMs: 2,
    };
    render(<ReasoningActivitySummary activities={[reasoning]} />);
    expect(
      screen.getByRole('button', { name: /查看 Agent 思考摘要/u }).querySelector('.paw-conv-planet[data-state="thinking"]'),
    ).toBeInTheDocument();
    cleanup();
    const tree = render(<FxActivityStack activities={[reasoning]} />);
    expect(tree.container.querySelector('.paw-activity__signal .paw-conv-planet[data-state="thinking"]')).toBeInTheDocument();
    tree.rerender(<FxActivityStack activities={[{ ...reasoning, status: 'completed' }]} />);
    expect(tree.container.querySelector('.paw-activity__signal .paw-conv-planet')).not.toBeInTheDocument();
  });

  it('marks a settled background subagent receipt with the violet completion tone', () => {
    const subagent = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-subagent-vio',
      toolName: 'subagent',
      result: { details: { results: [{ status: 'completed', output: '后台只读检查已完成。' }] } },
    });
    const ordinary = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-ordinary-ok',
      toolName: 'overview',
      result: { details: { ok: true, operation: 'status', result: { summary: '运行状态已读取' } } },
    });

    const { container } = render(<FxActivityStack activities={[subagent, ordinary]} />);

    const pills = [...container.querySelectorAll('.fx-pill')];
    expect(pills).toHaveLength(2);
    expect(pills[0]).toHaveClass('vio');
    expect(pills[0]).toHaveTextContent('后台完成');
    expect(pills[1]).toHaveClass('ok');
    expect(pills[1]).toHaveTextContent('完成');
  });

  it('explains a completed subagent with no child output instead of showing an empty receipt', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-subagent-empty-return',
      toolName: 'subagent',
      result: { results: [{ status: 'completed', output: '(no output)' }] },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    expect(container).toHaveTextContent('子进程未返回内容');
    expect(container).not.toHaveTextContent('这条历史回执未包含可公开的调用参数或返回内容。');
  });

  it('renders a read result as code with its safe file name and language', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-read-renderer',
      toolName: 'workspace_read',
      publicResult: {
        path: 'src/example.ts',
        outputPreview: 'export const ready: boolean = true;',
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const details = openInlineActivity(container);
    const row = details.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    const result = within(row).getByRole('region', { name: '文件内容：src/example.ts' });
    expect(result).toHaveAttribute('data-result-kind', 'code');
    expect(within(result).getByLabelText('src/example.ts 代码内容')).toHaveTextContent('export const ready');
  });

  it('links a workspace file result to the Files app for the current Session', () => {
    const openRoute = vi.fn();
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-read-file-link',
      toolName: 'workspace_read',
      publicResult: {
        path: 'src/example.ts',
        outputPreview: 'export const ready: boolean = true;',
      },
    });
    expect(publicToolResultView(activity)).toMatchObject({ targetPath: 'src/example.ts' });

    const { container } = render(
      <PawOsDesktopProvider openRoute={openRoute} openWindow={() => undefined}>
        <ActivitySummary activities={[activity]} inline sessionId="session-files" />
      </PawOsDesktopProvider>,
    );
    const details = openInlineActivity(container);
    const row = details.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    const link = within(row).getByRole('link', { name: '打开文件 src/example.ts' });
    expect(link).toHaveAttribute('href', '/files?session=session-files&path=src%2Fexample.ts');
    fireEvent.click(link);
    expect(openRoute).toHaveBeenCalledWith('/files?session=session-files&path=src%2Fexample.ts');
  });

  it('renders search output as individually readable matches', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-search-renderer',
      toolName: 'workspace_search',
      publicResult: {
        pattern: 'AgentTurn',
        outputPreview: [
          'src/AgentTimeline.tsx:520:export function AgentTurn()',
          'src/AgentTimeline.test.tsx:41:describe(\'AgentTurn\')',
        ].join('\n'),
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const details = openInlineActivity(container);
    const row = details.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    const result = within(row).getByRole('region', { name: '搜索匹配结果' });
    expect(result).toHaveAttribute('data-result-kind', 'matches');
    expect(within(result).getAllByRole('listitem')).toHaveLength(2);
    expect(result).toHaveTextContent('AgentTimeline.tsx:520');
  });

  it('keeps the file summary compact while exposing the complete nested receipt', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-list-renderer',
      toolName: 'workspace_list',
      result: {
        details: {
          ok: true,
          operation: 'list',
          result: {
            summary: '已读取 2 个工作区条目',
            entries: [
              { name: 'src', kind: 'directory', sourcePath: '/Users/private/project/src' },
              {
                name: 'README.md',
                kind: 'file',
                content: 'private file body',
                metadata: { cursor: 'page:2', accessToken: 'do-not-render-this-token' },
              },
            ],
          },
        },
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const details = openInlineActivity(container);
    const row = details.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    const result = within(row).getByRole('region', { name: '项目文件结果' });
    expect(result).toHaveAttribute('data-result-kind', 'files');
    expect(result).toHaveTextContent('src');
    expect(result).toHaveTextContent('README.md');
    expect(result).not.toHaveTextContent('/Users/private');
    expect(result).not.toHaveTextContent('private file body');

    const raw = within(row).getByLabelText('完整工具返回');
    fireEvent.click(within(raw).getByText('完整返回'));
    expect(raw).toHaveTextContent('/Users/private/project/src');
    expect(raw).toHaveTextContent('private file body');
    expect(raw).toHaveTextContent('page:2');
    expect(raw).toHaveTextContent('[REDACTED_SECRET]');
    expect(raw).not.toHaveTextContent('do-not-render-this-token');
  });

  it('virtualizes a long complete receipt while copying every result line', async () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-long-complete-result',
      toolName: 'custom_report',
      result: {
        rows: Array.from({ length: 320 }, (_, index) => ({
          index,
          value: `complete-result-line-${index}`,
        })),
      },
    });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const details = openInlineActivity(container);
    const row = details.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    const raw = within(row).getByLabelText('完整工具返回');
    expect(within(raw).queryByLabelText('完整工具返回内容')).not.toBeInTheDocument();
    fireEvent.click(within(raw).getByText('完整返回'));

    const viewport = within(raw).getByRole('region', { name: '完整工具返回内容' });
    expect(viewport).toHaveTextContent('complete-result-line-0');
    expect(viewport).not.toHaveTextContent('complete-result-line-319');
    fireEvent.click(within(raw).getByRole('button', { name: '复制完整返回' }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(
      expect.stringContaining('complete-result-line-319'),
    ));
  });

  it('renders a file mutation as a change receipt', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-change-renderer',
      toolName: 'workspace_edit',
      publicResult: {
        path: 'src/example.ts',
        additions: 4,
        deletions: 2,
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const details = openInlineActivity(container);
    const row = details.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    const result = within(row).getByRole('region', { name: '文件变更结果' });
    expect(result).toHaveAttribute('data-result-kind', 'change');
    expect(result).toHaveTextContent('src/example.ts');
    expect(result).toHaveTextContent('+4');
    expect(result).toHaveTextContent('−2');
  });

  it('expands an edit receipt into the structured diff reader, not a flat text wall', () => {
    const diff = [
      '--- a/src/example.ts',
      '+++ b/src/example.ts',
      '@@ -1,3 +1,4 @@',
      ' export function greet() {',
      "-  return 'hi';",
      "+  const name = 'PAW';",
      '+  return `hi ${name}`;',
      ' }',
    ].join('\n');
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-edit-diff',
      toolName: 'workspace_edit',
      publicResult: { path: 'src/example.ts' },
      result: { details: { ok: true, diff } },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const details = openInlineActivity(container);
    const row = details.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    const output = within(row).getByLabelText('工具变更差异');
    expect(output.querySelector(':scope > pre')).toBeNull();
    const preview = output.querySelector<HTMLElement>('.agent-diff-preview')!;
    expect(preview).not.toBeNull();
    expect(preview).toHaveTextContent('1 个文件 · +2 −1');
    expect(preview).toHaveTextContent('src/example.ts');
    expect(preview.querySelector('tr[data-kind="add"]')).toHaveTextContent("const name = 'PAW';");
    expect(preview.querySelector('tr[data-kind="remove"]')).toHaveTextContent("return 'hi';");
    expect(within(output).getByRole('radiogroup', { name: 'Diff 展示方式' })).toBeInTheDocument();
  });

  it('renders browser tabs as safe page cards without URL credentials or page markdown', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-browser-renderer',
      toolName: 'browser',
      result: {
        details: {
          ok: true,
          operation: 'tabs',
          result: {
            summary: '已读取 2 个浏览器标签页',
            items: [
              { title: 'React 文档', url: 'https://react.dev/reference?token=private' },
              { title: 'PAW 本地页', url: 'http://127.0.0.1:5173/agent', markdown: 'private page body' },
            ],
          },
        },
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const details = openInlineActivity(container);
    const row = details.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    const result = within(row).getByRole('region', { name: '浏览器结果' });
    expect(result).toHaveAttribute('data-result-kind', 'browser');
    expect(result).toHaveTextContent('React 文档');
    expect(result).toHaveTextContent('react.dev');
    expect(result).toHaveTextContent('PAW 本地页');
    expect(result).not.toHaveTextContent('token=private');
    expect(result).not.toHaveTextContent('private page body');
  });

  it('bounds long public output and copies exactly the visible safe fragment', async () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-bounded-output',
      toolName: 'grep',
      publicResult: {
        outputPreview: '/Users/private/project/output apiKey=secret-value --token raw-token safe-output-line\n'.repeat(700),
        outputTruncated: false,
      },
    });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const { container } = render(
      <div data-testid="output-scrollport" style={{ maxHeight: 300, overflowY: 'auto' }}>
        <ActivitySummary activities={[activity]} inline />
      </div>,
    );
    const group = openInlineActivity(container);
    const row = group.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);
    const output = within(row).getByLabelText('工具返回片段');
    const visibleOutput = within(output).getByLabelText('工具返回内容');
    const scrollport = screen.getByTestId('output-scrollport');
    scrollport.scrollTop = 180;

    expect(visibleOutput.textContent?.length).toBeLessThanOrEqual(6_000);
    expect(visibleOutput.textContent?.split('\n')).toHaveLength(40);
    expect(visibleOutput).not.toHaveTextContent('/Users/private');
    expect(visibleOutput).not.toHaveTextContent('secret-value');
    expect(visibleOutput).not.toHaveTextContent('raw-token');
    expect(output).toHaveTextContent('完整结果仍由本机工具回执保留');
    expect(visibleOutput).toHaveTextContent('apiKey=[REDACTED_SECRET]');
    expect(visibleOutput).toHaveAttribute('tabindex', '0');
    expect(visibleOutput).toHaveAttribute('role', 'region');
    fireEvent.click(within(output).getByRole('button', { name: '复制结果' }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(visibleOutput.textContent));
    expect(scrollport.scrollTop).toBe(180);
    expect(within(output).getByRole('button', { name: '已复制结果' })).toBeInTheDocument();
  });

  it('unwraps legacy managed evidence JSON without exposing its internal envelope', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-managed-evidence',
      toolName: 'grep',
      publicResult: {
        outputPreview: JSON.stringify({
          evidenceHandle: 'private-evidence-handle',
          evidenceRequest: {
            path: '/Volumes/private/project/rag_ime',
            pattern: 'todo',
            limit: 40,
          },
          evidenceSummary: 'rag_ime/agent_tools.py:81:def _todo(...):',
          evidenceBytes: 2_048,
          evidenceSha256: 'a'.repeat(64),
        }),
        outputTruncated: true,
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const group = container.querySelector<HTMLDetailsElement>('details.agent-activity--inline')!;
    expect(group).toHaveTextContent('在 rag_ime 搜索 “todo”');
    const dialog = openInlineActivity(container);
    const row = dialog.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    expect(within(row).getByLabelText('工具调用参数')).toHaveTextContent('rag_ime');
    expect(within(row).getByLabelText('工具调用参数')).toHaveTextContent('todo');
    expect(within(row).getByRole('region', { name: '搜索匹配结果' })).toHaveTextContent('agent_tools.py:81');
    expect(container).not.toHaveTextContent('evidenceHandle');
    expect(container).not.toHaveTextContent('private-evidence-handle');
    expect(container).not.toHaveTextContent('evidenceSha256');
  });

  it('keeps the timeline compact and opens full activity details in a dialog', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-compact-dialog',
      toolName: 'overview',
      result: { details: { ok: true, operation: 'status', result: { summary: '运行状态已读取' } } },
    });

    const { container } = render(<ActivitySummary activities={[activity]} />);

    expect(container.querySelector('details.agent-activity')).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /查看活动详情：操作记录/ }));
    expect(screen.getByRole('dialog', { name: '操作记录' })).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toHaveTextContent('当前状态');
  });

  it('labels the running activity preview and keeps every activity reachable in the full timeline', async () => {
    const user = userEvent.setup();
    const activities = Array.from({ length: 5 }, (_, index) => toolActivity('tool_started', 'running', {
      toolCallId: `live-window-${index + 1}`,
      toolName: 'knowledge',
      summary: `实时步骤 ${index + 1}`,
    }));
    const { container } = render(<ActivitySummary activities={activities} />);

    expect(screen.getByText('当前显示最近 3 / 共 5 项活动')).toBeInTheDocument();
    expect(screen.queryByText('实时步骤 1')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '查看全部 5 项活动' }));
    expect(screen.getByRole('dialog', { name: '正在处理' })).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toHaveTextContent('实时步骤 1');
    expect(container).toHaveTextContent('实时步骤 5');
  });

  it('keeps every public reasoning summary available from its compact strip', async () => {
    const user = userEvent.setup();
    const reasoningItems = Array.from({ length: 14 }, (_, index) => `公开推理 ${index + 1}`);
    render(<ReasoningActivitySummary activities={[{
      id: 'long-reasoning',
      turnId: 'turn-long-reasoning',
      kind: 'reasoning_summary',
      status: 'running',
      summary: '公开推理摘要',
      payload: { source: 'provider_reasoning_summary', items: reasoningItems },
      createdAtMs: 1,
      updatedAtMs: 2,
    }]} />);

    expect(screen.getByRole('button', { name: /查看 Agent 思考摘要：公开推理 14/ })).toHaveTextContent('14 项');
    await user.click(screen.getByRole('button', { name: /查看 Agent 思考摘要：公开推理 14/ }));
    const dialog = screen.getByRole('dialog', { name: '正在思考' });
    expect(dialog).toHaveTextContent('公开推理 1');
    expect(dialog).toHaveTextContent('公开推理 14');
  });

  it('labels provider turn failures as model service failures instead of tool operations', () => {
    const activity: AgentActivityProjection = {
      id: 'turn-provider-failed',
      turnId: 'turn-provider-failed',
      kind: 'turn_failed',
      status: 'failed',
      summary: '400 Error from provider (Console Go): Upstream request failed',
      payload: {
        error: '400 Error from provider (Console Go): Upstream request failed',
        retryExhausted: true,
        providerRetryAttempts: 6,
        providerRetryMaxAttempts: 6,
      },
      createdAtMs: 1,
      updatedAtMs: 2,
    };
    const { container } = render(<ActivitySummary activities={[activity]} />);

    expect(container).toHaveTextContent('模型服务请求失败');
    expect(container).not.toHaveTextContent('工具操作');
    openActivity(container);
    expect(screen.getByRole('dialog')).toHaveTextContent('已自动重试 6 次，模型服务仍未恢复；请稍后重试或切换模型。');
    expect(screen.queryByText(/Console Go|Upstream request failed/)).not.toBeInTheDocument();
  });

  it('renders the public overview capability result without raw tool material', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-overview-capabilities',
      toolName: 'overview',
      args: {
        op: 'capabilities',
        path: '/Users/private/project',
        apiKey: 'do-not-render',
        reasoning: 'private chain of thought',
      },
      isError: false,
      result: {
        content: [{
          type: 'text',
          text: '{"raw":"this serialized result must not render"}',
        }],
        details: {
          schemaVersion: 'rag-ime.agent-tool-result.v1',
          ok: true,
          tool: 'overview',
          operation: 'capabilities',
          result: {
            summary: '已连接 9 个控制中心领域',
            toolCount: 13,
            tools: [
              { id: 'overview', displayName: '控制中心概览', operations: ['status'] },
              { id: 'input', displayName: '输入法', operations: ['get_settings'] },
              { id: 'voice', displayName: '语音输入', operations: ['status'] },
            ],
            approvalGatedOperations: ['input.apply_settings', 'voice.provider_apply'],
            writePolicy: '写入操作必须经过本机确认并保存回执',
          },
        },
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} />);
    openActivity(container);

    expect(screen.getByText('当前状态')).toBeInTheDocument();
    expect(screen.getAllByText('已连接 9 个控制中心领域')).toHaveLength(2);
    expect(screen.getByText('查看可用能力')).toBeInTheDocument();
    expect(screen.getByText('13 项')).toBeInTheDocument();
    expect(screen.getByText('当前状态、输入法与词库、语音输入')).toBeInTheDocument();
    expect(screen.getByText('2 项操作')).toBeInTheDocument();
    expect(screen.getByText('写入操作必须经过本机确认并保存回执')).toBeInTheDocument();
    expect(container).not.toHaveTextContent('overview');
    expect(container).not.toHaveTextContent('do-not-render');
    expect(container).not.toHaveTextContent('/Users/private/project');
    expect(container).not.toHaveTextContent('private chain of thought');
    expect(container).not.toHaveTextContent('serialized result');
  });

  it('shows bounded status and progress fields while hiding nested metadata', () => {
    const completed = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-overview-status',
      toolName: 'overview',
      result: {
        details: {
          ok: true,
          operation: 'status',
          result: {
            summary: '发现 1 个未就绪组件',
            components: {
              inputMethod: { truncated: true },
              sidecar: { truncated: true },
              predictor: { truncated: true },
            },
            unhealthyComponents: ['predictor'],
            memory: {
              memoryBookCount: 4,
              memoryAtomCount: 21,
              pendingCompileEvents: 2,
            },
          },
        },
      },
    });
    const progress = toolActivity('tool_progress', 'running', {
      toolCallId: 'call-overview-progress',
      toolName: 'overview',
      args: { op: 'status', token: 'hidden-token' },
      partialResult: {
        content: [{ type: 'text', text: 'raw progress content' }],
        details: { summary: '正在读取控制中心状态', metadata: { path: '/Volumes/private/model' } },
      },
    });

    const { container } = render(<ActivitySummary activities={[completed, progress]} />);
    openActivity(container);

    const dialog = screen.getByRole('dialog');
    expect([...dialog.querySelectorAll('.agent-activity-row > summary strong')].map((node) => node.textContent)).toEqual([
      '当前状态',
      '当前状态',
    ]);
    expect(dialog).toHaveTextContent('2 / 3 可用');
    expect(dialog).toHaveTextContent('预测服务');
    expect(dialog).toHaveTextContent('4 条');
    expect(dialog).toHaveTextContent('21 条');
    expect(dialog).toHaveTextContent('2 条');
    expect(dialog).toHaveTextContent('正在读取控制中心状态');
    expect(dialog).toHaveTextContent('进行中');
    expect(container).not.toHaveTextContent('/Volumes/private/model');
    expect(container).not.toHaveTextContent('hidden-token');
    expect(container).not.toHaveTextContent('raw progress content');
  });

  it('renders retained tool checkpoints and the completed duration', () => {
    const activity: AgentActivityProjection = {
      ...toolActivity('tool_finished', 'completed', {
        toolCallId: 'call-progress-history',
        toolName: 'knowledge',
        result: { details: { result: { summary: '知识检索完成' } } },
        progressHistory: [
          { eventId: 'event-1', kind: 'tool_started', status: 'running', summary: '开始检索知识库', createdAtMs: 1_000 },
          { eventId: 'event-2', kind: 'tool_progress', status: 'running', summary: '已找到候选来源', createdAtMs: 2_000 },
          { eventId: 'event-3', kind: 'tool_finished', status: 'completed', summary: '知识检索完成', createdAtMs: 4_500 },
        ],
      }),
      createdAtMs: 1_000,
      updatedAtMs: 4_500,
    };

    const { container } = render(<ActivitySummary activities={[activity]} />);
    openActivity(container);

    const dialog = screen.getByRole('dialog');
    /* Durations are reported in the largest honest unit: raw milliseconds stop
       being readable above a second, and sub-second work still gets ms. */
    expect(dialog).toHaveTextContent('完成 · 3.5 秒');
    expect(dialog).toHaveTextContent('过程记录');
    expect(dialog).toHaveTextContent('+0 ms · 开始检索知识库 · 进行中');
    expect(dialog).toHaveTextContent('+1.0 秒 · 已找到候选来源 · 进行中');
    expect(dialog).toHaveTextContent('+3.5 秒 · 知识检索完成 · 完成');
  });

  it('summarizes document knowledge citations without expanding raw chunks or paths', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-document-knowledge',
      toolName: 'knowledge',
      result: {
        details: {
          ok: true,
          operation: 'search',
          result: {
            summary: '文档知识库返回 2 条引用证据',
            items: [
              {
                kbId: 'kb-paw',
                fileId: 'doc-acceptance',
                fileName: 'acceptance.md',
                content: '不应在时间线详情里展开的文档原文',
                sourcePath: '/Users/private/acceptance.md',
                citation: { startLine: 41, endLine: 57 },
              },
              {
                kbId: 'kb-paw',
                fileId: 'doc-design',
                fileName: 'design.pdf',
                content: '另一段不应展示的原文',
                citation: { page: 3 },
              },
            ],
          },
        },
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} />);
    openActivity(container);

    expect(screen.getByText('检索文档')).toBeInTheDocument();
    expect(screen.getByText('信息来源')).toBeInTheDocument();
    expect(screen.getByText('acceptance.md · 41-57 行')).toBeInTheDocument();
    expect(screen.getByText('design.pdf · 第 3 页')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'acceptance.md · 41-57 行' })).toHaveAttribute(
      'href',
      '#/knowledge?base=kb-paw&document=doc-acceptance&tab=viewer',
    );
    expect(screen.getByRole('link', { name: 'design.pdf · 第 3 页' })).toHaveAttribute(
      'href',
      '#/knowledge?base=kb-paw&document=doc-design&tab=viewer',
    );
    expect(screen.getByRole('link', { name: '打开知识库' })).toHaveAttribute('href', '#/knowledge');
    expect(container).not.toHaveTextContent('不应在时间线详情里展开');
    expect(container).not.toHaveTextContent('/Users/private');
  });

  it('renders safe generic interface entries without dumping their private fields', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-public-structure',
      toolName: 'workspace_list',
      result: {
        details: {
          ok: true,
          operation: 'list',
          result: {
            summary: '已读取 2 个工作区条目',
            entries: [
              { name: 'src', kind: 'directory', itemCount: 12, sourcePath: '/Users/private/project/src', content: 'private file body' },
              { name: 'README.md', kind: 'file' },
            ],
            nextCursor: 'cursor-public-2',
            metadata: { healthy: true, authorization: 'Bearer hidden' },
            content: { state: 'ready', resultCount: 2 },
            apiKey: 'sk-do-not-render',
            reasoning: 'private chain of thought',
          },
        },
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} />);
    openActivity(container);
    const dialog = screen.getByRole('dialog');

    expect(screen.queryByLabelText('工具公开结果')).not.toBeInTheDocument();
    expect(dialog).toHaveTextContent('已读取 2 个工作区条目');
    expect(dialog).not.toHaveTextContent('entries');
    expect(screen.getByRole('region', { name: '项目文件结果' })).toHaveTextContent('README.md');
    expect(screen.getByRole('region', { name: '项目文件结果' })).toHaveTextContent('src');
    expect(dialog).not.toHaveTextContent('cursor-public-2');
    expect(dialog).not.toHaveTextContent('/Users/private');
    expect(dialog).not.toHaveTextContent('private file body');
    expect(dialog).not.toHaveTextContent('sk-do-not-render');
    expect(dialog).not.toHaveTextContent('private chain of thought');
    expect(dialog).not.toHaveTextContent('authorization');
  });

  it('renders a memory book as a semantic expandable preview', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-memory-book',
      toolName: 'memory',
      operation: 'read',
      result: {
        book: {
          title: '输入法与 Agent 上下文',
          summary: '输入缓冲与长期记忆之间的边界。',
          tags: ['输入法', '上下文'],
          memories: [
            {
              type: 'principle',
              text: '单个词不得进入长期上下文。',
              ref: { type: 'memory_atom', id: 'atom:input-boundary' },
            },
            { type: 'decision', text: '闪电联想只读取临时缓冲。' },
          ],
        },
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const dialog = openInlineActivity(container);
    const row = dialog.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    expect(within(row).getByLabelText('《输入法与 Agent 上下文》内容')).toHaveTextContent('输入缓冲与长期记忆之间的边界。');
    expect(row).toHaveTextContent('原则');
    expect(row).toHaveTextContent('单个词不得进入长期上下文。');
    expect(within(row).getByRole('link', { name: /单个词不得进入长期上下文/ })).toHaveAttribute(
      'href',
      '#/memory?layer=atoms&id=atom%3Ainput-boundary',
    );
    expect(row).not.toHaveTextContent('结果摘要');
    expect(row).not.toHaveTextContent('读取内容');
    expect(row).not.toHaveTextContent('"memories"');
  });

  it('uses the stable ref kind before an Atom semantic kind when building memory links', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-memory-atom-ref',
      toolName: 'memory',
      operation: 'search',
      result: {
        summary: '命中 1 条当前事实',
        items: [{
          memoryId: 'atom:completion-latency',
          kind: 'preference',
          text: '输入法首候选延迟需要保持在 50ms 内。',
          ref: {
            referenceKind: 'atom',
            referenceId: 'atom:completion-latency',
          },
        }],
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const dialog = openInlineActivity(container);
    const row = dialog.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    expect(within(row).getByRole('link', { name: /输入法首候选延迟/ })).toHaveAttribute(
      'href',
      '#/memory?layer=atoms&id=atom%3Acompletion-latency',
    );
  });

  it('deep-links memory catalog entries through their stable Book reference', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-memory-catalog-ref',
      toolName: 'memory',
      operation: 'catalog',
      result: {
        summary: '查询到 1 本主题书',
        items: [{
          kind: 'book',
          bookId: 'book:input-method',
          title: '输入法产品与上下文边界',
          ref: {
            kind: 'book',
            id: 'book:input-method',
            referenceKind: 'book',
            referenceId: 'book:input-method',
          },
        }],
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const dialog = openInlineActivity(container);
    const row = dialog.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    expect(within(row).getByRole('link', { name: /输入法产品与上下文边界/ })).toHaveAttribute(
      'href',
      '#/memory?layer=books&id=book%3Ainput-method',
    );
  });

  it('renders governed timeline recall as a deep-linked semantic result', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-memory-timeline',
      toolName: 'memory',
      operation: 'search',
      result: {
        summary: '命中 1 条时间线记忆',
        items: [{
          id: 'activity-timeline:2026-07-18',
          kind: 'daily_timeline',
          text: 'CAS 切换 Codex 账号，然后继续验证记忆界面。',
          ref: { type: 'timeline', id: 'activity-timeline:2026-07-18' },
        }],
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const dialog = openInlineActivity(container);
    const row = dialog.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    expect(within(row).getByLabelText('记忆召回结果内容')).toHaveTextContent('CAS 切换 Codex 账号');
    expect(within(row).getByRole('link', { name: /CAS 切换 Codex 账号/ })).toHaveAttribute(
      'href',
      '#/memory?layer=timelines&id=activity-timeline%3A2026-07-18',
    );
  });

  it('deep-links a governed memory preview to its target Atom and Evidence', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-memory-correct-preview',
      toolName: 'memory',
      operation: 'correct_preview',
      result: {
        summary: '已生成事实更正预览',
        proposalId: 'memory-proposal:1',
        targetId: 'atom:model-size',
        proposedText: '当前使用 100M 自训练输入法模型。',
        evidenceIds: ['message:evidence-1'],
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const dialog = openInlineActivity(container);
    const row = dialog.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    expect(within(row).getByRole('link', { name: /当前使用 100M/ })).toHaveAttribute(
      'href',
      '#/memory?layer=atoms&id=atom%3Amodel-size',
    );
    expect(within(row).getByRole('link', { name: '来源证据 1' })).toHaveAttribute(
      'href',
      '#/memory?layer=evidence&id=message%3Aevidence-1',
    );
    expect(row).toHaveTextContent('尚未应用');
  });

  it('labels and deep-links the pinned Agent Role Book revision', () => {
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-role-book-get',
      toolName: 'agent_role_book',
      operation: 'get',
      result: {
        summary: '已读取角色书 revision 3',
        result: {
          revision: {
            revisionId: 'role-book-revision:3',
            revisionNumber: 3,
            status: 'active',
            changeSummary: '补充最近完成的记忆迁移工作。',
          },
          pinned: true,
        },
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const dialog = openInlineActivity(container);
    const row = dialog.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    expect(row).toHaveTextContent('伙伴记忆');
    expect(within(row).getByRole('link', { name: /补充最近完成的记忆迁移工作/ })).toHaveAttribute(
      'href',
      '#/memory?layer=role-books&id=role-book-revision%3A3',
    );
  });

  it('summarizes a Pi write_file result as a safe file name and added line count', () => {
    const writtenContent = Array.from({ length: 335 }, (_, index) => `line ${index + 1}`).join('\n');
    const activity = toolActivity('tool_finished', 'completed', {
      toolCallId: 'call-pi-write-file',
      toolName: 'write_file',
      args: {
        path: '/Users/private/project/src/report.ts',
        content: writtenContent,
      },
      result: {
        content: [{
          type: 'text',
          text: 'Successfully wrote 2908 bytes to /Users/private/project/src/report.ts',
        }],
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} />);
    openActivity(container);

    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('写入文件');
    expect(dialog).toHaveTextContent('report.ts +335');
    expect(dialog).toHaveTextContent('335 行');
    expect(dialog).toHaveTextContent('+335 / -0');
    expect(dialog).not.toHaveTextContent('/Users/private/project');
    expect(dialog).not.toHaveTextContent('Successfully wrote');

    // The written body is a concrete payload the reader may inspect, shown as
    // a bounded, redacted fragment instead of an empty change card.
    const written = within(dialog).getByLabelText('工具写入内容');
    const writtenBody = within(written).getByLabelText('写入内容正文');
    expect(writtenBody).toHaveTextContent('line 1');
    expect(writtenBody.textContent?.split('\n')).toHaveLength(40);
    expect(writtenBody).not.toHaveTextContent('line 41');
    expect(written).toHaveTextContent('完整结果仍由本机工具回执保留');
  });

  it('shows the sanitized permission failure and opens the real permission picker entry point', () => {
    const onRequestPermission = vi.fn();
    const activity = toolActivity('tool_finished', 'failed', {
      toolCallId: 'call-permission-failed',
      toolName: 'workspace_shell',
      result: {
        details: {
          ok: false,
          operation: 'run',
          result: {
            error: '工作区不在授权目录内，当前权限不足。',
            sourcePath: '/Users/private/project',
            apiKey: 'sk-do-not-render',
          },
        },
      },
    });

    const { container } = render(
      <ActivitySummary activities={[activity]} onRequestPermission={onRequestPermission} />,
    );
    openActivity(container);

    expect(screen.getByLabelText('工具失败')).toHaveTextContent('工作区不在授权目录内，当前权限不足。');
    expect(within(screen.getByLabelText('工具失败')).getByRole('button', { name: '复制错误' })).toBeInTheDocument();
    expect(container).not.toHaveTextContent('/Users/private/project');
    expect(container).not.toHaveTextContent('sk-do-not-render');
    fireEvent.click(screen.getByRole('button', { name: '请求权限' }));
    expect(onRequestPermission).toHaveBeenCalledOnce();
  });

  it('routes an approval-gated failure to the existing bound approval review', () => {
    const onOpenApproval = vi.fn();
    const activity = toolActivity('tool_finished', 'failed', {
      toolCallId: 'call-approval-failed',
      toolName: 'input',
      approvalId: 'approval-bound-1',
      payloadSha256: 'a'.repeat(64),
      preview: { title: '应用输入法设置', summary: '需要本机审批后才能应用。' },
      result: {
        details: {
          ok: false,
          operation: 'apply_settings',
          result: { error: '该操作需要本机审批后继续。' },
        },
      },
    });

    const { container } = render(
      <ActivitySummary activities={[activity]} onOpenApproval={onOpenApproval} />,
    );
    openActivity(container);

    expect(screen.getByLabelText('工具失败')).toHaveTextContent('该操作需要本机审批后继续。');
    fireEvent.click(screen.getByRole('button', { name: '去审批' }));
    expect(onOpenApproval).toHaveBeenCalledOnce();
    expect(onOpenApproval).toHaveBeenCalledWith(activity);
  });

  it('does not present an authorized Room execution bridge as an approval step', () => {
    const activity = toolActivity('tool_started', 'running', {
      toolCallId: 'call-room-policy',
      toolName: 'workspace_shell',
      approvalId: 'approval-room-policy',
      payloadSha256: 'c'.repeat(64),
      state: 'pending',
      causalMetadata: {
        roomBound: true,
        roomId: 'room-1',
        rootId: 'root-1',
        dispatchId: 'dispatch-1',
        generation: 4,
      },
    });

    const { container } = render(<ActivitySummary activities={[activity]} inline />);
    const details = openInlineActivity(container);
    const row = details.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);

    expect(row).toHaveTextContent('运行项目命令');
    expect(row).not.toHaveTextContent('权限确认');
    expect(row).not.toHaveTextContent('审批状态');
    expect(row.querySelector('.agent-activity-row__approval')).toBeNull();
  });

  it('shows a fail-closed approval inside its owning tool row', () => {
    const activity = toolActivity('tool_finished', 'failed', {
      toolCallId: 'call-owned-approval',
      toolName: 'workspace_shell',
      approvalId: 'approval-owned-1',
      automatic: true,
      result: {
        details: {
          result: {
            summary: '审批未通过，命令没有执行。',
            decisionMode: 'model',
            decisionStatus: 'failed_closed',
            approvalModelDecision: {
              decision: 'deny',
              status: 'failed_closed',
              model: 'openai-codex/gpt-5.6-luna',
              reasonCodes: ['model_timeout'],
              rationaleSummary: '审批模型未能形成可验证裁决。',
            },
          },
        },
      },
    });

    const { container } = render(
      <ActivitySummary activities={[activity]} inline />,
    );
    const details = openInlineActivity(container);
    expect(details.querySelectorAll('.agent-activity-row')).toHaveLength(1);
    const row = details.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);
    expect(row).toHaveTextContent('审批未通过，命令没有执行');
    expect(row).toHaveTextContent('Luna Max 独立判定');
    expect(row).toHaveTextContent('无法形成可验证裁决，已按拒绝处理');
    expect(row).toHaveTextContent('审批模型未能形成可验证裁决');
    expect(row).toHaveTextContent('审批模型超时');
  });

  it('shows the independent Luna approval agent without exposing human controls', () => {
    const onApprovalDecision = vi.fn();
    const activity: AgentActivityProjection = {
      id: 'approval-model-1',
      turnId: 'turn-model-approval',
      kind: 'approval_required',
      status: 'waiting',
      summary: '',
      payload: {
        approvalId: 'approval-model-1',
        payloadSha256: 'b'.repeat(64),
        decisionMode: 'model',
        automatic: true,
        approvalModelDecision: {
          status: 'pending',
          model: 'openai-codex/gpt-5.6-luna',
          receiptId: 'approval-model-decision:1',
        },
      },
      createdAtMs: 1,
      updatedAtMs: 2,
    };

    const { container } = render(
      <ActivitySummary activities={[activity]} inline onApprovalDecision={onApprovalDecision} />,
    );

    expect(container).toHaveTextContent('独立审批 Agent（Luna Max）正在评估这次操作');
    expect(screen.queryByRole('button', { name: '批准' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '拒绝' })).not.toBeInTheDocument();
    const details = openInlineActivity(container);
    const row = details.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);
    expect(row).toHaveTextContent('不读取当前 Agent 的输出或推理');
    expect(row).toHaveTextContent('无需人工操作');
    expect(row).toHaveTextContent('approval-model-decision:1');
    expect(onApprovalDecision).not.toHaveBeenCalled();
  });
  it('shows a fail-closed Luna verdict with rationale, reason, and history', () => {
    const activity: AgentActivityProjection = {
      id: 'approval-model-denied',
      turnId: 'turn-model-denied',
      kind: 'approval_resolved',
      status: 'failed',
      summary: '',
      payload: {
        approvalId: 'approval-model-denied',
        decisionMode: 'model',
        automatic: true,
        approvalModelDecision: {
          decision: 'deny',
          status: 'failed_closed',
          modelProfile: 'openai-codex/gpt-5.6-luna',
          receiptId: 'approval-model-decision:denied',
          reasonCodes: ['model_timeout', 'insufficient_evidence'],
          rationaleSummary: '审批模型未能形成可验证裁决。',
          contextKind: 'session',
          contextId: 'session-1',
          historyEntryCount: 4,
        },
      },
      createdAtMs: 1,
      updatedAtMs: 2,
    };

    const { container } = render(
      <ActivitySummary activities={[activity]} inline />,
    );

    expect(container).toHaveTextContent('独立审批 Agent（Luna Max）无法形成可验证裁决，已拒绝这次操作');
    const details = openInlineActivity(container);
    const row = details.querySelector<HTMLDetailsElement>('.agent-activity-row')!;
    fireEvent.click(row.querySelector('summary')!);
    expect(row).toHaveTextContent('已按拒绝处理');
    expect(row).toHaveTextContent('审批模型未能形成可验证裁决');
    expect(row).toHaveTextContent('审批模型超时');
    expect(row).toHaveTextContent('已参考 4 条同一 Session 审批历史');
    expect(screen.queryByRole('button', { name: '批准' })).not.toBeInTheDocument();
  });

});

function toolActivity(
  kind: 'tool_started' | 'tool_progress' | 'tool_finished',
  status: AgentActivityProjection['status'],
  payload: Record<string, unknown>,
): AgentActivityProjection {
  return {
    id: String(payload.toolCallId),
    turnId: 'turn-tool-details',
    kind,
    status,
    summary: String(payload.toolName ?? kind),
    payload,
    createdAtMs: 1,
    updatedAtMs: 2,
  };
}

function openInlineActivity(container: HTMLElement): HTMLElement {
  const group = container.querySelector<HTMLDetailsElement>('details.agent-activity--inline');
  expect(group).not.toBeNull();
  if (!group!.hasAttribute('open')) fireEvent.click(group!.querySelector('summary')!);
  expect(group).toHaveAttribute('open');
  expect(screen.queryByRole('dialog', { name: '操作记录' })).not.toBeInTheDocument();
  const details = group!.querySelector<HTMLElement>('.agent-activity__inline-timeline');
  expect(details).not.toBeNull();
  for (const row of details!.querySelectorAll<HTMLDetailsElement>('.agent-activity-row[open]')) {
    fireEvent.click(row.querySelector('summary')!);
  }
  return details!;
}

function openActivity(container: HTMLElement): void {
  const outer = container.querySelector<HTMLButtonElement>('.agent-activity');
  expect(outer).not.toBeNull();
  fireEvent.click(outer!);
  const dialog = document.querySelector<HTMLElement>('.agent-activity-dialog');
  expect(dialog).not.toBeNull();
  for (const row of dialog!.querySelectorAll<HTMLDetailsElement>('.agent-activity-row')) {
    fireEvent.click(row.querySelector('summary')!);
  }
}
