import { CompactActivityContext } from './CompactActivityContext';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { UiAgentBlock, UiAgentMessage } from '@/contracts/ui-events';
import { PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import { messageWithWorkspaceContext } from '@/paw-os/apps/workspace-draft';
import { agentEventFixture } from '@/test/fixtures/events';
import { StubControlTransport } from '@/test/stub-control-transport';
import { useAgentLiveStore } from '../state/live-store';
import {
  AgentTimeline,
  AgentTurn,
  activityDisplayRuns,
  agentDeliveryFeedback,
  agentTurnMarkerKind,
  initialAgentResponseTurnId,
  interleavedTurnEntries,
  projectUserMessageBlocks,
  visibleAssistantMessages,
  visibleAgentTurnIds,
} from './AgentTimeline';
import { AgentBlock, AgentBlocks, MarkdownBody } from './BlockRenderer';
import { agentRendererPolicy, TRUSTED_AGENT_RENDERERS } from './renderer-registry';
import { resetAgentTurnDisclosureOverrides } from './AgentTurnWorkDisclosure';
import { resetActivityDisclosureOverrides } from './ActivitySummary';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  resetActivityDisclosureOverrides();
  resetAgentTurnDisclosureOverrides();
  useAgentLiveStore.getState().clear('session-failed-snapshot');
  useAgentLiveStore.getState().clear('session-1');
  useAgentLiveStore.getState().clear('session-empty-timeline');
});

