import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import type { ObservationEventV1 } from '@/contracts/generated/observation-event.v1';
import type { EvalScheduleListV1 } from '@/contracts/generated/eval-schedule-list.v1';
import type { EvalSuiteListV1 } from '@/contracts/generated/eval-suite-list.v1';
import type { SandboxRunV1 } from '@/contracts/generated/sandbox-run.v1';
import type { ControlRequest } from '@/platform/transport';
import type { ControlPathId } from '@/platform/routes';
import { MockControlTransport } from '@/test/mock-transport';
import observabilityStylesheet from './observability.css?raw';
import { ObservabilityFeature } from '.';

const SANDBOX_RUNS_PATH_ID = 'observability.sandboxRuns.list' as ControlPathId;

afterEach(cleanup);

describe('ObservabilityFeature', () => {
  it('keeps historical running events distinct from the current execution state', async () => {
    renderFeature(observationTransport({ trace: canonicalTraceResponse() }));
    const timeline = await screen.findByRole('list', { name: '运行记录事件' });
    await screen.findByText('Canonical spans · 2');

    expect(within(timeline).getByText('当时运行中')).toBeInTheDocument();
    expect(within(timeline).queryByText('运行中', { exact: true })).not.toBeInTheDocument();
    const overview = document.querySelector('.observation-pulse') as HTMLElement;
    expect(overview).toHaveTextContent('执行中记录 1');
    expect(overview).not.toHaveTextContent('进行中 1');
  });

  it('distinguishes event dates when different days have the same clock time', async () => {
    const first = observationEvent({ sequence: 1, category: 'tool', phase: 'tool_finished', status: 'completed', summary: '前一天的工具记录' });
    const second = observationEvent({ sequence: 2, category: 'tool', phase: 'tool_finished', status: 'completed', summary: '后一天的工具记录' });
    first.createdAtMs = Date.UTC(2026, 8, 1, 8, 4, 0);
    second.createdAtMs = Date.UTC(2026, 8, 2, 8, 4, 0);
    renderFeature(observationTransport({ items: [second, first] }));
    const timeline = await screen.findByRole('list', { name: '运行记录事件' });
    const firstTime = within(timeline).getByText('前一天的工具记录').closest('li')?.querySelector('time');
    const secondTime = within(timeline).getByText('后一天的工具记录').closest('li')?.querySelector('time');

    expect(firstTime).toHaveAttribute('datetime', new Date(first.createdAtMs).toISOString());
    expect(secondTime).toHaveAttribute('datetime', new Date(second.createdAtMs).toISOString());
    expect(firstTime?.textContent).not.toBe(secondTime?.textContent);
  });

  it('does not present a truncated page count as the total history', async () => {
    renderFeature(observationTransport({ truncated: true, total: 2 }));
    await screen.findByRole('list', { name: '运行记录事件' });

    expect(screen.getByText('当前显示最近 2 条')).toBeInTheDocument();
    expect(screen.queryByText('最近 2 / 共 2')).not.toBeInTheDocument();
  });

  it('retains the last snapshot and selected evidence when a refresh fails', async () => {
    const user = userEvent.setup();
    let failRead = false;
    const transport = observationTransport({
      beforeSnapshot: () => { if (failRead) throw new Error('snapshot temporarily unavailable'); },
    });
    renderFeature(transport);
    const timeline = await screen.findByRole('list', { name: '运行记录事件' });
    await screen.findByText('Canonical spans · 2');
    failRead = true;
    await user.click(screen.getByRole('button', { name: '刷新' }));

    expect(await screen.findByText('刷新未完成，保留上次记录')).toBeInTheDocument();
    expect(timeline).toBeInTheDocument();
    expect(screen.getByText('Canonical spans · 2')).toBeInTheDocument();
    failRead = false;
    await user.click(screen.getByRole('button', { name: '重试读取' }));
    await waitFor(() => expect(screen.queryByText('刷新未完成，保留上次记录')).not.toBeInTheDocument());
  });

  it('requests the canonical trace detail only for the selected trace and progressively reveals measured spans and evidence', async () => {
    const user = userEvent.setup();
    const transport = observationTransport({
      trace: canonicalTraceResponse({ truncated: false }),
    });
    renderFeature(transport);

    const timeline = await screen.findByRole('list', { name: '运行记录事件' });
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'observability.trace.get'
    ))).toBe(true));
    expect(transport.requests.find(({ request }) => request.pathId === 'observability.trace.get')?.request).toEqual({
      pathId: 'observability.trace.get',
      params: { traceId: 'trace:turn:test' },
      query: { limit: 500 },
      responseContract: 'observability-trace-get.v1',
      signal: expect.any(AbortSignal),
    });
    expect(within(timeline).getByText('记忆工具 已完成')).toBeInTheDocument();

    const spansDisclosure = screen.getByText('Canonical spans · 2').closest('summary') as HTMLElement;
    expect(spansDisclosure).toHaveAttribute('aria-expanded', 'false');
    await user.click(spansDisclosure);
    expect(screen.getByText('0 ms')).toBeInTheDocument();
    expect(screen.getByText('未记录')).toBeInTheDocument();
    expect(screen.getByText(/duration_not_recorded/)).toBeInTheDocument();
    expect(screen.getByText('父阶段 · span:turn:test')).toBeInTheDocument();
    expect(screen.getByText('openai')).toBeInTheDocument();
    expect(screen.getByText('42')).toBeInTheDocument();

    const evidenceDisclosure = screen.getByText('证据 · 2').closest('summary') as HTMLElement;
    await user.click(evidenceDisclosure);
    expect(screen.getAllByText(/retrieval_output/).length).toBeGreaterThan(0);
    expect(screen.getByText('source:manual')).toBeInTheDocument();
    expect(screen.getByText(/-0\.25/)).toBeInTheDocument();
    expect(screen.getByText(/1 → 2/)).toBeInTheDocument();

    const relations = screen.getByRole('region', { name: 'Trace 关联' });
    expect(within(relations).getAllByRole('definition')[0]).toHaveTextContent('session-a');
    expect(within(relations).getByText('work:trace-test')).toBeInTheDocument();
    expect(within(relations).getByRole('link', { name: 'trace:parent:test' })).toHaveAttribute(
      'href', '#/observability?traceId=trace%3Aparent%3Atest',
    );
    expect(within(relations).getByRole('link', { name: 'trace:retry:test' })).toHaveAttribute(
      'href', '#/observability?traceId=trace%3Aretry%3Atest',
    );
    expect(within(relations).getByRole('link', { name: '返回原 Session' })).toHaveAttribute(
      'href', '#/agent?session=session-a',
    );
    expect(within(relations).getByRole('link', { name: '检查原 Turn 上下文' })).toHaveAttribute(
      'href', '#/context-debug?sessionId=session-a&turnId=turn-a',
    );

    const artifactDisclosure = screen.getByText('产物 · 1').closest('summary') as HTMLElement;
    await user.click(artifactDisclosure);
    expect(screen.getByText('artifact:report')).toBeInTheDocument();
    expect(screen.getByText(/1\.0 KB/)).toBeInTheDocument();
  });

  it('opens a source-owned canonical trace from the URL when the observation journal has no copy', async () => {
    const user = userEvent.setup();
    const traceId = 'trace:prediction:input-session:request-7';
    const sourceTrace = canonicalTraceResponse();
    sourceTrace.traceId = traceId;
    sourceTrace.trace.traceId = traceId;
    sourceTrace.trace.sourceKind = 'rime_prediction';
    sourceTrace.trace.spans[0].name = 'input.prediction';
    sourceTrace.projectionSource = 'source_adapter';
    sourceTrace.observationWindow = {
      firstSequence: 0,
      lastSequence: 0,
      resumeToken: `source:${traceId}`,
      nextBeforeSequence: null,
    };
    const transport = observationTransport({ items: [], trace: sourceTrace });

    renderFeature(transport, `/observability?traceId=${encodeURIComponent(traceId)}`);

    expect(await screen.findByText('这是来源系统直接提供的 Trace')).toBeInTheDocument();
    expect(screen.getAllByText(new RegExp(traceId))).toHaveLength(2);
    expect(screen.getByText('来源 Trace')).toBeInTheDocument();
    await user.click(screen.getByText('Canonical spans · 2').closest('summary') as HTMLElement);
    expect(screen.getByText('input.prediction')).toBeInTheDocument();
    expect(transport.requests.find(({ request }) => request.pathId === 'observability.trace.get')?.request.params).toEqual({ traceId });
  });

  it('names a durable canonical trace without pretending it came from the source adapter', async () => {
    const traceId = 'trace:vertical-agent:durable-7';
    const durableTrace = canonicalTraceResponse();
    durableTrace.traceId = traceId;
    durableTrace.trace.traceId = traceId;
    durableTrace.trace.sourceKind = 'vertical_agent';
    durableTrace.projectionSource = 'trace_store';
    durableTrace.observationWindow = {
      firstSequence: 0,
      lastSequence: 0,
      resumeToken: `trace-store:${traceId}`,
      nextBeforeSequence: null,
    };
    const transport = observationTransport({ items: [], trace: durableTrace });

    renderFeature(transport, `/observability?traceId=${encodeURIComponent(traceId)}`);

    expect(await screen.findByText('这是本机持久化的标准 Trace')).toBeInTheDocument();
    expect(screen.getByText('已保存 Trace')).toBeInTheDocument();
    expect(screen.queryByText('这是来源系统直接提供的 Trace')).not.toBeInTheDocument();
  });

  it('keeps snapshot events visible when the canonical trace request fails', async () => {
    const transport = observationTransport({
      trace: () => Promise.reject(new Error('trace detail unavailable')),
    });
    renderFeature(transport, '/observability?traceId=trace%3Aturn%3Atest');

    const timeline = await screen.findByRole('list', { name: '运行记录事件' });
    expect(within(timeline).getByText('记忆工具 已完成')).toBeInTheDocument();
    expect(await screen.findByText('这次流程的标准 Trace 暂时不可用')).toBeInTheDocument();
    expect(within(timeline).getByText('伙伴 正在分析')).toBeInTheDocument();
  });

  it('renders an expired observation as an explicit terminal failure state', async () => {
    const transport = observationTransport({
      items: [observationEvent({
        sequence: 4,
        category: 'memory',
        phase: 'expired',
        status: 'expired',
        summary: '记忆整理租约已过期',
      })],
    });

    renderFeature(transport, '/observability?traceId=trace%3Aturn%3Atest');

    const timeline = await screen.findByRole('list', { name: '运行记录事件' });
    expect(within(timeline).getByText('记忆整理租约已过期')).toBeInTheDocument();
    expect(within(timeline).getByText('已过期')).toHaveAttribute('data-tone', 'danger');
  });

  it('labels a truncated canonical trace as a degraded local window', async () => {
    const transport = observationTransport({
      trace: canonicalTraceResponse({ truncated: true }),
    });
    renderFeature(transport);

    expect(await screen.findByText('局部窗口，状态已降级')).toBeInTheDocument();
    expect(screen.getByText(/不能据此断言完整流程已经结束/)).toBeInTheDocument();
  });

  it('loads Eval summaries with explicit truth authority and never invents accuracy', async () => {
    const transport = observationTransport({ evals: evalListResponse() });
    renderFeature(transport);

    expect(await screen.findByText('Ground truth · 人工/冻结真值')).toBeInTheDocument();
    expect(screen.getByText('AI Judge · 模型估计')).toBeInTheDocument();
    expect(screen.getByText('指标 authority：ground_truth 真值')).toBeInTheDocument();
    expect(screen.getByText('指标 authority：ai_judge_estimate AI 估计')).toBeInTheDocument();
    expect(screen.getByText('Suite：sgg · fixture-v2')).toBeInTheDocument();
    expect(screen.queryByText(/accuracy/i)).not.toBeInTheDocument();
    expect(transport.requests.find(({ request }) => request.pathId === 'observability.evals.list')?.request).toEqual({
      pathId: 'observability.evals.list',
      query: { traceId: 'trace:turn:test', limit: 100 },
      responseContract: 'observability-eval-list.v1',
      signal: expect.any(AbortSignal),
    });
  });

  it('renders the plugin-owned SandboxRun to Trace to EvalRun chain with explicit policy facts', async () => {
    const transport = observationTransport({ sandboxRuns: sandboxRunListResponse() });
    renderFeature(transport);

    const panel = await screen.findByRole('region', { name: '垂直 Agent SandboxRun 链路' });
    const detail = within(panel).getByRole('tabpanel');
    expect(within(detail).getByText('sgg')).toBeInTheDocument();
    expect(within(detail).getByText('sandbox:sgg:demo')).toBeInTheDocument();
    expect(within(detail).getAllByText('已完成')).toHaveLength(2);
    const policy = within(detail).getByLabelText('SandboxRun 策略');
    expect(policy).toHaveTextContent('network blocked');
    expect(policy).toHaveTextContent('mutation read_only');
    expect(policy).toHaveTextContent('productionWriteBlocked true');
    expect(within(detail).getByText('eval:sgg:demo')).toBeInTheDocument();
    expect(within(detail).getByRole('link', { name: 'trace:sgg:demo' })).toHaveAttribute(
      'href', '#/observability?traceId=trace%3Asgg%3Ademo',
    );
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === SANDBOX_RUNS_PATH_ID
    ))).toBe(true));
    expect(transport.requests.find(({ request }) => request.pathId === SANDBOX_RUNS_PATH_ID)?.request).toEqual({
      pathId: 'observability.sandboxRuns.list',
      query: { limit: 20 },
      signal: expect.any(AbortSignal),
    });
  });

  it('projects a vertical App through frozen candidate, Host sandbox, multiple Eval receipts, and a truthful pending decision', async () => {
    const run = sandboxRun();
    run.evalRunIds = ['eval:ground-truth', 'eval:judge'];
    const transport = observationTransport({
      sandboxRuns: sandboxRunListResponse([run]),
      evals: (request: ControlRequest) => request.query?.traceId === 'trace:sgg:demo' ? {
        schemaVersion: 'rag-ime.observability-eval-list.v1',
        traceId: 'trace:sgg:demo',
        total: 2,
        truncated: false,
        items: [
          {
            evalRunId: 'eval:ground-truth', mode: 'ground_truth', metricAuthority: 'ground_truth',
            truthStatus: 'frozen', datasetId: 'sgg:fixture-v2', labelRevision: 'labels:1',
            evaluatorDisplayName: '确定性 Eval', suiteBinding: { suiteId: 'sgg', suiteRevision: 'fixture-v2' },
            metrics: { evidenceRecall: 0.92 }, status: 'completed', createdAtMs: 3, updatedAtMs: 3,
          },
          {
            evalRunId: 'eval:judge', mode: 'ai_judge', metricAuthority: 'ai_judge_estimate',
            truthStatus: 'none', datasetId: 'sgg:fixture-v2', labelRevision: 'rubric:1',
            evaluatorDisplayName: 'AI Judge', suiteBinding: { suiteId: 'sgg', suiteRevision: 'fixture-v2' },
            metrics: { qualityEstimate: 0.81 }, status: 'completed', createdAtMs: 4, updatedAtMs: 4,
          },
        ],
      } : evalListResponse(),
    });
    renderFeature(transport);

    const panel = await screen.findByRole('region', { name: '垂直 Agent SandboxRun 链路' });
    expect(within(panel).getByText('垂直 App 多重 Eval 试验场')).toBeInTheDocument();
    expect(within(panel).getByText('候选配置')).toBeInTheDocument();
    expect(within(panel).getByText('Host 沙盒测试')).toBeInTheDocument();
    expect(within(panel).getByText('多重 Eval')).toBeInTheDocument();
    expect(within(panel).getByText('Keep / Reject')).toBeInTheDocument();
    expect(await within(panel).findByText('2 / 2 份回执')).toBeInTheDocument();
    expect(within(panel).getByText('evidenceRecall')).toBeInTheDocument();
    expect(within(panel).getByText('0.92')).toBeInTheDocument();
    expect(within(panel).getByText('qualityEstimate')).toBeInTheDocument();
    expect(within(panel).getByText(/AI Judge · AI 估计/)).toBeInTheDocument();
    expect(within(panel).getByText('等待 verification / promotion 回执')).toBeInTheDocument();
    expect(within(panel).getByText(/晋升回执前不会 Keep/)).toBeInTheDocument();
    expect(within(panel).queryByText('Keep 当前候选')).not.toBeInTheDocument();
    expect(within(panel).queryByText('Trace → 修复 → 复验')).not.toBeInTheDocument();
  });

  it('reveals the Trace repair recheck branch only after a SandboxRun failure', async () => {
    const failedRun = { ...sandboxRun(), status: 'failed' as const, evalRunIds: [] };
    const transport = observationTransport({ sandboxRuns: sandboxRunListResponse([failedRun]) });
    renderFeature(transport);

    const panel = await screen.findByRole('region', { name: '垂直 Agent SandboxRun 链路' });
    expect(await within(panel).findByText('候选失败，等待 Trace / 评价回执')).toBeInTheDocument();
    expect(within(panel).getByText('等待 verification / promotion 回执')).toBeInTheDocument();
    expect(within(panel).queryByText('Reject 当前候选')).not.toBeInTheDocument();
    expect(within(panel).getByText('Trace → 修复 → 复验')).toBeInTheDocument();
    expect(within(panel).getByRole('link', { name: '查看 trace:sgg:demo' })).toHaveAttribute(
      'href', '#/observability?traceId=trace%3Asgg%3Ademo',
    );
  });

  it('loads Eval only for the selected latest experiment and switches it with keyboard tabs', async () => {
    const user = userEvent.setup();
    const older = {
      ...sandboxRun(),
      sandboxRunId: 'sandbox:sgg:older',
      traceIds: ['trace:sgg:older'],
      evalRunIds: ['eval:sgg:older'],
      updatedAtMs: 20,
    };
    const latest = {
      ...sandboxRun(),
      sandboxRunId: 'sandbox:sgg:latest',
      traceIds: ['trace:sgg:latest'],
      evalRunIds: ['eval:sgg:latest'],
      updatedAtMs: 40,
    };
    const transport = observationTransport({
      sandboxRuns: sandboxRunListResponse([older, latest]),
      evals: (request: ControlRequest) => ({
        ...evalListResponse(),
        traceId: String(request.query?.traceId ?? ''),
        items: [],
        total: 0,
      }),
    });
    renderFeature(transport);

    const panel = await screen.findByRole('region', { name: '垂直 Agent SandboxRun 链路' });
    const tabs = within(panel).getAllByRole('tab');
    expect(tabs).toHaveLength(2);
    expect(tabs[0]).toHaveTextContent('sandbox:sgg:latest');
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true');
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'observability.evals.list' && request.query?.traceId === 'trace:sgg:latest'
    ))).toBe(true));
    expect(transport.requests.some(({ request }) => (
      request.pathId === 'observability.evals.list' && request.query?.traceId === 'trace:sgg:older'
    ))).toBe(false);

    tabs[0].focus();
    await user.keyboard('{ArrowRight}');
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'observability.evals.list' && request.query?.traceId === 'trace:sgg:older'
    ))).toBe(true));
    expect(tabs[1]).toHaveAttribute('aria-selected', 'true');
  });

  it('keeps experiment identities inside a responsive selected-detail layout', () => {
    expect(observabilityStylesheet).toContain(
      "grid-template-columns: minmax(180px, .34fr) minmax(0, 1.66fr);",
    );
    expect(observabilityStylesheet).toContain(
      ".observation-sandbox-runs__picker small {\n  overflow: hidden;\n  text-overflow: ellipsis;\n  white-space: nowrap;",
    );
    expect(observabilityStylesheet).toContain(
      "grid-template-columns: repeat(4, minmax(0, 1fr));",
    );
    expect(observabilityStylesheet).toContain(
      ".observation-app-eval__flow { grid-template-columns: minmax(0, 1fr); }",
    );
  });

  it('explains plugin enablement when no SandboxRun exists and keeps direct run absent', async () => {
    const transport = observationTransport({ sandboxRuns: sandboxRunListResponse([]) });
    renderFeature(transport);

    const panel = await screen.findByRole('region', { name: '垂直 Agent SandboxRun 链路' });
    expect(within(panel).getByText('先在 App Center 启用 Vertical Agent Sandbox，再从新 Session 运行 SGG。')).toBeInTheDocument();
    expect(within(panel).queryByRole('button', { name: '立即自测' })).not.toBeInTheDocument();
    expect(screen.getByText('Trace Eval')).toBeInTheDocument();
  });

  it('keeps the Trace and Eval area available when SandboxRun listing fails', async () => {
    const transport = observationTransport({
      sandboxRuns: () => Promise.reject(new Error('sandbox runs unavailable')),
    });
    renderFeature(transport);

    const panel = await screen.findByRole('region', { name: '垂直 Agent SandboxRun 链路' });
    expect(within(panel).getByText('SandboxRun 暂时不可用')).toBeInTheDocument();
    expect(within(panel).getByText('Trace 与 Eval 仍可查看；请刷新重试。')).toBeInTheDocument();
    expect(screen.getByText('Trace Eval')).toBeInTheDocument();
  });

  it('submits a human evidence set once per canonical evidenceId and refreshes Eval results', async () => {
    const user = userEvent.setup();
    let listCalls = 0;
    const traceResponse = canonicalTraceResponse();
    traceResponse.trace.evidence.push({ ...traceResponse.trace.evidence[0], evidenceStage: 'generation' });
    const transport = observationTransport({
      trace: traceResponse,
      evals: () => {
        listCalls += 1;
        return listCalls === 1 ? evalListResponse() : evalListResponse('eval:human:refreshed');
      },
      evidenceEval: (request: ControlRequest) => ({
        ...evalRunResponse(),
        evalRunId: 'eval:human:submitted',
        truth: {
          status: 'human' as const,
          datasetId: String((request.body as Record<string, unknown>).datasetId),
          labelRevision: String((request.body as Record<string, unknown>).labelRevision),
        },
      }),
    });
    renderFeature(transport);

    const disclosure = await screen.findByText('人工 evidence-set 标注');
    await user.click(disclosure.closest('summary') as HTMLElement);

    const checkboxes = screen.getAllByRole('checkbox');
    expect(checkboxes).toHaveLength(2);
    expect(checkboxes.every((checkbox) => !(checkbox as HTMLInputElement).checked)).toBe(true);
    await user.click(checkboxes[0]);
    await user.clear(screen.getByRole('textbox', { name: 'datasetId' }));
    await user.type(screen.getByRole('textbox', { name: 'datasetId' }), 'dataset:reviewed');
    await user.clear(screen.getByRole('textbox', { name: 'labelRevision' }));
    await user.type(screen.getByRole('textbox', { name: 'labelRevision' }), 'labels:7');
    await user.type(
      screen.getByRole('textbox', { name: '额外 required evidenceId' }),
      'knowledge:missing\nknowledge:missing',
    );
    await user.click(screen.getByRole('button', { name: '运行人工 Eval' }));

    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'observability.evals.evidence.run')).toHaveLength(1));
    expect(transport.requests.find(({ request }) => request.pathId === 'observability.evals.evidence.run')?.request).toMatchObject({
      pathId: 'observability.evals.evidence.run',
      body: {
        schemaVersion: 'rag-ime.observability-evidence-eval-request.v1',
        traceId: 'trace:turn:test',
        requiredEvidenceIds: ['evidence:manual', 'knowledge:missing'],
        datasetId: 'dataset:reviewed',
        labelRevision: 'labels:7',
        truthKind: 'human',
      },
      responseContract: 'eval-run.v1',
    });
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'observability.evals.list')).toHaveLength(2));
    expect(screen.getByText('人工 Eval 已提交')).toBeInTheDocument();
  });

  it('runs the default Luna Max AI Judge and labels the receipt as an estimate', async () => {
    const user = userEvent.setup();
    const transport = observationTransport({
      aiJudge: (request: ControlRequest) => {
        expect(request.body).toEqual({ traceId: 'trace:turn:test' });
        return aiJudgeRunResponse();
      },
    });
    renderFeature(transport);

    await user.click(await screen.findByRole('button', { name: '运行 Luna Max Eval' }));

    await waitFor(() => expect(transport.requests.filter(({ request }) => (
      request.pathId === 'observability.evals.aiJudge.run'
    ))).toHaveLength(1));
    expect(transport.requests.find(({ request }) => request.pathId === 'observability.evals.aiJudge.run')?.request).toMatchObject({
      pathId: 'observability.evals.aiJudge.run',
      body: { traceId: 'trace:turn:test' },
      responseContract: 'eval-run.v1',
    });
    expect(await screen.findByText('Luna Max AI Judge 已提交')).toBeInTheDocument();
    expect(screen.getByText(/AI 评审估计：ai_judge_estimate/)).toBeInTheDocument();
  });

  it('refreshes a building Trace and its Eval list when a matching observation arrives', async () => {
    const user = userEvent.setup();
    let traceResponse = canonicalTraceResponse({ truncated: true });
    let traceCalls = 0;
    let evalCalls = 0;
    const transport = observationTransport({
      trace: () => {
        traceCalls += 1;
        return traceResponse;
      },
      evals: () => {
        evalCalls += 1;
        return evalListResponse();
      },
    });
    renderFeature(transport);

    expect(await screen.findByText('局部窗口，状态已降级')).toBeInTheDocument();
    await waitFor(() => expect(transport.subscriptionCalls).toHaveLength(1));
    await waitFor(() => {
      expect(traceCalls).toBeGreaterThan(0);
      expect(evalCalls).toBeGreaterThan(0);
    });
    const previousTraceCalls = traceCalls;
    const previousEvalCalls = evalCalls;

    const completed = canonicalTraceResponse({ truncated: false });
    completed.trace.spans[0].name = 'agent.turn.completed';
    traceResponse = completed;
    expect(transport.emit('observability.events', observationEvent({
      sequence: 3,
      category: 'agent',
      phase: 'turn_completed',
      status: 'completed',
      summary: 'Agent 已完成',
    }))).toBe(1);

    await waitFor(() => {
      expect(traceCalls).toBeGreaterThan(previousTraceCalls);
      expect(evalCalls).toBeGreaterThan(previousEvalCalls);
    });
    expect(await screen.findByText('Canonical spans · 2')).toBeInTheDocument();
    await user.click(screen.getByText('Canonical spans · 2').closest('summary') as HTMLElement);
    expect(screen.getByText('agent.turn.completed')).toBeInTheDocument();
  });

  it('refreshes every active observability query from the top Refresh action', async () => {
    const user = userEvent.setup();
    const counts = new Map<string, number>();
    const count = (pathId: string) => {
      const next = (counts.get(pathId) ?? 0) + 1;
      counts.set(pathId, next);
      return pathId === 'observability.snapshot'
        ? {
            schemaVersion: 'rag-ime.observation-snapshot.v1' as const,
            generatedAtMs: 1_000,
            firstSequence: 1,
            lastSequence: 2,
            resumeToken: 'observation:2',
            truncated: false,
            filters: {},
            counts: { total: 2, byCategory: {}, byStatus: {} },
            items: [
              observationEvent({ sequence: 2, category: 'tool', phase: 'tool_finished', status: 'completed', summary: 'ime.memory 已完成' }),
              observationEvent({ sequence: 1, category: 'agent', phase: 'status_changed', status: 'running', summary: 'Agent 正在分析' }),
            ],
          }
        : undefined;
    };
    const transport = observationTransport({
      trace: () => { count('observability.trace.get'); return canonicalTraceResponse(); },
      evals: () => { count('observability.evals.list'); return evalListResponse(); },
      schedules: () => {
        count('observability.evalSchedules.list');
        return evalScheduleListResponse([evalSchedule('eval-schedule:sgg')]);
      },
      scheduleRuns: () => {
        count('observability.evalSchedule.runs');
        return evalScheduleRunListResponse('eval-schedule:sgg');
      },
    });
    renderFeature(transport);

    const panel = await screen.findByRole('region', { name: '周期 Eval' });
    await within(panel).findByRole('list', { name: '周期 Eval 执行记录' });
    await screen.findByText('Ground truth · 人工/冻结真值');
    const previous = new Map(counts);

    await user.click(screen.getByRole('button', { name: '刷新' }));

    await waitFor(() => {
      const snapshotCalls = transport.requests.filter(({ request }) => request.pathId === 'observability.snapshot').length;
      for (const pathId of [
        'observability.trace.get',
        'observability.evals.list',
        'observability.evalSchedules.list',
        'observability.evalSchedule.runs',
      ]) {
        expect(counts.get(pathId) ?? 0).toBeGreaterThan(previous.get(pathId) ?? 0);
      }
      expect(snapshotCalls).toBeGreaterThan(1);
    });
  });

  it('does not show a completed human Eval from an old Trace after selection changes', async () => {
    const user = userEvent.setup();
    let resolveOldEval: ((value: ReturnType<typeof evalRunResponse>) => void) | undefined;
    const pendingOldEval = new Promise<ReturnType<typeof evalRunResponse>>((resolve) => {
      resolveOldEval = resolve;
    });
    const other = observationEvent({
      sequence: 3,
      category: 'agent',
      phase: 'status_changed',
      status: 'running',
      summary: '另一个 Trace 正在分析',
    });
    other.traceId = 'trace:other:test';
    other.eventId = 'observation:test:other';
    const transport = observationTransport({
      items: [
        observationEvent({ sequence: 2, category: 'tool', phase: 'tool_finished', status: 'completed', summary: 'ime.memory 已完成' }),
        observationEvent({ sequence: 1, category: 'agent', phase: 'status_changed', status: 'running', summary: 'Agent 正在分析' }),
        other,
      ],
      evidenceEval: () => pendingOldEval,
    });
    renderFeature(transport, '/observability?traceId=trace%3Aturn%3Atest');

    const timeline = await screen.findByRole('list', { name: '运行记录事件' });
    await user.click((await screen.findByText('人工 evidence-set 标注')).closest('summary') as HTMLElement);
    await user.click(screen.getAllByRole('checkbox')[0]);
    await user.click(screen.getByRole('button', { name: '运行人工 Eval' }));
    await waitFor(() => expect(transport.requests.filter(({ request }) => (
      request.pathId === 'observability.evals.evidence.run'
    ))).toHaveLength(1));

    await user.click(within(timeline).getByText('另一个 Trace 正在分析'));
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'observability.trace.get'
      && request.params?.traceId === 'trace:other:test'
    ))).toBe(true));

    resolveOldEval?.({
      ...evalRunResponse(),
      evalRunId: 'eval:human:old-trace',
      traceIds: ['trace:turn:test'],
    });
    await waitFor(() => expect(screen.queryByText('eval:human:old-trace')).not.toBeInTheDocument());
    expect(screen.queryByText('人工 Eval 已提交')).not.toBeInTheDocument();
  });

  it('does not submit human Eval for truncated or incomplete canonical traces and keeps errors local', async () => {
    const user = userEvent.setup();
    const transport = observationTransport({
      trace: canonicalTraceResponse({ truncated: true }),
      evals: evalListResponse(),
      evidenceEval: () => Promise.reject(new Error('save failed')),
    });
    renderFeature(transport);

    await user.click((await screen.findByText('人工 evidence-set 标注')).closest('summary') as HTMLElement);
    const button = screen.getByRole('button', { name: '运行人工 Eval' });
    expect(button).toBeDisabled();
    expect(screen.getByText('Trace 仍是截断窗口，需等完整 Trace 后再标注。')).toBeInTheDocument();
    expect(screen.getByText('Ground truth · 人工/冻结真值')).toBeInTheDocument();
    expect(screen.queryByText(/accuracy/i)).not.toBeInTheDocument();
  });

  it('lists local Eval schedules, reads their run receipts, and creates a revision-pinned plan', async () => {
    const user = userEvent.setup();
    let created = false;
    const transport = observationTransport({
      schedules: () => evalScheduleListResponse(created
        ? [evalSchedule('eval-schedule:sgg'), evalSchedule('eval-schedule:merchant', {
            suiteId: 'zhanggui-wenshu', suiteRevision: 'fixture-v2', recurrenceKind: 'weekly', recurrenceInterval: 2,
          })]
        : [evalSchedule('eval-schedule:sgg')]),
      scheduleRuns: (request: ControlRequest) => evalScheduleRunListResponse(
        String(request.params?.scheduleId),
      ),
      scheduleCreate: (request: ControlRequest) => {
        created = true;
        const body = request.body as Record<string, unknown>;
        return {
          schemaVersion: 'rag-ime.eval-schedule-create.v1' as const,
          ok: true as const,
          schedule: evalSchedule('eval-schedule:merchant', {
            suiteId: String(body.suiteId),
            suiteRevision: String(body.suiteRevision),
            recurrenceKind: body.recurrenceKind as 'daily' | 'weekly',
            recurrenceInterval: Number(body.recurrenceInterval),
            maxRuns: Number(body.maxRuns),
            initialDueAtMs: Number(body.nextDueAtMs),
            nextDueAtMs: Number(body.nextDueAtMs),
          }),
        };
      },
    });
    renderFeature(transport);

    const panel = await screen.findByRole('region', { name: '周期 Eval' });
    expect(within(panel).getByRole('button', { name: 'sgg fixture-v2，已计划' })).toBeInTheDocument();
    await waitFor(() => expect(transport.requests.some(({ request }) => (
      request.pathId === 'observability.evalSchedule.runs'
      && request.params?.scheduleId === 'eval-schedule:sgg'
    ))).toBe(true));
    expect(await within(panel).findByRole('list', { name: '周期 Eval 执行记录' })).toHaveTextContent('eval:sgg:1');
    expect(within(panel).getByRole('link', { name: 'trace:sgg:1' })).toHaveAttribute(
      'href', '#/observability?traceId=trace%3Asgg%3A1',
    );

    await user.click(within(panel).getByText('新建周期 Eval').closest('summary') as HTMLElement);
    await user.selectOptions(within(panel).getByRole('combobox', { name: 'Eval suite' }), 'zhanggui-wenshu');
    await user.selectOptions(within(panel).getByRole('combobox', { name: 'Eval 周期' }), 'weekly');
    await user.clear(within(panel).getByRole('spinbutton', { name: 'Eval 周期间隔' }));
    await user.type(within(panel).getByRole('spinbutton', { name: 'Eval 周期间隔' }), '2');
    await user.clear(within(panel).getByRole('spinbutton', { name: 'Eval 最大执行次数' }));
    await user.type(within(panel).getByRole('spinbutton', { name: 'Eval 最大执行次数' }), '8');
    const due = '2099-01-01T08:00';
    fireEvent.change(within(panel).getByLabelText('Eval 首次执行时间'), { target: { value: due } });
    await user.click(within(panel).getByRole('button', { name: '创建计划' }));

    await waitFor(() => expect(transport.requests.filter(({ request }) => (
      request.pathId === 'observability.evalSchedules.create'
    ))).toHaveLength(1));
    expect(transport.requests.find(({ request }) => request.pathId === 'observability.evalSchedules.create')?.request).toMatchObject({
      body: {
        suiteId: 'zhanggui-wenshu',
        suiteRevision: 'fixture-v2',
        recurrenceKind: 'weekly',
        recurrenceInterval: 2,
        maxRuns: 8,
        nextDueAtMs: new Date(due).getTime(),
      },
      responseContract: 'eval-schedule-create.v1',
    });
    expect(await within(panel).findByText('周期 Eval 已创建')).toBeInTheDocument();
    await waitFor(() => expect(within(panel).getByRole('button', { name: 'zhanggui-wenshu fixture-v2，已计划' })).toBeInTheDocument());
  });

  it('keeps vertical Agent execution owned by the installed sandbox plugin', async () => {
    const transport = observationTransport();
    renderFeature(transport);

    const panel = await screen.findByRole('region', { name: '周期 Eval' });
    expect(within(panel).queryByRole('button', { name: '立即自测' })).not.toBeInTheDocument();
    expect(within(panel).getByText('垂直 App 多重 Eval 试验场')).toBeInTheDocument();
    expect(transport.requests.filter(({ request }) => (
      request.pathId === 'observability.evalSchedules.create'
    ))).toHaveLength(0);
  });

  it('keeps schedules readable but disables creation when the registered suite catalog is unavailable', async () => {
    const user = userEvent.setup();
    const transport = observationTransport({
      suites: () => Promise.reject(new Error('suite catalog unavailable')),
      schedules: evalScheduleListResponse([evalSchedule('eval-schedule:sgg')]),
    });
    renderFeature(transport);

    const panel = await screen.findByRole('region', { name: '周期 Eval' });
    expect(await within(panel).findByRole('button', { name: 'sgg fixture-v2，已计划' })).toBeInTheDocument();
    await user.click(within(panel).getByText('新建周期 Eval').closest('summary') as HTMLElement);
    expect(await within(panel).findByText('Eval suite 目录暂不可用')).toBeInTheDocument();
    expect(within(panel).getByRole('button', { name: '创建计划' })).toBeDisabled();
    expect(within(panel).queryByRole('textbox', { name: 'Eval suiteId' })).not.toBeInTheDocument();
  });

  it('blocks schedule creation while a cached suite catalog is refetching and adopts the fresh revision', async () => {
    const user = userEvent.setup();
    let suiteCalls = 0;
    let resolveSuiteRefetch: ((value: ReturnType<typeof evalSuiteListResponse>) => void) | undefined;
    const pendingSuiteRefetch = new Promise<ReturnType<typeof evalSuiteListResponse>>((resolve) => {
      resolveSuiteRefetch = resolve;
    });
    const transport = observationTransport({
      suites: () => {
        suiteCalls += 1;
        return suiteCalls === 1 ? evalSuiteListResponse() : pendingSuiteRefetch;
      },
    });
    renderFeature(transport);

    const panel = await screen.findByRole('region', { name: '周期 Eval' });
    await user.click(within(panel).getByText('新建周期 Eval').closest('summary') as HTMLElement);
    const suiteSelect = await within(panel).findByRole('combobox', { name: 'Eval suite' });
    expect(suiteSelect).not.toBeDisabled();

    await user.click(screen.getByRole('button', { name: '刷新' }));
    await waitFor(() => expect(suiteCalls).toBe(2));
    expect(suiteSelect).toBeDisabled();
    const createButton = within(panel).getByRole('button', { name: '创建计划' });
    expect(createButton).toBeDisabled();
    await user.click(createButton);
    expect(transport.requests.filter(({ request }) => request.pathId === 'observability.evalSchedules.create')).toHaveLength(0);

    resolveSuiteRefetch?.(evalSuiteListResponse([
      {
        suiteId: 'sgg', suiteRevision: 'fixture-v2', displayName: 'SGG 示例垂直 Agent', fixtureCount: 1,
        capabilities: ['eval.ground_truth', 'memory.recall', 'rag.retrieval', 'sandbox.self_test', 'trace.emit'],
      },
    ]));
    await waitFor(() => expect(suiteSelect).not.toBeDisabled());
    expect(within(panel).getByText('fixture-v2')).toBeInTheDocument();

    await user.click(createButton);
    await waitFor(() => expect(transport.requests.filter(({ request }) => request.pathId === 'observability.evalSchedules.create')).toHaveLength(1));
    expect(transport.requests.find(({ request }) => request.pathId === 'observability.evalSchedules.create')?.request).toMatchObject({
      body: { suiteId: 'sgg', suiteRevision: 'fixture-v2' },
    });
  });

  it('loads a scoped snapshot, subscribes from its cursor, and merges live events', async () => {
    const transport = observationTransport();
    renderFeature(transport, '/observability?sessionId=session-a');

    expect(await screen.findByRole('heading', { name: '运行记录' })).toBeInTheDocument();
    const timeline = await screen.findByRole('list', { name: '运行记录事件' });
    expect(within(timeline).getByText('记忆工具 已完成')).toBeInTheDocument();
    expect(screen.getByText('运行记录只保存状态、耗时、数量和脱敏后的标识。', { exact: false })).toBeInTheDocument();
    expect(screen.getByText('开启“本机上下文快照”后', { exact: false })).toBeInTheDocument();
    expect(screen.queryByText('PRIVATE_TOOL_RESULT')).not.toBeInTheDocument();

    await waitFor(() => expect(transport.subscriptionCalls).toHaveLength(1));
    expect(transport.requests[0]?.request).toEqual(expect.objectContaining({
      pathId: 'observability.snapshot',
      query: { limit: 300, sessionId: 'session-a' },
    }));
    expect(transport.subscriptionCalls[0]?.request).toEqual({
      pathId: 'observability.events',
      query: { sessionId: 'session-a' },
      lastEventId: 'observation:2',
    });

    expect(transport.emit('observability.events', observationEvent({
      sequence: 3,
      category: 'memory',
      phase: 'draft_ready',
      status: 'waiting',
      summary: '记忆整理草案已生成，等待审阅',
    }))).toBe(1);
    await waitFor(() => {
      expect(
        within(timeline).getByText('记忆整理草案已生成，等待审阅'),
      ).toBeInTheDocument();
    });
  });

  it('keeps a subagent run scope in the URL, snapshot, stream, and rendered timeline', async () => {
    const targetRunId = 'subagent-run:test';
    const transport = observationTransport({
      items: [
        {
          ...observationEvent({
          sequence: 2,
          category: 'agent',
          phase: 'subagent_failed',
          status: 'failed',
          summary: '子 Agent 读取配置失败',
          }),
          traceId: 'trace:subagent:test',
          runId: targetRunId,
        },
        {
          ...observationEvent({
          sequence: 1,
          category: 'agent',
          phase: 'subagent_completed',
          status: 'completed',
          summary: '其他运行不应出现在这里',
          }),
          traceId: 'trace:subagent:other',
          runId: 'subagent-run:other',
        },
      ],
    });

    renderFeature(transport, `/observability?runId=${encodeURIComponent(targetRunId)}`);

    const timeline = await screen.findByRole('list', { name: '运行记录事件' });
    expect(within(timeline).getByText(/读取配置失败/)).toBeInTheDocument();
    expect(within(timeline).queryByText(/其他运行不应出现在这里/)).not.toBeInTheDocument();
    expect(screen.getByText(`只看子 Agent 运行 · ${targetRunId}`)).toBeInTheDocument();
    await waitFor(() => expect(transport.subscriptionCalls).toHaveLength(1));
    expect(transport.requests.find(({ request }) => request.pathId === 'observability.snapshot')?.request.query).toEqual({
      limit: 300,
      runId: targetRunId,
    });
    expect(transport.subscriptionCalls[0]?.request).toEqual({
      pathId: 'observability.events',
      query: { runId: targetRunId },
      lastEventId: 'observation:2',
    });
  });

  it('switches categories and exposes a causal trace without rendering raw attributes', async () => {
    const user = userEvent.setup();
    const transport = observationTransport();
    renderFeature(transport);

    const timeline = await screen.findByRole('list', { name: '运行记录事件' });
    expect(within(timeline).getByText('记忆工具 已完成')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '工具' }));

    await waitFor(() => {
      const snapshotRequest = transport.requests.filter(({ request }) => (
        request.pathId === 'observability.snapshot'
        && request.query?.category === 'tool'
      )).at(-1)?.request;
      expect(snapshotRequest?.query).toEqual({ limit: 300, category: 'tool' });
    });

    await user.click(within(timeline).getByText('记忆工具 已完成'));
    expect(screen.getByText('参数字段')).toBeInTheDocument();
    expect(screen.getByText('已脱敏')).toBeInTheDocument();
    expect(screen.queryByText('PRIVATE_TOOL_RESULT')).not.toBeInTheDocument();

    await user.type(screen.getByRole('searchbox', { name: '搜索运行记录' }), '不会匹配');
    expect(await screen.findByRole('heading', { name: '没有匹配的记录' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '清除筛选' }));
    expect((await screen.findAllByText('记忆工具 已完成')).length).toBeGreaterThan(0);
  });

  it('uses local search only for the timeline and keeps every loaded step of the selected trace', async () => {
    const user = userEvent.setup();
    renderFeature(observationTransport());

    const timeline = await screen.findByRole('list', { name: '运行记录事件' });
    const tracePanel = screen.getByRole('heading', { name: '这次是怎样完成的' }).closest('section') as HTMLElement;
    expect(within(tracePanel).getByText('2 步')).toBeInTheDocument();
    expect(within(tracePanel).getByText('伙伴 正在分析')).toBeInTheDocument();

    await user.type(screen.getByRole('searchbox', { name: '搜索运行记录' }), '记忆工具');

    expect(within(timeline).queryByText('伙伴 正在分析')).not.toBeInTheDocument();
    expect(within(tracePanel).getByText('伙伴 正在分析')).toBeInTheDocument();
    expect(within(tracePanel).getByText('2 步')).toBeInTheDocument();
  });

  it('stays truthful about connection state, status tones, and step timing', async () => {
    const transport = observationTransport();
    renderFeature(transport);

    const timeline = await screen.findByRole('list', { name: '运行记录事件' });
    await waitFor(() => expect(transport.subscriptionCalls).toHaveLength(1));

    const pulse = document.querySelector('.observation-pulse') as HTMLElement;
    expect(pulse).toHaveAttribute('data-connection', 'live');
    expect(screen.getByText('实时')).toBeInTheDocument();
    expect(screen.queryByText(/快照生成于/)).not.toBeInTheDocument();

    const runningBadge = screen.getByText('当时运行中', { selector: '.mgmt-status' });
    expect(runningBadge).toHaveAttribute('data-tone', 'info');
    const runningRow = within(timeline).getByText('伙伴 正在分析').closest('li');
    expect(runningRow).toHaveAttribute('data-status', 'running');

    const stepTimes = document.querySelectorAll('.observation-trace__summary > time');
    expect(stepTimes).toHaveLength(2);
    expect(stepTimes[0]).toHaveAttribute('datetime', new Date(1_001).toISOString());

    expect(transport.fail('observability.events', new Error('stream down'))).toBe(1);
    await waitFor(() => expect(pulse).toHaveAttribute('data-connection', 'offline'));
    expect(screen.getByText('快照模式')).toBeInTheDocument();
    expect(screen.getByText(/快照生成于/)).toBeInTheDocument();
    expect(screen.getByText('实时事件暂时不可用，正在保留当前快照并尝试重连。')).toBeInTheDocument();
  });

  it('names a truncated snapshot and progressively reveals complete facts and real tool progress', async () => {
    const user = userEvent.setup();
    const item = observationEvent({
      sequence: 9,
      category: 'tool',
      phase: 'tool_progress',
      status: 'running',
      summary: '已扫描 24 / 48 段',
      durationMs: 1_420,
      attributes: { model: 'gpt-test', provider: 'openai' },
      metrics: {
        progressCurrent: 24,
        progressTotal: 48,
        argumentFieldCount: 2,
        resultFieldCount: 4,
        evidenceCount: 7,
        candidateCount: 9,
        eventCount: 12,
        compactionCount: 1,
      },
    });
    renderFeature(observationTransport({ items: [item], total: 48, truncated: true }));

    expect(await screen.findByText('当前显示最近 1 / 共 48 条')).toBeVisible();
    const progress = screen.getByRole('progressbar', { name: '工具进度 24 / 48' });
    expect(progress).toHaveAttribute('value', '24');
    expect(progress).toHaveAttribute('max', '48');

    const disclosure = screen.getByText(/查看其余 \d+ 项事实/).closest('summary') as HTMLElement;
    expect(disclosure).toHaveAttribute('aria-expanded', 'false');
    const reveal = document.getElementById(disclosure.getAttribute('aria-controls') ?? '') as HTMLElement;
    expect(reveal).toHaveAttribute('aria-hidden', 'true');
    expect(reveal).toHaveAttribute('inert');
    await user.click(disclosure);
    expect(disclosure).toHaveAttribute('aria-expanded', 'true');
    expect(reveal).toHaveAttribute('aria-hidden', 'false');
    expect(reveal).not.toHaveAttribute('inert');
    const originalFact = within(reveal).getByText('压缩次数').parentElement?.textContent;
    expect(originalFact).toContain('1');
    disclosure.focus();
    await user.keyboard('{Enter}');
    expect(disclosure).toHaveAttribute('aria-expanded', 'false');
    expect(reveal).toHaveAttribute('aria-hidden', 'true');
    expect(reveal).toHaveAttribute('inert');
    expect(within(reveal).queryByRole('term')).toBeNull();
    expect(reveal).not.toHaveTextContent('压缩次数');
    await user.keyboard('{Enter}');
    expect(disclosure).toHaveAttribute('aria-expanded', 'true');
    expect(reveal).not.toHaveAttribute('inert');
    const reopenedFact = await within(reveal).findByText('压缩次数');
    expect(reopenedFact.parentElement?.textContent).toBe(originalFact);
  });
});

