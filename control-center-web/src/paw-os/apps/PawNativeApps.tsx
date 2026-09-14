import {
  Bot,
  Brain,
  BriefcaseBusiness,
  ChevronRight,
  Clock3,
  FlaskConical,
  FileText,
  FolderKanban,
  LibraryBig,
  Network,
  Settings2,
  Sparkles,
  type LucideIcon,
} from 'lucide-react';
import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { useControlTransport } from '@/app/control-transport';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import { useOptionalTeam } from '@/features/team/team-context';
import { TeamProjectWorkbench } from '@/features/team/TeamProjectWorkbench';
import { useWorkDocumentWorkspace, type WorkDocumentScope } from '@/features/work-documents/api';
import type { ControlPathId } from '@/platform/routes';
import type { ControlRequest } from '@/platform/transport';
import type { PawOsAppId } from '@/features/paw-os/model/app-registry';
import { pawApp } from '../runtime/app-registry';
import { AppSidebarToggle, useAppSidebar } from './app-sidebar';
import { PawSystemAppsMigrated, isPawSystemAppId, type PawSystemAppId } from './PawSystemAppsMigrated';
import { PawWorkbenchDocumentLifecycle } from './PawWorkbenchDocumentLifecycle';
import { PawWorkbenchMigrated, type PawWorkbenchPageId } from './PawWorkbenchMigrated';
import { PawWorkbenchDocumentRegisterDialog, PawWorkbenchGoalDialog, PawWorkbenchTaskDialog } from './PawWorkbenchOperations';
import { PawWorkbenchPlanningTools } from './PawWorkbenchPlanningTools';

const KnowledgeFeature = lazy(async () => ({ default: (await import('@/features/knowledge')).KnowledgeFeature }));
const EvalLabFeature = lazy(async () => ({ default: (await import('@/features/eval-lab')).EvalLabFeature }));
const MemoryFeature = lazy(async () => ({ default: (await import('@/features/memory')).MemoryFeature }));

export type PawNativeAppId = Exclude<PawOsAppId, 'agent' | 'agent-capsule' | 'browser' | 'files' | 'terminal' | 'trace-agent' | 'schedules'>;
type PawFeatureAppId = Exclude<PawNativeAppId, PawSystemAppId>;

type NativePage = { id: string; label: string; icon: LucideIcon; route: string };

const pagesByApp: Record<PawFeatureAppId, readonly NativePage[]> = {
  'project-workbench': [
    { id: 'overview', label: '概览', icon: BriefcaseBusiness, route: '/overview' },
    { id: 'planning', label: '任务', icon: FolderKanban, route: '/planning' },
    { id: 'documents', label: '工作文档', icon: FileText, route: '/work-documents' },
  ],
  memory: [
    { id: 'memory', label: '记忆库', icon: Brain, route: '/memory' },
    { id: 'roleBooks', label: '伙伴记忆', icon: Bot, route: '/memory?view=roleBooks' },
    { id: 'timeline', label: '时间线', icon: Clock3, route: '/memory?view=timeline' },
    { id: 'relations', label: '关系图', icon: Network, route: '/memory?view=relations' },
    { id: 'organize', label: '整理', icon: Sparkles, route: '/memory?view=organize' },
    { id: 'preferences', label: '记忆偏好', icon: Settings2, route: '/memory?view=preferences' },
  ],
  knowledge: [
    { id: 'libraries', label: '知识库', icon: LibraryBig, route: '/knowledge' },
  ],
  'eval-lab': [
    { id: 'eval-lab', label: '评测任务', icon: FlaskConical, route: '/eval-lab' },
  ],
};

export function PawNativeApp({ appId, initialRoute = '' }: { appId: PawNativeAppId; initialRoute?: string }) {
  if (isPawSystemAppId(appId)) return <PawSystemAppsMigrated appId={appId} initialRoute={initialRoute} />;
  return <PawFeatureApp appId={appId} initialRoute={initialRoute} />;
}

