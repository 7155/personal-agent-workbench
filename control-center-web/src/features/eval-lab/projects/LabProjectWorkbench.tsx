import { ArrowLeft, ArrowUpRight, CheckCircle2, CircleAlert, FileText, FolderOpen, FlaskConical, GitBranch, Layers3, Play, PackageCheck, LayoutDashboard, Database, History, LoaderCircle, MessageSquare, PanelLeftClose, PanelLeftOpen, Plus, RefreshCw, Upload } from 'lucide-react';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { useControlTransport } from '@/app/control-transport';
import { Button, IconButton } from '@/components/primitives';
import { GoldenWorkflow } from '../golden/GoldenWorkflow';
import { SceneTrialPanel } from '../trials/SceneTrialPanel';
import { ArtifactSurface, type ArtifactDraft } from './ArtifactSurface';
import { isExperimentSnapshot } from './ExperimentReport';
import { LabProjectHome } from './LabProjectHome';
import { projectError, useLabArtifact, useLabProjects } from './api';
import { initialProjectMessage, pendingProjectMessages, ProjectGuide, retainProjectMessage, sendProjectGuideMessage, type PendingProjectMessage } from './ProjectGuide';
import { readArtifactDrafts, writeArtifactDrafts } from './drafts';
import { LabAppDelivery } from './LabAppDelivery';
import { LabKnowledge } from './LabKnowledge';
import { LabKnowledgeRecord } from './LabKnowledgeRecord';
import { LabSessionRecord } from './LabSessionRecord';
import { LabExperimentLifecycle } from './LabExperimentLifecycle';
import { LabWorkflowGraph } from './LabWorkflowGraph';
import { LabProjectSpace } from './LabProjectSpace';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import type { LabWorkflowNode } from './project-workflow-types';
import { projectGuidanceMessage, type ProjectGuidanceMode } from './project-guidance';
import { defaultProjectView, readProjectViews, writeProjectViews, type ProjectPage, type ProjectView } from './views';
import { object, type ArtifactAction, type JsonValue, type LabBinding, type LabBindingExecution, type LabProject, type ProjectReceipt } from './types';
import './lab-project-workbench.css';

type Intake = { sourceId?: string; title: string; kind?: string; text: string };
function BindingExecutionStatus({ execution }: { execution?: LabBindingExecution }) {
  if (!execution) return <small className="lab-binding-execution lab-binding-execution--unknown">读取运行状态中…</small>;
  const Icon = execution.status === 'running' || execution.status === 'queued' ? LoaderCircle
    : execution.status === 'completed' ? CheckCircle2 : execution.status === 'failed' || execution.status === 'interrupted' ? CircleAlert : null;
  return <small className={`lab-binding-execution lab-binding-execution--${execution.status}`}>
    {Icon ? <Icon size={13} aria-hidden="true" className={execution.status === 'running' ? 'lab-binding-execution__spin' : undefined} /> : null}
    <strong>{execution.label}</strong><span>{execution.reason}</span>
  </small>;
}

function ProjectBindingList({ bindings, onOpen }: { bindings: LabBinding[]; onOpen: (binding: LabBinding) => void }) {
  return <section className="lab-project-bindings" aria-label="真实模型运行与优化">
    <header className="lab-project-bindings__header"><div><small>执行回执</small><h2>模型运行与下一轮优化</h2><p>这里的状态来自 Golden/Pi 或场景执行器的真实回执。进入后才能开始模型调用、查看逐题结果和提交下一轮候选。</p></div><span>{bindings.length} 个绑定</span></header>
    {bindings.map((item) => <button className="lab-project-binding" key={item.bindingId} onClick={() => onOpen(item)}>
      <span><strong>{item.adapterId === 'golden.context_qa' ? '资料问答评测' : item.adapterId === 'golden.knowledge_qa' ? '知识库回答评测' : item.adapterId === 'scene.trial' ? '场景验证' : item.adapterId}</strong><small>{item.execution?.status === 'completed' ? `真实模型运行已完成${item.execution.latestJob?.decision ? ` · ${item.execution.latestJob.decision}` : ''}` : item.summary || '进入执行器'}</small><BindingExecutionStatus execution={item.execution} /></span><ArrowUpRight size={18} />
    </button>)}
  </section>;
}

