import {
  ArrowRight,
  ChevronDown,
  ChevronLeft,
  CircleAlert,
  CircleDashed,
  ExternalLink,
  FileText,
  FolderTree,
  GitBranch,
  LoaderCircle,
  Network,
  PencilLine,
  PanelsTopLeft,
  Plus,
  RefreshCw,
} from 'lucide-react';
import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from 'react';
import '../styles/paw-os-workbench.css';

/**
 * THESIS: Workbench is a live project dependency field, never a summary-card dashboard.
 * SURFACE: White reading areas, quiet gray structure, one restrained blue identity and task-derived dependency geometry.
 * STORY: Answer "what is the next unresolved thing" first, then inspect task ownership and dependencies, then open the authoritative task or WorkDocument.
 * WINDOW SHAPE: Every page is fixed chrome around one flexible workspace, the way a native window App behaves — command deck on top, a status ledger at the
 * bottom, and working panes that own their own scroll in between. Nothing stacks a page of plates that pushes the work below the first viewport.
 * FIRST VIEWPORT: The console band answers the next unresolved thing in one strip, then the task and WorkDocument panes are already on screen.
 * FORM: Archive-led Operate extension, pawos-workbench-v1.
 */

export const PAW_WORKBENCH_MIGRATED_ROUTES = {
  overview: '/overview',
  planning: '/planning',
  documents: '/work-documents',
} as const;

export type PawWorkbenchPageId = keyof typeof PAW_WORKBENCH_MIGRATED_ROUTES;
export type PawWorkbenchRecord = Record<string, unknown>;
export type PawWorkbenchResourceKey = 'overview' | 'planning' | 'documents' | 'documentDetail';
export type PawWorkbenchDocumentScope = 'active' | 'history';

export interface PawWorkbenchResourceState {
  loading?: boolean;
  error?: string;
}

export interface PawWorkbenchProps {
  pageId: PawWorkbenchPageId;
  overview: PawWorkbenchRecord;
  planning: PawWorkbenchRecord;
  documents: readonly PawWorkbenchRecord[];
  documentTotal?: number;
  selectedDocument: PawWorkbenchRecord | null;
  resourceStates?: Partial<Record<PawWorkbenchResourceKey, PawWorkbenchResourceState>>;
  onOpenTask: (task: PawWorkbenchRecord) => void;
  onEditTask?: (task: PawWorkbenchRecord) => void;
  onCreateGoal?: () => void;
  onEditGoal?: (goal: PawWorkbenchRecord) => void;
  onOpenDocument: (document: PawWorkbenchRecord) => void | Promise<void>;
  onOpenDocumentWindow?: (document: PawWorkbenchRecord) => void;
  onCloseDocument: () => void;
  documentLifecycle?: ReactNode;
  documentScope?: PawWorkbenchDocumentScope;
  documentHistoryQuery?: string;
  documentHistorySupported?: boolean;
  onDocumentScopeChange?: (scope: PawWorkbenchDocumentScope) => void;
  onDocumentHistoryQueryChange?: (query: string) => void;
  planningTools?: ReactNode | ((selectedTask: PawWorkbenchRecord | null) => ReactNode);
  primaryAction?: { label: string; onClick: () => void };
  onNavigate?: (pageId: PawWorkbenchPageId) => void;
  onRefresh?: (resource: PawWorkbenchResourceKey) => void;
}

type TaskLane = 'done' | 'active' | 'review' | 'blocked' | 'todo';

interface TaskGraphNode {
  id: string;
  lane: TaskLane;
  task: PawWorkbenchRecord;
  x: number;
  y: number;
}

interface TaskGraphEdge {
  id: string;
  path: string;
  active: boolean;
}

interface TaskGraphLane {
  count: number;
  lane: TaskLane;
  x: number;
}

const GRAPH_LANES: readonly TaskLane[] = ['done', 'active', 'review', 'blocked', 'todo'];
const GRAPH_LANE_LABELS: Record<TaskLane, string> = {
  done: '已完成',
  active: '进行中',
  review: '待验收',
  blocked: '受阻',
  todo: '待办',
};
const GRAPH_NODE_WIDTH = 188;
const GRAPH_NODE_HEIGHT = 82;
const GRAPH_COLUMN_GAP = 42;
const GRAPH_ROW_GAP = 28;
const GRAPH_PADDING_X = 28;
const GRAPH_PADDING_Y = 62;

