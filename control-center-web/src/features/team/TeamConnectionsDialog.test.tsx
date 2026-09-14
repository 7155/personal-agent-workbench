import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { StrictMode } from 'react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TeamApi } from './team-api';
import { TeamConnectionsDialog } from './TeamConnectionsDialog';
import { TeamProvider, useTeam } from './team-context';
import type { TeamConnection, TeamConnectionGrant, TeamConnectionList, TeamSession } from './types';

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
const connection: TeamConnection = {
  id: 'connection-1',
  provider: 'github',
  scope: 'personal',
  ownerId: session.user.id,
  label: '代码只读',
  accountLogin: 'zhou',
  repositories: ['acme/repo'],
  operations: ['repo.read', 'file.read'],
  status: 'active',
  revision: 1,
  canManage: true,
  createdAtMs: 123,
};
const grant: TeamConnectionGrant = {
  id: 'grant-1',
  connectionId: connection.id,
  sessionId: 'session-1',
  spaceId: project.id,
  repository: 'acme/repo',
  operations: ['repo.read'],
  expiresAtMs: Date.now() + 3_600_000,
  status: 'active' as const,
  connectionLabel: connection.label,
  accountLogin: connection.accountLogin,
};
const connectionList: TeamConnectionList = {
  configured: true,
  oauthAvailable: true,
  items: [connection],
  grants: [grant],
  sessions: [{ id: 'session-1', title: '官网任务', status: 'idle', ownerUserId: session.user.id }],
  canCreateProject: true,
};

afterEach(() => cleanup());

