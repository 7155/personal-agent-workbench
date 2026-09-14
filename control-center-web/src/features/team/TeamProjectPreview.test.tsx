import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TeamApi, TeamApiError } from './team-api';
import { TeamProvider, useTeam } from './team-context';
import { TeamProjectPreviewCard } from './TeamProjectPreview';
import type { TeamProjectDeployment, TeamProjectPreview, TeamSession } from './types';

const project = {
  id: 'project-1',
  kind: 'project' as const,
  name: '官网',
  role: 'maintainer' as const,
  revision: 3,
};
const deployment: TeamProjectDeployment = {
  id: 'preview-1',
  status: 'ready',
  branch: 'team/main',
  commit: 'a'.repeat(40),
  requirementsRevision: 1,
  requestedByDisplayName: '小周',
  createdAtMs: 1_700_000_000_000,
  readyAtMs: 1_700_000_001_000,
};
const session: TeamSession = {
  user: { id: 'user-owner', username: 'zhou', displayName: '小周', role: 'member', active: true },
  csrfToken: 'csrf-memory',
  spaces: [project],
};

afterEach(() => cleanup());

describe('TeamProjectPreviewCard', () => {
  it('shows a fixed ready commit and same-origin open handoff for managers', async () => {
    const api = fakeApi({ getProjectPreview: vi.fn().mockResolvedValue(readyPreview()) });
    renderCard(api);

    expect(await screen.findByText('已就绪')).toBeVisible();
    expect(screen.getByText('aaaaaaaa…aaaa')).toBeVisible();
    const link = screen.getByRole('link', { name: '打开预览' });
    expect(link).toHaveAttribute('href', '/api/team/projects/project-1/preview/open');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getByRole('button', { name: '刷新共享预览状态' })).toBeVisible();
    expect(screen.getByRole('button', { name: '生成新预览' })).toBeVisible();
    expect(screen.getByRole('button', { name: '停止预览' })).toBeVisible();
  });

  it('keeps the previous preview available while a new deployment is starting', async () => {
    const user = userEvent.setup();
    const oldPreview = readyPreview();
    const startingPreview: TeamProjectPreview = {
      ...oldPreview,
      latest: { ...deployment, id: 'preview-2', status: 'starting', commit: 'b'.repeat(40), requirementsRevision: 2 },
    };
    const api = fakeApi({
      getProjectPreview: vi.fn().mockResolvedValue(oldPreview),
      startProjectPreview: vi.fn().mockResolvedValue(startingPreview),
    });
    renderCard(api);

    await user.click(await screen.findByRole('button', { name: '生成新预览' }));
    await waitFor(() => expect(api.startProjectPreview).toHaveBeenCalledWith('project-1', expect.any(String), 'csrf-memory'));
    expect(await screen.findByText('正在准备固定预览')).toBeVisible();
    expect(screen.getByText('当前可用版本')).toBeVisible();
    expect(screen.getByText('生成中')).toBeVisible();
    expect(screen.getByRole('link', { name: '打开预览' })).toBeVisible();
    expect(screen.getByText('aaaaaaaa…aaaa')).toBeVisible();
    expect(screen.queryByText('bbbbbbbb…bbbb')).toBeInTheDocument();
  });

  it('lets a manager stop a first preview that is still starting and retry cleanup', async () => {
    const user = userEvent.setup();
    const starting: TeamProjectPreview = {
      ...readyPreview(),
      active: null,
      latest: { ...deployment, status: 'starting' },
      openPath: null,
    };
    const recovering: TeamProjectPreview = {
      ...starting,
      latest: { ...starting.latest!, status: 'recovery_required' },
    };
    const stopped: TeamProjectPreview = {
      ...recovering,
      latest: { ...recovering.latest!, status: 'stopped' },
    };
    const api = fakeApi({
      getProjectPreview: vi.fn().mockResolvedValue(starting),
      stopProjectPreview: vi.fn().mockResolvedValueOnce(recovering).mockResolvedValueOnce(stopped),
    });
    renderCard(api);

    expect(await screen.findByText('生成中')).toBeVisible();
    await user.click(screen.getByRole('button', { name: '停止预览' }));
    expect(await screen.findByText('最新预览需要恢复')).toBeVisible();
    await user.click(screen.getByRole('button', { name: '重试清理' }));
    await waitFor(() => expect(api.stopProjectPreview).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('已停止')).toBeVisible();
  });

  it('keeps the prior truthful state and shows an actionable error when start fails', async () => {
    const user = userEvent.setup();
    const api = fakeApi({
      getProjectPreview: vi.fn().mockResolvedValue(readyPreview()),
      startProjectPreview: vi.fn().mockRejectedValue(new TeamApiError(
        'team.project-preview.start',
        503,
        { error: '执行服务健康检查失败，请联系管理员。' },
      )),
    });
    renderCard(api);

    await user.click(await screen.findByRole('button', { name: '生成新预览' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('执行服务健康检查失败，请联系管理员。');
    expect(screen.getByText('已就绪')).toBeVisible();
    expect(screen.getByRole('link', { name: '打开预览' })).toBeVisible();
  });

  it('does not let a background read overwrite a completed start or stop', async () => {
    const user = userEvent.setup();
    const oldRead = deferred<TeamProjectPreview>();
    const stopRead = deferred<TeamProjectPreview>();
    const oldPreview = readyPreview();
    const startedPreview = readyPreview({
      active: { ...deployment, id: 'preview-2', commit: 'b'.repeat(40) },
      latest: { ...deployment, id: 'preview-2', commit: 'b'.repeat(40) },
    });
    const stoppedPreview: TeamProjectPreview = {
      ...startedPreview,
      active: null,
      latest: { ...startedPreview.latest!, status: 'stopped' },
      openPath: null,
    };
    const api = fakeApi({
      getProjectPreview: vi.fn()
        .mockResolvedValueOnce(oldPreview)
        .mockReturnValueOnce(oldRead.promise)
        .mockReturnValueOnce(stopRead.promise),
      startProjectPreview: vi.fn().mockResolvedValue(startedPreview),
      stopProjectPreview: vi.fn().mockResolvedValue(stoppedPreview),
    });
    renderCard(api);

    await screen.findByText('aaaaaaaa…aaaa');
    fireEvent.focus(window);
    await waitFor(() => expect(api.getProjectPreview).toHaveBeenCalledTimes(2));
    await user.click(screen.getByRole('button', { name: '生成新预览' }));
    expect(await screen.findByText('bbbbbbbb…bbbb')).toBeVisible();
    await oldRead.resolve(oldPreview);
    await waitFor(() => expect(screen.getByText('bbbbbbbb…bbbb')).toBeVisible());

    fireEvent.focus(window);
    await waitFor(() => expect(api.getProjectPreview).toHaveBeenCalledTimes(3));
    await user.click(screen.getByRole('button', { name: '停止预览' }));
    expect(await screen.findByText('已停止')).toBeVisible();
    await stopRead.resolve(startedPreview);
    await waitFor(() => expect(screen.getByText('已停止')).toBeVisible());
    expect(screen.queryByRole('link', { name: '打开预览' })).not.toBeInTheDocument();
  });

  it('invalidates a status read that starts while a preview start POST is pending', async () => {
    const user = userEvent.setup();
    const start = deferred<TeamProjectPreview>();
    const duringPostRead = deferred<TeamProjectPreview>();
    const oldPreview = readyPreview();
    const startedPreview = readyPreview({
      active: { ...deployment, id: 'preview-2', commit: 'b'.repeat(40) },
      latest: { ...deployment, id: 'preview-2', commit: 'b'.repeat(40) },
    });
    const api = fakeApi({
      getProjectPreview: vi.fn().mockResolvedValueOnce(oldPreview).mockReturnValueOnce(duringPostRead.promise),
      startProjectPreview: vi.fn().mockReturnValue(start.promise),
    });
    renderCard(api);

    await screen.findByText('aaaaaaaa…aaaa');
    const startClick = user.click(screen.getByRole('button', { name: '生成新预览' }));
    await waitFor(() => expect(api.startProjectPreview).toHaveBeenCalled());
    fireEvent.focus(window);
    await waitFor(() => expect(api.getProjectPreview).toHaveBeenCalledTimes(2));
    await start.resolve(startedPreview);
    await screen.findByText('bbbbbbbb…bbbb');
    await duringPostRead.resolve(oldPreview);
    await waitFor(() => expect(screen.getByText('bbbbbbbb…bbbb')).toBeVisible());
    await startClick;
  });

  it('marks a fixed preview as outdated when the project requirements moved on', async () => {
    const api = fakeApi({
      getProjectPreview: vi.fn().mockResolvedValue({
        ...readyPreview(),
        currentRequirementsRevision: 2,
        repositoryHead: 'b'.repeat(40),
      }),
    });
    renderCard(api);

    expect(await screen.findByText(/需求已更新/)).toBeVisible();
    expect(screen.getByText(/未证明满足最新版本/)).toBeVisible();
    expect(screen.getByText(/固定提交与当前稳定分支 HEAD 不同/)).toBeVisible();
  });

  it('gives readers the open handoff while hiding manager-only mutations', async () => {
    const viewerSession: TeamSession = {
      ...session,
      spaces: [{ ...project, role: 'viewer' }],
    };
    const api = fakeApi({
      me: vi.fn().mockResolvedValue(viewerSession),
      getProjectPreview: vi.fn().mockResolvedValue({ ...readyPreview(), canManage: false }),
    });
    renderCard(api);

    expect(await screen.findByRole('link', { name: '打开预览' })).toBeVisible();
    expect(screen.queryByRole('button', { name: /生成|停止/u })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '刷新共享预览状态' })).toBeVisible();
  });

  it('drops a pending response after switching project scope', async () => {
    const first = deferred<TeamProjectPreview>();
    const projectTwo = { ...project, id: 'project-2', name: '移动端', role: 'viewer' as const };
    const twoSession: TeamSession = { ...session, spaces: [project, projectTwo] };
    const firstPreview = readyPreview({ active: { ...deployment, branch: 'team/old-project' } });
    const secondPreview = readyPreview({ active: { ...deployment, branch: 'team/new-project', commit: 'c'.repeat(40) } });
    const api = fakeApi({
      me: vi.fn().mockResolvedValue(twoSession),
      getProjectPreview: vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue(secondPreview),
    });
    render(
      <TeamProvider api={api}>
        <SwitchProject />
        <TeamProjectPreviewCard />
      </TeamProvider>,
    );

    await waitFor(() => expect(api.getProjectPreview).toHaveBeenCalledWith('project-1', 'csrf-memory'));
    fireEvent.click(screen.getByRole('button', { name: '切换到移动端' }));
    await waitFor(() => expect(api.getProjectPreview).toHaveBeenCalledWith('project-2', 'csrf-memory'));
    expect(await screen.findByText('team/new-project')).toBeVisible();
    await first.resolve(firstPreview);
    await waitFor(() => expect(screen.getByText('team/new-project')).toBeVisible());
    expect(screen.queryByText('team/old-project')).not.toBeInTheDocument();
  });
});