export function PawWorkbench({
  pageId,
  overview,
  planning,
  documents,
  documentTotal,
  selectedDocument,
  resourceStates = {},
  onOpenTask,
  onEditTask,
  onCreateGoal,
  onEditGoal,
  onOpenDocument,
  onOpenDocumentWindow,
  onCloseDocument,
  documentLifecycle,
  documentScope = 'active',
  documentHistoryQuery = '',
  documentHistorySupported = false,
  onDocumentScopeChange,
  onDocumentHistoryQueryChange,
  planningTools,
  primaryAction,
  onNavigate,
  onRefresh,
}: PawWorkbenchProps) {
  const project = record(overview.project);
  const plan = record(planning.plan);
  const tasks = rows(planning, ['tasks', 'items']);
  const goals = rows(planning, ['goals']);
  const projectPath = text(project.path)
    || text(overview.projectPath)
    || text(plan.workspaceRoot)
    || documents.map((document) => text(document.workspaceRoot)).find(Boolean)
    || '';
  const projectName = text(project.name)
    || text(overview.projectName)
    || text(plan.project)
    || text(planning.project)
    || pathName(projectPath)
    || '未选择项目';
  const [selectedTaskId, setSelectedTaskId] = useState('');
  const [openedOverviewDocument, setOpenedOverviewDocument] = useState<PawWorkbenchRecord | null>(null);

  useEffect(() => {
    if (pageId !== 'overview') setOpenedOverviewDocument(null);
  }, [pageId]);

  useEffect(() => {
    if (tasks.some((task, index) => taskId(task, index) === selectedTaskId)) return;
    const firstActiveIndex = tasks.findIndex((task) => taskLane(task) === 'active');
    const nextIndex = firstActiveIndex >= 0 ? firstActiveIndex : 0;
    setSelectedTaskId(tasks[nextIndex] ? taskId(tasks[nextIndex], nextIndex) : '');
  }, [selectedTaskId, tasks]);

  const selectedTask = tasks.find((task, index) => taskId(task, index) === selectedTaskId) ?? null;
  const selectTask = (task: PawWorkbenchRecord, index: number) => {
    setSelectedTaskId(taskId(task, index));
  };
  const openTask = (task: PawWorkbenchRecord, index: number) => {
    setSelectedTaskId(taskId(task, index));
    onOpenTask(task);
  };
  const openDocument = async (document: PawWorkbenchRecord): Promise<void> => {
    if (pageId === 'overview') setOpenedOverviewDocument(document);
    await onOpenDocument(document);
  };
  const closeDocument = () => {
    setOpenedOverviewDocument(null);
    onCloseDocument();
  };
  const activePageId: PawWorkbenchPageId = pageId === 'overview' && openedOverviewDocument ? 'documents' : pageId;
  const activeDocument = openedOverviewDocument
    ? (selectedDocument && sameDocument(selectedDocument, openedOverviewDocument) ? selectedDocument : openedOverviewDocument)
    : selectedDocument;

  return (
    <section className="paw-workbench" data-page-id={activePageId}>
      <span aria-hidden data-paw-workbench-direction="pawos-workbench-v1" hidden />
      <WorkbenchChrome
        pageId={activePageId}
        primaryAction={primaryAction}
        projectName={projectName}
        projectPath={projectPath}
      />
      {activePageId === 'overview' ? (
        <ProjectOverview
          documents={documents}
          documentTotal={documentTotal}
          documentsState={resourceStates.documents}
          goals={goals}
          onOpenDocument={openDocument}
          onOpenTask={openTask}
          onNavigate={onNavigate}
          onRefresh={onRefresh}
          overview={overview}
          overviewState={resourceStates.overview}
          planning={planning}
          planningState={resourceStates.planning}
          project={project}
          projectName={projectName}
          projectPath={projectPath}
          tasks={tasks}
        />
      ) : null}
      {activePageId === 'planning' ? (
        <div className="paw-wb-planning">
          {typeof planningTools === 'function' ? planningTools(selectedTask) : planningTools}
          <TaskOrchestration
            goals={goals}
            onCreateGoal={onCreateGoal}
            onEditGoal={onEditGoal}
            onEditTask={onEditTask}
            onOpenTask={onOpenTask}
            onRefresh={onRefresh}
            onSelectTask={selectTask}
            projectName={projectName}
            resourceState={resourceStates.planning}
            selectedTask={selectedTask}
            selectedTaskId={selectedTaskId}
            tasks={tasks}
          />
        </div>
      ) : null}
      {activePageId === 'documents' ? (
        <WorkDocumentWorkspace
          documents={documents}
          documentTotal={documentTotal}
          documentHistoryQuery={documentHistoryQuery}
          documentHistorySupported={documentHistorySupported}
          documentLifecycle={documentLifecycle}
          documentScope={documentScope}
          detailState={resourceStates.documentDetail}
          listState={resourceStates.documents}
          onCloseDocument={closeDocument}
          onOpenDocument={openDocument}
          onOpenDocumentWindow={onOpenDocumentWindow}
          onDocumentHistoryQueryChange={onDocumentHistoryQueryChange}
          onDocumentScopeChange={onDocumentScopeChange}
          onRefresh={onRefresh}
          projectName={projectName}
          selectedDocument={activeDocument}
        />
      ) : null}
    </section>
  );
}

const WORKBENCH_PAGES: Record<PawWorkbenchPageId, { label: string; intent: string }> = {
  overview: { label: '项目概览', intent: '先处理最需要处理的事' },
  planning: { label: '任务', intent: '编排任务与真实依赖' },
  documents: { label: '工作文档', intent: '打开权威 WorkDocument' },
};

/** The host owns page navigation; this strip keeps purpose and the primary action. */
function WorkbenchChrome({
  pageId,
  projectName,
  projectPath,
  primaryAction,
}: {
  pageId: PawWorkbenchPageId;
  projectName: string;
  projectPath: string;
  primaryAction?: PawWorkbenchProps['primaryAction'];
}) {
  const page = WORKBENCH_PAGES[pageId];
  return (
    <header className="paw-wb-chrome">
      <div className="paw-wb-chrome__purpose">
        <h1>{page.label}</h1>
        <p>{page.intent}</p>
      </div>
      <span aria-hidden className="paw-wb-chrome__spacer" />
      {primaryAction ? (
        <button aria-label={primaryAction.label} className="paw-wb-primary" onClick={primaryAction.onClick} type="button">
          {pageId === 'overview' ? <ArrowRight aria-hidden size={15} /> : <Plus aria-hidden size={15} />}
          <span>{primaryAction.label}</span>
        </button>
      ) : null}
      <div className="paw-wb-chrome__identity">
        <span>
          <strong title={projectName}>{projectName}</strong>
          <small title={projectPath || page.label}>{projectPath || page.label}</small>
        </span>
      </div>
    </header>
  );
}

