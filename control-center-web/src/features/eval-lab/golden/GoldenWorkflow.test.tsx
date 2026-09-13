import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { HttpControlTransport } from '@/platform/http-transport';
import type { ControlRequest, ControlTransport } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { goldenPollInterval, isGoldenRejection, submitGoldenCommand } from './api';
import { GoldenWorkflow } from './GoldenWorkflow';
import { CaseReview } from './CaseReview';
import { CalibrationPanel } from './Calibration';
import { GoldenExperiment } from './Experiment';
import { LabAppDelivery } from '../projects/LabAppDelivery';
import { isExperimentResult, parseGoldenRead, type ExperimentResult, type ExperimentUsage, type GoldenCase, type GoldenCommand, type GoldenJob, type GoldenSuite } from './types';

const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); window.sessionStorage.clear(); vi.useRealTimers(); });
const model = { provider: 'configured-provider', model: 'configured-model', thinkingLevel: 'high', prompt: '' };
const usage: ExperimentUsage = { calls: 2, inputTokens: null, outputTokens: null, totalTokens: null, costUsd: null, knownCostUsd: null, pricedCalls: 0, tokensComplete: false, costComplete: false, source: 'runtime_receipts' };
const sources: GoldenSuite['sources'] = [{ sourceId: 'source-1', title: '服务约定', kind: 'document', uri: 'docs/service.md', text: '退款期限为七天。退款需要保留收据。' }];
const goldenCase = (caseId = 'case-dev', split: GoldenCase['split'] = 'development'): GoldenCase => ({
  caseId, split, question: split === 'development' ? '退款期限是多少？' : '申请退款需要什么？', taskType: '事实问答', answerable: true,
  requiredFacts: ['七天'], evidence: [{ sourceId: 'source-1', quote: '退款期限为七天。' }], rubric: ['依据来源回答，不得虚构'],
  review: { status: 'pending', note: '', reviewedAtMs: null },
  samples: split === 'holdout' ? [] : ([['correct', '七天。'], ['incorrect', '三十天。'], ['boundary', '大概一周，但不确定。']] as const).map(([category, answer]) => ({ sampleId: `sample-${category}`, category, answer, humanVerdict: null, humanNote: '' })),
});
const suite = (patch: Partial<GoldenSuite> = {}): GoldenSuite => ({
  schemaVersion: 'rag-ime.agent-lab-golden-suite.v1', suiteId: 'suite-1', title: '退款问答', scenario: '回答售后问题', revision: 1, targetCount: 2,
  sources, cases: [], judgeConfig: model, calibration: null, snapshot: null, jobs: [], createdAtMs: 1000, updatedAtMs: 2000, ...patch,
});
const job = (state: GoldenJob['state'], kind: GoldenJob['kind'] = 'draft', result: GoldenJob['result'] = null): GoldenJob => ({ jobId: `job-${kind}`, state, kind, progress: '', error: '', sessionId: 'session-1', result, createdAtMs: 3000, updatedAtMs: 4000 });
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const read = (current: GoldenSuite) => ({ ok: true as const, items: [current], suite: current });
const receipt = (command: GoldenCommand, current: GoldenSuite, replayed = false) => ({ ok: true, suite: current, job: current.jobs.at(-1) ?? null, clientRequestId: command.clientRequestId, replayed });

function mount(transport: ControlTransport, options: { startNew?: boolean; onClose?: () => void } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  clients.push(client);
  return { client, ...render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><GoldenWorkflow {...options} /></ControlTransportProvider></QueryClientProvider>) };
}
async function enabledButton(name: string) {
  await waitFor(() => expect(screen.getByRole('button', { name })).toBeEnabled());
  return screen.getByRole('button', { name });
}

function experimentResult(): ExperimentResult {
  const answer = { answer: '退款期限为七天。', status: 'graded' as const, judgment: { verdict: 'pass' as const, reason: '与来源一致', evidence: [{ sourceId: 'source-1', quote: '退款期限为七天。' }] }, requestId: 'request-1', sessionId: 'session-1', turnId: 'turn-1' };
  const metrics = { total: 1, passed: 1, failed: 0, uncertain: 0, runtimeErrors: 0, passRate: 1 };
  const phase = { cases: [{ caseId: 'case-dev', question: '退款期限是多少？', taskType: '事实问答', baseline: answer, candidate: answer }], baselineMetrics: metrics, candidateMetrics: metrics, baselineUsage: usage, candidateUsage: usage, usageScope: 'answer_calls_only' as const, businessCost: { basis: 'model_catalog_estimate' as const, baselineUsd: .02, candidateUsd: .01, deltaUsd: -.01, reductionFraction: .5, baselineCostPerSuccessUsd: .02, candidateCostPerSuccessUsd: .01 } };
  return {
    schemaVersion: 'rag-ime.agent-lab-golden-experiment.v1', suiteId: 'suite-1', snapshotId: 'snapshot-1', executionMode: 'context_qa', optimizationScope: 'prompt', judgeConfig: model,
    baseline: model, candidate: { ...model, prompt: '引用退款证据' }, development: phase, holdout: { ...phase, cases: [{ ...phase.cases[0], caseId: 'case-hold', question: '申请退款需要什么？' }] },
    optimization: { enabled: true, maxCandidates: 1, selectedCandidateIndex: 1, proposals: [] }, comparison: { decision: 'improved', comparable: true, developmentDelta: 0, holdoutDelta: 0, reasons: ['质量保持，业务答案估算费用更低。'], sameSnapshot: true, goldenChanged: false, improvementBasis: 'answer_cost_estimate' },
    receipts: [{ requestId: 'request-1', stage: 'development_baseline', sessionId: 'session-1', turnId: 'turn-1', usage, receipt: {} }], usage: { ...usage, calls: 12 }, usageByScope: { judging: { ...usage, calls: 6 }, optimization: { ...usage, calls: 1 }, candidateAnswers: { ...usage, calls: 4 } },
  };
}

