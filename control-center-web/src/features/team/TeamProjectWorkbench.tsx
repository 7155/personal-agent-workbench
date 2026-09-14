import {
  BookOpen,
  CheckCircle2,
  CircleAlert,
  CircleDashed,
  CircleStop,
  ExternalLink,
  FileCheck2,
  GitBranch,
  History,
  LoaderCircle,
  PencilLine,
  RefreshCw,
  Settings2,
  Users,
  Wrench,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { Button } from '@/components/primitives';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import { TeamApiError, teamApiErrorMessage } from './team-api';
import { TeamManagementDialog } from './TeamManagementDialog';
import { TeamProjectPreviewCard } from './TeamProjectPreview';
import { useTeam } from './team-context';
import type {
  TeamProjectBrief,
  TeamProjectBriefUpdateInput,
  TeamProjectMember,
  TeamProjectOverview,
  TeamProjectRoom,
  TeamProjectRoomWorkItem,
  TeamProjectDraft,
  TeamSpaceRole,
} from './types';
import './team-project-workbench.css';

export interface TeamProjectWorkbenchProps {
  onNavigate?: (page: 'planning' | 'documents') => void;
}

export function TeamProjectWorkbench({ onNavigate }: TeamProjectWorkbenchProps) {
  const team = useTeam();
  const desktop = usePawOsDesktop();
  const project = team.activeSpace?.kind === 'project' ? team.activeSpace : null;
  const projectId = project?.id ?? '';
  const [overview, setOverview] = useState<TeamProjectOverview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [managementOpen, setManagementOpen] = useState(false);
  const [editingBrief, setEditingBrief] = useState(false);
  const [editBaseRevision, setEditBaseRevision] = useState<number | null>(null);
  const [objectiveDraft, setObjectiveDraft] = useState('');
  const [criteriaDraft, setCriteriaDraft] = useState('');
  const [savingBrief, setSavingBrief] = useState(false);
  const [briefError, setBriefError] = useState('');
  const [conflictNotice, setConflictNotice] = useState('');
  const [savedNotice, setSavedNotice] = useState('');
  const [requirementsBusySessionId, setRequirementsBusySessionId] = useState('');
  const [requirementsError, setRequirementsError] = useState('');
  const [requirementsSavedNotice, setRequirementsSavedNotice] = useState('');
  const requestRef = useRef(0);
  const refreshInFlightProjectRef = useRef<string | null>(null);
  const refreshPromiseRef = useRef<Promise<TeamProjectOverview> | null>(null);
  const currentProjectRef = useRef(projectId);
  currentProjectRef.current = projectId;

  const refresh = useCallback(async ({ background = false }: { background?: boolean } = {}) => {
    if (!projectId) return;
    // Polls can reuse the in-flight read. A foreground refresh after a write
    // must wait for that older read, discard it, and obtain a fresh snapshot.
    while (refreshInFlightProjectRef.current === projectId && refreshPromiseRef.current) {
      if (background) return;
      requestRef.current += 1;
      await refreshPromiseRef.current.catch(() => undefined);
      if (currentProjectRef.current !== projectId) return;
    }
    refreshInFlightProjectRef.current = projectId;
    const requestId = ++requestRef.current;
    if (!background) {
      setLoading(true);
      setError('');
    }
    try {
      const pending = team.getProjectOverview(projectId);
      refreshPromiseRef.current = pending;
      const next = await pending;
      if (requestId !== requestRef.current) return;
      setOverview(next);
      setError('');
    } catch (reason) {
      if (requestId !== requestRef.current) return;
      setError(teamApiErrorMessage(reason, '团队服务暂时不可用，请稍后重试。'));
    } finally {
      if (refreshInFlightProjectRef.current === projectId) {
        refreshInFlightProjectRef.current = null;
        refreshPromiseRef.current = null;
      }
      if (requestId === requestRef.current && !background) setLoading(false);
    }
  }, [projectId, team.getProjectOverview]);

  useEffect(() => {
    currentProjectRef.current = projectId;
    requestRef.current += 1;
    setOverview(null);
    setError('');
    setEditingBrief(false);
    setEditBaseRevision(null);
    setObjectiveDraft('');
    setCriteriaDraft('');
    setBriefError('');
    setConflictNotice('');
    setSavedNotice('');
    setRequirementsBusySessionId('');
    setRequirementsError('');
    setRequirementsSavedNotice('');
    if (projectId) void refresh();
    return () => {
      requestRef.current += 1;
      if (currentProjectRef.current === projectId) currentProjectRef.current = '';
    };
  }, [projectId, refresh]);

  useEffect(() => {
    if (!projectId) return;
    const syncWhenVisible = () => {
      if (!document.hidden) void refresh({ background: true });
    };
    const interval = window.setInterval(syncWhenVisible, 5_000);
    window.addEventListener('focus', syncWhenVisible);
    document.addEventListener('visibilitychange', syncWhenVisible);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener('focus', syncWhenVisible);
      document.removeEventListener('visibilitychange', syncWhenVisible);
    };
  }, [projectId, refresh]);

  useEffect(() => {
    if (!overview || editingBrief) return;
    setObjectiveDraft(overview.brief.objective);
    setCriteriaDraft(overview.brief.acceptanceCriteria.join('\n'));
  }, [editingBrief, overview]);

  const canEditBrief = project?.role === 'owner' || project?.role === 'maintainer';
  const acceptanceCriteria = useMemo(
    () => criteriaDraft.split(/\r?\n/u).map((item) => item.trim()).filter(Boolean),
    [criteriaDraft],
  );
  const briefValidation = useMemo(
    () => validateBriefDraft(objectiveDraft, acceptanceCriteria),
    [acceptanceCriteria, objectiveDraft],
  );
  const briefDirty = Boolean(overview && (
    objectiveDraft.trim() !== overview.brief.objective
    || !sameStringArray(acceptanceCriteria, overview.brief.acceptanceCriteria)
  ));

  function beginBriefEdit(): void {
    if (!overview || !canEditBrief) return;
    setObjectiveDraft(overview.brief.objective);
    setCriteriaDraft(overview.brief.acceptanceCriteria.join('\n'));
    setEditBaseRevision(overview.brief.revision);
    setBriefError('');
    setConflictNotice('');
    setSavedNotice('');
    setEditingBrief(true);
  }

  function cancelBriefEdit(): void {
    setEditingBrief(false);
    setEditBaseRevision(null);
    setBriefError('');
    setConflictNotice('');
    if (overview) {
      setObjectiveDraft(overview.brief.objective);
      setCriteriaDraft(overview.brief.acceptanceCriteria.join('\n'));
    }
  }

  async function saveBrief(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!project || !overview || !canEditBrief || savingBrief) return;
    if (briefValidation) {
      setBriefError(briefValidation);
      return;
    }
    const input: TeamProjectBriefUpdateInput = {
      baseRevision: editBaseRevision ?? overview.brief.revision,
      objective: objectiveDraft.trim(),
      acceptanceCriteria,
    };
    setSavingBrief(true);
    setBriefError('');
    setConflictNotice('');
    setSavedNotice('');
    try {
      const brief = await team.updateProjectBrief(project.id, input);
      setOverview((current) => current ? {
        ...current,
        brief,
        briefHistory: [brief, ...current.briefHistory.filter((item) => item.revision !== brief.revision)].slice(0, 20),
      } : current);
      setEditingBrief(false);
      setEditBaseRevision(null);
      setSavedNotice(`需求 v${brief.revision} 已保存。新的执行仍需组织。`);
      await refresh();
    } catch (reason) {
      if (isBriefConflict(reason)) {
        setConflictNotice('项目需求已被其他成员更新。你的草稿已保留，请对照当前版本后再保存。');
        await refresh();
      } else {
        setBriefError(teamApiErrorMessage(reason, '保存项目需求失败，请重试。'));
      }
    } finally {
      setSavingBrief(false);
    }
  }

  function continueBriefFromServer(): void {
    if (!overview || !canEditBrief) return;
    setEditBaseRevision(overview.brief.revision);
    setConflictNotice('');
    setBriefError('');
    setSavedNotice('');
  }

  function openSession(sessionId: string, title: string, ownerDisplayName?: string): void {
    const request = {
      appId: 'agent' as const,
      target: {
        kind: 'session' as const,
        id: sessionId,
        title,
        subtitle: ownerDisplayName ? `${ownerDisplayName} · 项目任务` : '项目任务',
      },
    };
    if (desktop?.openWindow) desktop.openWindow(request);
    else openPawOsRoute(desktop, `/agent?session=${encodeURIComponent(sessionId)}`);
  }

  async function adoptWorkItemRequirements(item: TeamProjectRoomWorkItem): Promise<void> {
    if (
      !project
      || !overview
      || !item.ownerSessionId
      || item.ownerRequirementsRevision === undefined
      || item.currentOwnerUserId !== team.user?.id
      || requirementsBusySessionId
    ) return;
    setRequirementsBusySessionId(item.ownerSessionId);
    setRequirementsError('');
    setRequirementsSavedNotice('');
    try {
      const result = await team.adoptSessionRequirements(project.id, item.ownerSessionId, {
        baseRevision: item.ownerRequirementsRevision,
        revision: overview.brief.revision,
      });
      setRequirementsSavedNotice(`已将“${item.objective}”的需求基线更新为 v${result.requirementsRevision}。后续执行不会自动开始。`);
      await refresh();
    } catch (reason) {
      if (isBriefConflict(reason)) {
        setRequirementsError('项目需求或此任务基线已变化，当前信息已刷新；请再次确认后重试。');
        await refresh();
      } else {
        setRequirementsError(teamApiErrorMessage(reason, '更新任务需求基线失败，请重试。'));
      }
    } finally {
      setRequirementsBusySessionId('');
    }
  }

  function openRoom(room: TeamProjectRoom): void {
    const request = {
      appId: 'agent' as const,
      target: {
        kind: 'room' as const,
        id: room.id,
        title: room.title,
        subtitle: `${room.participantCount} 位成员`,
      },
    };
    if (desktop?.openWindow) desktop.openWindow(request);
    else openPawOsRoute(desktop, `/agent?room=${encodeURIComponent(room.id)}`);
  }

  function openKnowledge(): void {
    if (desktop?.openApp) desktop.openApp('knowledge', '/knowledge');
    else openPawOsRoute(desktop, '/knowledge');
  }

  if (!project) {
    return (
      <section aria-label="项目工作台" className="team-project-workbench team-project-workbench--empty">
        <div className="team-project-workbench__empty-state">
          <CircleDashed aria-hidden="true" size={28} />
          <h1>请选择项目空间</h1>
          <p>从账户与工作空间菜单进入项目后，这里会显示目标、成员、Room 和固定交付。</p>
        </div>
      </section>
    );
  }

  const headingId = 'team-project-workbench-title';
  return (
    <section
      aria-labelledby={headingId}
      className="team-project-workbench"
      data-loading={loading || undefined}
      data-project-id={project.id}
      data-project-role={project.role}
    >
      <header className="team-project-workbench__header">
        <div className="team-project-workbench__heading">
          <h1 id={headingId}>{project.name}</h1>
          <p>项目工作台 · {spaceRoleLabel(project.role)} · 每 5 秒自动同步</p>
        </div>
        <div className="team-project-workbench__header-actions">
          {onNavigate ? (
            <div aria-label="项目导航" className="team-project-workbench__nav" role="group">
              <button onClick={() => onNavigate('planning')} type="button">任务</button>
              <button onClick={() => onNavigate('documents')} type="button">工作文档</button>
            </div>
          ) : null}
          <button aria-label="刷新项目工作台" disabled={loading} onClick={() => void refresh()} type="button">
            <RefreshCw aria-hidden="true" className={loading ? 'ui-spin' : undefined} size={14} />
            <span>刷新</span>
          </button>
          <Button leadingIcon={<BookOpen size={14} />} onClick={openKnowledge} size="small" variant="quiet">项目资料</Button>
          <Button leadingIcon={<Settings2 size={14} />} onClick={() => setManagementOpen(true)} size="small" variant="quiet">管理成员与交付</Button>
        </div>
      </header>

      {error ? (
        <div className="team-project-workbench__error" role="alert">
          <CircleAlert aria-hidden="true" size={16} />
          <span>{error}</span>
          <button onClick={() => void refresh()} type="button">重新读取</button>
        </div>
      ) : null}
      {requirementsError ? <div className="team-project-workbench__error team-project-workbench__requirements-error" role="alert"><CircleAlert aria-hidden="true" size={16} /><span>{requirementsError}</span></div> : null}
      {requirementsSavedNotice ? <p className="team-project-workbench__saved team-project-workbench__requirements-saved" role="status">{requirementsSavedNotice}</p> : null}

      {loading && !overview ? <ProjectWorkbenchSkeleton /> : overview ? (
        <div className="team-project-workbench__body">
          <main className="team-project-workbench__main">
            <BriefSection
              brief={overview.brief}
              briefHistory={overview.briefHistory}
              briefHistoryTruncated={overview.truncated.briefHistory}
              briefValidation={briefValidation}
              canEdit={canEditBrief}
              criteriaDraft={criteriaDraft}
              conflictNotice={conflictNotice}
              editBaseRevision={editBaseRevision}
              briefError={briefError}
              dirty={briefDirty}
              editing={editingBrief}
              objectiveDraft={objectiveDraft}
              savedNotice={savedNotice}
              saving={savingBrief}
              onBeginEdit={beginBriefEdit}
              onCancel={cancelBriefEdit}
              onCriteriaChange={setCriteriaDraft}
              onObjectiveChange={setObjectiveDraft}
              onContinueFromServer={continueBriefFromServer}
              onSave={saveBrief}
            />
            <RoomsSection
              currentRequirementsRevision={overview.brief.revision}
              currentUserId={team.user?.id}
              requirementsBusySessionId={requirementsBusySessionId}
              rooms={overview.rooms}
              truncated={overview.truncated.rooms || overview.truncated.workItems}
              onAdoptRequirements={adoptWorkItemRequirements}
              onOpenSession={openSession}
              onOpenRoom={openRoom}
            />
          </main>
          <aside className="team-project-workbench__aside">
            <MembersSection members={overview.members} />
            <DeliveriesSection
              drafts={overview.drafts}
              repository={overview.repository}
              currentRequirementsRevision={overview.brief.revision}
              truncated={overview.truncated.drafts}
              memberById={new Map(overview.members.map((member) => [member.id, member]))}
              onManage={() => setManagementOpen(true)}
            />
            <TeamProjectPreviewCard />
            <RuntimeSection configured={overview.runtime.configured} />
          </aside>
        </div>
      ) : (
        <div className="team-project-workbench__empty-state">
          <CircleDashed aria-hidden="true" size={28} />
          <h2>项目资料暂时没有显示</h2>
          <p>重新读取项目概览，继续查看需求和协作记录。</p>
          <button onClick={() => void refresh()} type="button">重新读取</button>
        </div>
      )}

      <TeamManagementDialog onOpenChange={setManagementOpen} open={managementOpen} />
    </section>
  );
}

