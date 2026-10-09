import {
  ArrowUpRight,
  CheckCircle2,
  ClipboardList,
  Download,
  FlaskConical,
  GitBranch,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
  Scale,
  Users,
} from 'lucide-react';
import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { useInRouterContext, useLocation, useNavigate } from 'react-router-dom';
import { Button, EmptyState } from '@/components/primitives';
import { useControlTransport } from '@/app/control-transport';
import { roleItems } from '@/features/agent/types';
import type { AgentPersonaV1 } from '@/contracts/generated/agent-persona.v1';
import { openPawOsRoute, usePawOsAppActive, usePawOsDesktop } from '@/features/paw-os/surface-context';
import { PawRoomWorkspace } from '@/paw-os/apps/PawRoomWorkspace';
import type { RoomPermissionPolicy, RoomSummary } from '@/features/rooms/room-types';
import {
  useEvalLabEvidenceCatalog,
  useEvalLabEvidenceDetail,
  useEvalLabRuns,
  isEvalLabEvidenceUnavailable,
  type EvalLabComparisonResult,
  type EvalLabEvidenceRun,
  type EvalLabEvidenceDetail,
  type EvalLabEvidenceProjectionDetail,
  type EvalLabEvidenceResponse,
  type EvalLabEvidenceTask,
  type EvalLabFailureOwner,
  type EvalLabExperiment,
  type EvalLabPathSearch,
  type EvalLabRun,
  type EvalLabTask,
} from './api';
import { createEvalLabExperimentAuditDownload } from './experiment-audit-html';
import { LinkedOptimizationWorkbench } from './optimization/OptimizationWorkbench';
import { ExperimentWorkspace } from './ExperimentWorkspace';
import { GoldenWorkflow } from './golden/GoldenWorkflow';
import { LabProjectWorkbench } from './projects/LabProjectWorkbench';
import { ExperimentApplication } from './ExperimentApplication';
import { ExperimentResultSummary } from './ExperimentResultSummary';
import { buildExperimentDisplayMetrics } from './experiment-display-metrics';
import { PAW_METRIC_LABELS, pawSelfbootEfficiency, pawSelfbootQuality, pawSelfbootReliability } from './paw-selfboot-metrics';
import { PawSelfbootResultsChart } from './PawSelfbootResultsChart';
import { compileExperimentDispatch, type ExperimentSetup } from './execution-contract';
import { bindSceneRecipeState, getSceneRecipeState, sceneRecipeIdForExperiment } from './scene-recipes';
import { TraceLabContext } from './TraceLabContext';
import { CandidatePatchEvidence } from './CandidatePatchEvidence';
import { CandidateDispatchRecovery, useCandidateRoomDispatch } from './candidate-room-dispatch';
import { StoryDeck } from './StoryDeck';
import { canonicalEvidenceRunId, evidenceRunMatches, evidenceRunMatchesVersion, evidenceTraceIds, experimentBaselineTraceIds, experimentRunBindings, matchingEvidenceRuns } from './evidence-identity';
import './eval-lab.css';

/** The Room/Session owner used by the Agent Lab product surface. */
export const AGENT_LAB_OWNER_APP_ID = 'extension:agent-lab' as const;

const EVAL_LAB_READ_ONLY_PERMISSION_POLICY: RoomPermissionPolicy = {
  schemaVersion: 'rag-ime.room-permission-policy.v1',
  room: { executionMode: 'read_only' },
  partner: { executionMode: 'inherit' },
  toolAgent: { executionMode: 'inherit' },
};

const EVAL_LAB_CANDIDATE_PERMISSION_POLICY: RoomPermissionPolicy = {
  schemaVersion: 'rag-ime.room-permission-policy.v1',
  room: { executionMode: 'full_trust' },
  partner: { executionMode: 'inherit' },
  toolAgent: { executionMode: 'inherit' },
};

type EvalLabPage = 'workspace' | 'overview' | 'paths' | 'details' | 'sessions' | 'story' | 'golden';
type ExperimentRecordView = 'task' | 'dataset' | 'baseline' | 'optimization' | 'candidate';
type RoomAction = { runId: string; state: 'creating' | 'sending' | 'error'; message?: string };
const EVAL_LAB_PAGES = [
  ['workspace', '实验工作区'],
  ['overview', '实验结果'],
  ['paths', '方案路径'],
  ['details', '实验详情'],
  ['sessions', '对话与证据'],
  ['story', '实验记录'],
] as const satisfies readonly (readonly [EvalLabPage, string])[];

export function EvalLabFeature({ initialPage }: { initialPage?: EvalLabPage } = {}) {
  const routed = useInRouterContext();
  return routed ? <RoutedEvalLabFeature initialPage={initialPage} />
    : <EvalLabEntry initialPage={initialPage} search={window.location.hash.split('?')[1] ?? window.location.search} onProjectSelect={(id) => {
      if (window.location.hash.startsWith('#/eval-lab')) window.history.replaceState(null, '', `#${projectRoute(id)}`);
    }} />;
}

function RoutedEvalLabFeature({ initialPage }: { initialPage?: EvalLabPage }) {
  const location = useLocation(); const navigate = useNavigate();
  const active = usePawOsAppActive(); const activeRef = useRef(active); activeRef.current = active;
  const mounted = useRef(true); const deferredRoute = useRef<string | undefined>(undefined);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (active === false || !deferredRoute.current) return;
    const route = deferredRoute.current; deferredRoute.current = undefined;
    navigate(route, { replace: true }); reflectProjectRoute(route, active);
  }, [active, navigate]);
  return <EvalLabEntry initialPage={initialPage} search={location.search} onProjectSelect={(id) => {
    if (!mounted.current) return;
    const route = projectRoute(id);
    if (activeRef.current === false) { deferredRoute.current = route; return; }
    deferredRoute.current = undefined;
    navigate(route, { replace: true }); reflectProjectRoute(route, activeRef.current);
  }} />;
}

function projectRoute(id: string) { return id ? `/eval-lab?project=${encodeURIComponent(id)}` : '/eval-lab'; }
function reflectProjectRoute(route: string, active: boolean | null) {
  if (active === true || window.location.hash.startsWith('#/eval-lab')) window.history.replaceState(null, '', `${window.location.search}#${route}`);
}

function EvalLabEntry({ initialPage, search, onProjectSelect }: { initialPage?: EvalLabPage; search: string; onProjectSelect: (id: string) => void }) {
  const parameters = new URLSearchParams(search);
  const requestedGolden = parameters.get('view') === 'golden';
  const traceReportId = parameters.get('traceReportId')?.trim() ?? '';
  const projectId = parameters.get('project') ?? '';
  const [historyOpen, setHistoryOpen] = useState(Boolean(initialPage) || requestedGolden || Boolean(traceReportId));
  useEffect(() => { setHistoryOpen(Boolean(initialPage) || requestedGolden || Boolean(traceReportId)); }, [initialPage, requestedGolden, traceReportId, projectId]);
  return historyOpen ? <div className="lab-project-legacy"><Button className="lab-project-legacy__back" size="small" onClick={() => setHistoryOpen(false)}>返回 Lab 项目</Button><LegacyEvalLabFeature initialPage={initialPage ?? (requestedGolden ? 'golden' : 'workspace')} /></div>
    : <LabProjectWorkbench initialProjectId={projectId} onProjectSelect={onProjectSelect} onOpenHistory={() => setHistoryOpen(true)} />;
}

export function LegacyEvalLabFeature({ initialPage = 'workspace' }: { initialPage?: EvalLabPage } = {}) {
  const id = useId();
  const desktop = usePawOsDesktop();
  const transport = useControlTransport();
  const runs = useEvalLabRuns();
  const [page, setPage] = useState<EvalLabPage>(() => initialPage === 'workspace' && new URLSearchParams(window.location.hash.split('?')[1]).get('view') === 'golden' ? 'golden' : initialPage);
  const [goldenStartNew, setGoldenStartNew] = useState(false);
  const [recordsOpen, setRecordsOpen] = useState(initialPage !== 'workspace');
  const [selectedExperimentId, setSelectedExperimentId] = useState('');
  const [roomAction, setRoomAction] = useState<RoomAction>();
  const [ownedRooms, setOwnedRooms] = useState<RoomSummary[]>([]);
  const [activeRoomId, setActiveRoomId] = useState('');
  const [roomPersonas, setRoomPersonas] = useState<AgentPersonaV1[]>([]);
  const [roomCatalogError, setRoomCatalogError] = useState('');
  const candidateDispatch = useCandidateRoomDispatch();
  useEffect(() => {
    const pendingRooms = Object.values(candidateDispatch.pending).map((item) => item.room);
    if (!pendingRooms.length) return;
    setOwnedRooms((current) => mergeAgentLabRooms(pendingRooms, current));
    setActiveRoomId((current) => current || pendingRooms[0]!.id);
  }, [candidateDispatch.pending]);
  // The overview itself now reports whether each project is backed by a real
  // transcript, a report-only receipt, or a controlled fixture. Load the
  // bounded catalog up front so that status is not inferred from titles.
  const sourceEvidence = useEvalLabEvidenceCatalog(true);
  const runItems = runs.data?.items ?? [];
  const experiments = runs.data?.experiments ?? [];
  const evidenceRunCount = sourceEvidence.data?.runs.length ?? 0;
  const hasSessionRecords = runItems.length > 0 || evidenceRunCount > 0 || ownedRooms.length > 0;
  const hasRecords = hasSessionRecords || experiments.length > 0;
  // A first refetch clears Query's error before it settles. Retain only the
  // presentation so its recovery control stays mounted; Query owns pending.
  const [lastRunsError, setLastRunsError] = useState(runs.error);
  const [lastEvidenceError, setLastEvidenceError] = useState(sourceEvidence.error);
  useEffect(() => { if (!runs.isFetching) setLastRunsError(runs.error); }, [runs.error, runs.isFetching]);
  useEffect(() => { if (!sourceEvidence.isFetching) setLastEvidenceError(sourceEvidence.error); }, [sourceEvidence.error, sourceEvidence.isFetching]);
  const runsError = runs.error ?? (runs.isFetching ? lastRunsError : null);
  const evidenceError = sourceEvidence.error ?? (sourceEvidence.isFetching ? lastEvidenceError : null);
  const evidenceLoading = sourceEvidence.isLoading && !evidenceError;
  const directoryEmpty = !runs.isLoading && !runsError && Boolean(runs.data) && !hasRecords;

  function showGolden(startNew = false) {
    setGoldenStartNew(startNew);
    setPage('golden');
    if (window.location.hash.startsWith('#/eval-lab')) window.history.replaceState(null, '', '#/eval-lab?view=golden');
  }

  function closeGolden() {
    setPage('workspace');
    if (window.location.hash.startsWith('#/eval-lab')) window.history.replaceState(null, '', '#/eval-lab');
  }

  useEffect(() => {
    if (page !== 'sessions' && page !== 'details' && page !== 'workspace') return;
    let active = true;
    void Promise.all([
      transport.request({ pathId: 'agent.rooms.list', query: { ownerAppId: AGENT_LAB_OWNER_APP_ID, limit: 100 } }),
      transport.request({ pathId: 'agent.roles.list' }),
    ]).then(([roomResponse, roleResponse]) => {
      if (!active) return;
      const restored = agentLabRoomItems(roomResponse);
      const personas = roleItems(roleResponse);
      setRoomPersonas(personas);
      setOwnedRooms((current) => mergeAgentLabRooms(current, restored));
      setActiveRoomId((current) => current || restored[0]?.id || '');
      setRoomCatalogError('');
    }).catch(() => {
      if (active) setRoomCatalogError('Agent Lab Room 暂时无法恢复；已保留当前页面中的 Room。');
    });
    return () => { active = false; };
  }, [page, transport]);

  function showOwnedRoom(room: RoomSummary, personas: AgentPersonaV1[], targetPage: EvalLabPage = 'sessions'): void {
    setRoomPersonas(personas);
    setOwnedRooms((current) => mergeAgentLabRooms([room], current));
    setActiveRoomId(room.id);
    setPage(targetPage);
  }

  async function findResumableAgentLabRoom(value: unknown): Promise<RoomSummary | undefined> {
    for (const summary of agentLabRoomItems(value)) {
      let room = summary;
      try {
        const detailResponse = await transport.request<Record<string, unknown>>({
          pathId: 'agent.room.get',
          params: { roomId: summary.id },
        });
        const detail = record(detailResponse).room;
        if (isAgentLabRoom(detail)) room = detail;
      } catch {
        // Older transports may expose only the list projection. Reuse that
        // summary unless it already carries a terminal failure.
      }
      if (canResumeAgentLabRoom(room)) return room;
    }
    return undefined;
  }

  async function createOptimizationRoom(experiment: EvalLabExperiment, run?: EvalLabRun, actionKey = experiment.experimentId, targetPage: EvalLabPage = 'sessions'): Promise<void> {
    if (roomAction?.state === 'creating' || roomAction?.state === 'sending') return;
    if (targetPage === 'workspace') setSelectedExperimentId(experiment.experimentId);
    setRoomAction({ runId: actionKey, state: 'creating' });
    try {
      const rolesResponse = await transport.request({ pathId: 'agent.roles.list' });
      const personas = roleItems(rolesResponse);
      const participants = buildRoomParticipants(experiment, personas);
      const brief = buildEvalLabRoomBrief(experiment, run);
      const message = publicRoomMessage(brief);
      const surfaceKey = `experiment.${experiment.experimentId}`.slice(0, 64);
      try {
        const existingResponse = await transport.request({
          pathId: 'agent.rooms.list',
          query: { ownerAppId: AGENT_LAB_OWNER_APP_ID, surfaceKey, limit: 1 },
        });
        const existing = await findResumableAgentLabRoom(existingResponse);
        if (existing) {
          showOwnedRoom(existing, personas, targetPage);
          setRoomAction(undefined);
          return;
        }
      } catch {
        // A catalog read is an optimization. Room creation remains available
        // on older local transports that do not yet expose owner filtering.
      }
      const roomResponse = await transport.request<Record<string, unknown>>({
        pathId: 'agent.rooms.create',
        body: {
          title: `Agent Lab · ${experiment.title}`,
          // Evaluation discussion is a normal coordinator-led Room, not a
          // free-form roleplay. Collaboration routing deterministically assigns
          // an unaddressed intake to the coordinator (Sol), so the first user
          // message always gets a visible planet/Session lane.
          roomKind: 'collaboration',
          avatar: 'briefcase',
          description: `围绕 ${experiment.title} 的证据驱动优化讨论`,
          scenarioPrompt: buildEvalLabSkillContext(brief, experiment),
          participants,
          routingPolicy: 'natural',
          routingConfig: { maxResponders: 1, naturalJitter: 0, fallbackParticipantId: '' },
          workspaceRoots: [],
          permissionPolicy: EVAL_LAB_READ_ONLY_PERMISSION_POLICY,
          ownerAppId: AGENT_LAB_OWNER_APP_ID,
          surfaceKey,
        },
      });
      const room = record(roomResponse).room;
      if (!isAgentLabRoom(room)) {
        throw new Error('服务端没有返回可验证的 Room。');
      }
      showOwnedRoom(room, personas, targetPage);
      setRoomAction({ runId: actionKey, state: 'sending' });
      await transport.request({
        pathId: 'agent.room.message',
        params: { roomId: room.id },
        body: {
          message,
          clientMessageId: `eval-lab:${experiment.experimentId}`,
        },
      });
      setRoomAction(undefined);
    } catch (error) {
      setRoomAction({ runId: actionKey, state: 'error', message: error instanceof Error ? error.message : 'Room 暂时无法创建，请稍后重试。' });
    }
  }

  async function createCandidateRoom(experiment: EvalLabExperiment, setup: ExperimentSetup): Promise<void> {
    if (roomAction?.state === 'creating' || roomAction?.state === 'sending') return;
    const existing = candidateDispatch.get(experiment.experimentId);
    if (existing) { showOwnedRoom(existing.room, roomPersonas, 'workspace'); return; }
    let compiled = compileExperimentDispatch(experiment, setup, scenarioTaskContract(experiment));
    if (!compiled.ok) {
      setRoomAction({ runId: experiment.experimentId, state: 'error', message: compiled.errors.join(' ') });
      return;
    }
    setSelectedExperimentId(experiment.experimentId);
    setRoomAction({ runId: experiment.experimentId, state: 'creating' });
    try {
      const sceneId = sceneRecipeIdForExperiment(experiment.experimentId);
      if (sceneId) {
        const state = await getSceneRecipeState(transport, sceneId, experiment.experimentId);
        compiled = compileExperimentDispatch(experiment, setup, scenarioTaskContract(experiment), bindSceneRecipeState(state));
        if (!compiled.ok) throw new Error(compiled.errors.join(' '));
      }
      const rolesResponse = await transport.request({ pathId: 'agent.roles.list' });
      const personas = roleItems(rolesResponse);
      const participants = buildRoomParticipants(experiment, personas);
      const roomResponse = await transport.request<Record<string, unknown>>({
        pathId: 'agent.rooms.create',
        body: {
          title: `Agent Lab · 新候选 · ${experiment.title}`,
          roomKind: 'collaboration',
          avatar: 'briefcase',
          description: '按已设定目标、范围和预算执行的新候选实验',
          scenarioPrompt: compiled.scenarioPrompt,
          participants,
          routingPolicy: 'natural',
          routingConfig: { maxResponders: 1, naturalJitter: 0, fallbackParticipantId: '' },
          workspaceRoots: [compiled.contract.workspace.root],
          permissionPolicy: EVAL_LAB_CANDIDATE_PERMISSION_POLICY,
          dangerousModeConfirmation: 'ENABLE_FULL_TRUST',
          ownerAppId: AGENT_LAB_OWNER_APP_ID,
          surfaceKey: `candidate.${experiment.experimentId}`.slice(0, 64),
        },
      });
      const room = record(roomResponse).room;
      if (!isAgentLabRoom(room)) throw new Error('服务端没有返回可验证的候选 Room。');
      showOwnedRoom(room, personas, 'workspace');
      setRoomAction({ runId: experiment.experimentId, state: 'sending' });
      await candidateDispatch.begin(experiment.experimentId, room, compiled.message);
      setRoomAction(undefined);
    } catch (error) {
      setRoomAction({ runId: experiment.experimentId, state: 'error', message: error instanceof Error ? error.message : '候选 Room 暂时无法创建。' });
    }
  }

  if (page === 'golden') return <main className="eval-lab eval-lab--golden"><GoldenWorkflow onClose={closeGolden} startNew={goldenStartNew} /></main>;

  return (
    <main aria-labelledby="eval-lab-title" className={`eval-lab${page === 'workspace' ? ' eval-lab--workspace' : ''}`}>
      <header className="eval-lab__header">
        <div>
          <h1 id="eval-lab-title"><FlaskConical aria-hidden="true" size={23} /> Agent 工作流实验室</h1>
          <p className="eval-lab__lede">沿真实任务、执行记录与验收结果，比较 Agent 方案。</p>
          <Button aria-expanded={recordsOpen} leadingIcon={<ClipboardList size={15} />} onClick={() => setRecordsOpen((open) => !open)} variant="secondary">浏览实验记录</Button>
        </div>
        <div className="eval-lab__header-actions">
          {transport.kind === 'mock' ? <span className="eval-lab__source-badge eval-lab__source-badge--mock">示例数据</span> : null}
          <Button onClick={() => showGolden()} variant="secondary">继续评测集</Button>
          <Button leadingIcon={<ClipboardList size={15} />} onClick={() => showGolden(true)} variant="primary">新建评测</Button>
          <Button leadingIcon={<RefreshCw size={15} />} loading={runs.isFetching || sourceEvidence.isFetching} onClick={() => { void runs.refetch(); void sourceEvidence.refetch(); }} preserveFocusWhileLoading variant="secondary">
            刷新
          </Button>
        </div>
      </header>
      {page === 'workspace' ? <div className="eval-lab__start-guide"><div><strong>资料问答，从「新建评测」开始</strong><p>提供资料 → 审核题目 → 校准评审 → 对比结果。每一步都会提示完成条件。</p></div><span>诊断、检索等场景从下方已有实验进入，使用各自的数据、工具和评分规则；运行页显示本机是否已连接执行环境。</span></div> : null}

      <TraceLabContext experiment={page === 'workspace' ? activeWorkspaceExperiment(runs.data?.experiments ?? [], selectedExperimentId) : activeProjectExperiment(runs.data?.experiments ?? [], selectedExperimentId)} />

      {runs.isLoading && !runsError ? <div aria-live="polite" className="eval-lab__state" role="status">正在读取评测回执…</div> : null}
      {runsError ? (
        <div aria-live="assertive" className="eval-lab__error" role="alert">
          <strong>{runs.data ? '更新暂时失败，仍显示上次读取的实验' : '还没读取到评测结果'}</strong>
          <span>{runs.data ? '当前页面和未提交设置已保留。重新连接后，读取最新回执。' : '请确认本机服务正在运行，再重新读取。已经保存的实验不会丢失。'}</span>
          <Button size="small" loading={runs.isFetching} onClick={() => void runs.refetch()} preserveFocusWhileLoading>重新读取实验</Button>
          <details><summary>查看技术信息</summary><code>{publicErrorText(runsError)}</code></details>
        </div>
      ) : null}
      {!runs.isLoading && runs.data?.truncation ? (
        <p className="eval-lab__lede" role="status">
          {`历史目录显示 ${runs.data.items.length} / ${runs.data.total} 次运行。`}
          {runs.data.truncation.omittedRunCount ? `另有 ${runs.data.truncation.omittedRunCount} 次运行未在本页列出。` : ''}
          {runs.data.truncation.omittedTaskCount ? `已显示运行内另有 ${runs.data.truncation.omittedTaskCount} 条任务明细未列出。` : ''}
          运行汇总仍按全部可读评测任务计算。
        </p>
      ) : null}
      {page !== 'sessions' && directoryEmpty ? (
        <EmptyState icon={ClipboardList} title="还没有实验" description="点击“新建评测”，从真实资料起草题目，审核标准并校准评审后，开始对照实验。" />
      ) : null}
      {recordsOpen || hasRecords ? (
        <>
          {recordsOpen ? <nav aria-label="Agent Lab 页面" className="eval-lab__pages" role="tablist">
            {EVAL_LAB_PAGES.map(([key, label], index) => (
              <button
                aria-controls={`${id}-records-panel`}
                aria-selected={page === key}
                id={`${id}-${key}-tab`}
                key={key}
                onClick={() => setPage(key)}
                onKeyDown={(event) => moveTabbedSelection(event, index, EVAL_LAB_PAGES.map(([value]) => value), setPage)}
                role="tab"
                tabIndex={page === key ? 0 : -1}
                type="button"
              >{label}</button>
            ))}
          </nav> : null}
          <section aria-label={recordsOpen ? undefined : '评测批次列表'} aria-labelledby={recordsOpen ? `${id}-${page}-tab` : undefined} className="eval-lab__runs" id={`${id}-records-panel`} role={recordsOpen ? 'tabpanel' : undefined}>
            {page === 'workspace' && experiments.length ? (() => {
              const experiment = activeWorkspaceExperiment(experiments, selectedExperimentId);
              if (!experiment) return <EmptyState icon={FlaskConical} title="还没有可运行的垂直实验" description="新建评测后，固定任务与基线即可进入实验工作区。" />;
              const evidenceRuns = matchingEvidenceRuns(sourceEvidence.data, experiment);
              const rooms = matchingExperimentRooms(ownedRooms, experiment);
              const currentAction = roomAction?.runId === experiment.experimentId ? roomAction : undefined;
              const busy = currentAction?.state === 'creating' || currentAction?.state === 'sending';
              const pendingDispatch = candidateDispatch.pending[experiment.experimentId];
              return <ExperimentWorkspace
                application={<ExperimentApplication experiment={experiment} />}
                baseline={<ExperimentTraceLinks desktop={desktop} evidenceRuns={evidenceRuns} experiment={experiment} kind="baseline" />}
                busy={busy}
                dispatchPending={Boolean(pendingDispatch)}
                recovery={pendingDispatch ? <CandidateDispatchRecovery pending={pendingDispatch} onRefresh={() => void candidateDispatch.refresh(experiment.experimentId)} onRetry={() => void candidateDispatch.retry(experiment.experimentId)} /> : undefined}
                datasetSummary={publicDatasetSummary(experiment)}
                decision={statusLabel(experiment.status)}
                error={currentAction?.state === 'error' ? currentAction.message : undefined}
                experiment={experiment}
                trialSceneId={experiment.projectionState !== 'history' ? projectKeyForExperiment(experiment) ?? undefined : undefined}
                key={experiment.experimentId}
                onDiscuss={() => { if (pendingDispatch) showOwnedRoom(pendingDispatch.room, roomPersonas, 'workspace'); else void createOptimizationRoom(experiment, undefined, experiment.experimentId, 'workspace'); }}
                onStart={(setup) => createCandidateRoom(experiment, setup)}
                onCreateEvaluation={() => showGolden(true)}
                results={<ExperimentWorkspaceResults desktop={desktop} evidenceCatalog={sourceEvidence.data} evidenceError={Boolean(evidenceError)} evidenceLoading={evidenceLoading} experiment={experiment} linkedRuns={matchingRuns(experiment, runItems)} />}
                room={<AgentLabRoomDeck activeRoomId={activeRoomId} error={roomCatalogError} onRoomUpdated={(updated) => { setOwnedRooms((current) => mergeAgentLabRooms([updated], current)); void runs.refetch(); void sourceEvidence.refetch(); }} onSelect={setActiveRoomId} personas={roomPersonas} rooms={rooms} />}
                roomCount={rooms.length}
                scene={candidateLabel(experiment)}
                selector={<ExperimentWorkspacePicker experiments={experiments} onSelect={setSelectedExperimentId} selectedId={experiment.experimentId} />}
                title={projectTitleForExperiment(experiment)}
              />;
            })() : null}
            {page === 'workspace' && !experiments.length && !runs.isLoading && !directoryEmpty ? <EmptyState icon={FlaskConical} title="还没有实验配置" description="已有运行可在对话与证据中核对。点击新建评测，定义任务与基线。" /> : null}
            {page === 'overview' && experiments.length ? (
              <ExperimentMatrix evidenceCatalog={sourceEvidence.data} evidenceLoading={evidenceLoading} experiments={experiments} onOpenExperiment={(experimentId) => { setSelectedExperimentId(experimentId); setPage('details'); }} />
            ) : null}
            {(page === 'overview' || page === 'details') && !experiments.length && !runs.isLoading && !directoryEmpty ? <EmptyState icon={FlaskConical} title="还没有已关联的实验结果" description="已有运行仍可在对话与证据中查看；建立评测集后，才能保存有明确基线的实验比较。" action={<Button onClick={() => setPage('sessions')}>查看已有运行</Button>} /> : null}
            {page === 'paths' && runs.data?.pathSearches?.length ? <OptimalPathPanel searches={runs.data.pathSearches} /> : null}
            {page === 'paths' && !runs.data?.pathSearches?.length && !runs.isLoading && !directoryEmpty ? <EmptyState icon={GitBranch} title="还没有可比较的方案路径" description="先新建评测并运行至少一个新方案，这里会显示每一步为何保留或淘汰。" /> : null}
            {page === 'details' && experiments.length ? (
              <section aria-label="Agent Lab 实验" className="eval-lab__experiments">
                {evidenceLoading ? <p className="eval-lab__evidence-loading" role="status">正在读取可回溯的原始运行…</p> : null}
                {evidenceError ? <p className="eval-lab__evidence-error" role="alert">原始运行证据暂时不可读：{publicErrorText(evidenceError)}</p> : null}
                <ProjectExperimentPicker experiments={experiments} selectedId={selectedExperimentId} onSelect={setSelectedExperimentId} />
                {activeProjectExperiment(experiments, selectedExperimentId) ? (() => {
                  const experiment = activeProjectExperiment(experiments, selectedExperimentId)!;
                  return <ExperimentSection
                    desktop={desktop}
                    evidenceCatalog={sourceEvidence.data}
                    experiment={experiment}
                    key={experiment.experimentId}
                    linkedRuns={matchingRuns(experiment, runs.data?.items ?? [])}
                    onOpenRoom={(roomId) => { setActiveRoomId(roomId); setPage('sessions'); }}
                    rooms={matchingExperimentRooms(ownedRooms, experiment)}
                  />;
                })() : null}
              </section>
            ) : null}
            {page === 'story' && experiments.length ? <StoryDeck experiments={experiments} onSelect={setSelectedExperimentId} selectedId={selectedExperimentId} /> : null}
            {page === 'sessions' ? (
              <section aria-label="真实 Session runs" className="eval-lab__session-runs">
                <AgentLabRoomDeck
                  activeRoomId={activeRoomId}
                  error={roomCatalogError}
                  onRoomUpdated={(updated) => {
                    setOwnedRooms((current) => mergeAgentLabRooms([updated], current));
                    void runs.refetch();
                    void sourceEvidence.refetch();
                  }}
                  onSelect={setActiveRoomId}
                  personas={roomPersonas}
                  rooms={ownedRooms}
                />
                <header className="eval-lab__session-intro">
                  <div>
                    <p className="eval-lab__eyebrow"><ClipboardList aria-hidden="true" size={15} /> 对话与证据</p>
                    <h2>查看每轮对话、工具调用和验收报告</h2>
                    <p>打开任意任务，可以核对 Agent 收到什么、做了哪些操作、最终输出什么，以及自动验收为何通过或失败。原始记录只读；内部推理、隐藏标准答案和凭据不会显示。</p>
                  </div>
                  <span>{runItems.length} 个回执{sourceEvidence.data?.source.runCount ? ` · ${sourceEvidence.data.source.runCount} 个历史批次` : ''}</span>
                </header>
                {evidenceLoading ? <p className="eval-lab__evidence-loading" role="status">正在读取研究盘里的历史运行…</p> : null}
                {evidenceError ? <p className="eval-lab__evidence-error" role="alert">历史运行目录暂时不可读：{publicErrorText(evidenceError)} 可使用上方“刷新”重新读取。</p> : null}
                {sourceEvidence.data && evidenceRunCount > 0 ? <SourceEvidenceCatalog catalog={sourceEvidence.data} /> : null}
                {runItems.map((run) => <EvalRunSection desktop={desktop} key={run.runId} run={run} roomAction={roomAction} onCreateRoom={() => void createOptimizationRoom(fallbackExperiment(run), run, run.runId)} />)}
                {!runs.isLoading && !runsError && !sourceEvidence.isLoading && !evidenceError && !hasSessionRecords ? <EmptyState icon={ClipboardList} title="还没有可查看的运行记录" description="点击“新建评测”，完成一次实验后，这里会按任务展示只读对话、工具调用和验收结果。" /> : null}
              </section>
            ) : null}
          </section>
        </>
      ) : null}
    </main>
  );
}