function ProjectOverview({
  documents,
  documentTotal,
  documentsState,
  goals,
  onOpenDocument,
  onOpenTask,
  onNavigate,
  onRefresh,
  overview,
  overviewState,
  planning,
  planningState,
  project,
  projectName,
  projectPath,
  tasks,
}: {
  documents: readonly PawWorkbenchRecord[];
  documentTotal?: number;
  documentsState?: PawWorkbenchResourceState;
  goals: PawWorkbenchRecord[];
  onOpenDocument: PawWorkbenchProps['onOpenDocument'];
  onOpenTask: (task: PawWorkbenchRecord, index: number) => void;
  onNavigate?: PawWorkbenchProps['onNavigate'];
  onRefresh?: PawWorkbenchProps['onRefresh'];
  overview: PawWorkbenchRecord;
  overviewState?: PawWorkbenchResourceState;
  planning: PawWorkbenchRecord;
  planningState?: PawWorkbenchResourceState;
  project: PawWorkbenchRecord;
  projectName: string;
  projectPath: string;
  tasks: PawWorkbenchRecord[];
}) {
  const metrics = overviewMetrics(overview, planning, documents, documentsState);
  const resolvedDocumentTotal = Math.max(documents.length, documentTotal ?? 0);
  const [visibleTaskCount, setVisibleTaskCount] = useState(8);
  const [visibleDocumentCount, setVisibleDocumentCount] = useState(6);
  const displayedTasks = tasks.slice(0, Math.min(tasks.length, visibleTaskCount));
  const displayedDocuments = documents.slice(0, Math.min(documents.length, visibleDocumentCount));
  const remainingTasks = Math.max(0, tasks.length - displayedTasks.length);
  const remainingLoadedDocuments = Math.max(0, documents.length - displayedDocuments.length);
  const remainingUnloadedDocuments = Math.max(0, resolvedDocumentTotal - documents.length);
  const repoFacts: { label: string; value: string; mono?: boolean }[] = [
    { label: '分支', value: text(project.branch) || text(overview.branch) },
    { label: '状态', value: stateLabel(text(project.status) || text(overview.status)) },
    { label: 'Revision', value: text(project.revision) || text(overview.revision), mono: true },
  ].filter((fact) => Boolean(fact.value));
  return (
    <div className="paw-wb-overview">
      <ResourceNotice label="项目概览" onRefresh={onRefresh ? () => onRefresh('overview') : undefined} state={overviewState} />
      {resourceSettled(planningState) ? (
        <NowBand
          documents={documents}
          documentsSettled={resourceSettled(documentsState)}
          onOpenTask={onOpenTask}
          tasks={tasks}
        />
      ) : null}

      {/* The working panes are the window's flexible band: each owns its own
          scroll so the first viewport always holds real tasks and documents. */}
      <div className="paw-wb-overview__workspace" data-tasks-empty={resourceSettled(planningState) && !tasks.length || undefined}>
        {!resourceSettled(planningState) || tasks.length ? (
          <section className="paw-wb-pane" data-pane="tasks">
            <header>
              <div><GitBranch aria-hidden size={16} /><h2>当前工作</h2></div>
              <div className="paw-wb-pane__scope">
                <span>{resourceCountLabel(planningState, tasks.length > 8 ? `当前 ${displayedTasks.length} / 共 ${tasks.length}` : `${tasks.length} 项`)}</span>
              </div>
            </header>
            <ResourceNotice label="任务" onRefresh={onRefresh ? () => onRefresh('planning') : undefined} state={planningState} />
            {tasks.length ? (
              <ol className="paw-wb-pane__list" data-scrollable={displayedTasks.length > 8 || undefined}>
                {displayedTasks.map((task, index) => (
                  <li key={taskId(task, index)}>
                    <button onClick={() => onOpenTask(task, index)} title={`${taskTitle(task)} · ${taskMeta(task)}`} type="button">
                      <StatusMark lane={taskLane(task)} />
                      <span><strong>{taskTitle(task)}</strong><small>{taskMeta(task)}</small></span>
                      <em>{taskStateLabel(task)}</em>
                    </button>
                  </li>
                ))}
              </ol>
          ) : null}
          {remainingTasks ? <button className="paw-wb-pane__more" aria-label={`显示更多任务：${remainingTasks} 项`} onClick={() => setVisibleTaskCount((value) => value + 8)} type="button">显示更多 {Math.min(8, remainingTasks)} 项</button> : null}
        </section>
        ) : null}

        <div className="paw-wb-overview__side">
          <section className="paw-wb-pane" data-pane="documents">
            <header>
              <div><FileText aria-hidden size={16} /><h2>工作文档</h2></div>
              <div className="paw-wb-pane__scope">
                <span>{resourceCountLabel(documentsState, resolvedDocumentTotal > 6 ? `当前 ${displayedDocuments.length} / 已加载 ${documents.length}${remainingUnloadedDocuments ? ` · 共 ${resolvedDocumentTotal}` : ''}` : `${resolvedDocumentTotal} 份`)}</span>
              </div>
            </header>
            <ResourceNotice label="工作文档" onRefresh={onRefresh ? () => onRefresh('documents') : undefined} state={documentsState} />
            {documents.length ? (
              <ol className="paw-wb-pane__rows" data-scrollable={displayedDocuments.length > 6 || undefined}>
                {displayedDocuments.map((document, index) => (
                  <li key={documentId(document, index)}>
                    <button onClick={() => void onOpenDocument(document)} title={`${documentTitle(document)} · ${authorityLabel(text(document.authorityKind))}`} type="button">
                      <FileText aria-hidden size={15} />
                      <span><strong>{documentTitle(document)}</strong><small>{authorityLabel(text(document.authorityKind))} · {updatedLabel(number(document.updatedAtMs))}</small></span>
                      <StatusMark lane={documentLane(document)} />
                    </button>
                  </li>
                ))}
              </ol>
            ) : resourceSettled(documentsState) ? <EmptyState icon={<FileText size={22} />} title="暂无工作文档" copy="这里只显示 Runtime 已登记的 WorkDocument。" /> : null}
            {remainingLoadedDocuments ? <button className="paw-wb-pane__more" aria-label={`显示更多工作文档：${remainingLoadedDocuments} 项`} onClick={() => setVisibleDocumentCount((value) => value + 6)} type="button">显示已加载的更多 {Math.min(6, remainingLoadedDocuments)} 项</button> : null}
            {!remainingLoadedDocuments && remainingUnloadedDocuments ? (
              onNavigate ? <button className="paw-wb-pane__more" aria-label={`在工作文档中查看其余 ${remainingUnloadedDocuments} 项`} onClick={() => onNavigate('documents')} type="button">在工作文档中查看其余 {remainingUnloadedDocuments} 项<ArrowRight aria-hidden size={13} /></button> : <p className="paw-wb-pane__boundary">已加载全部 {documents.length} 项；其余 {remainingUnloadedDocuments} 项尚未加载。</p>
            ) : null}
          </section>

          {goals.length ? (
            <section className="paw-wb-pane" data-pane="goals">
              <header>
                <div><Network aria-hidden size={16} /><h2>目标层级</h2></div>
                <div className="paw-wb-pane__scope"><span>{goals.length} 项</span></div>
              </header>
              <ol className="paw-wb-pane__goals">{goals.map((goal, index) => <li key={text(goal.id) || `goal-${index}`}><StatusMark lane={taskLane(goal)} /><span><strong title={text(goal.title) || '未命名目标'}>{text(goal.title) || '未命名目标'}</strong>{text(goal.detail) ? <small title={text(goal.detail)}>{text(goal.detail)}</small> : null}</span><em>{stateLabel(text(goal.status))}</em></li>)}</ol>
            </section>
          ) : null}
        </div>
      </div>

      {/* Status ledger, not a project hero: workspace identity and the numbers
          Runtime actually reported sit on the window's bottom rail. */}
      <footer className="paw-wb-ledger">
        <span aria-hidden className="paw-wb-ledger__mark"><FolderTree size={15} /></span>
        <span className="paw-wb-ledger__identity">
          <strong title={projectName}>{projectName}</strong>
          {projectPath
            ? <small className="paw-wb-mono" title={projectPath}>{projectPath}</small>
            : <small>当前项目尚未提供工作区路径。</small>}
        </span>
        {metrics.length ? <dl className="paw-wb-metrics" aria-label="项目真实指标">{metrics.map((metric) => <Fact key={metric.label} label={metric.label} value={metric.value} />)}</dl> : null}
        {repoFacts.length ? (
          <details className="paw-wb-repo">
            <summary>
              <ChevronDown aria-hidden size={13} />
              仓库与运行事实
            </summary>
            <dl className="paw-wb-project-facts">
              {repoFacts.map((fact) => <Fact key={fact.label} label={fact.label} mono={fact.mono} value={fact.value} />)}
            </dl>
          </details>
        ) : null}
      </footer>
    </div>
  );
}

/**
 * The overview leads with the next unresolved thing, before any metric.
 * Every value here is derived from real planning tasks and real registered
 * WorkDocuments; unresolved reads never masquerade as truthful zeros.
 * Cross-page navigation lives in the command deck above, so the band keeps
 * exactly one action: open the next unresolved task.
 */
