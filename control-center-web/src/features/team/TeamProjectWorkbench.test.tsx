import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PawOsDesktopProvider, type PawOsWindowRequest } from '@/features/paw-os/surface-context';
import { TeamApi, TeamApiError } from './team-api';
import { TeamProvider } from './team-context';
import { TeamProjectWorkbench } from './TeamProjectWorkbench';
import type { TeamProjectOverview, TeamProjectPreview, TeamSession } from './types';

const project = {
  id: 'project-1',
  kind: 'project' as const,
  name: '官网',
  role: 'maintainer' as const,
  revision: 3,
};
const session: TeamSession = {
  user: { id: 'user-owner', username: 'zhou', displayName: '小周', role: 'member', active: true },
  csrfToken: 'csrf-memory',
  spaces: [project],
};
const brief = {
  revision: 1,
  objective: '交付可恢复的注册流程',
  acceptanceCriteria: ['正常注册成功', '重复邮箱有明确提示', '错误状态能恢复'],
  updatedAtMs: 1_700_000_000_000,
  updatedByUserId: 'user-owner',
  updatedByDisplayName: '小周',
};
const overview: TeamProjectOverview = {
  project: { id: project.id, name: project.name, role: project.role },
  brief,
  briefHistory: [brief],
  members: [
    { id: 'user-owner', username: 'zhou', displayName: '小周', role: 'owner', active: true },
    { id: 'user-wang', username: 'wang', displayName: '小王', role: 'contributor', active: true },
  ],
  rooms: [{
    id: 'room-1',
    title: '注册协作',
    status: 'active',
    updatedAtMs: 1_700_000_000_000,
    participantCount: 2,
    workItems: [{
      id: 'work-1',
      roomId: 'room-1',
      objective: '实现页面校验',
      state: 'active',
      currentOwnerParticipantId: 'participant-mars',
      currentOwnerUserId: 'user-wang',
      currentOwnerDisplayName: '小王',
      ownerSessionId: 'session-wang',
      ownerRequirementsRevision: 0,
      requirementsStale: true,
      expectedOutput: '页面与错误提示可运行',
      updatedAtMs: 1_700_000_000_000,
    }],
  }],
  drafts: [{
    draftId: 'draft-1',
    sessionId: 'session-1',
    title: '页面固定版本',
    creatorUserId: 'user-wang',
    creatorDisplayName: '小王',
    baseCommit: 'a'.repeat(40),
    draftCommit: 'b'.repeat(40),
    manifestHash: 'c'.repeat(64),
    createdAtMs: 1_700_000_000_000,
    status: 'pending',
    requirementsRevision: 0,
    currentRequirementsRevision: 1,
    requirementsStale: true,
  }],
  repository: { branch: 'team/main', headCommit: 'd'.repeat(40), revision: 4 },
  runtime: { configured: false },
  truncated: { rooms: false, workItems: false, drafts: false, briefHistory: false },
};
const preview: TeamProjectPreview = {
  configured: true,
  active: null,
  latest: null,
  currentRequirementsRevision: brief.revision,
  repositoryHead: overview.repository?.headCommit ?? null,
  canManage: true,
  openPath: null,
};

afterEach(() => cleanup());