function ExperimentWorkspacePicker({ experiments, selectedId, onSelect }: {
  experiments: readonly EvalLabExperiment[];
  selectedId: string;
  onSelect: (id: string) => void;
}) {
  const groups = groupProjectExperiments(experiments.filter((experiment) => !isHistoricalExperiment(experiment)))
    .sort((left, right) => Number(right.key === 'enterprise-rag') - Number(left.key === 'enterprise-rag'));
  return <div className="lab-workspace__selector"><label htmlFor="lab-experiment-selector">当前实验</label><select id="lab-experiment-selector" onChange={(event) => onSelect(event.target.value)} value={selectedId}>
    {groups.map((group) => <optgroup key={group.key} label={group.title}>{group.experiments.map((experiment) => <option key={experiment.experimentId} value={experiment.experimentId}>{projectTitleForExperiment(experiment)}{group.experiments.length > 1 ? ` · ${candidateLabel(experiment)}` : ''}</option>)}</optgroup>)}
    {experiments.find((experiment) => experiment.experimentId === selectedId && isHistoricalExperiment(experiment)) ? <option value={selectedId}>历史实验 · {candidateLabel(experiments.find((experiment) => experiment.experimentId === selectedId)!)}</option> : null}
  </select></div>;
}

function activeWorkspaceExperiment(experiments: readonly EvalLabExperiment[], selectedId: string): EvalLabExperiment | undefined {
  const selected = experiments.find((experiment) => experiment.experimentId === selectedId);
  if (selected) return selected;
  const current = experiments.filter((experiment) => !isHistoricalExperiment(experiment));
  return current.find((experiment) => experiment.experimentId === 'enterprise-rag.luna-prompt-v4-standard-r6.v1')
    ?? current.filter((experiment) => projectKeyForExperiment(experiment) === 'enterprise-rag').sort(compareProjectExperimentPriority)[0]
    ?? activeProjectExperiment(experiments, '');
}

function ExperimentTraceLinks({ desktop, evidenceRuns, experiment, kind }: {
  desktop: ReturnType<typeof usePawOsDesktop>;
  evidenceRuns: readonly EvalLabEvidenceRun[];
  experiment: EvalLabExperiment;
  kind: 'baseline' | 'candidate';
}) {
  const version = experiment[kind];
  const frozenTrace = kind === 'baseline' ? experiment.optimizationEvidence?.baselineTrace : undefined;
  const traces = kind === 'baseline' ? experimentBaselineTraceIds(evidenceRuns, experiment) : evidenceTraceIds(evidenceRuns, version);
  return <section aria-label={kind === 'baseline' ? '原运行 Trace' : '候选运行 Trace'} className="lab-workspace__trace-links">
    <h4>{kind === 'baseline' ? '原运行' : '候选运行'}</h4>
    <code>{version.runId}</code>
    {traces.length ? traces.map((traceId) => <button className="lab-workspace__text-action" key={traceId} onClick={() => openPawOsRoute(desktop, `/observability?traceId=${encodeURIComponent(traceId)}`)} type="button">打开 Trace · {traceId} <ArrowUpRight aria-hidden="true" size={14} /></button>) : <p>{frozenTrace?.reason || '未绑定可打开的 Trace；报告与对话证据仍可在下方核对。'}</p>}
  </section>;
}

function ExperimentWorkspaceResults({ desktop, evidenceCatalog, evidenceLoading, evidenceError, experiment, linkedRuns }: {
  desktop: ReturnType<typeof usePawOsDesktop>;
  evidenceCatalog?: EvalLabEvidenceResponse;
  evidenceLoading: boolean;
  evidenceError: boolean;
  experiment: EvalLabExperiment;
  linkedRuns: readonly EvalLabRun[];
}) {
  const evidenceRuns = matchingEvidenceRuns(evidenceCatalog, experiment);
  const boundary = experiment.vertical === 'paw-selfboot' ? experiment.claim.forbidden : experiment.optimizationEvidence?.validationBoundary.candidateAware && !experiment.optimizationEvidence.validationBoundary.candidateBlind
    ? '本轮数据已经参与候选调优，结果仅适用于这批验证题；尚不能据此证明未见任务上的效果。'
    : `${experiment.dataset.caseCount} 个固定 Case。${boundaryText(experiment.dataset.split)}`;
  const resultSummary = (
    <ExperimentResultSummary
      experiment={experiment}
      displayMetrics={buildExperimentDisplayMetrics(experiment)}
      qualitySummary={matrixQuality(experiment)}
      reliabilitySummary={matrixReliability(experiment)}
      efficiencySummary={matrixEfficiency(experiment)}
      boundary={boundary}
    />
  );
  return <div className="lab-workspace__results">
    {experiment.vertical === 'paw-selfboot' ? <>
      <p className="paw-results-intro">{publicDecisionSummary(experiment)}</p>
      <PawSelfbootResultsChart experiment={experiment} />
      <details className="lab-workspace__details"><summary>查看完整指标口径</summary>{resultSummary}</details>
    </> : resultSummary}
    <CandidatePatchEvidence experiment={experiment} />
    {evidenceLoading ? <p className="lab-workspace__notice" role="status">正在读取本实验的前后证据…</p> : evidenceError ? <p className="lab-workspace__error" role="alert">前后运行证据暂时无法读取。已有结论保留，点击上方刷新可重试。</p> : null}
    <ExperimentPairedCases evidenceRuns={evidenceRuns} experiment={experiment} />
    <details className="lab-workspace__details"><summary>原始评估与适用范围</summary><p>{humanClaimText(experiment.comparison.decisionReason)}</p><p>{humanClaimText(experiment.claim.allowed)}</p><p>{humanClaimText(experiment.claim.forbidden)}</p></details>
    <details className="lab-workspace__details"><summary>查看原运行与候选记录</summary><div className="lab-workspace__evidence-summary">
      <ExperimentTraceLinks desktop={desktop} evidenceRuns={evidenceRuns} experiment={experiment} kind="baseline" />
      <ExperimentTraceLinks desktop={desktop} evidenceRuns={evidenceRuns} experiment={experiment} kind="candidate" />
    </div></details>
    <details className="lab-workspace__details"><summary>查看任务、数据与完整运行档案</summary><ExperimentRecordBrowser evidenceCatalog={evidenceCatalog} experiment={experiment} linkedRuns={linkedRuns} /><ExperimentEvidenceLinks catalog={evidenceCatalog} experiment={experiment} /></details>
  </div>;
}

type CaseEvidenceBinding = { run: EvalLabEvidenceRun; task: EvalLabEvidenceTask };

function ExperimentPairedCases({ evidenceRuns, experiment }: {
  evidenceRuns: readonly EvalLabEvidenceRun[];
  experiment: EvalLabExperiment;
}) {
  const [selectedCase, setSelectedCase] = useState('');
  const before = new Map<string, CaseEvidenceBinding>();
  const after = new Map<string, CaseEvidenceBinding>();
  for (const run of [...evidenceRuns].sort((left, right) => right.updatedAtMs - left.updatedAtMs)) {
    for (const [kind, target] of [['baseline', before], ['candidate', after]] as const) {
      if (!evidenceRunMatchesVersion(run, experiment[kind])) continue;
      for (const task of run.tasks) if (!target.has(evidenceTaskKey(task))) target.set(evidenceTaskKey(task), { run, task });
    }
  }
  const outputs = new Map((experiment.comparison.outputComparisons ?? []).map((item) => [item.caseId, item]));
  const scored = new Map((experiment.optimizationEvidence?.caseComparisons ?? []).map((item) => [item.caseId, item]));
  // A RAG Session can answer the complete batch. Its lane/task label is not a
  // Case identity. A typed case projection takes precedence over Session rows.
  const batchEvidence = projectKeyForExperiment(experiment) === 'enterprise-rag' || experiment.evaluationKind === 'answer_evidence';
  const cases = scored.size ? [...scored.keys()] : batchEvidence ? [] : [...new Set([...before.keys(), ...after.keys(), ...outputs.keys()])].filter((id) => id !== 'aggregate');
  const baselineRun = evidenceRuns.find((run) => run.reportAvailable && evidenceRunMatchesVersion(run, experiment.baseline));
  const candidateRun = evidenceRuns.find((run) => run.reportAvailable && evidenceRunMatchesVersion(run, experiment.candidate));
  return <section aria-label="逐 Case 前后对比" className="lab-workspace__cases">
    <header><h3>逐 Case 前后对比</h3><p>{cases.length} 条公开记录 · {experiment.vertical === 'paw-selfboot' ? publicDatasetSummary(experiment) : <>固定分母 {experiment.dataset.caseCount} 个 Case</>}</p></header>
    {cases.length ? <div aria-hidden="true" className="lab-workspace__case-columns"><span>案例</span><span>原方案</span><span>候选方案</span><span>证据</span></div> : null}
    {cases.length ? <ol>{cases.map((caseId) => {
      const baseline = before.get(caseId);
      const candidate = after.get(caseId);
      const output = outputs.get(caseId);
      const expanded = selectedCase === caseId;
      return <li className="lab-workspace__case" key={caseId}>
        <header><strong>{caseId}</strong>
          <CaseResultSummary label="原方案" binding={baseline} scored={scored.get(caseId)?.before} />
          <CaseResultSummary label="候选方案" binding={candidate} scored={scored.get(caseId)?.after} />
          <button aria-expanded={expanded} aria-label={`${expanded ? '收起' : '查看'} ${caseId} 前后证据`} className="lab-workspace__text-action" onClick={() => setSelectedCase(expanded ? '' : caseId)} type="button">{expanded ? '收起证据' : '前后证据'} <ArrowUpRight aria-hidden="true" size={14} /></button>
        </header>
        {expanded ? <div className="lab-workspace__case-output">
          <CaseEvidenceSide binding={baseline} label="原方案证据" output={output?.before} scored={scored.get(caseId)?.before} run={baselineRun} />
          <CaseEvidenceSide binding={candidate} label="候选方案证据" output={output?.after} scored={scored.get(caseId)?.after} run={candidateRun} />
        </div> : null}
      </li>;
    })}</ol> : <p className="lab-workspace__notice">还没有精确绑定的逐 Case 证据；汇总分数不能代替每道题的前后结果。</p>}
  </section>;
}

type ScoredCase = NonNullable<EvalLabExperiment['optimizationEvidence']>['caseComparisons'][number]['before'];

function CaseResultSummary({ label, binding, scored }: { label: string; binding?: CaseEvidenceBinding; scored?: ScoredCase }) {
  return <div><span className="lab-workspace__case-side">{label}</span><p>{scored ? scored.status === 'passed' ? '通过' : '未通过' : binding ? evidenceTaskResult(binding.task) : '未绑定运行记录'}</p>{binding ? <small>{typeof binding.task.verifierTotal === 'number' ? `验收 ${binding.task.verifierPassed ?? '未知'}/${binding.task.verifierTotal}` : '验收明细未记录'} · {binding.task.transcriptAvailable ? '可查看对话' : evidenceTaskIsReportOnly(binding.run, binding.task) ? '仅报告证据' : '对话缺失'}</small> : null}</div>;
}

function CaseEvidenceSide({ binding, label, output, scored, run }: { binding?: CaseEvidenceBinding; label: string; output?: string; scored?: ScoredCase; run?: EvalLabEvidenceRun }) {
  const [showReport, setShowReport] = useState(false);
  return <section aria-label={label}><h4>{label}</h4>{output ? <p>{output}</p> : null}{scored ? <MetricList metrics={scored.metrics} /> : null}{binding
    ? <EvidencePanel runId={binding.run.runId} taskIndex={evidenceTaskIndex(binding.run, binding.task)} fallbackTask={binding.task} initialTab={evidenceTaskIsReportOnly(binding.run, binding.task) ? 'report' : 'task'} />
    : run ? <><p>本次对话处理整批任务，未记录独立的单 Case 对话位置。</p><button className="lab-workspace__text-action" onClick={() => setShowReport((open) => !open)} type="button">{showReport ? '收起本批报告' : `打开${label.replace('证据', '')}所在批次报告`}</button>{showReport ? <EvidencePanel runId={run.runId} taskIndex={0} initialTab="report" /> : null}</>
      : <p>没有这侧的精确运行记录，无法打开原始输入、输出和验收过程。</p>}</section>;
}

function AgentLabRoomDeck({ activeRoomId, error, onRoomUpdated, onSelect, personas, rooms }: {
  activeRoomId: string;
  error: string;
  onRoomUpdated: (room: RoomSummary) => void;
  onSelect: (roomId: string) => void;
  personas: AgentPersonaV1[];
  rooms: RoomSummary[];
}) {
  const activeRoom = rooms.find((room) => room.id === activeRoomId) ?? rooms[0];
  if (!activeRoom) return error ? <p className="eval-lab__room-error" role="alert">{error}</p> : null;
  return (
    <section aria-label="Agent Lab Room 对话" className="eval-lab__room-deck">
      <header>
        <div>
          <h2>{activeRoom.permissionPolicy?.room.executionMode === 'read_only' || activeRoom.executionMode === 'read_only' ? '证据讨论' : '本实验的 Agent 对话'}</h2>
          <p>查看真实进展，补充要求或停止运行。消息与运行状态来自同一个 Room。</p>
        </div>
        {rooms.length > 1 ? <nav aria-label="切换 Agent Lab Room">{rooms.map((room) => (
          <button aria-pressed={room.id === activeRoom.id} key={room.id} onClick={() => onSelect(room.id)} type="button">{room.title}</button>
        ))}</nav> : null}
      </header>
      {error ? <p className="eval-lab__room-error" role="alert">{error}</p> : null}
      <div className="eval-lab__room-workspace">
        <PawRoomWorkspace
          key={activeRoom.id}
          participantProcessLocation="room-transcript"
          personas={personas}
          record={activeRoom}
          recordId={activeRoom.id}
          onRoomUpdated={onRoomUpdated}
        />
      </div>
    </section>
  );
}

function ExperimentMatrix({ evidenceCatalog, evidenceLoading, experiments, onOpenExperiment }: {
  evidenceCatalog?: EvalLabEvidenceResponse;
  evidenceLoading: boolean;
  experiments: readonly EvalLabExperiment[];
  onOpenExperiment?: (experimentId: string) => void;
}) {
  const projectExperiments = experiments.filter((experiment) => projectKeyForExperiment(experiment) !== null);
  const currentExperiments = projectExperiments.filter((experiment) => !isHistoricalExperiment(experiment));
  const historicalExperiments = projectExperiments.filter(isHistoricalExperiment);
  const projects = groupProjectExperiments(currentExperiments);
  return (
    <section aria-label="Agent Lab 实验结果" className="eval-lab__matrix">
      <header className="eval-lab__matrix-header">
        <div>
          <p className="eval-lab__eyebrow"><ClipboardList aria-hidden="true" size={15} /> 实验结果</p>
          <h2>每一轮都回答：为什么改、改了什么、结果如何</h2>
          <p>下面按业务场景整理所有实验。先看任务是否做对、结果是否安全可靠，再比较成本；耗时只用于诊断，不阻止保留正确方案。</p>
          <p className="eval-lab__matrix-guide"><span><i className="eval-lab__legend-dot eval-lab__legend-dot--good" />效果变好</span><span><i className="eval-lab__legend-dot eval-lab__legend-dot--warn" />还不能下结论</span><span><i className="eval-lab__legend-dot eval-lab__legend-dot--bad" />已回到原方案</span> 打开完整报告可核对原始输入、输出、验收结果和 Agent 对话。</p>
        </div>
        <span className="eval-lab__matrix-count">{projects.length} 个项目 · {currentExperiments.length} 个当前实验 · {historicalExperiments.length} 条历史记录</span>
      </header>
      <div className="eval-lab__project-matrices">
        {projects.map((project, index) => (
          <article className="eval-lab__project-matrix" key={project.key}>
            <header>
              <span className="eval-lab__project-index">0{index + 1}</span>
              <div><h3>{project.title}</h3><small>{project.codeName}</small><p>{project.goal}</p></div>
              <span>{project.experiments.length} 轮实验</span>
            </header>
            <ProjectQualificationSummary evidenceCatalog={evidenceCatalog} evidenceLoading={evidenceLoading} project={project} />
            <div className="eval-lab__matrix-cards">
              {project.experiments.map((experiment) => (
                <article className="eval-lab__matrix-card" data-status={experiment.status} key={experiment.experimentId}>
                  <header>
                    <div>
                      <div className="eval-lab__matrix-card-title"><h4>{candidateLabel(experiment)}</h4><em className="eval-lab__experiment-type">{experimentTypeLabel(experiment)}</em></div>
                      <p>{splitLabel(experiment.dataset.split)} · {publicDatasetSummary(experiment)}</p>
                    </div>
                    <div className="eval-lab__matrix-card-status"><span className={`eval-lab__effect eval-lab__effect--${candidateEffect(experiment)}`}>{effectStatusLabel(candidateEffect(experiment))}</span><span className={`eval-lab__matrix-decision eval-lab__matrix-decision--${experiment.status}`}>{statusLabel(experiment.status)}</span></div>
                  </header>
                  <dl>
                    <div><dt>为什么要改</dt><dd>{publicProblemSummary(experiment)}</dd></div>
                    <div><dt>本轮改了什么</dt><dd>{publicChangeSummary(experiment)}</dd></div>
                    <div><dt>质量结果</dt><dd>{matrixQuality(experiment)}</dd></div>
                    <div><dt>可靠性与安全</dt><dd>{matrixReliability(experiment)}</dd></div>
                    <div><dt>成本与耗时观察</dt><dd>{matrixEfficiency(experiment)}</dd></div>
                  </dl>
                  {experiment.vertical === 'paw-selfboot' ? <PawSelfbootResultsChart experiment={experiment} /> : null}
                  <footer><div><strong>结论</strong><span>{publicDecisionSummary(experiment)}</span></div><button className="eval-lab__matrix-open" onClick={() => onOpenExperiment?.(experiment.experimentId)} type="button">查看完整报告</button></footer>
                </article>
              ))}
            </div>
            <footer><ShieldCheck size={14} /><strong>独立检查回执</strong><span>打开实验详情查看真实 Room 回执；没有回执时不会合成通过结论。</span></footer>
          </article>
        ))}
      </div>
      {historicalExperiments.length ? (
        <details className="eval-lab__experiment-history">
          <summary><span>查看 {historicalExperiments.length} 条历史记录</span><small>历史失败不参与当前结论</small></summary>
          <div>
            {historicalExperiments.map((experiment) => (
              <article key={experiment.experimentId}>
                <div>
                  <span>{projectTitleForExperiment(experiment)} · {statusLabel(experiment.status)}</span>
                  <strong>{candidateLabel(experiment)}</strong>
                  <small>{experiment.supersededBy ? `已由 ${experiment.supersededBy} 替代` : '已从当前比较中隔离'}</small>
                </div>
                <button aria-label={`查看历史实验 ${candidateLabel(experiment)}`} onClick={() => onOpenExperiment?.(experiment.experimentId)} type="button">查看历史报告</button>
              </article>
            ))}
          </div>
        </details>
      ) : null}
    </section>
  );
}

type ProjectQualification = {
  status: 'qualified' | 'incomplete' | 'checking';
  quality: string;
  cost: string;
  evidence: string;
  reason: string;
};

type ProjectCostComparison = {
  experiment: EvalLabExperiment;
  improves: boolean;
  qualifies: boolean;
  summary: string;
  mode: 'provider_bill' | 'runtime_reconciled' | 'pricing_estimate' | 'usage_only' | 'invalid_receipt';
};

type VerifiedPriceReceipt = {
  authority: 'runtime_cost_reconciled' | 'pricing_estimate';
  requestCount?: number;
  totalEstimateUsd: number;
  billedTotalUsd?: number;
  billingStatus: 'provided' | 'not_provided';
  pricingId: string;
  provider: string;
  model: string;
  publishedDate: string;
  sourceUrl: string;
  sourceSha256: string;
  rates: {
    uncachedInputUsd: number;
    cachedInputUsd: number;
    outputUsd: number;
  };
};

function ProjectQualificationSummary({ evidenceCatalog, evidenceLoading, project }: {
  evidenceCatalog?: EvalLabEvidenceResponse;
  evidenceLoading: boolean;
  project: ProjectExperimentGroup;
}) {
  const qualification: ProjectQualification = evidenceLoading && !evidenceCatalog
    ? {
        status: 'checking',
        quality: '正在核对同一冻结候选的任务与质量门禁',
        cost: '正在核对绑定且可核验的价格回执',
        evidence: '正在读取可回溯的运行证据',
        reason: '证据目录加载完成后再生成项目验收结论。',
      }
    : projectQualification(project, evidenceCatalog);
  return (
    <section aria-label={`${project.title} 项目验收`} className="eval-lab__project-qualification" data-status={qualification.status}>
      <header>
        <strong>{qualification.status === 'checking' ? '项目验收：正在核对证据' : qualification.status === 'qualified' ? '项目验收：已达标' : '项目验收：未完成'}</strong>
        <span>必须由同一冻结候选证明任务成功、质量不退化且成本下降；耗时只作诊断。</span>
      </header>
      <dl>
        <div><dt>质量 / 任务结果</dt><dd>{qualification.quality}</dd></div>
        <div><dt>成本</dt><dd>{qualification.cost}</dd></div>
        <div><dt>证据形态</dt><dd>{qualification.evidence}</dd></div>
      </dl>
      <p>{qualification.reason}</p>
    </section>
  );
}

function projectQualification(project: ProjectExperimentGroup, evidenceCatalog?: EvalLabEvidenceResponse): ProjectQualification {
  const qualityExperiments = project.experiments.filter(experimentShowsQualityImprovement);
  const finalContractExperiments = project.experiments.filter(experimentPassesFinalTaskContract);
  const nonRegressingFinalExperiments = finalContractExperiments.filter(experimentQualityDoesNotRegress);
  const costComparisons = project.experiments.flatMap((experiment) => {
    const comparison = projectCostComparison(experiment, evidenceCatalog);
    return comparison ? [comparison] : [];
  });
  const qualified = costComparisons.find(({ experiment, improves, qualifies }) => (
    experiment.status === 'kept'
    && experimentPassesFinalTaskContract(experiment)
    && experimentQualityDoesNotRegress(experiment)
    && qualifies
    && improves
  ));
  const lowerCostProof = costComparisons.find(({ improves, qualifies }) => qualifies && improves);
  const comparableCost = costComparisons.find(({ qualifies }) => qualifies);
  const invalidReceipt = costComparisons.find(({ mode }) => mode === 'invalid_receipt');
  const usageOnly = costComparisons.find(({ mode }) => mode === 'usage_only');
  const evidence = projectEvidenceShape(project, evidenceCatalog);
  if (qualified) {
    return {
      status: 'qualified',
      quality: `${experimentShowsQualityImprovement(qualified.experiment) ? '最终任务成功且质量提高' : '最终任务成功且质量不退化'}：${candidateLabel(qualified.experiment)}`,
      cost: qualified.summary,
      evidence,
      reason: '同一冻结候选已证明任务成功、质量不退化且成本下降。',
    };
  }
  return {
    status: 'incomplete',
    quality: nonRegressingFinalExperiments.length
      ? `最终任务成功且质量不退化：${nonRegressingFinalExperiments.map(candidateLabel).join('、')}`
      : finalContractExperiments.length
        ? `最终任务成功，但相对基线的质量不退化尚未证明：${finalContractExperiments.map(candidateLabel).join('、')}`
      : qualityExperiments.length
        ? `质量有改善，但最终任务合同尚未全部通过：${qualityExperiments.map(candidateLabel).join('、')}`
        : '尚无同时通过最终任务与质量门禁的候选',
    cost: lowerCostProof
      ? `${lowerCostProof.summary}，但该候选未同时通过质量门禁`
      : comparableCost
        ? `${comparableCost.summary}；价格没有下降`
        : invalidReceipt?.summary
          ?? usageOnly?.summary
          ?? '同一候选缺少绑定且可核验的价格回执；Token 用量不能替代价格',
    evidence,
    reason: '同一冻结候选尚未同时证明任务成功、质量不退化与成本下降。',
  };
}

function projectCostComparison(experiment: EvalLabExperiment, evidenceCatalog?: EvalLabEvidenceResponse): ProjectCostComparison | undefined {
  const baseline = experiment.baseline.metrics;
  const candidate = experiment.candidate.metrics;
  const sameRoute = baseline.sameProviderModelThinking === 1
    && candidate.sameProviderModelThinking === 1;
  const usageReceipts = [baseline, candidate].every((metrics) => (
    metrics.usageReceiptAvailable === 1 || metrics.costReceiptAvailable === 1
  ));
  const beforeTokens = usageTokenCategories(baseline);
  const afterTokens = usageTokenCategories(candidate);
  const metricUsage = sameRoute && usageReceipts && beforeTokens && afterTokens
    ? usageComparisonSummary(beforeTokens, afterTokens)
    : undefined;

  if (
    baseline.categoryReceiptComplete === 0
    || candidate.categoryReceiptComplete === 0
    || baseline.usageReceiptAvailable === 0
    || candidate.usageReceiptAvailable === 0
  ) {
    return {
      experiment,
      improves: false,
      qualifies: false,
      summary: `${metricUsage?.summary ?? 'Usage 回执'}；Usage 分类不完整，价格比较已关闭`,
      mode: 'invalid_receipt',
    };
  }

  const baselinePrice = priceReceiptForRun(evidenceCatalog, experiment.baseline);
  const candidatePrice = priceReceiptForRun(evidenceCatalog, experiment.candidate);
  if (baselinePrice.issue || candidatePrice.issue) {
    return {
      experiment,
      improves: false,
      qualifies: false,
      summary: [baselinePrice.issue, candidatePrice.issue].filter(Boolean).join('；'),
      mode: 'invalid_receipt',
    };
  }
  if (baselinePrice.receipt && candidatePrice.receipt) {
    if (!priceProvenanceMatches(baselinePrice.receipt, candidatePrice.receipt)) {
      return {
        experiment,
        improves: false,
        qualifies: false,
        summary: 'Baseline 与 Candidate 的价格回执 provenance 不一致，价格比较已关闭',
        mode: 'invalid_receipt',
      };
    }
    const factorShapeIsComparable = (
      experiment.candidateType === 'single_factor'
      && experiment.factors.length === 1
    ) || (
      experiment.candidateType === 'compound_repair'
      && experiment.factors.length > 1
    );
    if (!factorShapeIsComparable || experiment.baseline.runId === experiment.candidate.runId) {
      return {
        experiment,
        improves: false,
        qualifies: false,
        summary: '价格回执已找到，但实验缺少明确的单因素或多阶段候选合同，价格比较已关闭',
        mode: 'invalid_receipt',
      };
    }
    const bothBilled = baselinePrice.receipt.billingStatus === 'provided'
      && candidatePrice.receipt.billingStatus === 'provided'
      && baselinePrice.receipt.billedTotalUsd !== undefined
      && candidatePrice.receipt.billedTotalUsd !== undefined;
    const before = bothBilled ? baselinePrice.receipt.billedTotalUsd! : baselinePrice.receipt.totalEstimateUsd;
    const after = bothBilled ? candidatePrice.receipt.billedTotalUsd! : candidatePrice.receipt.totalEstimateUsd;
    const estimateOnly = baselinePrice.receipt.authority === 'pricing_estimate'
      && candidatePrice.receipt.authority === 'pricing_estimate';
    if (baselinePrice.receipt.authority !== candidatePrice.receipt.authority) {
      return {
        experiment,
        improves: false,
        qualifies: false,
        summary: 'Baseline 与 Candidate 的成本证据等级不同，价格比较已关闭',
        mode: 'invalid_receipt',
      };
    }
    return {
      experiment,
      improves: after < before,
      qualifies: true,
      summary: verifiedPriceSummary(baselinePrice.receipt, candidatePrice.receipt, before, after, bothBilled),
      mode: bothBilled ? 'provider_bill' : estimateOnly ? 'pricing_estimate' : 'runtime_reconciled',
    };
  }

  const baselineEvidence = usageEvidenceRun(evidenceCatalog, experiment.baseline);
  const candidateEvidence = usageEvidenceRun(evidenceCatalog, experiment.candidate);
  if (!metricUsage && baselineEvidence && candidateEvidence && sameEvidenceRoute(baselineEvidence, candidateEvidence)) {
    const evidenceBefore = usageTokenCategories(usageReceiptMetrics(baselineEvidence));
    const evidenceAfter = usageTokenCategories(usageReceiptMetrics(candidateEvidence));
    if (evidenceBefore && evidenceAfter) {
      const usage = usageComparisonSummary(evidenceBefore, evidenceAfter);
      return {
        experiment,
        improves: usage.improves,
        qualifies: false,
        summary: usage.summary,
        mode: 'usage_only',
      };
    }
  }
  if (metricUsage) {
    return {
      experiment,
      improves: metricUsage.improves,
      qualifies: false,
      summary: metricUsage.summary,
      mode: 'usage_only',
    };
  }
  return undefined;
}

function usageComparisonSummary(
  before: { uncachedInput: number; cachedInput: number; output: number },
  after: { uncachedInput: number; cachedInput: number; output: number },
): { improves: boolean; summary: string } {
  const categories = (['uncachedInput', 'cachedInput', 'output'] as const);
  const nonIncreasing = categories.every((category) => after[category] <= before[category]);
  const strictDecrease = categories.some((category) => after[category] < before[category]);
  const improves = nonIncreasing && strictDecrease;
  const observation = `同模型 OAuth 用量回执：未缓存输入 ${formatMetric(before.uncachedInput)} → ${formatMetric(after.uncachedInput)} · 缓存输入 ${formatMetric(before.cachedInput)} → ${formatMetric(after.cachedInput)} · 输出 ${formatMetric(before.output)} → ${formatMetric(after.output)}`;
  return {
    improves,
    summary: improves
      ? `${observation}；用量下降，但缺少绑定且可核验的价格回执，不能证明成本下降`
      : `${observation}；用量未全面下降，且缺少绑定且可核验的价格回执`,
  };
}

function priceReceiptForRun(
  catalog: EvalLabEvidenceResponse | undefined,
  version: EvalLabExperiment['baseline'],
): { receipt?: VerifiedPriceReceipt; issue?: string } {
  if (!catalog) return {};
  const bindings = experimentRunBindings(version);
  const candidates = catalog.runs.filter((run) => (
    bindings.some((binding) => evidenceRunMatches(binding, run.runId))
    && Boolean(run.environment.costEstimate || run.environment.pricingIdentity || run.environment.billing)
  ));
  if (!candidates.length) return {};
  for (const run of candidates) {
    const receipt = parseVerifiedPriceReceipt(run, version.runId);
    if (!receipt) continue;
    if (pricingIdentityHasCollision(catalog, receipt)) {
      return { issue: `价格版本 ${receipt.pricingId} 在证据目录中对应多组费率，provenance 不一致或不唯一，价格比较已关闭` };
    }
    return { receipt };
  }
  return {
    issue: `${version.runId} 缺少 Runtime 逐请求成本对账权限，或价格回执没有通过绑定、Usage、定价来源与金额校验`,
  };
}