function PawFeatureApp({ appId, initialRoute }: { appId: PawFeatureAppId; initialRoute: string }) {
  const pages = pagesByApp[appId];
  const sidebar = useAppSidebar(appId, appId === 'eval-lab');
  const app = pawApp(appId);
  const desktop = usePawOsDesktop();
  const route = initialRoute || app.route;
  const pageId = pageForRoute(pages, route).id;
  return (
    <div className="paw-native-app" data-app-id={appId} data-page-id={pageId} data-sidebar-collapsed={sidebar.collapsed} data-owns-navigation={appId === 'knowledge' || appId === 'eval-lab' || undefined} data-single-page={pages.length === 1 || undefined}>
      {appId === 'knowledge' || appId === 'eval-lab' ? null : <aside className="paw-native-nav">
        <AppSidebarToggle collapsed={sidebar.collapsed} controlsId={sidebar.controlsId} label={`${app.label}导航`} onToggle={() => sidebar.setCollapsed(!sidebar.collapsed)} toggleRef={sidebar.toggleRef} />
        <nav aria-label={`${app.label}页面`} {...sidebar.contentProps}>
          {pages.map((page) => {
            const Icon = page.icon;
            return <button aria-current={page.id === pageId ? 'page' : undefined} aria-label={page.label} key={page.id} onClick={() => openPawOsRoute(desktop, page.route)} title={page.label} type="button"><Icon aria-hidden="true" size={15} /><span>{page.label}</span><ChevronRight aria-hidden="true" size={13} /></button>;
          })}
        </nav>
      </aside>}
      <section className="paw-native-stage">
        <MemoryRouter initialEntries={[route]} key={route}>
          <NativeRouteReporter expectedRoute={route} />
          <div className="paw-native-page" key={`${appId}:${pageId}`}>
            <Suspense fallback={<div className="paw-app-loading" role="status">正在打开 {app.label}…</div>}>
              <NativeSurface appId={appId} pageId={pageId} route={route} />
            </Suspense>
          </div>
        </MemoryRouter>
      </section>
    </div>
  );
}

function NativeRouteReporter({ expectedRoute }: { expectedRoute: string }) {
  const desktop = usePawOsDesktop();
  const location = useLocation();
  const route = `${location.pathname}${location.search}${location.hash}`;
  useEffect(() => {
    if (route !== expectedRoute) openPawOsRoute(desktop, route);
  }, [desktop, expectedRoute, route]);
  return null;
}

function NativeSurface({ appId, pageId, route }: { appId: PawFeatureAppId; pageId: string; route: string }) {
  if (appId === 'project-workbench') return <ProjectSurface pageId={pageId} route={route} />;
  if (appId === 'memory') return <MemoryFeature />;
  if (appId === 'eval-lab') return <EvalLabFeature />;
  return <KnowledgeFeature />;
}

function ProjectSurface({ pageId, route }: { pageId: string; route: string }) {
  const desktop = usePawOsDesktop();
  const team = useOptionalTeam();
  const workbenchPageId: PawWorkbenchPageId = pageId === 'planning'
    ? 'planning'
    : pageId === 'documents'
      ? 'documents'
      : 'overview';
  if (team?.activeSpace?.kind === 'project' && workbenchPageId === 'overview') {
    return <TeamProjectWorkbench onNavigate={(nextPage) => openPawOsRoute(desktop, nextPage === 'planning' ? '/planning' : '/work-documents')} />;
  }
  return <ProjectWorkbenchSurface pageId={workbenchPageId} route={route} />;
}