describe('TeamProjectWorkbench', () => {
  it('shows project intent, member ownership, Room work, fixed delivery and truthful runtime state', async () => {
    const user = userEvent.setup();
    const openWindow = vi.fn<(request: PawOsWindowRequest) => void>();
    const onNavigate = vi.fn();
    const api = fakeApi({ getProjectOverview: vi.fn().mockResolvedValue(overview) });
    renderWorkbench(api, openWindow, onNavigate);

    expect(await screen.findByRole('heading', { name: '当前目标与验收' })).toBeVisible();
    const objective = document.querySelector('.team-project-workbench__objective');
    expect(objective).not.toBeNull();
    expect(within(objective as HTMLElement).getByText('交付可恢复的注册流程')).toBeVisible();
    expect(screen.getByText('正常注册成功')).toBeVisible();
    expect(screen.getByText('小王')).toBeVisible();
    expect(screen.getByText('贡献者')).toBeVisible();
    expect(screen.getByText('注册协作')).toBeVisible();
    const room = document.querySelector('.team-project-workbench__room');
    expect(room).not.toBeNull();
    expect(within(room as HTMLElement).getByText(/负责人/)).toBeVisible();
    expect(screen.getByText('页面固定版本')).toBeVisible();
    expect(screen.getByText('team/main')).toBeVisible();
    expect(screen.getByText('执行服务尚未配置')).toBeVisible();
    expect(screen.getByText('进行中 · 可协作')).toBeVisible();
    expect(screen.getAllByText(/未证明满足新版/).length).toBeGreaterThan(0);
    expect(screen.queryByText('Runtime 正在执行')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '打开 Room：注册协作' }));
    expect(openWindow).toHaveBeenCalledWith(expect.objectContaining({
      appId: 'agent',
      target: expect.objectContaining({ kind: 'room', id: 'room-1', title: '注册协作' }),
    }));
    await user.click(screen.getByRole('button', { name: '打开任务：实现页面校验' }));
    expect(openWindow).toHaveBeenCalledWith(expect.objectContaining({
      appId: 'agent',
      target: expect.objectContaining({ kind: 'session', id: 'session-wang', title: '实现页面校验' }),
    }));
    await user.click(screen.getByRole('button', { name: '任务' }));
    expect(onNavigate).toHaveBeenCalledWith('planning');
  });

  it('opens the existing Knowledge window for project materials', async () => {
    const user = userEvent.setup();
    const openApp = vi.fn();
    renderWorkbench(fakeApi(), vi.fn(), vi.fn(), openApp);

    await screen.findByRole('heading', { name: '当前目标与验收' });
    await user.click(screen.getByRole('button', { name: '项目资料' }));

    expect(openApp).toHaveBeenCalledWith('knowledge', '/knowledge');
  });

  it('lets an owner or maintainer save a new brief version with the server revision', async () => {
    const user = userEvent.setup();
    const updatedBrief = {
      ...brief,
      revision: 2,
      objective: '交付可恢复且必须填写公司名称的注册流程',
      acceptanceCriteria: [...brief.acceptanceCriteria, '公司名称必须填写'],
    };
    const updatedOverview = { ...overview, brief: updatedBrief, briefHistory: [updatedBrief, brief] };
    const api = fakeApi({
      getProjectOverview: vi.fn().mockResolvedValueOnce(overview).mockResolvedValueOnce(updatedOverview),
      updateProjectBrief: vi.fn().mockResolvedValue(updatedBrief),
    });
    renderWorkbench(api);

    await screen.findByRole('heading', { name: '当前目标与验收' });
    await user.click(await screen.findByRole('button', { name: '编辑当前目标' }));
    await user.clear(screen.getByRole('textbox', { name: '当前目标' }));
    await user.type(screen.getByRole('textbox', { name: '当前目标' }), updatedBrief.objective);
    await user.click(screen.getByRole('button', { name: '保存需求 v2' }));

    await waitFor(() => expect(api.updateProjectBrief).toHaveBeenCalledWith('project-1', {
      baseRevision: 1,
      objective: updatedBrief.objective,
      acceptanceCriteria: brief.acceptanceCriteria,
    }, 'csrf-memory'));
    expect(await screen.findByText('需求 v2 已保存。新的执行仍需组织。')).toBeVisible();
    const currentObjective = document.querySelector('.team-project-workbench__objective');
    expect(currentObjective).not.toBeNull();
    expect(within(currentObjective as HTMLElement).getByText(updatedBrief.objective)).toBeVisible();
  });

  it('lets the current owner stop old execution and adopt the latest brief for their WorkItem', async () => {
    const user = userEvent.setup();
    const ownOverview: TeamProjectOverview = {
      ...overview,
      rooms: [{
        ...overview.rooms[0],
        workItems: [{
          ...overview.rooms[0].workItems[0],
          currentOwnerUserId: 'user-owner',
          currentOwnerDisplayName: '小周',
          ownerSessionId: 'session-owner',
        }],
      }],
    };
    const api = fakeApi({
      getProjectOverview: vi.fn().mockResolvedValue(ownOverview),
      adoptSessionRequirements: vi.fn().mockResolvedValue({
        sessionId: 'session-owner',
        requirementsRevision: 1,
        previousRequirementsRevision: 0,
      }),
    });
    renderWorkbench(api);

    await screen.findByRole('heading', { name: '当前目标与验收' });
    await user.click(await screen.findByRole('button', { name: '停止旧执行并采用 v1' }));

    await waitFor(() => expect(api.adoptSessionRequirements).toHaveBeenCalledWith('project-1', 'session-owner', {
      baseRevision: 0,
      revision: 1,
    }, 'csrf-memory'));
    expect(await screen.findByRole('status')).toHaveTextContent('后续执行不会自动开始');
  });

  it('keeps the captured brief base when a background refresh arrives during editing', async () => {
    const user = userEvent.setup();
    const refreshedBrief = {
      ...brief,
      revision: 2,
      objective: '服务器刚刚发布的新目标',
      acceptanceCriteria: ['服务器当前验收条件'],
    };
    const api = fakeApi({
      getProjectOverview: vi.fn().mockResolvedValueOnce(overview).mockResolvedValueOnce({ ...overview, brief: refreshedBrief }),
      updateProjectBrief: vi.fn().mockResolvedValue({ ...brief, revision: 3, objective: '我的草稿目标' }),
    });
    renderWorkbench(api);

    await screen.findByRole('heading', { name: '当前目标与验收' });
    await user.click(await screen.findByRole('button', { name: '编辑当前目标' }));
    const objective = screen.getByRole('textbox', { name: '当前目标' });
    await user.clear(objective);
    await user.type(objective, '我的草稿目标');
    await user.click(screen.getByRole('button', { name: '刷新项目工作台' }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: '当前目标' })).toHaveValue('我的草稿目标'));
    await user.click(screen.getByRole('button', { name: '保存需求 v3' }));

    await waitFor(() => expect(api.updateProjectBrief).toHaveBeenCalledWith('project-1', expect.objectContaining({
      baseRevision: 1,
      objective: '我的草稿目标',
    }), 'csrf-memory'));
  });

  it('refreshes the visible project on focus without replacing the workbench with a skeleton', async () => {
    const refreshedOverview = {
      ...overview,
      brief: { ...brief, revision: 2, objective: '聚焦后读取到的新目标' },
    };
    const api = fakeApi({
      getProjectOverview: vi.fn().mockResolvedValueOnce(overview).mockResolvedValueOnce(refreshedOverview),
    });
    renderWorkbench(api);

    await screen.findByRole('heading', { name: '当前目标与验收' });
    window.dispatchEvent(new Event('focus'));
    await waitFor(() => expect(api.getProjectOverview).toHaveBeenCalledTimes(2));
    const objective = document.querySelector('.team-project-workbench__objective');
    expect(objective).toHaveTextContent('聚焦后读取到的新目标');
    expect(screen.queryByRole('status', { name: '正在读取项目概览' })).not.toBeInTheDocument();
  });

  it('waits out an older background read and refreshes after saving a new brief', async () => {
    const user = userEvent.setup();
    const staleRead = deferred<TeamProjectOverview>();
    const savedBrief = { ...brief, revision: 2, objective: '保存后的新目标' };
    const api = fakeApi({
      getProjectOverview: vi.fn().mockResolvedValueOnce(overview)
        .mockReturnValueOnce(staleRead.promise)
        .mockResolvedValue({ ...overview, brief: savedBrief }),
      updateProjectBrief: vi.fn().mockResolvedValue(savedBrief),
    });
    renderWorkbench(api);
    await user.click(await screen.findByRole('button', { name: '编辑当前目标' }));
    const objective = screen.getByRole('textbox', { name: '当前目标' });
    await user.clear(objective);
    await user.type(objective, savedBrief.objective);
    act(() => window.dispatchEvent(new Event('focus')));
    await waitFor(() => expect(api.getProjectOverview).toHaveBeenCalledTimes(2));
    await user.click(screen.getByRole('button', { name: '保存需求 v2' }));
    await screen.findByText('需求 v2 已保存。新的执行仍需组织。');
    await act(async () => staleRead.resolve(overview));
    await waitFor(() => expect(api.getProjectOverview).toHaveBeenCalledTimes(3));
    expect(screen.getByText(savedBrief.objective)).toBeInTheDocument();
  });

  it('keeps an unsaved draft and shows the reread server brief after a CAS conflict', async () => {
    const user = userEvent.setup();
    const serverBrief = {
      ...brief,
      revision: 2,
      objective: '另一位成员已经补充了公司名称要求',
      acceptanceCriteria: ['公司名称必须填写', '重复邮箱有明确提示'],
    };
    const api = fakeApi({
      getProjectOverview: vi.fn().mockResolvedValueOnce(overview).mockResolvedValueOnce({ ...overview, brief: serverBrief }),
      updateProjectBrief: vi.fn()
        .mockRejectedValueOnce(new TeamApiError('team.project-brief.update', 409, { error: '需求版本已变化' }))
        .mockResolvedValueOnce(serverBrief),
    });
    renderWorkbench(api);

    await screen.findByRole('heading', { name: '当前目标与验收' });
    await user.click(await screen.findByRole('button', { name: '编辑当前目标' }));
    const objective = screen.getByRole('textbox', { name: '当前目标' });
    await user.clear(objective);
    await user.type(objective, '我的未保存修改');
    await user.click(screen.getByRole('button', { name: '保存需求 v2' }));

    const conflict = await screen.findByRole('alert');
    expect(conflict).toHaveTextContent('你的草稿已保留');
    expect(conflict).toHaveTextContent('另一位成员已经补充了公司名称要求');
    expect(conflict).toHaveTextContent('公司名称必须填写');
    expect(objective).toHaveValue('我的未保存修改');
    expect(api.updateProjectBrief).toHaveBeenCalledWith('project-1', expect.objectContaining({ baseRevision: 1 }), 'csrf-memory');

    const criteriaBeforeRebase = (screen.getByRole('textbox', { name: '验收条件（每行一项）' }) as HTMLTextAreaElement).value;
    await user.click(screen.getByRole('button', { name: '保留草稿，基于 v2 继续编辑' }));
    expect(objective).toHaveValue('我的未保存修改');
    expect(screen.getByRole('textbox', { name: '验收条件（每行一项）' })).toHaveValue(criteriaBeforeRebase);
    await user.clear(objective);
    await user.type(objective, '基于新版本继续编辑');
    await user.click(screen.getByRole('button', { name: '保存需求 v3' }));
    await waitFor(() => expect(api.updateProjectBrief).toHaveBeenLastCalledWith('project-1', expect.objectContaining({
      baseRevision: 2,
      objective: '基于新版本继续编辑',
    }), 'csrf-memory'));
  });

  it('shows a recoverable loading and service error state', async () => {
    const user = userEvent.setup();
    const pending = deferred<TeamProjectOverview>();
    const api = fakeApi({
      getProjectOverview: vi.fn().mockReturnValueOnce(pending.promise).mockRejectedValueOnce(new Error('服务暂时不可用')),
    });
    renderWorkbench(api);

    expect(await screen.findByRole('status', { name: '正在读取项目概览' })).toBeVisible();
    pending.resolve(overview);
    await screen.findByRole('heading', { name: '当前目标与验收' });
    await user.click(screen.getByRole('button', { name: '刷新项目工作台' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('服务暂时不可用');
    expect(screen.getByRole('button', { name: '重新读取' })).toBeVisible();
  });
});

function renderWorkbench(
  api: TeamApi,
  openWindow: (request: PawOsWindowRequest) => void = vi.fn(),
  onNavigate = vi.fn(),
  openApp = vi.fn(),
) {
  return render(
    <TeamProvider api={api}>
      <PawOsDesktopProvider openApp={openApp} openWindow={openWindow}>
        <TeamProjectWorkbench onNavigate={onNavigate} />
      </PawOsDesktopProvider>
    </TeamProvider>,
  );
}

function fakeApi(overrides: Partial<Record<keyof TeamApi, unknown>> = {}): TeamApi {
  return {
    status: vi.fn().mockResolvedValue({ enabled: true, name: 'PAW Team' }),
    login: vi.fn(),
    me: vi.fn().mockResolvedValue(session),
    logout: vi.fn().mockResolvedValue({ ok: true }),
    createProject: vi.fn(),
    listMembers: vi.fn().mockResolvedValue([]),
    listDirectory: vi.fn().mockResolvedValue([]),
    createMember: vi.fn(),
    setMemberStatus: vi.fn(),
    listProjectMembers: vi.fn().mockResolvedValue([]),
    addProjectMember: vi.fn(),
    removeProjectMember: vi.fn(),
    getProjectOverview: vi.fn().mockResolvedValue(overview),
    getProjectPreview: vi.fn().mockResolvedValue(preview),
    startProjectPreview: vi.fn().mockResolvedValue(preview),
    stopProjectPreview: vi.fn().mockResolvedValue(preview),
    updateProjectBrief: vi.fn(),
    adoptSessionRequirements: vi.fn(),
    listProjectDrafts: vi.fn().mockResolvedValue([]),
    listProjectSessions: vi.fn().mockResolvedValue([]),
    getProjectDraftDiff: vi.fn(),
    publishProjectDraft: vi.fn(),
    integrateProjectDraft: vi.fn(),
    adoptProjectDraft: vi.fn(),
    ...overrides,
  } as unknown as TeamApi;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}
