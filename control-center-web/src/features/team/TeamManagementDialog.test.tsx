import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TeamManagementDialog } from './TeamManagementDialog';
import { TeamProvider } from './team-context';
import type { TeamApi } from './team-api';
import type { TeamSession } from './types';

const session: TeamSession = {
  user: { id: 'user-1', username: 'alice', displayName: 'Alice', role: 'admin', active: true },
  csrfToken: 'csrf-memory',
  spaces: [{ id: 'project-1', kind: 'project', name: 'PAW Team', role: 'maintainer', revision: 3 }],
};

afterEach(() => cleanup());

describe('TeamManagementDialog', () => {
  it('loads account and project membership through the typed Team API', async () => {
    const api = {
      status: vi.fn().mockResolvedValue({ enabled: true, name: 'PAW Team' }),
      me: vi.fn().mockResolvedValue(session),
      listMembers: vi.fn().mockResolvedValue([session.user]),
      listDirectory: vi.fn().mockResolvedValue([{ id: session.user.id, username: session.user.username, displayName: session.user.displayName }]),
      listProjectMembers: vi.fn().mockResolvedValue([]),
      listProjectDrafts: vi.fn().mockResolvedValue([]),
      listProjectSessions: vi.fn().mockResolvedValue([]),
    } as unknown as TeamApi;
    render(<TeamProvider api={api}><TeamManagementDialog onOpenChange={() => undefined} open /></TeamProvider>);

    expect(await screen.findByRole('heading', { name: '管理团队与项目成员' })).toBeInTheDocument();
    await waitFor(() => {
      expect(api.listMembers).toHaveBeenCalledWith('csrf-memory');
      expect(api.listDirectory).toHaveBeenCalledWith('csrf-memory');
      expect(api.listProjectMembers).toHaveBeenCalledWith('project-1', 'csrf-memory');
      expect(api.listProjectDrafts).toHaveBeenCalledWith('project-1', 'csrf-memory');
    });
    expect(await screen.findByRole('option', { name: 'Alice · alice' })).toBeVisible();
  });

  it('shows shared drafts and renders the maintainer integration result', async () => {
    const draft = {
      draftId: 'draft-1',
      sessionId: 'session-1',
      title: '固定版本一',
      creatorUserId: 'user-2',
      baseCommit: 'a'.repeat(40),
      draftCommit: 'b'.repeat(40),
      manifestHash: 'c'.repeat(64),
      createdAtMs: 123,
      status: 'pending',
      requirementsRevision: 1,
      currentRequirementsRevision: 2,
      requirementsStale: true,
    };
    const api = {
      status: vi.fn().mockResolvedValue({ enabled: true, name: 'PAW Team' }),
      me: vi.fn().mockResolvedValue(session),
      listMembers: vi.fn().mockResolvedValue([session.user]),
      listDirectory: vi.fn().mockResolvedValue([{ id: session.user.id, username: session.user.username, displayName: session.user.displayName }]),
      listProjectMembers: vi.fn().mockResolvedValue([]),
      listProjectDrafts: vi.fn().mockResolvedValue([draft]),
      listProjectSessions: vi.fn().mockResolvedValue([{
        id: 'session-target',
        title: '我的任务',
        status: 'idle',
        updatedAtMs: 123,
        ownerUserId: session.user.id,
        canControl: true,
        roomParticipant: { roomId: 'shared-room', participantId: 'my-partner', status: 'active' },
      }]),
      getProjectDraftDiff: vi.fn().mockResolvedValue({
        draftId: draft.draftId,
        baseCommit: draft.baseCommit,
        draftCommit: draft.draftCommit,
        diff: 'diff --git a/readme b/readme\n+change\n',
        truncated: false,
      }),
      integrateProjectDraft: vi.fn().mockResolvedValue({ status: 'integrated', headCommit: 'd'.repeat(40) }),
      adoptProjectDraft: vi.fn().mockResolvedValue({
        status: 'conflict',
        targetSessionId: 'session-target',
        message: '目标 Session 有冲突，现有文件保持不变。',
      }),
    } as unknown as TeamApi;
    const user = userEvent.setup();
    render(<TeamProvider api={api}><TeamManagementDialog onOpenChange={() => undefined} open /></TeamProvider>);

    expect(await screen.findByText('固定版本一')).toBeVisible();
    await user.click(screen.getByRole('button', { name: '查看差异' }));
    await waitFor(() => expect(document.querySelector('.team-management-dialog__diff pre')).toHaveTextContent('+change'));
    await user.click(screen.getByRole('button', { name: '集成' }));
    await waitFor(() => expect(api.integrateProjectDraft).toHaveBeenCalledWith('project-1', 'draft-1', 'csrf-memory'));
    expect(await screen.findByText(/固定版本已集成到项目分支/)).toBeVisible();
    expect(screen.getByText(/HEAD dddddddddddd/)).toBeVisible();
    expect(screen.getByText(/基于需求 v1 · 当前 v2/)).toBeVisible();
    await user.click(screen.getByRole('button', { name: '采纳到我的任务' }));
    await waitFor(() => expect(api.adoptProjectDraft).toHaveBeenCalledWith('project-1', 'draft-1', 'session-target', 'csrf-memory'));
    expect(await screen.findByText(/目标 Session 有冲突/)).toBeVisible();
  });

  it('does not let a slower previous project load repaint the selected project', async () => {
    const firstProjectDrafts = deferred<unknown[]>();
    const secondProjectDraft = {
      draftId: 'draft-2',
      sessionId: 'session-2',
      title: '项目二固定版本',
      creatorUserId: 'user-2',
      baseCommit: 'a'.repeat(40),
      draftCommit: 'b'.repeat(40),
      manifestHash: 'c'.repeat(64),
      createdAtMs: 123,
      status: 'pending',
    };
    const multiProjectSession: TeamSession = {
      ...session,
      spaces: [
        { ...session.spaces[0], id: 'project-1', name: '项目一' },
        { ...session.spaces[0], id: 'project-2', name: '项目二' },
      ],
    };
    const api = {
      status: vi.fn().mockResolvedValue({ enabled: true, name: 'PAW Team' }),
      me: vi.fn().mockResolvedValue(multiProjectSession),
      listMembers: vi.fn().mockResolvedValue([session.user]),
      listDirectory: vi.fn().mockResolvedValue([]),
      listProjectMembers: vi.fn().mockResolvedValue([]),
      listProjectDrafts: vi.fn((spaceId: string) => spaceId === 'project-1'
        ? firstProjectDrafts.promise
        : Promise.resolve([secondProjectDraft])),
      listProjectSessions: vi.fn().mockResolvedValue([]),
    } as unknown as TeamApi;
    const user = userEvent.setup();
    render(<TeamProvider api={api}><TeamManagementDialog onOpenChange={() => undefined} open /></TeamProvider>);

    await waitFor(() => expect(api.listProjectDrafts).toHaveBeenCalledWith('project-1', 'csrf-memory'));
    await user.selectOptions(screen.getByRole('combobox', { name: '选择固定版本项目' }), 'project-2');
    expect(await screen.findByText('项目二固定版本')).toBeVisible();

    firstProjectDrafts.resolve([{
      ...secondProjectDraft,
      draftId: 'draft-1',
      title: '项目一固定版本',
    }]);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText('项目一固定版本')).not.toBeInTheDocument();
    expect(screen.getByText('项目二固定版本')).toBeVisible();
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => {
    resolve = nextResolve;
  });
  return { promise, resolve };
}
