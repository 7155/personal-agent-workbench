import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { previewEvalLabRuns } from '@/app/preview-eval-lab-data';
import { previewRoomSnapshot } from '@/app/preview-room-data';
import { PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import { MockControlTransport } from '@/test/mock-transport';
import type { ControlRequest } from '@/platform/transport';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import { parseEvalLabRunsResponse, type EvalLabEvidenceRun, type EvalLabExperiment } from './api';
import { EvalLabFeature } from './index';
import { ExperimentWorkspace } from './ExperimentWorkspace';
import { evidenceTraceIds } from './evidence-identity';
import { buildOptimizationWorkbenchModel } from './optimization/optimization-view-model';
import { ENTERPRISE_RAG_SCENE_ID, type SceneRecipeState } from './scene-recipes';
import type { TrialJob } from './trials/api';

vi.mock('@/paw-os/apps/PawRoomWorkspace', () => ({
  PawRoomWorkspace: ({ recordId }: { recordId: string }) => <section aria-label="运行中的原生 Room" data-room-id={recordId} />,
}));

afterEach(() => { cleanup(); sessionStorage.clear(); useRoomLiveStore.getState().reset(); });

const preview = parseEvalLabRunsResponse(previewEvalLabRuns());
const experiment: EvalLabExperiment = {
  ...structuredClone(preview.experiments.find((item) => item.evaluationKind === 'answer_evidence')!),
  experimentId: 'enterprise-rag.luna-prompt-v4-standard-r6.v1',
  title: 'Enterprise RAG · Luna + Prompt-v4',
  vertical: 'enterprise-knowledge',
  projectionState: 'current',
  status: 'kept',
  businessProblem: '保持答案与引用质量，同时减少用量。',
  baseline: { runId: 'rag-baseline', metrics: { answerCaseCount: 1, answerJudgeCorrectnessRate: 0 }, evidenceRefs: [] },
  candidate: { runId: 'rag-candidate', metrics: { answerCaseCount: 1, answerJudgeCorrectnessRate: 1 }, evidenceRefs: [] },
  comparison: { decision: 'keep', decisionReason: '这个候选通过同一组验收。', metricDeltas: [] },
  dataset: { datasetId: 'rag-standard', split: 'validation', caseCount: 1, unit: '问答', manifestSha256: 'c'.repeat(64), heldOutConsumed: false },
  optimizationEvidence: {
    status: 'partial',
    provenance: 'existing_run_artifacts',
    patch: {
      status: 'available', kind: 'frozen_configuration', artifactPath: 'conditions.json',
      beforeRef: 'baseline:conditions', afterRef: 'candidate:conditions',
      unifiedDiff: '--- baseline/conditions.json\n+++ candidate/conditions.json\n-  "model": "sol"\n+  "model": "luna"',
      reason: '这是实际保存的两份运行配置差异。',
    },
    baselineTrace: { runId: 'rag-baseline', status: 'unavailable', traceIds: [], reason: '当前运行没有保存可打开的基线 Trace。' },
    caseComparisons: [{ caseId: 'case-01', before: { status: 'failed', metrics: { citationFactCoverage: .6 } }, after: { status: 'passed', metrics: { citationFactCoverage: 1 } } }],
    validationBoundary: { candidateAware: true, candidateBlind: false, heldOutOpened: false, unbiasedPromotionClaimAllowed: false, costAuthority: 'runtime_cost_reconciled' },
    gaps: ['尚无可打开的原 Trace。'],
  },
};

function evidenceRun(runId: string, traceId: string): EvalLabEvidenceRun {
  return {
    runId, title: runId, family: 'enterprise-rag', split: 'validation', status: 'completed',
    evidenceKind: 'transcript_and_report', reportAvailable: true, databaseAvailable: true,
    sessionCount: 1, transcriptCount: 1, transcriptBytes: 100, metrics: {},
    environment: { traceIds: traceId ? [traceId] : [] }, updatedAtMs: 1,
    tasks: [{
      taskIndex: 1, taskLabel: '整批问答 Session', title: '整批问答 Session', transcriptAvailable: true,
      transcriptSha256: 'a'.repeat(64), transcriptBytes: 100, jsonlLines: 2, userMessages: 1,
      assistantMessages: 1, toolCalls: 1, toolFailures: 0, toolNames: ['search'], externalSessionRef: 'session:batch',
      model: 'luna', thinking: 'high', executionMode: 'full_trust', createdAtMs: 1, updatedAtMs: 1, evidenceStatus: 'available',
      taskSucceeded: true,
    }],
  };
}

function catalog(runs: EvalLabEvidenceRun[] = []) {
  return {
    schemaVersion: 'rag-ime.eval-lab-evidence.v1', ok: true,
    source: { available: true, label: 'Test receipt', runCount: runs.length, sessionCount: runs.length, transcriptCount: runs.length, transcriptBytes: 100 },
    runs, total: runs.length,
  };
}

function makeTransport(extra: Record<string, unknown> = {}) {
  const sceneState: SceneRecipeState = {
    schemaVersion: 'rag-ime.agent-lab-scene-recipe-state.v1', ok: true, sceneId: ENTERPRISE_RAG_SCENE_ID, revision: 0,
    activeVersion: {
      schemaVersion: 'rag-ime.agent-lab-scene-recipe-version.v1', sceneId: ENTERPRISE_RAG_SCENE_ID,
      versionId: 'enterprise-rag.validation.incumbent.v1', title: '内置默认', origin: 'runner_builtin',
      recipe: { provider: 'openai-codex', model: 'gpt-5.6-sol', thinkingLevel: 'max', promptProfile: 'incumbent', promptContractVersion: 'rag-agent-evidence-state-budget-routing-v19', agenticSupplementalLimit: 6, answerOnly: true, developmentOnly: true, split: 'validation', candidateAware: true, unbiasedPromotionClaimAllowed: false },
      sourceExperimentId: '', sourceCandidateRunId: '',
    },
    previousVersion: null, rollbackAvailable: false,
    candidate: { available: false, reasonCode: 'candidate_identity_mismatch', reason: '这个候选尚未登记为场景版本。', experimentId: experiment.experimentId, version: null },
    lastEvent: null, effectScope: 'future_validation_runs',
  };
  return new MockControlTransport({
    pickedFiles: [{ id: 'workspace', name: 'candidate', mimeType: 'inode/directory', byteSize: 0, path: '/workspace/candidates' }],
    routes: {
      'agent.eval-lab.runs': { schemaVersion: 'rag-ime.eval-lab-run-list.v1', ok: true, items: [], total: 0, experiments: [preview.experiments[0], experiment], experimentTotal: 2 },
      'agent.eval-lab.evidence': catalog(),
      'agent.eval-lab.scene-recipes.get': sceneState,
      'agent.rooms.list': { items: [] },
      'agent.roles.list': { items: [
        { schemaVersion: 'rag-ime.agent-persona.v1', roleId: 'coordinator', version: '1', displayName: '协调者' },
        { schemaVersion: 'rag-ime.agent-persona.v1', roleId: 'implementer', version: '1', displayName: '实施者' },
      ] },
      ...extra,
    },
  });
}

async function showLab(transport: MockControlTransport, route = '/eval-lab', openExisting = true) {
  const openRoute = vi.fn();
  render(<MemoryRouter initialEntries={[route]}><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <ControlTransportProvider transport={transport}><PawOsDesktopProvider openWindow={vi.fn()} openRoute={openRoute}><EvalLabFeature /></PawOsDesktopProvider></ControlTransportProvider>
  </QueryClientProvider></MemoryRouter>);
  if (openExisting) {
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '项目更多操作' }));
    await user.click(screen.getByRole('menuitem', { name: '已有实验' }));
  }
  return { openRoute };
}