function parseVerifiedPriceReceipt(run: EvalLabEvidenceRun, sourceRunId: string): VerifiedPriceReceipt | undefined {
  const origin = `${run.sourceId ?? ''} ${run.sourceLabel ?? ''}`.toLocaleLowerCase();
  if (origin.includes('preview') || !run.reportAvailable) return undefined;
  const usage = record(run.environment.usageReceipt);
  const estimate = record(run.environment.costEstimate);
  const pricing = record(run.environment.pricingIdentity);
  const rates = record(pricing.rates);
  const billing = record(run.environment.billing);
  const runtimeCost = record(run.environment.runtimeCostReceipt);
  const reportedCost = record(runtimeCost.reportedCostUsd);
  const costAuthority = strictText(run.environment.costAuthority);
  const sourceRef = strictText(usage.sourceRef);
  const usageSourceSha = strictSha256(usage.sourceSha256);
  const sourceIdentity = sourceRef?.split(':').at(-1) ?? '';
  const isRuntimeReceipt = costAuthority === 'runtime_cost_reconciled';
  const isBoundEstimate = costAuthority === 'pricing_estimate';
  if (
    (!isRuntimeReceipt && !isBoundEstimate)
    || usage.available !== true
    || !sourceRef
    || (isRuntimeReceipt && !sourceRef.startsWith('runtime-cost:'))
    || (isBoundEstimate && !sourceRef.startsWith('public-report:'))
    || !usageSourceSha
    || canonicalEvidenceRunId(sourceIdentity) !== canonicalEvidenceRunId(sourceRunId)
  ) return undefined;
  const uncachedInputTokens = strictTokenCount(usage.uncachedInputTokens);
  const cachedInputTokens = strictTokenCount(usage.cachedInputTokens);
  const outputTokens = strictTokenCount(usage.outputTokens);
  const uncachedInputUsd = strictDecimal(rates.uncachedInputUsd);
  const cachedInputUsd = strictDecimal(rates.cachedInputUsd);
  const outputUsd = strictDecimal(rates.outputUsd);
  const uncachedInputCostUsd = strictDecimal(estimate.uncachedInputCostUsd);
  const cachedInputCostUsd = strictDecimal(estimate.cachedInputCostUsd);
  const outputCostUsd = strictDecimal(estimate.outputCostUsd);
  const totalEstimateUsd = strictDecimal(estimate.totalCostUsd);
  const requestCount = isRuntimeReceipt ? strictPositiveInteger(runtimeCost.requestCount) : undefined;
  const runtimeDbSha256 = strictSha256(runtimeCost.runtimeDbSha256);
  const runtimeSourceSha256 = strictSha256(runtimeCost.sourceSha256);
  const transcriptSha256s = Array.isArray(runtimeCost.transcriptSha256s)
    ? runtimeCost.transcriptSha256s.map(strictSha256)
    : [];
  const reportedInputCostUsd = strictDecimal(reportedCost.input);
  const reportedCachedCostUsd = strictDecimal(reportedCost.cacheRead);
  const reportedOutputCostUsd = strictDecimal(reportedCost.output);
  const reportedTotalCostUsd = strictDecimal(reportedCost.total);
  if (
    uncachedInputTokens === undefined || cachedInputTokens === undefined || outputTokens === undefined
    || uncachedInputTokens + cachedInputTokens + outputTokens <= 0
    || uncachedInputUsd === undefined || cachedInputUsd === undefined || outputUsd === undefined
    || uncachedInputCostUsd === undefined || cachedInputCostUsd === undefined || outputCostUsd === undefined
    || totalEstimateUsd === undefined || totalEstimateUsd <= 0
  ) return undefined;
  const expectedParts = [
    uncachedInputTokens * uncachedInputUsd / 1_000_000,
    cachedInputTokens * cachedInputUsd / 1_000_000,
    outputTokens * outputUsd / 1_000_000,
  ];
  if (
    !approximatelyEqual(uncachedInputCostUsd, expectedParts[0])
    || !approximatelyEqual(cachedInputCostUsd, expectedParts[1])
    || !approximatelyEqual(outputCostUsd, expectedParts[2])
    || !approximatelyEqual(totalEstimateUsd, expectedParts.reduce((sum, value) => sum + value, 0))
  ) return undefined;
  if (isRuntimeReceipt && (
    requestCount === undefined || !runtimeDbSha256 || !runtimeSourceSha256
    || runtimeSourceSha256 !== usageSourceSha
    || transcriptSha256s.length === 0
    || transcriptSha256s.some((value) => !value)
    || new Set(transcriptSha256s).size !== transcriptSha256s.length
    || reportedInputCostUsd === undefined || reportedCachedCostUsd === undefined
    || reportedOutputCostUsd === undefined || reportedTotalCostUsd === undefined
    || !approximatelyEqual(reportedInputCostUsd, uncachedInputCostUsd)
    || !approximatelyEqual(reportedCachedCostUsd, cachedInputCostUsd)
    || !approximatelyEqual(reportedOutputCostUsd, outputCostUsd)
    || !approximatelyEqual(reportedTotalCostUsd, totalEstimateUsd)
  )) return undefined;
  const pricingId = strictText(pricing.pricingId);
  const provider = strictText(pricing.provider);
  const model = strictText(pricing.model);
  const publishedDate = strictText(pricing.publishedDate);
  const sourceUrl = strictText(pricing.sourceUrl);
  const sourceSha256 = strictSha256(pricing.sourceSha256);
  if (
    !pricingId || !/^pricing:sha256:[a-f0-9]{64}$/u.test(pricingId) || !provider || !model
    || pricing.currency !== 'USD' || pricing.unit !== 'per_million_tokens'
    || !publishedDate || !/^20\d{2}-(0[1-9]|1[0-2])-([0-2]\d|3[01])$/u.test(publishedDate)
    || !sourceUrl || !sourceUrl.startsWith('https://') || !sourceSha256
  ) return undefined;
  const billingStatus = billing.status;
  let billedTotalUsd: number | undefined;
  if (billingStatus === 'provided') {
    billedTotalUsd = strictDecimal(billing.totalUsd);
    if (
      billing.currency !== 'USD'
      || billedTotalUsd === undefined
      || billedTotalUsd <= 0
      || !strictText(billing.receiptRef)
      || !strictSha256(billing.receiptSha256)
    ) return undefined;
  } else if (billingStatus !== 'not_provided') return undefined;
  return {
    authority: costAuthority as VerifiedPriceReceipt['authority'],
    ...(requestCount !== undefined ? { requestCount } : {}),
    totalEstimateUsd,
    ...(billedTotalUsd !== undefined ? { billedTotalUsd } : {}),
    billingStatus,
    pricingId,
    provider,
    model,
    publishedDate,
    sourceUrl,
    sourceSha256,
    rates: { uncachedInputUsd, cachedInputUsd, outputUsd },
  };
}

function priceProvenanceMatches(before: VerifiedPriceReceipt, after: VerifiedPriceReceipt): boolean {
  const sameSource = before.provider === after.provider
    && before.sourceSha256 === after.sourceSha256;
  if (!sameSource) return false;
  if (before.pricingId !== after.pricingId) return true;
  return before.model === after.model
    && before.rates.uncachedInputUsd === after.rates.uncachedInputUsd
    && before.rates.cachedInputUsd === after.rates.cachedInputUsd
    && before.rates.outputUsd === after.rates.outputUsd;
}

function pricingIdentityHasCollision(catalog: EvalLabEvidenceResponse, receipt: VerifiedPriceReceipt): boolean {
  return catalog.runs.some((run) => {
    const pricing = record(run.environment.pricingIdentity);
    if (strictText(pricing.pricingId) !== receipt.pricingId) return false;
    const rates = record(pricing.rates);
    return strictText(pricing.provider) !== receipt.provider
      || strictText(pricing.model) !== receipt.model
      || strictText(pricing.publishedDate) !== receipt.publishedDate
      || strictText(pricing.sourceUrl) !== receipt.sourceUrl
      || strictText(pricing.sourceSha256) !== receipt.sourceSha256
      || strictDecimal(rates.uncachedInputUsd) !== receipt.rates.uncachedInputUsd
      || strictDecimal(rates.cachedInputUsd) !== receipt.rates.cachedInputUsd
      || strictDecimal(rates.outputUsd) !== receipt.rates.outputUsd;
  });
}

function verifiedPriceSummary(
  beforeReceipt: VerifiedPriceReceipt,
  afterReceipt: VerifiedPriceReceipt,
  before: number,
  after: number,
  billed: boolean,
): string {
  const decrease = ((before - after) / before) * 100;
  const pricingId = beforeReceipt.pricingId === afterReceipt.pricingId
    ? beforeReceipt.pricingId
    : `${beforeReceipt.pricingId} → ${afterReceipt.pricingId}`;
  const dates = beforeReceipt.publishedDate === afterReceipt.publishedDate
    ? beforeReceipt.publishedDate
    : `${beforeReceipt.publishedDate} → ${afterReceipt.publishedDate}`;
  const beforeRates = priceRatesSummary(beforeReceipt);
  const afterRates = priceRatesSummary(afterReceipt);
  const rates = beforeRates === afterRates ? beforeRates : `Baseline ${beforeRates}；Candidate ${afterRates}`;
  const billStatus = beforeReceipt.billingStatus === afterReceipt.billingStatus
    ? beforeReceipt.billingStatus === 'provided' ? '已提供并绑定' : '未提供'
    : `Baseline ${beforeReceipt.billingStatus} / Candidate ${afterReceipt.billingStatus}`;
  const runtimeReconciled = beforeReceipt.authority === 'runtime_cost_reconciled'
    && afterReceipt.authority === 'runtime_cost_reconciled';
  const requestEvidence = runtimeReconciled
    ? `逐请求回执 ${beforeReceipt.requestCount === afterReceipt.requestCount ? beforeReceipt.requestCount : `${beforeReceipt.requestCount} → ${afterReceipt.requestCount}`} 次`
    : '报告用量回执已绑定';
  const priceLabel = billed ? 'Provider 账单' : runtimeReconciled ? 'Runtime 对账 API 成本' : '绑定用量 API 估算';
  return `${priceLabel} $${before.toFixed(4)} USD → $${after.toFixed(4)} USD（${decrease >= 0 ? '降低' : '增加'} ${Math.abs(decrease).toFixed(1)}%） · ${requestEvidence} · 价格版本 ${pricingId} · 发布日 ${dates} · 费率/百万 Token：${rates} · Provider 账单：${billStatus}`;
}

function priceRatesSummary(receipt: VerifiedPriceReceipt): string {
  return `未缓存输入 $${formatCompactUsd(receipt.rates.uncachedInputUsd)} · 缓存输入 $${formatCompactUsd(receipt.rates.cachedInputUsd)} · 输出 $${formatCompactUsd(receipt.rates.outputUsd)}`;
}

function formatCompactUsd(value: number): string {
  return Number.isInteger(value) ? value.toFixed(0) : String(value);
}

function strictText(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined;
}

function strictSha256(value: unknown): string | undefined {
  const text = strictText(value);
  return text && /^[a-f0-9]{64}$/u.test(text) ? text : undefined;
}