function renderFeature(
  transport: MockControlTransport,
  initialEntry = '/observability',
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={client}>
          <TooltipProvider>
            <ObservabilityFeature />
          </TooltipProvider>
        </QueryClientProvider>
      </ControlTransportProvider>
    </MemoryRouter>,
  );
}

function observationTransport(options: {
  beforeSnapshot?: () => void;
  items?: ObservationEventV1[];
  total?: number;
  truncated?: boolean;
  trace?: unknown | ((request: ControlRequest) => unknown | Promise<unknown>);
  evals?: unknown | ((request: ControlRequest) => unknown | Promise<unknown>);
  suites?: unknown | ((request: ControlRequest) => unknown | Promise<unknown>);
  evidenceEval?: unknown | ((request: ControlRequest) => unknown | Promise<unknown>);
  aiJudge?: unknown | ((request: ControlRequest) => unknown | Promise<unknown>);
  schedules?: unknown | ((request: ControlRequest) => unknown | Promise<unknown>);
  scheduleCreate?: unknown | ((request: ControlRequest) => unknown | Promise<unknown>);
  scheduleRuns?: unknown | ((request: ControlRequest) => unknown | Promise<unknown>);
  sandboxRuns?: unknown | ((request: ControlRequest) => unknown | Promise<unknown>);
} = {}): MockControlTransport {
  return new MockControlTransport({
    routes: {
      'observability.snapshot': (request: ControlRequest) => {
        options.beforeSnapshot?.();
        const category = String(request.query?.category ?? '');
        const items = (options.items ?? [
          observationEvent({
            sequence: 2,
            category: 'tool',
            phase: 'tool_finished',
            status: 'completed',
            summary: 'ime.memory 已完成',
            durationMs: 48,
            metrics: { argumentFieldCount: 2, resultFieldCount: 4 },
            attributes: { rawTextStored: false, result: 'PRIVATE_TOOL_RESULT' },
          }),
          observationEvent({
            sequence: 1,
            category: 'agent',
            phase: 'status_changed',
            status: 'running',
            summary: 'Agent 正在分析',
          }),
        ]).filter((item) => !category || item.category === category);
        return {
          schemaVersion: 'rag-ime.observation-snapshot.v1',
          generatedAtMs: 1_000,
          firstSequence: 1,
          lastSequence: 2,
          resumeToken: 'observation:2',
          truncated: options.truncated ?? false,
          filters: {},
          counts: { total: options.total ?? items.length, byCategory: {}, byStatus: {} },
          items,
        };
      },
      'observability.trace.get': options.trace ?? canonicalTraceResponse(),
      'observability.evals.list': options.evals ?? evalListResponse(),
      'observability.evalSuites.list': options.suites ?? evalSuiteListResponse(),
      'observability.evals.evidence.run': options.evidenceEval ?? evalRunResponse(),
      'observability.evals.aiJudge.run': options.aiJudge ?? aiJudgeRunResponse(),
      'observability.evalSchedules.list': options.schedules ?? evalScheduleListResponse([]),
      'observability.evalSchedules.create': options.scheduleCreate ?? {
        schemaVersion: 'rag-ime.eval-schedule-create.v1',
        ok: true,
        schedule: evalSchedule('eval-schedule:default'),
      },
      'observability.evalSchedule.runs': options.scheduleRuns ?? evalScheduleRunListResponse('eval-schedule:default', []),
      [SANDBOX_RUNS_PATH_ID]: options.sandboxRuns ?? sandboxRunListResponse([]),
    },
  });
}