describe('experiment workspace', () => {
  it('opens the just-completed Trial as its own result and keeps the saved experiment comparison separate', async () => {
    let jobs: TrialJob[] = [];
    const transport = makeTransport({
      'agent.eval-lab.trials.get': () => ({ schemaVersion: 'rag-ime.agent-lab-trial.v1', jobs, registeredSceneIds: ['enterprise-rag'] }),
      'agent.eval-lab.trials.start': (request: ControlRequest) => {
        const command = request.body as Record<string, unknown>;
        const job: TrialJob = { jobId: 'trial-new-result', clientRequestId: String(command.clientRequestId), sceneId: 'enterprise-rag', state: 'completed',
          publicSpec: { model: 'actual-trial-model', candidatePrompt: { enabled: true, sha256: 'a'.repeat(64), charCount: 12 } },
          cancelRequested: false, progress: '', error: '', sessions: [{ sessionId: 'trial-session', turnId: 'trial-turn' }],
          result: { caseCount: 2, metrics: { answerJudgeCorrectnessRate: .5 } }, createdAtMs: 4000, updatedAtMs: 5000, resumeAvailable: false };
        jobs = [job];
        return { schemaVersion: 'rag-ime.agent-lab-trial.v1', job, replayed: false };
      },
    });
    await showLab(transport);
    const user = userEvent.setup();
    await screen.findByRole('heading', { name: '保留这个候选' });
    await user.click(screen.getByRole('tab', { name: '运行' }));
    await user.click(await screen.findByRole('button', { name: '运行验证' }));
    await screen.findByText('已完成', { selector: 'strong' });
    expect(screen.getByRole('button', { name: '查看本次验证结果' })).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: '检查结果' }));
    expect(await screen.findByRole('heading', { name: '本次验证结果' })).toBeInTheDocument();
    const result = screen.getByRole('region', { name: '所选验证记录' });
    expect(result).toHaveTextContent('trial-new-result');
    expect(result).toHaveTextContent('actual-trial-model');
    expect(result).toHaveTextContent('50%');
    expect(result).toHaveTextContent('尚无可核对的质量结论');
    expect(result).toHaveTextContent('暂不可核对');
    expect(screen.queryByRole('heading', { name: '保留这个候选' })).not.toBeInTheDocument();
    expect(result).toHaveTextContent('尚未关联同条件基线');
    await user.click(screen.getByRole('button', { name: '查看已保存实验对比' }));
    expect(await screen.findByRole('heading', { name: '保留这个候选' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '返回所选验证记录' }));
    expect(await screen.findByRole('region', { name: '所选验证记录' })).toHaveTextContent('trial-new-result');
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.eval-lab.trials.start')).toHaveLength(1);
  });

  it('keeps a pending Trial request as the result target while its admission receipt is still outstanding', async () => {
    let resolveStart!: (value: unknown) => void;
    let requestId = '';
    const previous: TrialJob = { jobId: 'trial-previous', clientRequestId: 'previous-request', sceneId: 'enterprise-rag', state: 'completed', publicSpec: { model: 'previous-model' }, cancelRequested: false, progress: '', sessions: [], result: { metrics: { passRate: 1 } }, error: '', createdAtMs: 1000, updatedAtMs: 2000, resumeAvailable: false };
    const transport = makeTransport({
      'agent.eval-lab.trials.get': { schemaVersion: 'rag-ime.agent-lab-trial.v1', jobs: [previous], registeredSceneIds: ['enterprise-rag'] },
      'agent.eval-lab.trials.start': (request: ControlRequest) => { requestId = String((request.body as Record<string, unknown>).clientRequestId); return new Promise((resolve) => { resolveStart = resolve; }); },
    });
    await showLab(transport);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: '运行' }));
    await user.click(await screen.findByRole('button', { name: '运行验证' }));
    await user.click(screen.getByRole('tab', { name: '检查结果' }));
    expect(await screen.findByRole('heading', { name: '本次验证结果' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: '所选验证记录' })).toHaveTextContent('正在等待这次验证的启动回执');
    expect(screen.getByRole('region', { name: '所选验证记录' })).not.toHaveTextContent('trial-previous');
    expect(screen.getByRole('region', { name: '所选验证记录' })).not.toHaveTextContent('100%');
    expect(screen.queryByRole('heading', { name: '保留这个候选' })).not.toBeInTheDocument();
    const job: TrialJob = { ...previous, jobId: 'trial-late-receipt', clientRequestId: requestId, state: 'failed', publicSpec: { model: 'trial-model' }, result: null, error: 'execution failed', createdAtMs: 4000, updatedAtMs: 5000 };
    await act(async () => resolveStart({ schemaVersion: 'rag-ime.agent-lab-trial.v1', job, replayed: false }));
    expect(await screen.findByText('trial-late-receipt')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: '所选验证记录' })).toHaveTextContent('执行失败');
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.eval-lab.trials.start')).toHaveLength(1);
  });

  it('keeps a rejected Trial request separate from prior success until history is explicitly selected', async () => {
    const previous: TrialJob = { jobId: 'trial-prior-success', clientRequestId: 'prior-request', sceneId: 'enterprise-rag', state: 'completed', publicSpec: { model: 'prior-model' }, cancelRequested: false, progress: '', sessions: [], result: { metrics: { passRate: 1 } }, error: '', createdAtMs: 1000, updatedAtMs: 2000, resumeAvailable: false };
    const transport = makeTransport({
      'agent.eval-lab.trials.get': { schemaVersion: 'rag-ime.agent-lab-trial.v1', jobs: [previous], registeredSceneIds: ['enterprise-rag'] },
      'agent.eval-lab.trials.start': () => { throw Object.assign(new Error('unsupported model'), { status: 422 }); },
    });
    await showLab(transport);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: '运行' }));
    await user.click(await screen.findByRole('button', { name: '运行验证' }));
    await screen.findByText('这组设置未能启动。请检查模型与场景执行环境后重试。');
    await user.click(screen.getByRole('tab', { name: '检查结果' }));
    const result = await screen.findByRole('region', { name: '所选验证记录' });
    expect(result).not.toHaveTextContent('trial-prior-success');
    expect(result).not.toHaveTextContent('100%');
    expect(result).toHaveTextContent('本次启动未被接受');
    await user.click(screen.getByRole('button', { name: '返回运行与历史' }));
    await user.click(screen.getByRole('tab', { name: '检查结果' }));
    expect(screen.getByRole('region', { name: '所选验证记录' })).toHaveTextContent('本次启动未被接受');
    await user.click(screen.getByRole('button', { name: '返回运行与历史' }));
    await user.click(screen.getByText('此前的执行 · 1 次'));
    await user.click(screen.getByRole('button', { name: /已完成/ }));
    await user.click(screen.getByRole('button', { name: '查看本次验证结果' }));
    expect(await screen.findByRole('region', { name: '所选验证记录' })).toHaveTextContent('trial-prior-success');
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.eval-lab.trials.start')).toHaveLength(1);
  });

  it('preserves a created Room and the exact dispatch through an unknown send outcome and edited setup', async () => {
    const source = previewRoomSnapshot('room-dispatch-recovery');
    const room = { ...source.room, ownerAppId: 'extension:agent-lab', surfaceKey: `candidate.${experiment.experimentId}`.slice(0, 64), lastEventSequence: 0, workItems: [] };
    let attempts = 0;
    let readable = false;
    const transport = makeTransport({
      'agent.rooms.create': { ok: true, room },
      'agent.room.snapshot': () => { if (!readable) throw new TypeError('snapshot unavailable'); return { ...source, room, events: [], firstSequence: 0, lastSequence: 0, resumeToken: '' }; },
      'agent.room.message': () => { if (++attempts === 1) throw new TypeError('network disconnected after send'); return { ok: true }; },
    });
    await showLab(transport);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: '设置实验' }));
    await user.click(screen.getByRole('button', { name: '选择候选目录' }));
    await user.click(screen.getByRole('button', { name: '开始实验' }));
    expect(await screen.findByLabelText('运行中的原生 Room')).toHaveAttribute('data-room-id', room.id);
    await screen.findByRole('button', { name: '核对并重试原派发' });
    cleanup();
    await showLab(transport);
    await screen.findByRole('button', { name: '核对并重试原派发' });
    await user.click(screen.getByRole('tab', { name: '设置实验' }));
    fireEvent.change(screen.getByRole('textbox', { name: '优化目标' }), { target: { value: '下次再提交的另一个目标' } });
    expect(screen.getByRole('button', { name: '开始实验' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '刷新 Room 状态' }));
    expect(attempts).toBe(1);
    await user.click(screen.getByRole('button', { name: '核对并重试原派发' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '核对并重试原派发' })).toBeEnabled());
    expect(attempts).toBe(1);
    readable = true;
    await user.click(screen.getByRole('button', { name: '核对并重试原派发' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '核对并重试原派发' })).not.toBeInTheDocument());
    const writes = transport.requests.filter(({ request }) => request.pathId === 'agent.room.message');
    expect(writes).toHaveLength(2);
    expect(writes[1].request.body).toEqual(writes[0].request.body);
    expect(writes[1].request.params).toEqual({ roomId: room.id });
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.rooms.create')).toHaveLength(1);
    expect(screen.getByRole('textbox', { name: '优化目标' })).toHaveValue('下次再提交的另一个目标');
  });

  it.each(['in_flight', 'unresolved'])('keeps a %s Room dispatch read-only until its original admission is settled', async (recoveryState) => {
    const source = previewRoomSnapshot('room-dispatch-pending');
    const room = { ...source.room, ownerAppId: 'extension:agent-lab', surfaceKey: `candidate.${experiment.experimentId}`.slice(0, 64), lastEventSequence: 0, workItems: [] };
    const transport = makeTransport({
      'agent.rooms.create': { ok: true, room },
      'agent.room.snapshot': { ...source, room, events: [], firstSequence: 0, lastSequence: 0, resumeToken: '' },
      'agent.room.message': (request: ControlRequest) => { throw Object.assign(new Error('original command pending'), { payload: { code: 'AGENT_COMMAND_PENDING', commandReceipt: { state: 'pending', clientMessageId: (request.body as Record<string, unknown>).clientMessageId, recoveryState } } }); },
    });
    await showLab(transport);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: '设置实验' }));
    await user.click(screen.getByRole('button', { name: '选择候选目录' }));
    await user.click(screen.getByRole('button', { name: '开始实验' }));
    await user.click(await screen.findByRole('button', { name: '刷新 Room 状态' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新 Room 状态' })).toBeEnabled());
    expect(screen.queryByRole('button', { name: '核对并重试原派发' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '在原 Room 重试派发' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('运行中的原生 Room')).toHaveAttribute('data-room-id', room.id);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.message')).toHaveLength(1);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.rooms.create')).toHaveLength(1);
  });

  it('settles a lost first-dispatch receipt from the Room snapshot without replaying the message', async () => {
    const source = previewRoomSnapshot('room-dispatch-accepted');
    const room = { ...source.room, ownerAppId: 'extension:agent-lab', surfaceKey: `candidate.${experiment.experimentId}`.slice(0, 64), lastEventSequence: 1, workItems: [] };
    let sent: Record<string, unknown> = {};
    const transport = makeTransport({
      'agent.rooms.create': { ok: true, room },
      'agent.room.message': (request: ControlRequest) => { sent = request.body as Record<string, unknown>; throw new TypeError('receipt lost'); },
      'agent.room.snapshot': () => ({ ...source, room, events: [{ ...source.events[0], payload: { ...source.events[0].payload, text: sent.message, clientMessageId: sent.clientMessageId } }], firstSequence: 1, lastSequence: 1, resumeToken: `${room.id}:1` }),
    });
    await showLab(transport);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: '设置实验' }));
    await user.click(screen.getByRole('button', { name: '选择候选目录' }));
    await user.click(screen.getByRole('button', { name: '开始实验' }));
    await user.click(await screen.findByRole('button', { name: '刷新 Room 状态' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '核对并重试原派发' })).not.toBeInTheDocument());
    expect(screen.getByLabelText('运行中的原生 Room')).toHaveAttribute('data-room-id', room.id);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.message')).toHaveLength(1);
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.rooms.create')).toHaveLength(1);
  });

  it('retries an authoritatively failed dispatch only on request, in the existing Room with a new attempt identity', async () => {
    const source = previewRoomSnapshot('room-dispatch-failed');
    const room = { ...source.room, ownerAppId: 'extension:agent-lab', surfaceKey: `candidate.${experiment.experimentId}`.slice(0, 64), lastEventSequence: 0, workItems: [] };
    let attempts = 0;
    const transport = makeTransport({
      'agent.rooms.create': { ok: true, room },
      'agent.room.snapshot': { ...source, room, events: [], firstSequence: 0, lastSequence: 0, resumeToken: '' },
      'agent.room.message': (request: ControlRequest) => {
        if (++attempts === 1) throw Object.assign(new Error('participant busy'), { payload: { code: 'AGENT_COMMAND_FAILED', commandReceipt: { state: 'failed', clientMessageId: (request.body as Record<string, unknown>).clientMessageId, causeCode: 'ROOM_PARTICIPANT_BUSY' } } });
        return { ok: true };
      },
    });
    await showLab(transport);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: '设置实验' }));
    await user.click(screen.getByRole('button', { name: '选择候选目录' }));
    await user.click(screen.getByRole('button', { name: '开始实验' }));
    await screen.findByRole('button', { name: '在原 Room 重试派发' });
    expect(attempts).toBe(1);
    await user.click(screen.getByRole('button', { name: '在原 Room 重试派发' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '在原 Room 重试派发' })).not.toBeInTheDocument());
    const writes = transport.requests.filter(({ request }) => request.pathId === 'agent.room.message').map(({ request }) => request);
    expect(writes).toHaveLength(2);
    expect(writes[1].params).toEqual(writes[0].params);
    expect((writes[1].body as Record<string, unknown>).message).toEqual((writes[0].body as Record<string, unknown>).message);
    expect((writes[1].body as Record<string, unknown>).clientMessageId).not.toEqual((writes[0].body as Record<string, unknown>).clientMessageId);
    expect(writes[1].body).not.toHaveProperty('retryOfClientMessageId');
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.rooms.create')).toHaveLength(1);
  });

  it('retains the selected step and unsent experiment settings when refreshing the catalog fails', async () => {
    let unavailable = false;
    const saved = { schemaVersion: 'rag-ime.eval-lab-run-list.v1', ok: true, items: [], total: 0, experiments: [experiment], experimentTotal: 1 };
    const transport = makeTransport({ 'agent.eval-lab.runs': () => {
      if (unavailable) throw new Error('Runtime disconnected');
      return saved;
    } });
    await showLab(transport);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: '设置实验' }));
    const goal = screen.getByRole('textbox', { name: '优化目标' });
    await user.clear(goal);
    await user.type(goal, '保留这个尚未提交的实验目标');
    unavailable = true;
    await user.click(screen.getByRole('button', { name: '刷新' }));
    expect(await screen.findByText('更新暂时失败，仍显示上次读取的实验')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '设置实验' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('textbox', { name: '优化目标' })).toBe(goal);
    expect(goal).toHaveValue('保留这个尚未提交的实验目标');
    unavailable = false;
    await user.click(screen.getByRole('button', { name: '重新读取实验' }));
    expect(await screen.findByRole('textbox', { name: '优化目标' })).toHaveValue('保留这个尚未提交的实验目标');
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.rooms.create')).toBe(false);
  });

  it('makes Model, Prompt, RAG, Tool, Skill and Workflow explicit optimization targets', async () => {
    render(<ControlTransportProvider transport={makeTransport()}><ExperimentWorkspace
      experiment={experiment} title="企业知识库问答" scene="Validation" datasetSummary="1 个任务" decision="保留候选"
      busy={false} selector={null} baseline={null} results={null} application={null} room={null} roomCount={0}
      onStart={vi.fn().mockResolvedValue(undefined)} onDiscuss={vi.fn()}
    /></ControlTransportProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole('tab', { name: '设置实验' }));
    const target = screen.getByRole('combobox', { name: '优化对象' });
    expect(target).toHaveValue('prompt');
    expect(within(target).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'Model / 模型与成本', 'Prompt / 回答规则', 'RAG / 检索', 'Tool / 工具调用', 'Skill / 技能加载', 'Workflow / 工作流',
    ]);
    await user.selectOptions(target, 'skill');
    expect(target).toHaveValue('skill');
    expect(screen.getByRole('textbox', { name: '允许修改的范围' })).toHaveValue('Skill 选择、版本或加载配置；冻结 Prompt、模型和权限。');
    await user.click(screen.getByRole('radio', { name: /实现层/u }));
    await user.selectOptions(target, 'model');
    expect(screen.getByRole('radio', { name: /配置层/u })).toBeChecked();
    expect(screen.getByRole('radio', { name: /实现层/u })).toBeDisabled();
    expect(screen.getByRole('textbox', { name: '允许修改的范围' })).toHaveValue('只替换业务执行模型；固定推理强度、Judge、Prompt、Tool、Skill 和工作流。');
  });

  it('accepts a candidate directory on a connection without a native file picker', async () => {
    const transport = makeTransport();
    Object.defineProperty(transport, 'pickFiles', { value: undefined });
    const onStart = vi.fn().mockResolvedValue(undefined);
    render(<ControlTransportProvider transport={transport}><ExperimentWorkspace
      experiment={experiment} title="企业知识库问答" scene="Validation" datasetSummary="1 个任务" decision="保留候选"
      busy={false} selector={null} baseline={null} results={null} application={null} room={null} roomCount={0}
      onStart={onStart} onDiscuss={vi.fn()}
    /></ControlTransportProvider>);
    const user = userEvent.setup();
    await user.click(screen.getByRole('tab', { name: '设置实验' }));
    await user.type(screen.getByRole('textbox', { name: '候选目录路径' }), '/workspace/lab-candidates');
    await user.click(screen.getByRole('button', { name: '开始实验' }));
    expect(onStart).toHaveBeenCalledWith(expect.objectContaining({ workspaceRoot: '/workspace/lab-candidates' }));
    expect(screen.queryByText(/从桌面 App 打开 Lab/)).not.toBeInTheDocument();
  });
  it('opens the current Enterprise RAG experiment and keeps Keep separate from application', async () => {
    await showLab(makeTransport());
    expect(await screen.findByRole('combobox', { name: '当前实验' })).toHaveValue(experiment.experimentId);
    const steps = within(screen.getByRole('tablist', { name: '实验流程' }));
    expect(steps.getAllByRole('tab').map((tab) => tab.textContent)).toEqual(['1设置实验', '2运行', '3检查结果', '4应用版本']);
    expect(screen.queryByLabelText('Agent Lab 实验结果')).not.toBeInTheDocument();
    expect(screen.queryByRole('tablist', { name: 'Agent Lab 页面' })).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '检查结果' })).toHaveAttribute('aria-selected', 'true');
    await userEvent.setup().click(screen.getByRole('tab', { name: '应用版本' }));
    expect(await screen.findByLabelText('当前生效版本')).toHaveTextContent('gpt-5.6-sol');
    expect(screen.getByText('正在使用内置版本，尚无应用记录。')).toBeVisible();
    expect(screen.queryByRole('button', { name: '应用到场景' })).not.toBeInTheDocument();
    expect(screen.getByText('这个候选尚未登记为场景版本。')).toBeVisible();
    screen.getByRole('tab', { name: '应用版本' }).focus();
    await userEvent.setup().keyboard('{Home}');
    expect(screen.getByRole('tab', { name: '设置实验' })).toHaveFocus();
    expect(screen.getByRole('textbox', { name: '优化目标' })).toBeVisible();
  });

  it('dispatches edited implementation scope and limits to a real full-trust Room, then retains the experiment', async () => {
    const room = { id: 'room-experiment', title: '新实验', ownerAppId: 'extension:agent-lab', surfaceKey: `candidate.${experiment.experimentId}`.slice(0, 64), participants: [] };
    const transport = makeTransport({ 'agent.rooms.create': { ok: true, room }, 'agent.room.message': { ok: true } });
    await showLab(transport);
    await screen.findByRole('combobox', { name: '当前实验' });
    const user = userEvent.setup();
    await user.click(screen.getByRole('tab', { name: '设置实验' }));
    await user.click(screen.getByRole('radio', { name: /实现层/ }));
    await user.clear(screen.getByRole('textbox', { name: '允许修改的范围' }));
    await user.type(screen.getByRole('textbox', { name: '允许修改的范围' }), '只修改 rerank 的实现');
    await user.clear(screen.getByRole('spinbutton', { name: '最多候选数' }));
    await user.type(screen.getByRole('spinbutton', { name: '最多候选数' }), '2');
    await user.clear(screen.getByRole('spinbutton', { name: '预算上限（美元）' }));
    await user.type(screen.getByRole('spinbutton', { name: '预算上限（美元）' }), '2.5');
    await user.click(screen.getByRole('button', { name: '选择候选目录' }));
    await user.click(screen.getByRole('button', { name: '开始实验' }));
    expect(await screen.findByLabelText('运行中的原生 Room')).toHaveAttribute('data-room-id', 'room-experiment');
    const creation = transport.requests.find(({ request }) => request.pathId === 'agent.rooms.create')?.request.body as Record<string, unknown>;
    expect(creation).toMatchObject({
      ownerAppId: 'extension:agent-lab', workspaceRoots: ['/workspace/candidates'], dangerousModeConfirmation: 'ENABLE_FULL_TRUST',
      permissionPolicy: { room: { executionMode: 'full_trust' }, partner: { executionMode: 'inherit' }, toolAgent: { executionMode: 'inherit' } },
    });
    const contractLine = String(creation.scenarioPrompt).split('\n').find((line) => line.startsWith('agentLabDispatch='))!;
    expect(JSON.parse(contractLine.slice('agentLabDispatch='.length))).toMatchObject({
      experimentId: experiment.experimentId,
      scope: { layer: 'implementation', target: 'prompt', allowedChanges: '只修改 rerank 的实现' },
      baseline: { runId: 'rag-baseline' },
      budget: { maxCandidates: 2, maxEstimatedCostUsd: 2.5, enforcement: 'agent_observed' },
      workspace: { root: '/workspace/candidates', isolation: 'candidate_copy_required' },
    });
    expect(screen.getByRole('tab', { name: '运行' })).toHaveAttribute('aria-selected', 'true');
    await user.click(screen.getByRole('tab', { name: '检查结果' }));
    expect(transport.requests.filter(({ request }) => request.pathId === 'agent.room.abort')).toHaveLength(0);
    expect(screen.getByRole('combobox', { name: '当前实验' })).toHaveValue(experiment.experimentId);
  });

  it('requires an actual candidate directory before dispatch and describes unenforced budget truthfully', async () => {
    const transport = makeTransport();
    await showLab(transport);
    await screen.findByRole('combobox', { name: '当前实验' });
    const user = userEvent.setup();
    await user.click(screen.getByRole('tab', { name: '设置实验' }));
    expect(screen.getByText(/当前没有系统强制扣费上限/)).toBeVisible();
    await user.click(screen.getByRole('button', { name: '开始实验' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('请选择有效的候选工作目录');
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.rooms.create')).toBe(false);
  });

  it('shows real frozen diff and paired case scores without inventing a per-case Session position', async () => {
    const transport = makeTransport({ 'agent.eval-lab.evidence': catalog([evidenceRun('rag--rag-baseline', 'trace:baseline'), evidenceRun('rag--rag-candidate', 'trace:candidate')]) });
    const { openRoute } = await showLab(transport);
    const cases = await screen.findByLabelText('逐 Case 前后对比');
    expect(within(cases).getAllByRole('listitem')).toHaveLength(1);
    expect(cases).not.toHaveTextContent('整批问答 Session');
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '查看 case-01 前后证据' }));
    const before = screen.getByRole('region', { name: '原方案证据' });
    const after = screen.getByRole('region', { name: '候选方案证据' });
    expect(before).toHaveTextContent('60.00%');
    expect(after).toHaveTextContent('100.00%');
    expect(before).toHaveTextContent('本次对话处理整批任务');
    await user.click(within(before).getByRole('button', { name: '打开原方案所在批次报告' }));
    expect(transport.requests.some(({ request }) => request.pathId === 'agent.eval-lab.evidence' && request.query?.runId === 'rag--rag-baseline' && request.query?.taskIndex === '0')).toBe(true);
    await user.click(screen.getByText('查看实际改动'));
    expect(screen.getByLabelText('候选实际差异')).toHaveTextContent('"model": "sol"');
    expect(screen.getByLabelText('候选实际差异')).toHaveTextContent('"model": "luna"');
    await user.click(screen.getByRole('button', { name: /打开 Trace · trace:baseline/ }));
    expect(openRoute).toHaveBeenCalledWith('/observability?traceId=trace%3Abaseline');
  });

  it('keeps an incoming diagnosis reachable without binding it to a different experiment', async () => {
    const reportId = `trace-report:${'a'.repeat(32)}`;
    const transport = makeTransport({ 'observability.traceDiagnosticReport.get': {
      schemaVersion: 'rag-ime.trace-diagnostic-report.v1', reportId, revision: 1, status: 'completed', title: '另一轮任务诊断', diagnosticSessionId: 'session:diagnosis',
      targets: [{ targetKey: 'run:unrelated-run', kind: 'run', id: 'unrelated-run', title: 'Other run', traceIds: [], sourceAvailable: true }],
      traceIds: [], inspectionSha256: 'd'.repeat(64), inspection: {}, result: {}, failureReason: '', createdAtMs: 1, updatedAtMs: 1,
    } });
    const { openRoute } = await showLab(transport, `/eval-lab?traceReportId=${encodeURIComponent(reportId)}`, false);
    expect(await screen.findByText('另一轮任务诊断')).toBeVisible();
    expect(screen.getByText(/来源报告尚未与本实验基线关联/)).toBeVisible();
    expect(screen.getByRole('combobox', { name: '当前实验' })).toHaveValue(experiment.experimentId);
    await userEvent.setup().click(screen.getByRole('button', { name: '返回来源诊断' }));
    expect(openRoute).toHaveBeenCalledWith(`/trace-agent?reportId=${encodeURIComponent(reportId)}`);
  });

  it('uses only explicit evidence aliases to open a baseline Trace', () => {
    expect(evidenceTraceIds([evidenceRun('rag--rag-baseline.v1', 'trace:exact'), evidenceRun('rag-baseline-retry', 'trace:wrong')], experiment.baseline)).toEqual(['trace:exact']);
  });

  it('uses scored Case identity instead of a failed batch Session in diagnosis details', () => {
    const baseline = evidenceRun('rag--rag-baseline', 'trace:baseline');
    baseline.tasks = baseline.tasks.map((task) => ({ ...task, taskSucceeded: false }));
    const model = buildOptimizationWorkbenchModel({ experiment, linkedRuns: [], evidenceRuns: [baseline, evidenceRun('rag--rag-candidate', 'trace:candidate')] });
    expect(model.failure?.caseId).toBe('case-01');
    expect(model.failure?.terminal).toBe('未记录独立的单 Case 终态');
    expect(model.identity.baselineTraceIds).toEqual(['trace:baseline']);
  });
});
