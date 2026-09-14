import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TeamDraftPublishDialog } from './TeamDraftPublishDialog';
import type { TeamApi } from './team-api';
import { TeamProvider } from './team-context';
import type { TeamSession } from './types';

const session: TeamSession = {
  user: { id: 'user-1', username: 'alice', displayName: 'Alice', role: 'member', active: true },
  csrfToken: 'csrf-memory',
  spaces: [{ id: 'project-1', kind: 'project', name: 'PAW Team', role: 'contributor', revision: 3 }],
};

const draft = {
  draftId: 'draft-1',
  sessionId: 'session-1',
  title: '客服评测基线',
  creatorUserId: 'user-1',
  baseCommit: 'a'.repeat(40),
  draftCommit: 'b'.repeat(40),
  manifestHash: 'c'.repeat(64),
  createdAtMs: 123,
  status: 'pending',
};

afterEach(() => cleanup());

describe('TeamDraftPublishDialog', () => {
  it('publishes the current Session and shows the server draft receipt', async () => {
    const api = fakeApi({ publishProjectDraft: vi.fn().mockResolvedValue(draft) });
    const user = userEvent.setup();
    render(<TeamProvider api={api}><TeamDraftPublishDialog open onOpenChange={() => undefined} sessionId="session-1" sessionTitle="当前 Session" /></TeamProvider>);

    await screen.findByRole('heading', { name: '发布 Session 固定版本' });
    await user.clear(screen.getByRole('textbox', { name: '固定版本标题' }));
    await user.type(screen.getByRole('textbox', { name: '固定版本标题' }), '客服评测基线');
    await user.click(screen.getByRole('button', { name: '发布固定版本' }));

    await waitFor(() => expect(api.publishProjectDraft).toHaveBeenCalledWith('project-1', {
      sessionId: 'session-1', title: '客服评测基线',
    }, 'csrf-memory'));
    expect(await screen.findByText('固定版本已发布')).toBeVisible();
    expect(screen.getByText(/基线 aaaaaaaaaaaa/)).toBeVisible();
  });

  it('keeps an unavailable worker as an actionable error', async () => {
    const api = fakeApi({ publishProjectDraft: vi.fn().mockRejectedValue(Object.assign(new Error('worker unavailable'), { status: 503 })) });
    const user = userEvent.setup();
    render(<TeamProvider api={api}><TeamDraftPublishDialog open onOpenChange={() => undefined} sessionId="session-1" sessionTitle="当前 Session" /></TeamProvider>);

    await screen.findByRole('heading', { name: '发布 Session 固定版本' });
    await user.click(screen.getByRole('button', { name: '发布固定版本' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('worker unavailable');
    expect(screen.queryByText('固定版本已发布')).not.toBeInTheDocument();
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