function NowBand({
  documents,
  documentsSettled,
  onOpenTask,
  tasks,
}: {
  documents: readonly PawWorkbenchRecord[];
  documentsSettled: boolean;
  onOpenTask: (task: PawWorkbenchRecord, index: number) => void;
  tasks: PawWorkbenchRecord[];
}) {
  const next = nextUnresolvedTask(tasks);
  const laneCount = (lane: TaskLane) => tasks.filter((task) => taskLane(task) === lane).length;
  const blockedCount = laneCount('blocked');
  const reviewCount = laneCount('review');
  const activeCount = laneCount('active');
  const doneCount = laneCount('done');
  const donePercent = tasks.length ? Math.round((doneCount / tasks.length) * 100) : 0;
  const latestDocumentMs = documents.reduce((latest, document) => Math.max(latest, number(document.updatedAtMs)), 0);
  const evidenceValue = documentsSettled ? (latestDocumentMs ? updatedLabel(latestDocumentMs) : '暂无文档') : '读取中';
  return (
    <section aria-label="当前最需要处理的工作" className="paw-wb-now" data-state={next ? next.lane : tasks.length ? 'clear' : 'empty'}>
      <div className="paw-wb-now__lead">
        {next ? (
          <>
            <h2 title={taskTitle(next.task)}>{taskTitle(next.task)}</h2>
            <p>{taskMeta(next.task)} · {taskStateLabel(next.task)}</p>
          </>
        ) : (
          <>
            <h2>{tasks.length ? '未完成工作已清零' : '还没有可执行任务'}</h2>
            <p>{tasks.length ? '当前计划的任务都已完成，可以规划下一步或复盘。' : '在任务编排里写下第一个可执行的下一步。'}</p>
          </>
        )}
        {next ? (
          <div className="paw-wb-now__actions">
            <button className="paw-wb-now__primary" onClick={() => onOpenTask(next.task, next.index)} type="button">
              打开任务窗口<ArrowRight aria-hidden size={14} />
            </button>
          </div>
        ) : null}
      </div>
      <dl aria-label="当前计划状态" className="paw-wb-now__pulse">
        {tasks.length ? <div><dt>已完成</dt><dd>{doneCount} / {tasks.length} 项 · {donePercent}%</dd></div> : null}
        <div data-tone={blockedCount ? 'blocked' : undefined}><dt>受阻</dt><dd>{blockedCount}</dd></div>
        <div data-tone={reviewCount ? 'review' : undefined}><dt>待验收</dt><dd>{reviewCount}</dd></div>
        <div data-tone={activeCount ? 'active' : undefined}><dt>进行中</dt><dd>{activeCount}</dd></div>
        <div><dt>证据更新</dt><dd>{evidenceValue}</dd></div>
      </dl>
    </section>
  );
}

function TaskOrchestration({
  goals,
  onCreateGoal,
  onEditGoal,
  onEditTask,
  onOpenTask,
  onRefresh,
  onSelectTask,
  projectName,
  resourceState,
  selectedTask,
  selectedTaskId,
  tasks,
}: {
  goals: PawWorkbenchRecord[];
  onCreateGoal?: PawWorkbenchProps['onCreateGoal'];
  onEditGoal?: PawWorkbenchProps['onEditGoal'];
  onEditTask?: PawWorkbenchProps['onEditTask'];
  onOpenTask: PawWorkbenchProps['onOpenTask'];
  onRefresh?: PawWorkbenchProps['onRefresh'];
  onSelectTask: (task: PawWorkbenchRecord, index: number) => void;
  projectName: string;
  resourceState?: PawWorkbenchResourceState;
  selectedTask: PawWorkbenchRecord | null;
  selectedTaskId: string;
  tasks: PawWorkbenchRecord[];
}) {
  const graph = useMemo(() => buildTaskGraph(tasks), [tasks]);
  const [taskQuery, setTaskQuery] = useState('');
  const trimmedTaskQuery = taskQuery.trim();
  const outlineTasks = trimmedTaskQuery ? tasks.filter((task) => matchesTask(task, trimmedTaskQuery)) : tasks;
  const selectedLane = selectedTask ? taskLane(selectedTask) : null;
  return (
    <div className="paw-wb-orchestration">
      <aside className="paw-wb-outline">
        <header>
          <FolderTree aria-hidden size={16} />
          <strong>目标与任务</strong>
          {onCreateGoal ? <button aria-label="添加目标" onClick={onCreateGoal} type="button"><Plus aria-hidden size={14} /></button> : null}
        </header>
        <div className="paw-wb-outline__project"><strong title={projectName}>{projectName}</strong><small>{resourceCountLabel(resourceState, `${tasks.length} 项`)}</small></div>
        {goals.length ? <ol className="paw-wb-outline__goals">{goals.map((goal, index) => {
          const title = text(goal.title) || '未命名目标';
          return <li key={text(goal.id) || `goal-${index}`}>
            {onEditGoal ? (
              <button aria-label={`编辑目标：${title}`} onClick={() => onEditGoal(goal)} type="button">
                <StatusMark lane={taskLane(goal)} /><span>{title}</span><PencilLine aria-hidden size={13} />
              </button>
            ) : <><StatusMark lane={taskLane(goal)} /><span>{title}</span></>}
          </li>;
        })}</ol> : null}
        {tasks.length > 5 || taskQuery ? (
          <input
            aria-label="筛选任务"
            className="paw-wb-outline__filter"
            onChange={(event) => setTaskQuery(event.target.value)}
            placeholder="标题、负责人或状态"
            type="search"
            value={taskQuery}
          />
        ) : null}
        {outlineTasks.length ? (
          <ol className="paw-wb-outline__tasks">{outlineTasks.map((task) => {
            const index = tasks.indexOf(task);
            return <li key={taskId(task, index)}><button aria-label={`在任务列表中选择：${taskTitle(task)}`} aria-current={taskId(task, index) === selectedTaskId || undefined} onClick={() => onSelectTask(task, index)} title={`${taskTitle(task)} · ${taskMeta(task)}`} type="button"><StatusMark lane={taskLane(task)} /><span>{taskTitle(task)}</span><small>{taskProgressLabel(task)}</small></button></li>;
          })}</ol>
        ) : trimmedTaskQuery ? <div className="paw-wb-filter-empty"><p>没有匹配的任务。</p><button onClick={() => setTaskQuery('')} type="button">清除任务筛选</button></div> : null}
      </aside>

      <section aria-label="真实任务依赖图" className="paw-wb-graph" role="region">
        <header>
          <div><GitBranch aria-hidden size={16} /><strong>依赖图</strong><span>{resourceCountLabel(resourceState, `${tasks.length} 节点 · ${graph.edges.length} 条真实依赖`)}</span></div>
        </header>
        <ResourceNotice label="任务编排" onRefresh={onRefresh ? () => onRefresh('planning') : undefined} state={resourceState} />
        {tasks.length ? (
          <div className="paw-wb-graph__viewport">
            <div className="paw-wb-graph__field" style={{ height: graph.height, width: graph.width }}>
              {/* Lane ownership is drawn, not implied: each populated state owns a
                  tinted column, and its header rides the vertical scroll. */}
              <div aria-hidden className="paw-wb-graph__bands">
                {graph.lanes.map((lane) => (
                  <span
                    className="paw-wb-graph__band"
                    data-current={lane.lane === selectedLane || undefined}
                    data-lane={lane.lane}
                    key={lane.lane}
                    style={{ '--paw-wb-lane-x': `${lane.x}px` } as CSSProperties}
                  />
                ))}
              </div>
              <svg aria-hidden className="paw-wb-graph__edges" height={graph.height} viewBox={`0 0 ${graph.width} ${graph.height}`} width={graph.width}>
                {graph.edges.map((edge) => <path className="paw-wb-graph__edge" d={edge.path} data-active={edge.active || undefined} key={edge.id} />)}
                {graph.edges.filter((edge) => edge.active).map((edge) => (
                  <circle className="paw-wb-flow-packet" data-flow-packet key={`packet-${edge.id}`} r="4">
                    <animateMotion begin="300ms" dur="900ms" fill="remove" path={edge.path} repeatCount="1" />
                  </circle>
                ))}
              </svg>
              <div className="paw-wb-graph__lanes">
                {graph.lanes.map((lane) => (
                  <span
                    className="paw-wb-graph__lane"
                    data-current={lane.lane === selectedLane || undefined}
                    data-lane={lane.lane}
                    key={lane.lane}
                    style={{ '--paw-wb-lane-x': `${lane.x}px` } as CSSProperties}
                    title={`${GRAPH_LANE_LABELS[lane.lane]}：${lane.count} 项`}
                  >
                    <StatusMark lane={lane.lane} />
                    <b>{GRAPH_LANE_LABELS[lane.lane]}</b>
                    <em>{lane.count}</em>
                  </span>
                ))}
              </div>
              {graph.nodes.map((node) => (
                <button
                  aria-label={`在依赖图中选择：${taskTitle(node.task)}`}
                  aria-pressed={node.id === selectedTaskId}
                  className="paw-wb-task-node"
                  data-gate={taskIsGate(node.task) || undefined}
                  data-lane={node.lane}
                  key={node.id}
                  onClick={() => onSelectTask(node.task, tasks.indexOf(node.task))}
                  style={{ '--paw-wb-node-x': `${node.x}px`, '--paw-wb-node-y': `${node.y}px` } as CSSProperties}
                  title={`${taskTitle(node.task)} · ${taskMeta(node.task)} · ${taskStateLabel(node.task)}`}
                  type="button"
                >
                  <span className="paw-wb-task-node__title"><StatusMark lane={node.lane} /><strong>{taskTitle(node.task)}</strong></span>
                  <span className="paw-wb-task-node__meta"><small>{taskMeta(node.task)}</small><em>{taskStateLabel(node.task)}</em></span>
                  {taskProgress(node.task) !== null ? <span className="paw-wb-task-progress" aria-label={`进度 ${taskProgressLabel(node.task)}`}><i style={{ width: `${Math.round((taskProgress(node.task) ?? 0) * 100)}%` }} /></span> : null}
                </button>
              ))}
            </div>
          </div>
        ) : resourceSettled(resourceState) ? <EmptyState icon={<GitBranch size={24} />} title="还没有可编排的任务" copy="添加任务后，这里会显示任务之间的依赖关系。" /> : null}
      </section>

      <TaskDetail onEditTask={onEditTask} onOpenTask={onOpenTask} onSelectTask={onSelectTask} task={selectedTask} tasks={tasks} />
    </div>
  );
}

