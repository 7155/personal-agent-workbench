import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TeamLoginScreen } from './TeamGateway';
import { TeamProvider } from './team-context';
import type { TeamApi } from './team-api';
import type { TeamSession } from './types';

const session: TeamSession = {
  user: { id: 'user-1', username: 'alice', displayName: 'Alice', role: 'member', active: true },
  csrfToken: 'csrf-memory',
  spaces: [{ id: 'personal-1', kind: 'personal', name: 'Alice 的空间', role: 'owner', revision: 1 }],
};

afterEach(() => cleanup());

describe('TeamLoginScreen', () => {
  it('keeps invalid credentials local and submits valid credentials to the team service', async () => {
    const api = fakeApi({
      status: vi.fn().mockResolvedValue({ enabled: true, name: 'PAW Team' }),
      me: vi.fn().mockRejectedValue(Object.assign(new Error('unauthenticated'), { status: 401 })),
      login: vi.fn().mockResolvedValue(session),
    });
    render(<TeamProvider api={api}><TeamLoginScreen /></TeamProvider>);

    await screen.findByRole('heading', { name: '进入共享工作台' });
    fireEvent.click(screen.getByRole('button', { name: '登录 PAW' }));
    expect(screen.getByRole('alert')).toHaveTextContent('至少 8 位密码');
    expect(api.login).not.toHaveBeenCalled();

    fireEvent.change(screen.getByRole('textbox', { name: /账号/ }), { target: { value: 'alice' } });
    fireEvent.change(screen.getByLabelText(/密码/), { target: { value: 'password-123' } });
    fireEvent.click(screen.getByRole('button', { name: '登录 PAW' }));
    await waitFor(() => expect(api.login).toHaveBeenCalledWith('alice', 'password-123'));
  });
});

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
    ...overrides,
  } as unknown as TeamApi;
}