function sandboxRunListResponse(items: SandboxRunV1[] = [sandboxRun()]) {
  return {
    schemaVersion: 'rag-ime.observability-sandbox-run-list.v1' as const,
    ok: true as const,
    items,
    total: items.length,
  };
}

function sandboxRun(): SandboxRunV1 {
  return {
    schemaVersion: 'rag-ime.sandbox-run.v1',
    sandboxRunId: 'sandbox:sgg:demo',
    appId: 'sgg',
    status: 'completed',
    policy: {
      workspaceBindingId: 'workspace-binding:demo',
      workspaceFingerprint: `sha256:${'0'.repeat(64)}`,
      mutationMode: 'read_only',
      network: 'blocked',
      productionWriteBlocked: true,
    },
    traceIds: ['trace:sgg:demo'],
    evalRunIds: ['eval:sgg:demo'],
    createdAtMs: 1,
    updatedAtMs: 2,
  };
}

function evalSuiteListResponse(items: EvalSuiteListV1['items'] = [
  {
    suiteId: 'sgg', suiteRevision: 'fixture-v2', displayName: 'SGG 示例垂直 Agent', fixtureCount: 1,
    capabilities: ['eval.ground_truth', 'memory.recall', 'rag.retrieval', 'sandbox.self_test', 'trace.emit'],
  },
  {
    suiteId: 'zhanggui-wenshu', suiteRevision: 'fixture-v2', displayName: '掌柜问数', fixtureCount: 1,
    capabilities: ['eval.ground_truth', 'memory.recall', 'rag.retrieval', 'sandbox.self_test', 'trace.emit'],
  },
]) {
  return {
    schemaVersion: 'rag-ime.eval-suite-list.v1' as const,
    ok: true as const,
    items,
  };
}