function TaskDetail({
  onEditTask,
  onOpenTask,
  onSelectTask,
  task,
  tasks,
}: {
  onEditTask?: PawWorkbenchProps['onEditTask'];
  onOpenTask: PawWorkbenchProps['onOpenTask'];
  onSelectTask: (task: PawWorkbenchRecord, index: number) => void;
  task: PawWorkbenchRecord | null;
  tasks: PawWorkbenchRecord[];
}) {
  const [detailsExpanded, setDetailsExpanded] = useState(false);
  const selectedTaskKey = task ? taskId(task, tasks.indexOf(task)) : '';
  useEffect(() => setDetailsExpanded(false), [selectedTaskKey]);
  if (!task) return <aside className="paw-wb-detail"><EmptyState icon={<CircleDashed size={23} />} title="未选择任务" copy="选择任务，查看状态与依赖。" /></aside>;
  const dependencies = taskDependencyIds(task);
  const progress = taskProgress(task);
  const detail = text(task.detail) || text(task.description);
  const realTaskId = text(task.id) || text(task.taskId);
  return (
    <aside className="paw-wb-detail" data-expanded={detailsExpanded || undefined}>
      <header><StatusMark lane={taskLane(task)} /><div><h2 title={taskTitle(task)}>{taskTitle(task)}</h2><p>{taskMeta(task)}</p></div></header>
      <div className="paw-wb-detail__actions">
        {onEditTask ? <button onClick={() => onEditTask(task)} type="button"><PencilLine aria-hidden size={14} />编辑任务</button> : null}
        <button
          aria-expanded={detailsExpanded}
          className="paw-wb-detail__compact-toggle"
          onClick={() => setDetailsExpanded((expanded) => !expanded)}
          type="button"
        >
          <ChevronDown aria-hidden size={14} />
          {detailsExpanded ? '收起任务详情' : '展开任务详情'}
        </button>
        <button onClick={() => onOpenTask(task)} type="button"><PanelsTopLeft aria-hidden size={14} />打开任务窗口</button>
      </div>
      <div className="paw-wb-detail__body">
        {detail ? <p className="paw-wb-detail__description">{detail}</p> : null}
        {progress !== null ? <section><h3>进度 · 真实比例</h3><strong>{taskProgressLabel(task)}</strong><span className="paw-wb-task-progress"><i style={{ width: `${Math.round(progress * 100)}%` }} /></span></section> : null}
        <dl>
          {text(task.owner) ? <Fact label="Owner" value={text(task.owner)} /> : null}
          {text(task.project) ? <Fact label="项目" value={text(task.project)} /> : null}
          {text(task.source) ? <Fact label="来源" value={text(task.source)} /> : null}
          <Fact label="状态" value={taskStateLabel(task)} />
          {realTaskId ? <Fact label="任务 ID" value={realTaskId} mono wide /> : null}
        </dl>
        <section>
          <h3>依赖</h3>
          {dependencies.length ? <ol className="paw-wb-dependencies">{dependencies.map((id) => {
            const dependencyIndex = tasks.findIndex((candidate, index) => taskId(candidate, index) === id);
            const dependency = dependencyIndex >= 0 ? tasks[dependencyIndex] : null;
            return <li key={id}>{dependency ? (
              <button aria-label={`查看依赖任务：${taskTitle(dependency)}`} onClick={() => onSelectTask(dependency, dependencyIndex)} title={`${taskTitle(dependency)} · ${id}`} type="button">
                <ArrowRight aria-hidden size={13} />
                <span><strong>{taskTitle(dependency)}</strong><small className="paw-wb-mono">{id}</small></span>
              </button>
            ) : <span className="paw-wb-dependencies__unresolved" title={id}><ArrowRight aria-hidden size={13} /><span className="paw-wb-mono">{id}</span></span>}</li>;
          })}</ol> : <p className="paw-wb-detail__quiet">该任务记录没有声明依赖。</p>}
        </section>
      </div>
    </aside>
  );
}