describe('TeamConnectionsDialog', () => {
  it('shows ownership, exact repositories and operations, grants, and project sharing disclosure', async () => {
    const api = fakeApi({ listConnections: vi.fn().mockResolvedValue(connectionList) });
    renderDialog(api);

    expect(await screen.findByRole('heading', { name: '连接 GitHub' })).toBeVisible();
    expect(screen.getAllByText('代码只读').length).toBeGreaterThan(0);
    expect(screen.getByText(/个人连接 · 你的账号/)).toBeVisible();
    expect(screen.getAllByText(/acme\/repo/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/读取仓库信息/).length).toBeGreaterThan(0);
    expect(screen.getByText('授权中')).toBeVisible();
    expect(await screen.findByRole('note')).toHaveTextContent('个人连接；返回结果会进入当前项目会话');
    expect(screen.getByRole('combobox', { name: '我的任务' })).toHaveValue('session-1');
  });

  it('reviews and consumes the OAuth callback result while preserving unrelated URL state', async () => {
    const previousUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    window.history.replaceState(window.history.state, '', '/desktop?keep=1&teamConnection=connected#anchor');
    try {
      const api = fakeApi({ listConnections: vi.fn().mockResolvedValue({ ...connectionList, items: [], grants: [] }) });
      renderDialog(api);

      expect(await screen.findByText('已返回 GitHub 授权，请在下方核对连接结果。')).toBeVisible();
      expect(window.location.pathname).toBe('/desktop');
      expect(window.location.search).toBe('?keep=1');
      expect(window.location.hash).toBe('#anchor');
      expect(new URLSearchParams(window.location.search).has('teamConnection')).toBe(false);
      expect(api.listConnections).toHaveBeenCalledWith('project-1', 'csrf-memory');
    } finally {
      window.history.replaceState(window.history.state, '', previousUrl || '/');
    }
  });

  it('creates a PAT connection with normalized repositories and clears the secret after the request', async () => {
    const user = userEvent.setup();
    const api = fakeApi({
      listConnections: vi.fn().mockResolvedValue(connectionList),
      createConnectionWithToken: vi.fn().mockResolvedValue(connection),
    });
    renderDialog(api);

    await screen.findAllByText('代码只读');
    await user.type(screen.getByRole('textbox', { name: '连接名称' }), '官网 PAT');
    await user.type(screen.getByRole('textbox', { name: '允许仓库' }), 'Acme/Repo');
    await user.type(screen.getByLabelText(/GitHub PAT/), 'ghp_secret_value');
    await user.click(screen.getByRole('button', { name: '保存 PAT 连接' }));

    await waitFor(() => expect(api.createConnectionWithToken).toHaveBeenCalledWith(
      'project-1',
      expect.objectContaining({
        scope: 'personal',
        label: '官网 PAT',
        repositories: ['acme/repo'],
        operations: ['repo.read', 'file.read', 'issues.list', 'issue.read'],
      }),
      'ghp_secret_value',
      'csrf-memory',
    ));
    expect(screen.getByLabelText(/GitHub PAT/)).toHaveValue('');
    expect(window.localStorage.getItem('ghp_secret_value')).toBeNull();
    expect(await screen.findByRole('status')).toHaveTextContent('已添加 GitHub 连接');
  });

  it('starts OAuth only after an explicit click and keeps the safe result same-tab', async () => {
    const user = userEvent.setup();
    const authorizationUrl = 'https://github.com/login/oauth/authorize?state=opaque&code_challenge=opaque';
    const api = fakeApi({
      listConnections: vi.fn().mockResolvedValue(connectionList),
      startConnectionOAuth: vi.fn().mockResolvedValue({ authorizationUrl }),
    });
    const navigate = vi.fn();
    renderDialog(api, navigate);

    await screen.findAllByText('代码只读');
    expect(navigate).not.toHaveBeenCalled();
    expect(api.startConnectionOAuth).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '使用 GitHub 授权' }));
    await user.type(screen.getByRole('textbox', { name: '连接名称' }), 'OAuth 只读');
    await user.type(screen.getByRole('textbox', { name: '允许仓库' }), 'Acme/Repo');
    await user.click(screen.getByRole('button', { name: '前往 GitHub 授权' }));

    await waitFor(() => expect(navigate).toHaveBeenCalledExactlyOnceWith(authorizationUrl));
    expect(api.startConnectionOAuth).toHaveBeenCalledWith('project-1', expect.objectContaining({ repositories: ['acme/repo'] }), 'csrf-memory');
  });

  it('keeps explicit operation choices on refresh and does not select writes by default', async () => {
    const user = userEvent.setup();
    const source = { ...connectionList, items: [{ ...connection, operations: ['repo.read', 'issue.create'] as const }] };
    const api = fakeApi({ listConnections: vi.fn().mockImplementation(async () => JSON.parse(JSON.stringify(source))) });
    render(<StrictMode><TeamProvider api={api}><TeamConnectionsDialog onOpenChange={() => undefined} open /></TeamProvider></StrictMode>);
    const choices = within(await screen.findByRole('group', { name: '这项任务允许的操作' }));
    await waitFor(() => expect(choices.getByRole('checkbox', { name: '读取仓库信息' })).toBeChecked());
    expect(choices.getByRole('checkbox', { name: '创建 Issue' })).not.toBeChecked();
    await user.click(choices.getByRole('checkbox', { name: '读取仓库信息' }));
    await user.click(screen.getByRole('button', { name: '刷新' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '刷新' })).not.toBeDisabled());
    expect(choices.getByRole('checkbox', { name: '读取仓库信息' })).not.toBeChecked();
    expect(choices.getByRole('checkbox', { name: '创建 Issue' })).not.toBeChecked();
    expect(screen.getByText('任务：官网任务')).toBeVisible();
  });

  it('rejects a mixed invalid repository list and accepts a leading-dot GitHub repository', async () => {
    const user = userEvent.setup();
    const api = fakeApi();
    renderDialog(api);
    await user.type(await screen.findByRole('textbox', { name: '连接名称' }), 'Repository scope');
    await user.type(screen.getByRole('textbox', { name: '允许仓库' }), 'Acme/.github\nhttps://example.test/elsewhere');
    await user.type(screen.getByLabelText(/GitHub PAT/), 'fixture-private-token');
    await user.click(screen.getByRole('button', { name: '保存 PAT 连接' }));
    expect(api.createConnectionWithToken).not.toHaveBeenCalled();
    await user.clear(screen.getByRole('textbox', { name: '允许仓库' }));
    await user.type(screen.getByRole('textbox', { name: '允许仓库' }), 'Acme/.github');
    await user.click(screen.getByRole('button', { name: '保存 PAT 连接' }));
    await waitFor(() => expect(api.createConnectionWithToken).toHaveBeenCalledWith('project-1', expect.objectContaining({ repositories: ['acme/.github'] }), 'fixture-private-token', 'csrf-memory'));
  });

  it('ignores a pending PAT result after switching spaces without clearing the new space secret', async () => {
    const user = userEvent.setup();
    const pendingCreate = deferred<TeamConnection>();
    const projectTwo = { ...project, id: 'project-2', name: '移动端', role: 'contributor' as const };
    const multiSpaceSession: TeamSession = { ...session, spaces: [project, projectTwo] };
    const projectTwoConnection = { ...connection, id: 'connection-2', label: '移动端连接' };
    const api = fakeApi({
      me: vi.fn().mockResolvedValue(multiSpaceSession),
      listConnections: vi.fn((requestedSpaceId: string) => Promise.resolve(
        requestedSpaceId === 'project-1'
          ? connectionList
          : { ...connectionList, items: [projectTwoConnection], grants: [] },
      )),
      createConnectionWithToken: vi.fn().mockReturnValue(pendingCreate.promise),
    });
    render(
      <TeamProvider api={api}>
        <SwitchSpace />
        <TeamConnectionsDialog navigate={vi.fn()} onOpenChange={() => undefined} open />
      </TeamProvider>,
    );

    await screen.findByLabelText(/GitHub PAT/);
    await user.type(screen.getByRole('textbox', { name: '连接名称' }), '旧空间 PAT');
    await user.type(screen.getByRole('textbox', { name: '允许仓库' }), 'Acme/Repo');
    await user.type(screen.getByLabelText(/GitHub PAT/), 'old-secret');
    await user.click(screen.getByRole('button', { name: '保存 PAT 连接' }));
    await waitFor(() => expect(api.createConnectionWithToken).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { hidden: true, name: '切换到移动端' }));
    expect(await screen.findByText('移动端连接')).toBeVisible();
    await user.type(screen.getByLabelText(/GitHub PAT/), 'new-secret');
    pendingCreate.resolve(connection);
    await waitFor(() => expect(screen.getByLabelText(/GitHub PAT/)).toHaveValue('new-secret'));
  });

  it('creates a bounded grant for a caller-owned Session and supports revocation', async () => {
    const user = userEvent.setup();
    const api = fakeApi({
      listConnections: vi.fn().mockResolvedValue(connectionList),
      createConnectionGrant: vi.fn().mockResolvedValue(grant),
      revokeConnectionGrant: vi.fn().mockResolvedValue({ ...grant, status: 'revoked' }),
      revokeConnection: vi.fn().mockResolvedValue({ ...connection, status: 'revoked' }),
    });
    renderDialog(api);

    await screen.findAllByText('代码只读');
    await user.click(screen.getByRole('button', { name: '授权给我的任务' }));
    await waitFor(() => expect(api.createConnectionGrant).toHaveBeenCalledWith('project-1', {
      connectionId: 'connection-1',
      sessionId: 'session-1',
      repository: 'acme/repo',
      operations: ['repo.read', 'file.read'],
      ttlSeconds: 28_800,
    }, 'csrf-memory'));
    expect(await screen.findByRole('status')).toHaveTextContent('已将');

    await user.click(screen.getByRole('button', { name: '撤销授权' }));
    await waitFor(() => expect(api.revokeConnectionGrant).toHaveBeenCalledWith('project-1', 'grant-1', 'csrf-memory'));
    await user.click(screen.getByRole('button', { name: '撤销连接' }));
    await waitFor(() => expect(api.revokeConnection).toHaveBeenCalledWith('project-1', 'connection-1', 'csrf-memory'));
  });

  it('rejects an unsafe OAuth result without exposing a navigation target', async () => {
    const user = userEvent.setup();
    const api = fakeApi({
      listConnections: vi.fn().mockResolvedValue(connectionList),
      startConnectionOAuth: vi.fn().mockResolvedValue({ authorizationUrl: 'https://evil.example.test/oauth' }),
    });
    const navigate = vi.fn();
    renderDialog(api, navigate);

    await screen.findAllByText('代码只读');
    expect(navigate).not.toHaveBeenCalled();
    expect(api.startConnectionOAuth).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '使用 GitHub 授权' }));
    await user.type(screen.getByRole('textbox', { name: '连接名称' }), '恶意地址测试');
    await user.type(screen.getByRole('textbox', { name: '允许仓库' }), 'Acme/Repo');
    await user.click(screen.getByRole('button', { name: '前往 GitHub 授权' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('GitHub 授权地址不安全');
    expect(navigate).not.toHaveBeenCalled();
  });

  it('drops a previous space response and clears secret input after switching spaces', async () => {
    const user = userEvent.setup();
    const oldSpaceRead = deferred<TeamConnectionList>();
    const projectTwo = { ...project, id: 'project-2', name: '移动端', role: 'contributor' as const };
    const multiSpaceSession: TeamSession = { ...session, spaces: [project, projectTwo] };
    const projectTwoConnection = { ...connection, id: 'connection-2', label: '移动端连接' };
    const projectTwoList: TeamConnectionList = { ...connectionList, items: [projectTwoConnection], grants: [] };
    let projectOneReads = 0;
    const api = fakeApi({
      me: vi.fn().mockResolvedValue(multiSpaceSession),
      listConnections: vi.fn((requestedSpaceId: string) => {
        if (requestedSpaceId === 'project-1') {
          projectOneReads += 1;
          return projectOneReads === 1 ? Promise.resolve(connectionList) : oldSpaceRead.promise;
        }
        return Promise.resolve(projectTwoList);
      }),
    });
    render(
      <TeamProvider api={api}>
        <SwitchSpace />
        <TeamConnectionsDialog navigate={vi.fn()} onOpenChange={() => undefined} open />
      </TeamProvider>,
    );

    await waitFor(() => expect(api.listConnections).toHaveBeenCalledWith('project-1', 'csrf-memory'));
    await screen.findByLabelText(/GitHub PAT/);
    fireEvent.change(screen.getByLabelText(/GitHub PAT/), { target: { value: 'ghp_should_clear' } });
    await user.click(screen.getByRole('button', { name: '刷新' }));
    await waitFor(() => expect(api.listConnections).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { hidden: true, name: '切换到移动端' }));
    await waitFor(() => expect(api.listConnections).toHaveBeenCalledWith('project-2', 'csrf-memory'));
    expect(await screen.findByText('移动端连接')).toBeVisible();
    expect(screen.getByLabelText(/GitHub PAT/)).toHaveValue('');
    await oldSpaceRead.resolve(connectionList);
    await waitFor(() => expect(screen.getByText('移动端连接')).toBeVisible());
    expect(screen.queryByText('代码只读')).not.toBeInTheDocument();
  });
});