describe('Golden workflow user boundaries', () => {
  it('selects a completed frozen variant without submitting another experiment', () => {
    const result = experimentResult();
    const method = { kind: 'application_skill' as const, title: '冻结方法', body: '逐项核对来源', sha256: 'a'.repeat(64), source: { kind: 'inline' as const } };
    result.candidate.applicationMethod = method;
    const onSelectDelivery = vi.fn(); const onExperiment = vi.fn();
    render(<GoldenExperiment suite={suite({ jobs: [job('completed', 'experiment', result as unknown as Record<string, unknown>)] })} disabled={false} onFreeze={vi.fn()} onExperiment={onExperiment} onSelectDelivery={onSelectDelivery} />);
    expect(screen.getByRole('button', { name: '选择基线用于交付' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: '选择候选用于交付' }));
    expect(onSelectDelivery).toHaveBeenCalledWith({ suiteId: 'suite-1', jobId: 'job-experiment', variant: 'candidate' });
    expect(onExperiment).not.toHaveBeenCalled();
  });
  it('reads the selected completion for delivery and submits only its exact identity', async () => {
    const result = experimentResult();
    result.candidate.applicationMethod = { kind: 'application_skill', title: '冻结方法', body: '逐项核对来源', sha256: 'a'.repeat(64), source: { kind: 'inline' } };
    const current = suite({ jobs: [job('completed', 'experiment', result as unknown as Record<string, unknown>)] });
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.apps.get': () => ({ ok: true, items: [], app: null }),
      'agent.eval-lab.golden.get': () => read(current),
    } });
    const client = new QueryClient(); clients.push(client);
    const prepare = vi.fn(async () => false);
    const selection = { suiteId: 'suite-1', jobId: 'job-experiment', variant: 'candidate' as const };
    render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><LabAppDelivery projectId="project-1" evaluationSelection={selection} onPrepare={prepare} /></ControlTransportProvider></QueryClientProvider>);
    expect(await screen.findByText('冻结方法')).toBeVisible();
    fireEvent.click(screen.getByText('审查原评测、双方 Prompt、指标与逐题来源'));
    expect(screen.getByText('基线 Prompt')).toBeVisible();
    fireEvent.click(await enabledButton('准备应用版本'));
    expect(prepare).toHaveBeenCalledWith('app', undefined, selection);
    expect(transport.requests.every(({ request }) => request.pathId.endsWith('.get'))).toBe(true);
  });
  it('accepts Agent review as its own running job rather than an experiment result', () => {
    const value = job('running', 'review');
    const parsed = parseGoldenRead(read(suite({ jobs: [value] })), 'suite-1');
    expect(parsed.suite?.jobs[0]?.kind).toBe('review');
    expect(isExperimentResult(value.result)).toBe(false);
  });
  it('keeps a missing selected delivery job blocked instead of using a different completion', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.apps.get': () => ({ ok: true, items: [], app: null }),
      'agent.eval-lab.golden.get': () => read(suite({ jobs: [job('completed', 'experiment', experimentResult() as unknown as Record<string, unknown>)] })),
    } });
    const client = new QueryClient(); clients.push(client); const prepare = vi.fn(async () => false);
    render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><LabAppDelivery projectId="project-1" evaluationSelection={{ suiteId: 'suite-1', jobId: 'missing-job', variant: 'candidate' }} onPrepare={prepare} /></ControlTransportProvider></QueryClientProvider>);
    expect(await screen.findByRole('alert')).toHaveTextContent('未替换为其他实验');
    expect(screen.getByRole('button', { name: '准备应用版本' })).toBeDisabled();
    expect(prepare).not.toHaveBeenCalled();
  });
  it('accepts an exact application method comparison and displays the retained requested job', () => {
    const result = experimentResult(); result.optimizationScope = 'skill';
    const method = { kind: 'application_skill' as const, title: '证据核对方法', body: '阅读原文后核对限制', sha256: 'a'.repeat(64), source: { kind: 'inline' as const } };
    result.candidate = { ...model, applicationMethod: method };
    result.applicationMethodComparison = { scope: 'application_skill_body', changed: true, baseline: null, candidate: method, diff: '+阅读原文后核对限制' };
    expect(isExperimentResult(result)).toBe(true);
    const old = { ...job('completed', 'experiment', result), jobId: 'retained-job', createdAtMs: 1 };
    const recent = { ...job('completed', 'experiment', experimentResult()), jobId: 'recent-job', createdAtMs: 2 };
    render(<GoldenExperiment suite={suite({ jobs: [old, recent] })} initialJobId="retained-job" disabled={false} onFreeze={vi.fn()} onExperiment={vi.fn(async () => true)} />);
    expect(screen.getByText('证据核对方法')).toBeVisible();
    expect(screen.getByRole('region', { name: '应用 Skill 方法对照' })).toBeVisible();
  });
  it('keeps the frozen annotation provenance on a historical experiment after calibration changes', () => {
    const result = { ...experimentResult(), referenceAuthority: 'agent_assisted' as const };
    render(<GoldenExperiment suite={suite({ jobs: [job('completed', 'experiment', result)] })} disabled={false} onFreeze={vi.fn()} onExperiment={vi.fn(async () => true)} />);
    expect(screen.getByText(/此实验使用 Agent 辅助标注的冻结标准/)).toBeInTheDocument();
  });

  it('records an explicit Agent label source without calling it a human label', async () => {
    const item = goldenCase(); item.review.status = 'approved';
    const onLabel = vi.fn(async () => true);
    render(<CalibrationPanel suite={suite({ cases: [item] })} disabled={false} onLabel={onLabel} onJudge={vi.fn(async () => true)} onCalibrate={vi.fn()} onNext={vi.fn()} />);
    fireEvent.change(screen.getByRole('combobox', { name: '标注来源' }), { target: { value: 'agent' } });
    expect(screen.getByText(/不计作独立人工金标/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('radio', { name: '通过' }));
    fireEvent.click(screen.getByRole('button', { name: '保存 Agent 标签' }));
    await waitFor(() => expect(onLabel).toHaveBeenCalledWith(expect.objectContaining({ labelAuthor: 'agent', humanVerdict: 'pass' })));
  });

  it('reopens the unfinished calibration and selects the first unlabelled sample', async () => {
    const cases = [goldenCase(), goldenCase('case-hold', 'holdout')].map((item) => ({ ...item,
      review: { status: 'approved' as const, note: '', reviewedAtMs: 3000 },
      samples: item.samples.map((sample, index) => ({ ...sample, humanVerdict: index === 0 ? 'pass' as const : null })),
    }));
    const transport = new MockControlTransport({ routes: { 'agent.eval-lab.golden.get': read(suite({ cases })) } });
    mount(transport);
    await waitFor(() => expect(screen.getByRole('tab', { name: '校准评审' })).toHaveAttribute('aria-selected', 'true'));
    expect(screen.getByRole('textbox', { name: '待标注答案' })).toHaveValue('三十天。');
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.eval-lab.golden.command')).toBe(false);
  });

  it('explains the minimum split instead of leaving a one-question form silently disabled', async () => {
    const transport = new MockControlTransport({ routes: { 'agent.eval-lab.golden.get': { ok: true, items: [], suite: null } } });
    mount(transport, { startNew: true });
    await screen.findByRole('heading', { name: '新建评测集' });
    fireEvent.change(screen.getByRole('spinbutton', { name: '计划题数' }), { target: { value: '1' } });
    expect(screen.getByRole('alert')).toHaveTextContent('至少 2 题');
    expect(screen.getByRole('button', { name: '保存来源，建立评测集' })).toBeDisabled();
    expect(screen.getByRole('complementary', { name: '本步指引' })).toHaveTextContent('先提供一份能核对答案的资料');
  });

  it('advances to the next pending case only after a successful save', async () => {
    const onReview = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    render(<CaseReview suite={suite({ cases: [goldenCase(), goldenCase('case-hold', 'holdout')] })} disabled={false} onReview={onReview} onNext={() => {}} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '通过并看下一题' }));
    expect(screen.getByRole('textbox', { name: '问题' })).toHaveValue('退款期限是多少？');
    await user.click(screen.getByRole('button', { name: '通过并看下一题' }));
    expect(screen.getByRole('textbox', { name: '问题' })).toHaveValue('申请退款需要什么？');
    expect(onReview).toHaveBeenLastCalledWith(expect.objectContaining({ caseId: 'case-dev', verdict: 'approved' }));
  });

  it('opens the original failed run without creating another model request', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.golden.get': read(suite({ jobs: [job('failed')] })),
      'agent.session.snapshot': { items: [{ blocks: [{ type: 'error', data: { message: 'Cannot find module runtime-host/openai-codex.js' } }] }] },
    } });
    mount(transport);
    await userEvent.setup().click(await screen.findByRole('button', { name: '查看原运行记录' }));
    await screen.findByText(/本机模型运行组件缺失/);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.session.snapshot')[0]?.request.params).toEqual({ sessionId: 'session-1' });
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.eval-lab.golden.command')).toBe(false);
  });

  it('shows the owning Runtime retry while a model call is still active', async () => {
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.golden.get': read(suite({ jobs: [job('running')] })),
      'agent.session.snapshot': { items: [], status:'busy', liveEvents:[{eventType:'status_changed',payload:{phase:'provider_retry',summary:'模型连接暂时失败，正在自动重试（3/7）',delayMs:10000}}] },
    } });
    mount(transport);
    expect(await screen.findByRole('status', { name:'当前模型运行状态' })).toHaveTextContent('自动重试（3/7）');
    expect(screen.getByRole('status', { name:'当前模型运行状态' })).toHaveTextContent('10 秒');
    expect(transport.requests.some(({request}) => request.pathId==='agent.eval-lab.golden.command')).toBe(false);
  });

  it('selects an available drafting model and saves it before allowing execution', async () => {
    let current = suite();
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.golden.get': () => read(current),
      'agent.role.models': { providers: [{ id: 'configured-provider', models: [{ id: 'another-model', name: '另一个已配置模型', thinkingLevels: ['low', 'high'] }] }] },
      'agent.eval-lab.golden.command': (request: ControlRequest) => {
        const command = request.body as GoldenCommand;
        current = { ...current, revision: current.revision + 1, judgeConfig: command.input.judgeConfig as typeof model };
        return receipt(command, current);
      },
    } });
    mount(transport);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '选择已配置模型' }));
    await user.selectOptions(await screen.findByRole('combobox', { name: '选择起草模型' }), 'configured-provider/another-model');
    expect(screen.getByRole('button', { name: '让 Agent 起草题目' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '保存起草模型' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '让 Agent 起草题目' })).toBeEnabled());
    expect(current.judgeConfig).toMatchObject({ model: 'another-model', thinkingLevel: 'high' });
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.eval-lab.golden.command')).toHaveLength(1);
  });

  it.each([
    ['审核标准', '问题', '退款期限是多少？', '退款的申请期限是多少？'],
    ['校准评审', '待标注答案', '七天。', '按来源应为七天。'],
    ['校准评审', '评审模型', 'configured-model', 'another-configured-model'],
  ])('keeps unsaved %s / %s visible across steps and prevents freezing the old standard', async (step, field, saved, draft) => {
    const cases = [goldenCase(), goldenCase('case-hold', 'holdout')].map((item) => ({
      ...item, review: { status: 'approved' as const, note: '', reviewedAtMs: 3000 },
      samples: item.samples.map((sample) => ({ ...sample, humanVerdict: sample.category === 'correct' ? 'pass' as const : 'fail' as const })),
    }));
    const current = suite({ cases, calibration: { calibrationId: 'cal-ready', suiteRevision: 1, judgeConfig: model, judgments: [], metrics: { total: 3, comparable: 3, agreement: 1, falsePasses: 0, falseFails: 0, uncertain: 0 }, ready: true, reasons: [], createdAtMs: 6000 } });
    const transport = new MockControlTransport({ routes: { 'agent.eval-lab.golden.get': read(current) } });
    mount(transport);
    const user = userEvent.setup();
    await screen.findByRole('heading', { name: '校准通过，可以冻结' });
    await user.click(screen.getByRole('tab', { name: step }));
    if (field === '评审模型') await user.click(screen.getByText('模型标识与推理强度', { selector: '[role="tabpanel"]:not([hidden]) summary' }));
    fireEvent.change(screen.getByRole('textbox', { name: field }), { target: { value: draft } });
    await user.click(screen.getByRole('tab', { name: '冻结与实验' }));
    expect(screen.getByRole('button', { name: '冻结当前标准' })).toBeDisabled();
    const notice = screen.getByRole('status', { name: '未保存的评测标准' });
    expect(notice).toHaveTextContent(`${step}有未保存修改`);
    await user.click(within(notice).getByRole('button', { name: `返回${step}` }));
    expect(screen.getByRole('textbox', { name: field })).toHaveValue(draft);
    fireEvent.change(screen.getByRole('textbox', { name: field }), { target: { value: saved } });
    await user.click(screen.getByRole('tab', { name: '冻结与实验' }));
    expect(screen.queryByRole('status', { name: '未保存的评测标准' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '冻结当前标准' })).toBeEnabled();
    expect(transport.requests.every(({ request }) => request.pathId === 'agent.eval-lab.golden.get')).toBe(true);
  });

  it('keeps newer job progress when an old receipt has the same standard revision', async () => {
    let current = suite();
    let attempts = 0;
    let unavailable = false;
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.golden.get': () => { if (unavailable) throw new Error('read unavailable'); return read(current); },
      'agent.eval-lab.golden.command': (request: ControlRequest) => {
        const command = request.body as GoldenCommand;
        if (++attempts === 1) {
          current = suite({ jobs: [{ ...job('running'), updatedAtMs: 5000 }] });
          throw new Error('admission receipt lost');
        }
        unavailable = true;
        return receipt(command, suite({ jobs: [job('queued')] }), true);
      },
    } });
    mount(transport);
    const user = userEvent.setup();
    await user.click(await enabledButton('让 Agent 起草题目'));
    await screen.findByRole('button', { name: '核对本次操作' });
    await user.click(screen.getByRole('button', { name: '重新读取评测集' }));
    await screen.findByText('起草题目 · 进行中');
    await user.click(screen.getByRole('button', { name: '核对本次操作' }));
    await screen.findByText('无法读取评测集');
    expect(screen.getByRole('region', { name: '当前任务状态' })).toHaveTextContent('起草题目 · 进行中');
    expect(screen.getByRole('region', { name: '当前任务状态' })).toHaveTextContent('上次读取的状态，当前进展尚未确认。');
    const commands = transport.requests.filter(({ request }) => request.pathId === 'agent.eval-lab.golden.command');
    expect(commands[1].request.body).toEqual(commands[0].request.body);
  });

  it('keeps a newly accepted suite when its follow-up read fails', async () => {
    let accepted = false;
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.golden.get': () => { if (accepted) throw new Error('read unavailable'); return read(suite()); },
      'agent.eval-lab.golden.command': (request: ControlRequest) => {
        accepted = true;
        const command = request.body as GoldenCommand;
        return receipt(command, suite({ suiteId: 'suite-new', title: String(command.input.title), sources: command.input.sources as GoldenSuite['sources'] }));
      },
    } });
    mount(transport, { startNew: true });
    await screen.findByRole('combobox', { name: '选择评测集' });
    for (const [name, value] of [['评测集名称', '新保存的评测集'], ['要评测的任务', '回答退款问题'], ['来源 1 标题', '退款来源'], ['来源 1 引用', 'docs/refund.md'], ['来源 1 原文', '期限为七天。']]) {
      fireEvent.change(screen.getByRole('textbox', { name }), { target: { value } });
    }
    fireEvent.click(await enabledButton('保存来源，建立评测集'));
    await screen.findByText('无法读取评测集');
    expect(screen.getByRole('heading', { name: '新保存的评测集' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '选择评测集' })).toHaveValue('suite-new');
    expect(screen.getByText('退款来源')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '让 Agent 起草题目' })).toBeDisabled();
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.eval-lab.golden.command')).toHaveLength(1);
  });

  it('keeps accepted human edits when the follow-up read fails', async () => {
    let accepted = false;
    const current = suite({ cases: [goldenCase()] });
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.golden.get': () => { if (accepted) throw new Error('read unavailable'); return read(current); },
      'agent.eval-lab.golden.command': (request: ControlRequest) => {
        accepted = true;
        const command = request.body as GoldenCommand;
        return receipt(command, suite({ revision: 2, updatedAtMs: 3000, cases: [{ ...goldenCase(), question: String(command.input.question), review: { status: 'approved', note: '', reviewedAtMs: 3000 } }] }));
      },
    } });
    mount(transport);
    await enabledButton('保存并通过');
    fireEvent.change(screen.getByRole('textbox', { name: '问题' }), { target: { value: '退款必须在多少天内申请？' } });
    fireEvent.click(screen.getByRole('button', { name: '保存并通过' }));
    await screen.findByText('无法读取评测集');
    expect(screen.getByRole('textbox', { name: '问题' })).toHaveValue('退款必须在多少天内申请？');
    expect(screen.getByText('标准版本 2')).toBeInTheDocument();
    expect(screen.getByText('1 / 1 题通过 · 0 题待审核')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存并通过' })).toBeDisabled();
  });

  it('marks cached execution as stale and stops activity feedback after a failed refresh', async () => {
    let unavailable = false;
    const current = suite({ jobs: [job('running')] });
    mount(new MockControlTransport({ routes: { 'agent.eval-lab.golden.get': () => {
      if (unavailable) throw new Error('Runtime disconnected');
      return read(current);
    } } }));
    await enabledButton('重新读取评测集');
    expect(document.querySelector('.golden-job__spinner')).not.toBeNull();
    unavailable = true;
    await userEvent.setup().click(screen.getByRole('button', { name: '重新读取评测集' }));
    await screen.findByText('无法读取评测集');
    expect(screen.getByRole('region', { name: '当前任务状态' })).toHaveTextContent('上次读取的状态，当前进展尚未确认。');
    expect(document.querySelector('.golden-job__spinner')).toBeNull();
    expect(screen.getByRole('button', { name: '停止任务' })).toBeDisabled();
  });

  it('returns empty later steps to their exact prerequisite without submitting work', async () => {
    const current = suite();
    const transport = new MockControlTransport({ routes: { 'agent.eval-lab.golden.get': read(current) } });
    mount(transport);
    const user = userEvent.setup();
    await enabledButton('让 Agent 起草题目');
    await user.click(screen.getByRole('tab', { name: '审核标准' }));
    await user.click(screen.getByRole('button', { name: '返回起草题目' }));
    expect(screen.getByRole('tab', { name: '起草题目' })).toHaveAttribute('aria-selected', 'true');
    await user.click(screen.getByRole('tab', { name: '校准评审' }));
    await user.click(screen.getByRole('button', { name: '去审核开发题' }));
    expect(screen.getByRole('tab', { name: '审核标准' })).toHaveAttribute('aria-selected', 'true');
    await user.click(screen.getByRole('tab', { name: '冻结与实验' }));
    await user.click(screen.getByRole('button', { name: '去审核题目' }));
    expect(screen.getByRole('tab', { name: '审核标准' })).toHaveAttribute('aria-selected', 'true');
    expect(transport.requests.every(({ request }) => request.pathId === 'agent.eval-lab.golden.get')).toBe(true);
  });

  it('starts with empty sources beside saved suites and creates a suite without a Room side effect', async () => {
    let current = suite();
    const commands: GoldenCommand[] = [];
    const close = vi.fn();
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.golden.get': () => read(current),
      'agent.eval-lab.golden.command': (request: ControlRequest) => { const command = request.body as GoldenCommand; commands.push(command); current = suite({ title: String(command.input.title) }); return receipt(command, current); },
    } });
    mount(transport, { startNew: true, onClose: close });
    const user = userEvent.setup();
    expect(await screen.findByRole('heading', { name: '这次想评测什么？' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Golden 评测集' })).toBeInTheDocument();
    const source = screen.getByRole('textbox', { name: '来源 1 原文' });
    expect(source).toHaveValue('');
    expect(screen.getByRole('textbox', { name: '评测集名称' })).toHaveValue('');
    expect(await screen.findByRole('combobox', { name: '选择评测集' })).toBeInTheDocument();
    await user.type(screen.getByRole('textbox', { name: '评测集名称' }), '新任务评测');
    await user.type(screen.getByRole('textbox', { name: '要评测的任务' }), '回答退款问题');
    await user.type(screen.getByRole('textbox', { name: '来源 1 标题' }), '退款说明');
    await user.type(screen.getByRole('textbox', { name: '来源 1 引用' }), 'docs/refund.md');
    await user.type(source, '期限为七天。');
    await user.click(screen.getByRole('button', { name: '保存来源，建立评测集' }));
    await screen.findByRole('button', { name: '让 Agent 起草题目' });
    expect(commands).toHaveLength(1);
    expect(commands[0]).toMatchObject({ action: 'create', expectedRevision: 0, input: { title: '新任务评测', targetCount: 4 } });
    expect(commands[0].suiteId).toBeUndefined();
    expect(transport.requests.every(({ request }) => request.pathId.startsWith('agent.eval-lab.golden.'))).toBe(true);
    await user.click(screen.getByRole('button', { name: '返回实验工作区' }));
    expect(close).toHaveBeenCalledOnce();
  });

  it('requires explicit review and human sample labels before calibration, then displays only the returned frozen experiment', async () => {
    let current = suite();
    const commands: GoldenCommand[] = [];
    const transport = new MockControlTransport({ routes: {
      'agent.eval-lab.golden.get': () => read(current),
      'agent.eval-lab.golden.command': (request: ControlRequest) => {
        const command = request.body as GoldenCommand; commands.push(structuredClone(command));
        expect(command.expectedRevision).toBe(current.revision);
        const input = command.input;
        if (command.action === 'draft') current = { ...current, revision: 2, cases: [goldenCase(), goldenCase('case-hold', 'holdout')], jobs: [job('completed')] };
        if (command.action === 'review_case') current = { ...current, revision: current.revision + 1, cases: current.cases.map((item) => item.caseId === input.caseId ? { ...item, question: String(input.question), review: { status: input.verdict as 'approved', note: String(input.note), reviewedAtMs: 5000 } } : item) };
        if (command.action === 'label_sample') current = { ...current, revision: current.revision + 1, cases: current.cases.map((item) => ({ ...item, samples: item.samples.map((sample) => sample.sampleId === input.sampleId ? { ...sample, humanVerdict: input.humanVerdict as 'pass' | 'fail', humanNote: String(input.humanNote) } : sample) })) };
        if (command.action === 'calibrate') current = { ...current, calibration: { calibrationId: 'cal-1', suiteRevision: current.revision, judgeConfig: model, judgeProtocolVersion: 'paw.golden.context-qa-judge.v1', judgments: [], metrics: { total: 3, comparable: 3, agreement: 1, falsePasses: 0, falseFails: 0, uncertain: 0 }, ready: true, reasons: [], createdAtMs: 6000 }, jobs: [...current.jobs, job('completed', 'calibrate')] };
        if (command.action === 'freeze') current = { ...current, snapshot: { snapshotId: 'snapshot-1', suiteId: current.suiteId, version: 1, sourceRevision: current.revision, createdAtMs: 7000, developmentCount: 1, holdoutCount: 1, judgeConfig: model, judgeProtocolVersion: 'paw.golden.context-qa-judge.v1' } };
        if (command.action === 'experiment') current = { ...current, jobs: [...current.jobs, job('completed', 'experiment', experimentResult())] };
        return receipt(command, current);
      },
    } });
    mount(transport);
    const user = userEvent.setup();
    await user.click(await enabledButton('让 Agent 起草题目'));
    expect(await screen.findByText('0 / 2 题通过 · 2 题待审核')).toBeInTheDocument();
    expect(commands.map((command) => command.action)).toEqual(['draft']);
    const question = screen.getByRole('textbox', { name: '问题' });
    await user.clear(question); await user.type(question, '来源规定退款期限是多少？');
    await user.click(screen.getByRole('button', { name: '保存并通过' }));
    await screen.findByText('1 / 2 题通过 · 1 题待审核');
    await user.click(within(screen.getByRole('complementary', { name: '题目列表' })).getByRole('button', { name: /申请退款需要什么/ }));
    await user.click(screen.getByRole('button', { name: '保存并通过' }));
    await user.click(await screen.findByRole('button', { name: '继续校准评审' }));
    expect(screen.getAllByRole('radio').every((input) => !(input as HTMLInputElement).checked)).toBe(true);
    expect(screen.getByRole('button', { name: '开始校准评审' })).toBeDisabled();
    await user.click(screen.getByText('模型标识与推理强度', { selector: '[role="tabpanel"]:not([hidden]) summary' }));
    expect(screen.getByRole('textbox', { name: '评审模型' })).toHaveValue('configured-model');
    for (const [category, label] of [['正确样例', '通过'], ['错误样例', '不通过'], ['边界样例', '不通过']] as const) {
      await user.click(within(screen.getByRole('complementary', { name: '校准样例列表' })).getByRole('button', { name: new RegExp(category) }));
      await user.click(screen.getByRole('radio', { name: label }));
      await user.click(screen.getByRole('button', { name: '保存人类标签' }));
      await waitFor(() => expect(screen.queryByText('正在提交本次操作…')).not.toBeInTheDocument());
    }
    await user.click(screen.getByRole('button', { name: '开始校准评审' }));
    expect(await screen.findByRole('heading', { name: '校准通过，可以冻结' })).toBeInTheDocument();
    expect(screen.getByText(/一致率 100%/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '继续冻结与实验' }));
    await user.click(screen.getByRole('button', { name: '冻结当前标准' }));
    expect(await screen.findByLabelText('冻结快照')).toHaveTextContent('paw.golden.context-qa-judge.v1');
    await user.click(within(screen.getByRole('group', { name: '基线' })).getByText('模型标识与推理强度'));
    expect(screen.getByRole('textbox', { name: '基线模型' })).toHaveValue('configured-model');
    await user.click(screen.getByRole('button', { name: '开始冻结集实验' }));
    expect(await screen.findByRole('heading', { name: '候选在本次冻结题集上有改善' })).toBeInTheDocument();
    expect(screen.getByText('成本改善依据模型目录估算，实际费用尚未完整提供。')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: '开发题比较' })).toHaveTextContent('基线 $0.0200，候选 $0.0100');
    expect(screen.getByRole('region', { name: '实验总开销' })).toHaveTextContent('未提供完整成本');
    await user.click(screen.getByText('按用途查看调用开销'));
    expect(screen.getByRole('rowheader', { name: '评审开销' })).toBeInTheDocument();
    expect(commands.map((command) => command.action)).toEqual(['draft', 'review_case', 'review_case', 'label_sample', 'label_sample', 'label_sample', 'calibrate', 'freeze', 'experiment']);
    expect(commands[1].input).toMatchObject({ question: '来源规定退款期限是多少？', verdict: 'approved' });
    expect(commands[1].input).not.toHaveProperty('samples');
    expect(commands.at(-1)?.input).toMatchObject({ snapshotId: 'snapshot-1', baseline: model, candidate: model, optimizePrompt: true, maxCandidates: 1 });
  });

  it('keeps incomplete answerable standards from approval, while rejection remains explicit', async () => {
    const onReview = vi.fn(async () => true);
    render(<CaseReview suite={suite({ cases: [{ ...goldenCase(), requiredFacts: [], evidence: [], rubric: [] }] })} disabled={false} onReview={onReview} onNext={() => {}} />);
    expect(screen.getByRole('button', { name: '保存并通过' })).toBeDisabled();
    await userEvent.setup().click(screen.getByRole('button', { name: '保存并拒绝' }));
    expect(onReview).toHaveBeenCalledWith(expect.objectContaining({ verdict: 'rejected' }));
  });

  it('supports keyboard steps and does not spin or poll interrupted jobs', async () => {
    const current = suite({ jobs: [job('interrupted')] });
    mount(new MockControlTransport({ routes: { 'agent.eval-lab.golden.get': read(current) } }));
    expect(await screen.findByRole('button', { name: '恢复任务' })).toBeInTheDocument();
    const tab = screen.getByRole('tab', { name: '起草题目' });
    tab.focus(); fireEvent.keyDown(tab, { key: 'ArrowRight' });
    expect(screen.getByRole('tab', { name: '审核标准' })).toHaveFocus();
    expect(screen.getByRole('tab', { name: '审核标准' })).toHaveAttribute('aria-selected', 'true');
    expect(document.querySelector('.golden-job__spinner')).toBeNull();
    expect(goldenPollInterval(read(current), false)).toBe(false);
  });

  it('keeps an old frozen snapshot visible after standards change and sends its exact identity', async () => {
    const current = suite({ revision: 5, judgeConfig: { ...model, prompt: '只返回评审判断，不能回答用户问题' }, snapshot: { snapshotId: 'frozen-revision-3', suiteId: 'suite-1', version: 1, sourceRevision: 3, createdAtMs: 6000, developmentCount: 1, holdoutCount: 1, judgeConfig: model } });
    const commands: GoldenCommand[] = [];
    mount(new MockControlTransport({ routes: {
      'agent.eval-lab.golden.get': read(current),
      'agent.eval-lab.golden.command': (request: ControlRequest) => { const command = request.body as GoldenCommand; commands.push(command); return receipt(command, current); },
    } }));
    const user = userEvent.setup();
    await enabledButton('开始冻结集实验');
    expect(screen.getByLabelText('冻结快照')).toHaveTextContent('当前标准已修改，旧快照仍保持不变');
    expect(screen.getByRole('button', { name: '冻结当前标准' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '开始冻结集实验' }));
    await waitFor(() => expect(commands).toHaveLength(1));
    expect(commands[0]).toMatchObject({ action: 'experiment', expectedRevision: 5, input: { snapshotId: 'frozen-revision-3' } });
    expect(commands[0].input.baseline).toEqual(model);
    expect(commands[0].input.candidate).toEqual(model);
  });

  it('shows failed partial receipts without inventing a completed comparison and allows a read retry', async () => {
    const current = suite({ jobs: [{ ...job('failed', 'experiment', { partial: true, usage, error: '运行未返回答案' }), error: '运行未返回答案' }] });
    const transport = new MockControlTransport({ routes: { 'agent.eval-lab.golden.get': read(current) } });
    mount(transport);
    await enabledButton('重新读取评测集');
    const user = userEvent.setup();
    await user.click(screen.getByRole('tab', { name: '冻结与实验' }));
    expect(screen.getByRole('region', { name: '实验结果' })).toHaveTextContent('运行未返回答案');
    expect(screen.queryByRole('region', { name: '开发题比较' })).not.toBeInTheDocument();
    expect(screen.queryByText('候选在本次冻结题集上有改善')).not.toBeInTheDocument();
    expect(document.querySelector('.golden-job__spinner')).toBeNull();
    const before = transport.requests.length;
    await user.click(screen.getByRole('button', { name: '重新读取评测集' }));
    await waitFor(() => expect(transport.requests.length).toBeGreaterThan(before));
    expect(transport.requests.every(({ request }) => request.pathId === 'agent.eval-lab.golden.get')).toBe(true);
  });

  it('reprocesses only explicitly eligible failed draft results through the existing resume nonce recovery', async () => {
    let current = suite({ jobs: [{ ...job('failed'), canReprocess: true, error: '结果格式未能解析' }] });
    const commands: GoldenCommand[] = [];
    const fetchMock = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method !== 'POST') return json(read(current));
      const command = JSON.parse(String(init.body)) as GoldenCommand; commands.push(command);
      current = suite({ revision: 2, cases: [goldenCase()], jobs: [job('completed')] });
      if (commands.length === 1) throw new TypeError('lost after cached result processing');
      return json(receipt(command, current, true));
    });
    const mounted = mount(new HttpControlTransport({ baseUrl: 'http://127.0.0.1:8791', fetch: fetchMock }));
    const user = userEvent.setup();
    await user.click(await enabledButton('重新处理结果'));
    expect(screen.getByText('复用原结果重新处理，不会重新调用模型。')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '起草题目' })).toHaveAttribute('aria-selected', 'true');
    await user.click(await screen.findByRole('button', { name: '核对本次操作' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '核对本次操作' })).not.toBeInTheDocument());
    expect(commands).toHaveLength(2);
    expect(commands[0]).toMatchObject({ action: 'resume', suiteId: 'suite-1', expectedRevision: 1, input: { jobId: 'job-draft' } });
    expect(commands[1]).toEqual(commands[0]);
    expect(screen.queryByRole('button', { name: '重新处理结果' })).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '审核标准' })).toHaveAttribute('aria-selected', 'true');
    mounted.unmount(); mounted.client.clear();

    current = suite({ jobs: [{ ...job('failed'), canReprocess: true }] });
    const direct = mount(new HttpControlTransport({ baseUrl: 'http://127.0.0.1:8791', fetch: fetchMock }));
    await user.click(await enabledButton('重新处理结果'));
    await waitFor(() => expect(screen.getByRole('tab', { name: '审核标准' })).toHaveAttribute('aria-selected', 'true'));
    expect(commands).toHaveLength(3);
    expect(commands[2].action).toBe('resume');
    direct.unmount(); direct.client.clear();

    for (const unavailable of [{ ...job('failed'), canReprocess: false }, job('failed'), { ...job('failed', 'experiment'), canReprocess: true }]) {
      const next = mount(new MockControlTransport({ routes: { 'agent.eval-lab.golden.get': read(suite({ jobs: [unavailable] })) } }));
      await enabledButton('重新读取评测集');
      expect(screen.queryByRole('button', { name: '重新处理结果' })).not.toBeInTheDocument();
      next.unmount(); next.client.clear();
    }
  });

  it('offers only executable Pi thinking levels and requires explicit experiment models', async () => {
    const onExperiment = vi.fn(async () => true);
    const current = suite({ judgeConfig: { ...model, thinkingLevel: 'ultra' }, snapshot: { snapshotId: 'snapshot-1', suiteId: 'suite-1', version: 1, sourceRevision: 1, createdAtMs: 6000, developmentCount: 1, holdoutCount: 1, judgeConfig: model } });
    render(<GoldenExperiment suite={current} disabled={false} onFreeze={() => {}} onExperiment={onExperiment} />);
    const user = userEvent.setup();
    const start = screen.getByRole('button', { name: '开始冻结集实验' });
    expect(start).toBeDisabled();
    const baselineThinking = screen.getByRole('combobox', { name: '基线推理强度' }) as HTMLSelectElement;
    expect([...baselineThinking.options].filter((option) => !option.disabled).map((option) => option.value)).toEqual(['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']);
    expect([...baselineThinking.options].find((option) => option.value === 'ultra')).toBeDisabled();
    await user.selectOptions(baselineThinking, 'high');
    await user.selectOptions(screen.getByRole('combobox', { name: '候选推理强度' }), 'high');
    expect(start).toBeEnabled();
    await user.clear(screen.getByRole('textbox', { name: '基线服务' }));
    expect(start).toBeDisabled();
    await user.type(screen.getByRole('textbox', { name: '基线服务' }), model.provider);
    await user.clear(screen.getByRole('textbox', { name: '候选模型' }));
    expect(start).toBeDisabled();
    await user.type(screen.getByRole('textbox', { name: '候选模型' }), model.model);
    await user.click(start);
    expect(onExperiment).toHaveBeenCalledWith(expect.objectContaining({ baseline: model, candidate: model }));
  });

  it('restores experiment rules and optimization choice after reopening without starting a run', () => {
    const current = suite({ snapshot:{snapshotId:'snapshot-1',suiteId:'suite-1',version:1,sourceRevision:1,createdAtMs:6000,developmentCount:1,holdoutCount:1,judgeConfig:model} });
    const onExperiment = vi.fn();
    const first = render(<GoldenExperiment suite={current} disabled={false} onFreeze={() => {}} onExperiment={onExperiment} />);
    fireEvent.click(screen.getByText('基线回答规则（Prompt）'));
    fireEvent.click(screen.getByText('候选回答规则（Prompt）'));
    fireEvent.change(screen.getByLabelText('基线回答规则',{exact:true}),{target:{value:'保留原始规则'}});
    fireEvent.change(screen.getByLabelText('候选回答规则',{exact:true}),{target:{value:'先提取事实，再检查规则'}});
    fireEvent.click(screen.getByRole('checkbox',{name:'基于开发题自动优化 Prompt'}));
    first.unmount();
    render(<GoldenExperiment suite={current} disabled={false} onFreeze={() => {}} onExperiment={onExperiment} />);
    fireEvent.click(screen.getByText('基线回答规则（Prompt）'));
    fireEvent.click(screen.getByText('候选回答规则（Prompt）'));
    expect(screen.getByLabelText('基线回答规则',{exact:true})).toHaveValue('保留原始规则');
    expect(screen.getByLabelText('候选回答规则',{exact:true})).toHaveValue('先提取事实，再检查规则');
    expect(screen.getByRole('checkbox',{name:'基于开发题自动优化 Prompt'})).not.toBeChecked();
    expect(onExperiment).not.toHaveBeenCalled();
  });

  it('does not submit a blank Judge model or thinking level as a default', async () => {
    const reviewed = goldenCase();
    reviewed.review.status = 'approved';
    reviewed.samples = reviewed.samples.map((sample) => ({ ...sample, humanVerdict: sample.category === 'correct' ? 'pass' : 'fail' }));
    const current = suite({ cases: [reviewed], judgeConfig: { ...model, thinkingLevel: '' } });
    const onJudge = vi.fn(async () => true);
    const onCalibrate = vi.fn();
    const props = { disabled: false, onLabel: vi.fn(async () => true), onJudge, onCalibrate, onNext: () => {} };
    const rendered = render(<CalibrationPanel suite={current} {...props} />);
    const user = userEvent.setup();
    expect(screen.getByRole('button', { name: '开始校准评审' })).toBeDisabled();
    const thinking = screen.getByRole('combobox', { name: '评审推理强度' }) as HTMLSelectElement;
    expect([...thinking.options].find((option) => option.value === '')).toBeDisabled();
    await user.selectOptions(thinking, 'high');
    await user.clear(screen.getByRole('textbox', { name: '评审模型' }));
    expect(screen.getByRole('button', { name: '保存评审设置' })).toBeDisabled();
    await user.type(screen.getByRole('textbox', { name: '评审模型' }), model.model);
    await user.click(screen.getByRole('button', { name: '保存评审设置' }));
    expect(onJudge).toHaveBeenCalledWith({ judgeConfig: model });
    expect(onCalibrate).not.toHaveBeenCalled();
    rendered.rerender(<CalibrationPanel suite={{ ...current, judgeConfig: model }} {...props} />);
    await user.click(await enabledButton('开始校准评审'));
    expect(onCalibrate).toHaveBeenCalledOnce();
  });

  it('leads completed experiments with results, keeps the full scenario expandable, and can submit the next round', async () => {
    const scenario = '此合成验收数据用于验证界面，题目、人工标签和模型回执都是自动化测试夹具，不代表真实业务结果。';
    let current = suite({ title: '四步流程验收 · 合成数据', scenario, revision: 7, jobs: [job('completed', 'experiment', experimentResult())], snapshot: { snapshotId: 'snapshot-1', suiteId: 'suite-1', version: 1, sourceRevision: 7, createdAtMs: 6000, developmentCount: 1, holdoutCount: 1, judgeConfig: model } });
    const commands: GoldenCommand[] = [];
    mount(new MockControlTransport({ routes: {
      'agent.eval-lab.golden.get': () => read(current),
      'agent.eval-lab.golden.command': (request: ControlRequest) => { const command = request.body as GoldenCommand; commands.push(command); current = { ...current, jobs: [...current.jobs, { ...job('running', 'experiment'), jobId: 'next-experiment', createdAtMs: 8000 }] }; return receipt(command, current); },
    } }));
    const user = userEvent.setup();
    await screen.findByRole('heading', { name: '候选在本次冻结题集上有改善' });
    const settings = screen.getByText('设置下一轮实验');
    const result = screen.getByRole('region', { name: '实验结果' });
    expect(result.parentElement?.firstElementChild).toBe(result);
    expect(screen.queryByRole('textbox', { name: '基线模型' })).not.toBeInTheDocument();
    expect(screen.queryByText(scenario)).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '四步流程验收 · 合成数据' })).toBeInTheDocument();
    expect(screen.getByText('标准版本 7')).toBeInTheDocument();
    await user.click(screen.getByText('任务说明'));
    expect(screen.getByText(scenario)).toBeVisible();
    expect(screen.getByRole('region', { name: '留出题比较' }).compareDocumentPosition(screen.getByRole('region', { name: '开发题逐题差异' })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await user.click(settings);
    await user.click(within(screen.getByRole('group', { name: '候选' })).getByText('模型标识与推理强度'));
    const candidateModel = screen.getByRole('textbox', { name: '候选模型' });
    await user.clear(candidateModel); await user.type(candidateModel, 'next-model');
    await user.click(await enabledButton('开始冻结集实验'));
    await waitFor(() => expect(commands).toHaveLength(1));
    expect(commands[0]).toMatchObject({ action: 'experiment', expectedRevision: 7, input: { snapshotId: 'snapshot-1', candidate: { ...model, model: 'next-model' } } });
    expect(await screen.findByLabelText('当前任务状态')).toHaveTextContent('运行实验 · 进行中');
  });

  it.each(['answer', 'verdict', 'note'] as const)('keeps unsaved sample %s changes visible and gates calibration until a successful save', async (field) => {
    const reviewed = goldenCase();
    reviewed.review.status = 'approved';
    reviewed.requiredFacts = ['必须说明七天期限'];
    reviewed.samples = reviewed.samples.map((sample) => ({ ...sample, humanVerdict: sample.category === 'correct' ? 'pass' : 'fail' }));
    let current = suite({ cases: [reviewed], calibration: { calibrationId: 'cal-ready', suiteRevision: 1, judgeConfig: model, judgments: [], metrics: { total: 3, comparable: 3, agreement: 1, falsePasses: 0, falseFails: 0, uncertain: 0 }, ready: true, reasons: [], createdAtMs: 6000 } });
    const onLabel = vi.fn<(input: GoldenCommand['input']) => Promise<boolean>>().mockResolvedValueOnce(false);
    const props = { disabled: false, onLabel, onJudge: vi.fn(async () => true), onCalibrate: vi.fn(), onNext: vi.fn() };
    const rendered = render(<CalibrationPanel suite={current} {...props} />);
    const user = userEvent.setup();
    expect(screen.getByRole('button', { name: '继续冻结与实验' })).toBeEnabled();
    expect(screen.getByRole('heading', { name: '必须回答的事实' })).toBeInTheDocument();
    expect(screen.getByText('必须说明七天期限')).toBeInTheDocument();
    expect(screen.getByText('依据来源回答，不得虚构')).toBeInTheDocument();
    expect(screen.getByText('退款期限为七天。')).toBeInTheDocument();

    if (field === 'answer') { await user.clear(screen.getByRole('textbox', { name: '待标注答案' })); await user.type(screen.getByRole('textbox', { name: '待标注答案' }), '尚未保存的答案'); }
    if (field === 'verdict') await user.click(screen.getByRole('radio', { name: '不通过' }));
    if (field === 'note') await user.type(screen.getByRole('textbox', { name: '标注说明' }), '尚未保存的判断依据');
    const list = within(screen.getByRole('complementary', { name: '校准样例列表' }));
    expect(list.getByRole('button', { name: /正确样例.*未保存修改/u })).toBeInTheDocument();
    expect(screen.getByText(/1 个样例有未保存修改/u)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重新校准评审' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '继续冻结与实验' })).toBeDisabled();
    expect(screen.queryByRole('heading', { name: '校准通过，可以冻结' })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '当前修改尚未校准' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '保存人类标签' }));
    expect(onLabel).toHaveBeenCalledOnce();
    expect(list.getByRole('button', { name: /正确样例.*未保存修改/u })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重新校准评审' })).toBeDisabled();
    if (field === 'answer') expect(screen.getByRole('textbox', { name: '待标注答案' })).toHaveValue('尚未保存的答案');
    if (field === 'verdict') expect(screen.getByRole('radio', { name: '不通过' })).toBeChecked();
    if (field === 'note') expect(screen.getByRole('textbox', { name: '标注说明' })).toHaveValue('尚未保存的判断依据');
    await user.click(list.getByRole('button', { name: /错误样例/u }));
    expect(screen.getByRole('button', { name: '重新校准评审' })).toBeDisabled();
    await user.click(list.getByRole('button', { name: /正确样例/u }));

    onLabel.mockImplementationOnce(async (input) => {
      current = { ...current, revision: 2, calibration: null, cases: current.cases.map((item) => ({ ...item, samples: item.samples.map((sample) => sample.sampleId === input.sampleId ? { ...sample, answer: String(input.answer), humanVerdict: input.humanVerdict as 'pass' | 'fail', humanNote: String(input.humanNote) } : sample) })) };
      rendered.rerender(<CalibrationPanel suite={current} {...props} />);
      return true;
    });
    await user.click(screen.getByRole('button', { name: '保存人类标签' }));
    expect(onLabel).toHaveBeenCalledTimes(2);
    expect(list.queryByRole('button', { name: /未保存修改/u })).not.toBeInTheDocument();
    expect(screen.queryByText(/个样例有未保存修改/u)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存人类标签' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '继续冻结与实验' })).not.toBeInTheDocument();
    expect(props.onCalibrate).not.toHaveBeenCalled();
    expect(props.onNext).not.toHaveBeenCalled();
  });
});

