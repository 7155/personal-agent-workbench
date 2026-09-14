import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { StrictMode } from 'react';
import { TeamProvider, useTeam } from './team-context';
import type { TeamSession } from './types';
import type { TeamApi } from './team-api';

const personal = { id: 'personal-1', kind: 'personal' as const, name: 'Alice 的空间', role: 'owner' as const, revision: 1 };
const project = { id: 'project-1', kind: 'project' as const, name: 'PAW Team', role: 'maintainer' as const, revision: 3 };
const session: TeamSession = {
  user: { id: 'user-1', username: 'alice', displayName: 'Alice', role: 'admin', active: true },
  csrfToken: 'csrf-memory',
  spaces: [personal, project],
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('TeamProvider', () => {
  it('does not probe credentials outside its explicit Team deployment owner', async () => {
    const api = fakeApi({
      status: vi.fn().mockResolvedValue({ enabled: true, name: 'PAW Team' }),
      me: vi.fn().mockRejectedValue(Object.assign(new Error('unauthenticated'), { status: 401 })),
    });

    render(<TeamProvider api={api}><Probe /></TeamProvider>);

    await waitFor(() => expect(screen.getByTestId('phase')).toHaveTextContent('anonymous'));
    expect(api.status).toHaveBeenCalledOnce();
    expect(api.me).toHaveBeenCalledOnce();
    expect(window.localStorage.length).toBe(0);
  });

  it('keeps the personal default and remount key changes when selecting a project', async () => {
    const api = fakeApi({
      status: vi.fn().mockResolvedValue({ enabled: true, name: 'PAW Team' }),
      me: vi.fn().mockResolvedValue(session),
    });
    render(<TeamProvider api={api}><Probe /></TeamProvider>);

    await waitFor(() => expect(screen.getByTestId('space')).toHaveTextContent('personal-1'));
    expect(screen.getByTestId('scope')).toHaveTextContent('team:http://localhost:3000:user-1:personal-1');

    fireEvent.click(screen.getByRole('button', { name: '选择项目空间' }));
    expect(screen.getByTestId('space')).toHaveTextContent('project-1');
    expect(screen.getByTestId('scope')).toHaveTextContent('team:http://localhost:3000:user-1:project-1');
  });

  it('refreshes me after project creation and selects the returned space', async () => {
    const refreshed: TeamSession = { ...session, spaces: [...session.spaces, { ...project, id: 'project-2', name: '新项目', revision: 1 }] };
    const api = fakeApi({
      status: vi.fn().mockResolvedValue({ enabled: true, name: 'PAW Team' }),
      me: vi.fn().mockResolvedValueOnce(session).mockResolvedValueOnce(refreshed),
      createProject: vi.fn().mockResolvedValue({ ...project, id: 'project-2', name: '新项目', revision: 1 }),
    });
    render(<TeamProvider api={api}><Probe /></TeamProvider>);

    await waitFor(() => expect(screen.getByTestId('space')).toHaveTextContent('personal-1'));
    fireEvent.click(screen.getByRole('button', { name: '创建项目' }));
    await waitFor(() => expect(screen.getByTestId('space')).toHaveTextContent('project-2'));
    expect(api.createProject).toHaveBeenCalledWith('新项目', 'csrf-memory');
    expect(api.me).toHaveBeenCalledTimes(2);
  });

  it('does not restore the previous desktop when a pending me response arrives after logout', async () => {
    const pending = deferred<TeamSession>();
    const api = fakeApi({ me: vi.fn().mockResolvedValueOnce(session).mockReturnValueOnce(pending.promise) });
    render(<TeamProvider api={api}><Probe /></TeamProvider>);
    await screen.findByText('personal-1');
    fireEvent.click(screen.getByRole('button', { name: '刷新身份' }));
    fireEvent.click(screen.getByRole('button', { name: '退出' }));
    await waitFor(() => expect(screen.getByTestId('phase')).toHaveTextContent('anonymous'));
    await act(async () => { pending.resolve(session); });
    expect(screen.getByTestId('phase')).toHaveTextContent('anonymous');
    expect(screen.getByTestId('scope')).toHaveTextContent('none');
  });

  it.each(['success', 'unauthorized'] as const)('ignores a previous account me %s after the next sign-in', async (outcome) => {
    const pending = deferred<TeamSession>();
    const bob: TeamSession = { user: { ...session.user, id: 'user-2', username: 'bob', displayName: 'Bob' },
      csrfToken: 'bob-csrf', spaces: [{ ...personal, id: 'bob-private', name: 'Bob 的空间' }] };
    const api = fakeApi({ me: vi.fn().mockResolvedValueOnce(session).mockReturnValueOnce(pending.promise),
      login: vi.fn().mockResolvedValue(bob) });
    render(<TeamProvider api={api}><Probe /></TeamProvider>);
    await screen.findByText('personal-1');
    fireEvent.click(screen.getByRole('button', { name: '刷新身份' }));
    fireEvent.click(screen.getByRole('button', { name: '退出' }));
    await waitFor(() => expect(screen.getByTestId('phase')).toHaveTextContent('anonymous'));
    fireEvent.click(screen.getByRole('button', { name: '登录 Bob' }));
    await waitFor(() => expect(screen.getByTestId('space')).toHaveTextContent('bob-private'));
    await act(async () => {
      if (outcome === 'success') pending.resolve(session);
      else pending.reject(Object.assign(new Error('old login expired'), { status: 401 }));
    });
    expect(screen.getByTestId('phase')).toHaveTextContent('authenticated');
    expect(screen.getByTestId('scope')).toHaveTextContent('user-2:bob-private');
  });

  it('does not restore removed memberships from an older refresh of the same login', async () => {
    const older = deferred<TeamSession>();
    const current = deferred<TeamSession>();
    const api = fakeApi({ me: vi.fn().mockResolvedValueOnce(session)
      .mockReturnValueOnce(older.promise).mockReturnValueOnce(current.promise) });
    render(<TeamProvider api={api}><Probe /></TeamProvider>);
    await screen.findByText('personal-1');
    fireEvent.click(screen.getByRole('button', { name: '刷新身份' }));
    fireEvent.click(screen.getByRole('button', { name: '刷新身份' }));
    await act(async () => { current.resolve({ ...session, spaces: [personal] }); });
    await act(async () => { older.resolve(session); });
    expect(screen.getByTestId('spaces')).not.toHaveTextContent('project-1');
  });

  it('does not clear a newer same-identity refresh when the probe returns 401 late', async () => {
    const probe = deferred<TeamSession>();
    const refresh = deferred<TeamSession>();
    const api = fakeApi({
      me: vi.fn().mockReturnValueOnce(probe.promise).mockReturnValueOnce(refresh.promise),
    });
    render(<TeamProvider api={api}><Probe /></TeamProvider>);

    await waitFor(() => expect(api.me).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '刷新身份' }));
    await act(async () => { refresh.resolve(session); });
    await waitFor(() => expect(screen.getByTestId('phase')).toHaveTextContent('authenticated'));

    await act(async () => {
      probe.reject(Object.assign(new Error('probe expired'), { status: 401 }));
    });
    expect(screen.getByTestId('phase')).toHaveTextContent('authenticated');
    expect(screen.getByTestId('spaces')).toHaveTextContent('project-1');
  });

  it('does not start a new me request when a project mutation resolves after unmount', async () => {
    const creation = deferred<typeof project>();
    const api = fakeApi({ createProject: vi.fn().mockReturnValue(creation.promise) });
    const view = render(<TeamProvider api={api}><Probe /></TeamProvider>);

    await screen.findByText('personal-1');
    expect(api.me).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: '创建项目' }));
    await waitFor(() => expect(api.createProject).toHaveBeenCalledOnce());
    view.unmount();

    await act(async () => { creation.resolve(project); });
    expect(api.me).toHaveBeenCalledTimes(1);
  });

  it('keeps the current probe usable through StrictMode effect replay', async () => {
    const api = fakeApi();
    render(<StrictMode><TeamProvider api={api}><Probe /></TeamProvider></StrictMode>);

    await waitFor(() => expect(screen.getByTestId('phase')).toHaveTextContent('authenticated'));
    expect(screen.getByTestId('space')).toHaveTextContent('personal-1');
  });

  it('sends only one cookie-changing request for duplicate sign-in submissions', async () => {
    const pending = deferred<TeamSession>();
    const api = fakeApi({ login: vi.fn().mockReturnValue(pending.promise) });
    render(<TeamProvider api={api}><Probe /></TeamProvider>);
    await screen.findByText('personal-1');
    fireEvent.click(screen.getByRole('button', { name: '登录 Bob' }));
    fireEvent.click(screen.getByRole('button', { name: '登录 Bob' }));
    expect(api.login).toHaveBeenCalledOnce();
    await act(async () => { pending.resolve(session); });
    expect(screen.getByTestId('phase')).toHaveTextContent('authenticated');
  });
});