function ProjectWorkbenchSurface({ pageId, route }: { pageId: PawWorkbenchPageId; route: string }) {
  const desktop = usePawOsDesktop();
  const [planningDate, setPlanningDate] = useState(() => localDate(new Date()));
  const overview = useNativeResource('overview.get');
  const planning = useNativeResource('planning.dashboard', { query: { date: planningDate } });
  const [documentScope, setDocumentScope] = useState<WorkDocumentScope>('active');
  const [documentHistoryQuery, setDocumentHistoryQuery] = useState('');
  const [taskDialogOpen, setTaskDialogOpen] = useState(false);
  const [selectedTask, setSelectedTask] = useState<Record<string, unknown> | null>(null);
  const [goalDialogOpen, setGoalDialogOpen] = useState(false);
  const [selectedGoal, setSelectedGoal] = useState<Record<string, unknown> | null>(null);
  const [registerDialogOpen, setRegisterDialogOpen] = useState(false);
  const requestedDocumentId = new URLSearchParams(route.split('?', 2)[1] ?? '').get('document') ?? '';
  const workDocuments = useWorkDocumentWorkspace(documentScope, documentHistoryQuery, pageId === 'documents' ? requestedDocumentId : '');
  const documentList = documentScope === 'history' ? workDocuments.history : workDocuments.active;
  const documentItems = documentList.data?.items ?? [];
  const docs = documentItems.map(record);
  const selectedWorkDocument = workDocuments.detail.data?.document
    ?? documentItems.find((document) => document.documentId === requestedDocumentId)
    ?? null;
  const selectedDocument = selectedWorkDocument ? record(selectedWorkDocument) : null;

  function openTask(task: Record<string, unknown>): void {
    const id = text(task.id) || text(task.taskId);
    if (!id) return;
    desktop?.openWindow({
      appId: 'project-workbench',
      target: {
        kind: 'task',
        id,
        title: text(task.title) || '未命名任务',
        subtitle: text(task.detail) || text(task.owner),
        date: text(task.date) || text(planning.data.date) || new Date().toISOString().slice(0, 10),
        project: text(task.project),
      },
    });
  }

  function openDocument(document: Record<string, unknown>): void {
    const documentId = text(document.documentId) || text(document.id);
    if (!documentId) return;
    openPawOsRoute(desktop, `/work-documents?document=${encodeURIComponent(documentId)}`);
  }

  function openDocumentWindow(document: Record<string, unknown>): void {
    const documentId = text(document.documentId) || text(document.id);
    if (!documentId) return;
    desktop?.openWindow({
      appId: 'project-workbench',
      target: {
        kind: 'work-document',
        id: documentId,
        title: text(document.title) || '未命名工作文档',
        subtitle: text(document.path) || text(document.authorityId),
      },
    });
  }

  const primaryAction = pageId === 'overview'
    ? { label: '查看任务', onClick: () => openPawOsRoute(desktop, '/planning') }
    : pageId === 'planning'
      ? { label: '添加任务', onClick: () => setTaskDialogOpen(true) }
      : { label: '登记工作文档', onClick: () => setRegisterDialogOpen(true) };

  return <>
    <PawWorkbenchMigrated
      documents={docs}
      documentTotal={documentList.data?.total ?? docs.length}
      documentHistoryQuery={documentHistoryQuery}
      documentHistorySupported={workDocuments.access.history}
      documentLifecycle={selectedWorkDocument ? (
        <PawWorkbenchDocumentLifecycle
          access={workDocuments.access}
          current={workDocuments.detail.data ?? selectedWorkDocument}
          onChanged={() => {
            void workDocuments.active.refetch();
            void workDocuments.history.refetch();
            void workDocuments.detail.refetch();
          }}
          onErased={() => {
            openPawOsRoute(desktop, '/work-documents');
            void workDocuments.active.refetch();
            void workDocuments.history.refetch();
          }}
          transport={workDocuments.transport}
        />
      ) : undefined}
      documentScope={documentScope}
      onCloseDocument={() => openPawOsRoute(desktop, '/work-documents')}
      onCreateGoal={() => {
        setSelectedGoal(null);
        setGoalDialogOpen(true);
      }}
      onDocumentHistoryQueryChange={setDocumentHistoryQuery}
      onDocumentScopeChange={(scope) => {
        setDocumentScope(scope);
        if (requestedDocumentId) openPawOsRoute(desktop, '/work-documents');
      }}
      onEditGoal={(goal) => {
        setSelectedGoal(goal);
        setGoalDialogOpen(true);
      }}
      onEditTask={(task) => {
        setSelectedTask(task);
        setTaskDialogOpen(true);
      }}
      onOpenDocument={openDocument}
      onOpenDocumentWindow={openDocumentWindow}
      onOpenTask={openTask}
      onNavigate={(nextPage) => openPawOsRoute(desktop, nextPage === 'planning' ? '/planning' : nextPage === 'documents' ? '/work-documents' : '/overview')}
      onRefresh={(resource) => {
        if (resource === 'overview') overview.reload();
        else if (resource === 'planning') planning.reload();
        else if (resource === 'documentDetail') void workDocuments.detail.refetch();
        else void documentList.refetch();
      }}
      overview={overview.data}
      pageId={pageId}
      planning={planning.data}
      planningTools={(selectedTask) => (
        <PawWorkbenchPlanningTools
          selectedTask={selectedTask}
          date={planningDate}
          onDateChange={setPlanningDate}
          onOpenAgent={(draft) => {
            const agentRoute = `/agent?${new URLSearchParams({ draft }).toString()}`;
            if (desktop?.openApp) desktop.openApp('agent', agentRoute);
            else openPawOsRoute(desktop, agentRoute);
          }}
          planning={planning.data}
          projectName={text(record(overview.data.project).name) || text(overview.data.projectName) || text(record(planning.data.plan).project) || '未选择项目'}
          projectPath={text(record(overview.data.project).path) || text(overview.data.projectPath) || text(record(planning.data.plan).workspaceRoot)}
        />
      )}
      primaryAction={primaryAction}
      resourceStates={{
        overview: { loading: overview.loading, error: overview.error },
        planning: { loading: planning.loading, error: planning.error },
        documents: {
          loading: !workDocuments.capabilityKnown || documentList.isLoading,
          error: workDocumentListError(workDocuments, documentList.error),
        },
        documentDetail: { loading: workDocuments.detail.isLoading, error: errorText(workDocuments.detail.error) },
      }}
      selectedDocument={selectedDocument}
    />
    {pageId === 'planning' ? <PawWorkbenchTaskDialog
      onChanged={planning.reload}
      onOpenChange={(open) => {
        setTaskDialogOpen(open);
        if (!open) setSelectedTask(null);
      }}
      open={taskDialogOpen}
      planning={planning.data}
      selectedTask={selectedTask}
    /> : null}
    {pageId === 'planning' ? <PawWorkbenchGoalDialog
      onChanged={planning.reload}
      onOpenChange={(open) => {
        setGoalDialogOpen(open);
        if (!open) setSelectedGoal(null);
      }}
      open={goalDialogOpen}
      planning={planning.data}
      selectedGoal={selectedGoal}
    /> : null}
    {pageId === 'documents' ? <PawWorkbenchDocumentRegisterDialog
      onChanged={() => void workDocuments.active.refetch()}
      onOpenChange={setRegisterDialogOpen}
      open={registerDialogOpen}
    /> : null}
  </>;
}