function strictDecimal(value: unknown): number | undefined {
  if (typeof value !== 'string' || !/^(0|[1-9]\d*)(\.\d+)?$/u.test(value)) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function strictPositiveInteger(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0
    ? value
    : undefined;
}

function strictTokenCount(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
}

function approximatelyEqual(left: number, right: number): boolean {
  return Math.abs(left - right) <= Math.max(1e-9, Math.abs(right) * 1e-9);
}

function usageTokenCategories(metrics: Readonly<Record<string, number>>): {
  uncachedInput: number;
  cachedInput: number;
  output: number;
} | undefined {
  const uncachedInput = firstMetric(metrics, ['uncachedInputTokens', 'inputTokens', 'input']);
  const cachedInput = firstMetric(metrics, ['cachedInputTokens', 'cacheReadTokens', 'cacheRead']);
  const output = firstMetric(metrics, ['outputTokens', 'output']);
  if (uncachedInput === undefined || cachedInput === undefined || output === undefined) return undefined;
  if (uncachedInput < 0 || cachedInput < 0 || output < 0) return undefined;
  return { uncachedInput, cachedInput, output };
}

function usageEvidenceRun(
  catalog: EvalLabEvidenceResponse | undefined,
  version: EvalLabExperiment['baseline'],
): EvalLabEvidenceRun | undefined {
  if (!catalog) return undefined;
  const bindings = experimentRunBindings(version);
  return catalog.runs.find((run) => {
    const origin = `${run.sourceId ?? ''} ${run.sourceLabel ?? ''}`.toLocaleLowerCase();
    const receipt = run.environment.usageReceipt;
    return !origin.includes('preview')
      && Boolean(receipt)
      && receipt?.categoryReceiptComplete !== false
      && bindings.some((binding) => evidenceRunMatches(binding, run.runId));
  });
}

function sameEvidenceRoute(baseline: EvalLabEvidenceRun, candidate: EvalLabEvidenceRun): boolean {
  return (['provider', 'model', 'thinking'] as const).every((key) => {
    const before = baseline.environment[key]?.trim();
    const after = candidate.environment[key]?.trim();
    return Boolean(before) && before === after;
  });
}

function usageReceiptMetrics(run: EvalLabEvidenceRun): Readonly<Record<string, number>> {
  return Object.fromEntries(Object.entries(run.environment.usageReceipt ?? {}).filter((entry): entry is [string, number] => (
    typeof entry[1] === 'number' && Number.isFinite(entry[1])
  )));
}

function experimentShowsQualityImprovement(experiment: EvalLabExperiment): boolean {
  if (experiment.status === 'rejected' || experiment.status === 'open_gap') return false;
  const effect = candidateEffect(experiment);
  return effect === 'improved' || effect === 'scoring_recovered';
}

function experimentQualityDoesNotRegress(experiment: EvalLabExperiment): boolean {
  if (!experimentPassesFinalTaskContract(experiment)) return false;
  const effect = candidateEffect(experiment);
  if (['regressed', 'runtime_failed', 'unverified', 'not_run', 'observed_failure', 'partial'].includes(effect)) return false;
  const higherIsBetter = [
    'taskSuccessRate', 'verifierPassRate', 'taskSuccess', 'answerCoverage', 'answerSuccessRate',
    'answerJudgeCorrectnessRate', 'highLevelFactCoverage', 'citationFactCoverage',
    'answerableCitationSupportRate', 'citationResolutionRate', 'toolSuccessRate', 'outputProtocolRate',
    'abstentionAccuracy', 'abstentionF1', 'ca', 'jra', 'top3Jra', 'recallAt10', 'mrr', 'ndcgAt10',
  ];
  const lowerIsBetter = [
    'failedTasks',
    'falseAbstentionRate',
    ...(zeroToolFailuresRequired(experiment) ? ['failedToolCalls'] : []),
  ];
  const baseline = experiment.baseline.metrics;
  const candidate = experiment.candidate.metrics;
  if (higherIsBetter.some((key) => hasMetric(baseline, key) && hasMetric(candidate, key) && candidate[key] < baseline[key])) return false;
  if (lowerIsBetter.some((key) => hasMetric(baseline, key) && hasMetric(candidate, key) && candidate[key] > baseline[key])) return false;
  return true;
}

function zeroToolFailuresRequired(experiment: EvalLabExperiment): boolean {
  return experiment.scoring.hardGates.some((gate) => (
    /(?:zero|0|零)\s*(?:failed\s*)?(?:tool|工具)[\s_-]*(?:failure|failures|失败)/iu.test(gate)
    || /(?:tool|工具)[\s_-]*(?:failure|failures|失败)\s*(?:=|为|must be)?\s*(?:zero|0|零)/iu.test(gate)
  ));
}

function experimentPassesFinalTaskContract(experiment: EvalLabExperiment): boolean {
  const metrics = experiment.candidate.metrics;
  const noToolFailure = !zeroToolFailuresRequired(experiment)
    || !hasMetric(metrics, 'failedToolCalls')
    || metrics.failedToolCalls === 0;
  if (hasMetric(metrics, 'taskPassed') && hasMetric(metrics, 'taskTotal')) {
    return metrics.taskTotal > 0 && metrics.taskPassed === metrics.taskTotal && noToolFailure;
  }
  if (hasMetric(metrics, 'taskSuccessRate')) {
    const verifierPassed = !hasMetric(metrics, 'verifierPassRate') || metrics.verifierPassRate === 1;
    return metrics.taskSuccessRate === 1 && verifierPassed && noToolFailure;
  }
  if (experiment.evaluationKind === 'answer_evidence') {
    const success = firstMetric(metrics, ['answerSuccessRate', 'answerJudgeCorrectnessRate']);
    const citationGate = firstMetric(metrics, ['citationHardGatePassed', 'citationGatePassed', 'formalAcceptancePassed']);
    const protocol = firstMetric(metrics, ['outputProtocolRate']);
    return success === 1 && citationGate === 1 && (protocol === undefined || protocol === 1);
  }
  if (projectKeyForExperiment(experiment) === 'memory' && hasMetric(metrics, 'curationCases')) {
    return metrics.curationCases > 0
      && metrics.curationPassed === metrics.curationCases
      && metrics.durableRecallPassed === metrics.durableRecallTotal
      && metrics.abstentionPassed === metrics.abstentionTotal
      && metrics.vectorCoverage === 1
      && metrics.rollbackPassed === 1
      && metrics.replayPassed === 1
      && metrics.receiptJsonValid === 1;
  }
  if (hasMetric(metrics, 'answerCoverage') && hasMetric(metrics, 'ca')) {
    return metrics.answerCoverage === 1
      && metrics.ca === 1
      && metrics.formalScoreProduced === 1
      && noToolFailure;
  }
  return false;
}

function projectEvidenceShape(project: ProjectExperimentGroup, catalog?: EvalLabEvidenceResponse): string {
  if (project.experiments.some((experiment) => /fixture/u.test(`${experiment.dataset.datasetId} ${experiment.dataset.unit}`.toLocaleLowerCase()))) {
    const alsoUnverified = project.experiments.some((experiment) => experiment.effectStatus === 'unverified');
    return alsoUnverified
      ? '受控 Fixture 回执；另有未验证记录 · 不代表生产结果'
      : '受控 Fixture 回执 · 不代表生产结果';
  }
  const matches = catalog
    ? project.experiments.flatMap((experiment) => matchingEvidenceRuns(catalog, experiment))
    : [];
  const unique = [...new Map(matches.map((run) => [run.runId, run])).values()];
  if (unique.some((run) => `${run.sourceId ?? ''} ${run.sourceLabel ?? ''}`.toLocaleLowerCase().includes('preview'))) {
    return '示例投影 · 不代表真实验收';
  }
  const transcriptCount = unique.reduce((total, run) => total + Math.max(0, run.transcriptCount), 0);
  if (transcriptCount > 0) return `真实运行 · ${transcriptCount} 份 Transcript 可回溯`;
  if (unique.some((run) => run.reportAvailable)) return '公开回执可回溯 · Transcript 未公开';
  return '未验证 · 尚无精确 runId 证据绑定';
}

function OptimalPathPanel({ searches }: { searches: readonly EvalLabPathSearch[] }) {
  const newestReceiptBySubject = new Map<string, number>();
  for (const search of searches) {
    const subject = pathSearchSubject(search);
    newestReceiptBySubject.set(subject, Math.max(newestReceiptBySubject.get(subject) ?? 0, search.generatedAtMs));
  }

  return (
    <section aria-label="方案路径" className="eval-lab__path-searches">
      <header className="eval-lab__path-searches-header">
        <div>
          <p className="eval-lab__eyebrow"><GitBranch aria-hidden="true" size={15} /> 方案路径</p>
          <h2>方案是怎样一步步筛出来的</h2>
          <p>每轮只改变一个因素。先淘汰做错任务或不安全的方案，再从合格方案中比较成本；耗时保留为诊断信息。</p>
        </div>
        <span className="eval-lab__path-searches-count"><Scale aria-hidden="true" size={14} /> {searches.length} 条方案路径</span>
      </header>
      <div className="eval-lab__path-search-list">
        {searches.map((search) => (
          <PathSearchCard
            isSuperseded={(newestReceiptBySubject.get(pathSearchSubject(search)) ?? search.generatedAtMs) > search.generatedAtMs}
            key={search.searchId}
            search={search}
          />
        ))}
      </div>
    </section>
  );
}

function pathSearchSubject(search: EvalLabPathSearch): string {
  const separatorIndex = search.title.indexOf('·');
  return (separatorIndex === -1 ? search.title : search.title.slice(0, separatorIndex)).trim();
}

function PathSearchCard({ isSuperseded, search }: { isSuperseded: boolean; search: EvalLabPathSearch }) {
  const selectedCandidate = search.candidates.find((candidate) => candidate.nodeId === search.selectedNodeId);
  const baselineCandidate = search.candidates.find((candidate) => candidate.nodeId === 'baseline');
  return (
    <article className={`eval-lab__path-search-card${isSuperseded ? ' is-superseded' : ''}`}>
      <header>
        <div>
          <div className="eval-lab__run-heading">
            <h3>{search.title}</h3>
            <span className={isSuperseded ? 'eval-lab__path-claim eval-lab__path-claim--superseded' : `eval-lab__path-claim eval-lab__path-claim--${search.claimStatus}`}>{isSuperseded ? '历史快照' : pathClaimLabel(search.claimStatus)}</span>
          </div>
          <p>{search.objectiveSummary}</p>
        </div>
        <span className="eval-lab__path-frozen">{search.frozenControlCount} 项条件保持不变</span>
      </header>
      <div className="eval-lab__path-meta">
        <div><span>{isSuperseded ? '当时的质量门槛' : '必须通过的底线'}</span><strong>{pathGateSummary(selectedCandidate ?? baselineCandidate)}</strong></div>
        <div><span>{isSuperseded ? '当时选择' : '当前最优方案'}</span><strong>{pathLabel(selectedCandidate?.nodeId ?? search.selectedNodeId, selectedCandidate?.changedFactor)} · {isSuperseded ? '历史回执' : pathDecisionLabel(search.claimStatus)}</strong></div>
      </div>
      <div className="eval-lab__path-candidates">
        <h4>{isSuperseded ? '当时同一批任务下的方案对比' : '同一批任务下的方案对比'}</h4>
        <div className="eval-lab__path-table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">候选 / 改了什么</th>
                <th scope="col">任务完成</th>
                <th scope="col">自动验收</th>
                <th scope="col">运行错误</th>
                <th scope="col">效率</th>
                <th scope="col">API 成本</th>
                <th scope="col">判定</th>
              </tr>
            </thead>
            <tbody>
              {search.candidates.map((candidate) => (
                <tr key={candidate.nodeId} className={candidate.nodeId === search.selectedNodeId ? 'is-selected' : undefined}>
                  <th scope="row">
                    <strong>{pathLabel(candidate.nodeId, candidate.changedFactor)}</strong>
                    <span className="eval-lab__path-change">{factorLabel(candidate.changedFactor)}</span>
                  </th>
                  <td>{pathMetricValue(candidate.metrics, 'taskSuccessRate')}</td>
                  <td>{pathMetricValue(candidate.metrics, 'verifierPassRate')}</td>
                  <td>{pathMetricValue(candidate.metrics, 'failedToolCalls')}</td>
                  <td>
                    <span>{pathMetricValue(candidate.metrics, 'toolCalls')} 次工具调用</span>
                    <small>{pathMetricValue(candidate.metrics, 'latencyMs')}</small>
                  </td>
                  <td>{pathMetricValue(candidate.metrics, 'apiCostUsd')}</td>
                  <td>
                    <span className={`eval-lab__path-status eval-lab__path-status--${candidate.status}`}>{pathCandidateStatusLabel(candidate, isSuperseded)}</span>
                    <small className="eval-lab__path-reason">{candidate.reason}</small>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="eval-lab__path-candidate-details">
          {search.candidates.map((candidate) => (
            <details key={`${candidate.nodeId}-detail`} className="eval-lab__path-detail">
              <summary><span className="eval-lab__path-detail-title">{pathLabel(candidate.nodeId, candidate.changedFactor)}</span><span>{pathCandidateStatusLabel(candidate, isSuperseded)} · 查看优化与证据边界</span></summary>
              <div className="eval-lab__path-detail-grid">
                <div><span>改动</span><strong>{factorLabel(candidate.changedFactor)}</strong><p>{candidate.changedFactor === 'baseline' ? '冻结基线，不做改动。' : `只改变${factorLabel(candidate.changedFactor)}，其余控制保持冻结。`}</p></div>
                <div><span>结果</span><strong>{pathOutcomeSummary(candidate.metrics)}</strong><p>判定说明已在矩阵“判定”列展示；详细 receipt、配置 hash 和 split 仍以评测账本为准。</p></div>
                <div><span>证据边界</span><strong>{candidate.changedFactor === 'baseline' ? pathCandidateStatusLabel(candidate, isSuperseded) : candidate.status === 'eligible' ? (isSuperseded ? '当时 Validation 可行' : 'Validation 可行') : pathStatusLabel(candidate.status, isSuperseded)}</strong><p>原始节点：{candidate.nodeId}；详细 receipt、配置 hash 和 split 仍以评测账本为准。</p></div>
              </div>
            </details>
          ))}
        </div>
      </div>
      <p className="eval-lab__path-summary"><strong>{isSuperseded ? '历史结论：' : '当前结论：'}</strong>{isSuperseded ? '此路径已被同项目较新的回执覆盖，不代表当前选择。 ' : ''}{search.claimSummary}{selectedCandidate ? ` 当前候选为${pathLabel(selectedCandidate.nodeId, selectedCandidate.changedFactor)}。` : ''}</p>
      <details className="eval-lab__path-raw">
        <summary>查看实验谱系与原始字段</summary>
        <p>路径：<span className="eval-lab__path-legacy-label">{search.selectedPath.map((step) => step.nodeId).join(' → ')}</span></p>
        <p>优化目标：{search.metricSummary}</p>
        <p>这里保留内部节点 ID，便于从 UI 回跳到原始 receipt；它们不是用户验收结论的替代物。</p>
      </details>
    </article>
  );
}


function pathClaimLabel(status: EvalLabPathSearch['claimStatus']): string {
  if (status === 'best_known') return '当前最好方案';
  if (status === 'blocked') return '未通过底线';
  return '证据不足';
}

function pathStatusLabel(status: EvalLabPathSearch['candidates'][number]['status'], isSuperseded = false): string {
  let label = '状态未知';
  if (status === 'eligible') label = '可采用';
  else if (status === 'rejected') label = '已淘汰';
  else if (status === 'not_evaluated') label = '未运行';
  return isSuperseded ? `当时${label}` : label;
}

function pathCandidateStatusLabel(candidate: EvalLabPathSearch['candidates'][number], isSuperseded: boolean): string {
  if (candidate.changedFactor === 'baseline') return isSuperseded ? '当时比较基线' : '比较基线';
  return pathStatusLabel(candidate.status, isSuperseded);
}

function pathMetricValue(metrics: Readonly<Record<string, number>>, name: string): string {
  if (!(name in metrics)) return name === 'apiCostUsd' ? '待补 usage' : '—';
  const value = metrics[name];
  if (name === 'taskSuccessRate' || name === 'verifierPassRate') return percent(value);
  if (name === 'latencyMs') return duration(value);
  if (name === 'apiCostUsd') return `$${value.toFixed(4)}`;
  return formatMetric(value);
}

function pathOutcomeSummary(metrics: Readonly<Record<string, number>>): string {
  const task = pathMetricValue(metrics, 'taskSuccessRate');
  const verifier = pathMetricValue(metrics, 'verifierPassRate');
  const failures = pathMetricValue(metrics, 'failedToolCalls');
  return `任务 ${task} · 自动验收 ${verifier} · 运行错误 ${failures}`;
}

function pathGateSummary(candidate: EvalLabPathSearch['candidates'][number] | undefined): string {
  if (!candidate) return '未提供质量门禁结果';
  return `任务 ${pathMetricValue(candidate.metrics, 'taskSuccessRate')} · 自动验收 ${pathMetricValue(candidate.metrics, 'verifierPassRate')} · 运行错误 ${pathMetricValue(candidate.metrics, 'failedToolCalls')}`;
}

function pathDecisionLabel(status: EvalLabPathSearch['claimStatus']): string {
  if (status === 'best_known') return '已通过质量门禁';
  if (status === 'blocked') return '被硬门禁阻断';
  return '仍需补证据';
}

type ProjectKey = 'enterpriseops' | 'enterprise-rag' | 'cloudops' | 'memory' | 'paw-selfboot';

type ProjectExperimentGroup = {
  key: ProjectKey;
  title: string;
  codeName: string;
  goal: string;
  experiments: EvalLabExperiment[];
};

const PROJECT_DEFINITIONS: ReadonlyArray<Omit<ProjectExperimentGroup, 'experiments'>> = [
  { key: 'paw-selfboot', title: 'PAW 协作与接续', codeName: 'PAW Selfboot', goal: '验证完整任务、跨阶段接续和可靠执行；成本作为代价记录，保留未完成与失败结果。' },
  { key: 'enterpriseops', title: '企业客户支持', codeName: 'EnterpriseOps', goal: '让 Agent 真正完成跨系统客户任务，并在结果不变差的前提下降低成本。' },
  { key: 'enterprise-rag', title: '企业知识库问答', codeName: 'Enterprise RAG', goal: '让关键资料稳定被找到、答案有据可查，没有依据时明确拒答。' },
  { key: 'cloudops', title: '云上事故诊断', codeName: 'CloudOps', goal: '从日志和观测中定位故障根因，同时控制错误调用和成本；耗时只用于排查循环与阻塞。' },
  { key: 'memory', title: '长期记忆整理', codeName: 'Memory Maintenance', goal: '保留真正长期有用的信息，拒绝临时、重复或越界内容，并确保可回滚。' },
];

function projectKeyForExperiment(experiment: EvalLabExperiment): ProjectKey | null {
  if (experiment.vertical === 'paw-selfboot') return 'paw-selfboot';
  const identity = `${experiment.title} ${experiment.vertical} ${experiment.evaluationKind}`.toLocaleLowerCase();
  if (identity.includes('trace agent') || identity.includes('trace_repair') || identity.includes('agent-runtime-diagnosis')) return null;
  if (identity.includes('cloudops')) return 'cloudops';
  if (identity.includes('enterprise rag') || identity.includes('enterprise-knowledge')) return 'enterprise-rag';
  if (identity.includes('memory') || identity.includes('personal-memory')) return 'memory';
  if (identity.includes('enterpriseops') || identity.includes('enterprise-customer') || identity.includes('model cost') || identity.includes('agent-evaluation-cost')) return 'enterpriseops';
  return null;
}

function groupProjectExperiments(experiments: readonly EvalLabExperiment[]): ProjectExperimentGroup[] {
  return PROJECT_DEFINITIONS.map((project) => ({
    ...project,
    experiments: experiments
      .filter((experiment) => projectKeyForExperiment(experiment) === project.key)
      .sort(compareProjectExperimentPriority),
  })).filter((project) => project.experiments.length > 0);
}

function compareProjectExperimentPriority(left: EvalLabExperiment, right: EvalLabExperiment): number {
  const rank: Record<EvalLabExperiment['status'], number> = {
    kept: 0,
    diagnostic: 1,
    open_gap: 2,
    rejected: 3,
  };
  return rank[left.status] - rank[right.status]
    || Number(right.importedAtMs) - Number(left.importedAtMs)
    || left.experimentId.localeCompare(right.experimentId);
}

function isHistoricalExperiment(experiment: EvalLabExperiment): boolean {
  return experiment.projectionState === 'history';
}

function projectTitleForExperiment(experiment: EvalLabExperiment): string {
  const key = projectKeyForExperiment(experiment);
  return PROJECT_DEFINITIONS.find((project) => project.key === key)?.title ?? verticalLabel(experiment.vertical);
}

function activeProjectExperiment(experiments: readonly EvalLabExperiment[], selectedId: string): EvalLabExperiment | undefined {
  const projectExperiments = experiments.filter((experiment) => projectKeyForExperiment(experiment) !== null);
  return projectExperiments.find((experiment) => experiment.experimentId === selectedId)
    ?? projectExperiments.find((experiment) => !isHistoricalExperiment(experiment))
    ?? projectExperiments[0];
}

function candidateLabel(experiment: EvalLabExperiment): string {
  const title = experimentTitle(experiment.title);
  return title.includes('·') ? title.split('·').slice(1).join('·').trim() : title;
}

function publicDatasetSummary(experiment: EvalLabExperiment): string {
  if (experiment.vertical === 'paw-selfboot') return experiment.frozenControls.find((control) => control.name === 'observation_scope')?.value ?? experiment.dataset.unit;
  const count = experiment.dataset.caseCount;
  const project = projectKeyForExperiment(experiment);
  if (project === 'enterpriseops') return `${count} 个客户支持任务 · 由系统逐项自动验收`;
  if (project === 'enterprise-rag' && experiment.evaluationKind === 'answer_evidence') return `${count} 个问答任务 · 同时检查答案、引用和拒答`;
  if (project === 'enterprise-rag') return `${count} 个检索问题 · 使用同一知识库`;
  if (project === 'cloudops') return `${count} 个故障诊断任务 · 使用同一组观测数据`;
  if (project === 'memory') return `${count} 个记忆案例 · 在隔离数据中运行`;
  return `${count} 个固定任务`;
}

function publicProblemSummary(experiment: EvalLabExperiment): string {
  const identity = `${experiment.experimentId} ${experiment.title}`.toLocaleLowerCase();
  if (identity.includes('execution-chain')) return '原方案在真正执行前就被权限和工具连接阻断，客户支持任务没有写入业务系统。';
  if (identity.includes('state-contract')) return '原方案可能漏掉依赖步骤或过早结束，界面显示完成时，业务数据仍可能不完整。';
  if (experiment.evaluationKind === 'model_cost') return '现有方案质量已经达标，需要确认更便宜的模型能否完成同样的任务。';
  if (identity.includes('graph') && identity.includes('tag')) return '图谱与标签候选尚未取得完整输入和绑定证据，直接比较会得出不可信的结论。';
  if (experiment.evaluationKind === 'rag_retrieval') return '关键资料没有稳定排进前十条，后续回答容易漏掉真正相关的证据。';
  if (experiment.evaluationKind === 'answer_evidence') return '需要确认回答不仅看起来合理，而且每条关键事实都有原文支持；没有依据时必须拒答。';
  if (identity.includes('runtime-selection')) return '候选模型没有按配置进入任务，导致实验在真正开始前就被错误阻断。';
  if (identity.includes('luna') && projectKeyForExperiment(experiment) === 'cloudops') return '更换模型后运行超时且取消失败，没有产生可用于比较的诊断结果。';
  if (identity.includes('validation-baseline') && projectKeyForExperiment(experiment) === 'cloudops') return '原始运行虽然留下记录，却无法被稳定评分，需要先建立可信的起始结果。';
  if (projectKeyForExperiment(experiment) === 'cloudops') return '需要减少无效查询，但不能因此漏掉故障证据或降低根因判断质量。';
  if (identity.includes('observed-failure')) return '真实记忆整理运行近十四分钟后因输出不完整而失败，没有形成可用结果。';
  if (projectKeyForExperiment(experiment) === 'memory') return '需要确保长期信息能留下，临时、重复或越界内容不会进入个人记忆。';
  return humanClaimText(experiment.businessProblem);
}

function publicChangeSummary(experiment: EvalLabExperiment): string {
  const identity = `${experiment.experimentId} ${experiment.title}`.toLocaleLowerCase();
  if (identity.includes('execution-chain')) return '同时修复授权、工具连接和任务收尾，让操作能执行、结果能验收、临时数据能清理。';
  if (identity.includes('state-contract')) return '加入明确的步骤状态和依赖检查；写入后回读，全部自动验收通过后才算完成。';
  if (experiment.evaluationKind === 'model_cost') return '只替换执行模型；任务、工具、提示词、工作流程和验收标准保持不变。';
  if (identity.includes('graph') && identity.includes('tag')) return '只准备图谱与标签重排候选；因输入证据不完整，本轮没有消耗计算资源。';
  if (experiment.evaluationKind === 'rag_retrieval') return '调整检索融合与排序方式；知识库、问题和相关性标准答案保持不变。';
  if (experiment.evaluationKind === 'answer_evidence') {
    const factors = (experiment.factors ?? []).map((factor) => factorLabel(factor.name)).join('、');
    return `只调整${factors || '回答方案'}；知识库、问题和逐条验收标准保持不变。`;
  }
  if (identity.includes('runtime-selection')) return '修正运行时的模型选择顺序，确保候选模型和提示词先正确进入任务，再开始计分。';
  if (identity.includes('luna') && projectKeyForExperiment(experiment) === 'cloudops') return '只替换执行模型，其余诊断流程和测试数据保持不变。';
  if (identity.includes('validation-baseline') && projectKeyForExperiment(experiment) === 'cloudops') return '修复运行记录与评分器的绑定，先让同一批任务能够被稳定、重复地验收。';
  if (projectKeyForExperiment(experiment) === 'cloudops') {
    const factor = experiment.factors?.[0];
    return factor ? `只调整${factorLabel(factor.name)}；同一组任务、观测数据和验收标准保持不变。` : '只调整故障诊断中的一处流程，其他条件保持不变。';
  }
  if (identity.includes('observed-failure')) return '不改动原始失败记录，把它固定为后续方案必须超过的起始基线。';
  if (projectKeyForExperiment(experiment) === 'memory') {
    const factors = experiment.factors ?? [];
    if (factors.length === 1) return `只调整${factorLabel(factors[0]!.name)}；同一组记忆数据和验收标准保持不变。`;
    return '同时修复输出格式、重复运行和质量验收，作为一条组合修复记录。';
  }
  const factors = experiment.factors ?? [];
  if (factors.length === 1) return `只调整${factorLabel(factors[0]!.name)}；其他条件保持不变。`;
  if (factors.length > 1) return `同时调整${factors.map((factor) => factorLabel(factor.name)).join('、')}，作为一条组合修复记录。`;
  return '本轮没有记录具体改动。';
}

function publicDecisionSummary(experiment: EvalLabExperiment): string {
  if (experiment.vertical === 'paw-selfboot') return experiment.comparison.decisionReason;
  if (experiment.status === 'kept' && experiment.evaluationKind === 'model_cost') return '方案在实验账本中标记为保留；成本结论仍以项目验收中的绑定价格回执为准。';
  if (experiment.status === 'kept') return '主要质量指标变好，并通过当前可靠性检查，因此保留。';
  if (experiment.status === 'rejected') return '关键质量或可靠性没有通过，因此不采用，并回到上一版。';
  if (experiment.status === 'open_gap') return '还缺少完整运行或关键证据，暂时不能作为可用方案。';
  return '这轮只用于定位问题，尚未证明修复有效。';
}


function ProjectExperimentPicker({ experiments, selectedId, onSelect }: {
  experiments: readonly EvalLabExperiment[];
  selectedId: string;
  onSelect: (experimentId: string) => void;
}) {
  const projects = groupProjectExperiments(experiments.filter((experiment) => !isHistoricalExperiment(experiment)));
  const active = activeProjectExperiment(experiments, selectedId);
  const activeProjectKey = active ? projectKeyForExperiment(active) : projects[0]?.key;
  const activeProject = projects.find((project) => project.key === activeProjectKey) ?? projects[0];
  return (
    <div className="eval-lab__project-picker-stack">
      <nav aria-label="选择评测项目" className="eval-lab__scene-picker">
        <span className="eval-lab__scene-picker-label">业务场景</span>
        <div className="eval-lab__scene-picker-list">
          {projects.map((project) => (
            <button
              aria-pressed={activeProjectKey === project.key}
              key={project.key}
              onClick={() => onSelect(project.experiments[0]?.experimentId ?? '')}
              type="button"
            >
              {project.title}
            </button>
          ))}
        </div>
      </nav>
      {activeProject && activeProject.experiments.length > 1 ? <nav aria-label="选择候选实验" className="eval-lab__candidate-picker">
        <span>实验轮次</span>
        <div>{activeProject.experiments.map((experiment) => (
          <button
            aria-pressed={active?.experimentId === experiment.experimentId}
            key={experiment.experimentId}
            onClick={() => onSelect(experiment.experimentId)}
            type="button"
          >
            {candidateLabel(experiment)}
          </button>
        ))}</div>
      </nav> : null}
      {active && isHistoricalExperiment(active) ? <div className="eval-lab__history-selection"><span>当前打开的是只读历史记录，不参与当前结论。</span><button onClick={() => onSelect(activeProject?.experiments[0]?.experimentId ?? '')} type="button">返回当前结果</button></div> : null}
    </div>
  );
}

function pathLabel(nodeId: string, changedFactor?: string): string {
  const labels: Record<string, string> = {
    baseline: '原方案',
    'state-contract': '状态合同工作流',
    'sol-state-contract': 'Sol · 状态合同',
    'luna-state-contract': 'Luna · 状态合同',
    luna: 'Luna 模型',
  };
  if (labels[nodeId]) return labels[nodeId];
  if (changedFactor === 'baseline') return nodeId.toLocaleLowerCase().startsWith('sol-') ? 'Sol · 比较基线' : '比较基线';
  if (nodeId.toLocaleLowerCase().startsWith('luna-')) return `Luna · ${factorLabel(changedFactor ?? 'model')}候选`;
  if (nodeId.toLocaleLowerCase().startsWith('sol-')) return `Sol · ${factorLabel(changedFactor ?? 'model')}候选`;
  return changedFactor ? `${factorLabel(changedFactor)}候选` : nodeId;
}

function metricDigest(metrics: Readonly<Record<string, number>>): string {
  const entries = Object.entries(metrics).slice(0, 4);
  return entries.length ? entries.map(([name, value]) => `${metricLabel(name)} ${formatMetricByName(name, value)}`).join(' · ') : '尚未运行';
}

function isCloudOpsTranscriptOnlyFailure(experiment: EvalLabExperiment): boolean {
  const identity = `${experiment.title} ${experiment.vertical}`.toLocaleLowerCase();
  const candidate = experiment.candidate.metrics;
  return identity.includes('cloudops')
    && typeof candidate.transcriptToolCalls === 'number'
    && candidate.hostFormalCaJraAvailable === 0;
}

function isCloudOpsRuntimeSelectionRepair(experiment: EvalLabExperiment): boolean {
  return experiment.experimentId === 'cloudops.runtime-selection-repair-retry3.v1';
}

function matrixQuality(experiment: EvalLabExperiment): string {
  if (experiment.vertical === 'paw-selfboot') return pawSelfbootQuality(experiment);
  const baseline = experiment.baseline.metrics;
  const candidate = experiment.candidate.metrics;
  if (isCloudOpsTranscriptOnlyFailure(experiment)) {
    return '没有业务质量分 · 正式故障诊断评分没有运行，只留下了运行记录';
  }
  if (isCloudOpsRuntimeSelectionRepair(experiment)) {
    return '运行时选择 未通过 → 通过 · Prompt 未进入 → 已进入 · 业务质量分未产生';
  }
  if (experiment.evaluationKind === 'answer_evidence') return answerEvidenceQualitySummary(experiment);
  if (experiment.evaluationKind === 'rag_retrieval' && (hasMetric(baseline, 'recallAt10') || hasMetric(candidate, 'recallAt10') || hasMetric(baseline, 'recall_at_10') || hasMetric(candidate, 'recall_at_10'))) {
    return `关键证据进入前十条 ${ratePair(baseline.recallAt10 ?? baseline.recall_at_10, candidate.recallAt10 ?? candidate.recall_at_10)} · 首条正确证据排名 ${ratePair(baseline.mrr, candidate.mrr)} · 整体排序质量 ${ratePair(baseline.ndcgAt10 ?? baseline.ndcg_at_10, candidate.ndcgAt10 ?? candidate.ndcg_at_10)}`;
  }
  if (experiment.evaluationKind === 'memory') {
    if (hasMetric(candidate, 'curationCases')) {
      return `整理通过 ${integerMetric(candidate.curationPassed)}/${integerMetric(candidate.curationCases)} · 长期信息召回 ${integerMetric(candidate.durableRecallPassed)}/${integerMetric(candidate.durableRecallTotal)} · 不该记的内容成功拦截 ${integerMetric(candidate.abstentionPassed)}/${integerMetric(candidate.abstentionTotal)}`;
    }
    const reviewed = candidate.reviewed ?? baseline.reviewed;
    const reviewPassed = candidate.reviewed ? candidate.passed : baseline.passed;
    const behaviorTotal = typeof candidate.behaviorCases === 'number' && typeof candidate.repeats === 'number'
      ? candidate.behaviorCases * candidate.repeats
      : 0;
    return `独立复核 ${integerMetric(reviewPassed)}/${integerMetric(reviewed)} · 行为检查 ${integerMetric(candidate.passed)}/${integerMetric(behaviorTotal || candidate.passed)}`;
  }
  if (hasMetric(baseline, 'taskSuccessRate') && hasMetric(candidate, 'taskSuccessRate')) {
    return `任务完成 ${ratePair(baseline.taskSuccessRate, candidate.taskSuccessRate)} · 验收条件 ${ratePair(baseline.verifierPassRate, candidate.verifierPassRate)}`;
  }
  if (hasMetric(baseline, 'ca') && hasMetric(candidate, 'ca')) {
    return `故障组件命中 ${ratePair(baseline.ca, candidate.ca)} · 组件与故障类型同时命中 ${ratePair(baseline.jra, candidate.jra)}`;
  }
  if (hasMetric(candidate, 'realPredictionCases')) return `真实预测 ${integerMetric(candidate.realPredictionCases)}（尚未形成成功样本）`;
  return metricDigest(candidate);
}

function matrixReliability(experiment: EvalLabExperiment): string {
  if (experiment.vertical === 'paw-selfboot') return pawSelfbootReliability(experiment);
  const candidate = experiment.candidate.metrics;
  if (isCloudOpsTranscriptOnlyFailure(experiment)) {
    return `运行记录中的工具调用 ${integerMetric(candidate.transcriptToolCalls)}（不是可比较的业务工具调用） · 失败 ${integerMetric(candidate.failedTranscriptToolCalls)} · 第三批超时 · 取消也超时`;
  }
  if (isCloudOpsRuntimeSelectionRepair(experiment)) {
    return '旧阻断已修复，但随后出现 8 次模型服务连接失败 · 0 次业务工具调用 · 0 条标准结果提交';
  }
  if (experiment.evaluationKind === 'answer_evidence') return answerEvidenceReliabilitySummary(experiment);
  if (experiment.evaluationKind === 'trace_repair') return '诊断标准答案未就绪 · 真实预测 0';
  if (experiment.evaluationKind === 'memory') {
    if (hasMetric(candidate, 'vectorCoverage')) return `向量可检索覆盖 ${percent(candidate.vectorCoverage)} · 回滚/重复运行 ${candidate.rollbackPassed === 1 && candidate.replayPassed === 1 ? '通过' : '未通过'} · 未触碰生产记忆库`;
    return '反回声、墓碑、项目范围门禁通过';
  }
  if (experiment.evaluationKind === 'model_cost') {
    const baseline = experiment.baseline.metrics;
    const receiptReady = candidate.costReceiptAvailable === 1;
    const qualityComparable = (
      hasMetric(baseline, 'taskSuccessRate')
      && hasMetric(candidate, 'taskSuccessRate')
      && hasMetric(baseline, 'verifierPassRate')
      && hasMetric(candidate, 'verifierPassRate')
    );
    const qualityPassed = qualityComparable
      && candidate.taskSuccessRate >= baseline.taskSuccessRate
      && candidate.verifierPassRate >= baseline.verifierPassRate;
    const quality = !qualityComparable
      ? '质量回执不完整'
      : qualityPassed
        ? experiment.comparison.decision === 'keep' ? '质量门禁通过' : '质量未回退，但候选已拒绝'
        : '质量门禁未通过，候选已拒绝';
    return receiptReady
      ? `${quality} · 账面成本回执标记已记录，价格身份由项目验收另行校验 · ${experiment.dataset.heldOutConsumed ? '最终盲测单独计入' : '最终盲测未使用'}`
      : `${quality} · 模型用量记录缺失，暂时不能比较成本`;
  }
  if (experiment.evaluationKind === 'rag_retrieval') return '相关性标准答案隐藏 · 引用门禁待验';
  const failures = candidate.failedToolCalls;
  const cleanup = candidate.temporaryDatabasesDeleted ?? candidate.databasesCleaned;
  const pieces: string[] = [];
  if (typeof failures === 'number') pieces.push(`工具失败 ${formatMetric(failures)}`);
  if (typeof cleanup === 'number') pieces.push(`临时库清理 ${formatMetric(cleanup)}/${formatMetric(cleanup)}`);
  if (!experiment.dataset.heldOutConsumed) pieces.push('最终盲测未使用');
  else pieces.push('最终盲测结果单独验收');
  return pieces.join(' · ') || '按当前实验合同验收';
}

function matrixEfficiency(experiment: EvalLabExperiment): string {
  if (experiment.vertical === 'paw-selfboot') return pawSelfbootEfficiency(experiment);
  const baseline = experiment.baseline.metrics;
  const candidate = experiment.candidate.metrics;
  if (isCloudOpsTranscriptOnlyFailure(experiment)) {
    const failedRunCostValue = firstMetric(candidate, ['apiCostUsd', 'estimatedApiCostUsd']);
    const failedRunCost = typeof failedRunCostValue === 'number'
      ? `失败运行 API 估算 $${failedRunCostValue.toFixed(4)}（无质量分，不算节省）`
      : '失败运行成本待补';
    return `输入 ${integerMetric(candidate.inputTokens)} tokens · 输出 ${integerMetric(candidate.outputTokens)} tokens · 复用缓存 ${integerMetric(candidate.cacheReadTokens)} tokens · 耗时 ${durationMetric(candidate.latencyMs)} · ${failedRunCost}`;
  }
  if (isCloudOpsRuntimeSelectionRepair(experiment)) {
    return `运行 ${durationMetric(candidate.elapsedMs)} · 没有产生模型用量记录，无法计价`;
  }
  if (experiment.evaluationKind === 'answer_evidence') return answerEvidenceEfficiencySummary(experiment);
  if (experiment.evaluationKind === 'model_cost') {
    const cost = hasMetric(baseline, 'apiCostUsd') && hasMetric(candidate, 'apiCostUsd')
      ? `账面 API 估算 $${baseline.apiCostUsd.toFixed(4)} → $${candidate.apiCostUsd.toFixed(4)}（未通过绑定价格回执门禁，不作为成本下降结论）`
      : '成本待补';
    return `工具调用 ${integerMetric(baseline.toolCalls)} → ${integerMetric(candidate.toolCalls)} · 延迟 ${durationMetricPair(baseline.latencyMs, candidate.latencyMs)} · ${cost}`;
  }
  if (experiment.evaluationKind === 'memory') {
    if (experiment.experimentId === 'memory.maintenance-observed-failure.v0') return '总耗时 13 分 54.9 秒 · 阶段耗时未知 · 模型用量未记录';
    if (hasMetric(candidate, 'actualModelCallElapsedMs')) return `模型调用 ${durationMetric(candidate.actualModelCallElapsedMs)} · 模型用量未记录 · 无法计算 API 成本`;
    return `端到端 p95 ${durationMetric(candidate.endToEndP95Ms)} · 工具调用 0`;
  }
  if (hasMetric(baseline, 'toolCalls') && hasMetric(candidate, 'toolCalls')) {
    const latency = hasMetric(baseline, 'latencyMs') || hasMetric(baseline, 'elapsedMs')
      ? durationMetricPair(baseline.latencyMs ?? baseline.elapsedMs, candidate.latencyMs ?? candidate.elapsedMs)
      : '耗时待补';
    return `工具调用 ${integerMetric(baseline.toolCalls)} → ${integerMetric(candidate.toolCalls)} · ${latency}`;
  }
  if (hasMetric(baseline, 'businessToolCalls') && hasMetric(candidate, 'businessToolCalls')) return `业务工具调用 ${integerMetric(baseline.businessToolCalls)} → ${integerMetric(candidate.businessToolCalls)}`;
  return '尚无可比效率数据';
}

function hasMetric(metrics: Readonly<Record<string, number>>, name: string): boolean {
  return typeof metrics[name] === 'number' && Number.isFinite(metrics[name]);
}

function firstMetric(metrics: Readonly<Record<string, number>>, names: readonly string[]): number | undefined {
  for (const name of names) {
    if (hasMetric(metrics, name)) return metrics[name];
  }
  return undefined;
}

function answerEvidenceRate(metrics: Readonly<Record<string, number>>, names: readonly string[]): number | undefined {
  return firstMetric(metrics, names);
}

function answerEvidenceCountPair(experiment: EvalLabExperiment): string | undefined {
  const counts = answerEvidenceCaseCounts(experiment, 'answerJudgeCorrect', 0);
  return counts ? answerEvidenceCaseCountLabel(counts) : answerEvidenceCoveragePair(
    experiment.baseline.metrics, experiment.candidate.metrics,
    ['answerSuccessRate', 'answerJudgeCorrectnessRate'], ['answerableCaseCount', 'answerableCases'],
    ['answerSuccessCount', 'answerJudgeCorrectCount'],
  );
}

type AnswerEvidenceCaseCounts = { before: number; after: number; denominator: number };

function answerEvidenceCaseCounts(
  experiment: EvalLabExperiment,
  metricName: string,
  abstentionExpected?: 0 | 1,
): AnswerEvidenceCaseCounts | undefined {
  const cases = experiment.optimizationEvidence?.caseComparisons ?? [];
  if (!cases.length || cases.length !== experiment.dataset.caseCount || new Set(cases.map((item) => item.caseId)).size !== cases.length) return undefined;
  const counts = { before: 0, after: 0, denominator: 0 };
  for (const item of cases) {
    if (abstentionExpected !== undefined) {
      const beforeClass = item.before.metrics.abstentionExpected;
      const afterClass = item.after.metrics.abstentionExpected;
      if ((beforeClass !== 0 && beforeClass !== 1) || beforeClass !== afterClass) return undefined;
      if (beforeClass !== abstentionExpected) continue;
    }
    const before = item.before.metrics[metricName];
    const after = item.after.metrics[metricName];
    if ((before !== 0 && before !== 1) || (after !== 0 && after !== 1)) return undefined;
    counts.before += before;
    counts.after += after;
    counts.denominator += 1;
  }
  return counts.denominator ? counts : undefined;
}

function answerEvidenceCaseCountLabel({ before, after, denominator }: AnswerEvidenceCaseCounts): string {
  return `${integerMetric(before)}/${integerMetric(denominator)} → ${integerMetric(after)}/${integerMetric(denominator)}`;
}

function answerEvidenceCoveragePair(
  baseline: Readonly<Record<string, number>>,
  candidate: Readonly<Record<string, number>>,
  rateNames: readonly string[],
  denominatorNames: readonly string[],
  countNames: readonly string[] = [],
  caseDenominator?: number,
): string | undefined {
  const before = answerEvidenceRate(baseline, rateNames);
  const after = answerEvidenceRate(candidate, rateNames);
  const beforeDenominator = firstMetric(baseline, denominatorNames) ?? caseDenominator;
  const afterDenominator = firstMetric(candidate, denominatorNames) ?? caseDenominator;
  const beforeCount = firstMetric(baseline, countNames) ?? (before === undefined || beforeDenominator === undefined ? undefined : before * beforeDenominator);
  const afterCount = firstMetric(candidate, countNames) ?? (after === undefined || afterDenominator === undefined ? undefined : after * afterDenominator);
  const hasExactCount = (count: number | undefined, denominator: number | undefined): boolean => count !== undefined
    && denominator !== undefined && Number.isInteger(denominator) && denominator > 0
    && count >= 0 && count <= denominator && Math.abs(count - Math.round(count)) < 1e-6;
  if (hasExactCount(beforeCount, beforeDenominator) && hasExactCount(afterCount, afterDenominator)) {
    return `${integerMetric(Math.round(beforeCount!))}/${integerMetric(beforeDenominator)} → ${integerMetric(Math.round(afterCount!))}/${integerMetric(afterDenominator)}`;
  }
  if (before === undefined || after === undefined) return undefined;
  return ratePair(before, after);
}

function answerEvidenceQualitySummary(experiment: EvalLabExperiment): string {
  const baseline = experiment.baseline.metrics;
  const candidate = experiment.candidate.metrics;
  const taskCounts = answerEvidenceCaseCounts(experiment, 'agentSuccess');
  const answerCounts = answerEvidenceCaseCounts(experiment, 'answerJudgeCorrect', 0);
  const abstentionCounts = answerEvidenceCaseCounts(experiment, 'abstentionCorrect', 1);
  const tasks = taskCounts ? answerEvidenceCaseCountLabel(taskCounts) : answerEvidenceCoveragePair(
    baseline, candidate, ['agentSuccessRate', 'taskSuccessRate'], ['taskCaseCount', 'protocolCaseCount'], ['agentSuccessCount', 'taskSuccessCount'],
  );
  const answers = answerEvidenceCountPair(experiment);
  const facts = answerEvidenceCoveragePair(baseline, candidate, ['highLevelFactCoverage', 'factCoverage'], ['highLevelFactCount']);
  const citations = answerEvidenceCoveragePair(
    baseline, candidate, ['exactCitationFactCoverage', 'citationFactCoverage'],
    ['citationFactCount', 'verifiedRequiredFacts', 'verifiedRequiredFactCount', 'requiredFactCount'], ['exactCitationFactsCovered'],
  );
  const answerableCitations = answerEvidenceCoveragePair(
    baseline, candidate, ['answerableCitationSupportRate', 'citationSupportRate'], ['answerableCases', 'answerableCaseCount'], [], answerCounts?.denominator,
  );
  const abstentions = abstentionCounts ? answerEvidenceCaseCountLabel(abstentionCounts) : answerEvidenceCoveragePair(
    baseline, candidate, ['infoNotFoundAbstentionRecall'], ['infoNotFoundCaseCount'],
  );
  const parts = [
    tasks ? `任务通过 ${tasks}` : '',
    answers ? `可回答题答案正确 ${answers}` : '',
    facts ? `高层事实覆盖 ${facts}` : '',
    citations ? `引用事实覆盖 ${citations}` : '',
    answerableCitations ? `可回答问题引用支持 ${answerableCitations}` : '',
    abstentions ? `应拒答问题拒答 ${abstentions}` : '',
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : '答案与引用指标待补';
}

function binaryGateLabel(value: number): string {
  return value > 0 ? '通过' : '未通过';
}

function answerEvidenceGatePair(
  baseline: Readonly<Record<string, number>>,
  candidate: Readonly<Record<string, number>>,
  names: readonly string[],
): string | undefined {
  const before = firstMetric(baseline, names);
  const after = firstMetric(candidate, names);
  return before === undefined || after === undefined ? undefined : `${binaryGateLabel(before)} → ${binaryGateLabel(after)}`;
}

function answerEvidenceReliabilitySummary(experiment: EvalLabExperiment): string {
  const baseline = experiment.baseline.metrics;
  const candidate = experiment.candidate.metrics;
  const gate = answerEvidenceGatePair(baseline, candidate, ['citationHardGatePassed', 'citationGatePassed', 'formalAcceptancePassed']);
  const protocol = ratePair(
    answerEvidenceRate(baseline, ['outputProtocolRate']),
    answerEvidenceRate(candidate, ['outputProtocolRate']),
  );
  const parts = [
    gate ? `引用门禁 ${gate}` : '引用/终态门禁待验',
    protocol !== '待补' ? `输出协议 ${protocol}` : '',
  ].filter(Boolean);
  return parts.join(' · ');
}

function answerEvidenceTokenCount(metrics: Readonly<Record<string, number>>): number | undefined {
  if (metrics.tokensComplete === 0 || metrics.agenticTokensComplete === 0) return undefined;
  const direct = firstMetric(metrics, ['tokens', 'totalTokens']);
  if (direct !== undefined) return direct;
  const input = firstMetric(metrics, ['inputTokens', 'input_tokens']);
  const output = firstMetric(metrics, ['outputTokens', 'output_tokens']);
  return input === undefined && output === undefined ? undefined : (input ?? 0) + (output ?? 0);
}

function answerEvidenceEfficiencySummary(experiment: EvalLabExperiment): string {
  const baseline = experiment.baseline.metrics;
  const candidate = experiment.candidate.metrics;
  const cost = answerEvidenceCostSummary(experiment);
  const tokens = `${answerEvidenceTokenCount(baseline) !== undefined && answerEvidenceTokenCount(candidate) !== undefined ? `Token ${integerMetric(answerEvidenceTokenCount(baseline))} → ${integerMetric(answerEvidenceTokenCount(candidate))}` : ''}`;
  const toolBefore = firstMetric(baseline, ['toolCalls', 'businessToolCalls']);
  const toolAfter = firstMetric(candidate, ['toolCalls', 'businessToolCalls']);
  const tool = toolBefore !== undefined && toolAfter !== undefined ? `Tool ${integerMetric(toolBefore)} → ${integerMetric(toolAfter)}` : '';
  const latency = durationMetricPair(
    firstMetric(baseline, ['latencyMs', 'elapsedMs']),
    firstMetric(candidate, ['latencyMs', 'elapsedMs']),
  );
  const parts = [cost, tokens, tool, latency === '耗时待补' ? '' : `耗时 ${latency}`].filter(Boolean);
  return parts.length ? parts.join(' · ') : '尚无可比效率数据';
}

function answerEvidenceCostSummary(experiment: EvalLabExperiment): string | undefined {
  const before = firstMetric(experiment.baseline.metrics, ['apiCostUsd', 'estimatedApiCostUsd']);
  const after = firstMetric(experiment.candidate.metrics, ['apiCostUsd', 'estimatedApiCostUsd']);
  if (before === undefined || after === undefined || before < 0 || after < 0) return undefined;
  const costScope = experiment.frozenControls.find((control) => control.name === 'cost_scope')?.value.split(';')[0].trim();
  const details = [
    experiment.optimizationEvidence?.validationBoundary.costAuthority === 'runtime_cost_reconciled' ? 'Runtime 对账' : '',
    costScope === 'full four-lane run plus frozen Judge' ? '完整四条 lane + 冻结 Judge' : costScope,
    experiment.baseline.metrics.providerBillAvailable === 0 && experiment.candidate.metrics.providerBillAvailable === 0 ? '非 Provider 账单' : '',
  ].filter(Boolean);
  return `API 估算 $${before.toFixed(4)} → $${after.toFixed(4)}${details.length ? `（${details.join('；')}）` : ''}`;
}

function ratePair(before: number | undefined, after: number | undefined): string {
  if (typeof before !== 'number' || typeof after !== 'number') return '待补';
  return `${percent(before)} → ${percent(after)}`;
}

function integerMetric(value: number | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? formatMetric(value) : '待补';
}

function durationMetric(value: number | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '待补';
  return value < 1_000 ? `${Math.round(value)} ms` : duration(value);
}

function durationMetricPair(before: number | undefined, after: number | undefined): string {
  if (typeof before !== 'number' || typeof after !== 'number') return '耗时待补';
  return `${duration(before)} → ${duration(after)}`;
}

type CandidateEffect = 'improved' | 'regressed' | 'neutral' | 'unverified' | 'not_run' | 'runtime_failed' | 'scoring_recovered' | 'observed_failure' | 'partial';

function candidateEffect(experiment: EvalLabExperiment): CandidateEffect {
  if (isCloudOpsTranscriptOnlyFailure(experiment)) return 'runtime_failed';
  if (isCloudOpsRuntimeSelectionRepair(experiment)) return 'partial';
  if (experiment.experimentId === 'cloudops.validation-baseline.v1') return 'scoring_recovered';
  if (experiment.experimentId === 'memory.maintenance-observed-failure.v0') return 'observed_failure';
  if (['memory.maintenance-shadow-v1', 'memory.maintenance-shadow-v3', 'memory.maintenance-shadow-v4'].includes(experiment.experimentId)) return 'partial';
  if (experiment.effectStatus) return experiment.effectStatus;
  if (experiment.status === 'kept') return 'improved';
  if (experiment.status === 'rejected') return 'regressed';
  if (experiment.status === 'open_gap') return 'not_run';
  const hasDelta = experiment.comparison.metricDeltas.length > 0;
  return hasDelta ? 'unverified' : 'neutral';
}

function effectStatusLabel(status: CandidateEffect): string {
  if (status === 'improved') return '效果变好';
  if (status === 'regressed') return '效果变差';
  if (status === 'runtime_failed') return '运行失败';
  if (status === 'scoring_recovered') return '可以正常验收';
  if (status === 'observed_failure') return '原始失败记录';
  if (status === 'partial') return '部分通过';
  if (status === 'neutral') return '效果持平';
  if (status === 'not_run') return '尚未运行';
  return '还不能下结论';
}

function experimentTitle(title: string): string {
  if (title.includes('·')) return title;
  const normalized = title.toLocaleLowerCase();
  if (normalized.includes('model cost') || normalized.includes('sol versus luna')) return 'EnterpriseOps · Sol / Luna 成本门禁';
  if (normalized.includes('cloudops') && normalized.includes('luna')) return 'CloudOps · Luna Max 运行失败';
  if (normalized.includes('cloudops')) return 'CloudOps · 证据工作流与拒答';
  if (normalized.includes('enterprise rag')) return 'Enterprise RAG · 检索方案选择';
  if (normalized.includes('execution-chain')) return 'EnterpriseOps · 执行链修复';
  if (normalized.includes('state-contract')) return 'EnterpriseOps · 状态合同工作流';
  if (normalized.includes('trace agent')) return 'Trace Agent · 跨 Session 闭环';
  if (normalized.includes('memory') || normalized.includes('personal rag')) return 'Memory · 个人知识召回';
  return title;
}

function experimentTypeLabel(experiment: EvalLabExperiment): string {
  if (experiment.candidateType === 'single_factor') return '单变量对照';
  if (experiment.candidateType === 'compound_repair') return '多处联合修复';
  if (experiment.candidateType === 'baseline') return experiment.experimentId.includes('observed-failure') ? '原始失败记录' : '起始版本';
  return '历史组合结果';
}

function metricLabel(name: string): string {
  if (PAW_METRIC_LABELS[name]) return PAW_METRIC_LABELS[name];
  return ({
    abstentionCorrect: '拒答判断',
    abstentionExpected: '本题要求',
    agentSuccess: '任务验收',
    answerJudgeCorrect: '答案评审',
    toolSuccess: '工具执行',
    taskSuccessRate: '任务完成',
    taskSuccess: '任务完成',
    taskSuccessCount: '完成任务数',
    taskCount: '任务总数',
    verifierPassRate: '验收条件通过率',
    verifierPass: '验收条件通过率',
    verifierPassCount: '通过的验收条件',
    verifierCount: '验收条件总数',
    failedToolCalls: '工具失败',
    toolCalls: '工具调用',
    fa: '故障类型命中（FA）',
    transcriptToolCalls: '运行记录中的工具调用',
    failedTranscriptToolCalls: '运行记录中的工具失败',
    sessionCount: 'Session 数',
    transcriptCount: 'Transcript 数',
    latencyMs: '总耗时',
    apiCostUsd: 'API 成本',
    estimatedApiCostUsd: 'API 成本估算',
    providerBillAvailable: 'Provider 账单',
    allDatabasesCleaned: '临时数据清理',
    temporaryDatabasesDeleted: '临时数据库清理',
    databasesCleaned: '临时数据库清理',
    businessToolCalls: '业务工具调用',
    answerCoverage: '回答覆盖',
    answerJudgeCorrectnessRate: '答案 Judge 正确率',
    answerSuccessRate: '答案通过率',
    highLevelFactCoverage: '高层事实覆盖',
    factCoverage: '事实覆盖',
    citationFactCoverage: '引用事实覆盖',
    answerableCitationSupportRate: '可回答引用支持',
    citationSupportRate: '引用支持率',
    infoNotFoundAbstentionRecall: '无信息拒答召回',
    abstentionAccuracy: '拒答准确率',
    abstentionF1: '拒答 F1',
    citationResolutionRate: '引用解析率',
    citationHardGatePassed: '引用门禁',
    citationGatePassed: '引用门禁',
    terminalCompletionPassed: '终态完成门禁',
    toolContractPassed: '工具协议验收',
    toolSuccessRate: '工具成功率',
    outputProtocolRate: '输出协议通过率',
    ca: '故障组件命中（CA）',
    jra: '联合根因命中（JRA）',
    top3Jra: '前三项联合命中',
    top3_jra: '前三项联合命中',
    realPredictionCases: '真实预测案例',
    publicHistoricalCasesPrepared: '已准备历史案例',
    scorerContractTestsPassed: 'Scorer 合同测试',
    reviewed: '独立复核数',
    passed: '通过数',
    behaviorCases: '行为 Case',
    repeats: '重复轮次',
    traces: 'Trace 数',
    costReceiptAvailable: '成本回执',
    directRank1Cases: '直接命中数',
    endToEndP95Ms: '端到端 P95',
    predictionCases: '预测案例',
    publicCases: '公开案例',
    totalTokens: '总 Token',
    providerCalls: '模型请求数',
    tokens: 'Token',
    inputTokens: '输入 Token',
    outputTokens: '输出 Token',
    cacheReadTokens: '缓存读取 Token',
    thirdBatchTimeout: '第三批 timeout',
    abortTimeout: 'Abort timeout',
    hostFormalCaJraAvailable: 'Host formal CA/JRA',
    sourceLocal: '仅本机',
    localOnly: '仅本机',
    installed: '已安装',
    candidateRuntimeInstalled: '候选 Runtime 已安装',
    heldOutConsumed: '最终盲测是否使用',
    agentCaseCount: 'Agent 任务数',
    agentSuccessRate: 'Agent 任务通过率',
    answerCaseCount: '答案任务数',
    answerSuccessCount: '答案通过数',
    verifiedRequiredFacts: '已核验必要事实数',
    verifiedRequiredFactCount: '已核验必要事实数',
    highLevelFactCount: '必要事实总数',
    infoNotFoundCaseCount: '应拒答案例数',
    curationCases: '记忆判断总数',
    curationPassed: '记忆判断通过数',
    durableRecallPassed: '长期记忆召回通过数',
    durableRecallTotal: '长期记忆召回总数',
    abstentionPassed: '临时信息拒记通过数',
    abstentionTotal: '临时信息拒记总数',
    vectorCoverage: '向量可检索覆盖',
    rollbackPassed: '回滚验证',
    replayPassed: '重复运行验证',
    receiptJsonValid: 'JSON 结果可解析',
    jsonReceiptValid: 'JSON 结果可解析',
    actualModelCallElapsedMs: '模型调用耗时',
    modelTokensAvailable: '模型 Token 回执',
    answerableCaseCount: '可回答案例数',
    tokensComplete: 'Token 回执完整',
    agenticTokensComplete: 'Agentic Token 回执完整',
    cachedInputTokens: '缓存输入 Token',
    failedTasks: '失败任务数',
    completedTasks: '完成任务数',
    answerableCases: '可回答案例',
    abstentionCases: '拒答案例',
    recallAt10: 'Recall@10',
    recall_at_10: 'Recall@10',
    mrr: 'MRR',
    ndcgAt10: 'nDCG@10',
    ndcg_at_10: 'nDCG@10',
    tool_calls: '工具调用',
    failed_tool_calls: '工具失败',
    elapsed_ms: '总耗时',
    verifier_pass_rate: '验收条件通过率',
    task_success_rate: '任务完成',
    verifier_pass: '验收条件通过率',
    task_success: '任务完成',
    direct_rank1_cases: '直接命中数',
    end_to_end_p95_ms: '端到端 P95',
    business_tool_calls: '业务工具调用',
    temporary_databases_deleted: '临时数据库清理',
    databases_cleaned: '临时数据库清理',
    cost_receipt_available: '成本回执',
  } as Record<string, string>)[name] ?? name;
}

function formatMetricByName(name: string, value: number): string {
  if (name === 'abstentionExpected') return value === 1 ? '应拒答' : value === 0 ? '应回答' : '未知';
  if (['abstentionCorrect', 'agentSuccess', 'answerJudgeCorrect', 'toolSuccess'].includes(name)) return value === 1 ? '通过' : value === 0 ? '未通过' : '未知';
  if (name === 'costReceiptAvailable' || name === 'cost_receipt_available') return value > 0 ? '已提供' : '缺失';
  if (name === 'hostFormalCaJraAvailable') return value > 0 ? '已运行' : '未运行';
  if (name === 'thirdBatchTimeout' || name === 'abortTimeout') return value > 0 ? '发生' : '未发生';
  if (name === 'thirdBatchTimeout' || name === 'abortTimeout' || name === 'sourceLocal' || name === 'localOnly' || name === 'installed' || name === 'candidateRuntimeInstalled' || name === 'heldOutConsumed' || name === 'tokensComplete' || name === 'agenticTokensComplete' || name === 'citationHardGatePassed' || name === 'citationGatePassed' || name === 'terminalCompletionPassed' || name === 'toolContractPassed' || name === 'rollbackPassed' || name === 'replayPassed' || name === 'receiptJsonValid' || name === 'jsonReceiptValid' || name === 'modelTokensAvailable') {
    if (name === 'citationHardGatePassed' || name === 'citationGatePassed' || name === 'terminalCompletionPassed' || name === 'toolContractPassed') return value > 0 ? '通过' : '未通过';
    if (name === 'sourceLocal' || name === 'localOnly') return value > 0 ? '是' : '否';
    if (name === 'installed' || name === 'candidateRuntimeInstalled') return value > 0 ? '是' : '否';
    if (name === 'heldOutConsumed') return value > 0 ? '已使用' : '未使用';
    if (name === 'rollbackPassed' || name === 'replayPassed' || name === 'receiptJsonValid' || name === 'jsonReceiptValid') return value > 0 ? '通过' : '未通过';
    if (name === 'modelTokensAvailable') return value > 0 ? '已记录' : '未记录';
    return value > 0 ? '完整' : '不完整';
  }
  if (name.endsWith('Rate') || name.endsWith('_rate') || name === 'taskSuccess' || name === 'task_success' || name === 'verifierPass' || name === 'allDatabasesCleaned' || name === 'answerCoverage' || name === 'answerJudgeCorrectnessRate' || name === 'answerSuccessRate' || name === 'highLevelFactCoverage' || name === 'factCoverage' || name === 'citationFactCoverage' || name === 'answerableCitationSupportRate' || name === 'citationSupportRate' || name === 'infoNotFoundAbstentionRecall' || name === 'abstentionAccuracy' || name === 'abstentionF1' || name === 'citationResolutionRate' || name === 'toolSuccessRate' || name === 'outputProtocolRate' || name === 'fa' || name === 'ca' || name === 'jra' || name === 'top3Jra' || name === 'top3_jra' || name === 'recallAt10' || name === 'recall_at_10' || name === 'mrr' || name === 'ndcgAt10' || name === 'ndcg_at_10') return percent(value);
  if (name === 'latencyMs' || name === 'elapsedMs' || name === 'latency_ms' || name === 'elapsed_ms' || name === 'endToEndP95Ms' || name === 'end_to_end_p95_ms' || name === 'actualModelCallElapsedMs') return durationMetric(value);
  if (name === 'apiCostUsd' || name === 'estimatedApiCostUsd') return `$${value.toFixed(4)}`;
  if (name === 'providerBillAvailable') return value > 0 ? '已提供' : '未提供';
  return formatMetric(value);
}

function ExperimentSection({ desktop, evidenceCatalog, experiment, linkedRuns, onOpenRoom, rooms }: {
  desktop: ReturnType<typeof usePawOsDesktop>;
  experiment: EvalLabExperiment;
  linkedRuns: EvalLabRun[];
  evidenceCatalog?: EvalLabEvidenceResponse;
  onOpenRoom: (roomId: string) => void;
  rooms: RoomSummary[];
}) {
  const factors = experiment.factors ?? [];
  const frozenControls = experiment.frozenControls ?? [];
  const datasetExplanation = experimentDatasetExplanation(experiment);

  function downloadAudit(): void {
    const matches = matchingEvidenceRuns(evidenceCatalog, experiment);
    const download = createEvalLabExperimentAuditDownload(experiment, {
      osOrigin: auditOriginLabel(matches),
      evidence: matches.map((run) => ({
        runId: run.runId,
        relation: evidenceRelation(experiment, run),
        title: run.title,
        status: run.status,
        evidenceKind: run.evidenceKind,
        summary: `${evidenceAvailabilitySummary(run)}；${runEvidenceLabel(run)}`,
        refs: [
          ...(run.reportSha256 ? [`report:${run.reportSha256}`] : []),
          ...(run.databaseSha256 ? [`database:${run.databaseSha256}`] : []),
          ...((run.artifacts ?? []).filter((item) => item.available && item.sha256).map((item) => `${item.kind}:${item.sha256}`)),
        ],
        osOrigin: evidenceOriginLabel(run),
      })),
      traces: matches.flatMap((run) => (run.environment.traceIds ?? []).map((traceId) => ({
        traceId,
        status: run.status,
        summary: `${run.title} 的公开运行回执绑定了这条 Trace；私有 Trace 正文不写入导出文件。`,
        evidenceRefs: run.reportSha256 ? [`report:${run.reportSha256}`] : [],
      }))),
    });
    const anchor = document.createElement('a');
    anchor.href = download.href;
    anchor.download = download.download;
    anchor.click();
    window.setTimeout(() => download.revoke(), 0);
  }
  return (
    <article className="eval-lab__experiment">
      <header className="eval-lab__experiment-header">
        <div>
          <div className="eval-lab__run-heading">
            <h2>{candidateLabel(experiment)}</h2>
            <span className={`eval-lab__status eval-lab__status--${experiment.status}`}>{statusLabel(experiment.status)}</span>
            <span className="eval-lab__lane">{splitLabel(experiment.dataset.split)}</span>
            {isHistoricalExperiment(experiment) ? <span className="eval-lab__history-badge">历史记录 · 不参与当前结论</span> : null}
          </div>
          <p>{projectTitleForExperiment(experiment)} · {publicDatasetSummary(experiment)}</p>
        </div>
        <div className="eval-lab__run-actions">
          <Button leadingIcon={<Download size={15} />} onClick={downloadAudit} variant="secondary">导出报告</Button>
          <span className="eval-lab__read-only"><ShieldCheck size={14} /> 证据只读</span>
        </div>
      </header>

      <ExperimentOutcomeSummary experiment={experiment} />
      {experiment.vertical === 'paw-selfboot' ? <PawSelfbootResultsChart experiment={experiment} /> : null}
      <ExperimentRecordBrowser evidenceCatalog={evidenceCatalog} experiment={experiment} linkedRuns={linkedRuns} />
      <RoomReviewEvidence onOpenRoom={onOpenRoom} rooms={rooms} />
      <div className="eval-lab__experiment-context">
        <div><span>为什么需要 Agent</span><strong>{humanClaimText(experiment.whyAgent)}</strong></div>
        <div><span>本轮用了哪些题</span><strong>{datasetSummary(experiment.dataset)} · {experiment.dataset.heldOutConsumed ? '最终盲测已经使用' : '最终盲测没有使用'}</strong></div>
        <div><span>怎样判断做对</span><strong>{metricLabel(experiment.scoring.primaryMetric)} · {experiment.scoring.goldHiddenFromAgent ? '验收答案对 Agent 隐藏' : '验收答案可见'}</strong></div>
      </div>
      <section aria-label="数据集与金标准" className="eval-lab__dataset-story">
        <header><h3>数据从哪里来，怎样判定对错</h3><p>先固定输入和标准答案，再允许 Agent 看任务；评测器与标准答案不进入 Agent 上下文。</p></header>
        <dl>
          <div><dt>原始数据</dt><dd>{datasetExplanation.source}</dd></div>
          <div><dt>怎样处理</dt><dd>{datasetExplanation.preparation}</dd></div>
          <div><dt>金标准</dt><dd>{datasetExplanation.gold}</dd></div>
          <div><dt>Agent 必须遵守</dt><dd>{datasetExplanation.agentContract}</dd></div>
          <div><dt>能说明什么</dt><dd>{datasetExplanation.boundary}</dd></div>
        </dl>
      </section>
      <section aria-label="实验变量与冻结控制" className="eval-lab__controls">
        <div className="eval-lab__control-panel">
          <h3>实验变量（可改）</h3>
          <ul>
            {factors.length ? factors.map((factor) => (
              <li key={factor.name}>
                <strong>{factorLabel(factor.name)}</strong>
                <span>{humanClaimText(factor.before)} → {humanClaimText(factor.after)}</span>
                <small>{humanClaimText(factor.reason)}</small>
              </li>
            )) : <li><span>旧版回执未记录变量投影。</span></li>}
          </ul>
        </div>
        <div className="eval-lab__control-panel eval-lab__control-panel--frozen">
          <h3>冻结控制（不可改）</h3>
          <ul>
            {frozenControls.length ? frozenControls.map((control) => (
              <li key={control.name}>
                <strong>{controlLabel(control.name)}</strong>
                <span>{humanClaimText(control.value)}</span>
                <small>{humanClaimText(control.reason)}</small>
              </li>
            )) : <li><span>旧版回执未记录冻结控制。</span></li>}
          </ul>
        </div>
      </section>
      <div className="eval-lab__boundary"><CheckCircle2 size={15} /><span>{humanClaimText(experiment.claim.forbidden)}</span></div>

      <div className="eval-lab__experiment-comparison">
        <div>
          <h3>原方案结果</h3>
          <MetricList metrics={experiment.baseline.metrics} />
        </div>
        <div>
          <h3>新方案结果</h3>
          <MetricList metrics={experiment.candidate.metrics} />
        </div>
        <div className="eval-lab__delta-panel">
          <h3>相比原方案</h3>
          {experiment.comparison.metricDeltas.length ? <ul>{experiment.comparison.metricDeltas.map((delta) => <li key={delta.metric}><span>{metricLabel(delta.metric)}</span><strong>{formatMetricByName(delta.metric, delta.delta)}</strong></li>)}</ul> : <p>暂无可比较的指标变化。</p>}
        </div>
      </div>

      <OutputComparison baseline={experiment.baseline} candidate={experiment.candidate} comparisons={experiment.comparison.outputComparisons} />

      <div className="eval-lab__claim-grid">
        <div><h3>本次结论</h3><p>{humanClaimText(experiment.claim.allowed)}</p><p className="eval-lab__scope-note">只代表本轮{splitLabel(experiment.dataset.split)}。</p></div>
        <div><h3>使用边界</h3><p>{humanClaimText(experiment.claim.forbidden)}</p></div>
      </div>
      <details className="eval-lab__star">
        <summary>查看过程复盘</summary>
        <dl>
          <div><dt>背景</dt><dd>{humanClaimText(experiment.star.situation)}</dd></div>
          <div><dt>任务</dt><dd>{humanClaimText(experiment.star.task)}</dd></div>
          <div><dt>做法</dt><dd>{humanClaimText(experiment.star.action)}</dd></div>
          <div><dt>结果</dt><dd>{humanClaimText(experiment.star.result)}</dd></div>
        </dl>
      </details>
      {experiment.openGaps.length ? <div className="eval-lab__gaps"><h3>仍待补齐</h3><ul>{experiment.openGaps.map((gap) => <li key={gap}>{humanClaimText(gap)}</li>)}</ul></div> : null}

      <section aria-label={`${experiment.title} 的真实 Session`} className="eval-lab__linked-runs">
        <h3>真实 Session 记录</h3>
        {linkedRuns.length
          ? linkedRuns.map((run) => <LinkedSessionRun desktop={desktop} key={run.runId} run={run} />)
          : hasReportOnlyExperimentEvidence(evidenceCatalog, experiment)
            ? <p>有汇总回执，原始对话未公开；请在下方“查看原始证据”打开逐 case/报告。</p>
            : <p>尚未找到与 baseline/candidate runId 匹配的真实 Session。</p>}
      </section>
      <ExperimentEvidenceLinks catalog={evidenceCatalog} experiment={experiment} />
    </article>
  );
}

const EXPERIMENT_RECORD_VIEWS = [
  ['task', '任务定义'],
  ['dataset', '数据集 Cases'],
  ['baseline', '原结果'],
  ['optimization', '优化'],
  ['candidate', '新结果'],
] as const satisfies readonly (readonly [ExperimentRecordView, string])[];

function ExperimentRecordBrowser({ evidenceCatalog, experiment, linkedRuns }: {
  evidenceCatalog?: EvalLabEvidenceResponse;
  experiment: EvalLabExperiment;
  linkedRuns: readonly EvalLabRun[];
}) {
  const [view, setView] = useState<ExperimentRecordView>('task');
  const datasetExplanation = experimentDatasetExplanation(experiment);
  const evidenceRuns = matchingEvidenceRuns(evidenceCatalog, experiment);
  return (
    <section aria-label="任务、数据集与优化记录" className="eval-lab__record-browser">
      <header>
        <div><span>本轮档案</span><h3>从任务一路核对到新结果</h3></div>
        <p>所有内容都在 Agent Lab 内查看；本机文件夹只作为高级用户的次级入口。</p>
      </header>
      <nav aria-label="选择实验档案" className="eval-lab__record-tabs" role="tablist">
        {EXPERIMENT_RECORD_VIEWS.map(([key, label], index) => (
          <button
            aria-selected={view === key}
            key={key}
            onClick={() => setView(key)}
            onKeyDown={(event) => moveTabbedSelection(event, index, EXPERIMENT_RECORD_VIEWS.map(([value]) => value), setView)}
            role="tab"
            type="button"
          >{label}</button>
        ))}
      </nav>
      <div className="eval-lab__record-panel" role="tabpanel">
        {view === 'task' ? (
          <dl>
            <div><dt>业务问题</dt><dd>{humanClaimText(experiment.businessProblem)}</dd></div>
            <div><dt>本轮任务</dt><dd>{humanClaimText(experiment.star.task)}</dd></div>
            <div><dt>为什么使用 Agent</dt><dd>{humanClaimText(experiment.whyAgent)}</dd></div>
            <div><dt>成功定义</dt><dd>{metricLabel(experiment.scoring.primaryMetric)}；{experiment.scoring.hardGates.map(humanClaimText).join('；') || '未记录硬门禁'}</dd></div>
            <div><dt>冻结条件</dt><dd>{experiment.frozenControls.map((control) => `${controlLabel(control.name)}：${humanClaimText(control.value)}`).join('；') || '未记录冻结条件'}</dd></div>
          </dl>
        ) : null}
        {view === 'dataset' ? (
          <div className="eval-lab__record-dataset">
            <dl>
              <div><dt>数据集</dt><dd>{experiment.dataset.datasetId}</dd></div>
              <div><dt>范围</dt><dd>{splitLabel(experiment.dataset.split)} · {datasetSummary(experiment.dataset)}</dd></div>
              <div><dt>Manifest</dt><dd><code>{experiment.dataset.manifestSha256.slice(0, 16)}</code></dd></div>
              <div><dt>原始数据</dt><dd>{datasetExplanation.source}</dd></div>
              <div><dt>构造方法</dt><dd>{datasetExplanation.preparation}</dd></div>
              <div><dt>Golden Data</dt><dd>{datasetExplanation.gold}</dd></div>
              <div><dt>评测权威</dt><dd>{humanClaimText(experiment.scoring.evaluatorAuthority)}</dd></div>
              <div><dt>泄漏边界</dt><dd>{datasetLeakBoundary(experiment)}</dd></div>
              <div><dt>负例 / 拒答</dt><dd>{datasetNegativeContract(experiment)}</dd></div>
            </dl>
            <ExperimentDatasetCases evidenceRuns={evidenceRuns} experiment={experiment} />
          </div>
        ) : null}
        {view === 'baseline' ? <ExperimentResultRecord experiment={experiment} kind="baseline" evidenceRuns={evidenceRuns} /> : null}
        {view === 'optimization' ? <div className="eval-lab__optimization-record">
          <LinkedOptimizationWorkbench evidenceRuns={evidenceRuns} experiment={experiment} linkedRuns={linkedRuns} />
          <ExperimentChangeRecord evidenceRuns={evidenceRuns} experiment={experiment} />
        </div> : null}
        {view === 'candidate' ? <ExperimentResultRecord experiment={experiment} kind="candidate" evidenceRuns={evidenceRuns} /> : null}
      </div>
    </section>
  );
}

function ExperimentDatasetCases({ evidenceRuns, experiment }: {
  evidenceRuns: readonly EvalLabEvidenceRun[];
  experiment: EvalLabExperiment;
}) {
  const [selected, setSelected] = useState<{ runId: string; task: EvalLabEvidenceTask }>();
  if (experiment.optimizationEvidence?.caseComparisons.length) return <ExperimentPairedCases evidenceRuns={evidenceRuns} experiment={experiment} />;
  const preferredRuns = [...evidenceRuns].sort((left, right) => {
    const relationDelta = Number(evidenceRelation(experiment, right) === 'candidate') - Number(evidenceRelation(experiment, left) === 'candidate');
    return relationDelta || right.updatedAtMs - left.updatedAtMs;
  });
  const cases = new Map<string, { run: EvalLabEvidenceRun; task: EvalLabEvidenceTask }>();
  for (const run of preferredRuns) {
    for (const task of run.tasks ?? []) {
      const key = task.taskLabel.trim() || String(task.taskIndex);
      if (!cases.has(key)) cases.set(key, { run, task });
    }
  }
  const rows = [...cases.values()].slice(0, experiment.dataset.caseCount);
  return (
    <section aria-label="数据集 Case 浏览器" className="eval-lab__case-browser">
      <header><div><strong>Case 浏览器</strong><span>点击后仍在本页查看输入、输出、Trace、环境和验收。</span></div><em>{rows.length ? `${rows.length}/${experiment.dataset.caseCount} 条公开证据` : 'Case 正文未公开'}</em></header>
      {rows.length ? <ol>{rows.map(({ run, task }) => {
        const active = selected?.runId === run.runId && selected.task.taskIndex === task.taskIndex;
        return <li key={`${run.runId}:${task.taskIndex}`}>
          <div><strong>{task.taskLabel}</strong><span>{evidenceRelationLabel(experiment, run)} · {task.taskSucceeded === true ? '任务通过' : task.taskSucceeded === false ? '需要复核' : '结果未记录'} · {task.transcriptAvailable ? `${task.toolCalls} 次 Tool` : evidenceTaskIsReportOnly(run, task) ? '报告证据' : 'Transcript 缺失'}</span></div>
          <button onClick={() => setSelected(active ? undefined : { runId: run.runId, task })} type="button">{active ? '收起 Case' : '查看 Case'}</button>
          {active ? <EvidencePanel runId={run.runId} taskIndex={evidenceTaskIndex(run, task)} fallbackTask={task} initialTab={evidenceTaskIsReportOnly(run, task) ? 'report' : 'task'} /> : null}
        </li>;
      })}</ol> : <p>当前只公开了数据集规模、构造方法、Golden Data 与校验码，没有公开逐 Case 正文；App 不会用示例内容冒充真实数据。</p>}
    </section>
  );
}

function ExperimentResultRecord({ evidenceRuns, experiment, kind }: {
  evidenceRuns: readonly EvalLabEvidenceRun[];
  experiment: EvalLabExperiment;
  kind: 'baseline' | 'candidate';
}) {
  const result = experiment[kind];
  const runs = evidenceRuns.filter((run) => evidenceRelation(experiment, run) === kind);
  return (
    <div className="eval-lab__result-record">
      <div className="eval-lab__result-verdicts">
        <div><span>任务结果</span><strong>{matrixQuality(experiment)}</strong></div>
        <div><span>发布 / 可靠性门禁</span><strong>{matrixReliability(experiment)}</strong></div>
        <div><span>成本与耗时观察</span><strong>{matrixEfficiency(experiment)}</strong></div>
      </div>
      <MetricList metrics={result.metrics} />
      <footer><span>Run ID</span><code>{result.runId}</code><span>{runs.length ? `${runs.length} 个证据批次已绑定` : '没有匹配的公开运行证据'}</span></footer>
    </div>
  );
}

type ExperimentCaseDiff = {
  caseId: string;
  result: string;
  gates: string;
  usage: string;
};

function evidenceTaskKey(task: EvalLabEvidenceTask): string {
  return task.taskLabel.trim() || `case-${task.taskIndex}`;
}

function evidenceTaskResult(task: EvalLabEvidenceTask): string {
  if (task.taskSucceeded === true) return '成功';
  if (task.taskSucceeded === false) return '失败';
  return task.terminalEvent || '未记录';
}

function evidenceTaskGates(task: EvalLabEvidenceTask): string {
  const parts: string[] = [];
  if (typeof task.verifierPassed === 'number' && typeof task.verifierTotal === 'number') {
    parts.push(`验收 ${task.verifierPassed}/${task.verifierTotal}`);
  }
  if (task.terminalEvent) parts.push(`终态 ${task.terminalEvent}`);
  parts.push(task.toolReceiptComplete === false ? 'Tool 失败未按 Case 归属' : `Tool 失败 ${task.toolFailures}`);
  if (task.cleanupStatus) parts.push(`清理 ${task.cleanupStatus}`);
  return parts.join(' · ');
}

function evidenceTaskUsage(task: EvalLabEvidenceTask): { input: number; cacheRead: number; output: number } | undefined {
  if (typeof task.inputTokens !== 'number' || typeof task.cacheReadTokens !== 'number' || typeof task.outputTokens !== 'number') return undefined;
  return { input: task.inputTokens, cacheRead: task.cacheReadTokens, output: task.outputTokens };
}

function experimentCaseDiffs(experiment: EvalLabExperiment, evidenceRuns: readonly EvalLabEvidenceRun[]): ExperimentCaseDiff[] {
  const baselineRuns = evidenceRuns.filter((run) => evidenceRunMatchesVersion(run, experiment.baseline)).sort((left, right) => right.updatedAtMs - left.updatedAtMs);
  const candidateRuns = evidenceRuns.filter((run) => evidenceRunMatchesVersion(run, experiment.candidate)).sort((left, right) => right.updatedAtMs - left.updatedAtMs);
  const baselineTasks = new Map<string, EvalLabEvidenceTask>();
  const candidateTasks = new Map<string, EvalLabEvidenceTask>();
  for (const run of baselineRuns) for (const task of run.tasks ?? []) if (!baselineTasks.has(evidenceTaskKey(task))) baselineTasks.set(evidenceTaskKey(task), task);
  for (const run of candidateRuns) for (const task of run.tasks ?? []) if (!candidateTasks.has(evidenceTaskKey(task))) candidateTasks.set(evidenceTaskKey(task), task);
  return [...baselineTasks.entries()].flatMap(([caseId, before]) => {
    const after = candidateTasks.get(caseId);
    if (!after) return [];
    const beforeUsage = evidenceTaskUsage(before);
    const afterUsage = evidenceTaskUsage(after);
    return [{
      caseId,
      result: `${evidenceTaskResult(before)} → ${evidenceTaskResult(after)}`,
      gates: `${evidenceTaskGates(before)} → ${evidenceTaskGates(after)}`,
      usage: beforeUsage && afterUsage
        ? `未缓存输入 ${formatMetric(beforeUsage.input)} → ${formatMetric(afterUsage.input)} · 缓存输入 ${formatMetric(beforeUsage.cacheRead)} → ${formatMetric(afterUsage.cacheRead)} · 输出 ${formatMetric(beforeUsage.output)} → ${formatMetric(afterUsage.output)}`
        : '逐 Case 用量分类未记录',
    }];
  }).slice(0, experiment.dataset.caseCount);
}

function ExperimentChangeRecord({ evidenceRuns, experiment }: { evidenceRuns: readonly EvalLabEvidenceRun[]; experiment: EvalLabExperiment }) {
  const caseDiffs = experimentCaseDiffs(experiment, evidenceRuns);
  return (
    <div className="eval-lab__change-record">
      <section><h4>为什么改</h4><p>{publicProblemSummary(experiment)}</p></section>
      <section><h4>实际改动</h4>{experiment.factors.length ? <ol>{experiment.factors.map((factor) => <li key={factor.name}><strong>{factorLabel(factor.name)}</strong><span>{humanClaimText(factor.reason)}</span></li>)}</ol> : <p>本轮没有记录可归因的改动。</p>}</section>
      {experiment.factors.length ? <section aria-label="方案配置摘要" className="eval-lab__factor-diffs"><h4>方案配置摘要</h4>{experiment.factors.map((factor) => (
        <article className="eval-lab__factor-diff" key={`${factor.name}-diff`}>
          <header><strong>{factorLabel(factor.name)}</strong><span>{humanClaimText(factor.reason)}</span></header>
          <div>
            <div data-kind="before"><span>修改前</span><pre><code>{humanClaimText(factor.before)}</code></pre></div>
            <div data-kind="after"><span>修改后</span><pre><code>{humanClaimText(factor.after)}</code></pre></div>
          </div>
        </article>
      ))}</section> : null}
      <section><h4>保持不变</h4><p>{experiment.frozenControls.map((control) => controlLabel(control.name)).join('、') || '未记录冻结控制'}</p></section>
      <section><h4>受影响 Case</h4>{caseDiffs.length ? <ol className="eval-lab__case-diffs">{caseDiffs.map((item) => <li key={item.caseId}><strong>{item.caseId}</strong><dl><div><dt>任务结果</dt><dd>{item.result}</dd></div><div><dt>门禁变化</dt><dd>{item.gates}</dd></div><div><dt>用量变化</dt><dd>{item.usage}</dd></div></dl></li>)}</ol> : <p>未单独记录逐 Case 影响归属；本轮仍按固定 {experiment.dataset.caseCount} 个 case 的原分母复核。</p>}</section>
      <section><h4>前后差值</h4>{experiment.comparison.metricDeltas.length ? <ul>{experiment.comparison.metricDeltas.map((delta) => <li key={delta.metric}><span>{metricLabel(delta.metric)}</span><strong>{formatMetricByName(delta.metric, delta.before)} → {formatMetricByName(delta.metric, delta.after)}</strong></li>)}</ul> : <p>暂无可比较的指标变化。</p>}</section>
      <section className="eval-lab__run-lineage"><h4>Run 与证据链</h4><p>{experiment.baseline.runId === experiment.candidate.runId
        ? <code>{experiment.baseline.runId}</code>
        : <><code>{experiment.baseline.runId}</code><span> → </span><code>{experiment.candidate.runId}</code></>}</p><small>Baseline {experiment.baseline.evidenceRefs.length} 条证据引用 · Candidate {experiment.candidate.evidenceRefs.length} 条证据引用；Case、Trace 与报告从相邻档案页签继续核对。</small></section>
      <footer><strong>{experiment.comparison.decision === 'keep' ? 'Keep' : experiment.comparison.decision === 'reject' ? 'Reject' : '待定'}</strong><span>{humanClaimText(experiment.comparison.decisionReason)}</span></footer>
    </div>
  );
}

function ExperimentOutcomeSummary({ experiment }: { experiment: EvalLabExperiment }) {
  return (
    <section aria-label="本轮实验结论" className="eval-lab__outcome-summary">
      <header>
        <div><span>先看结论</span><h3>这一轮解决了什么</h3></div>
        <span className={`eval-lab__matrix-decision eval-lab__matrix-decision--${experiment.status}`}>{statusLabel(experiment.status)}</span>
      </header>
      <dl>
        <div><dt>发现的问题</dt><dd>{publicProblemSummary(experiment)}</dd></div>
        <div><dt>本轮怎么改</dt><dd>{publicChangeSummary(experiment)}</dd></div>
        <div><dt>结果怎样</dt><dd>{matrixQuality(experiment)}</dd></div>
        <div><dt>可靠性与安全</dt><dd>{matrixReliability(experiment)}</dd></div>
        <div><dt>成本与耗时观察</dt><dd>{matrixEfficiency(experiment)}</dd></div>
        <div><dt>为什么这样决定</dt><dd>{publicDecisionSummary(experiment)}</dd></div>
      </dl>
    </section>
  );
}

function RoomReviewEvidence({ onOpenRoom, rooms }: { onOpenRoom: (roomId: string) => void; rooms: RoomSummary[] }) {
  const receipts = rooms.flatMap((room) => (room.workItems ?? [])
    .filter((workItem) => Boolean(workItem.review))
    .map((workItem) => ({ room, workItem, review: workItem.review! })))
    .sort((left, right) => (right.review.reviewedAtMs ?? right.workItem.updatedAtMs) - (left.review.reviewedAtMs ?? left.workItem.updatedAtMs));
  const latest = receipts[0];
  const room = latest?.room ?? rooms[0];
  const pendingWorkItem = room?.workItems?.find((workItem) => !workItem.review);

  return (
    <section aria-label="独立检查证据" className="eval-lab__room-review">
      <header>
        <div>
          <h3>独立检查回执</h3>
          <p>只显示 Room 真实记录的 Reviewer 结论；没有回执时不会根据实验状态自动补一个“通过”。</p>
        </div>
        <span className={`eval-lab__review-state ${latest ? 'is-recorded' : 'is-pending'}`}>{latest ? '已记录' : '待检查'}</span>
      </header>
      {latest ? (
        <>
          <dl>
            <div><dt>检查任务</dt><dd>{latest.workItem.objective || '未记录'}</dd></div>
            <div><dt>检查 Agent</dt><dd>{roomReviewParticipantName(latest.room, latest.review.reviewerParticipantId)}</dd></div>
            <div><dt>是否能运行</dt><dd>{roomReviewVerdictLabel(latest.review.operabilityVerdict)}</dd></div>
            <div><dt>是否满足需求</dt><dd>{roomReviewVerdictLabel(latest.review.requirementVerdict)}</dd></div>
            <div><dt>执行结果</dt><dd>{latest.workItem.resultSummary || '未记录'}</dd></div>
            <div><dt>检查结论</dt><dd>{latest.review.reason || '未记录'}</dd></div>
          </dl>
          <div className="eval-lab__review-evidence">
            <strong>检查依据</strong>
            {latest.review.evidenceRefs.length
              ? <ul>{latest.review.evidenceRefs.map((ref) => <li key={ref}><code>{ref}</code></li>)}</ul>
              : <span>未绑定可回跳证据，这条结论不应进入正式报告。</span>}
          </div>
          {receipts.length > 1 ? <p className="eval-lab__review-history">这个实验共有 {receipts.length} 条独立检查回执，当前显示最新一条。</p> : null}
        </>
      ) : (
        <p className="eval-lab__review-empty">{room
          ? `Room 已创建${pendingWorkItem?.state ? `，当前检查任务状态为 ${roomWorkStateLabel(pendingWorkItem.state)}` : ''}；尚未写入独立检查回执。`
          : '当前实验还没有绑定 Agent Lab Room，因此不能声称已经过独立检查。'}</p>
      )}
      {room ? <Button leadingIcon={<Users size={15} />} onClick={() => onOpenRoom(room.id)} variant="secondary">打开检查对话</Button> : null}
    </section>
  );
}

function roomReviewParticipantName(room: RoomSummary, participantId: string): string {
  return room.participants.find((participant) => participant.id === participantId)?.displayName || participantId || '未记录';
}

function roomReviewVerdictLabel(verdict: string): string {
  const normalized = verdict.trim().toLocaleLowerCase();
  if (['pass', 'passed', 'accepted', 'satisfied'].includes(normalized)) return '通过';
  if (['fail', 'failed', 'rejected', 'unsatisfied'].includes(normalized)) return '未通过';
  return verdict || '未记录';
}

function roomWorkStateLabel(state: string): string {
  return ({ queued: '排队中', active: '执行中', review: '检查中', blocked: '受阻', done: '已完成', failed: '失败', cancelled: '已取消' } as Record<string, string>)[state] ?? state;
}

function ExperimentEvidenceLinks({ catalog, experiment }: { catalog?: EvalLabEvidenceResponse; experiment: EvalLabExperiment }) {
  const [selected, setSelected] = useState<{ runId: string; taskIndex: number; tab?: EvidenceTab }>();
  if (!catalog) return null;
  const matches = matchingEvidenceRuns(catalog, experiment);
  return (
    <details className="eval-lab__experiment-evidence">
      <summary><span>查看原始证据</span><small>{matches.length ? `${matches.length} 个批次可回溯` : '当前没有匹配的公开 transcript'}</small></summary>
      {matches.length ? <div className="eval-lab__experiment-evidence-list">{matches.map((run) => <article key={run.runId}>
        <header><div><strong>{run.title}</strong><span><em className={`eval-lab__evidence-relation eval-lab__evidence-relation--${evidenceRelation(experiment, run)}`}>{evidenceRelationLabel(experiment, run)}</em>{run.sourceLabel || run.family} · {evidenceAvailabilitySummary(run)} · {runEvidenceLabel(run)}</span></div><code>{run.runId}</code></header>
        {(run.tasks?.length ?? 0) ? <ul>{run.tasks?.map((task) => <li key={task.taskIndex}><span>{task.taskLabel} · {evidenceTaskIsReportOnly(run, task) ? '仅报告证据' : task.transcriptAvailable ? `${task.toolCalls} 次 Tool` : 'transcript 缺失'}</span><button onClick={() => setSelected({ runId: run.runId, taskIndex: evidenceTaskIndex(run, task), ...(evidenceTaskIsReportOnly(run, task) ? { tab: 'report' as const } : {}) })} type="button">{selected?.runId === run.runId && selected.taskIndex === evidenceTaskIndex(run, task) ? '已打开' : evidenceTaskIsReportOnly(run, task) ? '查看报告证据' : '查看逐轮'}</button>{selected?.runId === run.runId && selected.taskIndex === evidenceTaskIndex(run, task) ? <EvidencePanel runId={run.runId} taskIndex={evidenceTaskIndex(run, task)} fallbackTask={task} initialTab={evidenceTaskIsReportOnly(run, task) ? 'report' : selected.tab} /> : null}</li>)}</ul> : <><button onClick={() => setSelected({ runId: run.runId, taskIndex: 0, tab: 'report' })} type="button">{selected?.runId === run.runId ? '已打开报告' : '查看报告'}</button>{selected?.runId === run.runId && selected.taskIndex === 0 ? <EvidencePanel runId={run.runId} taskIndex={0} initialTab="report" /> : null}</>}
      </article>)}</div> : <p className="eval-lab__evidence-empty">矩阵仍保留公开指标和 hash，但原始对话没有进入当前证据目录。</p>}
    </details>
  );
}

function evidenceOriginLabel(run: EvalLabEvidenceRun): string {
  const source = `${run.sourceId ?? ''} ${run.sourceLabel ?? ''}`.toLocaleLowerCase();
  if (source.includes('os-app') || source.includes('agent-lab-app')) return 'PAWOS Agent Lab App run';
  if (source.includes('preview')) return 'synthetic preview';
  if (run.evidenceKind === 'report_only' || source.includes('ledger')) return 'checked-in receipt';
  if (source.includes('paw-local') || run.transcriptCount > 0) return 'historical source-local run';
  return 'origin unverified';
}

function auditOriginLabel(runs: readonly EvalLabEvidenceRun[]): string {
  if (!runs.length) return 'historical evidence export · origin unverified';
  return [...new Set(runs.map(evidenceOriginLabel))].join(' + ');
}

function hasReportOnlyExperimentEvidence(catalog: EvalLabEvidenceResponse | undefined, experiment: EvalLabExperiment): boolean {
  if (!catalog) return false;
  const bindings = [...experimentRunBindings(experiment.baseline), ...experimentRunBindings(experiment.candidate)];
  return catalog.runs.some((run) => isReportOnlyEvidence(run) && bindings.some((binding) => evidenceRunMatches(binding, run.runId)));
}

function evidenceRelation(experiment: EvalLabExperiment, run: EvalLabEvidenceRun): 'baseline' | 'candidate' | 'related' {
  if (experimentRunBindings(experiment.baseline).some((binding) => evidenceRunMatches(binding, run.runId))) return 'baseline';
  if (experimentRunBindings(experiment.candidate).some((binding) => evidenceRunMatches(binding, run.runId))) return 'candidate';
  return 'related';
}

function evidenceRelationLabel(experiment: EvalLabExperiment, run: EvalLabEvidenceRun): string {
  const relation = evidenceRelation(experiment, run);
  return relation === 'baseline' ? '基线' : relation === 'candidate' ? '候选' : '相关';
}

function MetricList({ metrics }: { metrics: Readonly<Record<string, number>> }) {
  const entries = Object.entries(metrics);
  return entries.length ? <dl className="eval-lab__experiment-metrics">{entries.map(([name, value]) => <div key={name}><dt>{metricLabel(name)}</dt><dd>{formatMetricByName(name, value)}</dd></div>)}</dl> : <p>暂无指标。</p>;
}

function OutputComparison({ baseline, candidate, comparisons = [] }: {
  baseline: EvalLabExperiment['baseline'];
  candidate: EvalLabExperiment['candidate'];
  comparisons?: readonly { caseId: string; before: string; after: string }[];
}) {
  const before = publicOutputExamples(baseline);
  const after = publicOutputExamples(candidate);
  const comparisonRows = comparisons.filter(isOutputComparison);
  if (!before.length && !after.length && !comparisonRows.length) return null;
  const rows = Math.max(before.length, after.length, comparisonRows.length);
  return (
    <section aria-label="基线与候选输出对比" className="eval-lab__output-comparison">
      <header>
        <div>
          <h3>前后输出</h3>
          <p>先看 Agent 实际交付，再看分数；没有公开输出的实验不会在这里补写示例。</p>
        </div>
        <span>公开脱敏投影</span>
      </header>
      <div className="eval-lab__output-grid">
        {Array.from({ length: rows }, (_, index) => {
          const left = before[index];
          const right = after[index];
          const comparison = comparisonRows[index];
          return (
            <article key={`${left?.caseId ?? right?.caseId ?? comparison?.caseId ?? 'case'}-${index}`}>
              <div className="eval-lab__output-column">
                <span>基线</span>
                {left ? <OutputExample example={left} /> : comparison ? <p>{comparison.before}</p> : <p>未提供公开输出</p>}
              </div>
              <div className="eval-lab__output-column eval-lab__output-column--candidate">
                <span>候选</span>
                {right ? <OutputExample example={right} /> : comparison ? <p>{comparison.after}</p> : <p>未提供公开输出</p>}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}

type PublicOutputExample = { caseId: string; input: string; output: string };

function publicOutputExamples(summary: EvalLabExperiment['baseline']): PublicOutputExample[] {
  const raw = (summary as unknown as Record<string, unknown>).outputExamples;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item, index) => {
    if (!isRecord(item)) return [];
    const input = typeof item.input === 'string' ? item.input.trim() : '';
    const output = typeof item.output === 'string' ? item.output.trim() : '';
    if (!input && !output) return [];
    return [{ caseId: typeof item.caseId === 'string' ? item.caseId : `case-${index + 1}`, input: input || '未提供任务描述', output: output || '未提供公开结果' }];
  });
}

function OutputExample({ example }: { example: PublicOutputExample }) {
  return <div className="eval-lab__output-example"><strong>{example.caseId}</strong><p><b>任务：</b>{example.input}</p><p><b>结果：</b>{example.output}</p></div>;
}

function isOutputComparison(value: unknown): value is { caseId: string; before: string; after: string } {
  return isRecord(value)
    && typeof value.caseId === 'string'
    && typeof value.before === 'string'
    && typeof value.after === 'string';
}

function LinkedSessionRun({ desktop, run }: { desktop: ReturnType<typeof usePawOsDesktop>; run: EvalLabRun }) {
  return (
    <article className="eval-lab__linked-run">
      <header><div><h4>{run.title}</h4><p>{workflowLabel(run.workflowProfile)} · {run.taskCount} 个 Session · {run.taskSuccessCount}/{run.taskCount} 任务完成 · Verifier {run.verifierPassCount}/{run.verifierCount} · {percent(run.verifierPassRate)}</p></div><span>{splitLabel(run.split)}</span></header>
      <ol className="eval-lab__tasks">{run.tasks.map((task) => <EvalTaskRow desktop={desktop} key={task.sessionId} run={run} task={task} />)}</ol>
      <footer className="eval-lab__hashes">证据回执 · 报告校验码 {run.sourceReportSha256.slice(0, 12)} · 数据库校验码 {run.sourceDatabaseSha256.slice(0, 12)}</footer>
    </article>
  );
}

function EvalRunSection({ desktop, run, roomAction, onCreateRoom }: {
  desktop: ReturnType<typeof usePawOsDesktop>;
  run: EvalLabRun;
  roomAction?: { runId: string; state: 'creating' | 'sending' | 'error'; message?: string };
  onCreateRoom: () => void;
}) {
  const currentRoomAction = roomAction?.runId === run.runId ? roomAction : undefined;
  return (
    <article className="eval-lab__run">
      <header className="eval-lab__run-header">
        <div>
          <div className="eval-lab__run-heading">
            <h2>{run.title}</h2>
            <span className="eval-lab__status eval-lab__status--completed">已完成</span>
            <span className="eval-lab__lane">{splitLabel(run.split)}</span>
          </div>
          <p>{workflowLabel(run.workflowProfile)} · {run.taskCount} 个真实 Agent Session</p>
        </div>
        <div className="eval-lab__run-actions">
          <Button
            leadingIcon={currentRoomAction?.state === 'creating' || currentRoomAction?.state === 'sending' ? <LoaderCircle className="ui-spin" size={15} /> : <Users size={15} />}
            loading={currentRoomAction?.state === 'creating' || currentRoomAction?.state === 'sending'}
            onClick={onCreateRoom}
            variant="primary"
          >
            {currentRoomAction?.state === 'sending' ? '正在发送简报…' : '在 Room 中讨论优化'}
          </Button>
          <span className="eval-lab__read-only"><ShieldCheck size={14} /> 原始记录只读</span>
        </div>
      </header>

      <dl className="eval-lab__metrics">
        <Metric label="任务完成" value={`${run.taskSuccessCount}/${run.taskCount}`} tone={run.taskSuccessCount === run.taskCount ? 'good' : 'warn'} />
        <Metric label="Verifier" value={`${run.verifierPassCount}/${run.verifierCount}`} detail={percent(run.verifierPassRate)} tone={run.verifierPassCount === run.verifierCount ? 'good' : 'warn'} />
        <Metric label="Tool 调用" value={String(run.toolCalls)} detail={run.failedToolCalls ? `${run.failedToolCalls} 次失败` : '0 次失败'} />
        <Metric label="总耗时" value={duration(run.latencyMs)} />
      </dl>

      <div className="eval-lab__boundary">
        <CheckCircle2 size={15} />
        <span>{boundaryText(run.split)}</span>
      </div>

      <ol className="eval-lab__tasks">
        {run.tasks.map((task) => (
          <EvalTaskRow desktop={desktop} key={task.sessionId} run={run} task={task} />
        ))}
      </ol>
      <footer className="eval-lab__hashes">
        证据回执 · 报告校验码 {run.sourceReportSha256.slice(0, 12)} · 数据库校验码 {run.sourceDatabaseSha256.slice(0, 12)}
      </footer>
    </article>
  );
}

function Metric({ label, value, detail, tone = 'neutral' }: {
  label: string;
  value: string;
  detail?: string;
  tone?: 'neutral' | 'good' | 'warn';
}) {
  return (
    <div className={`eval-lab__metric eval-lab__metric--${tone}`}>
      <dt>{label}</dt>
      <dd><strong>{value}</strong>{detail ? <small>{detail}</small> : null}</dd>
    </div>
  );
}

function EvalTaskRow({ desktop, run, task }: {
  desktop: ReturnType<typeof usePawOsDesktop>;
  run: EvalLabRun;
  task: EvalLabTask;
}) {
  const [showEvidence, setShowEvidence] = useState(false);
  return (
    <li className="eval-lab__task">
      <div className="eval-lab__task-summary">
        <span aria-hidden="true" className={`eval-lab__task-mark ${task.taskSucceeded ? 'is-success' : 'is-review'}`} />
        <div className="eval-lab__task-copy">
          <strong>{task.taskAlias}</strong>
          <span>{task.taskSucceeded ? '任务通过' : '需要复核'} · Verifier {task.verifierPassed}/{task.verifierTotal} · {task.toolCalls} 次 Tool</span>
        </div>
        {desktop ? (
          <Button
            leadingIcon={<ArrowUpRight size={15} />}
            onClick={() => desktop.openWindow({
              appId: 'agent',
              target: {
                kind: 'session',
                id: task.sessionId,
                title: `${run.title} · ${task.taskAlias}`,
                subtitle: '评测记录 · 只读',
              },
            })}
            variant="secondary"
          >
            查看对话
          </Button>
        ) : null}
        <Button
          leadingIcon={<ClipboardList size={15} />}
          onClick={() => setShowEvidence((visible) => !visible)}
          variant="secondary"
        >
          {showEvidence ? '收起逐轮证据' : '查看逐轮证据'}
        </Button>
      </div>
      {task.explanation ? <TaskExplanation task={task} /> : <p className="eval-lab__task-unavailable">暂无可用的解释投影。</p>}
      {showEvidence ? <EvidencePanel runId={run.runId} taskIndex={task.taskIndex} fallbackTask={task} /> : null}
    </li>
  );
}

type EvidenceTab = 'task' | 'trace' | 'environment' | 'acceptance' | 'report';

function EvidencePanel({ runId, taskIndex, fallbackTask, initialTab }: {
  runId: string;
  taskIndex: number;
  fallbackTask?: EvalLabTask | EvalLabEvidenceTask;
  initialTab?: EvidenceTab;
}) {
  const evidence = useEvalLabEvidenceDetail(runId, taskIndex, true);
  const [tab, setTab] = useState<EvidenceTab>(initialTab ?? 'task');
  const [lastError, setLastError] = useState(evidence.error);
  useEffect(() => { if (!evidence.isFetching) setLastError(evidence.error); }, [evidence.error, evidence.isFetching]);
  const evidenceError = evidence.error ?? (evidence.isFetching ? lastError : null);
  const detail = evidence.data?.detail;
  const unavailableReason = detail ? unavailableEvidenceReason(detail) : '';
  const projection = detail && !isEvalLabEvidenceUnavailable(detail) && !unavailableReason ? detail : undefined;
  const reportOnly = detail?.status === 'report_only'
    || detail?.summary?.status === 'report_only'
    || detail?.summary?.evidenceKind === 'report_only'
    || fallbackTask?.evidenceStatus === 'report_only';
  const runReport = detail?.status === 'report_available'
    || Boolean(detail?.report && !detail?.task && !reportOnly);
  const transcriptMissing = detail?.status === 'transcript_missing';
  const previewEvidence = detail?.origin === 'preview' || evidence.data?.source.label.includes('预览');
  const reportView = tab === 'report' || reportOnly || runReport;
  const panelTitle = unavailableReason ? '证据暂时不可读' : reportView
    ? '运行报告'
    : tab === 'task'
      ? '任务与结果'
      : tab === 'trace'
        ? transcriptMissing ? 'Transcript 证据缺口' : '运行轨迹'
        : tab === 'acceptance'
          ? '验收结果'
          : '运行环境';
  const evidenceTabs = (reportOnly
    ? [['report', '回执 / 报告']]
    : [['task', '任务'], ['trace', '运行轨迹'], ['acceptance', '验收'], ['environment', '运行环境'], ...(detail?.report ? [['report', '公开报告']] as const : [])]) as readonly (readonly [EvidenceTab, string])[];
  return (
    <section aria-label="逐轮证据面板" className="eval-lab__evidence-panel">
      <header className="eval-lab__evidence-panel-header">
        <div>
          <span className="eval-lab__evidence-kind">{previewEvidence ? '预览样例' : '只读证据'}</span>
          <h3>{panelTitle}</h3>
          <p>{unavailableReason ? '原始证据当前不可读取；已有任务和验收记录保留。' : !detail ? '读取证据后，可核对这条任务的公开消息、工具返回和验收结果。' : reportView ? (reportOnly ? '这条运行只有回执/报告，原始对话未公开；请按逐 case/报告核对。' : '这是该运行的公开报告投影；报告与逐轮 transcript 分开展示。') : transcriptMissing ? '这条任务有验收回执，但原始 JSONL 没有随运行目录保存；不会用摘要伪造对话。' : previewEvidence ? '这是演示数据中的有限样例，不代表实际 JSONL transcript；真实运行请查看本机回执。' : '这是该任务的公开 transcript 投影；只显示可复核的用户消息、助手动作和 Tool 返回摘要。'}</p>
          <code className="eval-lab__evidence-id">{runId} · {taskIndex > 0 ? `Task ${taskIndex}` : '运行级'}</code>
        </div>
        <span className="eval-lab__read-only"><ShieldCheck size={14} /> 不会重新运行</span>
      </header>
      {evidence.isLoading && !evidenceError && !unavailableReason ? <p className="eval-lab__evidence-loading" role="status">正在读取这条任务的原始回执…</p> : null}
      {evidenceError || unavailableReason ? (
        <div className="eval-lab__evidence-error" role="alert">
          <p>{evidenceError ? '逐轮证据暂时不可读。已有记录和选择保留，可以重新读取证据。' : unavailableReason}</p>
          <Button size="small" loading={evidence.isFetching} onClick={() => void evidence.refetch()} preserveFocusWhileLoading>重新读取证据</Button>
        </div>
      ) : null}
      <>
          <nav aria-label="证据内容" className="eval-lab__evidence-tabs" role="tablist">
            {evidenceTabs.map(([key, label], index) => (
              <button aria-selected={tab === key} key={key} onClick={() => setTab(key)} onKeyDown={(event) => moveTabbedSelection(event, index, evidenceTabs.map(([value]) => value), setTab)} role="tab" type="button">{label}</button>
            ))}
          </nav>
          {tab === 'task' && projection ? <CaseProofSummary
            detail={projection}
            previewEvidence={Boolean(previewEvidence)}
            reportOnly={Boolean(reportOnly || runReport)}
            transcriptMissing={transcriptMissing}
          /> : null}
          {tab === 'trace' && !reportOnly && projection ? (
            <div className="eval-lab__trace-evidence">
              <section>
                <h4>Agent 对话与动作</h4>
                {transcriptMissing ? <MissingTranscriptEvidence detail={projection} /> : <ConversationEvidence detail={projection} />}
              </section>
              <section>
                <h4>Tool 返回</h4>
                <ToolEvidence detail={projection} />
              </section>
            </div>
          ) : null}
          {tab === 'environment' ? <EnvironmentEvidence detail={detail} fallbackTask={fallbackTask} /> : null}
          {tab === 'acceptance' ? <AcceptanceEvidence detail={detail} fallbackTask={fallbackTask} /> : null}
          {tab === 'report' ? <ReportEvidence detail={detail} /> : null}
      </>
    </section>
  );
}

function unavailableEvidenceReason(detail: EvalLabEvidenceDetail): string {
  const reasons: Partial<Record<EvalLabEvidenceDetail['status'], string>> = {
    invalid_run_id: '这条运行标识无效，暂时无法读取证据。',
    source_unavailable: '评测研究盘当前不可用。',
    not_found: '没有找到这条运行的证据。',
    task_not_found: '没有找到这条任务的证据。',
    no_sessions: '这条运行没有保存可读取的 Session 记录。',
    report_unavailable: '这条运行没有可读取的公开报告。',
  };
  const reason = reasons[detail.status];
  return reason ? detail.message || reason : '';
}

function CaseProofSummary({ detail, previewEvidence, reportOnly, transcriptMissing }: {
  detail: EvalLabEvidenceProjectionDetail;
  previewEvidence: boolean;
  reportOnly: boolean;
  transcriptMissing: boolean;
}) {
  const messages = detail.turns.filter((turn) => turn.kind === 'message');
  const request = messages.find((turn) => turn.role === 'user')?.text
    || detail.task?.taskLabel
    || detail.task?.title
    || '任务输入未公开';
  const outcome = [...messages].reverse().find((turn) => turn.role === 'assistant')?.text
    || (reportOnly ? '这条记录只有运行报告，Agent 原始输出未公开。' : transcriptMissing ? '原始对话缺失，无法展示 Agent 最终交付。' : 'Agent 最终交付未公开。');
  const passed = detail.task?.verifierPassed;
  const total = detail.task?.verifierTotal;
  const verdict = detail.task?.taskSucceeded === true
    ? '任务通过'
    : detail.task?.taskSucceeded === false
      ? '需要复核'
      : '任务结果未记录';
  const acceptance = typeof passed === 'number' && typeof total === 'number'
    ? `${passed}/${total} 条通过 · ${verdict}`
    : verdict;
  const source = previewEvidence
    ? '公开预览样例'
    : transcriptMissing
      ? '验收回执；原始对话缺失'
      : reportOnly
        ? '运行报告；原始对话未公开'
        : detail.turns.length
          ? '真实 transcript（只读）'
          : '公开证据投影';

  return (
    <section aria-label="任务证据摘要" className="eval-lab__case-proof">
      <header>
        <span>单条任务证据链</span>
        <h4>这条任务一眼看懂</h4>
      </header>
      <dl>
        <div><dt>任务要求</dt><dd>{request}</dd></div>
        <div><dt>Agent 最终交付</dt><dd>{outcome}</dd></div>
        <div><dt>系统验收</dt><dd>{acceptance}</dd></div>
        <div><dt>证据来源</dt><dd>{source}</dd></div>
      </dl>
      <p>下面可以继续核对逐轮对话、Tool 返回、运行环境和每条验收结果。</p>
    </section>
  );
}

function MissingTranscriptEvidence({ detail }: { detail?: EvalLabEvidenceDetail }) {
  const task = detail?.task;
  return (
    <div className="eval-lab__missing-evidence">
      <strong>无法打开逐轮对话</strong>
      <p>运行目录保留了这条任务的公开验收结果，但没有可读取的 transcript。请以报告和校验码核对结果；不要把“只有报告”当成 Agent 对话证据。</p>
      <dl>
        <div><dt>任务</dt><dd>{task?.taskLabel || task?.title || '未记录'}</dd></div>
        <div><dt>失败 Verifier</dt><dd>{task?.failedVerifierIndexes?.length ? task.failedVerifierIndexes.map((index) => `#${index}`).join('、') : '未记录'}</dd></div>
        <div><dt>初步归因</dt><dd>{failureOwnerText(task?.failureOwner) || '未记录（仅为报告字段，不是最终判定）'}</dd></div>
      </dl>
    </div>
  );
}

function ReportEvidence({ detail }: { detail?: EvalLabEvidenceDetail }) {
  const report = detail?.report;
  const summary = detail?.summary;
  if (!report && !summary) return <p className="eval-lab__evidence-empty">没有公开报告投影。</p>;
  const entries = report ? Object.entries(report).filter(([key]) => key !== 'available') : [];
  const priority = new Set(['status', 'decision', 'metrics', 'comparison', 'runtime', 'environment', 'configuration', 'dataset', 'usage', 'estimate', 'claim']);
  entries.sort(([left], [right]) => Number(priority.has(right)) - Number(priority.has(left)));
  return (
    <div className="eval-lab__report-evidence">
      <div className="eval-lab__evidence-summary-strip">
        <span>状态：<b>{String(report?.status || summary?.status || '未记录')}</b></span>
        <span>运行：<b>{summary?.title || detail?.runId || '未记录'}</b></span>
        <span>报告 hash：<b>{sourceShortHash(summary?.reportSha256)}</b></span>
        <span>原始报告只读</span>
      </div>
      {entries.length ? <div className="eval-lab__report-sections">{entries.map(([key, value]) => <details className="eval-lab__report-section" key={key} open={priority.has(key)}><summary><strong>{reportLabel(key)}</strong><span>{reportValueHint(value)}</span></summary><div className="eval-lab__report-section-body"><PublicReportValue value={value} /></div></details>)}</div> : <p className="eval-lab__evidence-empty">只有运行级摘要，没有可公开的细粒度字段。</p>}
      <p className="eval-lab__evidence-note">报告中的路径、凭据、隐藏标准答案和内部推理已在服务端投影阶段移除；这不是重新运行结果。</p>
    </div>
  );
}

function PublicReportValue({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (value === null || value === undefined || typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return <p className="eval-lab__report-scalar">{value === null || value === undefined || value === '' ? '—' : String(value)}</p>;
  }
  if (depth > 2) return <p className="eval-lab__report-scalar">已折叠更深层字段</p>;
  if (Array.isArray(value)) {
    return value.length ? <ul className="eval-lab__report-array">{value.map((item, index) => <li key={index}><PublicReportValue depth={depth + 1} value={item} /></li>)}</ul> : <p className="eval-lab__report-scalar">—</p>;
  }
  if (isRecord(value)) {
    const entries = Object.entries(value);
    return entries.length ? <dl className="eval-lab__report-fields">{entries.map(([key, item]) => <div key={key}><dt>{reportLabel(key)}</dt><dd><PublicReportValue depth={depth + 1} value={item} /></dd></div>)}</dl> : <p className="eval-lab__report-scalar">—</p>;
  }
  return <p className="eval-lab__report-scalar">—</p>;
}

function reportValueHint(value: unknown): string {
  if (Array.isArray(value)) return `${value.length} 项`;
  if (isRecord(value)) return `${Object.keys(value).length} 个字段`;
  return '公开字段';
}

function reportLabel(key: string): string {
  return ({
    schemaVersion: '报告版本',
    status: '状态',
    decision: '决策',
    metrics: '指标',
    comparison: '对比',
    failedHardGates: '失败门禁',
    provenanceGaps: '证据缺口',
    boundaries: '边界',
    executiveVerdict: '结论',
    runtime: '运行时',
    runtimeIdentity: '运行时身份',
    environment: '机器环境',
    configuration: '配置',
    dataset: '数据集',
    sample: '样本',
    result: '结果',
    usage: 'Usage',
    estimate: '成本估算',
    billing: '计费状态',
    pricingIdentity: '价格身份',
    observabilityLimit: '可观测性边界',
    verification: '验证',
    evidence: '证据',
    frozenIdentity: '冻结身份',
    baseline: '基线',
    winner: '候选结果',
    oneShot: '一次性门禁',
    evaluationScope: '评测范围',
    evaluationMode: '评测模式',
    heldOutEvaluated: 'Held-out 是否运行',
    formalAcceptanceEligible: '是否具备正式验收资格',
    formalAcceptancePassed: '正式验收是否通过',
    heldOutGateProduced: '是否产生 Held-out 门禁',
    cleanupPassed: '清理是否通过',
    localOnly: '仅本机',
    uploaded: '是否上传',
    elapsedMs: '总耗时（ms）',
    startedAtMs: '开始时间（ms）',
    completedAtMs: '完成时间（ms）',
    measuredAt: '测量时间',
    provider: 'Provider',
    model: '模型',
    thinking: '思考档位',
    runtimeVersion: 'Runtime 版本',
    piVersion: 'Pi 版本',
    protocolVersion: '协议版本',
    transport: '传输',
    taskCount: '任务数',
    taskSuccessCount: '完成任务数',
    taskSuccessRate: '任务完成率',
    verifierCount: 'Verifier 总数',
    verifierPassCount: 'Verifier 通过数',
    verifierPassRate: 'Verifier 通过率',
    toolCalls: 'Tool 调用',
    failedToolCalls: 'Tool 失败',
    transcriptToolCalls: 'Transcript Tool 调用',
    failedTranscriptToolCalls: 'Transcript Tool 失败',
    sessionCount: 'Session 数',
    transcriptCount: 'Transcript 数',
    latencyMs: '耗时（ms）',
    allDatabasesCleaned: '临时库是否清理',
    fa: '故障类型命中（FA）',
    answerJudgeCorrectnessRate: '答案 Judge 正确率',
    answerSuccessRate: '答案通过率',
    highLevelFactCoverage: '高层事实覆盖',
    factCoverage: '事实覆盖',
    citationFactCoverage: '引用事实覆盖',
    answerableCitationSupportRate: '可回答引用支持',
    citationSupportRate: '引用支持率',
    infoNotFoundAbstentionRecall: '无信息拒答召回',
    abstentionAccuracy: '拒答准确率',
    abstentionF1: '拒答 F1',
    citationResolutionRate: '引用解析率',
    citationHardGatePassed: '引用门禁',
    citationGatePassed: '引用门禁',
    terminalCompletionPassed: '终态完成门禁',
    toolContractPassed: 'Tool 合同门禁',
    toolSuccessRate: 'Tool 成功率',
    outputProtocolRate: '输出协议通过率',
    tokens: 'Token',
    totalTokens: '总 Token',
    inputTokens: '输入 Token',
    outputTokens: '输出 Token',
    cacheReadTokens: '缓存读取 Token',
    sourceLocal: '仅本机',
    installed: '已安装',
    candidateRuntimeInstalled: '候选 Runtime 已安装',
    heldOutConsumed: 'Held-out 是否消费',
    corpusDocumentCount: '文档数',
    chunkCount: 'Chunk 数',
    denseVectorCount: 'Dense 向量数',
    graphNodeCount: '图节点数',
    graphEdgeCount: '图边数',
    sourceSha256: '来源校验码',
    reportSha256: '报告校验码',
    databaseSha256: '数据库校验码',
    publishedDate: '价格发布日期',
    currency: '币种',
    unit: '计价单位',
  } as Record<string, string>)[key] ?? key;
}

function ConversationEvidence({ detail }: { detail?: EvalLabEvidenceProjectionDetail }) {
  const turns = detail?.turns ?? [];
  if (!turns.length) return <p className="eval-lab__evidence-empty">这条 Session 没有可公开的消息记录，或 transcript 尚未复制到证据目录。</p>;
  return (
    <ol aria-label="公开逐轮消息" className="eval-lab__evidence-timeline">
      {turns.map((turn, index) => (
        <li className={`eval-lab__evidence-turn eval-lab__evidence-turn--${turn.kind}`} key={`${turn.entryRef ?? 'entry'}-${index}`}>
          <div className="eval-lab__evidence-turn-meta">
            <strong>{turn.kind === 'tool_call' ? '助手 · Tool 调用' : turn.kind === 'tool_result' ? 'Tool 返回' : turn.role === 'user' ? '用户任务' : '助手输出'}</strong>
            {turn.toolName ? <span>{turn.toolName}</span> : null}
            {turn.status ? <span className={turn.status === 'failed' ? 'is-failed' : 'is-complete'}>{turn.status === 'failed' ? '失败' : '完成'}</span> : null}
            {turn.timestampMs ? <time dateTime={new Date(turn.timestampMs).toISOString()}>{formatEvidenceTime(turn.timestampMs)}</time> : null}
          </div>
          <p>{turn.text}</p>
          {turn.argumentKeys?.length ? <small>参数字段：{turn.argumentKeys.join('、')}</small> : null}
        </li>
      ))}
    </ol>
  );
}

function formatEvidenceTime(timestampMs: number): string {
  try {
    return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(timestampMs));
  } catch {
    return '';
  }
}

function ToolEvidence({ detail }: { detail?: EvalLabEvidenceProjectionDetail }) {
  const tools = detail?.tools ?? [];
  return (
    <div className="eval-lab__tool-evidence">
      <div className="eval-lab__evidence-summary-strip">
        <span><b>{tools.length}</b> 条可见 Tool 返回</span>
        <span><b>{tools.filter((tool) => tool.status === 'failed').length}</b> 条失败</span>
        <span>参数值、SQL 和凭据已隐藏</span>
      </div>
      {tools.length ? <ol className="eval-lab__tool-list">{tools.map((tool, index) => <li key={`${tool.toolName}-${index}`}><div><strong>{tool.toolName}</strong><span className={tool.status === 'failed' ? 'is-failed' : 'is-complete'}>{tool.status === 'failed' ? '失败' : '完成'}</span></div><p>{tool.text}</p></li>)}</ol> : <p className="eval-lab__evidence-empty">没有可公开的 Tool 返回。</p>}
    </div>
  );
}

function EnvironmentEvidence({ detail, fallbackTask }: { detail?: EvalLabEvidenceDetail; fallbackTask?: EvalLabTask | EvalLabEvidenceTask }) {
  const environment = detail?.environment ?? {};
  const session = detail?.session ?? {};
  const task = detail?.task ?? fallbackTask;
  const sourceTask = fallbackTask && 'model' in fallbackTask ? fallbackTask : undefined;
  const transcriptHash = task && 'transcriptSha256' in task && typeof task.transcriptSha256 === 'string' ? task.transcriptSha256 : '';
  const usage = environment.tokenUsage;
  const knowledge = environment.knowledge;
  const publicConfig = environment.publicConfig;
  const rows: Array<[string, string]> = [
    ['模型', session.model || environment.model || sourceTask?.model || '未记录'],
    ['Provider', environment.provider || '未记录'],
    ['思考档位', session.thinking || environment.thinking || sourceTask?.thinking || '未记录'],
    ['执行模式', session.executionMode || sourceTask?.executionMode || environment.executionModes?.join(' / ') || '未记录'],
    ['Host verifier', task && typeof task.verifierPassed === 'number' && typeof task.verifierTotal === 'number' ? `${task.verifierPassed}/${task.verifierTotal}` : '未记录'],
    ['运行合同', environment.workflowProfile || '未记录'],
    ['数据切分', environment.split ? splitLabel(environment.split) : '未记录'],
    ['传输', environment.transport || '未记录'],
    ['超时上限', environment.timeoutSeconds ? `${environment.timeoutSeconds}s` : '未记录'],
    ['Pi / Runtime', [environment.piVersion, environment.runtimeVersion].filter(Boolean).join(' · ') || '未记录'],
    ['工作区', environment.workspace || '未记录'],
    ['网络', environment.network || '未记录'],
    ['Token usage', usage && (usage.inputTokens || usage.outputTokens) ? `输入 ${usage.inputTokens ?? 0} · 输出 ${usage.outputTokens ?? 0} · 缓存读 ${usage.cacheReadTokens ?? 0}` : (environment.pricingUsage || '未投影')],
    ['Knowledge 快照', knowledge && knowledge.available ? `文档 ${knowledge.documentCount ?? 0} · Chunk ${knowledge.chunkCount ?? 0} · Dense ${knowledge.denseVectorCount ?? 0}` : '未记录'],
    ['Graph 快照', knowledge && knowledge.available ? `节点 ${knowledge.graphNodeCount ?? 0} · 边 ${knowledge.graphEdgeCount ?? 0}` : '未记录'],
    ['公开运行参数', publicConfig ? formatPublicConfig(publicConfig) : '未记录'],
    ['机器环境', environment.machine ? formatPublicConfig(environment.machine) : '未记录'],
    ['Usage 回执', environment.usageReceipt ? formatPublicConfig(environment.usageReceipt) : '未记录'],
    ['成本估算', environment.costEstimate ? formatPublicConfig(environment.costEstimate) : '未提供'],
    ['计费状态', environment.billing ? formatPublicConfig(environment.billing) : '未提供'],
    ['价格身份', environment.pricingIdentity ? formatPublicConfig(environment.pricingIdentity) : '未记录'],
    ['Trace 数量', typeof environment.traceCount === 'number' ? String(environment.traceCount) : '未记录'],
    ['Transcript 校验码', transcriptHash ? transcriptHash.slice(0, 16) + '…' : '未记录'],
  ];
  const artifacts = detail?.summary?.artifacts ?? [];
  return <><dl className="eval-lab__environment-grid">{rows.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>{artifacts.length ? <section className="eval-lab__artifact-list" aria-label="可复现证据文件"><h4>可复现证据</h4><ul>{artifacts.map((artifact) => <li key={`${artifact.kind}-${artifact.label}`}><span>{artifact.label}</span><small>{artifact.available ? `${artifact.bytes ? formatBytes(artifact.bytes) : '已登记'} · ${sourceShortHash(artifact.sha256)}` : '不可读'}</small></li>)}</ul></section> : null}</>;
}

function formatPublicConfig(value: Readonly<Record<string, unknown>>): string {
  const labels: Record<string, string> = {
    maxReadsPerCase: '每 case 最大读取', candidateDepth: '候选深度', finalDepth: '最终深度', batchPlan: '批次', chunking: '切块', embedding: 'Embedding', reranker: 'Reranker',
    available: '可用', inputTokens: '输入 Token', outputTokens: '输出 Token', cachedInputTokens: '缓存输入 Token', uncachedInputTokens: '未缓存输入 Token',
    totalCostUsd: '总成本 USD', cachedInputCostUsd: '缓存成本 USD', uncachedInputCostUsd: '未缓存成本 USD', outputCostUsd: '输出成本 USD',
    sourceRef: 'Usage 来源', sourceSha256: 'Usage 校验码', pricingId: '价格版本', provider: 'Provider', model: '模型', currency: '币种', unit: '计价单位', status: '状态',
  };
  return Object.entries(value).slice(0, 8).map(([key, item]) => `${labels[key] || reportLabel(key)} ${typeof item === 'object' ? JSON.stringify(item) : String(item)}`).join(' · ');
}

function AcceptanceEvidence({ detail, fallbackTask }: { detail?: EvalLabEvidenceDetail; fallbackTask?: EvalLabTask | EvalLabEvidenceTask }) {
  const task = detail?.task ?? fallbackTask;
  if (!task) return <p className="eval-lab__evidence-empty">没有验收投影。</p>;
  const passed = typeof task.verifierPassed === 'number' ? task.verifierPassed : undefined;
  const total = typeof task.verifierTotal === 'number' ? task.verifierTotal : undefined;
  return (
    <div className="eval-lab__acceptance-evidence">
      <div className="eval-lab__evidence-summary-strip">
        <span>任务结果：<b>{task.taskSucceeded === true ? '通过' : task.taskSucceeded === false ? '需要复核' : '未记录'}</b></span>
        <span>{`Host verifier ${passed !== undefined && total !== undefined ? `${passed}/${total}` : '未记录'}`}</span>
        <span>终态：<b>{task.terminalEvent || '未记录'}</b></span>
      </div>
      <dl className="eval-lab__acceptance-grid">
        <div><dt>失败 Verifier</dt><dd>{task.failedVerifierIndexes?.length ? task.failedVerifierIndexes.map((index) => `#${index}`).join('、') : '无 / 未记录'}</dd></div>
        <div><dt>失败项名称</dt><dd>{task.failedVerifierNames?.length ? task.failedVerifierNames.join('、') : '未记录'}</dd></div>
        <div><dt>初步归因</dt><dd>{failureOwnerText(task.failureOwner) || '未记录'}</dd></div>
        <div><dt>临时数据清理</dt><dd>{task.cleanupStatus || '未记录'}</dd></div>
        <div><dt>成功 Tool</dt><dd>{typeof task.successfulToolCalls === 'number' ? task.successfulToolCalls : '未记录'}</dd></div>
        <div><dt>Runtime 错误</dt><dd>{task.runtimeErrorType || '无 / 未记录'}</dd></div>
      </dl>
      {task.verifierResults?.length ? <div className="eval-lab__verifier-list" aria-label="逐项 Verifier 结果">{task.verifierResults.map((item) => <span className={item.passed ? 'is-pass' : 'is-fail'} key={item.index}>#{item.index} {item.passed ? '通过' : '失败'}</span>)}</div> : null}
      <p className="eval-lab__evidence-note">这里是 Host 验收投影，不是 Agent 自述；隐藏标准答案和原始 SQL 不会进入页面。失败项可以回到上面的逐轮 Tool 轨迹核对。</p>
    </div>
  );
}

function SourceEvidenceCatalog({ catalog }: { catalog: import('./api').EvalLabEvidenceResponse }) {
  const [selected, setSelected] = useState<{ runId: string; taskIndex: number; tab?: EvidenceTab }>();
  const [sourceFilter, setSourceFilter] = useState('all');
  const [search, setSearch] = useState('');
  const availableRuns = catalog.runs.filter((run) => {
    if (sourceFilter !== 'all' && run.sourceId !== sourceFilter) return false;
    const needle = search.trim().toLocaleLowerCase();
    const searchable = [
      run.title, run.family, run.runId, run.split, run.environment.model, run.environment.provider,
      ...(run.tasks ?? []).map((task) => `${task.title} ${task.taskLabel} ${task.failureOwner || ''}`),
    ].filter(Boolean).join(' ').toLocaleLowerCase();
    return !needle || searchable.includes(needle);
  });
  const sources = catalog.sources ?? [];
  const readableTranscripts = catalog.runs.length
    ? catalog.runs.reduce((count, run) => count + readableTranscriptCount(run), 0)
    : catalog.source.transcriptCount;
  const missingTranscripts = catalog.runs.length
    ? catalog.runs.reduce((count, run) => count + missingTranscriptCount(run), 0)
    : catalog.source.missingTranscriptCount ?? 0;
  const reportOnlyRuns = catalog.runs.length
    ? catalog.runs.filter(isReportOnlyEvidence).length
    : catalog.source.reportOnlyRunCount ?? 0;
  const groups = Array.from(availableRuns.reduce((map, run) => {
    const key = run.sourceLabel || run.sourceId || 'unknown';
    const group = map.get(key) ?? { label: run.sourceLabel || '来源未记录', runs: [] as EvalLabEvidenceRun[] };
    group.runs.push(run);
    map.set(key, group);
    return map;
  }, new Map<string, { label: string; runs: EvalLabEvidenceRun[] }>()));
  return (
    <section aria-label="研究盘历史运行" className="eval-lab__source-catalog">
      <header className="eval-lab__source-catalog-header">
        <div>
          <p className="eval-lab__eyebrow"><ClipboardList aria-hidden="true" size={15} /> 历史运行档案</p>
          <h3>所有测试轮次与证据</h3>
          <p>{catalog.source.label} · {catalog.source.runCount} 个批次 · {catalog.source.sessionCount} 个 Session · {readableTranscripts} 份可读 transcript。Report-only 只保留回执/报告，原文未公开；每个批次都保留失败和回退，不覆盖原始证据。</p>
        </div>
        <div className="eval-lab__source-head-stats"><span>{formatBytes(catalog.source.transcriptBytes)} transcript</span><span>{missingTranscripts} 个 Session 缺 transcript · {reportOnlyRuns} 条回执/报告，原文未公开</span></div>
      </header>
      {!catalog.source.available ? <p className="eval-lab__evidence-empty">评测研究盘未连接；已保存到 PAW 的 Session 仍可在下方查看。</p> : null}
      <div className="eval-lab__source-filters" aria-label="筛选证据来源">
        <label>来源
          <select value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value)}>
            <option value="all">全部来源（{catalog.runs.length}）</option>
            {sources.map((source) => <option key={source.sourceId} value={source.sourceId}>{source.label}（{source.runCount}）</option>)}
          </select>
        </label>
        <label className="eval-lab__source-search">搜索
          <input aria-label="搜索运行" onChange={(event) => setSearch(event.target.value)} placeholder="场景、模型或 run id" value={search} />
        </label>
        <span className="eval-lab__source-filter-count">显示 {availableRuns.length} 条</span>
      </div>
      <div className="eval-lab__source-run-list">
        {groups.map(([sourceId, group]) => <section className="eval-lab__source-group" key={sourceId}>
          <header><strong>{group.label}</strong><span>{group.runs.length} 个批次 · {group.runs.reduce((count, run) => count + readableTranscriptCount(run), 0)} 份可读 transcript</span></header>
          {group.runs.map((run) => (
            <details className="eval-lab__source-run" key={run.runId} open={run.runId === selected?.runId}>
              <summary><span><strong>{run.title}</strong><small>{run.family} · {splitLabel(run.split)} · {evidenceAvailabilitySummary(run)}</small></span><span className="eval-lab__source-summary-right"><span className={`eval-lab__source-status eval-lab__source-status--${run.reportAvailable ? 'reported' : 'unreported'}`}>{runEvidenceLabel(run)}</span></span></summary>
              <div className="eval-lab__source-run-body">
                <div className="eval-lab__source-run-metrics"><span>结果</span><strong>{sourceMetricSummary(run)}</strong><span>环境</span><strong>{[run.environment.model, run.environment.thinking, run.environment.executionModes?.join(' / ')].filter(Boolean).join(' · ') || '未记录'}</strong></div>
                {run.reportAvailable && run.sessionCount > 0 ? <div className="eval-lab__source-report-link"><span>运行级报告可用：可查看决策、指标、环境与证据边界。</span><button onClick={() => setSelected({ runId: run.runId, taskIndex: run.tasks?.[0]?.taskIndex ?? 1, tab: 'report' })} type="button">{selected?.runId === run.runId && selected.tab === 'report' ? '已打开运行报告' : '查看运行报告'}</button>{selected?.runId === run.runId && selected.tab === 'report' ? <EvidencePanel initialTab="report" fallbackTask={run.tasks?.[0]} runId={run.runId} taskIndex={run.tasks?.[0]?.taskIndex ?? 1} /> : null}</div> : null}
                {(run.tasks?.length ?? 0) ? <ol className="eval-lab__source-task-list">{run.tasks?.map((task) => <li key={`${run.runId}-${task.taskIndex}`}><div><strong>{task.taskLabel}</strong><span>{evidenceTaskIsReportOnly(run, task) ? '仅报告证据；原文未公开' : task.transcriptAvailable ? `${task.userMessages} 轮用户消息 · ${task.toolCalls} 次 Tool` : 'transcript 缺失'}{task.verifierTotal ? ` · Verifier ${task.verifierPassed ?? 0}/${task.verifierTotal}` : ''}{task.failedVerifierIndexes?.length ? ` · 失败 ${task.failedVerifierIndexes.map((index) => `#${index}`).join('、')}` : ''}{task.failureOwner ? ` · 初步归因：${failureOwnerText(task.failureOwner)}` : ''}</span></div><button onClick={() => setSelected({ runId: run.runId, taskIndex: evidenceTaskIndex(run, task), ...(evidenceTaskIsReportOnly(run, task) ? { tab: 'report' as const } : {}) })} type="button">{selected?.runId === run.runId && selected.taskIndex === evidenceTaskIndex(run, task) ? '已打开' : evidenceTaskIsReportOnly(run, task) ? '查看报告证据' : task.transcriptAvailable ? '查看逐轮证据' : '查看缺口'}</button>{selected?.runId === run.runId && selected.taskIndex === evidenceTaskIndex(run, task) ? <EvidencePanel initialTab={evidenceTaskIsReportOnly(run, task) ? 'report' : undefined} runId={run.runId} taskIndex={evidenceTaskIndex(run, task)} fallbackTask={task} /> : null}</li>)}</ol> : <div className="eval-lab__source-report-only"><span>这批只有回执/报告，原始对话未公开；可以查看逐 case/报告证据。</span><button onClick={() => setSelected({ runId: run.runId, taskIndex: 0, tab: 'report' })} type="button">{selected?.runId === run.runId ? '已打开报告' : '查看报告证据'}</button>{selected?.runId === run.runId && selected.taskIndex === 0 ? <EvidencePanel initialTab="report" runId={run.runId} taskIndex={0} /> : null}</div>}
                <p className="eval-lab__source-hashes">DB {sourceShortHash(run.databaseSha256)} · report {sourceShortHash(run.reportSha256)} · marker {sourceShortHash(run.markerSha256)} · 只读来源</p>
              </div>
            </details>
          ))}
        </section>)}
        {!availableRuns.length ? <p className="eval-lab__evidence-empty">没有匹配的运行记录。</p> : null}
      </div>
    </section>
  );
}

function isReportOnlyEvidence(run: Pick<EvalLabEvidenceRun, 'evidenceKind' | 'status'>): boolean {
  return run.evidenceKind === 'report_only' || run.status === 'report_only';
}

function readableTranscriptCount(run: EvalLabEvidenceRun): number {
  return isReportOnlyEvidence(run) ? 0 : Math.max(0, run.transcriptCount || 0);
}

function missingTranscriptCount(run: EvalLabEvidenceRun): number {
  if (isReportOnlyEvidence(run)) return 0;
  return Math.max(0, (run.sessionCount || 0) - readableTranscriptCount(run));
}

function evidenceAvailabilitySummary(run: EvalLabEvidenceRun): string {
  if (isReportOnlyEvidence(run)) {
    return `${run.sessionCount > 0 ? `${run.sessionCount} 个 Session ` : ''}回执/报告，原文未公开`;
  }
  const readable = readableTranscriptCount(run);
  return run.sessionCount > 0 ? `${readable}/${run.sessionCount} 份 transcript 可读` : `${readable} 份 transcript 可读`;
}

function runEvidenceLabel(run: EvalLabEvidenceRun): string {
  if (run.evidenceKind === 'transcript_and_report') return '对话 + 报告';
  if (isReportOnlyEvidence(run)) return '回执/报告，原文未公开';
  if (readableTranscriptCount(run)) return '只有对话';
  return '证据缺口';
}

function evidenceTaskIsReportOnly(run: EvalLabEvidenceRun, task: EvalLabEvidenceTask): boolean {
  return task.evidenceStatus === 'report_only' || isReportOnlyEvidence(run);
}

function evidenceTaskIndex(run: EvalLabEvidenceRun, task: EvalLabEvidenceTask): number {
  // The backend reserves taskIndex=0 for a report-only run, even when its
  // summary still contains per-task verifier rows.
  return evidenceTaskIsReportOnly(run, task) ? 0 : task.taskIndex;
}

function sourceMetricSummary(run: EvalLabEvidenceRun): string {
  const metrics = run.metrics;
  const task = metricValue(metrics, 'taskSuccessCount');
  const total = metricValue(metrics, 'taskCount');
  const verifier = metricValue(metrics, 'verifierPassCount');
  const verifierTotal = metricValue(metrics, 'verifierCount');
  if (task !== undefined && total !== undefined && verifier !== undefined && verifierTotal !== undefined) return `任务 ${task}/${total} · Verifier ${verifier}/${verifierTotal}`;
  const answerCoverage = metricValue(metrics, 'answerCoverage');
  const ca = metricValue(metrics, 'ca');
  const jra = metricValue(metrics, 'jra');
  if (answerCoverage !== undefined || ca !== undefined || jra !== undefined) {
    return [
      answerCoverage === undefined ? '' : `覆盖 ${percent(answerCoverage)}`,
      ca === undefined ? '' : `CA ${percent(ca)}`,
      jra === undefined ? '' : `JRA ${percent(jra)}`,
    ].filter(Boolean).join(' · ');
  }
  const recall = metricValue(metrics, 'recallAt10') ?? metricValue(metrics, 'recall_at_10') ?? metricValue(metrics, 'winner.recallAt10') ?? metricValue(metrics, 'winner.recallAt10');
  const mrr = metricValue(metrics, 'mrr') ?? metricValue(metrics, 'winner.mrr');
  const ndcg = metricValue(metrics, 'ndcgAt10') ?? metricValue(metrics, 'ndcg_at_10') ?? metricValue(metrics, 'winner.ndcgAt10');
  if (recall !== undefined || mrr !== undefined || ndcg !== undefined) {
    return [
      recall === undefined ? '' : `Recall ${percent(recall)}`,
      mrr === undefined ? '' : `MRR ${percent(mrr)}`,
      ndcg === undefined ? '' : `nDCG ${percent(ndcg)}`,
    ].filter(Boolean).join(' · ');
  }
  return evidenceAvailabilitySummary(run);
}

function metricValue(metrics: Readonly<Record<string, unknown>>, name: string): number | undefined {
  const value = metrics[name];
  return typeof value === 'number' ? value : undefined;
}

function sourceShortHash(value: string | undefined): string {
  return value ? value.slice(0, 12) : '未记录';
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '0 B';
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function TaskExplanation({ task }: { task: EvalLabTask }) {
  const explanation = task.explanation;
  if (!explanation) return null;
  return (
    <section aria-label={`任务：${task.taskAlias}`} className="eval-lab__explanation">
      <h3>任务：{task.taskAlias}</h3>
      <div className="eval-lab__explanation-grid">
        <ExplanationBlock title="任务">
          <p>{explanation.businessRequest.normalizedText}</p>
        </ExplanationBlock>
        <ExplanationBlock title="Agent 实际输出">
          <p className="eval-lab__actual-output">{explanation.agentOutcome.normalizedSummary}</p>
        </ExplanationBlock>
        <ExplanationBlock title="标准答案 / 验收">
          <p><strong>通过项：</strong>{explanation.acceptance.passed}/{explanation.acceptance.total}</p>
          <p>此处展示脱敏后的验收条件与逐项结论，不展示 raw gold、SQL 或隐藏推理。</p>
        </ExplanationBlock>
      </div>
      <div className="eval-lab__comparison">
        <h4>逐项对比</h4>
        {explanation.acceptance.items.length ? (
          <div className="eval-lab__comparison-table-wrap">
            <table>
              <thead><tr><th scope="col">验收项</th><th scope="col">结果</th><th scope="col">归因</th><th scope="col">说明</th></tr></thead>
              <tbody>{explanation.acceptance.items.map((item, index) => (
                <tr key={`${item.id}-${index}`}>
                  <th scope="row">{item.label}</th>
                  <td><span className={`eval-lab__comparison-result eval-lab__comparison-result--${item.status}`}>{comparisonResultLabel(item.status)}</span></td>
                  <td>{failureOwnerLabel(item.failureOwner)}</td>
                  <td>{item.explanation || '—'}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        ) : <p>没有逐项对比证据。</p>}
      </div>
      <div className="eval-lab__not-full">
        <h4>为什么不是 100%</h4>
        {explanation.acceptance.passed < explanation.acceptance.total ? (
          <ul>{explanation.acceptance.items.filter((item) => item.status !== 'pass').map((item) => <li key={item.id}>{item.explanation || item.label}</li>)}</ul>
        ) : <p>所有已提供的验收项都通过；这不等于系统没有缺陷。</p>}
      </div>
    </section>
  );
}

function ExplanationBlock({ title, children }: { title: string; children: ReactNode }) {
  return <div className="eval-lab__explanation-block"><h4>{title}</h4>{children}</div>;
}

function comparisonResultLabel(result: EvalLabComparisonResult): string {
  if (result === 'pass') return '通过';
  if (result === 'fail') return '未通过';
  if (result === 'partial') return '部分通过';
  return '未知';
}

function failureOwnerLabel(owner: EvalLabFailureOwner): string {
  if (owner === null) return '无失败归因';
  if (owner === 'prompt_context') return '提示 / 上下文';
  if (owner === 'evaluator_gold') return '评测标准';
  if (owner === 'agent') return 'Agent 输出';
  return '未知';
}

function failureOwnerText(owner: string | undefined): string {
  if (!owner) return '';
  const labels: Record<string, string> = {
    none: '无失败项',
    unknown: '未知',
    prompt_workflow_state_conflict: 'Prompt / 状态合同冲突',
    tool_capability_closure: 'Tool 能力未闭合',
    tool_workflow_read_closure: 'Tool / 读取流程未闭合',
    tool_fixture_capability_closure: 'Tool / Fixture 能力未闭合',
    workflow_state_model: '工作流状态模型',
    evaluator_fixture_reachability: '评测 Fixture 可达性',
  };
  return labels[owner] ?? owner.replaceAll('_', ' ');
}

function splitLabel(split: string): string {
  if (split === 'validation') return '调优数据';
  if (split === 'held-out') return '最终盲测';
  if (split === 'shadow_validation') return '隔离影子测试';
  if (split === 'historical_replay') return '历史复盘';
  if (split === 'production') return '生产';
  return split;
}

function datasetSummary(dataset: EvalLabExperiment['dataset']): string {
  const unit = dataset.unit.trim().replace(/^\d+\s*(?:条|个|项)?\s*/u, '');
  return `${dataset.caseCount} 个 case${unit ? ` · ${unit}` : ''}`;
}

type DatasetExplanation = {
  source: string;
  preparation: string;
  gold: string;
  agentContract: string;
  boundary: string;
};

function datasetLeakBoundary(experiment: EvalLabExperiment): string {
  const gold = experiment.scoring.goldHiddenFromAgent
    ? 'Golden / qrels /验收条件对 Agent 隐藏'
    : 'Golden 可见，不能作为盲测证据';
  const heldOut = experiment.dataset.heldOutConsumed
    ? 'Held-out 已消费，禁止重复调参'
    : 'Held-out 未消费';
  return `${gold}；${heldOut}；App 只展示 public-safe 摘要。`;
}

function datasetNegativeContract(experiment: EvalLabExperiment): string {
  const baseline = experiment.baseline.metrics;
  const candidate = experiment.candidate.metrics;
  const refusalCases = firstMetric(candidate, ['infoNotFoundCaseCount', 'abstentionTotal'])
    ?? firstMetric(baseline, ['infoNotFoundCaseCount', 'abstentionTotal']);
  if (refusalCases !== undefined) {
    const label = experiment.evaluationKind === 'memory' ? '拒记负例' : '必须拒答的 info_not_found case';
    return `${integerMetric(refusalCases)} 个${label}；是否正确由 Host 侧 Golden 判定。`;
  }
  return '没有单独公开负例/拒答标签；不得从总 case 数反推，需以逐 Case 验收回执为准。';
}

function currentExperimentBoundary(experiment: EvalLabExperiment, scope: string): string {
  const run = experiment.candidate.runId;
  if (experiment.status === 'open_gap' || candidateEffect(experiment) === 'not_run') {
    return `当前 run ${run} 尚未形成可验收候选；${scope}`;
  }
  if (experiment.status === 'rejected') {
    return `当前 run ${run} 已拒绝：${matrixReliability(experiment)}；${scope}`;
  }
  if (experimentPassesFinalTaskContract(experiment)) {
    return `当前 run ${run} 已通过最终任务与质量硬门禁；${scope}`;
  }
  return `当前 run ${run} 尚未通过最终任务与质量硬门禁：${matrixReliability(experiment)}；${scope}`;
}

function experimentDatasetExplanation(experiment: EvalLabExperiment): DatasetExplanation {
  const project = projectKeyForExperiment(experiment);
  if (project === 'enterpriseops') {
    return {
      source: 'EnterpriseOps-Gym 的客户支持任务；每个 case 都在独立的临时 CSM 数据库中执行。',
      preparation: '固定任务文本、业务日期、允许使用的 Tool、数据库种子和 case 清单，并用哈希防止运行中换题。',
      gold: '由 Host 在 Agent 之外执行数据库终态检查。Validation 这组共 3 个任务、31 条 SQL 验收条件；Agent 看不到查询和期望值。',
      agentContract: '精确保留日期、标题、角色和依赖关系；写入后必须回读。只有数据库终态与验收条件都通过，才算完成。',
      boundary: experiment.dataset.split === 'held-out'
        ? '这是已消费的一次性 Held-out，只能报告该次结果，不能据此继续调参。'
        : '这是 Validation，用来选择候选；通过后仍不能自动宣称生产成功。',
    };
  }
  if (project === 'enterprise-rag') {
    const answerLane = experiment.evaluationKind === 'answer_evidence';
    return {
      source: answerLane
        ? 'EnterpriseRAG-Bench 的冻结 Validation 语料；从 5,101 篇文档和 29,846 个 chunk 中选出 4 个回答协议 case。'
        : '公开 EnterpriseRAG-Bench 的冻结 Validation 子集：5,101 篇文档、29,846 个 chunk、16 个检索问题。',
      preparation: answerLane
        ? '先冻结语料、问题、检索配置与 4 个 case；其中 2 个有答案、2 个故意没有答案，避免“都回答”刷高分。'
        : '把标注相关文档转成隐藏 qrels，并冻结相同候选池、Top-K、索引与查询清单后再比较检索方案。',
      gold: answerLane
        ? '2 个可回答问题共有 9 条必要事实，逐条绑定 source、chunk 和原文；另 2 个 case 的正确行为是明确说找不到。答案正确、引用支持和拒答分别计分。'
        : '公开标注的相关文档是检索金标准；用 Recall@10、MRR 和 nDCG@10 评分，标准答案不放进 Agent 上下文。',
      agentContract: '只根据可回跳的业务原文回答；数字、日期、否定和适用范围不得改写。Room 简报、Agent 消息和指标账本不是业务原文，不能把它们的事件 ID 冒充 sourceId/chunkId；没有原文与绑定关系时必须明确拒答。',
      boundary: isHistoricalExperiment(experiment)
        ? answerLane
          ? '当前只证明检索显著改善；最终回答的引用门禁仍未通过，因此候选被拒绝。'
          : '结果只代表这 16 个 Validation 查询；Graph + Tag 尚无合法企业图，不能伪造对比分数。'
        : currentExperimentBoundary(
          experiment,
          answerLane
            ? '结果只代表这 4 个冻结 Validation case，不自动外推到生产或 Held-out。'
            : '结果只代表这 16 个冻结 Validation 查询；未运行的 Graph + Tag 不生成分数。',
        ),
    };
  }
  if (project === 'cloudops') {
    return {
      source: 'Cloud-OpsBench 中选取的 12 条故障快照，每条绑定只读观测与可用 Tool 清单；这是离线回放评测。',
      preparation: '固定 case、观测数据、模型/Runtime、Tool 地址和评分器；每个候选只改变一项搜索或工具策略。',
      gold: 'Host-only scorer 分别核对首位故障组件（CA）、故障类型（FA）、两者同时命中（JRA）和前三位联合命中（Top-3 JRA）。答案覆盖与工具失败另计；是否阻止采用以本轮验收条件为准。',
      agentContract: '通过登记的 Tool 查询冻结告警、资源状态、日志与可用指标；输出证据摘要及排序后的故障组件与类型。只使用快照实际包含的数据。',
      boundary: isHistoricalExperiment(experiment)
        ? '降低 Tool 次数不能抵消诊断质量下降或一次工具失败，所以更省调用的候选仍被回退。'
        : currentExperimentBoundary(experiment, '结果只代表同一冻结 CloudOps case 与观测快照。'),
    };
  }
  if (project === 'memory') {
    const observedFailure = experiment.experimentId.includes('observed-failure');
    return {
      source: observedFailure
        ? 'PAW 自己的一条真实月度记忆整理失败 Run；它只用于冻结“13.9 分钟后 JSONL 截断”的生产问题。'
        : '从 PAW 真实记忆维护问题抽出的 5 条 public-safe shadow fixture：4 条应该长期保留，1 条只是临时任务。',
      preparation: observedFailure
        ? '只保留脱敏错误、耗时和终态证据；没有把真实私有记忆内容放进展示或训练。'
        : '人工先标注每条是“应保留”还是“应拒记”，固定输入快照；每轮在全新 shadow 数据库运行并验证 rollback/replay。',
      gold: observedFailure
        ? '这条基线只判定是否形成合法 JSON 和持久化回执，不给内容质量打分。'
        : '结构化门禁由程序判：5/5 决策、4/4 可召回、1/1 拒记、向量覆盖、去重、合法 JSON、回滚和重复运行一致；表达质量由人工样例标准复核。',
      agentContract: '严格按人工冻结的“长期保留 / 临时拒记”标准整理；输出合法 JSON，不重复写入，能够回滚并再次读取。',
      boundary: isHistoricalExperiment(experiment) || observedFailure
        ? '真实失败与五例 shadow 不是同一分母，不能直接计算成功率提升。'
        : currentExperimentBoundary(experiment, 'Shadow fixture 不等于生产记忆，也不证明公开 benchmark 泛化。'),
    };
  }
  return {
    source: datasetSummary(experiment.dataset),
    preparation: '按 manifest 固定 case、split 与输入文件。',
    gold: experiment.scoring.goldHiddenFromAgent ? '标准答案由 Host 保存，对 Agent 隐藏。' : '当前合同允许 Agent 看到标准答案。',
    agentContract: '只使用本轮授权的数据与工具；未知就明确写未知，成功只由外部验收回执判定。',
    boundary: boundaryText(experiment.dataset.split),
  };
}

function workflowLabel(value: string): string {
  const labels: Record<string, string> = {
    'execution-chain-v5': '执行链修复（第 5 版）',
    'state-contract-v1': '状态合同工作流（第 1 版）',
    'bounded-evidence-workflow': '受限证据工作流',
    'memory-optimizer-v3': '记忆生命周期 Gate（第 3 版）',
    'baseline-v1': '基线工作流（第 1 版）',
  };
  return labels[value] ?? (value || '未记录工作流');
}

function humanClaimText(value: string): string {
  const normalized = value.trim();
  if (!normalized) return '未记录。';
  // The underlying ledger may keep export-oriented phrasing, but the product
  // surface should describe evidence boundaries rather than a resume claim.
  return normalized
    .replace(/^可表述为/u, '当前证据支持：')
    .replace(/^可以说/u, '当前证据支持：')
    .replace(/^不能表述为/u, '当前证据不支持：')
    .replace(/^不能说/u, '当前证据不支持：')
    .replace(/source-local candidate 尚未安装到当前 PAWOS/gu, '仅在隔离实验环境验证，尚未应用到当前系统')
    .replace(/source-local candidate 未安装到 PAWOS/gu, '仅在隔离实验环境验证，尚未应用到当前系统')
    .replace(/source-local candidate 未安装/gu, '仅在隔离实验环境验证，尚未应用到当前系统')
    .replace(/candidate 尚未安装/gu, '仅在隔离实验环境验证，尚未应用到当前系统')
    .replace(/candidate 未安装/gu, '仅在隔离实验环境验证，尚未应用到当前系统');
}

function boundaryText(split: string): string {
  if (split === 'validation') return '这是 Validation 结果，不代表 Held-out 或生产成功率。';
  if (split === 'held-out') return '这是一次性 Held-out 结果；不得反复用于调参。';
  return `当前证据边界：${splitLabel(split)}。`;
}

function percent(value: number): string {
  return `${(value * 100).toFixed(2)}%`;
}

function duration(milliseconds: number): string {
  const seconds = Math.round(milliseconds / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function moveTabbedSelection<T extends string>(
  event: KeyboardEvent<HTMLButtonElement>,
  currentIndex: number,
  values: readonly T[],
  select: (value: T) => void,
) {
  const nextIndex = event.key === 'ArrowRight' || event.key === 'ArrowDown'
    ? (currentIndex + 1) % values.length
    : event.key === 'ArrowLeft' || event.key === 'ArrowUp'
      ? (currentIndex - 1 + values.length) % values.length
      : event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? values.length - 1
          : null;
  if (nextIndex === null) return;
  event.preventDefault();
  const value = values[nextIndex];
  if (value === undefined) return;
  select(value);
  event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[nextIndex]?.focus();
}

function publicErrorText(_error: unknown): string {
  return '请检查 Runtime 是否提供 agent.eval-lab.runs。';
}

function buildRoomParticipants(experiment: EvalLabExperiment, personas: AgentPersonaV1[]) {
  const plan = roomParticipantPlan(experiment);
  if (personas.length < 2) throw new Error('当前没有足够的 Agent 伙伴可加入 Room。');
  return personas.slice(0, Math.min(plan.length, personas.length)).map((persona, index) => ({
    roleId: persona.roleId,
    roleVersion: persona.version,
    displayName: persona.displayName,
    collaborationRole: plan[index],
  }));
}

function isAgentLabRoom(value: unknown): value is RoomSummary {
  const room = record(value);
  return typeof room.id === 'string'
    && typeof room.title === 'string'
    && Array.isArray(room.participants);
}

function agentLabRoomItems(value: unknown): RoomSummary[] {
  const source = record(value);
  const items = Array.isArray(source.items) ? source.items : [];
  return items.filter(isAgentLabRoom).filter((room) => room.ownerAppId === AGENT_LAB_OWNER_APP_ID);
}

function canResumeAgentLabRoom(room: RoomSummary): boolean {
  if (['archived', 'failed', 'cancelled'].includes(room.status)) return false;
  const latestWorkItem = [...(room.workItems ?? [])].sort((left, right) => right.updatedAtMs - left.updatedAtMs)[0];
  return latestWorkItem?.state !== 'failed' && latestWorkItem?.state !== 'cancelled';
}

function mergeAgentLabRooms(primary: readonly RoomSummary[], secondary: readonly RoomSummary[]): RoomSummary[] {
  const merged = new Map<string, RoomSummary>();
  [...primary, ...secondary].forEach((room) => {
    if (!merged.has(room.id)) merged.set(room.id, room);
  });
  return [...merged.values()];
}

function matchingExperimentRooms(rooms: readonly RoomSummary[], experiment: EvalLabExperiment): RoomSummary[] {
  const surfaceKeys = new Set([
    `experiment.${experiment.experimentId}`.slice(0, 64),
    `candidate.${experiment.experimentId}`.slice(0, 64),
  ]);
  return rooms
    .filter((room) => room.ownerAppId === AGENT_LAB_OWNER_APP_ID && Boolean(room.surfaceKey) && surfaceKeys.has(room.surfaceKey!))
    .sort((left, right) => right.updatedAtMs - left.updatedAtMs);
}

function roomParticipantPlan(experiment: EvalLabExperiment): Array<'coordinator' | 'researcher' | 'implementer' | 'reviewer'> {
  const kind = `${experiment.evaluationKind} ${experiment.title}`.toLocaleLowerCase();
  const layers = `${experiment.star.action} ${experiment.openGaps.join(' ')}`.toLocaleLowerCase();
  if (kind.includes('trace') || kind.includes('diagnos')) return ['coordinator', 'researcher', 'reviewer', 'reviewer'];
  if (kind.includes('retriev') || layers.includes('tool') || layers.includes('workflow') || layers.includes('skill')) return ['coordinator', 'researcher', 'implementer', 'reviewer'];
  return ['coordinator', 'implementer'];
}

function buildEvalLabRoomBrief(experiment: EvalLabExperiment, run?: EvalLabRun): string {
  const linkedRun = run ? `关联真实 Session run：${run.runId}` : '尚未找到匹配的真实 Session run';
  return [
    'Agent Lab 优化简报',
    `评测：${experiment.title} · ${splitLabel(experiment.dataset.split)} · ${experiment.evaluationKind}`,
    `evaluationKind: ${experiment.evaluationKind}`,
    `业务问题：${experiment.businessProblem}`,
    `当前结果：Baseline ${summarizeMetrics(experiment.baseline.metrics)}；Candidate ${summarizeMetrics(experiment.candidate.metrics)}。`,
    `可变实验项：${(experiment.factors ?? []).map((factor) => `${factorLabel(factor.name)}=${factor.after}`).join('；') || '旧版回执未记录'}`,
    `冻结控制：${(experiment.frozenControls ?? []).map((control) => `${control.name}=${control.value}`).join('；') || '旧版回执未记录'}`,
    `修复方向：${experiment.star.action}`,
    linkedRun,
    `证据边界：${humanClaimText(experiment.claim.forbidden)}`,
    '候选执行协议：先由 Coordinator 生成候选卡；每个候选只改一层。用户未明确确认前不得运行 Validation、修改工作区或消费 Held-out。',
    'Trace Reviewer 协议：每位 Reviewer 使用全新 Session/context，只接收脱敏 Trace/Eval evidence envelope、此 Skill 和本轮候选卡；不得读取其他 Reviewer 的私有上下文。Host verifier 才是最终通过/失败依据。',
    '请先核对原始 Session 与脱敏验收投影，再提出最小改动和验证方式。',
  ].join('\n');
}

/** Keep the App Skill directive in the Room's scenario contract, not in the
 * visible user message.  This lets the Room load the Skill while the timeline
 * remains a normal human-readable intake message. */
function publicRoomMessage(brief: string): string {
  return brief.replace(/^\$agent-eval-room-optimizer[^\n]*\n/u, '').trim();
}

function buildEvalLabSkillContext(brief: string, experiment?: EvalLabExperiment): string {
  return [
    '$agent-eval-room-optimizer',
    '这是 Agent Lab 内部方法绑定，不得在用户消息、公开结果或普通 Agent 命令中复述 Skill 名称。',
    experiment ? scenarioTaskContract(experiment) : '向导阶段只补齐数据和验收标准；在项目类型确定前，不生成答案，也不运行评测。',
    brief,
  ].join('\n');
}

function scenarioTaskContract(experiment: EvalLabExperiment): string {
  const project = projectKeyForExperiment(experiment);
  if (project === 'enterprise-rag' || experiment.evaluationKind === 'rag_retrieval' || experiment.evaluationKind === 'answer_evidence') {
    return [
      '本项目工作合同（RAG）：',
      '只依据本轮检索到且可回跳的原始证据回答；不得用常识、模型记忆或隐藏标准答案补齐事实。',
      '数字、日期、否定词、适用范围和限制条件必须与原文一致；每条关键事实都要绑定 sourceId/chunkId。',
      'Room 简报、Agent 消息和指标账本只是控制或摘要证据，不是业务原文；禁止把 room/session/event ID 伪装成 sourceId/chunkId。',
      '没有同时取得原文正文与真实 source/chunk 绑定时，只能输出 info_not_found，不得生成形式化引用。',
      '证据缺失、冲突或不足时明确输出 info_not_found / 需要人工确认，禁止编造。',
    ].join('\n');
  }
  if (project === 'enterpriseops') {
    return [
      '本项目工作合同（企业执行链）：',
      '任务是否完成只看业务 Tool 回执、数据库最终状态和 Host 验收条件；Agent 自述不算完成。',
      '保留任务中的精确标题、日期、角色和依赖关系；写入后必须回读，失败时停止并留下可清理状态。',
    ].join('\n');
  }
  if (project === 'cloudops') {
    return [
      '本项目工作合同（故障诊断）：',
      '结论只来自冻结的日志、指标、Trace 和 runbook；观察、推断和未知必须分开写。',
      '没有观测证据时不得猜根因；Tool failure 单独报告，不能用少调用或快一点抵消诊断错误。',
    ].join('\n');
  }
  if (project === 'memory') {
    return [
      '本项目工作合同（记忆整理）：',
      '严格按照人工冻结的“长期保留 / 临时拒记”样例判断，不把短期任务、重复内容或私密原文扩写成长期事实。',
      '输出必须符合固定 JSON schema；同一输入重复运行不得新增重复记忆，并且必须能回滚、再读取和复验。',
    ].join('\n');
  }
  return '本项目工作合同：只使用本轮授权的数据和工具；未知就明确写未知，成功只由 Host 验收回执判定。';
}

function summarizeMetrics(metrics: Readonly<Record<string, number>>): string {
  return Object.entries(metrics).slice(0, 3).map(([name, value]) => `${metricLabel(name)}=${formatMetricByName(name, value)}`).join('、') || '暂无';
}

function factorLabel(name: string): string {
  const labels: Record<string, string> = {
    model: '模型',
    prompt: 'Prompt',
    skill: 'Skill',
    tool: 'Tool',
    workflow: '工作流',
    context: 'Context',
    baseline: '基线',
    memory_rag: 'Memory / RAG',
    guardrail: 'Guardrail',
    execution_policy: '执行策略',
    human_loop: '人工审核',
    pricing: '价格口径',
  };
  return labels[name] ?? name;
}

function controlLabel(name: string): string {
  const labels: Record<string, string> = {
    validation_cases: 'Validation 任务与验收项',
    validation: 'Validation 范围',
    split: '数据分片',
    suite: '评测套件',
    cases: '任务数量',
    seed_and_gold: '数据种子与标准答案',
    gold_and_seed: '数据种子与标准答案',
    runtime_identity: '运行时身份',
    held_out: 'Held-out 状态',
    run_receipt: '运行回执',
    corpus: '知识库版本',
    query_set: '问题集',
    pricing: '价格快照',
    shadow_data: 'Shadow 数据',
    claim_boundary: '结论边界',
    usage_authority: 'Usage 权威来源',
    task_and_split: '任务与数据分片',
    agent_contract: 'Agent 合同',
    validation_suite: 'Validation 套件',
    heldout_and_cost: 'Held-out 与成本',
    replay_cases: '回放案例',
    gold_boundary: '标准答案边界',
    promotion_gate: '晋级门禁',
  };
  return labels[name] ?? name.replaceAll('_', ' ');
}

function verticalLabel(value: string): string {
  const normalized = normalizeId(value);
  const labels: Record<string, string> = {
    cloudops: 'CloudOps',
    'enterprise-rag': 'Enterprise RAG',
    enterpriseops: 'EnterpriseOps',
    'enterpriseops-csm': 'EnterpriseOps CSM',
    'trace-agent': 'Trace Agent',
  };
  if (labels[normalized]) return labels[normalized];
  if (normalized.includes('memory') || normalized.includes('personal')) return '个人记忆整理';
  if (normalized.includes('knowledge') || normalized.includes('rag')) return '企业知识检索';
  if (normalized.includes('customer-support') || normalized.includes('enterpriseops')) return '企业客户支持';
  if (normalized.includes('cloudops') || normalized.includes('incident')) return '云上故障处理';
  if (normalized.includes('trace')) return 'Trace 诊断';
  return value || '未命名场景';
}

function matchingRuns(experiment: EvalLabExperiment, runs: readonly EvalLabRun[]): EvalLabRun[] {
  const runIds = new Set([experiment.baseline.runId, experiment.candidate.runId]);
  return runs.filter((run) => runIds.has(run.runId) || isConservativeRunAlias(experiment, run));
}

function isConservativeRunAlias(experiment: EvalLabExperiment, run: EvalLabRun): boolean {
  if (run.split !== experiment.dataset.split || run.taskCount !== experiment.dataset.caseCount) return false;
  const vertical = normalizeId(experiment.vertical);
  if (!vertical || !hasFamilyPrefix(run.suiteId, vertical) || !experimentRunIds(experiment).some((id) => hasFamilyPrefix(id, vertical))) return false;
  const expectedDates = new Set(experimentRunIds(experiment).flatMap(extractDate));
  const actualDates = extractDate(run.runId);
  return expectedDates.size === 0 || actualDates.some((date) => expectedDates.has(date));
}

function experimentRunIds(experiment: EvalLabExperiment): string[] {
  return [experiment.baseline.runId, experiment.candidate.runId];
}

function normalizeId(value: string): string {
  return value.toLocaleLowerCase().replace(/[^a-z0-9]+/gu, '-').replace(/^-|-$/gu, '');
}

function hasFamilyPrefix(value: string, family: string): boolean {
  const normalized = normalizeId(value);
  return normalized === family || normalized.startsWith(`${family}-`);
}

function extractDate(value: string): string[] {
  return normalizeId(value).match(/(?:^|-)20\d{6}(?=-|$)/gu)?.map((date) => date.replace(/^-/, '')) ?? [];
}

function fallbackExperiment(run: EvalLabRun): EvalLabExperiment {
  return {
    schemaVersion: 'rag-ime.agent-lab-experiment.v1',
    experimentId: `run-${run.runId}`,
    revisionSha256: run.sourceReportSha256,
    title: run.title,
    vertical: run.suiteId,
    evaluationKind: 'other',
    status: 'diagnostic',
    claimStatus: 'diagnostic',
    businessProblem: '该 Session run 尚未关联到实验账本。',
    whyAgent: '需要打开真实 Session 核对执行过程。',
    dataset: { datasetId: run.suiteId, split: run.split, caseCount: run.taskCount, unit: '真实 Session run', manifestSha256: run.sourceReportSha256, heldOutConsumed: run.split === 'held-out' },
    scoring: { primaryMetric: '当前 run 指标', evaluatorAuthority: '当前 run receipt', goldHiddenFromAgent: true, hardGates: [] },
    factors: [{ name: 'workflow', before: '未关联实验', after: '待在 Agent Lab 中确定', reason: '当前 run 没有公开的变量/控制投影。' }],
    frozenControls: [{ name: 'run_receipt', value: run.sourceReportSha256, reason: '仅允许依据已持久化回执诊断。' }],
    baseline: { runId: run.runId, metrics: { taskSuccessRate: run.taskSuccessRate }, evidenceRefs: [run.sourceReportSha256] },
    candidate: { runId: run.runId, metrics: { taskSuccessRate: run.taskSuccessRate }, evidenceRefs: [run.sourceReportSha256] },
    comparison: { decision: 'diagnostic', decisionReason: '尚未关联 experiment。', metricDeltas: [] },
    star: { situation: run.title, task: '核对真实 Session。', action: '先从证据和失败项开始。', result: '待关联实验。' },
    claim: { resumeBullet: '仅为未关联 run 的诊断入口。', allowed: '当前 run receipt。', forbidden: 'Production 或 Held-out 结论。' },
    openGaps: ['缺少匹配 experiment。'],
    importedAtMs: run.updatedAtMs,
  };
}

function statusLabel(status: EvalLabExperiment['status']): string {
  return status === 'kept' ? '保留候选' : status === 'rejected' ? '淘汰候选' : status === 'open_gap' ? '待补证据' : '仅记录';
}

function formatMetric(value: number): string {
  return Number.isInteger(value) ? value.toLocaleString('en-US') : value.toLocaleString('en-US', { maximumFractionDigits: 4 });
}

function record(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