describe('Golden command recovery', () => {
  it.each(['network', 500, 503])('restores an unknown %s request and retries its exact nonce without replacing newer GET state', async (failure) => {
    let current = suite();
    const commands: GoldenCommand[] = [];
    const oldReceiptSuite = suite({ revision: 2, cases: [goldenCase()] });
    const fetchMock = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method !== 'POST') return json(read(current));
      const command = JSON.parse(String(init.body)) as GoldenCommand; commands.push(command);
      if (commands.length === 1) {
        current = oldReceiptSuite;
        if (failure === 'network') throw new TypeError('response lost');
        return json({ ok: false, code: 'AGENT_LAB_GOLDEN_SERVICE_UNAVAILABLE' }, Number(failure));
      }
      return json(receipt(command, oldReceiptSuite, true));
    });
    const first = mount(new HttpControlTransport({ baseUrl: 'http://127.0.0.1:8791', fetch: fetchMock }));
    const user = userEvent.setup();
    await user.click(await enabledButton('让 Agent 起草题目'));
    await screen.findByRole('button', { name: '核对本次操作' });
    expect(commands).toHaveLength(1);
    first.unmount(); first.client.clear();
    current = suite({ revision: 7, title: '已人工修订的题集', cases: [goldenCase()] });
    mount(new HttpControlTransport({ baseUrl: 'http://127.0.0.1:8791/', fetch: fetchMock }));
    await screen.findByRole('heading', { name: '已人工修订的题集' });
    expect(commands).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: '核对本次操作' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '核对本次操作' })).not.toBeInTheDocument());
    expect(commands).toEqual([commands[0], commands[0]]);
    expect(screen.getByText('标准版本 7')).toBeInTheDocument();
    expect(Object.keys(window.sessionStorage).filter((key) => key.startsWith('paw.lab.golden-command.v1:'))).toEqual([]);
  });

  it.each([409, 422])('releases a known %s rejection and uses the newly read revision for a new command', async (status) => {
    let current = suite();
    const commands: GoldenCommand[] = [];
    const fetchMock = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method !== 'POST') return json(read(current));
      const command = JSON.parse(String(init.body)) as GoldenCommand; commands.push(command);
      if (commands.length === 1) { current = suite({ revision: 2 }); return json({ ok: false, code: 'AGENT_LAB_GOLDEN_CONFLICT', message: '标准版本已经更新。' }, status); }
      return json(receipt(command, current));
    });
    mount(new HttpControlTransport({ baseUrl: 'http://127.0.0.1:8791', fetch: fetchMock }));
    const user = userEvent.setup();
    await user.click(await enabledButton('让 Agent 起草题目'));
    expect(await screen.findByRole('alert')).toHaveTextContent('标准版本已经更新');
    await waitFor(() => expect(screen.getByRole('button', { name: '让 Agent 起草题目' })).toBeEnabled());
    await user.click(screen.getByRole('button', { name: '让 Agent 起草题目' }));
    await waitFor(() => expect(commands).toHaveLength(2));
    expect(commands[1].expectedRevision).toBe(2);
    expect(commands[1].clientRequestId).not.toBe(commands[0].clientRequestId);
    expect(screen.queryByRole('button', { name: '核对本次操作' })).not.toBeInTheDocument();
  });

  it('bounds a transport that never returns and preserves an uncertain outcome', async () => {
    vi.useFakeTimers();
    const transport = new MockControlTransport({ routes: { 'agent.eval-lab.golden.command': () => new Promise(() => {}) } });
    const pending = submitGoldenCommand(transport, { action: 'draft', suiteId: 'suite-1', expectedRevision: 1, clientRequestId: 'lab-golden:pending', input: {} });
    const assertion = expect(pending).rejects.toThrow('服务未及时返回回执');
    await vi.advanceTimersByTimeAsync(20_000);
    await assertion;
    expect(isGoldenRejection(new Error('timeout'))).toBe(false);
    expect(isGoldenRejection({ status: 503, payload: { code: 'AGENT_LAB_GOLDEN_VALIDATION' } })).toBe(false);
  });

  it('polls only real queued or running jobs and rejects malformed result fields', () => {
    for (const state of ['queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted'] as const) {
      const data = read(suite({ jobs: [job(state)] }));
      expect(goldenPollInterval(data, false)).toBe(state === 'queued' || state === 'running' ? 2_000 : false);
      expect(goldenPollInterval(data, true)).toBe(false);
    }
    expect(() => parseGoldenRead({ ok: true, items: [suite()], suite: { suiteId: 'suite-1' } })).toThrow('状态不完整');
    expect(isExperimentResult(experimentResult())).toBe(true);
    expect(isExperimentResult({ ...experimentResult(), receipts: [{ stage: { message: 'malformed' } }] })).toBe(false);
    expect(isExperimentResult({ partial: true, usage, error: 'interrupted' })).toBe(false);
  });
});