function WorkDocumentWorkspace({
  documents,
  documentTotal,
  documentHistoryQuery,
  documentHistorySupported,
  documentLifecycle,
  documentScope,
  detailState,
  listState,
  onCloseDocument,
  onOpenDocument,
  onOpenDocumentWindow,
  onDocumentHistoryQueryChange,
  onDocumentScopeChange,
  onRefresh,
  projectName,
  selectedDocument,
}: {
  documents: readonly PawWorkbenchRecord[];
  documentTotal?: number;
  documentHistoryQuery: string;
  documentHistorySupported: boolean;
  documentLifecycle?: ReactNode;
  documentScope: PawWorkbenchDocumentScope;
  detailState?: PawWorkbenchResourceState;
  listState?: PawWorkbenchResourceState;
  onCloseDocument: () => void;
  onOpenDocument: PawWorkbenchProps['onOpenDocument'];
  onOpenDocumentWindow?: PawWorkbenchProps['onOpenDocumentWindow'];
  onDocumentHistoryQueryChange?: PawWorkbenchProps['onDocumentHistoryQueryChange'];
  onDocumentScopeChange?: PawWorkbenchProps['onDocumentScopeChange'];
  onRefresh?: PawWorkbenchProps['onRefresh'];
  projectName: string;
  selectedDocument: PawWorkbenchRecord | null;
}) {
  const resolvedDocumentTotal = Math.max(documents.length, documentTotal ?? 0);
  const [activeQuery, setActiveQuery] = useState('');
  const trimmedActiveQuery = documentScope === 'active' ? activeQuery.trim() : '';
  const visibleDocuments = trimmedActiveQuery
    ? documents.filter((document) => matchesDocument(document, trimmedActiveQuery))
    : documents;
  return (
    <div className="paw-wb-documents" data-reader-open={selectedDocument || detailState?.loading || detailState?.error ? 'true' : undefined}>
      <aside className="paw-wb-document-index">
        <header><FileText aria-hidden size={16} /><strong>工作文档</strong><span title={resourceCountLabel(listState, `已载入 ${documents.length} / 共 ${resolvedDocumentTotal}`)}>{resourceCountLabel(listState, String(resolvedDocumentTotal))}</span></header>
        {onDocumentScopeChange ? (
          <div className="paw-wb-document-index__scope" role="group" aria-label="工作文档范围">
            <button aria-pressed={documentScope === 'active'} onClick={() => onDocumentScopeChange('active')} type="button">当前</button>
            <button aria-pressed={documentScope === 'history'} disabled={!documentHistorySupported} onClick={() => onDocumentScopeChange('history')} type="button">历史</button>
          </div>
        ) : null}
        {documentScope === 'history' && onDocumentHistoryQueryChange ? (
          <label className="paw-wb-document-index__search">
            <span>筛选历史</span>
            <input onChange={(event) => onDocumentHistoryQueryChange(event.target.value)} placeholder="标题、Authority 或路径" type="search" value={documentHistoryQuery} />
          </label>
        ) : null}
        {documentScope === 'active' && (documents.length > 5 || activeQuery) ? (
          <label className="paw-wb-document-index__search">
            <span>筛选当前文档</span>
            <input onChange={(event) => setActiveQuery(event.target.value)} placeholder="标题、Authority 或路径" type="search" value={activeQuery} />
          </label>
        ) : null}
        <ResourceNotice label="工作文档" onRefresh={onRefresh ? () => onRefresh('documents') : undefined} state={listState} />
        {visibleDocuments.length ? <ol>{visibleDocuments.map((document, index) => <li key={documentId(document, index)}><button aria-current={sameDocument(document, selectedDocument) || undefined} onClick={() => void onOpenDocument(document)} title={`${documentTitle(document)} · ${text(document.path) || text(document.activePath) || text(document.authorityId)}`} type="button"><FileText aria-hidden size={15} /><span><strong>{documentTitle(document)}</strong><small>{authorityLabel(text(document.authorityKind))} · r{number(document.documentRevision)}</small></span><StatusMark lane={documentLane(document)} /></button></li>)}</ol> : trimmedActiveQuery && documents.length ? (
          <EmptyState action={<button onClick={() => setActiveQuery('')} type="button">清除文档筛选</button>} icon={<FileText size={22} />} title="没有匹配的文档" copy="调整筛选词，或清空后查看全部当前文档。" />
        ) : resourceSettled(listState) ? <EmptyState icon={<FileText size={22} />} title={documentScope === 'history' ? '没有匹配的历史文档' : '暂无工作文档'} copy={documentScope === 'history' ? '调整筛选条件，或返回当前文档。' : '这里只投影 Runtime 已登记的文档。'} /> : null}
      </aside>

      {/* A reader, not a scrolling page: the document header is fixed chrome and
          only the authority body scrolls, so the title never leaves the pane. */}
      <section aria-label="工作文档阅读器" className="paw-wb-document-reader" role="region">
        {selectedDocument ? (
          <>
            <header>
              <button aria-label="返回文档列表" onClick={onCloseDocument} type="button"><ChevronLeft aria-hidden size={17} /></button>
              <div><h2 title={documentTitle(selectedDocument)}>{documentTitle(selectedDocument)}</h2><p title={`${projectName} · ${authorityLabel(text(selectedDocument.authorityKind))}`}>{projectName} · {authorityLabel(text(selectedDocument.authorityKind))}</p></div>
              <div className="paw-wb-document-reader__actions">
                {onOpenDocumentWindow ? <button aria-label="在独立窗口打开文档" onClick={() => onOpenDocumentWindow(selectedDocument)} type="button"><PanelsTopLeft aria-hidden size={15} />独立窗口</button> : null}
                <StateBadge lane={documentLane(selectedDocument)} label={stateLabel(text(selectedDocument.state) || 'active')} />
              </div>
            </header>
            <div className="paw-wb-document-reader__body">
              <ResourceNotice label="文档详情" onRefresh={onRefresh ? () => onRefresh('documentDetail') : undefined} state={detailState} />
              <p className="paw-wb-document-reader__note">查看登记来源、当前路径与归档状态。</p>
              <dl className="paw-wb-document-facts">
                <Fact label="来源类型" value={authorityLabel(text(selectedDocument.authorityKind))} />
                <Fact label="状态" value={stateLabel(text(selectedDocument.state) || 'active')} />
                <Fact label="更新时间" value={updatedLabel(number(selectedDocument.updatedAtMs))} />
                <Fact label="当前路径" value={text(selectedDocument.path) || text(selectedDocument.activePath) || '—'} mono wide />
              </dl>
              <details className="paw-wb-document-provenance">
                <summary>来源与版本详情</summary>
                <dl className="paw-wb-document-facts">
                  <Fact label="Authority" value={text(selectedDocument.authorityId) || '—'} mono />
                  <Fact label="Authority revision" value={String(number(selectedDocument.authorityRevision))} />
                  <Fact label="Document revision" value={String(number(selectedDocument.documentRevision))} />
                  <Fact label="Document ID" value={text(selectedDocument.documentId) || text(selectedDocument.id) || '—'} mono wide />
                  <Fact label="Authority key" value={text(selectedDocument.authorityKey) || '—'} mono wide />
                  {text(selectedDocument.contentSha256) ? <Fact label="内容校验" titleValue={text(selectedDocument.contentSha256)} value={shortHash(text(selectedDocument.contentSha256))} mono wide /> : null}
                </dl>
              </details>
              {text(selectedDocument.error) ? <div className="paw-wb-document-error"><CircleAlert aria-hidden size={16} /><span>{text(selectedDocument.error)}</span></div> : null}
              {resourceSettled(detailState) ? documentLifecycle : null}
            </div>
          </>
        ) : (
          <div className="paw-wb-document-reader__body">
            {detailState?.loading || detailState?.error ? <>
              <button className="paw-wb-document-reader__return" onClick={onCloseDocument} type="button"><ChevronLeft aria-hidden size={15} />返回文档列表</button>
              <ResourceNotice label="文档详情" onRefresh={onRefresh ? () => onRefresh('documentDetail') : undefined} state={detailState} />
            </> : <EmptyState icon={<ExternalLink size={25} />} title="选择一份工作文档" copy="查看来源、当前路径和归档记录，也可以在独立窗口中继续核对。" />}
          </div>
        )}
      </section>
    </div>
  );
}