function readyPreview(overrides: Partial<TeamProjectPreview> = {}): TeamProjectPreview {
  return {
    configured: true,
    active: deployment,
    latest: deployment,
    currentRequirementsRevision: 1,
    repositoryHead: deployment.commit,
    canManage: true,
    openPath: '/api/team/projects/project-1/preview/open',
    ...overrides,
  };
}

function renderCard(api: TeamApi) {
  return render(<TeamProvider api={api}><TeamProjectPreviewCard /></TeamProvider>);
}

function SwitchProject() {
  const team = useTeam();
  return <button onClick={() => team.selectSpace('project-2')} type="button">切换到移动端</button>;
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
    getProjectOverview: vi.fn(),
    getProjectPreview: vi.fn().mockResolvedValue(readyPreview()),
    startProjectPreview: vi.fn().mockResolvedValue(readyPreview()),
    stopProjectPreview: vi.fn().mockResolvedValue(readyPreview()),
    updateProjectBrief: vi.fn(),
    adoptSessionRequirements: vi.fn(),
    listProjectDrafts: vi.fn().mockResolvedValue([]),
    listProjectSessions: vi.fn().mockResolvedValue([]),
    getProjectDraftDiff: vi.fn(),
    publishProjectDraft: vi.fn(),
    integrateProjectDraft: vi.fn(),
    adoptProjectDraft: vi.fn(),
    openProjectPreview: vi.fn().mockReturnValue(`${window.location.origin}/api/team/projects/project-1/preview/open`),
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