function evalSchedule(
  id: string,
  overrides: Partial<EvalScheduleListV1['items'][number]> = {},
): EvalScheduleListV1['items'][number] {
  return { ...evalScheduleBase(), id, ...overrides };
}

function evalScheduleBase(): EvalScheduleListV1['items'][number] {
  return {
    id: 'eval-schedule:fixture',
    suiteId: 'sgg',
    suiteRevision: 'fixture-v2',
    recurrenceKind: 'daily' as const,
    recurrenceInterval: 1,
    maxRuns: 30,
    runCount: 1,
    status: 'scheduled' as const,
    initialDueAtMs: 4_102_444_800_000,
    nextDueAtMs: 4_102_444_800_000,
    lastErrorCode: '',
    createdAtMs: 1,
    updatedAtMs: 2,
    latestRun: {},
  };
}

function evalScheduleListResponse(items: EvalScheduleListV1['items']) {
  return {
    schemaVersion: 'rag-ime.eval-schedule-list.v1' as const,
    ok: true as const,
    items,
  };
}

function evalScheduleRunListResponse(
  scheduleId: string,
  items = [{
    id: `eval-run:${scheduleId}:1`, scheduleId, attempt: 1, state: 'succeeded' as const,
    dueAtMs: 1, claimedAtMs: 2, finishedAtMs: 3, evalRunId: 'eval:sgg:1', errorCode: '',
    traceIds: ['trace:sgg:1'], traceIdsTruncated: false,
  }],
) {
  return {
    schemaVersion: 'rag-ime.eval-schedule-run-list.v1' as const,
    ok: true as const,
    schedule: evalSchedule(scheduleId),
    items,
  };
}