export function LabProjectWorkbench({ initialProjectId = '', onOpenHistory, onProjectSelect }: { initialProjectId?: string; onOpenHistory?: () => void; onProjectSelect?: (id: string) => void }) {
  const desktop = usePawOsDesktop();
  const transport = useControlTransport(); const [projectId, setProjectId] = useState(initialProjectId);
  const currentProjectId = useRef(projectId); currentProjectId.current = projectId;
  const workflow = useLabProjects(projectId); const project = workflow.project.data?.project;
  const [views, setViews] = useState<Record<string, ProjectView>>(() => readProjectViews(workflow.connection)); const viewsRef = useRef(views);
  const projectDefaultView = { ...defaultProjectView, guideOpen:project?.workspace.layout !== 'focus' };
  const view = views[projectId] ?? projectDefaultView; const { page, guideOpen } = view;
  const [drafts, setDrafts] = useState<Record<string, ArtifactDraft>>(() => readArtifactDrafts(workflow.connection));
  const draftsRef = useRef(drafts);
  const [notice, setNotice] = useState(''); const [sending, setSending] = useState(false);
  const guidancePending = useRef(false);
  const [historyImportOpen, setHistoryImportOpen] = useState(false);
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [pendingMessages, setPendingMessages] = useState<PendingProjectMessage[]>([]);
  const [draftRequest, setDraftRequest] = useState<{ id: number; text: string }>();
  const [stagedAction, setStagedAction] = useState<{ text: string; title: string }>();
  const binding = project?.bindings.find((item) => item.bindingId === view.bindingId); const [intakeOpen, setIntakeOpen] = useState(false);
  const restored = useRef(''); const selectionKey = `paw.lab.project-selection.v1:${workflow.connection}`;
  const updateView = (patch: Partial<ProjectView>) => {
    const next = { ...viewsRef.current, [projectId]: { ...(viewsRef.current[projectId] ?? projectDefaultView), ...patch } };
    viewsRef.current = next; setViews(next);
    if (!writeProjectViews(workflow.connection, next)) setNotice('当前浏览器未能保存查看位置，已保存的项目和运行不受影响。');
  };
  const setPage = (next: ProjectPage) => updateView({ page:next });
  const setGuideOpen = (next: boolean | ((current: boolean) => boolean)) => updateView({ guideOpen:typeof next === 'function' ? next(viewsRef.current[projectId]?.guideOpen ?? projectDefaultView.guideOpen) : next });
  const setBinding = (next?: LabBinding) => updateView({ bindingId:next?.bindingId });
  const openWorkflowNode = (node: LabWorkflowNode) => {
    if (node.ref.kind === 'application_call') {
      const owner = node.evidenceRefs?.find((ref) => ref.kind === 'application');
      const appVersion = typeof node.ref.version === 'number' ? node.ref.version : owner?.version;
      if (!owner || !appVersion) { setNotice('原应用调用缺少明确的应用或版本来源，暂不能定位。'); return; }
      updateView({ page: 'apps', appId: owner.id, appVersion, appCallId: node.ref.id }); return;
    }
    if (node.ref.kind === 'runtime_session' || node.ref.kind === 'runtime_turn') {
      const sessions = [...new Set(node.evidenceRefs?.filter((ref) => ref.kind === 'runtime_session').map((ref) => ref.id) ?? [])];
      const sessionId = node.ref.kind === 'runtime_session' ? node.ref.id : sessions.length === 1 ? sessions[0] : undefined;
      if (!sessionId) { setNotice('这条回合来源未明确对应唯一 Session，暂不能定位原记录。'); return; }
      setSessionRecord({ projectId, sessionId, ...(node.ref.kind === 'runtime_turn' ? { turnId: node.ref.id } : {}) }); return;
    }
    if (node.ref.kind === 'progress_step') {
      const source = node.evidenceRefs?.find((ref) => ref.kind === 'artifact');
      if (source) { updateView({ page: 'artifact', artifactId: source.id, bindingId: undefined }); return; }
      setGuideOpen(true); return;
    }
    if (node.ref.kind === 'experiment_record') {
      const source = node.evidenceRefs?.find((ref) => ref.kind === 'artifact');
      const artifactId = source?.id ?? project?.historyOrigin?.snapshotArtifactId;
      if (artifactId) { setExperimentSelections((current) => ({ ...current, [projectId]: node.ref.id })); updateView({ page: 'artifact', artifactId, bindingId: undefined }); return; }
    }
    if (node.kind === 'materials') { setPage('materials'); return; }
    if (node.kind === 'application') { setPage('apps'); return; }
    if (node.ref.kind === 'artifact') { updateView({ page: 'artifact', artifactId: node.ref.id, bindingId: undefined }); return; }
    if (node.ref.kind === 'knowledge_job') { updateView({ jobId: node.ref.id }); setKnowledgeRecord({ projectId, jobId: node.ref.id }); return; }
    const parentNode = project?.workflow?.nodes.find((item) => item.id === node.parentId);
    const suiteRef = (node.evidenceRefs ?? parentNode?.evidenceRefs)?.find((ref) => ref.kind === 'golden_suite');
    const owner = project?.bindings.find((item) => (suiteRef && item.ownerRef.id === suiteRef.id) || item.execution?.latestJob?.jobId === node.ref.id
      || item.ownerRef.id === node.ref.id || (node.ref.kind === 'golden_job' && item.ownerRef.kind === 'golden_suite'));
    updateView({ page: 'runs', bindingId: owner?.bindingId, jobId: node.ref.kind === 'runtime_request' ? parentNode?.ref.id : node.ref.id });
  };
  const updateDraft = (key: string, draft?: ArtifactDraft) => {
    const next = { ...draftsRef.current }; if (draft) next[key] = draft; else delete next[key];
    draftsRef.current = next; setDrafts(next);
    if (!writeArtifactDrafts(workflow.connection, next)) setNotice('浏览器暂时无法保存草稿；本窗口仍保留输入，请先保存成果再关闭。');
  };
  useEffect(() => {
    if (restored.current === workflow.connection) return;
    restored.current = workflow.connection;
    const savedDrafts = readArtifactDrafts(workflow.connection); draftsRef.current = savedDrafts; setDrafts(savedDrafts);
    const savedViews = readProjectViews(workflow.connection); viewsRef.current = savedViews; setViews(savedViews);
    if (initialProjectId) return;
    try { setProjectId(localStorage.getItem(selectionKey) ?? ''); } catch { /* Start at the project list. */ }
  }, [workflow.connection, initialProjectId, selectionKey]);
  useEffect(() => { if (initialProjectId) setProjectId(initialProjectId); }, [initialProjectId]);
  useEffect(() => { setPendingMessages(project ? pendingProjectMessages(transport, project) : []); }, [transport, project?.projectId, project?.guideSessionId]);
  const openProject = (id: string, targetPage?: ProjectPage, artifactId?: string) => {
    if (targetPage) {
      const next = { ...viewsRef.current, [id]: { ...(viewsRef.current[id] ?? defaultProjectView), page: targetPage, ...(artifactId ? { artifactId } : {}) } };
      viewsRef.current = next; setViews(next);
      if (!writeProjectViews(workflow.connection, next)) setNotice('当前浏览器未能保存查看位置，已保存的项目和运行不受影响。');
    }
    setProjectId(id); setNotice(''); setStagedAction(undefined);
    try { if (id) localStorage.setItem(selectionKey, id); else localStorage.removeItem(selectionKey); } catch { /* The current view remains usable. */ }
    onProjectSelect?.(id);
  };
  const workspaceArtifact = project?.artifacts.find((item) => item.artifactId === project.workspace.primaryArtifactId && item.view !== 'json') ?? project?.artifacts.find((item) => item.view !== 'json');
  const selectedId = project ? view.artifactId ?? (page === 'workspace' ? workspaceArtifact?.artifactId ?? project.workspace.primaryArtifactId : project.workspace.primaryArtifactId) : '';
  const selected = project?.artifacts.find((item) => item.artifactId === selectedId);
  const artifact = useLabArtifact(projectId, selected?.artifactId ?? '', selected?.revision);
  const [sessionRecord, setSessionRecord] = useState<{ projectId: string; sessionId: string; turnId?: string }>();
  const [knowledgeRecord, setKnowledgeRecord] = useState<{ projectId: string; jobId: string }>();
  const [sourceRef, setSourceRef] = useState<{ projectId: string; artifactId: string; revision?: number }>();
  const sourceArtifact = useLabArtifact(projectId, sourceRef?.projectId === projectId ? sourceRef.artifactId : '', sourceRef?.revision);
  const history = project?.historyOrigin;
  const needsReport = (page === 'artifact' || page === 'workspace') && selected?.kind === 'experiment_history';
  const historySnapshot = useLabArtifact(projectId, needsReport ? history?.snapshotArtifactId ?? '' : '', history?.snapshotArtifactRevision);
  const snapshot = historySnapshot.data;
  const reportSnapshot = snapshot && snapshot.artifactId === history?.snapshotArtifactId && snapshot.revision === history.snapshotArtifactRevision
    && isExperimentSnapshot(snapshot.content) ? snapshot.content : undefined;
  const [experimentSelections, setExperimentSelections] = useState<Record<string, string>>({});
  const draftKey = `${workflow.connection}:${projectId}:${selectedId}`;
  const busy = Boolean(workflow.pending) || workflow.mutation.isPending;
  const activeError = workflow.mutation.error ?? workflow.project.error ?? workflow.catalog.error;
  const refresh = () => { void workflow.catalog.refetch(); if (projectId) void workflow.project.refetch(); };
  const start = async (current: LabProject) => {
    setSending(true); setNotice('');
    try { await sendProjectGuideMessage(transport, current, initialProjectMessage, `lab-project-start:${current.projectId}`); }
    catch (error) { setNotice(projectError(error, '消息未完成，已保留在项目对话中。')); }
    finally { setSending(false); if (currentProjectId.current === current.projectId) setPendingMessages(pendingProjectMessages(transport, current)); }
  };
  const created = async (receipt?: ProjectReceipt) => {
    if (!receipt) return;
    setNewProjectOpen(false);
    try { sessionStorage.removeItem(`paw.lab.new-project.v1:${workflow.connection}`); } catch { /* The created project is already durable. */ }
    openProject(receipt.project.projectId);
    if (receipt.project.guideSessionId) await start(receipt.project);
  };
  const ensureGuide = async () => {
    if (!project) return;
    const receipt = await workflow.submit('ensure_guide', {}, project);
    if (receipt) await start(receipt.project);
  };
  const continueProject = async (mode: ProjectGuidanceMode, current = project, request = '') => {
    if (!current || sending || guidancePending.current) return;
    guidancePending.current = true; setSending(true); setNotice(''); setGuideOpen(true);
    let connected: LabProject | undefined = current;
    try {
      connected = current.guideSessionId ? current : (await workflow.submit('ensure_guide', {}, current))?.project;
      if (!connected) return;
      await sendProjectGuideMessage(transport, connected, `${projectGuidanceMessage(mode)}${request ? `\n\n当前请求：${request}` : ''}`, `lab-project-continue:${crypto.randomUUID()}`);
    } catch (error) { setNotice(projectError(error, '继续请求未完成，已保留原消息。')); }
    finally { guidancePending.current = false; setSending(false); if (connected && currentProjectId.current === connected.projectId) setPendingMessages(pendingProjectMessages(transport, connected)); refresh(); }
  };
  const sendOptimizationDirection = async (direction: string) => {
    if (!project || sending) return;
    setSending(true); setGuideOpen(true); setNotice('');
    try { await sendProjectGuideMessage(transport, project, `开始下一轮 Agent Lab 实验。优化方向：${direction}。先读取当前项目成果、评测集、原始运行记录和上一轮指标，只改变这一层并保留其他冻结条件。若项目存在执行绑定，先用 execution_read 核对当前 suite/job；Prompt 或 Model 优化必须进入真实 Golden/Pi 实验，等待 completed 回执后再发布指标、原始聊天/Tool 回执和 Keep/Reject 判定。没有新的 completed 执行回执时，只记录 blocked/待执行状态，不得编造指标或把 Agent 建议当作运行结果。Tool、MCP/Workflow、检索等方向必须使用对应的已接入执行器；若当前项目没有该执行器，明确说明缺口，不要假称优化已完成。`, `lab-optimization-direction:${crypto.randomUUID()}`); }
    catch (error) { setNotice(projectError(error)); }
    finally { setSending(false); if (currentProjectId.current === project.projectId) setPendingMessages(pendingProjectMessages(transport, project)); }
  };
  const beginOptimization = async (direction: string) => {
    // Prompt and model optimization use the project-owned Golden runner. Open
    // that exact binding before sending the causal note so the next action is
    // a real experiment, rather than an Agent-only recommendation.
    if (direction === 'Prompt 优化' || direction === 'Model 优化') {
      const target = project?.bindings.find((item) => item.ownerRef.kind === 'golden_suite');
      if (target) updateView({ page: 'runs', bindingId: target.bindingId });
    }
    await sendOptimizationDirection(direction);
  };
  const act = async (action: ArtifactAction, values: Record<string, JsonValue>, staged = false) => {
    if (!project || !selected) return;
    const text = `${action.prompt}\n\n当前成果：${selected.title}\n成果引用：${selected.artifactId} · v${selected.revision}\n用户输入：${JSON.stringify(values)}`;
    setGuideOpen(true);
    if (staged) { setStagedAction({ text, title: selected.title }); return; }
    setSending(true); setNotice('');
    try { await sendProjectGuideMessage(transport, project, text, `lab-artifact-action:${crypto.randomUUID()}`); }
    catch (error) { setNotice(projectError(error)); }
    finally { setSending(false); if (currentProjectId.current === project.projectId) setPendingMessages(pendingProjectMessages(transport, project)); }
  };
  const recoverMessage = async (message: PendingProjectMessage) => {
    if (!project || sending) return;
    setSending(true); setNotice(''); setGuideOpen(true);
    const clientId = message.outcome === 'unknown' ? message.clientMessageId : `lab-project-retry:${crypto.randomUUID()}`;
    if (message.outcome === 'rejected') retainProjectMessage(transport, project, message, true);
    try { await sendProjectGuideMessage(transport, project, message.message, clientId); }
    catch (reason) { setNotice(projectError(reason)); }
    finally { setSending(false); if (currentProjectId.current === project.projectId) setPendingMessages(pendingProjectMessages(transport, project)); }
  };
  const renderArtifact = () => { if (!project) return null; return selected ? artifact.isPending ? <p className="lab-project-loading" role="status">正在读取成果…</p> : artifact.isError ? <div className="lab-project-error" role="alert"><p>{projectError(artifact.error)}</p><Button onClick={() => void artifact.refetch()}>重新读取成果</Button></div>
                    : artifact.data ? <ArtifactSurface artifact={artifact.data} draft={drafts[draftKey]} busy={busy || sending}
                      reportSnapshot={reportSnapshot} selectedExperiment={experimentSelections[projectId]} onSelectExperiment={(id) => setExperimentSelections((current) => ({ ...current, [projectId]: id }))}
                      onDraft={(draft) => updateDraft(draftKey, draft)}
                      onSave={async (content, revision) => Boolean(await workflow.submit('publish_artifact', { artifactId: artifact.data!.artifactId, expectedArtifactRevision: revision, content }, project))}
                      onAction={(action, values, staged) => void act(action, values, staged)} /> : null
                    : <section className="lab-project-empty"><span><MessageSquare size={24} /></span><h2>让成果跟随项目生长。</h2><p>和 Agent 继续工作，它会把适合当前项目的文档、表格、交互页面或其他成果放在这里。</p>{!guideOpen ? <Button onClick={() => setGuideOpen(true)}>打开项目对话</Button> : null}</section>; };
  const projectItems = workflow.catalog.data?.items ?? [];
  return <main className="eval-lab lab-project-workbench" aria-label="Agent Lab 项目工作台">
    <header className="lab-project-header">
      <div className="lab-project-heading">{projectId ? <IconButton icon={<ArrowLeft size={17} />} label="返回 Lab 项目" onClick={() => openProject('')} /> : <span className="lab-project-logo" aria-hidden="true"><FlaskConical size={21} /></span>}
        <div><small>Agent Lab</small><h1>{project?.title ?? (projectId ? '正在读取项目' : '工作台')}</h1></div></div>
      <div className="lab-project-header__actions"><IconButton icon={<RefreshCw size={16} />} label="重新读取 Lab 项目" onClick={refresh} disabled={workflow.catalog.isFetching || workflow.project.isFetching} />
        {project ? <><Button size="small" variant="primary" title={project.nextAction?.reason} disabled={busy || sending} onClick={() => void continueProject('guided')}>继续下一步</Button><Button size="small" onClick={() => setPage('brief')}>项目说明</Button>{project.directory?.path ? <IconButton icon={<FolderOpen size={16} />} label="在 Files 打开项目文件夹" onClick={() => openPawOsRoute(desktop, `/files?path=${encodeURIComponent(project.directory!.path)}`)} /> : null}<IconButton icon={<Upload size={16} />} label="添加材料" onClick={() => setIntakeOpen(true)} /><IconButton icon={guideOpen ? <PanelLeftClose size={17} /> : <PanelLeftOpen size={17} />} label={guideOpen ? '收起项目 Agent' : '展开项目 Agent'} onClick={() => setGuideOpen((value) => !value)} /></> : null}
        {!projectId ? <><Button size="small" onClick={() => setHistoryImportOpen(true)}><History size={14} />导入已有实验</Button></> : null}
        {onOpenHistory ? <Button size="small" onClick={onOpenHistory}><History size={14} />已有实验</Button> : null}</div>
    </header>
    {workflow.pending?.outcome === 'unknown' ? <div className="lab-project-notice" role="status"><p>上次操作的回执尚未确认，原请求和输入已保留。</p><Button disabled={workflow.mutation.isPending} onClick={() => void workflow.reconcile().then((receipt) => { if (receipt && workflow.pending?.command.action === 'create') void created(receipt); else if (receipt && workflow.pending?.command.action === 'import_history') { setHistoryImportOpen(false); openProject(receipt.project.projectId); } })}>核对原操作</Button></div> : null}
    {activeError ? <p className="lab-project-error" role="alert">{projectError(activeError)}</p> : null}
    {notice && !pendingMessages.length ? <p className="lab-project-notice" role="status">{notice}</p> : null}
    {pendingMessages.map((message) => <div className="lab-project-notice" role="status" key={message.clientMessageId}><p>{message.error || '项目消息的接纳结果尚未确认，原输入已保留。'}</p><Button disabled={sending} onClick={() => void recoverMessage(message)}>{message.outcome === 'unknown' ? '核对原消息' : '重新发送项目消息'}</Button></div>)}
    {!projectId ? workflow.catalog.isPending ? <p className="lab-project-loading" role="status">正在读取项目…</p> : <LabProjectHome items={projectItems} onOpen={openProject} onCreate={() => setNewProjectOpen(true)} /> : !project ? <div className="lab-project-loading" role="status">{workflow.project.isError ? '项目未能读取，已保存的工作仍保留。' : '正在恢复项目与成果…'}</div>
      : <><div className="lab-project-body" data-guide-open={guideOpen} data-workflow={page === 'workflow' || page === 'workspace'}>
        {guideOpen ? <ProjectGuide key={project.projectId} project={project} draftRequest={draftRequest} onNewProject={() => openProject('')} onProjectActivity={refresh} onEnsure={() => void ensureGuide()} /> : null}
        <div className="lab-project-results">
          <nav className="lab-project-tabs" aria-label="项目成果">
            <div className="lab-project-tabs__resources"><button aria-current={page === 'workspace' ? 'page' : undefined} onClick={() => setPage('workspace')}><LayoutDashboard size={15} />项目工作面</button><button aria-current={page === 'workflow' ? 'page' : undefined} onClick={() => setPage('workflow')}><GitBranch size={15} />工作流</button><button aria-current={page === 'lifecycle' ? 'page' : undefined} onClick={() => setPage('lifecycle')}><LayoutDashboard size={15} />实验闭环</button><button aria-current={page === 'materials' || page === 'knowledge' ? 'page' : undefined} onClick={() => setPage(project.knowledgeResources?.documentCount ? 'knowledge' : 'materials')}><Database size={15} />{project.knowledgeResources?.documentCount ? `知识库 ${project.knowledgeResources.documentCount} 篇` : `材料 ${project.materialCount}`}</button>
            <button aria-current={page === 'runs' ? 'page' : undefined} onClick={() => setPage('runs')}><Play size={15} />运行</button><button aria-current={page === 'artifact' ? 'page' : undefined} onClick={() => setPage('artifact')}><Layers3 size={15} />成果 {project.artifactCount}</button>
            <button aria-current={page === 'apps' ? 'page' : undefined} onClick={() => setPage('apps')}><PackageCheck size={15} />应用交付</button>
            </div>
            {page === 'materials' || page === 'knowledge' ? <div className="lab-project-tabs__artifacts"><button aria-current={page === 'materials' ? 'page' : undefined} onClick={() => setPage('materials')}>项目材料</button><button aria-current={page === 'knowledge' ? 'page' : undefined} onClick={() => setPage('knowledge')}>知识库实验</button></div> : null}
            {page === 'artifact' && project.artifacts.length ? <div className="lab-project-tabs__artifacts">{project.workspace.artifactOrder.map((id) => project.artifacts.find((item) => item.artifactId === id)).filter((item) => item !== undefined).map((item) => <button key={item.artifactId} title={item.title} aria-current={selectedId === item.artifactId ? 'page' : undefined} onClick={() => updateView({ artifactId:item.artifactId, page:'artifact', bindingId:undefined })}><FileText size={14} /><span>{item.title}</span></button>)}</div> : null}
          </nav>
          {stagedAction ? <div className="lab-project-notice" role="status"><span>“{stagedAction.title}”提供了一项交互输入。</span><Button size="small" onClick={() => { setDraftRequest({ id: Date.now(), text: stagedAction.text }); setStagedAction(undefined); setGuideOpen(true); }}>带入对话</Button><Button size="small" onClick={() => setStagedAction(undefined)}>关闭</Button></div> : null}
          <div className="lab-project-stage">
            {page === 'workspace' ? <LabProjectSpace key={project.projectId} project={project} artifactId={selectedId} artifactContent={renderArtifact()} sourceContent={sourceRef?.projectId === projectId ? sourceArtifact.isPending ? <p role="status">正在读取来源成果…</p> : sourceArtifact.isError ? <p role="alert">{projectError(sourceArtifact.error)}</p> : sourceArtifact.data ? <ArtifactSurface artifact={sourceArtifact.data} busy onDraft={() => undefined} onSave={async () => false} onAction={() => undefined} /> : null : undefined} onOpenSource={(node) => node.ref.kind === 'artifact' ? setSourceRef({ projectId, artifactId: node.ref.id, revision: typeof node.ref.version === 'number' ? node.ref.version : undefined }) : openWorkflowNode(node)} onCloseSource={() => setSourceRef(undefined)} onSelectArtifact={(artifactId) => updateView({ page: 'workspace', artifactId })} onOpenNode={openWorkflowNode} onOpenGraph={() => setPage('workflow')} onOpenRuns={() => setPage('runs')} onOpenApps={() => setPage('apps')} onOpenChat={() => setGuideOpen(true)} onOpenFile={(path) => openPawOsRoute(desktop, `/files?path=${encodeURIComponent(path)}`)} />
              : page === 'workflow' ? <LabWorkflowGraph key={`${workflow.connection}:${project.projectId}`} projectId={project.projectId} connection={workflow.connection} workflow={project.workflow} onOpenNode={openWorkflowNode} onOpenMaterials={() => setPage('materials')} onOpenRuns={() => setPage('runs')} onOpenApps={() => setPage('apps')} onOpenExperiments={() => setPage('lifecycle')} />
              : page === 'materials' ? <Materials project={project} onAdd={() => setIntakeOpen(true)} />
              : page === 'knowledge' ? <LabKnowledge key={project.projectId} initialJobId={view.jobId} project={project} busy={busy} onCommand={(input) => workflow.submit('knowledge', input, project)} onBind={(input) => workflow.submit('bind_execution', input, project)} onOpenBinding={(binding) => updateView({ page: 'runs', bindingId: binding.bindingId })} />
              : page === 'lifecycle' ? <LabExperimentLifecycle project={project} busy={busy || sending} onOpenArtifact={(artifactId) => updateView({ page: 'artifact', artifactId, bindingId: undefined })} onOpenRuns={() => updateView({ page: 'runs', bindingId: undefined })} onDirection={(direction) => void beginOptimization(direction)} onContinue={(mode) => void continueProject(mode)} onAddMaterials={() => setIntakeOpen(true)} onOpenMaterials={() => setPage('materials')} onOpenKnowledge={() => setPage('knowledge')} onOpenApps={() => setPage('apps')} />
              : page === 'apps' ? <LabAppDelivery key={project.projectId} projectId={project.projectId} initialAppId={view.appId} initialVersion={view.appVersion} initialCallId={view.appCallId} onOpenEvaluation={(suiteId, jobId) => { const owner = project.bindings.find((item) => item.ownerRef.kind === 'golden_suite' && item.ownerRef.id === suiteId); if (owner) updateView({ page: 'runs', bindingId: owner.bindingId, jobId }); else setNotice('原评测绑定暂不可读取，应用版本与配置仍保留。'); }} preparing={workflow.pending?.command.projectId === projectId && workflow.pending.command.action === 'prepare_app' && workflow.pending.outcome === 'sending'} blocked={busy || sending} evaluationSelection={view.evaluationSelection} onClearSelection={() => updateView({ evaluationSelection: undefined })} onGuide={() => void continueProject('guided', project, '把当前项目方案实现为可试用、可导出的应用，保留过程可视化与来源；检查已有版本后再准备新版本。')} onPrepare={async (directory, appId, evaluationSelection) => Boolean(await workflow.submit('prepare_app', { directory, ...(appId ? { appId } : {}), ...(evaluationSelection ? { evaluationSelection } : {}) }, project))} />
              : page === 'brief' ? <section className="lab-project-brief"><small>用户描述 · v{project.briefVersion}</small><h2>{project.title}</h2><p>{project.description}</p><p className="lab-project-muted">需要调整方向时，可以直接告诉项目 Agent。</p></section>
                : page === 'runs' ? binding?.ownerRef.kind === 'golden_suite' ? <GoldenWorkflow onSelectDelivery={(evaluationSelection) => updateView({ page: 'apps', evaluationSelection })} key={binding.bindingId} initialSuiteId={binding.ownerRef.id} initialJobId={view.jobId} onClose={() => setBinding(undefined)} />
                  : binding?.ownerRef.kind === 'scene_trial' ? <section className="lab-project-bindings"><h2>继续场景验证</h2><p>历史对照保存在项目成果中。这里使用执行器当前登记的资料与规则，新运行单独记录；不会覆盖历史成绩。</p><SceneTrialPanel sceneId={binding.ownerRef.id} /></section>
                  : <ProjectBindingList bindings={project.bindings} onOpen={setBinding} />
                  : renderArtifact()}
            {sessionRecord?.projectId === projectId ? <LabSessionRecord key={`${sessionRecord.sessionId}:${sessionRecord.turnId ?? ''}`} sessionId={sessionRecord.sessionId} turnId={sessionRecord.turnId} onClose={() => setSessionRecord(undefined)} /> : null}
            {knowledgeRecord?.projectId === projectId ? <LabKnowledgeRecord projectId={projectId} jobId={knowledgeRecord.jobId} onClose={() => setKnowledgeRecord(undefined)} onOpenKnowledge={() => { updateView({ page: 'knowledge', jobId: knowledgeRecord.jobId }); setKnowledgeRecord(undefined); }} /> : null}
          </div>
        </div>
      </div></>}
    <Dialog.Root open={intakeOpen && Boolean(project)} onOpenChange={setIntakeOpen}><Dialog.Overlay className="lab-project-modal-backdrop" /><Dialog.Content className="lab-project-modal" aria-describedby={undefined}><header><Dialog.Title asChild><h2>添加项目材料</h2></Dialog.Title><Dialog.Close asChild><Button size="small">关闭</Button></Dialog.Close></header>{project ? <MaterialIntake busy={busy || sending} onAdd={async (input, resume) => { const receipt = await workflow.submit('import_materials', input, project); if (receipt) { setIntakeOpen(false); setPage('materials'); if (resume) await continueProject('guided', receipt.project); } }} /> : null}</Dialog.Content></Dialog.Root>
    <Dialog.Root open={newProjectOpen && !projectId} onOpenChange={setNewProjectOpen}><Dialog.Overlay className="lab-project-modal-backdrop" /><Dialog.Content className="lab-project-modal" aria-describedby="lab-new-project-description"><header><Dialog.Title asChild><h2>新建项目</h2></Dialog.Title><Dialog.Close asChild><Button size="small">关闭</Button></Dialog.Close></header><p id="lab-new-project-description">先保存项目和材料，再由 Agent 在同一个项目里继续组织计划、实验与交付。创建不会伪造评测结果。</p><NewProject key={`dialog:${workflow.connection}`} connection={workflow.connection} busy={busy || sending} onCreate={async (input) => created(await workflow.submit('create', input))} /></Dialog.Content></Dialog.Root>
    <Dialog.Root open={historyImportOpen} onOpenChange={setHistoryImportOpen}><Dialog.Overlay className="lab-project-modal-backdrop" /><Dialog.Content className="lab-project-modal" aria-describedby="lab-history-import-description"><header><Dialog.Title asChild><h2>导入已有实验</h2></Dialog.Title><Dialog.Close asChild><Button size="small">关闭</Button></Dialog.Close></header><p id="lab-history-import-description">把已有基线、候选、失败记录和原始指标保留为项目成果。导入不会运行模型或重新评分。</p>
      {workflow.catalog.data?.historyUnavailable ? <p role="alert">已有实验来源暂时不可读取，请重新读取后再试。</p> : null}
      {workflow.catalog.data?.historyCollections?.map((source) => { const imported = workflow.catalog.data?.items.find((item) => item.historyOrigin?.sceneId === source.sceneId); return <section key={source.sceneId} className="lab-project-bindings" aria-label={source.title}><h3>{source.title}</h3><p>{source.experimentCount} 条实验记录 · {source.datasetIds.length} 个数据集版本</p><Button disabled={busy} onClick={() => { if (imported) { setHistoryImportOpen(false); openProject(imported.projectId); } else void workflow.submit('import_history', { sceneId: source.sceneId, sourceHash: source.sourceHash }).then((receipt) => { if (receipt) { setHistoryImportOpen(false); openProject(receipt.project.projectId); } }); }}>{imported ? '打开已导入项目' : '导入为项目'}</Button></section>; })}
      {!workflow.catalog.isPending && !workflow.catalog.data?.historyUnavailable && !workflow.catalog.data?.historyCollections?.length ? <p>当前执行器没有可导入的已有实验。</p> : null}
    </Dialog.Content></Dialog.Root>
  </main>;
}

