import { afterEach, describe, expect, it, vi } from 'vitest';
import { TeamApi } from './team-api';

const user = {
  id: 'user-1',
  username: 'alice',
  displayName: 'Alice',
  role: 'admin',
  active: true,
};
const spaces = [
  { id: 'personal-1', kind: 'personal', name: 'Alice 的空间', role: 'owner', revision: 1 },
  { id: 'project-1', kind: 'project', name: 'PAW Team', role: 'maintainer', revision: 3 },
];

afterEach(() => vi.restoreAllMocks());

describe('TeamApi', () => {
  it('uses same-origin cookies and keeps the CSRF token in request headers', async () => {
    const responses = [
      json({ enabled: true, name: 'PAW Team' }),
      json({ user, csrfToken: 'csrf-memory', spaces }),
      json({ ok: true }),
    ];
    const calls: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ input, init });
      const response = responses.shift();
      if (!response) throw new Error('unexpected request');
      return response;
    });
    const api = new TeamApi({ baseUrl: window.location.origin, fetch: fetchMock as typeof fetch });

    await expect(api.status()).resolves.toEqual({ enabled: true, name: 'PAW Team' });
    const session = await api.login('alice', 'password-123');
    await api.logout(session.csrfToken);

    expect(String(calls[0]?.input)).toBe(`${window.location.origin}/api/team/status`);
    expect(calls[0]?.init).toMatchObject({ method: 'GET', credentials: 'same-origin' });
    expect(String(calls[1]?.input)).toBe(`${window.location.origin}/api/team/login`);
    expect(calls[1]?.init).toMatchObject({
      method: 'POST',
      credentials: 'same-origin',
      body: JSON.stringify({ username: 'alice', password: 'password-123' }),
    });
    expect(new Headers(calls[1]?.init?.headers).get('X-CSRF-Token')).toBeNull();
    expect(new Headers(calls[2]?.init?.headers).get('X-CSRF-Token')).toBe('csrf-memory');
    expect(calls[2]?.init).toMatchObject({ body: '{}' });
    expect(window.localStorage.getItem('team.csrfToken')).toBeNull();
  });

  it('reads, publishes, withdraws, and selects immutable shared resource versions', async () => {
    const metadata = {
      displayName: '客服助手',
      description: '项目助手 Package',
      publisher: 'PAW',
      source: { kind: 'bundled', label: 'Product bundle' },
      permissions: ['workspace.read'],
      compatibility: {},
      security: { reviewed: true, networkAccess: 'none' },
      installable: true,
      distribution: 'team_staged_source',
      version: '1.2.0',
      manifestName: 'rag-ime-plugin.json',
    };
    const resource = {
      publicationId: 'publication-1',
      packageId: 'support-assistant',
      version: '1.2.0',
      digest: 'a'.repeat(64),
      status: 'published',
      metadata,
      publishedByUserId: user.id,
      publishedAtMs: 123,
      updatedAtMs: 456,
    };
    const catalogEntry = { ...metadata, id: 'support-assistant', packageId: 'support-assistant' };
    const selection = {
      spaceId: 'project-1',
      revision: 4,
      publicationIds: [resource.publicationId],
      items: [resource],
      updatedByUserId: user.id,
      updatedAtMs: 456,
    };
    const snapshot = {
      sessionId: 'session-1',
      spaceId: 'project-1',
      selectionRevision: 4,
      publicationIds: [resource.publicationId],
      items: [resource],
      createdAtMs: 789,
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ ok: true, items: [resource] }))
      .mockResolvedValueOnce(json({ ok: true, items: [catalogEntry] }))
      .mockResolvedValueOnce(json({ ok: true, resource }))
      .mockResolvedValueOnce(json({ ok: true, resource: { ...resource, status: 'withdrawn', updatedAtMs: 999 } }))
      .mockResolvedValueOnce(json(selection))
      .mockResolvedValueOnce(json({ ok: true, selection }))
      .mockResolvedValueOnce(json(snapshot));
    const api = new TeamApi({ fetch: fetchMock as typeof fetch });

    await expect(api.listResources('csrf-memory')).resolves.toEqual([expect.objectContaining({ publicationId: resource.publicationId })]);
    await expect(api.listResourceCatalog('csrf-memory')).resolves.toEqual([expect.objectContaining({ packageId: resource.packageId, version: resource.version })]);
    await expect(api.publishResource(resource.packageId, resource.version, 'csrf-memory')).resolves.toMatchObject({ packageId: resource.packageId });
    await expect(api.setPublishedResourceStatus(resource.publicationId, 'withdrawn', 'csrf-memory')).resolves.toMatchObject({ status: 'withdrawn' });
    await expect(api.getSpaceResourceSelection('project-1', 'csrf-memory')).resolves.toMatchObject({ revision: 4, publicationIds: [resource.publicationId] });
    await expect(api.updateSpaceResourceSelection('project-1', { baseRevision: 4, publicationIds: [resource.publicationId] }, 'csrf-memory')).resolves.toMatchObject({ revision: 4 });
    await expect(api.getSessionResourceSnapshot('project-1', 'session-1', 'csrf-memory')).resolves.toMatchObject({ sessionId: 'session-1', selectionRevision: 4 });

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(`${window.location.origin}/api/team/resources`);
    expect(String(fetchMock.mock.calls[1]?.[0])).toBe(`${window.location.origin}/api/team/resources/catalog`);
    expect(String(fetchMock.mock.calls[2]?.[0])).toBe(`${window.location.origin}/api/team/resources/publish`);
    expect(String(fetchMock.mock.calls[3]?.[0])).toBe(`${window.location.origin}/api/team/resources/publication-1/status`);
    expect(String(fetchMock.mock.calls[4]?.[0])).toBe(`${window.location.origin}/api/team/spaces/project-1/resources`);
    expect(String(fetchMock.mock.calls[5]?.[0])).toBe(`${window.location.origin}/api/team/spaces/project-1/resources`);
    expect(String(fetchMock.mock.calls[6]?.[0])).toBe(`${window.location.origin}/api/team/spaces/project-1/sessions/session-1/resources`);
    expect(fetchMock.mock.calls[2]?.[1]).toMatchObject({ method: 'POST', body: JSON.stringify({ packageId: resource.packageId, version: resource.version }) });
    expect(fetchMock.mock.calls[5]?.[1]).toMatchObject({ method: 'POST', body: JSON.stringify({ baseRevision: 4, publicationIds: [resource.publicationId] }) });
    for (const call of fetchMock.mock.calls.slice(2, 6)) expect(new Headers(call[1]?.headers).get('X-CSRF-Token')).toBe('csrf-memory');
  });

  it('parses project membership roles without confusing them with account roles', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => json({
      items: [{
        ...user,
        role: 'owner',
        spaceId: 'project-1',
        revision: 4,
      }],
    }));
    const api = new TeamApi({ fetch: fetchMock as typeof fetch });

    await expect(api.listProjectMembers('project-1', 'csrf-memory')).resolves.toEqual([{
      id: user.id,
      username: user.username,
      displayName: user.displayName,
      active: true,
      role: 'owner',
    }]);
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-memory');
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(`${window.location.origin}/api/team/projects/project-1/members`);
  });

  it('accepts the TeamGateway member envelope when adding a project member', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => json({
      ok: true,
      member: {
        ...user,
        role: 'contributor',
      },
    }));
    const api = new TeamApi({ fetch: fetchMock as typeof fetch });

    await expect(api.addProjectMember('project-1', { userId: user.id, role: 'contributor' }, 'csrf-memory'))
      .resolves.toMatchObject({ id: user.id, role: 'contributor' });
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-memory');
  });

  it('surfaces server errors with status and safe message', async () => {
    const fetchMock = vi.fn(async () => json({ error: '账号或密码错误' }, 401));
    const api = new TeamApi({ fetch: fetchMock as typeof fetch });

    await expect(api.me()).rejects.toMatchObject({ status: 401, operation: 'team.me' });
  });

  it('publishes and integrates project drafts through the unscoped Team API', async () => {
    const draft = {
      draftId: 'draft-1',
      sessionId: 'session-1',
      title: '基线',
      creatorUserId: user.id,
      baseCommit: 'a'.repeat(40),
      draftCommit: 'b'.repeat(40),
      manifestHash: 'c'.repeat(64),
      createdAtMs: 123,
      status: 'pending',
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ ok: true, items: [draft] }))
      .mockResolvedValueOnce(json({ ok: true, draft }))
      .mockResolvedValueOnce(json({ ok: true, integration: {
        status: 'integrated', headCommit: 'd'.repeat(40), draftId: draft.draftId,
      } }))
      .mockResolvedValueOnce(json({ ok: true, adoption: {
        status: 'adopted', draftId: draft.draftId, targetSessionId: 'session-target', targetTreeCommit: 'e'.repeat(40),
      } }))
      .mockResolvedValueOnce(json({ ok: true, draftId: draft.draftId, baseCommit: draft.baseCommit, draftCommit: draft.draftCommit, diff: 'diff --git a/a b/a\n+change\n', truncated: false }));
    const api = new TeamApi({ fetch: fetchMock as typeof fetch });

    await expect(api.listProjectDrafts('project-1', 'csrf-memory')).resolves.toEqual([draft]);
    await expect(api.publishProjectDraft('project-1', { sessionId: 'session-1', title: '基线' }, 'csrf-memory')).resolves.toEqual(draft);
    await expect(api.integrateProjectDraft('project-1', draft.draftId, 'csrf-memory')).resolves.toMatchObject({
      status: 'integrated',
      headCommit: 'd'.repeat(40),
    });
    await expect(api.adoptProjectDraft('project-1', draft.draftId, 'session-target', 'csrf-memory')).resolves.toMatchObject({
      status: 'adopted',
      targetSessionId: 'session-target',
    });
    await expect(api.getProjectDraftDiff('project-1', draft.draftId, 'csrf-memory')).resolves.toMatchObject({
      draftId: draft.draftId,
      diff: expect.stringContaining('+change'),
      truncated: false,
    });
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(`${window.location.origin}/api/team/projects/project-1/drafts`);
    expect(String(fetchMock.mock.calls[1]?.[0])).toBe(`${window.location.origin}/api/team/projects/project-1/drafts`);
    expect(String(fetchMock.mock.calls[2]?.[0])).toBe(`${window.location.origin}/api/team/projects/project-1/drafts/draft-1/integrate`);
    expect(String(fetchMock.mock.calls[3]?.[0])).toBe(`${window.location.origin}/api/team/projects/project-1/drafts/draft-1/adopt`);
    expect(String(fetchMock.mock.calls[4]?.[0])).toBe(`${window.location.origin}/api/team/projects/project-1/drafts/draft-1/diff`);
    expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({ body: JSON.stringify({ sessionId: 'session-1', title: '基线' }) });
    expect(fetchMock.mock.calls[2]?.[1]).toMatchObject({ body: '{}' });
    expect(fetchMock.mock.calls[3]?.[1]).toMatchObject({ body: JSON.stringify({ sessionId: 'session-target' }) });
    expect(new Headers(fetchMock.mock.calls[2]?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-memory');
    expect(new Headers(fetchMock.mock.calls[3]?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-memory');
  });

  it('lists project Sessions through the space-qualified control API', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ items: [
      { id: 'session-target', title: '我的任务', status: 'idle', updatedAtMs: 123, ownerUserId: user.id, canControl: true },
    ] }));
    const api = new TeamApi({ fetch: fetchMock as typeof fetch });

    await expect(api.listProjectSessions('project-1', 'csrf-memory')).resolves.toEqual([
      expect.objectContaining({ id: 'session-target', ownerUserId: user.id, canControl: true }),
    ]);
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(`${window.location.origin}/team/spaces/project-1/api/agent/sessions`);
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-memory');
  });

  it('encodes App history filters in the scoped project Sessions URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ items: [] }));
    const api = new TeamApi({ fetch: fetchMock as typeof fetch });

    await expect(api.listProjectSessions('project-1', 'csrf-memory', {
      surfaceKind: 'extension_app',
      ownerAppId: 'extension:zhanggui-wenshu',
      includeArchived: false,
      limit: 100,
    })).resolves.toEqual([]);

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      `${window.location.origin}/team/spaces/project-1/api/agent/sessions?surfaceKind=extension_app&ownerAppId=extension%3Azhanggui-wenshu&includeArchived=false&limit=100`,
    );
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-memory');
  });

  it('loads the safe active member directory for project invites', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ ok: true, items: [
      { id: 'user-2', username: 'bob', displayName: 'Bob' },
    ] }));
    const api = new TeamApi({ fetch: fetchMock as typeof fetch });

    await expect(api.listDirectory('csrf-memory')).resolves.toEqual([{
      id: 'user-2', username: 'bob', displayName: 'Bob',
    }]);
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(`${window.location.origin}/api/team/directory`);
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-memory');
  });

  it('loads project overview metadata and updates the brief with bounded CAS input', async () => {
    const brief = {
      revision: 1,
      objective: '交付可恢复的注册流程',
      acceptanceCriteria: ['正常注册成功', '重复邮箱有明确提示'],
      updatedAtMs: 123,
      updatedByUserId: user.id,
      updatedByDisplayName: user.displayName,
    };
    const overview = {
      ok: true,
      project: { id: 'project-1', name: '官网', role: 'maintainer' },
      brief,
      briefHistory: [brief],
      members: [{ ...user, role: 'owner' }],
      rooms: [{
        id: 'room-1', title: '注册协作', status: 'active', updatedAtMs: 123, participantCount: 2,
        workItems: [{
          id: 'work-1', roomId: 'room-1', objective: '实现页面', state: 'active',
          currentOwnerParticipantId: 'participant-1', ownerSessionId: 'session-1', ownerRequirementsRevision: 1,
          currentOwnerUserId: user.id, requirementsStale: true,
          currentOwnerDisplayName: user.displayName, expectedOutput: '可运行页面', updatedAtMs: 123,
        }],
      }],
      drafts: [{
        draftId: 'draft-1', sessionId: 'session-1', title: '页面固定版本', creatorUserId: user.id,
        creatorDisplayName: user.displayName, baseCommit: 'a'.repeat(40), draftCommit: 'b'.repeat(40),
        manifestHash: 'c'.repeat(64), createdAtMs: 123, status: 'pending',
        requirementsRevision: 1, currentRequirementsRevision: 2, requirementsStale: true,
      }],
      repository: { branch: 'team/main', headCommit: 'd'.repeat(40), revision: 4 },
      runtime: { configured: false },
      truncated: { rooms: false, workItems: false, drafts: false, briefHistory: false },
    };
    const updatedBrief = { ...brief, revision: 2, objective: '交付可恢复且必须填写公司名称的注册流程' };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json(overview))
      .mockResolvedValueOnce(json({ ok: true, brief: updatedBrief }));
    const api = new TeamApi({ fetch: fetchMock as typeof fetch });

    await expect(api.getProjectOverview('project-1', 'csrf-memory')).resolves.toMatchObject({
      project: { id: 'project-1', name: '官网', role: 'maintainer' },
      brief: { revision: 1, objective: '交付可恢复的注册流程' },
      rooms: [{ workItems: [{ currentOwnerDisplayName: 'Alice', ownerSessionId: 'session-1', ownerRequirementsRevision: 1, requirementsStale: true }] }],
      drafts: [{ creatorDisplayName: 'Alice', requirementsRevision: 1, currentRequirementsRevision: 2, requirementsStale: true }],
      runtime: { configured: false },
    });
    await expect(api.updateProjectBrief('project-1', {
      baseRevision: 1,
      objective: updatedBrief.objective,
      acceptanceCriteria: [...brief.acceptanceCriteria, '公司名称必须填写'],
    }, 'csrf-memory')).resolves.toEqual(updatedBrief);
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(`${window.location.origin}/api/team/projects/project-1/overview`);
    expect(String(fetchMock.mock.calls[1]?.[0])).toBe(`${window.location.origin}/api/team/projects/project-1/brief`);
    expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({
      method: 'POST',
      body: JSON.stringify({
        baseRevision: 1,
        objective: updatedBrief.objective,
        acceptanceCriteria: [...brief.acceptanceCriteria, '公司名称必须填写'],
      }),
    });
    expect(new Headers(fetchMock.mock.calls[1]?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-memory');
  });

  it('loads and mutates the shared preview through CSRF-protected project endpoints', async () => {
    const deployment = {
      id: 'preview-1',
      status: 'ready',
      branch: 'team/main',
      commit: 'e'.repeat(40),
      requirementsRevision: 2,
      requestedByDisplayName: 'Alice',
      createdAtMs: 123,
      readyAtMs: 456,
    };
    const status = {
      ok: true,
      configured: true,
      active: deployment,
      latest: deployment,
      currentRequirementsRevision: 2,
      repositoryHead: 'f'.repeat(40),
      canManage: true,
      openPath: '/api/team/projects/project-1/preview/open',
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json(status))
      .mockResolvedValueOnce(json({ ...status, latest: { ...deployment, status: 'starting' } }, 202))
      .mockResolvedValueOnce(json({ ...status, active: null, latest: { ...deployment, status: 'stopped' } }));
    const api = new TeamApi({ fetch: fetchMock as typeof fetch });

    await expect(api.getProjectPreview('project-1', 'csrf-memory')).resolves.toMatchObject({
      active: { id: 'preview-1', status: 'ready', requirementsRevision: 2 },
      latest: { commit: 'e'.repeat(40) },
      repositoryHead: 'f'.repeat(40),
      openPath: `${window.location.origin}/api/team/projects/project-1/preview/open`,
    });
    await expect(api.startProjectPreview('project-1', 'client-request-1', 'csrf-memory')).resolves.toMatchObject({
      latest: { status: 'starting' },
    });
    await expect(api.stopProjectPreview('project-1', 'csrf-memory')).resolves.toMatchObject({
      active: null,
      latest: { status: 'stopped' },
    });

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(`${window.location.origin}/api/team/projects/project-1/preview`);
    expect(String(fetchMock.mock.calls[1]?.[0])).toBe(`${window.location.origin}/api/team/projects/project-1/preview`);
    expect(String(fetchMock.mock.calls[2]?.[0])).toBe(`${window.location.origin}/api/team/projects/project-1/preview/stop`);
    expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({
      method: 'POST',
      body: JSON.stringify({ clientRequestId: 'client-request-1' }),
    });
    expect(fetchMock.mock.calls[2]?.[1]).toMatchObject({ method: 'POST', body: '{}' });
    expect(new Headers(fetchMock.mock.calls[1]?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-memory');
    expect(new Headers(fetchMock.mock.calls[2]?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-memory');
    expect(api.openProjectPreview('project/1')).toBe(`${window.location.origin}/api/team/projects/project%2F1/preview/open`);
  });

  it('rejects a preview open URL that leaves the authenticated origin', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({
      ok: true,
      configured: true,
      active: null,
      latest: null,
      currentRequirementsRevision: 0,
      repositoryHead: null,
      canManage: false,
      openPath: 'https://preview.example.test/project-1',
    }));
    const api = new TeamApi({ fetch: fetchMock as typeof fetch });

    await expect(api.getProjectPreview('project-1')).rejects.toThrow('unsafe preview open path');
  });

  it('rejects an overlong project brief before making a write request', async () => {
    const fetchMock = vi.fn();
    const api = new TeamApi({ fetch: fetchMock as typeof fetch });

    await expect(api.updateProjectBrief('project-1', {
      baseRevision: 0,
      objective: '目标',
      acceptanceCriteria: Array.from({ length: 21 }, () => '条件'),
    }, 'csrf-memory')).rejects.toThrow('at most 20 items');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('adopts a published brief as the current user Session requirements baseline', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({
      ok: true,
      sessionId: 'session-1',
      requirementsRevision: 2,
      previousRequirementsRevision: 1,
    }));
    const api = new TeamApi({ fetch: fetchMock as typeof fetch });

    await expect(api.adoptSessionRequirements('project-1', 'session-1', {
      baseRevision: 1,
      revision: 2,
    }, 'csrf-memory')).resolves.toEqual({
      sessionId: 'session-1',
      requirementsRevision: 2,
      previousRequirementsRevision: 1,
    });
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(`${window.location.origin}/api/team/projects/project-1/sessions/session-1/requirements`);
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({
      method: 'POST',
      body: JSON.stringify({ baseRevision: 1, revision: 2 }),
    });
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-memory');
  });

  it('scopes GitHub connections and grants to the active space with CSRF writes', async () => {
    const connection = {
      id: 'connection-1', provider: 'github', scope: 'personal', ownerId: user.id,
      label: '代码只读', accountLogin: 'alice', repositories: ['acme/repo'], operations: ['repo.read'],
      status: 'active', revision: 1, canManage: true, createdAtMs: 123,
    };
    const grant = {
      id: 'grant-1', connectionId: connection.id, sessionId: 'session-1', spaceId: 'project-1',
      repository: 'acme/repo', operations: ['repo.read'], expiresAtMs: 456, status: 'active',
      connectionLabel: connection.label, accountLogin: connection.accountLogin,
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ configured: true, oauthAvailable: true, items: [connection], grants: [grant], sessions: [{ id: 'session-1', title: '我的任务', status: 'idle', ownerUserId: user.id }], canCreateProject: true }))
      .mockResolvedValueOnce(json({ ok: true, connection }))
      .mockResolvedValueOnce(json({ ok: true, authorizationUrl: 'https://github.com/login/oauth/authorize?state=opaque&code_challenge=opaque' }))
      .mockResolvedValueOnce(json({ ok: true, connection: { ...connection, status: 'revoked', canManage: true, revision: 2 } }))
      .mockResolvedValueOnce(json({ ok: true, grant }))
      .mockResolvedValueOnce(json({ ok: true, grant: { ...grant, status: 'revoked' } }));
    const api = new TeamApi({ fetch: fetchMock as typeof fetch });

    await expect(api.listConnections('project-1', 'csrf-memory')).resolves.toMatchObject({
      items: [{ id: connection.id, repositories: ['acme/repo'], operations: ['repo.read'] }],
      grants: [{ id: grant.id, sessionId: grant.sessionId }],
      sessions: [{ id: 'session-1', title: '我的任务' }],
    });
    await expect(api.createConnectionWithToken('project-1', {
      scope: 'personal', label: '代码只读', repositories: ['Acme/Repo'], operations: ['repo.read'],
    }, 'ghp_secret', 'csrf-memory')).resolves.toMatchObject({ id: connection.id });
    await expect(api.startConnectionOAuth('project-1', {
      scope: 'project', label: '项目连接', repositories: ['Acme/Repo'], operations: ['repo.read'],
    }, 'csrf-memory')).resolves.toMatchObject({ authorizationUrl: expect.stringContaining('github.com/login/oauth/authorize') });
    await expect(api.revokeConnection('project-1', connection.id, 'csrf-memory')).resolves.toMatchObject({ status: 'revoked' });
    await expect(api.createConnectionGrant('project-1', {
      connectionId: connection.id, sessionId: grant.sessionId, repository: 'Acme/Repo', operations: ['repo.read'], ttlSeconds: 3_600,
    }, 'csrf-memory')).resolves.toMatchObject({ id: grant.id, repository: 'acme/repo' });
    await expect(api.revokeConnectionGrant('project-1', grant.id, 'csrf-memory')).resolves.toMatchObject({ status: 'revoked' });

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(`${window.location.origin}/api/team/spaces/project-1/connections`);
    expect(String(fetchMock.mock.calls[1]?.[0])).toBe(`${window.location.origin}/api/team/spaces/project-1/connections/token`);
    expect(String(fetchMock.mock.calls[2]?.[0])).toBe(`${window.location.origin}/api/team/spaces/project-1/connections/oauth/start`);
    expect(String(fetchMock.mock.calls[3]?.[0])).toBe(`${window.location.origin}/api/team/spaces/project-1/connections/connection-1/revoke`);
    expect(String(fetchMock.mock.calls[4]?.[0])).toBe(`${window.location.origin}/api/team/spaces/project-1/connections/grants`);
    expect(String(fetchMock.mock.calls[5]?.[0])).toBe(`${window.location.origin}/api/team/spaces/project-1/connections/grants/grant-1/revoke`);
    expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({ method: 'POST', body: JSON.stringify({ scope: 'personal', label: '代码只读', repositories: ['acme/repo'], operations: ['repo.read'], token: 'ghp_secret' }) });
    expect(fetchMock.mock.calls[4]?.[1]).toMatchObject({ method: 'POST', body: JSON.stringify({ connectionId: connection.id, sessionId: grant.sessionId, repository: 'acme/repo', operations: ['repo.read'], ttlSeconds: 3_600 }) });
    for (const call of fetchMock.mock.calls.slice(1)) expect(new Headers(call[1]?.headers).get('X-CSRF-Token')).toBe('csrf-memory');
    expect(window.localStorage.getItem('ghp_secret')).toBeNull();
  });

  it('rejects unsafe OAuth destinations and invalid grant bounds before network writes', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ ok: true, authorizationUrl: 'https://evil.example.test/login/oauth/authorize' }));
    const api = new TeamApi({ fetch: fetchMock as typeof fetch });

    await expect(api.startConnectionOAuth('project-1', {
      scope: 'personal', label: '只读', repositories: ['acme/repo'], operations: ['repo.read'],
    }, 'csrf-memory')).rejects.toThrow('unsafe OAuth URL');
    await expect(api.createConnectionGrant('project-1', {
      connectionId: 'connection-1', sessionId: 'session-1', repository: 'acme/repo', operations: ['repo.read'], ttlSeconds: 59,
    }, 'csrf-memory')).rejects.toThrow('between 60 and 86400');
    expect(fetchMock).toHaveBeenCalledOnce();
  });
});

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}