function evalListResponse(evalRunId = 'eval:human:existing') {
  return {
    schemaVersion: 'rag-ime.observability-eval-list.v1' as const,
    traceId: 'trace:turn:test',
    total: 2,
    truncated: false,
    items: [
      {
        evalRunId,
        mode: 'ground_truth' as const,
        metricAuthority: 'ground_truth' as const,
        truthStatus: 'human' as const,
        datasetId: 'dataset:human',
        labelRevision: 'labels:1',
        evaluatorDisplayName: '人工标注',
        suiteBinding: { suiteId: 'sgg', suiteRevision: 'fixture-v2' },
        metrics: { evidenceRecall: 0.5 },
        status: 'completed' as const,
        createdAtMs: 3,
        updatedAtMs: 3,
      },
      {
        evalRunId: 'eval:ai:existing',
        mode: 'ai_judge' as const,
        metricAuthority: 'ai_judge_estimate' as const,
        truthStatus: 'none' as const,
        datasetId: 'dataset:ai',
        labelRevision: 'labels:ai',
        evaluatorDisplayName: 'AI Judge',
        metrics: { qualityEstimate: 0.8 },
        status: 'completed' as const,
        createdAtMs: 4,
        updatedAtMs: 4,
      },
    ],
  };
}

function evalRunResponse() {
  return {
    schemaVersion: 'rag-ime.eval-run.v1' as const,
    evalRunId: 'eval:human:new',
    traceIds: ['trace:turn:test'] as [string, ...string[]],
    mode: 'ground_truth' as const,
    metricAuthority: 'ground_truth' as const,
    truth: { status: 'human' as const, datasetId: 'dataset:human', labelRevision: 'labels:1' },
    evaluator: { provider: 'human', model: 'manual', thinking: 'manual', displayName: '人工标注' },
    metrics: {},
    status: 'completed' as const,
    createdAtMs: 5,
    updatedAtMs: 5,
  };
}