function BriefSection({
  brief,
  briefHistory,
  briefHistoryTruncated,
  briefError,
  briefValidation,
  canEdit,
  criteriaDraft,
  conflictNotice,
  dirty,
  editBaseRevision,
  editing,
  objectiveDraft,
  savedNotice,
  saving,
  onBeginEdit,
  onCancel,
  onCriteriaChange,
  onContinueFromServer,
  onObjectiveChange,
  onSave,
}: {
  brief: TeamProjectBrief;
  briefHistory: TeamProjectBrief[];
  briefHistoryTruncated: boolean;
  briefError: string;
  briefValidation: string;
  canEdit: boolean;
  criteriaDraft: string;
  conflictNotice: string;
  dirty: boolean;
  editBaseRevision: number | null;
  editing: boolean;
  objectiveDraft: string;
  savedNotice: string;
  saving: boolean;
  onBeginEdit: () => void;
  onCancel: () => void;
  onCriteriaChange: (value: string) => void;
  onContinueFromServer: () => void;
  onObjectiveChange: (value: string) => void;
  onSave: (event: FormEvent<HTMLFormElement>) => Promise<void>;
}) {
  return (
    <section aria-labelledby="team-project-brief-title" className="team-project-workbench__brief">
      <header className="team-project-workbench__section-header">
        <div>
          <h2 id="team-project-brief-title">当前目标与验收</h2>
          <p>项目需求版本决定接下来要交付什么</p>
        </div>
        <div className="team-project-workbench__section-actions">
          <span className="team-project-workbench__version">需求 v{brief.revision}</span>
          {canEdit && !editing ? (
            <button aria-label="编辑当前目标" className="team-project-workbench__quiet-action" onClick={onBeginEdit} type="button">
              <PencilLine aria-hidden="true" size={14} />编辑
            </button>
          ) : null}
        </div>
      </header>
      {editing ? (
        <form aria-label="编辑项目需求" className="team-project-workbench__brief-editor" onSubmit={(event) => void onSave(event)}>
          <label htmlFor="team-project-objective">当前目标</label>
          <textarea
            id="team-project-objective"
            maxLength={4_000}
            onChange={(event) => onObjectiveChange(event.target.value)}
            placeholder="写下项目要交付的结果"
            rows={4}
            value={objectiveDraft}
          />
          <div className="team-project-workbench__field-meta">{objectiveDraft.length}/4000</div>
          <label htmlFor="team-project-criteria">验收条件（每行一项）</label>
          <textarea
            id="team-project-criteria"
            onChange={(event) => onCriteriaChange(event.target.value)}
            placeholder="正常注册成功\n重复邮箱有明确提示\n错误状态能恢复"
            rows={5}
            value={criteriaDraft}
          />
          <div className="team-project-workbench__field-meta">{criteriaDraft.split(/\r?\n/u).filter((item) => item.trim()).length}/20 项 · 每项最多 500 字</div>
          {briefValidation ? <p className="team-project-workbench__inline-error" role="alert">{briefValidation}</p> : null}
          {briefError && !briefValidation ? <p className="team-project-workbench__inline-error" role="alert">{briefError}</p> : null}
          <p className="team-project-workbench__edit-base">本次编辑基于需求 v{editBaseRevision ?? brief.revision}；刷新不会替换你的草稿。</p>
          {conflictNotice ? (
            <div className="team-project-workbench__conflict" role="alert">
              <strong>{conflictNotice}</strong>
              <div className="team-project-workbench__conflict-compare">
                <div>
                  <span>你的草稿</span>
                  <p>{objectiveDraft || '未填写目标'}</p>
                  {criteriaDraft.split(/\r?\n/u).map((item) => item.trim()).filter(Boolean).length ? (
                    <ul>{criteriaDraft.split(/\r?\n/u).map((item) => item.trim()).filter(Boolean).map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul>
                  ) : <small>未填写验收条件</small>}
                </div>
                <div>
                  <span>服务器当前 v{brief.revision}</span>
                  <p>{brief.objective || '未填写目标'}</p>
                  {brief.acceptanceCriteria.length ? <ul>{brief.acceptanceCriteria.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul> : <small>未填写验收条件</small>}
                </div>
              </div>
              <button className="team-project-workbench__rebase-action" onClick={onContinueFromServer} type="button">保留草稿，基于 v{brief.revision} 继续编辑</button>
            </div>
          ) : null}
          <p className="team-project-workbench__editor-note">保存需求版本只更新项目依据；新的执行仍需组织，不会自动派发。</p>
          <div className="team-project-workbench__editor-actions">
            <button disabled={saving} onClick={onCancel} type="button">取消</button>
            <button disabled={saving || Boolean(briefValidation) || !dirty} type="submit">
              {saving ? <LoaderCircle aria-hidden="true" className="ui-spin" size={14} /> : null}
              保存需求 v{brief.revision + 1}
            </button>
          </div>
        </form>
      ) : (
        <>
          <div className="team-project-workbench__objective">
            <p className={brief.objective ? undefined : 'team-project-workbench__muted'}>
              {brief.objective || '还没有发布项目目标。'}
            </p>
          </div>
          <div className="team-project-workbench__criteria" aria-label="验收条件">
            <h3>验收条件</h3>
            {brief.acceptanceCriteria.length ? (
              <ul>{brief.acceptanceCriteria.map((criterion, index) => <li key={`${criterion}-${index}`}><CheckCircle2 aria-hidden="true" size={14} /><span>{criterion}</span></li>)}</ul>
            ) : <p className="team-project-workbench__muted">尚未添加验收条件。</p>}
          </div>
          <div className="team-project-workbench__brief-meta">
            <span>最近更新 {formatTimestamp(brief.updatedAtMs)}</span>
            <span>{brief.updatedByDisplayName || '尚未记录更新者'}</span>
            {!canEdit ? <span>只有项目所有者或维护者可以更新需求</span> : null}
          </div>
          {savedNotice ? <p className="team-project-workbench__saved" role="status">{savedNotice}</p> : null}
          <p className="team-project-workbench__execution-note">需求版本只保存项目依据；新的执行仍需组织，不会自动派发。</p>
          {briefHistory.length ? (
            <details className="team-project-workbench__history">
              <summary><History aria-hidden="true" size={14} />需求版本记录 <span>{briefHistory.length}</span></summary>
              <ol>
                {briefHistory.map((version, index) => (
                  <li key={`${version.revision}-${index}`}>
                    <div><strong>v{version.revision}</strong><small>{formatTimestamp(version.updatedAtMs)}</small></div>
                    <p>{version.objective || '未填写目标'}</p>
                    <span>{version.updatedByDisplayName || '尚未记录更新者'}</span>
                  </li>
                ))}
              </ol>
              {briefHistoryTruncated ? <small className="team-project-workbench__boundary">历史记录已按服务端上限截取。</small> : null}
            </details>
          ) : null}
        </>
      )}
    </section>
  );
}

function MembersSection({ members }: { members: TeamProjectMember[] }) {
  return (
    <section aria-labelledby="team-project-members-title" className="team-project-workbench__members">
      <header className="team-project-workbench__section-header">
        <div><h2 id="team-project-members-title">成员职责</h2><p>项目空间里的真实负责人</p></div>
        <span className="team-project-workbench__count">{members.length}</span>
      </header>
      {members.length ? (
        <ul>
          {members.map((member) => (
            <li key={member.id}>
              <span aria-hidden="true" className="team-project-workbench__avatar">{memberInitial(member.displayName)}</span>
              <span className="team-project-workbench__member-copy"><strong>{member.displayName}</strong><small>{projectMemberRoleLabel(member.role)}</small></span>
              <span className={`team-project-workbench__member-status${member.active ? '' : ' is-inactive'}`}>{member.active ? '已加入' : '已停用'}</span>
            </li>
          ))}
        </ul>
      ) : <div className="team-project-workbench__inline-empty"><Users aria-hidden="true" size={18} /><p>还没有项目成员。</p></div>}
    </section>
  );
}

function RoomsSection({
  currentRequirementsRevision,
  currentUserId,
  requirementsBusySessionId,
  rooms,
  truncated,
  onAdoptRequirements,
  onOpenSession,
  onOpenRoom,
}: {
  currentRequirementsRevision: number;
  currentUserId?: string;
  requirementsBusySessionId: string;
  rooms: TeamProjectRoom[];
  truncated: boolean;
  onAdoptRequirements: (item: TeamProjectRoomWorkItem) => Promise<void>;
  onOpenSession: (sessionId: string, title: string, ownerDisplayName?: string) => void;
  onOpenRoom: (room: TeamProjectRoom) => void;
}) {
  return (
    <section aria-labelledby="team-project-rooms-title" className="team-project-workbench__rooms">
      <header className="team-project-workbench__section-header">
        <div><h2 id="team-project-rooms-title">Room 与工作项</h2><p>从项目目标进入真实协作记录</p></div>
        <span className="team-project-workbench__count">{rooms.length} 个 Room</span>
      </header>
      <p className="team-project-workbench__section-note">Room 的“进行中”表示协作空间仍可使用；它不等同于 Runtime 正在执行。</p>
      {rooms.length ? (
        <div className="team-project-workbench__room-list">
          {rooms.map((room) => (
            <RoomRow
              currentRequirementsRevision={currentRequirementsRevision}
              currentUserId={currentUserId}
              key={room.id}
              onAdoptRequirements={onAdoptRequirements}
              onOpen={onOpenRoom}
              onOpenSession={onOpenSession}
              requirementsBusySessionId={requirementsBusySessionId}
              room={room}
            />
          ))}
        </div>
      ) : (
        <div className="team-project-workbench__empty-inline">
          <Wrench aria-hidden="true" size={18} />
          <div><strong>还没有项目 Room</strong><p>从 Agent 中创建或打开一个项目协作空间，工作记录会在这里出现。</p></div>
        </div>
      )}
      {truncated ? <p className="team-project-workbench__boundary">Room 或工作项较多，列表已按服务端上限截取；打开对应 Room 查看完整记录。</p> : null}
    </section>
  );
}

function RoomRow({
  currentRequirementsRevision,
  currentUserId,
  onAdoptRequirements,
  onOpen,
  onOpenSession,
  requirementsBusySessionId,
  room,
}: {
  currentRequirementsRevision: number;
  currentUserId?: string;
  onAdoptRequirements: (item: TeamProjectRoomWorkItem) => Promise<void>;
  onOpen: (room: TeamProjectRoom) => void;
  onOpenSession: (sessionId: string, title: string, ownerDisplayName?: string) => void;
  requirementsBusySessionId: string;
  room: TeamProjectRoom;
}) {
  return (
    <article className="team-project-workbench__room" data-status={room.status}>
      <header>
        <div><h3>{room.title}</h3><p><span className="team-project-workbench__status" data-status={room.status}>{roomStatusLabel(room.status)}</span><span>最近更新 {formatTimestamp(room.updatedAtMs)}</span></p></div>
        <button aria-label={`打开 Room：${room.title}`} className="team-project-workbench__open-action" onClick={() => onOpen(room)} type="button"><ExternalLink aria-hidden="true" size={14} />打开 Room</button>
      </header>
      <div className="team-project-workbench__room-meta"><span>{room.participantCount} 位成员</span><span>{room.workItems.length} 项 WorkItem</span></div>
      {room.workItems.length ? (
        <ul className="team-project-workbench__work-items">
          {room.workItems.map((workItem) => (
            <WorkItemRow
              currentRequirementsRevision={currentRequirementsRevision}
              currentUserId={currentUserId}
              item={workItem}
              key={workItem.id}
              onAdoptRequirements={onAdoptRequirements}
              onOpenSession={onOpenSession}
              requirementsBusySessionId={requirementsBusySessionId}
            />
          ))}
        </ul>
      ) : <p className="team-project-workbench__muted">这个 Room 还没有登记 WorkItem。</p>}
    </article>
  );
}

function WorkItemRow({
  currentRequirementsRevision,
  currentUserId,
  item,
  onAdoptRequirements,
  onOpenSession,
  requirementsBusySessionId,
}: {
  currentRequirementsRevision: number;
  currentUserId?: string;
  item: TeamProjectRoomWorkItem;
  onAdoptRequirements: (item: TeamProjectRoomWorkItem) => Promise<void>;
  onOpenSession: (sessionId: string, title: string, ownerDisplayName?: string) => void;
  requirementsBusySessionId: string;
}) {
  const owner = item.currentOwnerDisplayName || '未分配';
  const ownerRequirementsRevision = item.ownerRequirementsRevision;
  const requirementsStale = item.requirementsStale === true
    || (ownerRequirementsRevision !== undefined && ownerRequirementsRevision !== currentRequirementsRevision);
  const canAdoptRequirements = Boolean(
    item.ownerSessionId
    && ownerRequirementsRevision !== undefined
    && item.currentOwnerUserId
    && item.currentOwnerUserId === currentUserId
    && requirementsStale,
  );
  const requirementLabel = ownerRequirementsRevision === undefined
    ? (requirementsStale ? '需求基线已过期' : '')
    : requirementsStale
      ? `需求 v${ownerRequirementsRevision} · 当前 v${currentRequirementsRevision}，未证明满足新版`
      : `需求 v${ownerRequirementsRevision}`;
  return (
    <li>
      <div className="team-project-workbench__work-item-copy">
        <strong>{item.objective}</strong>
        <small><span className="team-project-workbench__status" data-status={item.state}>{workItemStateLabel(item.state)}</span> · 负责人 {owner}</small>
        {requirementLabel ? <span className={`team-project-workbench__requirements-label${requirementsStale ? ' is-stale' : ''}`}>{requirementLabel}</span> : null}
      </div>
      <div className="team-project-workbench__work-item-actions">
        {item.expectedOutput ? <p>{item.expectedOutput}</p> : null}
        {item.ownerSessionId ? (
          <div className="team-project-workbench__work-item-buttons">
            <button aria-label={`打开任务：${item.objective}`} className="team-project-workbench__open-action" onClick={() => onOpenSession(item.ownerSessionId!, item.objective, owner)} type="button"><ExternalLink aria-hidden="true" size={13} />打开任务</button>
            {canAdoptRequirements ? (
              <button
                className="team-project-workbench__requirements-action"
                disabled={Boolean(requirementsBusySessionId)}
                onClick={() => void onAdoptRequirements(item)}
                type="button"
              >
                {requirementsBusySessionId === item.ownerSessionId ? <LoaderCircle aria-hidden="true" className="ui-spin" size={13} /> : <CircleStop aria-hidden="true" size={13} />}
                {requirementsBusySessionId === item.ownerSessionId ? '正在采用…' : `停止旧执行并采用 v${currentRequirementsRevision}`}
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </li>
  );
}

function DeliveriesSection({
  currentRequirementsRevision,
  drafts,
  repository,
  truncated,
  memberById,
  onManage,
}: {
  currentRequirementsRevision: number;
  drafts: TeamProjectDraft[];
  repository: TeamProjectOverview['repository'];
  truncated: boolean;
  memberById: Map<string, TeamProjectMember>;
  onManage: () => void;
}) {
  return (
    <section aria-labelledby="team-project-deliveries-title" className="team-project-workbench__deliveries">
      <header className="team-project-workbench__section-header">
        <div><h2 id="team-project-deliveries-title">固定交付</h2><p>固定版本与稳定分支</p></div>
        <button aria-label="管理成员与交付" className="team-project-workbench__icon-action" onClick={onManage} type="button"><Settings2 aria-hidden="true" size={14} /></button>
      </header>
      <div className="team-project-workbench__repository">
        <div className="team-project-workbench__subheading"><GitBranch aria-hidden="true" size={15} /><strong>稳定分支</strong></div>
        {repository ? (
          <div className="team-project-workbench__repo-value"><code>{repository.branch}</code><span>HEAD {shortCommit(repository.headCommit)}</span><small>仓库修订 {repository.revision}</small></div>
        ) : <p className="team-project-workbench__muted">尚未绑定稳定分支。</p>}
      </div>
      {drafts.length ? (
        <ol className="team-project-workbench__draft-list">
          {drafts.map((draft) => <DraftRow currentRequirementsRevision={currentRequirementsRevision} draft={draft} key={draft.draftId} memberById={memberById} />)}
        </ol>
      ) : <div className="team-project-workbench__inline-empty"><FileCheck2 aria-hidden="true" size={18} /><p>还没有共享固定版本。</p></div>}
      {truncated ? <p className="team-project-workbench__boundary">固定版本列表已按服务端上限截取。</p> : null}
    </section>
  );
}

function DraftRow({ currentRequirementsRevision, draft, memberById }: { currentRequirementsRevision: number; draft: TeamProjectDraft; memberById: Map<string, TeamProjectMember> }) {
  const creator = draft.creatorDisplayName || memberById.get(draft.creatorUserId)?.displayName || '项目成员';
  const requirementsStale = draft.requirementsStale === true
    || (draft.requirementsRevision !== undefined && draft.requirementsRevision !== currentRequirementsRevision);
  const requirementLabel = draft.requirementsRevision === undefined
    ? (requirementsStale ? '需求基线已过期' : '')
    : requirementsStale
      ? `基于需求 v${draft.requirementsRevision} · 当前 v${draft.currentRequirementsRevision ?? currentRequirementsRevision}，未证明满足新版`
      : `基于需求 v${draft.requirementsRevision}`;
  return (
    <li data-status={draft.status}>
      <div className="team-project-workbench__draft-copy"><strong>{draft.title}</strong><span>{draftStatusLabel(draft.status)} · {creator}</span></div>
      {requirementLabel ? <small className={`team-project-workbench__draft-requirements${requirementsStale ? ' is-stale' : ''}`}>{requirementLabel}</small> : null}
      <small><code>{shortCommit(draft.baseCommit)}</code><span>→</span><code>{shortCommit(draft.draftCommit)}</code><span>· {formatTimestamp(draft.createdAtMs)}</span></small>
      {requirementsStale ? <small className="team-project-workbench__draft-boundary">该交付尚未证明满足当前需求。</small> : null}
    </li>
  );
}

function RuntimeSection({ configured }: { configured: boolean }) {
  return (
    <section aria-labelledby="team-project-runtime-title" className="team-project-workbench__runtime">
      <header className="team-project-workbench__section-header">
        <div><h2 id="team-project-runtime-title">执行状态</h2><p>服务可用性来自当前项目元数据</p></div>
      </header>
      <div className="team-project-workbench__runtime-value" data-configured={configured}>
        {configured ? <CheckCircle2 aria-hidden="true" size={17} /> : <CircleAlert aria-hidden="true" size={17} />}
        <div><strong>{configured ? '执行服务已配置' : '执行服务尚未配置'}</strong><small>{configured ? '可以按项目授权发起新的执行。' : '执行请求会被服务明确拒绝；历史与项目资料仍可查看。'}</small></div>
      </div>
    </section>
  );
}

function ProjectWorkbenchSkeleton() {
  return (
    <div aria-label="正在读取项目概览" className="team-project-workbench__skeleton" role="status">
      <div className="team-project-workbench__skeleton-main"><i /><i /><i /><i /><i /></div>
      <div className="team-project-workbench__skeleton-side"><i /><i /><i /><i /></div>
    </div>
  );
}

function validateBriefDraft(objective: string, criteria: readonly string[]): string {
  const normalizedObjective = objective.trim();
  if (!normalizedObjective) return '请填写当前目标。';
  if (normalizedObjective.length > 4_000) return '当前目标最多 4000 个字符。';
  if (criteria.length > 20) return '验收条件最多 20 项。';
  const oversizedIndex = criteria.findIndex((item) => item.length > 500);
  return oversizedIndex >= 0 ? `第 ${oversizedIndex + 1} 项验收条件最多 500 个字符。` : '';
}

function sameStringArray(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((item, index) => item === right[index]);
}

function isBriefConflict(reason: unknown): boolean {
  if (reason instanceof TeamApiError) return reason.status === 409;
  return typeof reason === 'object' && reason !== null && 'status' in reason
    && (reason as { status?: unknown }).status === 409;
}

function projectMemberRoleLabel(role: TeamProjectMember['role']): string {
  return {
    owner: '项目所有者',
    maintainer: '维护者',
    contributor: '贡献者',
    viewer: '查看者',
  }[role];
}

function spaceRoleLabel(role: TeamSpaceRole): string {
  return projectMemberRoleLabel(role);
}

function roomStatusLabel(status: string): string {
  if (status === 'active') return '进行中 · 可协作';
  if (status === 'archived') return '已收起';
  if (status === 'failed') return '记录失败';
  return status || '状态未知';
}

function workItemStateLabel(state: string): string {
  if (state === 'queued' || state === 'todo') return '待处理';
  if (state === 'active' || state === 'running' || state === 'in_progress') return '进行中';
  if (state === 'review') return '待验收';
  if (state === 'done' || state === 'completed') return '已完成';
  if (state === 'blocked') return '受阻';
  if (state === 'failed') return '失败';
  if (state === 'cancelled') return '已取消';
  return state || '状态未知';
}

function draftStatusLabel(status: string | undefined): string {
  if (status === 'integrated') return '已集成';
  if (status === 'conflict') return '有冲突';
  if (status === 'verification_failed') return '验证失败';
  return status ? '待处理' : '已发布';
}

function memberInitial(displayName: string): string {
  return displayName.trim().slice(0, 1).toUpperCase() || '?';
}

function shortCommit(commit: string): string {
  const normalized = commit.trim();
  return normalized ? normalized.slice(0, 12) : '尚无提交';
}

function formatTimestamp(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '时间未知';
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      month: 'numeric',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    }).format(new Date(value));
  } catch {
    return '时间未知';
  }
}