function ResourceNotice({ label, onRefresh, state }: { label: string; onRefresh?: () => void; state?: PawWorkbenchResourceState }) {
  if (!state?.loading && !state?.error) return null;
  return (
    <div className="paw-wb-resource-notice" data-error={state.error || undefined} role={state.error ? 'alert' : 'status'}>
      {state.loading ? <LoaderCircle aria-hidden className="paw-wb-spin" size={15} /> : <CircleAlert aria-hidden size={15} />}
      <span>{state.error || `正在读取${label}`}</span>
      {state.error && onRefresh ? <button onClick={onRefresh} type="button"><RefreshCw aria-hidden size={13} />重试</button> : null}
    </div>
  );
}

function EmptyState({ action, icon, title, copy }: { action?: ReactNode; icon: ReactNode; title: string; copy: string }) {
  return <div className="paw-wb-empty">{icon}<strong>{title}</strong><p>{copy}</p>{action}</div>;
}

function resourceCountLabel(state: PawWorkbenchResourceState | undefined, value: string): string {
  return state?.error ? '数量暂不可用' : state?.loading ? '数量待读取' : value;
}

function resourceSettled(state?: PawWorkbenchResourceState): boolean {
  return !state?.loading && !state?.error;
}

function StatusMark({ lane }: { lane: TaskLane }) {
  return <i aria-hidden className="paw-wb-status-mark" data-lane={lane} />;
}

function StateBadge({ label, lane }: { label: string; lane: TaskLane }) {
  return <span className="paw-wb-state-badge" data-lane={lane}><StatusMark lane={lane} />{label}</span>;
}

function Fact({ label, mono = false, titleValue, value, wide = false }: { label: string; mono?: boolean; titleValue?: string; value: string; wide?: boolean }) {
  return <div data-wide={wide || undefined}><dt>{label}</dt><dd className={mono ? 'paw-wb-mono' : undefined} title={titleValue ?? value}>{value}</dd></div>;
}

function buildTaskGraph(tasks: PawWorkbenchRecord[]): { nodes: TaskGraphNode[]; edges: TaskGraphEdge[]; lanes: TaskGraphLane[]; height: number; width: number } {
  const byLane = new Map<TaskLane, PawWorkbenchRecord[]>();
  for (const lane of GRAPH_LANES) byLane.set(lane, []);
  for (const task of tasks) byLane.get(taskLane(task))?.push(task);
  // Only populated lanes earn a column: real data decides the field width.
  const populatedLanes = GRAPH_LANES.filter((lane) => (byLane.get(lane)?.length ?? 0) > 0);
  const laneX = new Map(populatedLanes.map((lane, index) => [lane, GRAPH_PADDING_X + index * (GRAPH_NODE_WIDTH + GRAPH_COLUMN_GAP)]));
  const lanes: TaskGraphLane[] = populatedLanes.map((lane) => ({
    count: byLane.get(lane)?.length ?? 0,
    lane,
    x: laneX.get(lane) ?? GRAPH_PADDING_X,
  }));
  const nodes: TaskGraphNode[] = [];
  for (const task of tasks) {
    const lane = taskLane(task);
    const rowIndex = byLane.get(lane)?.indexOf(task) ?? 0;
    nodes.push({
      id: taskId(task, tasks.indexOf(task)),
      lane,
      task,
      x: laneX.get(lane) ?? GRAPH_PADDING_X,
      y: GRAPH_PADDING_Y + rowIndex * (GRAPH_NODE_HEIGHT + GRAPH_ROW_GAP),
    });
  }
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const edges: TaskGraphEdge[] = [];
  for (const target of nodes) {
    for (const dependencyId of taskDependencyIds(target.task)) {
      const source = nodesById.get(dependencyId);
      if (!source) continue;
      const startX = source.x + GRAPH_NODE_WIDTH;
      const startY = source.y + GRAPH_NODE_HEIGHT / 2;
      const endX = target.x;
      const endY = target.y + GRAPH_NODE_HEIGHT / 2;
      const bend = Math.max(24, Math.abs(endX - startX) * .46);
      edges.push({
        id: `${source.id}:${target.id}`,
        path: `M ${startX} ${startY} C ${startX + bend} ${startY}, ${endX - bend} ${endY}, ${endX} ${endY}`,
        active: target.lane === 'active',
      });
    }
  }
  const laneCount = Math.max(1, populatedLanes.length);
  const maxRows = Math.max(1, ...populatedLanes.map((lane) => byLane.get(lane)?.length ?? 0));
  return {
    nodes,
    edges,
    lanes,
    height: GRAPH_PADDING_Y * 2 + maxRows * GRAPH_NODE_HEIGHT + Math.max(0, maxRows - 1) * GRAPH_ROW_GAP,
    width: GRAPH_PADDING_X * 2 + laneCount * GRAPH_NODE_WIDTH + (laneCount - 1) * GRAPH_COLUMN_GAP,
  };
}

function overviewMetrics(
  overview: PawWorkbenchRecord,
  planning: PawWorkbenchRecord,
  documents: readonly PawWorkbenchRecord[],
  documentsState?: PawWorkbenchResourceState,
): { label: string; value: string }[] {
  const metrics = record(overview.metrics);
  const summary = record(planning.summary);
  const candidates: [string, unknown][] = [
    ['活跃 Session', metrics.activeSessions ?? overview.activeSessions],
    ['开放任务', metrics.openTasks ?? summary.openTaskCount],
    ['已完成任务', summary.completedTaskCount],
    ['整体进度', percentage(summary.progress)],
    ['工作文档', documentsState?.loading || documentsState?.error ? undefined : documents.length],
  ];
  return candidates.flatMap(([label, value]) => value === undefined || value === null || value === '' ? [] : [{ label, value: scalar(value) }]);
}