function aiJudgeRunResponse() {
  return {
    schemaVersion: 'rag-ime.eval-run.v1' as const,
    evalRunId: 'eval:ai-judge:new',
    traceIds: ['trace:turn:test'] as [string, ...string[]],
    mode: 'ai_judge' as const,
    metricAuthority: 'ai_judge_estimate' as const,
    truth: { status: 'none' as const, datasetId: 'trace-eval-ai-judge', labelRevision: 'trace-eval-ai-judge-v1' },
    evaluator: { provider: 'openai-codex', model: 'gpt-5.6-luna', thinking: 'max', displayName: 'Luna Max' },
    metrics: { relevance: 0.9, coverage: 0.8, groundedness: 1, contradiction: 0, confidence: 0.7 },
    status: 'completed' as const,
    createdAtMs: 5,
    updatedAtMs: 5,
  };
}

function canonicalTraceResponse(options: { truncated?: boolean } = {}) {
  return {
    schemaVersion: 'rag-ime.observability-trace-get.v1' as const,
    traceId: 'trace:turn:test',
    trace: {
      schemaVersion: 'rag-ime.trace-envelope.v1' as const,
      traceId: 'trace:turn:test',
      sourceKind: 'agent',
      status: options.truncated ? 'building' as const : 'completed' as const,
      binding: {
        sessionId: 'session-a', turnId: 'turn-a', workItemId: 'work:trace-test', caseId: 'case:trace-test',
      },
      parentTraceId: 'trace:parent:test',
      links: [{ traceId: 'trace:retry:test', relation: 'retry' as const, targetKind: 'trace' as const }],
      input: {
        fingerprint: `sha256:${'0'.repeat(64)}`,
        contentPolicy: 'hash_only' as const,
        normalization: 'none',
      },
      spans: [
        {
          spanId: 'span:turn:test',
          name: 'agent.turn',
          parentSpanId: null,
          status: 'completed' as const,
          startedAtMs: 1,
          endedAtMs: 1,
          durationMs: 0,
          recorded: true,
          unavailableReason: '',
          metrics: {},
          attributes: { provider: 'openai' },
        },
        {
          spanId: 'span:retrieval:test',
          name: 'retrieval',
          parentSpanId: 'span:turn:test',
          status: 'completed' as const,
          startedAtMs: 2,
          endedAtMs: null,
          durationMs: null,
          recorded: false,
          unavailableReason: 'duration_not_recorded',
          metrics: { retrieved: 42 },
          attributes: {},
        },
      ],
      evidence: [
        {
          evidenceId: 'evidence:manual',
          sourceKind: 'document',
          sourceRef: 'source:manual',
          sourceLane: 'dense',
          evidenceStage: 'retrieval_output',
          disposition: 'included' as const,
          scores: { hybrid: -0.25 },
          rankBefore: 1,
          rankAfter: 2,
          omissionReason: '',
        },
        {
          evidenceId: 'evidence:memory',
          sourceKind: 'memory',
          sourceRef: 'source:memory',
          sourceLane: 'lexical',
          evidenceStage: 'retrieval_output',
          disposition: 'omitted' as const,
          scores: { lexical: 0.5 },
          rankBefore: 2,
          rankAfter: null,
          omissionReason: 'filtered',
        },
      ],
      artifacts: [{
        artifactId: 'artifact:report',
        kind: 'eval_report',
        mediaType: 'application/json',
        sha256: 'a'.repeat(64),
        byteSize: 1_024,
        recordCount: 2,
      }],
      createdAtMs: 1,
      updatedAtMs: 2,
    },
    truncated: options.truncated ?? false,
    projectionSource: 'observation_journal' as 'observation_journal' | 'source_adapter' | 'trace_store',
    observationWindow: {
      firstSequence: 1,
      lastSequence: 2,
      resumeToken: 'observation:2',
      nextBeforeSequence: options.truncated ? 1 : null,
    },
  };
}

