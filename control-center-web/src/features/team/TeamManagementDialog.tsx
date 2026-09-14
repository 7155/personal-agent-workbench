import { CheckCircle2, CircleAlert, Download, Eye, GitCommitHorizontal, LoaderCircle, RefreshCw, Upload } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Field,
  Input,
} from '@/components/primitives';
import { teamApiErrorMessage } from './team-api';
import { useTeam } from './team-context';
import type {
  TeamDirectoryUser,
  TeamProjectDraft,
  TeamProjectAdoption,
  TeamProjectDiff,
  TeamProjectIntegration,
  TeamProjectMember,
  TeamProjectMemberInputRole,
  TeamProjectSession,
  TeamUser,
} from './types';

export function TeamManagementDialog({ open, onOpenChange }: { open: boolean; onOpenChange(open: boolean): void }) {
  const team = useTeam();
  const isAdmin = team.user?.role === 'admin';
  const { listDirectory, listMembers, listProjectDrafts, listProjectMembers, listProjectSessions } = team;
  const manageableProjects = useMemo(
    () => team.spaces.filter((space) => space.kind === 'project' && (space.role === 'owner' || space.role === 'maintainer')),
    [team.spaces],
  );
  const projectSpaces = useMemo(
    () => team.spaces.filter((space) => space.kind === 'project'),
    [team.spaces],
  );
  const [members, setMembers] = useState<TeamUser[]>([]);
  const [directory, setDirectory] = useState<TeamDirectoryUser[]>([]);
  const [projectMembers, setProjectMembers] = useState<TeamProjectMember[]>([]);
  const [ownSessions, setOwnSessions] = useState<TeamProjectSession[]>([]);
  const [drafts, setDrafts] = useState<TeamProjectDraft[]>([]);
  const [integrations, setIntegrations] = useState<Record<string, TeamProjectIntegration>>({});
  const [adoptions, setAdoptions] = useState<Record<string, TeamProjectAdoption>>({});
  const [diffs, setDiffs] = useState<Record<string, TeamProjectDiff>>({});
  const [selectedProjectId, setSelectedProjectId] = useState('');
  const [loading, setLoading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [newUsername, setNewUsername] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [newDisplayName, setNewDisplayName] = useState('');
  const [newRole, setNewRole] = useState<'member' | 'admin'>('member');
  const [memberId, setMemberId] = useState('');
  const [memberRole, setMemberRole] = useState<TeamProjectMemberInputRole>('contributor');
  const [integratingDraftId, setIntegratingDraftId] = useState('');
  const [adoptingDraftId, setAdoptingDraftId] = useState('');
  const [diffLoadingDraftId, setDiffLoadingDraftId] = useState('');
  const [targetSessionIds, setTargetSessionIds] = useState<Record<string, string>>({});
  const loadRequestRef = useRef(0);
  const viewRef = useRef({ open, selectedProjectId });
  viewRef.current = { open, selectedProjectId };

  useEffect(() => {
    if (!open) return;
    const preferred = team.activeSpace?.kind === 'project' && projectSpaces.some((space) => space.id === team.activeSpace?.id)
      ? team.activeSpace.id
      : projectSpaces[0]?.id ?? '';
    setSelectedProjectId((current) => projectSpaces.some((space) => space.id === current) ? current : preferred);
  }, [open, projectSpaces, team.activeSpace]);

  useEffect(() => {
    setDrafts([]);
    setProjectMembers([]);
    setDirectory([]);
    setOwnSessions([]);
    setIntegrations({});
    setAdoptions({});
    setDiffs({});
    setTargetSessionIds({});
  }, [selectedProjectId]);

  const load = useCallback(async () => {
    if (!open) return;
    const projectId = selectedProjectId;
    if (projectSpaces.length > 0 && !projectId) return;
    if (!viewRef.current.open || viewRef.current.selectedProjectId !== projectId) return;
    const requestId = ++loadRequestRef.current;
    setLoading(true);
    setActionError(null);
    const [membersResult, directoryResult, projectMembersResult, draftsResult, sessionsResult] = await Promise.allSettled([
      isAdmin ? listMembers() : Promise.resolve([]),
      projectSpaces.length ? listDirectory() : Promise.resolve([]),
      manageableProjects.some((space) => space.id === projectId)
        ? listProjectMembers(projectId)
        : Promise.resolve([]),
      projectId ? listProjectDrafts(projectId) : Promise.resolve([]),
      projectId ? listProjectSessions(projectId) : Promise.resolve([]),
    ]);
    if (
      requestId !== loadRequestRef.current
      || !viewRef.current.open
      || viewRef.current.selectedProjectId !== projectId
    ) return;
    const errors: string[] = [];
    if (membersResult.status === 'fulfilled') setMembers(membersResult.value);
    else errors.push(teamApiErrorMessage(membersResult.reason, '读取团队账号失败，请重试。'));
    if (directoryResult.status === 'fulfilled') setDirectory(directoryResult.value);
    else errors.push(teamApiErrorMessage(directoryResult.reason, '读取可邀请成员失败，请重试。'));
    if (projectMembersResult.status === 'fulfilled') setProjectMembers(projectMembersResult.value);
    else errors.push(teamApiErrorMessage(projectMembersResult.reason, '读取项目成员失败，请重试。'));
    if (draftsResult.status === 'fulfilled') setDrafts(draftsResult.value);
    else errors.push(teamApiErrorMessage(draftsResult.reason, '读取共享固定版本失败，请重试。'));
    if (sessionsResult.status === 'fulfilled') {
      setOwnSessions(sessionsResult.value.filter((session) => (
        session.ownerUserId === team.user?.id
        && session.canControl !== false
        && session.status !== 'archived'
      )));
    } else errors.push(teamApiErrorMessage(sessionsResult.reason, '读取目标 Session 失败，请重试。'));
    setActionError(errors.length ? errors.join(' ') : null);
    setLoading(false);
  }, [isAdmin, listDirectory, listMembers, listProjectDrafts, listProjectMembers, listProjectSessions, manageableProjects, open, projectSpaces.length, selectedProjectId, team.user?.id]);

  useEffect(() => {
    if (!drafts.length) return;
    setTargetSessionIds((current) => {
      let changed = false;
      const next = { ...current };
      for (const draft of drafts) {
        if (next[draft.draftId] && ownSessions.some((session) => session.id === next[draft.draftId])) continue;
        const replacement = ownSessions[0]?.id ?? '';
        if (next[draft.draftId] !== replacement) {
          next[draft.draftId] = replacement;
          changed = true;
        }
      }
      for (const draftId of Object.keys(next)) {
        if (!drafts.some((draft) => draft.draftId === draftId)) {
          delete next[draftId];
          changed = true;
        }
      }
      return changed ? next : current;
    });
  }, [drafts, ownSessions]);

  useEffect(() => {
    void load();
  }, [load]);

  async function createMember(): Promise<void> {
    if (!newUsername.trim() || newPassword.length < 8) {
      setActionError('请输入账号和至少 8 位密码。');
      return;
    }
    try {
      await team.createMember({
        username: newUsername.trim(),
        password: newPassword,
        ...(newDisplayName.trim() ? { displayName: newDisplayName.trim() } : {}),
        role: newRole,
      });
      setNewUsername('');
      setNewPassword('');
      setNewDisplayName('');
      setActionError(null);
      await load();
    } catch (error) {
      setActionError(teamApiErrorMessage(error, '创建成员失败，请重试。'));
    }
  }

  async function toggleMember(member: TeamUser): Promise<void> {
    try {
      const updated = await team.setMemberStatus(member.id, !member.active);
      setMembers((current) => current.map((item) => item.id === updated.id ? updated : item));
      setActionError(null);
    } catch (error) {
      setActionError(teamApiErrorMessage(error, '更新成员状态失败，请重试。'));
    }
  }

  async function addMember(): Promise<void> {
    if (!selectedProjectId || !memberId.trim()) {
      setActionError('请选择项目和成员账号。');
      return;
    }
    try {
      await team.addProjectMember(selectedProjectId, { userId: memberId.trim(), role: memberRole });
      setMemberId('');
      setActionError(null);
      await load();
    } catch (error) {
      setActionError(teamApiErrorMessage(error, '添加项目成员失败，请重试。'));
    }
  }

  async function removeMember(member: TeamProjectMember): Promise<void> {
    if (member.role === 'owner') return;
    try {
      await team.removeProjectMember(selectedProjectId, member.id);
      setActionError(null);
      await load();
    } catch (error) {
      setActionError(teamApiErrorMessage(error, '移除项目成员失败，请重试。'));
    }
  }

  async function integrateDraft(draft: TeamProjectDraft): Promise<void> {
    if (!selectedProjectId || integratingDraftId) return;
    setIntegratingDraftId(draft.draftId);
    setActionError(null);
    try {
      const integration = await team.integrateProjectDraft(selectedProjectId, draft.draftId);
      setIntegrations((current) => ({ ...current, [draft.draftId]: integration }));
      await load();
    } catch (error) {
      // Keep the failed request visible. A 503 means the worker/verifier is
      // genuinely unavailable; the UI must not present an optimistic merge.
      setActionError(teamApiErrorMessage(error, '集成固定版本失败，请重试。'));
    } finally {
      setIntegratingDraftId('');
    }
  }

  async function viewDraftDiff(draft: TeamProjectDraft): Promise<void> {
    if (!selectedProjectId || diffLoadingDraftId) return;
    setDiffLoadingDraftId(draft.draftId);
    setActionError(null);
    try {
      const diff = await team.getProjectDraftDiff(selectedProjectId, draft.draftId);
      setDiffs((current) => ({ ...current, [draft.draftId]: diff }));
    } catch (error) {
      setActionError(teamApiErrorMessage(error, '读取固定版本差异失败，请重试。'));
    } finally {
      setDiffLoadingDraftId('');
    }
  }

  async function adoptDraft(draft: TeamProjectDraft): Promise<void> {
    if (!selectedProjectId || adoptingDraftId) return;
    const targetSessionId = targetSessionIds[draft.draftId] ?? '';
    if (!targetSessionId) {
      setActionError('请选择要采纳到的个人 Session。');
      return;
    }
    setAdoptingDraftId(draft.draftId);
    setActionError(null);
    try {
      const adoption = await team.adoptProjectDraft(selectedProjectId, draft.draftId, targetSessionId);
      setAdoptions((current) => ({ ...current, [draft.draftId]: adoption }));
      await load();
    } catch (error) {
      // Conflicts are returned as a real server result and leave the target
      // workspace untouched. Transport failures, including 503, remain errors.
      setActionError(teamApiErrorMessage(error, '采纳固定版本失败，请重试。'));
    } finally {
      setAdoptingDraftId('');
    }
  }

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="team-management-dialog">
        <DialogHeader>
          <DialogTitle>管理团队与项目成员</DialogTitle>
          <DialogDescription>成员权限由服务端保存；每次变更后会重新读取当前账号可见空间。</DialogDescription>
        </DialogHeader>
        {actionError ? <p className="team-management-dialog__error" role="alert">{actionError}</p> : null}
        {isAdmin ? (
          <section className="team-management-dialog__section" aria-labelledby="team-admin-members-title">
            <div className="team-management-dialog__section-heading">
              <div><h2 id="team-admin-members-title">团队账号</h2><p>停用账号会撤销该账号的登录会话。</p></div>
              <Button disabled={loading} leadingIcon={<span aria-hidden="true">↻</span>} onClick={() => void load()} size="small" variant="quiet">刷新</Button>
            </div>
            <div className="team-management-dialog__member-list" aria-label="团队账号列表">
              {members.map((member) => (
                <div className="team-management-dialog__member" key={member.id}>
                  <div><strong>{member.displayName}</strong><span>{member.username} · {member.role === 'admin' ? '管理员' : '成员'}</span></div>
                  <Button onClick={() => void toggleMember(member)} size="small" variant={member.active ? 'quiet' : 'secondary'}>{member.active ? '停用' : '启用'}</Button>
                </div>
              ))}
              {!loading && members.length === 0 ? <p className="team-management-dialog__empty">还没有可显示的成员。</p> : null}
            </div>
            <div className="team-management-dialog__form-grid">
              <Field htmlFor="team-new-member-username" label="新账号">
                <Input id="team-new-member-username" onChange={(event) => setNewUsername(event.target.value)} value={newUsername} />
              </Field>
              <Field htmlFor="team-new-member-password" label="初始密码">
                <Input id="team-new-member-password" minLength={8} onChange={(event) => setNewPassword(event.target.value)} type="password" value={newPassword} />
              </Field>
              <Field htmlFor="team-new-member-display-name" label="显示名（可选）">
                <Input id="team-new-member-display-name" onChange={(event) => setNewDisplayName(event.target.value)} value={newDisplayName} />
              </Field>
              <Field htmlFor="team-new-member-role" label="账号角色">
                <select className="paw-select" id="team-new-member-role" onChange={(event) => setNewRole(event.target.value as 'member' | 'admin')} value={newRole}>
                  <option value="member">成员</option>
                  <option value="admin">管理员</option>
                </select>
              </Field>
            </div>
            <Button loading={team.busy === 'create-member'} onClick={() => void createMember()} size="small" variant="primary">创建账号</Button>
          </section>
        ) : null}
        {manageableProjects.length > 0 ? (
          <section className="team-management-dialog__section" aria-labelledby="team-project-members-title">
            <div className="team-management-dialog__section-heading">
              <div><h2 id="team-project-members-title">项目成员</h2><p>项目成员只能访问自己被授予的项目空间。</p></div>
              <select className="paw-select" aria-label="选择项目" onChange={(event) => setSelectedProjectId(event.target.value)} value={selectedProjectId}>
                {manageableProjects.map((space) => <option key={space.id} value={space.id}>{space.name}</option>)}
              </select>
            </div>
            <div className="team-management-dialog__member-list" aria-label="项目成员列表">
              {projectMembers.map((member) => (
                <div className="team-management-dialog__member" key={member.id}>
                  <div><strong>{member.displayName}</strong><span>{member.username} · {member.role === 'owner' ? '所有者' : member.role === 'maintainer' ? '维护者' : member.role === 'contributor' ? '贡献者' : '查看者'}</span></div>
                  <Button disabled={member.role === 'owner'} onClick={() => void removeMember(member)} size="small" variant="quiet">移除</Button>
                </div>
              ))}
              {!loading && projectMembers.length === 0 ? <p className="team-management-dialog__empty">这个项目还没有成员。</p> : null}
            </div>
            <div className="team-management-dialog__form-grid">
              <Field htmlFor="team-project-member-id" label="成员账号">
                <select className="paw-select" id="team-project-member-id" onChange={(event) => setMemberId(event.target.value)} value={memberId}>
                  <option value="">选择账号</option>
                  {directory.map((member) => <option key={member.id} value={member.id}>{member.displayName} · {member.username}</option>)}
                </select>
              </Field>
              <Field htmlFor="team-project-member-role" label="项目角色">
                <select className="paw-select" id="team-project-member-role" onChange={(event) => setMemberRole(event.target.value as TeamProjectMemberInputRole)} value={memberRole}>
                  <option value="maintainer">维护者</option>
                  <option value="contributor">贡献者</option>
                  <option value="viewer">查看者</option>
                </select>
              </Field>
            </div>
            <Button loading={team.busy === 'add-project-member'} onClick={() => void addMember()} size="small" variant="primary">添加项目成员</Button>
          </section>
        ) : null}
        {projectSpaces.length > 0 ? (
          <section className="team-management-dialog__section" aria-labelledby="team-project-drafts-title">
            <div className="team-management-dialog__section-heading">
              <div><h2 id="team-project-drafts-title">共享固定版本</h2><p>项目成员可以查看差异；有权限的成员可以集成到项目分支或采纳到自己的 Session。</p></div>
              <select className="paw-select" aria-label="选择固定版本项目" onChange={(event) => setSelectedProjectId(event.target.value)} value={selectedProjectId}>
                {projectSpaces.map((space) => <option key={space.id} value={space.id}>{space.name}</option>)}
              </select>
              <Button disabled={loading} leadingIcon={<RefreshCw size={14} />} onClick={() => void load()} size="small" variant="quiet">刷新</Button>
            </div>
            <div className="team-management-dialog__draft-list" aria-label="共享固定版本列表" aria-busy={loading || undefined}>
              {loading && !drafts.length ? <p className="team-management-dialog__empty"><LoaderCircle className="ui-spin" size={14} />正在读取固定版本…</p> : null}
              {drafts.map((draft) => {
                const integration = integrations[draft.draftId];
                const adoption = adoptions[draft.draftId];
                const diff = diffs[draft.draftId];
                const terminalStatus = integration?.status ?? draft.status;
                const project = projectSpaces.find((space) => space.id === selectedProjectId);
                const canWriteProject = project?.role === 'owner' || project?.role === 'maintainer' || project?.role === 'contributor';
                const requirementsSummary = draftRequirementsSummary(draft);
                const requirementsStale = draftRequirementsStale(draft);
                return (
                  <article className="team-management-dialog__draft" key={draft.draftId}>
                    <div className="team-management-dialog__draft-copy">
                      <strong>{draft.title}</strong>
                      <span>{draft.creatorDisplayName || '项目成员'} · Session {draft.sessionId}</span>
                      {requirementsSummary ? <small className={requirementsStale ? 'team-management-dialog__draft-requirements is-stale' : 'team-management-dialog__draft-requirements'}>{requirementsSummary}</small> : null}
                      <small><GitCommitHorizontal size={12} />{shortCommit(draft.baseCommit)} → {shortCommit(draft.draftCommit)} · 清单 {shortCommit(draft.manifestHash)}</small>
                    </div>
                    <div className="team-management-dialog__draft-actions">
                      {terminalStatus ? <span className={`team-management-dialog__draft-status team-management-dialog__draft-status--${statusClass(terminalStatus)}`}>{integrationStatusLabel(terminalStatus)}</span> : null}
                      <div className="team-management-dialog__draft-buttons">
                        <Button disabled={Boolean(diffLoadingDraftId)} loading={diffLoadingDraftId === draft.draftId} leadingIcon={<Eye size={14} />} onClick={() => void viewDraftDiff(draft)} size="small" variant="quiet">查看差异</Button>
                      {manageableProjects.some((space) => space.id === selectedProjectId)
                        && draft.status !== 'integrated' && draft.status !== 'conflict' && draft.status !== 'verification_failed' && !integration ? (
                        <Button disabled={Boolean(integratingDraftId)} loading={integratingDraftId === draft.draftId} leadingIcon={<Upload size={14} />} onClick={() => void integrateDraft(draft)} size="small" variant="primary">集成</Button>
                      ) : null}
                      </div>
                    </div>
                    {integration ? <p className={`team-management-dialog__integration team-management-dialog__integration--${statusClass(integration.status)}`} role="status">{integration.status === 'integrated' ? <CheckCircle2 size={13} /> : <CircleAlert size={13} />}{integrationMessage(integration)}{integration.headCommit ? ` · HEAD ${shortCommit(integration.headCommit)}` : ''}</p> : null}
                    {canWriteProject ? (
                      <div className="team-management-dialog__adopt-row">
                        {ownSessions.length ? (
                          <>
                            <label htmlFor={`team-draft-target-${draft.draftId}`}>采纳到我的任务</label>
                            <select
                              aria-label={`采纳 ${draft.title} 到我的任务`}
                              className="paw-select"
                              id={`team-draft-target-${draft.draftId}`}
                              onChange={(event) => setTargetSessionIds((current) => ({ ...current, [draft.draftId]: event.target.value }))}
                              value={targetSessionIds[draft.draftId] ?? ownSessions[0]?.id ?? ''}
                            >
                              {ownSessions.map((session) => <option key={session.id} value={session.id}>{session.title || session.id}</option>)}
                            </select>
                            <Button
                              disabled={Boolean(adoptingDraftId) || adoption?.status === 'adopted'}
                              loading={adoptingDraftId === draft.draftId}
                              leadingIcon={<Download size={14} />}
                              onClick={() => void adoptDraft(draft)}
                              size="small"
                              variant="secondary"
                            >{adoption?.status === 'adopted' ? '已采纳' : '采纳到我的任务'}</Button>
                          </>
                        ) : <span>当前项目没有可采纳的个人 Session。</span>}
                      </div>
                    ) : <small className="team-management-dialog__draft-readonly">当前角色只能查看固定版本。</small>}
                    {adoption ? <p className={`team-management-dialog__integration team-management-dialog__integration--${adoption.status === 'adopted' ? 'integrated' : 'conflict'}`} role="status">{adoption.status === 'adopted' ? <CheckCircle2 size={13} /> : <CircleAlert size={13} />}{adoptionMessage(adoption)}{adoption.targetSessionId ? ` · 目标 ${adoption.targetSessionId}` : ''}{adoption.targetTreeCommit ? ` · 提交 ${shortCommit(adoption.targetTreeCommit)}` : ''}</p> : null}
                    {diff ? <details className="team-management-dialog__diff" open>
                      <summary>查看差异{diff.truncated ? '（内容已截断）' : ''}</summary>
                      <pre>{diff.diff || '没有文本变化。'}</pre>
                    </details> : null}
                  </article>
                );
              })}
              {!loading && !drafts.length ? <p className="team-management-dialog__empty">这个项目还没有共享固定版本。</p> : null}
            </div>
          </section>
        ) : null}
        <DialogFooter><Button onClick={() => onOpenChange(false)} variant="quiet">完成</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function shortCommit(value: string): string {
  const normalized = value.trim();
  return normalized ? normalized.slice(0, 12) : '未知';
}

function statusClass(status: string): string {
  return status === 'integrated' || status === 'conflict' || status === 'verification_failed' ? status : 'pending';
}

function integrationStatusLabel(status: string): string {
  if (status === 'integrated') return '已集成';
  if (status === 'conflict') return '有冲突';
  if (status === 'verification_failed') return '验证未通过';
  return '待集成';
}

function integrationMessage(integration: TeamProjectIntegration): string {
  if (integration.status === 'integrated') return '固定版本已集成到项目分支。';
  if (integration.status === 'conflict') return integration.message || '目标分支已有变化，未修改项目分支。';
  return integration.message || '验证未通过，项目分支保持不变。';
}

function adoptionMessage(adoption: TeamProjectAdoption): string {
  if (adoption.status === 'adopted') return '固定版本已采纳到目标 Session。';
  return adoption.message || '目标 Session 存在冲突，现有文件保持不变。';
}

function draftRequirementsSummary(draft: TeamProjectDraft): string {
  const stale = draftRequirementsStale(draft);
  if (draft.requirementsRevision === undefined) return stale ? '需求基线已过期' : '';
  if (!stale) return `基于需求 v${draft.requirementsRevision}`;
  return draft.currentRequirementsRevision === undefined
    ? `基于需求 v${draft.requirementsRevision} · 已过期，未证明满足新版`
    : `基于需求 v${draft.requirementsRevision} · 当前 v${draft.currentRequirementsRevision}，未证明满足新版`;
}

function draftRequirementsStale(draft: TeamProjectDraft): boolean {
  return draft.requirementsStale === true
    || (draft.requirementsRevision !== undefined
      && draft.currentRequirementsRevision !== undefined
      && draft.requirementsRevision !== draft.currentRequirementsRevision);
}