describe('Agent chat rendering', () => {
  it.each([
    ['tool_started', { toolCallId: 'first-tool', toolName: 'read', args: { path: 'README.md' } }],
    ['text_delta', { delta: '已收到' }],
    ['turn_completed', { status: 'completed' }],
    ['turn_failed', { message: '请求失败' }],
  ])('hands immediate feedback off when %s arrives', (eventType, payload) => {
    useAgentLiveStore.getState().appendOptimistic('session-1', {
      clientMessageId: 'first-response', text: '开始', nowMs: 1, turnId: 'turn-1',
    });
    const projection = () => useAgentLiveStore.getState().projections['session-1'];
    expect(initialAgentResponseTurnId(projection())).toBe('turn-1');
    const view = render(<AgentTurn sessionId="session-1" turnId="turn-1" showWorkingIndicator={false} onApprovalDecision={() => {}} />);
    expect(screen.queryByText(/Thinking/)).not.toBeInTheDocument();
    useAgentLiveStore.getState().applyEvents('session-1', [agentEventFixture(1, eventType as string, payload as Record<string, unknown>)]);
    expect(initialAgentResponseTurnId(projection())).toBe('');
    view.unmount();
  });

  it('does not claim Thinking during uncertain admission or cancellation', () => {
    useAgentLiveStore.getState().appendOptimistic('session-1', { clientMessageId: 'uncertain', text: '开始', nowMs: 1 });
    const projection = useAgentLiveStore.getState().projections['session-1']!;
    expect(initialAgentResponseTurnId({ ...projection, status: 'aborting' })).toBe('');
    const message = projection.messagesById['local:uncertain']!;
    expect(initialAgentResponseTurnId({ ...projection, messagesById: { ...projection.messagesById, [message.id]: { ...message, admissionState: 'unresolved' } } })).toBe('');
  });

  it('folds only completed embedded history, keeps failures visible, and expands the original sequence', () => {
    useAgentLiveStore.getState().hydrateSnapshot('session-1', { messages: [userMessage('session-1', 'turn-1')], liveEvents: [], lastSequence: 0, resumeToken: '', status: 'idle' });
    let sequence = 0;
    const events = [];
    for (let i = 0; i < 4; i++) {
      events.push(agentEventFixture(++sequence, 'reasoning_summary', { requestId: `r${i}`, summary: `分析阶段${i}`, items: [`分析阶段${i}`], source: 'provider_reasoning_summary', state: 'completed' }));
      events.push(agentEventFixture(++sequence, 'tool_started', { toolCallId: `t${i}`, toolName: 'read', args: { path: `file-${i}` } }));
      events.push(agentEventFixture(++sequence, 'tool_finished', { toolCallId: `t${i}`, toolName: 'read', isError: i === 1, publicResult: { summary: i === 1 ? '文件读取失败' : `读取完成${i}` } }));
    }
    useAgentLiveStore.getState().applyEvents('session-1', events);
    const view = render(<CompactActivityContext.Provider value><AgentTurn sessionId="session-1" turnId="turn-1" onApprovalDecision={() => {}} /></CompactActivityContext.Provider>);
    expect(screen.queryByRole('button', { name: '查看 Agent 思考摘要：分析阶段0' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看 Agent 思考摘要：分析阶段3' })).toBeVisible();
    const disclosures = view.container.querySelectorAll('.agent-compact-history > summary');
    expect(disclosures.length).toBeGreaterThan(0);
    fireEvent.click(disclosures[0]!);
    expect(screen.getByRole('button', { name: '查看 Agent 思考摘要：分析阶段0' })).toBeVisible();
    // The opt-in does not change the full Session transcript.
    view.rerender(<AgentTurn sessionId="session-1" turnId="turn-1" onApprovalDecision={() => {}} />);
    expect(view.container.querySelector('.agent-compact-history')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看 Agent 思考摘要：分析阶段0' })).toBeVisible();
  });


  it('explains an authoritative empty Session and points to the composer', () => {
    render(
      <AgentTimeline
        modelSelectionAvailable
        onApprovalDecision={() => {}}
        onRetryTurn={() => false}
        onSwitchModel={() => {}}
        sessionId="session-empty-timeline"
      />,
    );

    const empty = screen.getByRole('status', { name: '空 Session' });
    expect(empty).toHaveTextContent('还没有消息');
    expect(empty).toHaveTextContent('在下方输入第一条消息');
  });

  it('keeps a Provider attempt failure in evidence without rendering it as a second answer', () => {
    const failed = {
      ...assistantMessage('session-1', 'turn-1', '模型服务未能生成最终回复。请继续当前对话，或切换模型后继续。', 1),
      id: 'attempt-failed',
      status: 'failed' as const,
      blocks: [
        ...assistantMessage('session-1', 'turn-1', '模型服务未能生成最终回复。请继续当前对话，或切换模型后继续。', 1).blocks,
        {
          id: 'attempt-failed:error',
          type: 'error' as const,
          status: 'failed' as const,
          presentationKind: 'error' as const,
          data: { message: 'fetch failed' },
        },
      ],
    };
    const final = assistantMessage('session-1', 'turn-1', '目前缺少可访问的经营数据源。', 2);

    expect(visibleAssistantMessages([failed, final]).map((message) => message.id)).toEqual([final.id]);
  });

  it('projects only the customer request from a vertical App instruction envelope', () => {
    const original = userMessage('session-1', 'turn-1').blocks.map((block) => ({
      ...block,
      data: {
        ...block.data,
        text: [
          '请加载并严格遵循 `zhanggui-wenshu` Skill。',
          '不得假定本机存在某个数据库或项目。',
          '',
          '用户请求：本月销售额和上月相比怎样？',
        ].join('\n'),
      },
    }));

    const projected = projectUserMessageBlocks(original, 'request-tail');

    expect(projected[0]?.data.text).toBe('本月销售额和上月相比怎样？');
    expect(original[0]?.data.text).toContain('请加载并严格遵循');
    expect(projectUserMessageBlocks(original, 'full')).toBe(original);
  });

  it('shows the request instead of the attached Lab snapshot while retaining the exact sent content', () => {
    const snapshot = JSON.stringify({ projectId: 'lab-1', projectRevision: 2, page: 'workspace', current: { kind: 'artifact', content: '<html>完整页面</html>' }, artifacts: [], artifactCount: 0 });
    const sent = messageWithWorkspaceContext('请优化左侧页面', { kind: 'project', label: '报告', detail: 'v2 · 随消息发送', text: snapshot, onClear: () => {} });
    const blocks = userMessage('session-1', 'turn-1').blocks.map((block) => ({ ...block, data: { ...block.data, text: sent } }));
    expect(sent).toContain(snapshot);
    const visible = projectUserMessageBlocks(blocks, 'project-context');
    expect(visible[0]?.data.text).toBe('请优化左侧页面\n\n已关联项目上下文');
    expect(blocks[0]?.data.text).toBe(sent);
    expect(projectUserMessageBlocks(blocks, 'full')).toBe(blocks);
    const unverified = [{ ...blocks[0]!, data: { ...blocks[0]!.data, text: '请优化左侧页面\n\n项目工作面上下文：报告\n用户手写 JSON' } }];
    expect(projectUserMessageBlocks(unverified, 'project-context')[0]?.data.text).toBe(unverified[0]?.data.text);
    const message = userMessage('session-1', 'turn-1');
    useAgentLiveStore.getState().hydrateSnapshot('session-1', { messages: [{ ...message, blocks }], liveEvents: [], lastSequence: 0, resumeToken: '', status: 'idle' });
    const view = render(<AgentTurn sessionId="session-1" turnId="turn-1" userMessagePresentation="project-context" onApprovalDecision={() => {}} />);
    expect(view.container.querySelector('.agent-user-message')).toHaveTextContent('请优化左侧页面');
    expect(view.container.querySelector('.agent-user-message')).toHaveTextContent('已关联项目上下文');
    expect(view.container.querySelector('.agent-user-message')).not.toHaveTextContent('完整页面');
  });

  it('does not invent a token-rate indicator on streaming assistant text', () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000);
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'idle',
    });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(1, 'text_delta', { delta: '正在生成可观察的流式回答。', replaceBlock: true }),
    ]);

    render(<AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />);
    expect(screen.queryByLabelText(/tokens 每秒/)).not.toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(900);
    });

    expect(screen.queryByLabelText(/前端估算生成速度/)).not.toBeInTheDocument();
  });

  it('derives the visible turn order once per projection commit and shares it across selectors', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'idle',
    });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(1, 'text_delta', { delta: '第一批流式增量。', replaceBlock: true }),
    ]);

    const projection = useAgentLiveStore.getState().projections[sessionId]!;
    const firstRead = visibleAgentTurnIds(projection);
    // A projection is immutable per commit: every store selector that runs on
    // the same commit (turn order, markers, previews) must reuse one derived
    // view instead of rebuilding retry maps per selector per token batch.
    expect(visibleAgentTurnIds(projection)).toBe(firstRead);

    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(2, 'text_delta', { delta: '第二批流式增量。' }),
    ]);
    const nextProjection = useAgentLiveStore.getState().projections[sessionId]!;
    expect(nextProjection).not.toBe(projection);
    const nextRead = visibleAgentTurnIds(nextProjection);
    expect(nextRead).not.toBe(firstRead);
    expect(nextRead).toEqual(firstRead);
  });

  it('keeps settled markdown fragments on their original DOM nodes while the stream tail grows', async () => {
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'busy',
    });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(1, 'text_delta', {
        delta: '稳定的第一段结论。\n\n第二段正在生成',
        replaceBlock: true,
      }),
    ]);
    render(<AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />);

    const settledParagraph = screen.getByText('稳定的第一段结论。');
    expect(document.querySelector('.agent-assistant-message .agent-blocks'))
      .toHaveAttribute('data-has-stream-tail');

    act(() => void useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(2, 'text_delta', { delta: '，补充更多细节' }),
    ]));
    // The settled fragment must not remount while the active tail re-parses:
    // a replaced DOM node repaints settled prose and makes the reader's
    // anchor flicker on every token batch.
    expect(screen.getByText('稳定的第一段结论。')).toBe(settledParagraph);

    act(() => void useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(3, 'text_delta', { delta: '。\n\n第三段总结开始' }),
    ]));
    expect(screen.getByText('稳定的第一段结论。')).toBe(settledParagraph);
    // Appended deltas ride the safe-text release scheduler, so paragraph two
    // finishes (and gets promoted to a frozen chunk) a few frames later.
    const promotedParagraph = await screen.findByText('第二段正在生成，补充更多细节。');

    act(() => void useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(4, 'text_delta', { delta: '，仍在流式续写' }),
    ]));
    // Boundary advancement promotes paragraph two into its own stable
    // fragment exactly once; later tail growth may not remount it again.
    expect(screen.getByText('稳定的第一段结论。')).toBe(settledParagraph);
    expect(screen.getByText('第二段正在生成，补充更多细节。')).toBe(promotedParagraph);
    expect(document.querySelector('.agent-markdown [data-stream-tail]')).not.toBeNull();
  });

  it('shows the observable Steer delivery lifecycle instead of a static badge', () => {
    expect(agentDeliveryFeedback('steer', 'sending', 'queued')).toBe('正在发送干预');
    expect(agentDeliveryFeedback('steer', 'accepted', 'queued')).toBe('已接收，正在切换当前执行');
    expect(agentDeliveryFeedback('steer', 'applied', 'completed')).toBe('新指令已生效');
    expect(agentDeliveryFeedback('steer', undefined, 'completed')).toBe('新指令已生效');
    expect(agentDeliveryFeedback('steer', 'accepted', 'failed')).toBe('未能确认干预是否已接收');
  });

  it('uses message side as the visual identity in a one-to-one FX Session', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    const createdAtMs = Date.UTC(2026, 7, 24, 0, 48);
    const message = {
      ...userMessage(sessionId, turnId),
      id: 'user-message-with-audit-time',
      createdAtMs,
      completedAtMs: createdAtMs,
    };
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [message],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'idle',
    });

    render(<AgentTurn presentation="fx" sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />);

    const shell = document.querySelector<HTMLElement>('[data-agent-message-id="user-message-with-audit-time"]')!;
    expect(shell).toBeInTheDocument();
    expect(shell.querySelector('.fx-user-meta')).not.toBeInTheDocument();
    const auditTime = shell.querySelector('time');
    expect(auditTime).toHaveClass('sr-only');
    expect(auditTime).toHaveAttribute('dateTime', new Date(createdAtMs).toISOString());
    expect(auditTime).toHaveTextContent('用户消息');
  });

  it('uses authoritative event sequence before message clock drift', () => {
    const before = { ...assistantMessage('session-1', 'turn-1', '调用前', 120), timelineSequence: 8 };
    const after = { ...assistantMessage('session-1', 'turn-1', '调用后', 80), id: 'turn-1:assistant:segment:10', timelineSequence: 10 };
    const activity = {
      id: 'same-time-tool',
      turnId: 'turn-1',
      kind: 'tool_finished',
      status: 'completed' as const,
      summary: '工具完成',
      payload: { toolName: 'overview' },
      createdAtMs: 100,
      updatedAtMs: 100,
      timelineSequence: 9,
    };

    const entries = interleavedTurnEntries([after, before], [activity]);

    expect(entries.map((entry) => entry.kind)).toEqual(['message', 'activity-group', 'message']);
    expect(entries[0].kind === 'message' ? entries[0].message.blocks[0]?.data.text : '').toBe('调用前');
    expect(entries[2].kind === 'message' ? entries[2].message.blocks[0]?.data.text : '').toBe('调用后');
  });

  it('uses assistant text as the boundary between bounded reasoning and Tool blocks', () => {
    const before = { ...assistantMessage('session-1', 'turn-1', '先说明第一步。', 20), timelineSequence: 3 };
    const after = {
      ...assistantMessage('session-1', 'turn-1', '第一步完成，继续处理。', 60),
      id: 'turn-1:assistant:segment:7',
      timelineSequence: 7,
    };
    const activity = (
      id: string,
      kind: 'reasoning_summary' | 'tool_finished',
      timelineSequence: number,
    ) => ({
      id,
      turnId: 'turn-1',
      kind,
      status: 'completed' as const,
      summary: id,
      payload: kind === 'reasoning_summary'
        ? { source: 'provider_reasoning_summary', items: [id] }
        : { toolName: 'overview' },
      createdAtMs: timelineSequence * 10,
      updatedAtMs: timelineSequence * 10,
      timelineSequence,
    });

    const entries = interleavedTurnEntries(
      [after, before],
      [
        activity('reasoning-before', 'reasoning_summary', 1),
        activity('tool-before', 'tool_finished', 2),
        activity('reasoning-middle', 'reasoning_summary', 4),
        activity('tool-middle', 'tool_finished', 5),
        activity('tool-after', 'tool_finished', 8),
      ],
    );

    expect(entries.map((entry) => entry.kind)).toEqual([
      'activity-group',
      'message',
      'activity-group',
      'message',
      'activity-group',
    ]);
    expect(entries.flatMap((entry) => entry.kind === 'activity-group' ? [entry.activities.map((item) => item.id)] : [])).toEqual([
      ['reasoning-before', 'tool-before'],
      ['reasoning-middle', 'tool-middle'],
      ['tool-after'],
    ]);
  });

  it('keeps Tool, reasoning, and compaction disclosures in Runtime order', () => {
    const activity = (
      id: string,
      kind: 'tool_finished' | 'reasoning_summary' | 'context_compaction',
      timelineSequence: number,
    ) => ({
      id,
      turnId: 'turn-1',
      kind,
      status: 'completed' as const,
      summary: id,
      payload: kind === 'tool_finished' ? { toolName: id } : {},
      createdAtMs: timelineSequence * 10,
      updatedAtMs: timelineSequence * 10,
      timelineSequence,
    });

    const runs = activityDisplayRuns([
      activity('tool-before', 'tool_finished', 1),
      activity('reasoning', 'reasoning_summary', 2),
      activity('compaction', 'context_compaction', 3),
      activity('tool-after', 'tool_finished', 4),
    ]);

    expect(runs.map((run) => run.kind)).toEqual(['ordinary', 'reasoning', 'compaction', 'ordinary']);
    expect(runs.flatMap((run) => run.kind === 'compaction'
      ? [run.activity.id]
      : run.activities.map((item) => item.id))).toEqual([
      'tool-before',
      'reasoning',
      'compaction',
      'tool-after',
    ]);
  });

  it('renders one image prompt before its reasoning when snapshot media ids differ', () => {
    const sessionId = 'session-1';
    const question = '收起所有工具和思考';
    const transcriptUser = {
      ...userMessage(sessionId, 'history:pi-user-with-image'),
      id: 'pi-user-with-image',
      attachments: ['transcript-media-copy'],
      blocks: [
        {
          id: 'pi-user-with-image:text',
          type: 'text' as const,
          status: 'completed' as const,
          presentationKind: 'markdown',
          data: { text: question },
        },
        {
          id: 'pi-user-with-image:image',
          type: 'image' as const,
          status: 'completed' as const,
          presentationKind: 'image',
          data: { mediaId: 'transcript-media-copy', receiptUrl: '/media/transcript' },
        },
      ],
      createdAtMs: 1_000,
      completedAtMs: 1_000,
    };
    const replayUser = {
      ...transcriptUser,
      id: 'event-user-with-image',
      turnId: 'turn-with-image',
      clientMessageId: 'web-image-prompt',
      attachments: ['uploaded-media'],
      blocks: [
        { ...transcriptUser.blocks[0]!, id: 'event-user-with-image:text' },
        {
          ...transcriptUser.blocks[1]!,
          id: 'event-user-with-image:image',
          data: { mediaId: 'uploaded-media', receiptUrl: '/media/uploaded' },
        },
      ],
    };
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [transcriptUser],
      liveEvents: [
        {
          schemaVersion: 'rag-ime.agent-event.v1',
          eventId: `${sessionId}:41`,
          sessionId,
          turnId: 'turn-with-image',
          sequence: 41,
          createdAtMs: 1_000,
          eventType: 'message_completed',
          payload: { clientMessageId: 'web-image-prompt', message: replayUser },
          resumeToken: `${sessionId}:41`,
        },
        {
          ...agentEventFixture(42, 'reasoning_summary', {
            requestId: 'reasoning-after-image',
            summary: '正在处理图片消息',
            source: 'provider_reasoning_summary',
            state: 'completed',
          }),
          sessionId,
          turnId: 'turn-with-image',
          createdAtMs: 1_010,
          resumeToken: `${sessionId}:42`,
        },
      ],
      lastSequence: 42,
      resumeToken: `${sessionId}:42`,
      status: 'responding',
    });
    const projection = useAgentLiveStore.getState().projections[sessionId]!;
    const turnIds = visibleAgentTurnIds(projection);
    const { container } = render(
      <>{turnIds.map((turnId) => (
        <AgentTurn
          key={turnId}
          presentation="fx"
          sessionId={sessionId}
          turnId={turnId}
          onApprovalDecision={() => {}}
        />
      ))}</>,
    );

    expect(turnIds).toEqual(['history:pi-user-with-image']);
    expect(screen.getAllByText(question)).toHaveLength(1);
    expect(container.querySelectorAll('.paw-user-step')).toHaveLength(1);
    expect(screen.getAllByText('1 个附件')).toHaveLength(1);
    const user = container.querySelector('.paw-user-step')!;
    const reasoning = screen.getByText('正在处理图片消息').closest('.paw-activity-node')!;
    expect(user.compareDocumentPosition(reasoning) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('renders autocompact as one event-driven timeline step that settles in place', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'busy',
    });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(1, 'compaction_started', {
        reason: 'threshold',
        tokensBefore: 120_000,
      }),
    ]);

    render(<AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />);

    const running = screen.getByRole('status', { name: 'Autocompact 自动压缩中' });
    expect(running).toHaveAttribute('data-kind', 'autocompact');
    expect(running).toHaveAttribute('data-state', 'running');
    expect(running.querySelectorAll('.agent-compaction-notice__fold > i')).toHaveLength(3);
    expect(running).toHaveTextContent('达到上下文阈值，正在生成可继续对话的摘要');

    act(() => void useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(2, 'compaction_completed', {
        reason: 'threshold',
        tokensBefore: 120_000,
        estimatedTokensAfter: 24_000,
      }),
    ]));

    const completed = screen.getByRole('status', { name: 'Autocompact 自动压缩完成' });
    expect(completed).toBe(running);
    expect(completed).toHaveAttribute('data-state', 'completed');
    expect(completed).toHaveTextContent('120K → 约 24.0K');
  });

  it('restores Provider reasoning before the matching durable assistant body', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    const assistant = assistantMessage(sessionId, turnId, '最终正文', 100);
    const reasoning = {
      ...agentEventFixture(1, 'reasoning_summary', {
        requestId: `reasoning:${assistant.id}:0`,
        sourceMessageId: assistant.id,
        summary: '先整理回答结构',
        items: ['先整理回答结构'],
        source: 'provider_reasoning_summary',
        state: 'completed',
      }),
      createdAtMs: assistant.createdAtMs,
    };
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId), assistant],
      liveEvents: [reasoning],
      lastSequence: 1,
      resumeToken: `${sessionId}:1`,
      status: 'idle',
    });

    const { container } = render(
      <AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />,
    );

    const entries = [...container.querySelectorAll<HTMLElement>('[data-timeline-kind]')];
    expect(entries.map((entry) => entry.dataset.timelineKind)).toEqual(['activity', 'message']);
    expect(entries[0]).toHaveTextContent('先整理回答结构');
    expect(entries[1]).toHaveTextContent('最终正文');
  });

  it('renders assistant text and tool results in their real event order', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'idle',
    });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(1, 'text_delta', { delta: '我先读取运行状态。', replaceBlock: true }),
      agentEventFixture(2, 'message_completed', {
        message: assistantMessage(sessionId, turnId, '我先读取运行状态。', 10),
      }),
      agentEventFixture(3, 'tool_started', {
        toolCallId: 'call-interleaved-overview',
        toolName: 'overview',
      }),
      agentEventFixture(4, 'tool_finished', {
        toolCallId: 'call-interleaved-overview',
        toolName: 'overview',
        result: { details: { ok: true, operation: 'status', result: { summary: '运行状态正常' } } },
      }),
      agentEventFixture(5, 'text_delta', { delta: '读取完成，当前运行正常。', replaceBlock: true }),
      agentEventFixture(6, 'message_completed', {
        message: assistantMessage(sessionId, turnId, '读取完成，当前运行正常。', 50),
      }),
      agentEventFixture(7, 'turn_completed', { status: 'completed' }),
    ]);

    const { container } = render(
      <AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />,
    );

    const entries = [...container.querySelectorAll<HTMLElement>('[data-timeline-kind]')];
    expect(entries.map((entry) => entry.dataset.timelineKind)).toEqual(['message', 'activity', 'message']);
    expect(entries[0]).toHaveTextContent('我先读取运行状态。');
    expect(entries[1]).toHaveTextContent('运行状态正常');
    expect(entries[2]).toHaveTextContent('读取完成，当前运行正常。');
    expect(entries[0]).not.toHaveTextContent('读取完成');
  });

  it('keeps an in-flight Steer message at its exact place in the turn timeline', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'busy',
    });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(1, 'tool_finished', {
        toolCallId: 'call-before-steer',
        toolName: 'overview',
        result: { details: { ok: true, result: { summary: '插话前的工具结果' } } },
      }),
    ]);
    useAgentLiveStore.getState().appendOptimistic(sessionId, {
      clientMessageId: 'client-steer-order',
      text: 'ZDO',
      nowMs: 25,
      turnId,
      delivery: 'steer',
    });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      {
        ...agentEventFixture(2, 'tool_finished', {
          toolCallId: 'call-after-steer',
          toolName: 'overview',
          result: { details: { ok: true, result: { summary: '插话后的工具结果' } } },
        }),
        createdAtMs: 30,
      },
      agentEventFixture(3, 'message_completed', {
        message: assistantMessage(sessionId, turnId, '最终回复', 40),
      }),
      agentEventFixture(4, 'turn_completed', { status: 'completed' }),
    ]);

    render(
      <AgentTurn
        presentation="fx"
        sessionId={sessionId}
        turnId={turnId}
        onApprovalDecision={() => {}}
      />,
    );

    const collapsedSteer = document.querySelector<HTMLElement>('[data-timeline-kind="user-message"]');
    expect(collapsedSteer).toHaveTextContent('ZDO');
    expect(collapsedSteer?.closest('[inert]')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /展开 .* 个步骤/ }));

    const entries = [...document.querySelectorAll<HTMLElement>('[data-timeline-kind]')];
    expect(entries.map((entry) => entry.dataset.timelineKind)).toEqual([
      'activity',
      'user-message',
      'activity',
      'message',
    ]);
    expect(entries[1]).toHaveTextContent('ZDO');
  });

  it('collapses settled FX work to the explicit final response and restores every layer on demand', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    const first = {
      ...assistantMessage(sessionId, turnId, '我先读取运行状态。', 10),
      id: 'turn-1:assistant:step',
      blocks: [{
        ...assistantMessage(sessionId, turnId, '', 10).blocks[0]!,
        id: 'turn-1:assistant:step:text',
        data: { text: '我先读取运行状态。' },
      }],
    };
    const final = {
      ...assistantMessage(sessionId, turnId, '读取完成，当前运行正常。', 50),
      id: 'turn-1:assistant:final',
      blocks: [{
        ...assistantMessage(sessionId, turnId, '', 50).blocks[0]!,
        id: 'turn-1:assistant:final:text',
        data: { text: '读取完成，当前运行正常。' },
      }],
    };
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'idle',
    });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(1, 'message_completed', { message: first }),
      agentEventFixture(2, 'tool_finished', {
        toolCallId: 'call-settled-overview',
        toolName: 'overview',
        result: { details: { ok: true, operation: 'status', result: { summary: '运行状态正常' } } },
      }),
      agentEventFixture(3, 'message_completed', { message: final }),
      agentEventFixture(4, 'turn_completed', { status: 'completed' }),
    ]);

    const view = render(
      <AgentTurn presentation="fx" sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />,
    );

    // Message side is the fx identity (UR-075): the repeated "Agent/状态"
    // caption row must not exist in the DOM, not merely be hidden by CSS.
    expect(document.querySelector('.agent-assistant-turn__body > header')).toBeNull();

    const toggle = screen.getByRole('button', { name: /展开 2 个步骤/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveTextContent('2 个步骤');
    expect(toggle).toHaveTextContent('1 个工具');
    expect(screen.getByText('读取完成，当前运行正常。')).toBeInTheDocument();
    expect(screen.queryByText('我先读取运行状态。')).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('我先读取运行状态。')).toBeInTheDocument();
    const tree = screen.getByRole('tree', { name: '本轮步骤' });
    expect(within(tree).getAllByRole('treeitem')).toHaveLength(2);
    fireEvent.keyDown(toggle, { key: ' ' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.keyDown(toggle, { key: 'Enter' });
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const activityToggle = document.querySelector<HTMLButtonElement>('.paw-activity')!;
    expect(activityToggle).toBeInTheDocument();
    fireEvent.click(activityToggle);
    expect(activityToggle).toHaveAttribute('aria-expanded', 'true');
    expect(activityToggle).toHaveAttribute('aria-controls');
    const activityDetail = document.querySelector<HTMLElement>('.paw-activity__detail')!;
    expect(activityDetail).toHaveAttribute('role', 'region');
    expect(activityDetail).not.toHaveAttribute('hidden');
    expect(activityDetail.querySelector('.agent-activity-row > summary')).toHaveAttribute('hidden');
    expect(activityDetail).toHaveTextContent('运行状态正常');
    fireEvent.keyDown(activityToggle, { key: ' ' });
    expect(activityToggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.keyDown(activityToggle, { key: 'Enter' });
    expect(activityToggle).toHaveAttribute('aria-expanded', 'true');

    view.unmount();
    render(<AgentTurn presentation="fx" sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />);
    expect(screen.getByRole('button', { name: /收起 2 个步骤/ })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('我先读取运行状态。')).toBeInTheDocument();
    expect(document.querySelector('.paw-activity')).toHaveAttribute('aria-expanded', 'true');
    expect(document.querySelector('.paw-activity__detail')).not.toHaveAttribute('hidden');
  });

  it('keeps the active FX stack visible while its running tool detail stays folded', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'responding',
    });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(1, 'text_delta', { delta: '正在检查当前状态。', replaceBlock: true }),
      agentEventFixture(2, 'tool_started', { toolCallId: 'call-running', toolName: 'overview' }),
    ]);

    render(<AgentTurn presentation="fx" sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />);

    expect(screen.queryByRole('button', { name: /个步骤/ })).not.toBeInTheDocument();
    expect(screen.getByText('正在检查当前状态。')).toBeInTheDocument();
    expect(document.querySelector('.paw-activity-stack')).toBeInTheDocument();
    expect(document.querySelector('.paw-activity')).toHaveAttribute('aria-expanded', 'false');
  });

  it('collapses failed FX work behind the partial narrative with truthful controls', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'responding',
    });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(1, 'message_completed', {
        message: assistantMessage(sessionId, turnId, '我先读取运行状态。', 10),
      }),
      agentEventFixture(2, 'tool_finished', {
        toolCallId: 'call-failed-turn-overview',
        toolName: 'overview',
        isError: true,
        result: { details: { ok: true, operation: 'status', result: { summary: '运行状态正常' } } },
      }),
      agentEventFixture(3, 'turn_failed', { error: '503 upstream request failed' }),
    ]);

    render(
      <AgentTurn presentation="fx" sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />,
    );

    const toggle = screen.getByRole('button', { name: /展开 2 个步骤/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveAttribute('data-status', 'failed');
    expect(toggle).toHaveTextContent('未完成');
    expect(screen.getByText('我先读取运行状态。')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(document.querySelector('.paw-activity')).toBeNull();
    const controlledIds = (toggle.getAttribute('aria-controls') ?? '').split(' ').filter(Boolean);
    expect(controlledIds.length).toBeGreaterThan(0);
    for (const id of controlledIds) {
      expect(document.getElementById(id)).not.toBeNull();
    }

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const failedActivity = Array.from(
      document.querySelectorAll<HTMLButtonElement>('.paw-activity[data-state="failed"]'),
    ).find((button) => button.querySelector('[data-kind="tool"]'))!;
    expect(failedActivity).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(failedActivity);
    expect(failedActivity).toHaveAttribute('aria-expanded', 'true');
  });

  it('folds stopped FX work while the toggle states the stopped outcome', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'responding',
    });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(1, 'tool_finished', {
        toolCallId: 'call-stopped-overview',
        toolName: 'overview',
        result: { details: { ok: true, operation: 'status', result: { summary: '运行状态正常' } } },
      }),
      agentEventFixture(2, 'turn_completed', { status: 'aborted' }),
    ]);

    render(
      <AgentTurn presentation="fx" sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />,
    );

    const toggle = screen.getByRole('button', { name: /展开 1 个步骤/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveAttribute('data-status', 'aborted');
    expect(toggle).toHaveTextContent('已停止');
    expect(screen.queryByText('运行状态正常')).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(document.querySelector('.paw-activity')).not.toBeNull();
  });

  it('shows the real elapsed clock on a live FX activity row', () => {
    vi.useFakeTimers();
    vi.setSystemTime(60_000);
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'responding',
    });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(1, 'tool_started', { toolCallId: 'call-live-elapsed', toolName: 'overview' }),
    ]);

    render(<AgentTurn presentation="fx" sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />);

    const row = document.querySelector<HTMLButtonElement>('.paw-activity')!;
    expect(row).toHaveAttribute('aria-expanded', 'false');
    expect(row.querySelector('.fx-meta')).toHaveTextContent(/60 秒/);
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    expect(row.querySelector('.fx-meta')).toHaveTextContent(/1 分/);
  });

  it('aggregates Provider usage once after the whole Tool Loop settles', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'idle',
    });
    const first = {
      ...assistantMessage(sessionId, turnId, '先检查工具。', 10),
      id: 'turn-1:assistant:provider:1',
      provider: 'openai-codex',
      model: 'gpt-5.6-luna',
      usage: { input: 100, output: 10, cacheRead: 50, cacheWrite: 0, totalTokens: 160 },
    };
    const second = {
      ...assistantMessage(sessionId, turnId, '检查完成。', 20),
      id: 'turn-1:assistant:provider:2',
      provider: 'openai-codex',
      model: 'gpt-5.6-luna',
      usage: { input: 200, output: 20, cacheRead: 100, cacheWrite: 10, totalTokens: 330 },
    };
    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(1, 'message_completed', { message: first }),
      agentEventFixture(2, 'tool_finished', {
        toolCallId: 'usage-tool',
        toolName: 'overview',
        result: { details: { ok: true, operation: 'status', result: { summary: '状态已读取' } } },
      }),
      agentEventFixture(3, 'message_completed', { message: second }),
    ]);

    const view = render(
      <AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />,
    );
    expect(screen.queryByLabelText('本轮模型与 Token 用量')).not.toBeInTheDocument();

    act(() => useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(4, 'turn_completed', { status: 'completed' }),
    ]));
    view.rerender(
      <AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />,
    );

    const usage = screen.getByLabelText('本轮模型与 Token 用量');
    expect(screen.getAllByLabelText('本轮模型与 Token 用量')).toHaveLength(1);
    expect(usage).toHaveTextContent('gpt-5.6-luna');
    expect(usage).toHaveTextContent('openai-codex');
    expect(usage).toHaveTextContent('输入 460');
    expect(usage).toHaveTextContent('输出 30');
    expect(usage).toHaveTextContent('缓存 33%');
  });

  it('shows provider reasoning summaries and the live Agent state in the timeline', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'idle',
    });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(1, 'reasoning_summary', {
        requestId: 'reasoning:turn-1:0',
        summary: '正在分析问题与下一步',
        items: [],
        source: 'runtime_status',
        state: 'running',
      }),
    ]);

    const view = render(
      <AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />,
    );

    expect(screen.queryByRole('button', { name: /查看 Agent 思考摘要/ })).not.toBeInTheDocument();
    expect(screen.queryByText('正在分析问题与下一步')).not.toBeInTheDocument();
    expect(document.querySelector('details.agent-activity--inline')).not.toBeInTheDocument();

    act(() => useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(2, 'reasoning_summary', {
        requestId: 'reasoning:turn-1:0',
        summary: 'Implementing durable history reconstruction',
        items: [
          'Analyzing session message discrepancies',
          'Implementing durable history reconstruction',
        ],
        source: 'provider_reasoning_summary',
        state: 'completed',
      }),
      agentEventFixture(3, 'turn_completed', { status: 'completed' }),
    ]));
    view.rerender(
      <AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />,
    );

    const summary = screen.getByRole('button', { name: /查看 Agent 思考摘要/ });
    expect(summary).toHaveTextContent('思考摘要');
    expect(summary).toHaveTextContent('Implementing durable history reconstruction');
    fireEvent.click(summary);
    const reasoningDetails = screen.getByRole('region', { name: '可公开的思考摘要' });
    expect(reasoningDetails).toHaveTextContent('Analyzing session message discrepancies');
    expect(reasoningDetails).toHaveTextContent('Implementing durable history reconstruction');
    expect(reasoningDetails).not.toHaveTextContent('provider_reasoning_summary');
    expect(document.querySelector('details.agent-activity--inline')).not.toBeInTheDocument();
  });

  it('keeps reasoning out of Tool counts but inside the same bounded activity block', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'idle',
    });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(1, 'reasoning_summary', {
        requestId: 'reasoning:separate',
        summary: '先分析代码路径',
        items: ['先分析代码路径'],
        source: 'provider_reasoning_summary',
        state: 'completed',
      }),
      agentEventFixture(2, 'tool_started', {
        toolCallId: 'one-real-tool',
        toolName: 'grep',
        args: { pattern: 'AgentTurn', path: 'control-center-web' },
      }),
      agentEventFixture(3, 'tool_finished', {
        toolCallId: 'one-real-tool',
        toolName: 'grep',
        publicResult: {
          pattern: 'AgentTurn',
          path: 'control-center-web',
          outputPreview: 'AgentTimeline.tsx:342:export function AgentTurn',
        },
      }),
      agentEventFixture(4, 'turn_completed', { status: 'completed' }),
    ]);

    const { container } = render(
      <AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />,
    );

    const group = container.querySelector<HTMLDetailsElement>('details.agent-activity--inline')!;
    expect(group.querySelector('summary')).toHaveTextContent('1 项操作');
    expect(group).not.toHaveAttribute('open');
    expect(screen.getByRole('button', { name: /查看 Agent 思考摘要/ })).toHaveTextContent('先分析代码路径');
    fireEvent.click(group.querySelector('summary')!);
    expect(screen.queryByRole('dialog', { name: '操作记录' })).not.toBeInTheDocument();
    const details = within(group).getByRole('region', { name: '操作与思考过程' });
    expect(details.querySelectorAll('.agent-activity-row')).toHaveLength(1);
    for (const summary of details.querySelectorAll('.agent-activity-row summary')) {
      fireEvent.click(summary);
    }
    expect(details).toHaveTextContent('AgentTimeline.tsx:342:export function AgentTurn');
  });

  it('renders headings, lists, GFM tables, inline code, and fenced code blocks', () => {
    const markdown = [
      '## 验收结论',
      '',
      '正文包含 `ControlTransport`。',
      '',
      '- 保留消息顺序',
      '- 展示结构化结果',
      '',
      '| 项目 | 状态 |',
      '| --- | --- |',
      '| Markdown | 通过 |',
      '| GFM 表格 | 通过 |',
      '',
      '```ts',
      'const ready = true;',
      '```',
      '',
      '```',
      'plain fenced block',
      '```',
      '',
      '```jsonl',
      '{"type":"message","id":"entry-1"}',
      '{"type":"tool_result","ok":true}',
      '```',
    ].join('\n');

    const { container } = render(<TooltipProvider><MarkdownBody text={markdown} /></TooltipProvider>);

    expect(screen.getByRole('heading', { level: 2, name: '验收结论' })).toBeInTheDocument();
    const list = screen.getByRole('list');
    expect(within(list).getAllByRole('listitem')).toHaveLength(2);
    const table = screen.getByRole('table');
    expect(within(table).getAllByRole('columnheader').map((cell) => cell.textContent)).toEqual(['项目', '状态']);
    expect(within(table).getByText('GFM 表格')).toBeInTheDocument();
    expect(screen.getByText('ControlTransport').tagName).toBe('CODE');
    expect(container.querySelector('pre[data-language="ts"]')).toHaveTextContent('const ready = true;');
    expect(container.querySelector('pre[data-language="ts"]')).toHaveAttribute('role', 'region');
    expect(container.querySelector('pre[data-language="ts"]')).toHaveAttribute('tabindex', '0');
    expect(container.querySelector('pre[data-language="text"]')).toHaveTextContent('plain fenced block');
    const jsonl = container.querySelector('pre[data-language="jsonl"]');
    expect(jsonl).toHaveTextContent('{"type":"message","id":"entry-1"}');
    expect(jsonl?.querySelector('code')).toHaveClass('agent-code-block__content');
    expect(container.querySelectorAll('.agent-code-block')).toHaveLength(3);
  });

  it('projects a completed Trace diagnostic envelope as a report receipt instead of raw protocol JSON', async () => {
    const user = userEvent.setup();
    const openRoute = vi.fn();
    const reportId = `trace-report:${'7'.repeat(32)}`;
    const source = [
      '这段自然语言可能很长，不应和机器协议一起重复展示。',
      `网页报告：${reportId}`,
      '--- TRACE_DIAGNOSTIC_RESULT_V1 ---',
      JSON.stringify({
        schemaVersion: 'rag-ime.trace-diagnostic-result.v1',
        summary: '记忆整理没有完成，结果未保存。',
        hardGates: [{ gateId: 'task-completion', status: 'failed', reason: '本次整理没有产生可持久化的结果。', evidenceIds: [] }],
        judgeScores: [],
        findings: [{
          findingId: 'raw-protocol-only',
          observation: 'Pi Runtime Host 的 session.settlement.get 在 JSONL 解析阶段超时。',
          candidateRepair: '对结算读取做一次有界重试，再运行同一组验证。',
        }],
      }),
      '--- END_TRACE_DIAGNOSTIC_RESULT_V1 ---',
    ].join('\n');

    render(
      <PawOsDesktopProvider openRoute={openRoute} openWindow={() => undefined}>
        <MarkdownBody text={source} />
      </PawOsDesktopProvider>,
    );

    const receipt = screen.getByRole('status', { name: 'Trace 诊断结构化结果' });
    expect(receipt).toHaveTextContent('发生了什么');
    expect(receipt).toHaveTextContent('记忆整理没有完成，结果未保存。');
    expect(receipt).toHaveTextContent('对你的影响');
    expect(receipt).toHaveTextContent('本次整理没有产生可持久化的结果。');
    expect(receipt).toHaveTextContent('Trace 可以怎么修复');
    expect(receipt).toHaveTextContent('对结算读取做一次有界重试');
    expect(receipt).toHaveTextContent('下一步');
    expect(receipt).toHaveTextContent('先打开报告核对证据；确认后再交给独立修复 Agent');
    const details = within(receipt).getByText('技术细节与报告编号').closest('details');
    expect(details).not.toHaveAttribute('open');
    expect(details).toHaveTextContent('session.settlement.get');
    expect(receipt).toHaveTextContent(reportId);
    expect(screen.queryByText(/raw-protocol-only/)).not.toBeInTheDocument();
    expect(screen.queryByText(/TRACE_DIAGNOSTIC_RESULT_V1/)).not.toBeInTheDocument();
    expect(screen.queryByText(/这段自然语言可能很长/)).not.toBeInTheDocument();

    await user.click(within(receipt).getByRole('button', { name: '打开网页报告' }));
    expect(openRoute).toHaveBeenCalledWith(`/trace-agent?reportId=${encodeURIComponent(reportId)}`);
  });

  it('does not render a user instruction template as a completed Trace receipt', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-trace-diagnostic';
    const instructionTemplate = [
      '--- TRACE_DIAGNOSTIC_RESULT_V1 ---',
      JSON.stringify({
        schemaVersion: 'rag-ime.trace-diagnostic-result.v1',
        summary: '<简短结论>',
        hardGates: [],
        findings: [{ candidateRepair: '<候选修复>' }],
      }),
      '--- END_TRACE_DIAGNOSTIC_RESULT_V1 ---',
    ].join('\n');
    const completedResult = [
      '--- TRACE_DIAGNOSTIC_RESULT_V1 ---',
      JSON.stringify({
        schemaVersion: 'rag-ime.trace-diagnostic-result.v1',
        summary: '维护运行缺少权威阶段记录。',
        hardGates: [],
        findings: [{ candidateRepair: '补齐权威阶段回执。' }],
      }),
      '--- END_TRACE_DIAGNOSTIC_RESULT_V1 ---',
    ].join('\n');
    const user = userMessage(sessionId, turnId);
    user.id = 'trace-diagnostic-user';
    user.blocks = [{
      ...user.blocks[0]!,
      id: 'trace-diagnostic-user:text',
      data: { text: instructionTemplate },
    }];
    const assistant = assistantMessage(
      sessionId,
      turnId,
      completedResult,
      2,
    );
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [user, assistant],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'idle',
    });

    render(<AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />);

    const userBubble = document.querySelector('.agent-user-message');
    const assistantBubble = document.querySelector('.agent-assistant-message');
    expect(userBubble).not.toBeNull();
    expect(assistantBubble).not.toBeNull();
    expect(within(userBubble as HTMLElement).queryByRole('status', { name: 'Trace 诊断结构化结果' })).not.toBeInTheDocument();
    expect(within(assistantBubble as HTMLElement).getByRole('status', { name: 'Trace 诊断结构化结果' }))
      .toHaveTextContent('维护运行缺少权威阶段记录。');
  });

  it('opens workspace file references from prose while keeping hashes, commands, and code spans plain', async () => {
    const user = userEvent.setup();
    const openRoute = vi.fn();
    render(
      <PawOsDesktopProvider openRoute={openRoute} openWindow={() => undefined}>
        <MarkdownBody
          sessionId="session-files"
          text={'已读取 acceptance.md 和 src/features/rooms/RoomTurn.tsx。\n\n不要把 `ordinary-code.md` 当作文件入口；另有 [room-runtime-handoff.md](room-runtime-handoff.md)。\n\n命令：\ngit diff -- src/ignored.ts'}
        />
      </PawOsDesktopProvider>,
    );

    const acceptance = screen.getByRole('link', { name: '打开文件 acceptance.md' });
    const source = screen.getByRole('link', { name: '打开文件 RoomTurn.tsx' });
    expect(acceptance).toHaveAttribute('title', 'acceptance.md');
    expect(source).toHaveAttribute('title', 'src/features/rooms/RoomTurn.tsx');
    expect(screen.getByText('ordinary-code.md').tagName).toBe('CODE');
    expect(screen.getByRole('link', { name: '打开文件 room-runtime-handoff.md' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: '打开文件 ignored.ts' })).not.toBeInTheDocument();
    await user.click(acceptance);
    expect(openRoute).toHaveBeenCalledWith('/files?session=session-files&path=acceptance.md');
  });

  it('renders inline unified diffs and lets the user switch to a split view', () => {
    const diff = [
      'diff --git a/src/runtime.ts b/src/runtime.ts',
      'index 1111111..2222222 100644',
      '--- a/src/runtime.ts',
      '+++ b/src/runtime.ts',
      '@@ -1,2 +1,2 @@',
      '-const state = "queued";',
      '+const state = "running";',
      ' export { state };',
    ].join('\n');
    const { container } = render(
      <TooltipProvider>
        <AgentBlock block={{
          id: 'diff-1',
          type: 'diff',
          status: 'completed',
          presentationKind: 'diff.v1',
          data: { fileName: 'src/runtime.ts', diff },
        }} />
      </TooltipProvider>,
    );

    expect(container.querySelector('.agent-inline-diff')).toHaveAttribute('open');
    expect(screen.getAllByText('src/runtime.ts')).toHaveLength(2);
    expect(container.querySelector('tr[data-kind="remove"]')).toHaveTextContent('const state = "queued";');
    expect(container.querySelector('tr[data-kind="add"]')).toHaveTextContent('const state = "running";');

    const filesScroll = container.querySelector('.agent-diff-preview__files');
    expect(filesScroll).toHaveAttribute('role', 'region');
    expect(filesScroll).toHaveAttribute('aria-label', 'Diff 文件列表');
    expect(filesScroll).toHaveAttribute('tabindex', '0');
    const fileScroll = container.querySelector('.agent-diff-file__scroll');
    expect(fileScroll).toHaveAttribute('role', 'region');
    expect(fileScroll).toHaveAttribute('aria-label', 'src/runtime.ts 变更内容');
    expect(fileScroll).toHaveAttribute('tabindex', '0');

    fireEvent.click(screen.getByRole('radio', { name: /并排/ }));
    expect(container.querySelectorAll('.agent-diff-split')).toHaveLength(4);
  });

  it('renders hunk-only diffs with the trusted block file name', () => {
    const { container } = render(
      <TooltipProvider>
        <AgentBlock block={{
          id: 'diff-hunk-only',
          type: 'diff',
          status: 'completed',
          presentationKind: 'diff.v1',
          data: {
            fileName: 'src/state/live-store.ts',
            diff: [
              '@@ -12,2 +12,2 @@ export function commit(events) {',
              '-  set(next);',
              '+  set((state) => reduceBatch(state, events));',
            ].join('\n'),
          },
        }} />
      </TooltipProvider>,
    );

    expect(container.querySelector('.agent-diff-file')).toBeInTheDocument();
    expect(screen.getAllByText('src/state/live-store.ts')).toHaveLength(2);
    expect(container.querySelector('tr[data-kind="remove"]')).toHaveTextContent('set(next)');
    expect(container.querySelector('tr[data-kind="add"]')).toHaveTextContent('reduceBatch');
  });

  it('bounds an unparseable long diff as a keyboard-scrollable evidence region', async () => {
    const user = userEvent.setup();
    const { container } = render(
      <TooltipProvider>
        <AgentBlock block={{
          id: 'diff-plain-long',
          type: 'diff',
          status: 'completed',
          presentationKind: 'diff.v1',
          data: {
            fileName: 'notes.patch',
            diff: Array.from({ length: 900 }, (_, index) => `raw diff line ${index}`).join('\n'),
          },
        }} />
      </TooltipProvider>,
    );

    await user.click(container.querySelector('.agent-inline-diff > summary')!);
    const plain = container.querySelector('.agent-file-preview__plain');
    expect(plain).toHaveAttribute('role', 'region');
    expect(plain).toHaveAttribute('aria-label', 'notes.patch 变更内容');
    expect(plain).toHaveAttribute('tabindex', '0');
  });

  it('keeps long Markdown structurally stable when streaming becomes completed', () => {
    const markdown = Array.from(
      { length: 52 },
      (_, index) => `第 ${index + 1} 段仍可按需查看。`,
    ).join('\n\n');
    const { container, rerender } = render(<MarkdownBody streamingTail text={markdown} />);

    expect(container.querySelector('.agent-markdown')).not.toHaveAttribute('data-collapsed');
    expect(screen.getByText('第 1 段仍可按需查看。')).toBeInTheDocument();
    expect(screen.getByText('第 52 段仍可按需查看。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /展开全文|收起长回复/ })).not.toBeInTheDocument();

    rerender(<MarkdownBody text={markdown} />);
    expect(container.querySelector('.agent-markdown')).not.toHaveAttribute('data-collapsed');
    expect(screen.getByText('第 52 段仍可按需查看。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /展开全文|收起长回复/ })).not.toBeInTheDocument();
  });

  it('renders the typed rich block allowlist without executing untrusted markup or links', () => {
    const blocks: UiAgentBlock[] = [
      { id: 'card', type: 'card', status: 'completed', presentationKind: 'card.v1', data: { title: '发布检查', tone: 'success', bodyMarkdown: '[安全链接](https://example.com) <img src=x onerror=alert(1)> [危险](javascript:alert(1))', fields: [{ label: '测试', value: '通过' }] } },
      { id: 'checklist', type: 'checklist', status: 'completed', presentationKind: 'checklist.v1', data: { title: '验收项', items: [{ id: 'one', text: '类型检查', checked: true }, { id: 'two', text: '移动端检查' }] } },
      { id: 'table', type: 'table', status: 'completed', presentationKind: 'table.v1', data: { title: '结果表', columns: ['项目', '状态'], rows: [['Room Post', '通过']] } },
      { id: 'artifact', type: 'artifact', status: 'completed', presentationKind: 'artifact.v1', data: { title: '审计报告', summary: '已持久化', url: 'javascript:alert(1)' } },
      { id: 'reference', type: 'reference', status: 'completed', presentationKind: 'reference.v1', data: { title: '需求原文', url: 'javascript:alert(1)', excerpt: '原始需求保持不变' } },
      { id: 'artifact-protocol-relative', type: 'artifact', status: 'completed', presentationKind: 'artifact.v1', data: { title: '协议相对地址', url: '//evil.example/file' } },
      { id: 'reference-backslash', type: 'reference', status: 'completed', presentationKind: 'reference.v1', data: { title: '反斜杠地址', url: '/\\evil.example', excerpt: '不可点击' } },
      { id: 'status', type: 'status', status: 'completed', presentationKind: 'status.v1', data: { title: 'Runtime', state: 'completed', summary: '全部收束' } },
    ];

    const { container } = render(<TooltipProvider><AgentBlocks blocks={blocks} /></TooltipProvider>);

    expect(screen.getByRole('region', { name: '发布检查' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /安全链接/ })).toHaveAttribute('href', 'https://example.com/');
    expect(screen.getByText('危险').closest('a')).toBeNull();
    expect(container.querySelector('img[src="x"]')).toBeNull();
    expect(container.querySelector('script')).toBeNull();
    expect(screen.getByText('类型检查')).toBeInTheDocument();
    expect(screen.getByRole('table')).toHaveTextContent('Room Post通过');
    expect(screen.queryByRole('button', { name: '打开产物回执' })).not.toBeInTheDocument();
    expect(screen.getByText('原始需求保持不变').closest('a')).toBeNull();
    expect(screen.getByText('协议相对地址').closest('a')).toBeNull();
    expect(screen.getByText('不可点击').closest('a')).toBeNull();
    expect(screen.getByRole('region', { name: 'Runtime' })).toHaveTextContent('全部收束');
  });

  it('makes every rich-result disclosure share the primitive pointer and ARIA contract', () => {
    const blocks: UiAgentBlock[] = [
      {
        id: 'disclosure-checklist',
        type: 'checklist',
        status: 'completed',
        presentationKind: 'checklist.v1',
        data: { title: '交互清单', items: [{ id: 'one', text: '可展开', checked: true }] },
      },
      {
        id: 'disclosure-table',
        type: 'table',
        status: 'completed',
        presentationKind: 'table.v1',
        data: { title: '交互表格', columns: ['项目'], rows: [['可读取']] },
      },
      {
        id: 'disclosure-diff',
        type: 'diff',
        status: 'completed',
        presentationKind: 'diff.v1',
        data: { fileName: 'interaction.diff', diff: '@@ -1 +1 @@\n-old\n+new' },
      },
      {
        id: 'disclosure-code',
        type: 'code',
        status: 'completed',
        presentationKind: 'code.v1',
        data: {
          fileName: 'long-output.log',
          language: 'text',
          code: Array.from({ length: 40 }, (_, index) => `line ${index + 1}`).join('\n'),
        },
      },
      {
        id: 'disclosure-unknown',
        type: 'unknown',
        rawType: 'future_panel',
        status: 'completed',
        presentationKind: 'future.v1',
        summary: '安全保留的未来内容',
        data: {},
      },
    ];
    const { container } = render(
      <TooltipProvider><AgentBlocks blocks={blocks} /></TooltipProvider>,
    );

    const disclosures = [
      container.querySelector<HTMLDetailsElement>('.agent-rich-checklist')!,
      container.querySelector<HTMLDetailsElement>('.agent-rich-table')!,
      container.querySelector<HTMLDetailsElement>('.agent-inline-diff')!,
      container.querySelector<HTMLDetailsElement>('.agent-unknown-block')!,
    ];
    expect(disclosures.every(Boolean)).toBe(true);
    expect(within(screen.getByRole('region', { name: '交互表格可滚动内容' })).getByRole('table')).toHaveTextContent('可读取');
    expect(screen.getByRole('button', { name: /展开阅读区/ })).toHaveAttribute('aria-expanded', 'false');

    for (const details of disclosures) {
      const summary = details.querySelector<HTMLElement>(':scope > summary')!;
      const initialOpen = details.hasAttribute('open');
      const controlledId = summary.getAttribute('aria-controls');
      expect(summary).toHaveAttribute('aria-expanded', String(initialOpen));
      expect(controlledId).toBeTruthy();
      expect(document.getElementById(controlledId!)).toBeInTheDocument();

      fireEvent.click(summary);
      expect(summary).toHaveAttribute('aria-expanded', String(!initialOpen));

      fireEvent.click(summary);
      expect(summary).toHaveAttribute('aria-expanded', String(initialOpen));

      fireEvent.click(summary);
      expect(summary).toHaveAttribute('aria-expanded', String(!initialOpen));
    }
  });

  it('keeps oversized structured blocks bounded while allowing every safe item to be loaded', async () => {
    const user = userEvent.setup();
    const blocks: UiAgentBlock[] = [
      {
        id: 'long-checklist', type: 'checklist', status: 'completed', presentationKind: 'checklist.v1', summary: '',
        data: { title: '长清单', items: Array.from({ length: 25 }, (_, index) => ({ text: `清单 ${index + 1}`, checked: index < 10 })) },
      },
      {
        id: 'wide-table', type: 'table', status: 'completed', presentationKind: 'table.v1', summary: '',
        data: {
          title: '宽表',
          columns: Array.from({ length: 9 }, (_, index) => ({ key: `c${index + 1}`, label: `列 ${index + 1}` })),
          rows: Array.from({ length: 25 }, (_, index) => Object.fromEntries(Array.from({ length: 9 }, (_, column) => [`c${column + 1}`, `r${index + 1}c${column + 1}`]))),
        },
      },
      {
        id: 'long-reasoning', type: 'reasoning_summary', status: 'completed', presentationKind: 'reasoning_summary.v1', summary: '',
        data: { source: 'provider_reasoning_summary', items: Array.from({ length: 9 }, (_, index) => `工作要点 ${index + 1}`) },
      },
    ];
    const { container } = render(<TooltipProvider><AgentBlocks blocks={blocks} /></TooltipProvider>);
    const checklist = container.querySelector<HTMLElement>('.agent-rich-checklist')!;
    const table = container.querySelector<HTMLElement>('.agent-rich-table')!;
    const reasoning = container.querySelector<HTMLElement>('.agent-reasoning-summary')!;

    expect(checklist).toHaveTextContent('显示 24/25');
    expect(table).toHaveTextContent('显示 24/25 行 · 8/9 列');
    expect(reasoning).toHaveTextContent('显示 8/9 项');
    await user.click(checklist.querySelector('summary')!);
    await user.click(table.querySelector('summary')!);
    await user.click(reasoning.querySelector('summary')!);
    await user.click(within(checklist).getByRole('button', { name: /加载更多/ }));
    await user.click(within(table).getByRole('button', { name: /加载更多行/ }));
    await user.click(within(table).getByRole('button', { name: /加载更多列/ }));
    await user.click(within(reasoning).getByRole('button', { name: /加载更多/ }));
    expect(checklist).toHaveTextContent('清单 25');
    expect(table).toHaveTextContent('r25c9');
    expect(reasoning).toHaveTextContent('工作要点 9');
  });

  it('cleans wrappers from stored public summary blocks while retaining the source gate', async () => {
    const user = userEvent.setup();
    const blocks: UiAgentBlock[] = [
      { id: 'public-wrapped', type: 'reasoning_summary', status: 'completed', presentationKind: 'reasoning_summary.v1', summary: '',
        data: { source: 'provider_reasoning_summary', items: ['<thinking>Checking revision</thinking>'] } },
      { id: 'private-wrapped', type: 'reasoning_summary', status: 'completed', presentationKind: 'reasoning_summary.v1', summary: '',
        data: { source: 'private', items: ['<thinking>Do not show</thinking>'] } },
    ];
    const { container } = render(<TooltipProvider><AgentBlocks blocks={blocks} /></TooltipProvider>);
    await user.click(container.querySelector('summary')!);
    expect(container).toHaveTextContent('Checking revision');
    expect(container).not.toHaveTextContent('<thinking>');
    expect(container).not.toHaveTextContent('Do not show');
  });

  it('flattens repeated file receipts to one latest block per logical file', () => {
    const blocks: UiAgentBlock[] = [
      fileBlock('tui-v1', 'tui.py', 'media_tui_version_0001', '1'.repeat(64)),
      fileBlock('tui-diff-v1', 'tui.py.diff', 'media_tui_diff_000001', '2'.repeat(64)),
      fileBlock('tui-v2', 'tui.py', 'media_tui_version_0002', '3'.repeat(64)),
      fileBlock('tui-v2-replayed', 'tui.py', 'media_tui_version_0002', '3'.repeat(64)),
      fileBlock('tui-diff-v2', 'tui.py.diff', 'media_tui_diff_000002', '4'.repeat(64)),
      fileBlock('test-v1', 'test_tui.py', 'media_test_version_001', '5'.repeat(64)),
      fileBlock('test-diff-v1', 'test_tui.py.diff', 'media_test_diff_00001', '6'.repeat(64)),
    ];

    const { container } = render(
      <TooltipProvider><AgentBlocks blocks={blocks} sessionId="session:file-results" /></TooltipProvider>,
    );

    const collection = screen.getByRole('region', { name: '结果文件' });
    expect(collection).toBeInTheDocument();
    expect(container.querySelectorAll('.agent-file-collection__file')).toHaveLength(0);
    expect(container.querySelectorAll('.agent-file-collection__version')).toHaveLength(0);
    expect(container.querySelectorAll('.agent-file-block-shell')).toHaveLength(2);
    expect(screen.getByRole('button', { name: 'tui.py.diff 的预览回执不可用' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'test_tui.py.diff 的预览回执不可用' })).toBeDisabled();
  });

  it('keeps a structured Tool disclosure anchored and focused when opened', () => {
    const block: UiAgentBlock = {
      id: 'tool-result-anchor',
      type: 'tool_result',
      status: 'completed',
      presentationKind: 'tool_result.v1',
      data: {
        toolName: 'overview',
        status: 'completed',
        summary: '运行状态已读取',
      },
    };
    const { container } = render(
      <div data-testid="structured-tool-scrollport" style={{ maxHeight: 240, overflowY: 'auto' }}>
        <AgentBlock block={block} />
      </div>,
    );
    const scrollport = screen.getByTestId('structured-tool-scrollport');
    Object.defineProperty(scrollport, 'scrollHeight', { configurable: true, value: 800 });
    Object.defineProperty(scrollport, 'clientHeight', { configurable: true, value: 240 });
    scrollport.scrollTop = 220;
    const details = container.querySelector<HTMLDetailsElement>('details.agent-tool-activity')!;
    const summary = details.querySelector<HTMLElement>('summary')!;
    vi.spyOn(summary, 'getBoundingClientRect').mockImplementation(() => {
      const top = details.hasAttribute('open')
        ? 96 + (220 - scrollport.scrollTop)
        : 132;
      return {
        bottom: top + 38,
        height: 38,
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

    expect(details).toHaveAttribute('open');
    expect(summary).toHaveAttribute('aria-expanded', 'true');
    expect(scrollport.scrollTop).toBe(184);
    expect(document.activeElement).toBe(summary);
  });

  it('keeps partial JSON as streaming text, bounds large code, and degrades unknown blocks readably', async () => {
    const user = userEvent.setup();
    const partial = '{"type":"card","data":{"title":"还没结束"';
    const blocks: UiAgentBlock[] = [
      { id: 'stream', type: 'text', status: 'running', presentationKind: 'markdown', data: { text: partial } },
      { id: 'large-code', type: 'code', status: 'completed', presentationKind: 'code', data: { fileName: 'worker.log', language: 'text', code: Array.from({ length: 40 }, (_, index) => `line ${index + 1}`).join('\n') } },
      { id: 'future', type: 'unknown', rawType: 'timeline_chart', status: 'completed', presentationKind: 'timeline.v2', summary: '未来时间线', data: { title: '不应从任意 data 展示' } },
    ];

    const { container } = render(<TooltipProvider><AgentBlocks blocks={blocks} streaming /></TooltipProvider>);

    expect(container.querySelectorAll('.agent-rich-card')).toHaveLength(0);
    expect(screen.getByText(partial)).toBeInTheDocument();
    const codeRegion = screen.getByRole('region', { name: 'worker.log 代码内容' });
    expect(codeRegion).toHaveTextContent('line 40');
    const expand = screen.getByRole('button', { name: /展开阅读区/ });
    expect(expand).toHaveAttribute('aria-expanded', 'false');
    await user.click(expand);
    expect(expand).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText(/暂不支持的内容 · timeline_chart/)).toBeInTheDocument();
    await user.click(screen.getByText(/暂不支持的内容 · timeline_chart/));
    expect(screen.getByText('未来时间线')).toBeInTheDocument();
  });

  it('anchors the streaming cursor inside the final Markdown text container', () => {
    const blocks: UiAgentBlock[] = [{
      id: 'streaming-text',
      type: 'text',
      status: 'running',
      presentationKind: 'markdown',
      data: { text: '- 保留 `Markdown`\n- 光标紧贴最后一项' },
    }];
    const { container } = render(
      <div className="agent-assistant-message" data-status="streaming">
        <AgentBlocks blocks={blocks} streaming />
        <span aria-label="正在生成" className="agent-streaming-cursor" />
      </div>,
    );

    const items = screen.getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[0]).not.toHaveAttribute('data-stream-tail');
    expect(items[1]).toHaveAttribute('data-stream-tail', 'true');
    expect(within(items[0]!).getByText('Markdown').tagName).toBe('CODE');
    expect(items[1]!.querySelector('.agent-streaming-cursor--inline')).toBeInTheDocument();
    expect(container.querySelector('.agent-blocks')).toHaveAttribute('data-has-stream-tail', 'true');
    expect(screen.getByLabelText('正在生成')).toBe(container.querySelector('.agent-blocks')?.nextElementSibling);
  });

  it('only renders explicitly trusted non-executable block policies', () => {
    expect(agentRendererPolicy('text')).toMatchObject({
      streaming: 'incremental',
      executableContent: false,
    });
    expect(agentRendererPolicy('diff')?.Renderer).toBeTypeOf('function');
    expect(agentRendererPolicy('image')).toMatchObject({
      isolation: 'managed-receipt',
      executableContent: false,
    });
    expect(agentRendererPolicy('html')).toBeUndefined();
    expect(agentRendererPolicy('script')).toBeUndefined();
    expect(Object.values(TRUSTED_AGENT_RENDERERS).every((item) => !item.executableContent)).toBe(true);
  });

  it('removes stale segment cursors while a turn is waiting for confirmation', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'responding',
    });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(1, 'text_delta', {
        messageId: 'turn-1:assistant:segment:1',
        blockId: 'turn-1:assistant:segment:1:text',
        delta: '先盘点记忆库。',
        replaceBlock: true,
      }),
      agentEventFixture(2, 'text_delta', {
        messageId: 'turn-1:assistant:segment:2',
        blockId: 'turn-1:assistant:segment:2:text',
        delta: '草案已生成，等待确认。',
        replaceBlock: true,
      }),
      agentEventFixture(3, 'user_input_required', {
        requestId: 'memory-review-1',
        requestKind: 'memory_review',
        title: '审阅记忆草案',
      }),
    ]);

    const { container } = render(
      <AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />,
    );

    expect(container.querySelector('.agent-turn')).toHaveAttribute('data-turn-status', 'waiting');
    expect(container.querySelectorAll('.agent-assistant-message')).toHaveLength(2);
    expect(container.querySelector('.agent-streaming-cursor')).not.toBeInTheDocument();
  });

  it('renders only a same-origin managed image receipt and ignores src/url fallbacks', () => {
    const managedReceipt = '/api/agent/media/media-123/content?sessionId=session-123';
    const { container, rerender } = render(
      <AgentBlock block={imageBlock({
        receiptUrl: managedReceipt,
        src: 'https://example.com/untrusted.png',
        alt: '受控图片',
      })} />,
    );

    const image = screen.getByRole('img', { name: '受控图片' });
    expect(image).toHaveAttribute('src', managedReceipt);
    expect(container.querySelector('.paw-image-gallery img')).toBe(image);

    rerender(<AgentBlock block={imageBlock({ src: managedReceipt, alt: '伪造回执' })} />);
    expect(screen.queryByRole('img', { name: '伪造回执' })).not.toBeInTheDocument();
    expect(screen.getByText('图片回执不可用')).toBeInTheDocument();

    rerender(<AgentBlock block={imageBlock({ receiptUrl: 'https://example.com/remote.png', alt: '远程回执' })} />);
    expect(screen.queryByRole('img', { name: '远程回执' })).not.toBeInTheDocument();

    rerender(<AgentBlock block={imageBlock({ receiptUrl: '/companions/personas/companion-present-v9.webp', alt: '静态资产' })} />);
    expect(screen.queryByRole('img', { name: '静态资产' })).not.toBeInTheDocument();
  });

  it('resolves managed conversation images through the native media origin', () => {
    const managedReceipt = '/api/agent/media/media_native_fixture_01/content?sessionId=session:native-1';
    render(
      <ControlTransportProvider transport={new StubControlTransport('native', {})}>
        <AgentBlock block={imageBlock({ receiptUrl: managedReceipt, alt: '原始对话图片' })} />
      </ControlTransportProvider>,
    );

    const image = screen.getByRole('img', { name: '原始对话图片' });
    expect(image).toHaveAttribute(
      'src',
      'http://127.0.0.1:8766/api/agent/media/media_native_fixture_01/content?sessionId=session%3Anative-1',
    );
    fireEvent.error(image);
    expect(screen.queryByRole('img', { name: '原始对话图片' })).not.toBeInTheDocument();
    expect(screen.getByText('图片未能加载')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试预览' }));
    expect(screen.getByRole('img', { name: '原始对话图片' })).toHaveAttribute('src', image.getAttribute('src'));
  });

  it('keeps remote Markdown images blocked', () => {
    const { container } = render(
      <MarkdownBody text="远程图片：![外部图片](https://example.com/not-allowed.png)" />,
    );

    expect(container.querySelector('.agent-markdown img')).not.toBeInTheDocument();
    expect(screen.getByText('外部图片').closest('.paw-rich-image-reference')).toBeInTheDocument();
    expect(screen.getByText('没有受控回执，未自动加载外部图片')).toBeVisible();
  });

  it('shows one public failure notice without repeating the raw provider error', () => {
    const sessionId = 'session-failed-snapshot';
    const turnId = 'turn-failed-snapshot';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId), failedAssistantMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 7,
      resumeToken: `${sessionId}:7`,
      status: 'responding',
    });

    const projection = useAgentLiveStore.getState().projections[sessionId];
    expect(projection.turnsById[turnId]?.status).toBe('failed');

    const { container } = render(
      <AgentTurn
        sessionId={sessionId}
        turnId={turnId}
        onApprovalDecision={() => {}}
      />,
    );

    expect(screen.getByText('本轮失败')).toBeInTheDocument();
    expect(screen.queryByText('正在响应')).not.toBeInTheDocument();
    expect(container.querySelector('.agent-turn')).toHaveAttribute('data-turn-status', 'failed');
    expect(container.querySelectorAll('.agent-user-message')).toHaveLength(1);
    expect(screen.getAllByText('请检查失败状态')).toHaveLength(1);
    expect(screen.getAllByRole('alert')).toHaveLength(1);
    expect(screen.getByRole('alert')).toHaveTextContent('当前模型不可用，请切换模型后重试');
    expect(screen.queryByText(/not supported by any configured account/i)).not.toBeInTheDocument();
    expect(container.querySelectorAll('.agent-inline-notice[data-tone="danger"]')).toHaveLength(0);
  });

  it('clears an old failure recovery surface after newer input while preserving the turn history', () => {
    const sessionId = 'session-1';
    const failedTurnId = 'turn-old-failure';
    const nextTurnId = 'turn-new-input';
    const nextUser: UiAgentMessage = {
      ...userMessage(sessionId, nextTurnId),
      id: 'new-input-user-message',
      blocks: [{
        ...userMessage(sessionId, nextTurnId).blocks[0]!,
        id: 'new-input-user-text',
        data: { text: '这是失败之后的新输入' },
      }],
      createdAtMs: 10,
      completedAtMs: 11,
    };
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [
        userMessage(sessionId, failedTurnId),
        failedAssistantMessage(sessionId, failedTurnId),
        nextUser,
      ],
      liveEvents: [{
        ...agentEventFixture(9, 'turn_failed', {
          error: '503 upstream request failed',
          retryExhausted: true,
          providerRetryAttempts: 6,
        }),
        eventId: `${sessionId}:9`,
        sessionId,
        turnId: failedTurnId,
      }],
      lastSequence: 10,
      resumeToken: `${sessionId}:10`,
      status: 'idle',
    });
    const retryTurn = vi.fn(() => true);
    const continueTurn = vi.fn(() => true);
    const switchModel = vi.fn();

    const { container } = render(
      <TooltipProvider>
        <AgentTurn
          sessionId={sessionId}
          turnId={failedTurnId}
          modelSelectionAvailable
          onApprovalDecision={() => {}}
          onContinueTurn={continueTurn}
          onRetryTurn={retryTurn}
          onSwitchModel={switchModel}
        />
      </TooltipProvider>,
    );

    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(container.querySelector('.agent-turn')).toHaveAttribute('data-turn-status', 'failed');
    expect(container.querySelectorAll('.agent-user-message')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: '重试本轮' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '切换模型' })).not.toBeInTheDocument();

    act(() => useAgentLiveStore.getState().applyEvents(sessionId, [{
      ...agentEventFixture(11, 'turn_failed', {
        error: '503 upstream request failed',
        retryExhausted: true,
        providerRetryAttempts: 6,
      }),
      eventId: `${sessionId}:11`,
      sessionId,
      turnId: failedTurnId,
    }]));

    expect(useAgentLiveStore.getState().projections[sessionId].turnOrder.at(-1)).toBe(nextTurnId);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重试本轮' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '继续' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '切换模型' })).not.toBeInTheDocument();
    expect(retryTurn).not.toHaveBeenCalled();
    expect(continueTurn).not.toHaveBeenCalled();
    expect(switchModel).not.toHaveBeenCalled();
  });

  it('requires re-upload instead of retrying a failed local attachment placeholder', () => {
    const sessionId = 'session-local-attachment-failed';
    const turnId = 'turn-local-attachment-failed';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [{
        ...userMessage(sessionId, turnId),
        id: 'local-attachment-user',
        attachments: ['home-attachment-local-only'],
      }],
      liveEvents: [{
        ...agentEventFixture(2, 'turn_failed', {
          error: '附件未能导入，这条消息没有发送。请重新上传附件后发送。',
        }),
        eventId: `${sessionId}:2`,
        sessionId,
        turnId,
      }],
      lastSequence: 2,
      resumeToken: `${sessionId}:2`,
      status: 'faulted',
    });

    render(
      <TooltipProvider>
        <AgentTurn
          sessionId={sessionId}
          turnId={turnId}
          modelSelectionAvailable
          onApprovalDecision={() => {}}
          onRetryTurn={() => true}
          onSwitchModel={() => {}}
        />
      </TooltipProvider>,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('重新上传附件');
    expect(screen.queryByRole('button', { name: '重试本轮' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '切换模型' })).not.toBeInTheDocument();
    useAgentLiveStore.getState().clear(sessionId);
  });

  it('does not surface a superseded user-only snapshot turn as the current failure', () => {
    const sessionId = 'session-steered-snapshot';
    const interruptedTurnId = 'turn-before-steer';
    const steeredTurnId = 'turn-after-steer';
    const steeredUser: UiAgentMessage = {
      ...userMessage(sessionId, steeredTurnId),
      id: 'steered-user-message',
      blocks: [{
        ...userMessage(sessionId, steeredTurnId).blocks[0]!,
        id: 'steered-user-text',
        data: { text: '新指令已生效' },
      }],
      createdAtMs: 20,
      completedAtMs: 21,
    };
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, interruptedTurnId), steeredUser],
      liveEvents: [],
      lastSequence: 21,
      resumeToken: `${sessionId}:21`,
      status: 'responding',
    });

    const { container } = render(
      <AgentTurn
        sessionId={sessionId}
        turnId={interruptedTurnId}
        onApprovalDecision={() => {}}
      />,
    );

    expect(useAgentLiveStore.getState().projections[sessionId].turnsById[interruptedTurnId]?.status).toBe('failed');
    expect(container.querySelector('.agent-turn')).toHaveAttribute('data-turn-status', 'failed');
    expect(container.querySelectorAll('.agent-user-message')).toHaveLength(1);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByText('未收到助手回复。')).not.toBeInTheDocument();
  });

  it('offers a safe continuation instead of replaying tools after a network interruption', () => {
    const sessionId = 'session-network-interrupted';
    const turnId = 'turn-network-interrupted';
    const failedWithResult: UiAgentMessage = {
      ...failedAssistantMessage(sessionId, turnId),
      id: 'network-interrupted-assistant',
      blocks: [
        {
          id: 'network-interrupted-error',
          type: 'error',
          status: 'failed',
          presentationKind: 'error',
          data: { message: 'WebSocket error' },
        },
        fileBlock('network-result', 'tui.py', 'media_network_result_01', '7'.repeat(64)),
      ],
    };
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId), failedWithResult],
      liveEvents: [{
        ...agentEventFixture(9, 'turn_failed', {
          error: 'WebSocket error',
          retryExhausted: true,
          providerRetryAttempts: 6,
          providerRetryMaxAttempts: 6,
          nextStep: '模型连接在自动重试后仍未恢复，请稍后继续或切换模型。',
        }),
        eventId: `${sessionId}:9`,
        sessionId,
        turnId,
      }],
      lastSequence: 9,
      resumeToken: `${sessionId}:9`,
      status: 'faulted',
    });
    const continueTurn = vi.fn(() => true);
    const retryTurn = vi.fn(() => true);

    const { unmount } = render(
      <TooltipProvider>
        <AgentTurn
          sessionId={sessionId}
          turnId={turnId}
          onApprovalDecision={() => {}}
          onContinueTurn={continueTurn}
          onRetryTurn={retryTurn}
          onSwitchModel={() => {}}
        />
      </TooltipProvider>,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('网络中断');
    expect(screen.getByRole('alert')).toHaveTextContent('已自动重试 6 次，仍未恢复');
    expect(screen.getByRole('alert')).toHaveTextContent('已完成的工具与文件结果已保留');
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    expect(continueTurn).toHaveBeenCalledWith(turnId);
    expect(retryTurn).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: '重试本轮' })).not.toBeInTheDocument();

    unmount();
    render(
      <TooltipProvider>
        <AgentTurn
          failurePresentation="compact"
          sessionId={sessionId}
          turnId={turnId}
          onApprovalDecision={() => {}}
          onContinueTurn={continueTurn}
          onRetryTurn={retryTurn}
          onSwitchModel={() => {}}
        />
      </TooltipProvider>,
    );

    expect(screen.getByRole('button', { name: '继续问数' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '切换模型' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Trace Agent/ })).not.toBeInTheDocument();
  });

  it('offers one safe continuation after a Runtime host restart instead of replaying the failed turn', () => {
    const sessionId = 'session-runtime-restarted';
    const turnId = 'turn-runtime-restarted';
    const failedAfterRestart: UiAgentMessage = {
      ...failedAssistantMessage(sessionId, turnId),
      id: 'runtime-restarted-assistant',
      blocks: [{
        id: 'runtime-restarted-error',
        type: 'error',
        status: 'failed',
        presentationKind: 'error',
        data: { message: 'Agent 运行时中断，任务已暂停等待恢复' },
      }],
    };
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId), failedAfterRestart],
      liveEvents: [{
        ...agentEventFixture(12, 'turn_failed', {
          error: 'Agent 运行时中断，任务已暂停等待恢复',
          failureKind: 'runtime_host_exit',
          nextStep: '重新打开当前 Session 后继续。',
        }),
        eventId: `${sessionId}:12`,
        sessionId,
        turnId,
      }],
      lastSequence: 12,
      resumeToken: `${sessionId}:12`,
      status: 'faulted',
    });
    const continueTurn = vi.fn(() => true);
    const retryTurn = vi.fn(() => true);

    render(
      <TooltipProvider>
        <AgentTurn
          sessionId={sessionId}
          turnId={turnId}
          onApprovalDecision={() => {}}
          onContinueTurn={continueTurn}
          onRetryTurn={retryTurn}
          onSwitchModel={() => {}}
        />
      </TooltipProvider>,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('运行时中断');
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    expect(continueTurn).toHaveBeenCalledWith(turnId);
    expect(retryTurn).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: '重试本轮' })).not.toBeInTheDocument();
  });

  it('reports exhausted provider retries before offering a safe continuation', () => {
    const sessionId = 'session-provider-retry-exhausted';
    const turnId = 'turn-provider-retry-exhausted';
    const failedWithResult: UiAgentMessage = {
      ...failedAssistantMessage(sessionId, turnId),
      id: 'provider-retry-exhausted-assistant',
      blocks: [
        {
          id: 'provider-retry-exhausted-error',
          type: 'error',
          status: 'failed',
          presentationKind: 'error',
          data: { message: '503 upstream request failed' },
        },
        fileBlock('provider-retry-result', 'result.md', 'media_provider_retry_01', '8'.repeat(64)),
      ],
    };
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId), failedWithResult],
      liveEvents: [{
        ...agentEventFixture(10, 'turn_failed', {
          error: '503 upstream request failed',
          retryExhausted: true,
          providerRetryAttempts: 6,
          providerRetryMaxAttempts: 6,
        }),
        eventId: `${sessionId}:10`,
        sessionId,
        turnId,
      }],
      lastSequence: 10,
      resumeToken: `${sessionId}:10`,
      status: 'faulted',
    });
    const continueTurn = vi.fn(() => true);
    const retryTurn = vi.fn(() => true);

    render(
      <TooltipProvider>
        <AgentTurn
          sessionId={sessionId}
          turnId={turnId}
          onApprovalDecision={() => {}}
          onContinueTurn={continueTurn}
          onRetryTurn={retryTurn}
          onSwitchModel={() => {}}
        />
      </TooltipProvider>,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('模型服务请求失败');
    expect(screen.getByRole('alert')).toHaveTextContent('已自动重试 6 次，仍未恢复');
    expect(screen.getByRole('alert')).toHaveTextContent('已完成的工具与文件结果已保留');
    fireEvent.click(screen.getByRole('button', { name: '继续' }));
    expect(continueTurn).toHaveBeenCalledWith(turnId);
    expect(retryTurn).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: '重试本轮' })).not.toBeInTheDocument();
  });

  it('does not render an empty assistant shell after a stopped turn', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-stopped-empty';
    const emptyAssistant: UiAgentMessage = {
      ...assistantMessage(sessionId, turnId, '', 2),
      status: 'aborted',
      blocks: [],
    };
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId), emptyAssistant],
      liveEvents: [],
      lastSequence: 2,
      resumeToken: `${sessionId}:2`,
      status: 'idle',
    });

    const { container } = render(
      <AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />,
    );

    expect(container.querySelector('.agent-user-message')).toBeInTheDocument();
    expect(container.querySelector('.agent-assistant-turn')).not.toBeInTheDocument();
  });

  it('shows the stopping transition while the terminal receipt is pending', () => {
    const sessionId = 'session-1';
    const turnId = 'turn-1';
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [userMessage(sessionId, turnId)],
      liveEvents: [],
      lastSequence: 0,
      resumeToken: '',
      status: 'busy',
      partial: true,
    });
    useAgentLiveStore.getState().applyEvents(sessionId, [
      agentEventFixture(1, 'status_changed', { status: 'aborting' }),
    ]);

    render(<AgentTurn sessionId={sessionId} turnId={turnId} onApprovalDecision={() => {}} />);

    expect(screen.getAllByText('正在停止').length).toBeGreaterThan(0);
    expect(screen.getByText('正在取消当前模型与工具执行。')).toBeInTheDocument();
  });

  it('labels a tool-only terminal turn as completed and an aborted turn as stopped', () => {
    const sessionId = 'session-1';
    const toolTurnId = 'turn-tool-only';
    const stoppedTurnId = 'turn-stopped';
    const toolUser = {
      ...userMessage(sessionId, toolTurnId),
      id: 'tool-only-user',
      blocks: [{
        ...userMessage(sessionId, toolTurnId).blocks[0]!,
        id: 'tool-only-user:text',
        data: { text: '运行工具' },
      }],
    };
    const stoppedUser = {
      ...userMessage(sessionId, stoppedTurnId),
      id: 'stopped-user',
      blocks: [{
        ...userMessage(sessionId, stoppedTurnId).blocks[0]!,
        id: 'stopped-user:text',
        data: { text: '停止工具' },
      }],
    };
    const stoppedAssistant = {
      ...assistantMessage(sessionId, stoppedTurnId, '已停止。', 4),
      status: 'aborted' as const,
      blocks: assistantMessage(sessionId, stoppedTurnId, '已停止。', 4).blocks.map(
        (block) => ({ ...block, status: 'aborted' as const }),
      ),
    };
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [toolUser, stoppedUser, stoppedAssistant],
      liveEvents: [
        {
          ...agentEventFixture(1, 'tool_started', {
            toolCallId: 'tool-only-call',
            toolName: 'bash',
            summary: '运行工具',
          }),
          turnId: toolTurnId,
        },
        {
          ...agentEventFixture(2, 'tool_finished', {
            toolCallId: 'tool-only-call',
            toolName: 'bash',
            result: { summary: '完成' },
          }),
          turnId: toolTurnId,
        },
      ],
      lastSequence: 2,
      resumeToken: `${sessionId}:2`,
      status: 'idle',
    });

    const projection = useAgentLiveStore.getState().projections[sessionId];
    expect(agentTurnMarkerKind(projection, toolTurnId)).toBe('complete');
    expect(agentTurnMarkerKind(projection, stoppedTurnId)).toBe('aborted');
  });

  it('shows persisted Room posts in participant Sessions while ordinary Sessions avoid duplication', () => {
    const sessionId = 'session-1';
    const currentTurnId = '831ba902-637d-48aa-b92f-2b1c32c4c969';
    const roomTurnId = 'room-root:98459a93-e95c-4e9b-9054-693131c2af66';
    const current = assistantMessage(sessionId, currentTurnId, '当前 Session 的真实回答。', 100);
    const roomPost: UiAgentMessage = {
      ...assistantMessage(sessionId, roomTurnId, 'Room 公共交付不应复制到 Session。', 110),
      id: 'room-post:post-98459a93',
      blocks: [{
        id: 'room-post:post-98459a93:text',
        type: 'text',
        status: 'completed',
        presentationKind: 'markdown',
        data: {
          text: 'Room 公共交付不应复制到 Session。',
        },
        source: { kind: 'room_post', ref: 'post-98459a93' },
        visibility: 'room_post',
      }],
    };
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [current, roomPost],
      liveEvents: [],
      lastSequence: 1_101,
      resumeToken: `${sessionId}:1101`,
      status: 'idle',
    });

    const projection = useAgentLiveStore.getState().projections[sessionId];
    expect(projection.messagesById[roomPost.id]).toBeDefined();
    expect(visibleAgentTurnIds(projection)).toEqual([currentTurnId]);
    expect(visibleAgentTurnIds(projection, true)).toEqual([currentTurnId, roomTurnId]);

    const { container, rerender } = render(
      <AgentTurn sessionId={sessionId} turnId={roomTurnId} onApprovalDecision={() => {}} />,
    );
    expect(container.querySelector('.agent-assistant-turn')).not.toBeInTheDocument();
    expect(screen.queryByText('Room 公共交付不应复制到 Session。')).not.toBeInTheDocument();

    rerender(
      <AgentTurn
        includeRoomPublicPosts
        sessionId={sessionId}
        turnId={roomTurnId}
        onApprovalDecision={() => {}}
      />,
    );
    expect(screen.getByText('Room 公共交付不应复制到 Session。')).toBeInTheDocument();
  });

  it('replaces a failed turn in place while keeping retry attempts auditable', () => {
    const sessionId = 'session-1';
    const rootTurnId = 'turn-root';
    const rootMessage: UiAgentMessage = {
      ...userMessage(sessionId, rootTurnId),
      id: 'root-user-message',
      clientMessageId: 'client-root',
      blocks: [{
        id: 'root-user-text',
        type: 'text',
        status: 'completed',
        presentationKind: 'markdown',
        data: { text: '只显示一次的原始要求' },
      }],
    };
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [rootMessage, failedAssistantMessage(sessionId, rootTurnId)],
      liveEvents: [],
      lastSequence: 1,
      resumeToken: `${sessionId}:1`,
      status: 'faulted',
    });
    useAgentLiveStore.getState().appendOptimistic(sessionId, {
      clientMessageId: 'client-retry',
      retryOfClientMessageId: 'client-root',
      text: '只显示一次的原始要求',
      attachments: [],
      nowMs: 2,
    });

    const projection = useAgentLiveStore.getState().projections[sessionId];
    expect(projection.turnOrder).toEqual([rootTurnId, 'local-turn:client-retry']);
    expect(visibleAgentTurnIds(projection)).toEqual(['local-turn:client-retry']);

    render(
      <AgentTurn
        sessionId={sessionId}
        turnId="local-turn:client-retry"
        onApprovalDecision={() => {}}
      />,
    );
    expect(screen.getAllByText('只显示一次的原始要求')).toHaveLength(1);
    expect(screen.queryByText('本轮未完成')).not.toBeInTheDocument();
    expect(screen.getByText('正在处理')).toBeInTheDocument();
  });

  it('shows only the latest attempt when a failed turn has sibling retries', () => {
    const sessionId = 'session-1';
    const rootTurnId = 'turn-root-siblings';
    const rootMessage: UiAgentMessage = {
      ...userMessage(sessionId, rootTurnId),
      id: 'root-sibling-user',
      clientMessageId: 'client-root-siblings',
      blocks: [{
        id: 'root-sibling-user-text',
        type: 'text',
        status: 'completed',
        presentationKind: 'markdown',
        data: { text: '只保留最新重试尝试' },
      }],
    };
    useAgentLiveStore.getState().hydrateSnapshot(sessionId, {
      messages: [rootMessage, failedAssistantMessage(sessionId, rootTurnId)],
      liveEvents: [],
      lastSequence: 1,
      resumeToken: `${sessionId}:1`,
      status: 'faulted',
    });
    useAgentLiveStore.getState().appendOptimistic(sessionId, {
      clientMessageId: 'client-retry-sibling-1',
      retryOfClientMessageId: 'client-root-siblings',
      text: '只保留最新重试尝试',
      attachments: [],
      nowMs: 2,
    });
    useAgentLiveStore.getState().appendOptimistic(sessionId, {
      clientMessageId: 'client-retry-sibling-2',
      retryOfClientMessageId: 'client-root-siblings',
      text: '只保留最新重试尝试',
      attachments: [],
      nowMs: 3,
    });

    const projection = useAgentLiveStore.getState().projections[sessionId]!;
    expect(visibleAgentTurnIds(projection)).toEqual(['local-turn:client-retry-sibling-2']);
  });
});