type NativeResourceState = {
  data: Record<string, unknown>;
  error: string;
  loading: boolean;
  reload: () => void;
};

type NativeResourceOptions = Pick<ControlRequest, 'params' | 'query' | 'responseContract'>;

function workDocumentListError(
  workspace: ReturnType<typeof useWorkDocumentWorkspace>,
  listError: unknown,
): string {
  if (workspace.capabilities.error) return errorText(workspace.capabilities.error);
  if (workspace.capabilityKnown && !workspace.supported) {
    return `当前宿主缺少工作文档读取路由：${workspace.access.missingReadRoutes.join('、')}`;
  }
  return errorText(listError);
}

function useNativeResource(pathId: ControlPathId, options: NativeResourceOptions = {}, refreshMs = 0): NativeResourceState {
  const transport = useControlTransport();
  const [data, setData] = useState<Record<string, unknown>>({});
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const requestKey = JSON.stringify(options);
  const reload = useCallback(() => setRevision((value) => value + 1), []);
  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const requestOptions = JSON.parse(requestKey) as NativeResourceOptions;
        const response = await transport.request({ pathId, ...requestOptions });
        if (active) { setData(record(response)); setError(''); }
      } catch (requestError) {
        if (active) setError(errorText(requestError));
      } finally {
        if (active) setLoading(false);
      }
    }
    setLoading(true);
    void load();
    if (!refreshMs) return () => { active = false; };
    const timer = window.setInterval(() => void load(), refreshMs);
    return () => { active = false; window.clearInterval(timer); };
  }, [pathId, refreshMs, requestKey, revision, transport]);
  return { data, error, loading, reload };
}

function pageForRoute(pages: readonly NativePage[], route: string): NativePage {
  if (!route) return pages[0];
  const exactRoute = pages.find((page) => page.route === route);
  if (exactRoute) return exactRoute;
  const view = new URLSearchParams(route.split('?', 2)[1] ?? '').get('view');
  const matchingView = pages.find((page) => page.id === view);
  if (matchingView) return matchingView;
  const path = route.split('?', 1)[0];
  const exact = pages.find((page) => page.route.split('?', 1)[0] === path);
  if (exact) return exact;
  return pages[0];
}

function rows(envelope: Record<string, unknown>, keys: string[]): Record<string, unknown>[] {
  for (const key of keys) if (Array.isArray(envelope[key])) return (envelope[key] as unknown[]).map(record);
  return [];
}

function text(value: unknown): string { return typeof value === 'string' ? value : ''; }
function number(value: unknown): number { return typeof value === 'number' && Number.isFinite(value) ? value : 0; }
function record(value: unknown): Record<string, unknown> { return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {}; }
function localDate(value: Date): string {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, '0');
  const day = String(value.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}
function errorText(error: unknown): string {
  if (error === null || error === undefined || error === '') return '';
  return error instanceof Error && error.message ? error.message : '本机服务暂时不可用。';
}
