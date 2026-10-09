import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { ContextUsagePopover } from './ContextUsagePopover';
import contextUsageCss from './ContextUsagePopover.css?raw';

afterEach(() => cleanup());

describe('ContextUsagePopover', () => {
  it('shows a rejected context read instead of calling it a missing Runtime snapshot', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'agent.session.debugContext.get': () => { throw new Error('本机原始上下文调试尚未启用'); },
    } });
    render(<ControlTransportProvider transport={transport}><ContextUsagePopover sessionId="private-session" /></ControlTransportProvider>);
    await user.click(screen.getByRole('button', { name: '上下文用量' }));
    const dialog = await screen.findByRole('dialog', { name: '上下文用量' });
    expect(await within(dialog).findByText('本机原始上下文调试尚未启用')).toBeInTheDocument();
    expect(within(dialog).queryByText(/尚未收到 Runtime 上下文快照/)).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText('上下文分层占用')).not.toBeInTheDocument();
    expect(within(dialog).getByText(/各层用量未知/)).toBeInTheDocument();
    expect(within(dialog).getByText(/设置.*配置.*隐私与安全.*保存并查看本机上下文快照/)).toBeInTheDocument();
    expect(within(dialog).queryByText('0 Tokens')).not.toBeInTheDocument();
    expect(transport.requests).toHaveLength(1);
  });

  it('displays the reason of a successful unavailable read, keeping reported totals', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'agent.session.debugContext.get': { available: false, reason: 'session_not_resident' },
    } });
    render(<ControlTransportProvider transport={transport}><ContextUsagePopover sessionId="cold-session"
      telemetry={{ tokens: 4000, contextWindow: 100000, percent: 4, compactionCount: 2,
        latestCompaction: { status: 'completed', tokensBefore: 88000, estimatedTokensAfter: 12000 } }} /></ControlTransportProvider>);
    await user.click(screen.getByRole('button', { name: '上下文已用 4%' }));
    const dialog = await screen.findByRole('dialog', { name: '上下文用量' });
    expect(await within(dialog).findByText('原对话尚未载入，暂时无法查看原始上下文。')).toBeInTheDocument();
    expect(within(dialog).getByText('已用 4%')).toBeInTheDocument();
    expect(within(dialog).getByText(/约 4K.*100K.*Tokens/)).toBeInTheDocument();
    expect(within(dialog).getByText(/最近压缩.*第 2 次.*88K → 约 12K/)).toBeInTheDocument();
    expect(within(dialog).queryByLabelText('上下文分层占用')).not.toBeInTheDocument();
    expect(within(dialog).getByText(/各层用量未知/)).toBeInTheDocument();
  });

  it('keeps a reported total when the context window and layer breakdown are unavailable', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({ routes: {
      'agent.session.debugContext.get': { available: false },
    } });
    render(<ControlTransportProvider transport={transport}><ContextUsagePopover sessionId="partial-total"
      telemetry={{ tokens: 4000, contextWindow: 0, percent: null }} /></ControlTransportProvider>);
    await user.click(screen.getByRole('button', { name: '上下文用量' }));
    const dialog = await screen.findByRole('dialog', { name: '上下文用量' });
    await within(dialog).findByText(/各层用量未知/);
    expect(within(dialog).getByRole('status')).toHaveTextContent('尚未收到分层上下文快照');
    expect(within(dialog).getByText(/约 4K.*未知.*Tokens/)).toBeInTheDocument();
    expect(within(dialog).queryByLabelText('上下文分层占用')).not.toBeInTheDocument();
    expect(within(dialog).getByText(/关闭后重新打开/)).toBeInTheDocument();
  });

  it('keeps pending and failed reads compact, retaining the original draft, selection and Escape focus', async () => {
    const user = userEvent.setup();
    let rejectRead!: (error: Error) => void;
    const pending = new Promise<never>((_resolve, reject) => { rejectRead = reject; });
    const transport = new MockControlTransport({ routes: { 'agent.session.debugContext.get': () => pending } });
    render(<ControlTransportProvider transport={transport}>
      <textarea aria-label="对话草稿" />
      <ContextUsagePopover sessionId="held-context" telemetry={{ tokens: 4000, contextWindow: 100000, percent: 4 }} />
    </ControlTransportProvider>);
    const draft = screen.getByRole('textbox', { name: '对话草稿' }) as HTMLTextAreaElement;
    await user.type(draft, '公开未发送草稿');
    draft.setSelectionRange(1, 4);
    const trigger = screen.getByRole('button', { name: '上下文已用 4%' });
    await user.click(trigger);
    const dialog = await screen.findByRole('dialog', { name: '上下文用量' });
    expect(within(dialog).getByRole('status')).toHaveTextContent('正在读取上下文占用');
    expect(within(dialog).getByText(/约 4K.*100K.*Tokens/)).toBeInTheDocument();
    expect(within(dialog).queryByLabelText('上下文分层占用')).not.toBeInTheDocument();
    await act(async () => rejectRead(new Error('原读取连接中断')));
    expect(within(dialog).getByRole('status')).toHaveTextContent('原读取连接中断');
    expect(within(dialog).getByText(/各层用量未知.*关闭后重新打开/)).toBeInTheDocument();
    expect(within(dialog).queryByLabelText('上下文分层占用')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: '对话草稿' })).toBe(draft);
    expect(draft).toHaveValue('公开未发送草稿');
    expect([draft.selectionStart, draft.selectionEnd]).toEqual([1, 4]);
    expect(transport.requests).toHaveLength(1);
    expect(transport.requests[0].request).toEqual({ pathId: 'agent.session.debugContext.get', params: { sessionId: 'held-context' } });
    await user.keyboard('{Escape}');
    expect(trigger).toHaveFocus();
    expect(screen.getByRole('textbox', { name: '对话草稿' })).toBe(draft);
    expect(draft).toHaveValue('公开未发送草稿');
    expect([draft.selectionStart, draft.selectionEnd]).toEqual([1, 4]);
  });

  it('opens the Context Usage dialog from the chat composer meter', async () => {
    const user = userEvent.setup();
    const transport = new MockControlTransport({
      routes: {
        'agent.session.debugContext.get': {
          available: true,
          context: {
            systemPrompt: '<agent-profile>persona</agent-profile>\nbase system\n',
            systemPromptOptions: { contextFiles: [{ path: 'AGENTS.md', content: 'rules body' }] },
            activeTools: ['shell'],
            toolSchemas: [{ name: 'shell', description: 'run shell' }],
            modelCalls: [],
            updatedAtMs: 1,
          },
          telemetry: {
            context: { tokens: 12_000, contextWindow: 200_000, percent: 6 },
            latestCacheHitPercent: null,
            compactionCount: 2,
            latestCompaction: {
              reason: 'threshold',
              status: 'completed',
              tokensBefore: 128_000,
              estimatedTokensAfter: 29_000,
              updatedAtMs: 1,
            },
            updatedAtMs: 1,
          },
        },
      },
    });
    render(
      <ControlTransportProvider transport={transport}>
        <ContextUsagePopover
          sessionId="session-1"
          telemetry={{ tokens: 12_000, contextWindow: 200_000, percent: 6 }}
        />
      </ControlTransportProvider>,
    );

    expect(screen.getByRole('button', { name: '上下文已用 6%' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '上下文已用 6%' }));
    const dialog = await screen.findByRole('dialog', { name: '上下文用量' });
    expect(within(dialog).getByText('上下文用量')).toBeInTheDocument();
    await waitFor(() => expect(within(dialog).getByText(/已用 \d+%/)).toBeInTheDocument());
    expect(within(dialog).getByLabelText('上下文分层占用')).toBeInTheDocument();
    expect(within(dialog).getByRole('list', { name: '上下文分层占用' }).children).toHaveLength(9);
    expect(within(dialog).getByText('系统提示词')).toBeInTheDocument();
    expect(within(dialog).getAllByText('Token 未单独统计').length).toBeGreaterThan(0);
    expect(within(dialog).getByText(/总 Token 仅有整轮统计/)).toBeInTheDocument();
    expect(within(dialog).getByText(/128K → 约 29K/)).toBeInTheDocument();
  });

  it('moves focus into the dialog and restores it to the trigger after keyboard or button close', async () => {
    const user = userEvent.setup();
    render(
      <ContextUsagePopover
        sessionId="session-focus"
        telemetry={{ tokens: 4_000, contextWindow: 100_000, percent: 4 }}
      />,
    );
    const trigger = screen.getByRole('button', { name: '上下文已用 4%' });

    trigger.focus();
    await user.keyboard('{Enter}');
    expect(await screen.findByRole('button', { name: '关闭上下文用量' })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(trigger).toHaveFocus();

    await user.click(trigger);
    const close = await screen.findByRole('button', { name: '关闭上下文用量' });
    await user.click(close);
    expect(trigger).toHaveFocus();
  });

  it('sizes the portalled panel from Radix collision space and compacts its trigger in narrow composer containers', () => {
    expect(contextUsageCss).toContain('--radix-popover-content-available-width');
    expect(contextUsageCss).toMatch(/\.agent-context-usage__popover\s*\{[^}]*max-height:\s*min\(560px/s);
    expect(contextUsageCss).toContain('--radix-popover-content-available-height');
    expect(contextUsageCss).toMatch(/\.agent-context-usage__popover\s*\{[^}]*overflow:\s*hidden/s);
    expect(contextUsageCss).toMatch(/\.agent-context-usage__list\s*\{[^}]*overflow-y:\s*auto/s);
    expect(contextUsageCss).toMatch(/@container paw-composer-toolbar \(max-width: 360px\)/);
    expect(contextUsageCss).toMatch(/\.agent-context-usage__label\s*\{[^}]*clip-path:\s*inset\(50%\)/s);
    expect(contextUsageCss).not.toContain('repeating-linear-gradient');
  });
});