function imageBlock(data: Record<string, unknown>): UiAgentBlock {
  return {
    id: 'image-block',
    type: 'image',
    status: 'completed',
    presentationKind: 'image',
    data,
  };
}

function fileBlock(
  id: string,
  fileName: string,
  mediaId: string,
  sha256: string,
): UiAgentBlock {
  return {
    id,
    type: 'file',
    status: 'completed',
    presentationKind: 'file',
    data: {
      fileName,
      mediaId,
      sha256,
      mimeType: fileName.endsWith('.diff') ? 'text/x-diff' : 'text/plain',
      receiptUrl: `/api/agent/media/${mediaId}/content?sessionId=session:file-results`,
    },
  };
}

function failedAssistantMessage(sessionId: string, turnId: string): UiAgentMessage {
  return {
    schemaVersion: 'rag-ime.agent-message.v1',
    id: 'failed-assistant-message',
    sessionId,
    turnId,
    role: 'assistant',
    status: 'failed',
    blocks: [{
      id: 'failed-assistant-error',
      type: 'error',
      status: 'failed',
      presentationKind: 'error',
      data: { message: '404 Model "gpt-5.6-luna" is not supported by any configured account in this group' },
    }],
    attachments: [],
    citations: [],
    createdAtMs: 1,
    completedAtMs: 2,
  };
}

function userMessage(sessionId: string, turnId: string): UiAgentMessage {
  return {
    schemaVersion: 'rag-ime.agent-message.v1',
    id: 'failed-turn-user-message',
    sessionId,
    turnId,
    role: 'user',
    status: 'completed',
    blocks: [{
      id: 'failed-turn-user-text',
      type: 'text',
      status: 'completed',
      presentationKind: 'markdown',
      data: { text: '请检查失败状态' },
    }],
    attachments: [],
    citations: [],
    createdAtMs: 0,
    completedAtMs: 1,
  };
}

function assistantMessage(sessionId: string, turnId: string, value: string, createdAtMs: number): UiAgentMessage {
  return {
    schemaVersion: 'rag-ime.agent-message.v1',
    id: `${turnId}:assistant`,
    sessionId,
    turnId,
    role: 'assistant',
    status: 'completed',
    blocks: [{
      id: `${turnId}:assistant:text`,
      type: 'text',
      status: 'completed',
      presentationKind: 'markdown',
      data: { text: value },
    }],
    attachments: [],
    citations: [],
    createdAtMs,
    completedAtMs: createdAtMs,
  };
}