function nextUnresolvedTask(tasks: PawWorkbenchRecord[]): { index: number; lane: TaskLane; task: PawWorkbenchRecord } | null {
  for (const lane of ['blocked', 'review', 'active', 'todo'] as const) {
    const index = tasks.findIndex((task) => taskLane(task) === lane);
    if (index >= 0) return { index, lane, task: tasks[index] };
  }
  return null;
}

function matchesTask(task: PawWorkbenchRecord, query: string): boolean {
  const needle = query.toLowerCase();
  return [taskTitle(task), text(task.owner), text(task.project), text(task.source), taskStateLabel(task)]
    .some((value) => value.toLowerCase().includes(needle));
}

function matchesDocument(document: PawWorkbenchRecord, query: string): boolean {
  const needle = query.toLowerCase();
  return [
    documentTitle(document),
    authorityLabel(text(document.authorityKind)),
    text(document.authorityId),
    text(document.activePath) || text(document.path),
    stateLabel(text(document.state) || 'active'),
  ].some((value) => value.toLowerCase().includes(needle));
}

function taskId(task: PawWorkbenchRecord, index: number): string {
  return text(task.id) || text(task.taskId) || `task-${index}`;
}

function taskTitle(task: PawWorkbenchRecord): string {
  return text(task.title) || text(task.objective) || '未命名任务';
}

function taskMeta(task: PawWorkbenchRecord): string {
  return [text(task.owner), text(task.project), text(task.source)].filter(Boolean).join(' · ') || '当前项目';
}

function taskLane(task: PawWorkbenchRecord): TaskLane {
  const value = (text(task.state) || text(task.status)).toLowerCase();
  if (['done', 'complete', 'completed', 'accepted', 'success'].includes(value)) return 'done';
  if (['review', 'verifying', 'verification', 'approval_pending', 'waiting_approval'].includes(value)) return 'review';
  if (['blocked', 'failed', 'error', 'cancelled'].includes(value)) return 'blocked';
  if (['todo', 'pending', 'queued', 'ready', 'idle', 'waiting'].includes(value)) return 'todo';
  return 'active';
}

function taskStateLabel(task: PawWorkbenchRecord): string {
  const raw = text(task.state) || text(task.status);
  if (raw) return stateLabel(raw);
  return { done: '已完成', active: '进行中', review: '待验收', blocked: '受阻', todo: '待办' }[taskLane(task)];
}

function taskDependencyIds(task: PawWorkbenchRecord): string[] {
  const values = [task.dependencies, task.dependsOn, task.dependencyIds, task.blockedBy];
  const ids = values.flatMap((value) => dependencyIds(value));
  return [...new Set(ids.filter(Boolean))];
}

function dependencyIds(value: unknown): string[] {
  if (typeof value === 'string') return [value];
  if (Array.isArray(value)) return value.flatMap((item) => dependencyIds(item));
  const item = record(value);
  const id = text(item.id) || text(item.taskId) || text(item.workItemId);
  return id ? [id] : [];
}

function taskProgress(task: PawWorkbenchRecord): number | null {
  const raw = task.progress ?? task.progressRatio ?? task.completion;
  if (typeof raw === 'number' && Number.isFinite(raw)) return clamp(raw > 1 ? raw / 100 : raw);
  const completed = number(task.completedCount) || number(task.completed);
  const total = number(task.totalCount) || number(task.total);
  if (total > 0) return clamp(completed / total);
  return null;
}

function taskProgressLabel(task: PawWorkbenchRecord): string {
  const progress = taskProgress(task);
  if (progress === null) return '';
  const completed = number(task.completedCount) || number(task.completed);
  const total = number(task.totalCount) || number(task.total);
  return total > 0 ? `${completed} / ${total}` : `${Math.round(progress * 100)}%`;
}

function taskIsGate(task: PawWorkbenchRecord): boolean {
  const kind = `${text(task.kind)} ${text(task.type)} ${text(task.status)}`.toLowerCase();
  return /approval|gate|审批/.test(kind);
}

function documentId(document: PawWorkbenchRecord, index: number): string {
  return text(document.documentId) || text(document.id) || `document-${index}`;
}

function documentTitle(document: PawWorkbenchRecord): string {
  return text(document.title) || '未命名文档';
}

function documentLane(document: PawWorkbenchRecord): TaskLane {
  const state = text(document.state);
  if (state === 'archived') return 'done';
  if (state === 'error') return 'blocked';
  if (state.endsWith('_pending')) return 'review';
  return 'active';
}

function sameDocument(document: PawWorkbenchRecord, selected: PawWorkbenchRecord | null): boolean {
  if (!selected) return false;
  return (text(document.documentId) || text(document.id)) === (text(selected.documentId) || text(selected.id));
}

function authorityLabel(value: string): string {
  return ({ session_todo: 'Session Todo', session_goal: 'Session Goal', room_work_item: 'Room WorkItem' } as Record<string, string>)[value] ?? (value || 'WorkDocument');
}

function stateLabel(value: string): string {
  return ({
    active: '进行中', in_progress: '进行中', running: '运行中', review: '待验收', verifying: '验证中',
    done: '已完成', complete: '已完成', completed: '已完成', accepted: '已接受', pending: '待办', queued: '排队中',
    ready: '已就绪', idle: '空闲', blocked: '受阻', failed: '失败', error: '错误', cancelled: '已取消',
    archive_pending: '等待归档', archived: '已归档', reopen_pending: '等待重开',
  } as Record<string, string>)[value] ?? value;
}

function updatedLabel(timestamp: number): string {
  if (!timestamp) return '未提供时间';
  return new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(new Date(timestamp));
}

function shortHash(value: string): string {
  return value.length > 16 ? `${value.slice(0, 12)}…${value.slice(-4)}` : value;
}

function pathName(value: string): string {
  return value.split(/[\\/]/).filter(Boolean).at(-1) ?? '';
}

function percentage(value: unknown): string | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value)) return undefined;
  return `${Math.round((value > 1 ? value / 100 : value) * 100)}%`;
}

function scalar(value: unknown): string {
  return typeof value === 'number' ? new Intl.NumberFormat('zh-CN').format(value) : String(value);
}

function rows(envelope: PawWorkbenchRecord, keys: string[]): PawWorkbenchRecord[] {
  for (const key of keys) if (Array.isArray(envelope[key])) return (envelope[key] as unknown[]).map(record);
  return [];
}

function text(value: unknown): string { return typeof value === 'string' ? value : ''; }
function number(value: unknown): number { return typeof value === 'number' && Number.isFinite(value) ? value : 0; }
function record(value: unknown): PawWorkbenchRecord { return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as PawWorkbenchRecord : {}; }
function clamp(value: number): number { return Math.max(0, Math.min(1, value)); }
