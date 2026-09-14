import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TeamSwitcher } from './TeamSwitcher';
import { TeamProvider, useTeam } from './team-context';
import type { TeamApi } from './team-api';
import type { TeamSession } from './types';

const session: TeamSession = {
  user: { id: 'user-1', username: 'alice', displayName: 'Alice', role: 'admin', active: true },
  csrfToken: 'csrf-memory',
  spaces: [
    { id: 'personal-1', kind: 'personal', name: 'Alice 的空间', role: 'owner', revision: 1 },
    { id: 'project-1', kind: 'project', name: 'PAW Team', role: 'maintainer', revision: 3 },
  ],
};

afterEach(() => cleanup());

describe('TeamSwitcher', () => {
  it('switches only among spaces returned for the current account', async () => {
    const api = fakeApi();
    const user = userEvent.setup();
    render(<TeamProvider api={api}><><TeamSwitcher /><ActiveSpace /></></TeamProvider>);

    await waitFor(() => expect(screen.getByTestId('active-space')).toHaveTextContent('personal-1'));
    await user.click(screen.getByRole('button', { name: /账户与工作空间/ }));
    expect(screen.getByRole('menuitem', { name: /PAW Team/ })).toBeInTheDocument();

    await user.click(screen.getByRole('menuitem', { name: /PAW Team/ }));
    expect(screen.getByTestId('active-space')).toHaveTextContent('project-1');
  });

  it('opens GitHub connections for a regular project member', async () => {
    const api = fakeApi({
      me: vi.fn().mockResolvedValue({
        ...session,
        user: { ...session.user, role: 'member' },
        spaces: session.spaces.map((space) => space.id === 'project-1' ? { ...space, role: 'contributor' as const } : space),
      }),
      listConnections: vi.fn().mockResolvedValue({
        configured: false,
        oauthAvailable: false,
        items: [],
        grants: [],
        sessions: [],
        canCreateProject: false,
      }),
    });
    const user = userEvent.setup();
    render(<TeamProvider api={api}><TeamSwitcher /></TeamProvider>);

    await waitFor(() => expect(screen.getByTestId('team-switcher')).toBeInTheDocument());
    await user.click(screen.getByRole('button', { name: /账户与工作空间/ }));
    await user.click(screen.getByRole('menuitem', { name: '连接 GitHub' }));

    expect(await screen.findByRole('heading', { name: '连接 GitHub' })).toBeVisible();
    expect(api.listConnections).toHaveBeenCalledWith('personal-1', 'csrf-memory');
  });

  it('reopens connections to review an OAuth callback result and consumes only that query parameter', async () => {
    const previousUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    window.history.replaceState(window.history.state, '', '/desktop?keep=1&teamConnection=failed#anchor');
    try {
      const api = fakeApi({
        listConnections: vi.fn().mockResolvedValue({
          configured: false,
          oauthAvailable: false,
          items: [],
          grants: [],
          sessions: [],
          canCreateProject: false,
        }),
      });
      render(<TeamProvider api={api}><TeamSwitcher /></TeamProvider>);

      await waitFor(() => expect(screen.getByTestId('team-switcher')).toBeInTheDocument());
      expect(await screen.findByRole('heading', { name: '连接 GitHub' })).toBeVisible();
      expect(await screen.findByRole('alert')).toHaveTextContent('GitHub 连接没有完成');
      expect(window.location.pathname).toBe('/desktop');
      expect(window.location.search).toBe('?keep=1');
      expect(window.location.hash).toBe('#anchor');
      expect(new URLSearchParams(window.location.search).has('teamConnection')).toBe(false);
      expect(api.listConnections).toHaveBeenCalledWith('personal-1', 'csrf-memory');
    } finally {
      window.history.replaceState(window.history.state, '', previousUrl || '/');
    }
  });
});

function ActiveSpace() {
  const team = useTeam();
  return <output data-testid="active-space">{team.activeSpace?.id ?? 'none'}</output>;
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
    listProjectDrafts: vi.fn().mockResolvedValue([]),
    listProjectSessions: vi.fn().mockResolvedValue([]),
    getProjectDraftDiff: vi.fn(),
    publishProjectDraft: vi.fn(),
    integrateProjectDraft: vi.fn(),
    adoptProjectDraft: vi.fn(),
    getProjectOverview: vi.fn(),
    getProjectPreview: vi.fn(),
    startProjectPreview: vi.fn(),
    stopProjectPreview: vi.fn(),
    listConnections: vi.fn().mockResolvedValue({ configured: false, oauthAvailable: false, items: [], grants: [], sessions: [], canCreateProject: false }),
    createConnectionWithToken: vi.fn(),
    startConnectionOAuth: vi.fn(),
    revokeConnection: vi.fn(),
    createConnectionGrant: vi.fn(),
    revokeConnectionGrant: vi.fn(),
    updateProjectBrief: vi.fn(),
    adoptSessionRequirements: vi.fn(),
    openProjectPreview: vi.fn(),
    ...overrides,
  } as unknown as TeamApi;
}