function Probe() {
  const team = useTeam();
  return (
    <>
      <output data-testid="phase">{team.phase}</output>
      <output data-testid="space">{team.activeSpace?.id ?? 'none'}</output>
      <output data-testid="scope">{team.scopeKey ?? 'none'}</output>
      <output data-testid="spaces">{team.spaces.map((space) => space.id).join(',')}</output>
      <button onClick={() => team.selectSpace('project-1')} type="button">选择项目空间</button>
      <button onClick={() => void team.createProject('新项目').catch(() => undefined)} type="button">创建项目</button>
      <button onClick={() => void team.refreshMe().catch(() => undefined)} type="button">刷新身份</button>
      <button onClick={() => void team.logout()} type="button">退出</button>
      <button onClick={() => void team.login('bob', 'fixture-password').catch(() => undefined)} type="button">登录 Bob</button>
    </>
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => { resolve = resolvePromise; reject = rejectPromise; });
  return { promise, resolve, reject };
}

function fakeApi(overrides: Partial<Record<keyof TeamApi, unknown>> = {}): TeamApi {
  return {
    status: vi.fn().mockResolvedValue({ enabled: true, name: 'PAW Team' }),
    login: vi.fn(),
    me: vi.fn().mockResolvedValue(session),
    logout: vi.fn().mockResolvedValue({ ok: true }),
    createProject: vi.fn().mockResolvedValue(project),
    listMembers: vi.fn().mockResolvedValue([]),
    listDirectory: vi.fn().mockResolvedValue([]),
    createMember: vi.fn(),
    setMemberStatus: vi.fn(),
    listProjectMembers: vi.fn().mockResolvedValue([]),
    addProjectMember: vi.fn(),
    removeProjectMember: vi.fn(),
    listProjectDrafts: vi.fn().mockResolvedValue([]),
    listProjectSessions: vi.fn().mockResolvedValue([]),
    getProjectDraftDiff: vi.fn(),
    publishProjectDraft: vi.fn(),
    integrateProjectDraft: vi.fn(),
    adoptProjectDraft: vi.fn(),
    ...overrides,
  } as unknown as TeamApi;
}