function observationEvent(
  overrides: Partial<ObservationEventV1> & Pick<
    ObservationEventV1,
    'sequence' | 'category' | 'phase' | 'status' | 'summary'
  >,
): ObservationEventV1 {
  return {
    schemaVersion: 'rag-ime.observation-event.v1',
    eventType: 'observation',
    eventId: `observation:test:${overrides.sequence}`,
    sequence: overrides.sequence,
    resumeToken: `observation:${overrides.sequence}`,
    traceId: 'trace:turn:test',
    spanId: `span:test:${overrides.sequence}`,
    parentSpanId: overrides.sequence > 1 ? 'span:test:1' : '',
    sessionId: 'session-a',
    roomId: '',
    turnId: 'turn-a',
    runId: '',
    category: overrides.category,
    phase: overrides.phase,
    name: overrides.phase,
    status: overrides.status,
    summary: overrides.summary,
    createdAtMs: 1_000 + overrides.sequence,
    startedAtMs: 1_000 + overrides.sequence,
    endedAtMs: overrides.status === 'completed' ? 1_000 + overrides.sequence : null,
    durationMs: overrides.durationMs ?? null,
    privacyClass: 'redacted',
    metrics: overrides.metrics ?? {},
    attributes: overrides.attributes ?? {},
    refs: [],
  };
}