function NewProject({ connection, busy, onCreate }: { connection: string; busy: boolean; onCreate: (input: Record<string, JsonValue>) => Promise<void> }) {
  const key = `paw.lab.new-project.v1:${connection}`;
  const [saved] = useState(() => { try { return object(JSON.parse(sessionStorage.getItem(key) ?? '{}')); } catch { return {}; } });
  const [description, setDescription] = useState(typeof saved.description === 'string' ? saved.description : '');
  const [path, setPath] = useState(typeof saved.path === 'string' ? saved.path : '');
  const [materials, setMaterials] = useState<Intake[]>(() => Array.isArray(saved.materials) ? saved.materials.filter((item): item is Intake => typeof object(item).title === 'string' && typeof object(item).text === 'string') : []);
  const [error, setError] = useState(''); const [reading, setReading] = useState(false);
  useEffect(() => {
    try { sessionStorage.setItem(key, JSON.stringify({ description, path, materials })); }
    catch { setError('浏览器暂时无法保存草稿，请保持当前页面并完成创建。'); }
  }, [key, description, path, materials]);
  const chooseFiles = async (list: FileList | null) => {
    if (!list?.length || reading || busy) return;
    setReading(true); setError('');
    try { setMaterials(await loadFiles(list)); setPath(''); }
    catch (reason) { setError(projectError(reason)); }
    finally { setReading(false); }
  };
  const submit = async (event: FormEvent) => { event.preventDefault(); if (!description.trim() || busy || reading) return; await onCreate({ description: description.trim(), ...(materials.length ? { materials } : path.trim() ? { path: path.trim() } : {}) }); };
  return <form className="lab-project-compose" onSubmit={(event) => void submit(event)}><textarea aria-label="描述你的项目" placeholder="例如：我有一个处理售后问题的 Agent，希望它依据规则完成操作。也可以从一个新的业务想法开始。" rows={5} value={description} onChange={(event) => setDescription(event.target.value)} disabled={busy} />
    <div className="lab-project-compose__materials"><label className="lab-project-file-picker"><Upload size={15} />{reading ? '正在读取文件…' : '添加文件'}<input type="file" multiple disabled={busy || reading} onChange={(event) => { void chooseFiles(event.target.files); event.target.value = ''; }} /></label>
      {materials.length ? <><span>{materials.length} 份文件已选，创建时上传</span><Button type="button" size="small" disabled={busy || reading} onClick={() => setMaterials([])}>移除已选文件</Button></> : <input aria-label="连接执行器上的材料路径" value={path} placeholder="或填写已连接执行器上的绝对路径" disabled={busy || reading} onChange={(event) => setPath(event.target.value)} />}</div>
    {error ? <p role="alert" className="lab-project-error">{error}</p> : null}<footer><span>项目内容由 Agent 按实际任务组织。</span><Button type="submit" variant="primary" disabled={busy || reading || !description.trim()}>{busy ? '正在建立项目…' : reading ? '正在读取材料…' : '创建并开始'}<ArrowUpRight size={16} /></Button></footer>
  </form>;
}
function MaterialIntake({ busy, onAdd }: { busy: boolean; onAdd: (input: Record<string, JsonValue>, resume?: boolean) => Promise<void> }) {
  const [path, setPath] = useState(''); const [title, setTitle] = useState(''); const [text, setText] = useState(''); const [error, setError] = useState('');
  const [resume, setResume] = useState(true);
  return <div className="lab-project-intake"><p>选择文件、输入路径或粘贴正文。材料保存后可以直接继续评测准备。</p><label className="lab-project-intake__resume"><input type="checkbox" checked={resume} disabled={busy} onChange={(event) => setResume(event.target.checked)} />材料接入后，让 Agent 继续推进</label><label className="lab-project-file-picker"><Upload size={16} />上传文本或源码文件<input type="file" multiple disabled={busy} onChange={(event) => { const files = event.target.files; if (!files?.length) return; void loadFiles(files).then((materials) => onAdd({ materials }, resume)).catch((reason) => setError(projectError(reason))); }} /></label>
    <label>已连接执行器上的路径<input value={path} placeholder="绝对路径" onChange={(event) => setPath(event.target.value)} disabled={busy} /></label><Button disabled={busy || !path.trim()} onClick={() => void onAdd({ path: path.trim() }, resume)}>读取路径</Button>
    <hr /><label>粘贴材料标题<input value={title} onChange={(event) => setTitle(event.target.value)} disabled={busy} /></label><label>材料正文<textarea rows={7} value={text} onChange={(event) => setText(event.target.value)} disabled={busy} /></label>
    <Button disabled={busy || !title.trim() || !text.trim()} onClick={() => void onAdd({ materials: [{ title: title.trim(), text }] }, resume)}>保存文本材料</Button>{error ? <p role="alert" className="lab-project-error">{error}</p> : null}</div>;
}
function Materials({ project, onAdd }: { project: LabProject; onAdd: () => void }) {
  const [selectedId, setSelectedId] = useState(''); const selected = project.materialSet.materials.find((item) => item.sourceId === selectedId) ?? project.materialSet.materials[0];
  return <section className="lab-project-materials"><header><div><small>材料快照 · v{project.materialSet.version}</small><h2>项目实际材料</h2></div><Button onClick={onAdd}><Plus size={14} />添加材料</Button></header>
    {project.intake.issues.length ? <details className="lab-project-notice"><summary>最近接入有 {project.intake.skippedCount} 项未读取</summary><ul>{project.intake.issues.map((issue, index) => <li key={index}><strong>{issue.title}</strong>：{issue.message}</li>)}</ul></details> : null}
    {!selected ? <p>尚未读取材料。可以上传文件、粘贴正文或连接执行器路径。</p> : <div className="lab-project-materials__body"><nav aria-label="材料文件">{project.materialSet.materials.map((item) => <button key={item.sourceId} aria-current={selected.sourceId === item.sourceId ? 'page' : undefined} onClick={() => setSelectedId(item.sourceId)}><FileText size={15} />{item.title}<small>{item.kind}</small></button>)}</nav><article><header><h3>{selected.title}</h3><small>{selected.byteSize.toLocaleString()} bytes · 已保存快照</small></header><pre>{selected.text}</pre></article></div>}
  </section>;
}
async function loadFiles(list: FileList | null): Promise<Intake[]> {
  const files = [...(list ?? [])]; if (!files.length || files.length > 100 || files.reduce((total, file) => total + file.size, 0) > 2_000_000) throw new Error('请选择 1–100 份文件，合计不超过 2 MB。');
  return Promise.all(files.map(async (file) => {
    if (file.size > 500_000) throw new Error(`${file.name} 超过 500 KB，请拆分后添加。`);
    let text: string; try { text = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer()); } catch { throw new Error(`${file.name} 尚不能作为 UTF-8 文本读取。`); }
    if (!text.trim() || text.includes('\0')) throw new Error(`${file.name} 没有可用的文本内容。`);
    return { title: file.name, text, kind: file.name === 'SKILL.md' ? 'skill' : /\.(?:py|[cm]?js|jsx|ts|tsx|go|rs|java|swift)$/iu.test(file.name) ? 'code' : 'document' };
  }));
}