function renderDialog(api: TeamApi, navigate = vi.fn()) {
  return render(<TeamProvider api={api}><TeamConnectionsDialog navigate={navigate} onOpenChange={() => undefined} open /></TeamProvider>);
}

function SwitchSpace() {
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
    getProjectPreview: vi.fn(),
    startProjectPreview: vi.fn(),
    stopProjectPreview: vi.fn(),
    listConnections: vi.fn().mockResolvedValue(connectionList),
    createConnectionWithToken: vi.fn().mockResolvedValue(connection),
    startConnectionOAuth: vi.fn().mockResolvedValue({ authorizationUrl: 'https://github.com/login/oauth/authorize?state=opaque' }),
    revokeConnection: vi.fn().mockResolvedValue({ ...connection, status: 'revoked' }),
    createConnectionGrant: vi.fn().mockResolvedValue(grant),
    revokeConnectionGrant: vi.fn().mockResolvedValue({ ...grant, status: 'revoked' }),
    updateProjectBrief: vi.fn(),
    adoptSessionRequirements: vi.fn(),
    listProjectDrafts: vi.fn().mockResolvedValue([]),
    listProjectSessions: vi.fn().mockResolvedValue([]),
    getProjectDraftDiff: vi.fn(),
    publishProjectDraft: vi.fn(),
    integrateProjectDraft: vi.fn(),
    adoptProjectDraft: vi.fn(),
    openProjectPreview: vi.fn(),
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
