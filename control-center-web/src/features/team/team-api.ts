import { sessionItems, type SessionSummary } from '@/features/agent/types';
import { TEAM_GITHUB_OPERATIONS } from './types';
import type {
  TeamMemberInput,
  TeamProjectBrief,
  TeamProjectBriefUpdateInput,
  TeamProjectDraft,
  TeamProjectDiff,
  TeamProjectAdoption,
  TeamProjectIntegration,
  TeamProjectMember,
  TeamProjectMemberInput,
  TeamProjectOverview,
  TeamProjectRepository,
  TeamProjectRoom,
  TeamProjectRoomWorkItem,
  TeamProjectRuntime,
  TeamProjectOverviewTruncated,
  TeamProjectPreview,
  TeamProjectDeployment,
  TeamProjectPreviewStatus,
  TeamProjectSession,
  TeamResourceCatalogEntry,
  TeamResourceMetadata,
  TeamResourceSelection,
  TeamResourceSelectionInput,
  TeamPublishedResource,
  TeamPublishedResourceStatus,
  TeamSessionRequirementsInput,
  TeamSessionRequirementsResult,
  TeamSessionResourceSnapshot,
  TeamSession,
  TeamSpace,
  TeamStatus,
  TeamConnection,
  TeamConnectionCreateInput,
  TeamConnectionGrant,
  TeamConnectionGrantInput,
  TeamConnectionList,
  TeamConnectionSession,
  TeamConnectionStatus,
  TeamGrantStatus,
  TeamDirectoryUser,
  TeamUser,
} from './types';

export interface TeamApiOptions {
  baseUrl?: string;
  fetch?: typeof fetch;
}

export interface TeamProjectSessionListOptions {
  surfaceKind?: string;
  ownerAppId?: string;
  includeArchived?: boolean;
  limit?: number;
}

export class TeamApiError extends Error {
  readonly operation: string;
  readonly status: number;
  readonly payload: unknown;

  constructor(operation: string, status: number, payload: unknown) {
    const serverMessage = isRecord(payload) && typeof payload.error === 'string'
      ? payload.error
      : isRecord(payload) && typeof payload.message === 'string'
        ? payload.message
        : `${operation} 请求失败（HTTP ${status}）`;
    super(serverMessage);
    this.name = 'TeamApiError';
    this.operation = operation;
    this.status = status;
    this.payload = payload;
  }
}

export class TeamApi {
  private readonly baseUrl: URL;
  private readonly fetchImpl: typeof fetch;

  constructor(options: TeamApiOptions = {}) {
    this.baseUrl = new URL(options.baseUrl ?? globalThis.location?.origin ?? 'http://127.0.0.1/');
    if (this.baseUrl.protocol !== 'http:' && this.baseUrl.protocol !== 'https:') {
      throw new TypeError('TeamApi baseUrl must use HTTP or HTTPS');
    }
    if (this.baseUrl.username || this.baseUrl.password || this.baseUrl.search || this.baseUrl.hash) {
      throw new TypeError('TeamApi baseUrl must not contain credentials, query, or hash');
    }
    this.fetchImpl = options.fetch ?? globalThis.fetch.bind(globalThis);
  }

  async status(): Promise<TeamStatus> {
    const payload = await this.json('/api/team/status', { method: 'GET' }, 'team.status');
    if (!isRecord(payload) || payload.enabled !== true || typeof payload.name !== 'string' || !payload.name.trim()) {
      throw new TypeError('TeamGateway status returned an invalid response');
    }
    return { enabled: true, name: payload.name };
  }

  async listResources(csrfToken?: string): Promise<TeamPublishedResource[]> {
    const payload = await this.json('/api/team/resources', { method: 'GET' }, 'team.resources.list', csrfToken);
    if (!isRecord(payload) || !Array.isArray(payload.items)) {
      throw new TypeError('TeamGateway published resources returned an invalid response');
    }
    return payload.items.map((item) => parsePublishedResource(item, 'team.resources.list'));
  }

  async listResourceCatalog(csrfToken?: string): Promise<TeamResourceCatalogEntry[]> {
    const payload = await this.json('/api/team/resources/catalog', { method: 'GET' }, 'team.resources.catalog', csrfToken);
    if (!isRecord(payload) || !Array.isArray(payload.items)) {
      throw new TypeError('TeamGateway resource catalog returned an invalid response');
    }
    return payload.items.map((item) => parseResourceCatalogEntry(item, 'team.resources.catalog'));
  }

  async publishResource(packageId: string, version: string, csrfToken: string): Promise<TeamPublishedResource> {
    const payload = await this.json(
      '/api/team/resources/publish',
      { method: 'POST', body: JSON.stringify({ packageId: assertId(packageId, 'packageId'), version: assertId(version, 'version') }) },
      'team.resources.publish',
      csrfToken,
    );
    if (!isRecord(payload)) throw new TypeError('TeamGateway resource publication returned an invalid response');
    return parsePublishedResource(payload.resource, 'team.resources.publish');
  }

  async setPublishedResourceStatus(
    publicationId: string,
    status: TeamPublishedResourceStatus,
    csrfToken: string,
  ): Promise<TeamPublishedResource> {
    if (status !== 'published' && status !== 'withdrawn') {
      throw new TypeError('TeamGateway resource publication status is invalid');
    }
    const payload = await this.json(
      `/api/team/resources/${encodeURIComponent(assertId(publicationId, 'publicationId'))}/status`,
      { method: 'POST', body: JSON.stringify({ status }) },
      'team.resources.status',
      csrfToken,
    );
    if (!isRecord(payload)) throw new TypeError('TeamGateway resource status returned an invalid response');
    return parsePublishedResource(payload.resource, 'team.resources.status');
  }

  async getSpaceResourceSelection(spaceId: string, csrfToken?: string): Promise<TeamResourceSelection> {
    const payload = await this.json(
      `/api/team/spaces/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/resources`,
      { method: 'GET' },
      'team.space-resources.get',
      csrfToken,
    );
    return parseResourceSelection(payload, 'team.space-resources.get');
  }

  async updateSpaceResourceSelection(
    spaceId: string,
    input: TeamResourceSelectionInput,
    csrfToken: string,
  ): Promise<TeamResourceSelection> {
    validateResourceSelectionInput(input);
    const payload = await this.json(
      `/api/team/spaces/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/resources`,
      { method: 'POST', body: JSON.stringify(input) },
      'team.space-resources.update',
      csrfToken,
    );
    return parseResourceSelection(payload, 'team.space-resources.update');
  }

  async getSessionResourceSnapshot(
    spaceId: string,
    sessionId: string,
    csrfToken?: string,
  ): Promise<TeamSessionResourceSnapshot> {
    const payload = await this.json(
      `/api/team/spaces/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/sessions/${encodeURIComponent(assertId(sessionId, 'sessionId'))}/resources`,
      { method: 'GET' },
      'team.session-resources.get',
      csrfToken,
    );
    return parseSessionResourceSnapshot(payload, 'team.session-resources.get');
  }

  async login(username: string, password: string): Promise<TeamSession> {
    const payload = await this.json(
      '/api/team/login',
      { method: 'POST', body: JSON.stringify({ username, password }) },
      'team.login',
    );
    return parseTeamSession(payload, 'team.login');
  }

  async me(): Promise<TeamSession> {
    const payload = await this.json('/api/team/me', { method: 'GET' }, 'team.me');
    return parseTeamSession(payload, 'team.me');
  }

  async logout(csrfToken?: string): Promise<{ ok: true }> {
    const payload = await this.json(
      '/api/team/logout',
      { method: 'POST', body: JSON.stringify({}) },
      'team.logout',
      csrfToken,
    );
    if (!isRecord(payload) || payload.ok !== true) throw new TypeError('TeamGateway logout returned an invalid response');
    return { ok: true };
  }

  async createProject(name: string, csrfToken: string): Promise<TeamSpace> {
    const payload = await this.json(
      '/api/team/projects',
      { method: 'POST', body: JSON.stringify({ name }) },
      'team.projects.create',
      csrfToken,
    );
    if (!isRecord(payload)) throw new TypeError('TeamGateway project creation returned an invalid response');
    return parseSpace(payload.space, 'team.projects.create');
  }

  async listMembers(csrfToken?: string): Promise<TeamUser[]> {
    const payload = await this.json('/api/team/members', { method: 'GET' }, 'team.members.list', csrfToken);
    if (!isRecord(payload) || !Array.isArray(payload.items)) {
      throw new TypeError('TeamGateway members returned an invalid response');
    }
    return payload.items.map((item) => parseUser(item, 'team.members.list'));
  }

  async listDirectory(csrfToken?: string): Promise<TeamDirectoryUser[]> {
    const payload = await this.json('/api/team/directory', { method: 'GET' }, 'team.directory.list', csrfToken);
    if (!isRecord(payload) || payload.ok !== true || !Array.isArray(payload.items)) {
      throw new TypeError('TeamGateway directory returned an invalid response');
    }
    return payload.items.map((item) => parseDirectoryUser(item, 'team.directory.list'));
  }

  async createMember(input: TeamMemberInput, csrfToken: string): Promise<TeamUser> {
    const payload = await this.json(
      '/api/team/members',
      { method: 'POST', body: JSON.stringify(input) },
      'team.members.create',
      csrfToken,
    );
    if (!isRecord(payload)) throw new TypeError('TeamGateway member creation returned an invalid response');
    return parseUser(payload.user, 'team.members.create');
  }

  async setMemberStatus(userId: string, active: boolean, csrfToken: string): Promise<TeamUser> {
    const payload = await this.json(
      `/api/team/members/${encodeURIComponent(assertId(userId, 'userId'))}/status`,
      { method: 'POST', body: JSON.stringify({ active }) },
      'team.members.status',
      csrfToken,
    );
    if (!isRecord(payload)) throw new TypeError('TeamGateway member status returned an invalid response');
    return parseUser(payload.user, 'team.members.status');
  }

  async listProjectMembers(spaceId: string, csrfToken?: string): Promise<TeamProjectMember[]> {
    const payload = await this.json(
      `/api/team/projects/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/members`,
      { method: 'GET' },
      'team.project-members.list',
      csrfToken,
    );
    if (!isRecord(payload) || !Array.isArray(payload.items)) {
      throw new TypeError('TeamGateway project members returned an invalid response');
    }
    return payload.items.map((item) => parseProjectMember(item, 'team.project-members.list'));
  }

  async addProjectMember(spaceId: string, input: TeamProjectMemberInput, csrfToken: string): Promise<TeamProjectMember> {
    const payload = await this.json(
      `/api/team/projects/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/members`,
      { method: 'POST', body: JSON.stringify(input) },
      'team.project-members.add',
      csrfToken,
    );
    if (!isRecord(payload)) throw new TypeError('TeamGateway project member creation returned an invalid response');
    // TeamGateway returns the project-scoped role under `member`; accept the
    // older `user` envelope as a compatibility bridge for early gateways.
    return parseProjectMember(payload.member ?? payload.user, 'team.project-members.add');
  }

  async removeProjectMember(spaceId: string, userId: string, csrfToken: string): Promise<{ ok: true }> {
    const payload = await this.json(
      `/api/team/projects/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/members/${encodeURIComponent(assertId(userId, 'userId'))}`,
      { method: 'DELETE' },
      'team.project-members.remove',
      csrfToken,
    );
    if (!isRecord(payload) || payload.ok !== true) {
      throw new TypeError('TeamGateway project member removal returned an invalid response');
    }
    return { ok: true };
  }

  async listConnections(spaceId: string, csrfToken?: string): Promise<TeamConnectionList> {
    const payload = await this.json(
      projectConnectionsPath(spaceId),
      { method: 'GET' },
      'team.connections.list',
      csrfToken,
    );
    return parseConnectionList(payload, 'team.connections.list');
  }

  async createConnectionWithToken(
    spaceId: string,
    input: TeamConnectionCreateInput,
    token: string,
    csrfToken: string,
  ): Promise<TeamConnection> {
    const normalized = validateConnectionCreateInput(input);
    validateConnectionToken(token);
    const payload = await this.json(
      `${projectConnectionsPath(spaceId)}/token`,
      { method: 'POST', body: JSON.stringify({ ...normalized, token }) },
      'team.connections.token.create',
      csrfToken,
    );
    if (!isRecord(payload)) throw new TypeError('TeamGateway token connection returned an invalid response');
    return parseConnection(payload.connection, 'team.connections.token.create');
  }

  async startConnectionOAuth(
    spaceId: string,
    input: TeamConnectionCreateInput,
    csrfToken: string,
  ): Promise<{ authorizationUrl: string }> {
    const normalized = validateConnectionCreateInput(input);
    const payload = await this.json(
      `${projectConnectionsPath(spaceId)}/oauth/start`,
      { method: 'POST', body: JSON.stringify(normalized) },
      'team.connections.oauth.start',
      csrfToken,
    );
    if (!isRecord(payload)) throw new TypeError('TeamGateway OAuth start returned an invalid response');
    const authorizationUrl = parseGitHubAuthorizationUrl(payload.authorizationUrl, 'team.connections.oauth.start');
    return { authorizationUrl };
  }

  async revokeConnection(spaceId: string, connectionId: string, csrfToken: string): Promise<TeamConnection> {
    const payload = await this.json(
      `${projectConnectionsPath(spaceId)}/${encodeURIComponent(assertId(connectionId, 'connectionId'))}/revoke`,
      { method: 'POST', body: JSON.stringify({}) },
      'team.connections.revoke',
      csrfToken,
    );
    if (!isRecord(payload)) throw new TypeError('TeamGateway connection revoke returned an invalid response');
    return parseConnection(payload.connection, 'team.connections.revoke');
  }

  async createConnectionGrant(
    spaceId: string,
    input: TeamConnectionGrantInput,
    csrfToken: string,
  ): Promise<TeamConnectionGrant> {
    const normalized = validateConnectionGrantInput(input);
    const payload = await this.json(
      `${projectConnectionsPath(spaceId)}/grants`,
      { method: 'POST', body: JSON.stringify(normalized) },
      'team.connection-grants.create',
      csrfToken,
    );
    if (!isRecord(payload)) throw new TypeError('TeamGateway connection grant returned an invalid response');
    return parseConnectionGrant(payload.grant, 'team.connection-grants.create');
  }

  async revokeConnectionGrant(spaceId: string, grantId: string, csrfToken: string): Promise<TeamConnectionGrant> {
    const payload = await this.json(
      `${projectConnectionsPath(spaceId)}/grants/${encodeURIComponent(assertId(grantId, 'grantId'))}/revoke`,
      { method: 'POST', body: JSON.stringify({}) },
      'team.connection-grants.revoke',
      csrfToken,
    );
    if (!isRecord(payload)) throw new TypeError('TeamGateway connection grant revoke returned an invalid response');
    return parseConnectionGrant(payload.grant, 'team.connection-grants.revoke');
  }

  async getProjectOverview(spaceId: string, csrfToken?: string): Promise<TeamProjectOverview> {
    const payload = await this.json(
      `/api/team/projects/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/overview`,
      { method: 'GET' },
      'team.project-overview.get',
      csrfToken,
    );
    return parseProjectOverview(payload, 'team.project-overview.get');
  }

  async getProjectPreview(spaceId: string, csrfToken?: string): Promise<TeamProjectPreview> {
    const payload = await this.json(
      `/api/team/projects/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/preview`,
      { method: 'GET' },
      'team.project-preview.get',
      csrfToken,
    );
    return parseProjectPreview(payload, 'team.project-preview.get', this.baseUrl);
  }

  async startProjectPreview(spaceId: string, clientRequestId: string, csrfToken: string): Promise<TeamProjectPreview> {
    const payload = await this.json(
      `/api/team/projects/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/preview`,
      {
        method: 'POST',
        body: JSON.stringify({ clientRequestId: assertId(clientRequestId, 'clientRequestId') }),
      },
      'team.project-preview.start',
      csrfToken,
    );
    return parseProjectPreview(payload, 'team.project-preview.start', this.baseUrl);
  }

  async stopProjectPreview(spaceId: string, csrfToken: string): Promise<TeamProjectPreview> {
    const payload = await this.json(
      `/api/team/projects/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/preview/stop`,
      { method: 'POST', body: JSON.stringify({}) },
      'team.project-preview.stop',
      csrfToken,
    );
    return parseProjectPreview(payload, 'team.project-preview.stop', this.baseUrl);
  }

  /** URL for the same-origin preview handoff endpoint. The browser follows its 302. */
  openProjectPreview(spaceId: string): string {
    return new URL(
      `/api/team/projects/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/preview/open`,
      this.baseUrl,
    ).toString();
  }

  async updateProjectBrief(
    spaceId: string,
    input: TeamProjectBriefUpdateInput,
    csrfToken: string,
  ): Promise<TeamProjectBrief> {
    validateProjectBriefUpdate(input);
    const payload = await this.json(
      `/api/team/projects/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/brief`,
      { method: 'POST', body: JSON.stringify(input) },
      'team.project-brief.update',
      csrfToken,
    );
    if (!isRecord(payload)) throw new TypeError('TeamGateway project brief returned an invalid response');
    return parseProjectBrief(payload.brief, 'team.project-brief.update');
  }

  async listProjectDrafts(spaceId: string, csrfToken?: string): Promise<TeamProjectDraft[]> {
    const payload = await this.json(
      `/api/team/projects/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/drafts`,
      { method: 'GET' },
      'team.project-drafts.list',
      csrfToken,
    );
    if (!isRecord(payload) || !Array.isArray(payload.items)) {
      throw new TypeError('TeamGateway project drafts returned an invalid response');
    }
    return payload.items.map((item) => parseProjectDraft(item, 'team.project-drafts.list'));
  }

  async listProjectSessions(
    spaceId: string,
    csrfToken?: string,
    options?: TeamProjectSessionListOptions,
  ): Promise<TeamProjectSession[]> {
    const path = `/team/spaces/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/api/agent/sessions`;
    const query = new URLSearchParams();
    if (options?.surfaceKind) query.set('surfaceKind', options.surfaceKind);
    if (options?.ownerAppId) query.set('ownerAppId', options.ownerAppId);
    if (options?.includeArchived !== undefined) query.set('includeArchived', String(options.includeArchived));
    if (options?.limit !== undefined) query.set('limit', String(options.limit));
    const payload = await this.json(
      query.size > 0 ? `${path}?${query.toString()}` : path,
      { method: 'GET' },
      'team.project-sessions.list',
      csrfToken,
    );
    if (!isRecord(payload) || (!Array.isArray(payload.items) && !Array.isArray(payload.sessions))) {
      throw new TypeError('TeamGateway project sessions returned an invalid response');
    }
    return sessionItems(payload, { includeAppOwned: true }).map((session) => parseProjectSession(session));
  }

  async adoptSessionRequirements(
    spaceId: string,
    sessionId: string,
    input: TeamSessionRequirementsInput,
    csrfToken: string,
  ): Promise<TeamSessionRequirementsResult> {
    validateSessionRequirementsInput(input);
    const normalizedSessionId = assertId(sessionId, 'sessionId');
    const payload = await this.json(
      `/api/team/projects/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/sessions/${encodeURIComponent(normalizedSessionId)}/requirements`,
      {
        method: 'POST',
        body: JSON.stringify({ baseRevision: input.baseRevision, revision: input.revision }),
      },
      'team.project-session.requirements.update',
      csrfToken,
    );
    if (!isRecord(payload) || payload.ok !== true) {
      throw new TypeError('TeamGateway Session requirements returned an invalid response');
    }
    const returnedSessionId = boundedString(payload.sessionId);
    const requirementsRevision = nonNegativeInteger(payload.requirementsRevision);
    const previousRequirementsRevision = nonNegativeInteger(payload.previousRequirementsRevision);
    if (
      !returnedSessionId
      || returnedSessionId !== normalizedSessionId
      || requirementsRevision === null
      || previousRequirementsRevision === null
    ) {
      throw new TypeError('TeamGateway Session requirements returned an invalid response');
    }
    return { sessionId: returnedSessionId, requirementsRevision, previousRequirementsRevision };
  }

  async publishProjectDraft(
    spaceId: string,
    input: { sessionId: string; title: string },
    csrfToken: string,
  ): Promise<TeamProjectDraft> {
    const payload = await this.json(
      `/api/team/projects/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/drafts`,
      { method: 'POST', body: JSON.stringify(input) },
      'team.project-drafts.publish',
      csrfToken,
    );
    if (!isRecord(payload)) throw new TypeError('TeamGateway project draft creation returned an invalid response');
    return parseProjectDraft(payload.draft, 'team.project-drafts.publish');
  }

  async integrateProjectDraft(
    spaceId: string,
    draftId: string,
    csrfToken: string,
  ): Promise<TeamProjectIntegration> {
    const payload = await this.json(
      `/api/team/projects/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/drafts/${encodeURIComponent(assertId(draftId, 'draftId'))}/integrate`,
      { method: 'POST', body: JSON.stringify({}) },
      'team.project-drafts.integrate',
      csrfToken,
    );
    if (!isRecord(payload)) throw new TypeError('TeamGateway draft integration returned an invalid response');
    return parseProjectIntegration(payload.integration, 'team.project-drafts.integrate');
  }

  async adoptProjectDraft(
    spaceId: string,
    draftId: string,
    sessionId: string,
    csrfToken: string,
  ): Promise<TeamProjectAdoption> {
    const payload = await this.json(
      `/api/team/projects/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/drafts/${encodeURIComponent(assertId(draftId, 'draftId'))}/adopt`,
      { method: 'POST', body: JSON.stringify({ sessionId: assertId(sessionId, 'sessionId') }) },
      'team.project-drafts.adopt',
      csrfToken,
    );
    if (!isRecord(payload)) throw new TypeError('TeamGateway draft adoption returned an invalid response');
    return parseProjectAdoption(payload.adoption, 'team.project-drafts.adopt');
  }

  async getProjectDraftDiff(spaceId: string, draftId: string, csrfToken?: string): Promise<TeamProjectDiff> {
    const payload = await this.json(
      `/api/team/projects/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/drafts/${encodeURIComponent(assertId(draftId, 'draftId'))}/diff`,
      { method: 'GET' },
      'team.project-drafts.diff',
      csrfToken,
    );
    return parseProjectDiff(payload, 'team.project-drafts.diff');
  }

  private async json(
    path: string,
    init: RequestInit,
    operation: string,
    csrfToken?: string,
  ): Promise<unknown> {
    const headers = new Headers(init.headers);
    headers.set('Accept', 'application/json');
    if (init.body !== undefined) headers.set('Content-Type', 'application/json');
    if (csrfToken) headers.set('X-CSRF-Token', csrfToken);
    const response = await this.fetchImpl(new URL(path, this.baseUrl), {
      ...init,
      credentials: 'same-origin',
      headers,
    });
    const payload = await readPayload(response);
    if (!response.ok) throw new TeamApiError(operation, response.status, payload);
    return payload;
  }
}

export function teamApiErrorMessage(error: unknown, fallback = '团队服务暂时不可用，请稍后重试。'): string {
  if (error instanceof TeamApiError) {
    const code = isRecord(error.payload) ? String(error.payload.errorCode ?? '') : '';
    const connectionMessages: Record<string, string> = {
      connection_reconnect_required: 'GitHub 授权已失效，请重新连接这个外部账号。',
      connection_provider_rejected: 'GitHub 拒绝了这次请求，请检查仓库范围和账号权限。',
      connection_read_only: '所选任务为只读任务，请去掉创建和评论操作。',
      connection_task_policy_changed: '任务权限已变化，请重新检查任务授权。',
      connection_outcome_unknown: '外部操作的结果尚不能确定，请先到 GitHub 核实结果。',
      connections_unavailable: '团队尚未配置外部连接服务。',
    };
    if (connectionMessages[code]) return connectionMessages[code];
    if (error.status === 401) return '登录状态已失效，请重新登录。';
    if (error.status === 403) return '当前账号没有执行这项操作的权限。';
    if (error.status === 404) return '团队服务没有找到目标空间或成员。';
    return error.message.replaceAll('TeamGateway', '团队服务');
  }
  if (error instanceof Error && error.message) return error.message.replaceAll('TeamGateway', '团队服务');
  return fallback;
}

function parseTeamSession(value: unknown, operation: string): TeamSession {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid response`);
  const user = parseUser(value.user, operation);
  const csrfToken = typeof value.csrfToken === 'string' && value.csrfToken.length > 0 ? value.csrfToken : '';
  if (!csrfToken || !Array.isArray(value.spaces)) throw new TypeError(`TeamGateway ${operation} returned an invalid session`);
  return {
    user,
    csrfToken,
    spaces: value.spaces.map((space) => parseSpace(space, operation)),
  };
}

function parseUser(value: unknown, operation: string): TeamUser {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid user`);
  const id = boundedString(value.id);
  const username = boundedString(value.username);
  const displayName = typeof value.displayName === 'string' && value.displayName.trim() ? value.displayName.trim() : username;
  if (!id || !username || !['admin', 'member'].includes(String(value.role)) || typeof value.active !== 'boolean') {
    throw new TypeError(`TeamGateway ${operation} returned an invalid user`);
  }
  return {
    id,
    username,
    displayName,
    role: value.role as TeamUser['role'],
    active: value.active,
  };
}

function parseDirectoryUser(value: unknown, operation: string): TeamDirectoryUser {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid directory user`);
  const id = boundedString(value.id);
  const username = boundedString(value.username);
  const displayName = typeof value.displayName === 'string' && value.displayName.trim()
    ? value.displayName.trim()
    : username;
  if (!id || !username || !displayName) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid directory user`);
  }
  return { id, username, displayName };
}

function parseSpace(value: unknown, operation: string): TeamSpace {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid space`);
  const id = boundedString(value.id);
  const name = boundedString(value.name);
  if (
    !id ||
    !name ||
    !['personal', 'project'].includes(String(value.kind)) ||
    !['owner', 'maintainer', 'contributor', 'viewer'].includes(String(value.role)) ||
    !Number.isSafeInteger(value.revision) ||
    Number(value.revision) < 0
  ) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid space`);
  }
  return {
    id,
    kind: value.kind as TeamSpace['kind'],
    name,
    role: value.role as TeamSpace['role'],
    revision: value.revision as number,
  };
}

function parsePublishedResource(value: unknown, operation: string): TeamPublishedResource {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid published resource`);
  const publicationId = boundedString(value.publicationId);
  const packageId = boundedString(value.packageId || value.id);
  const version = boundedString(value.version);
  const digest = boundedString(value.digest);
  const publishedByUserId = boundedString(value.publishedByUserId);
  const publishedAtMs = nonNegativeInteger(value.publishedAtMs);
  const updatedAtMs = nonNegativeInteger(value.updatedAtMs);
  if (
    !publicationId
    || !packageId
    || !version
    || !digest
    || !publishedByUserId
    || publishedAtMs === null
    || updatedAtMs === null
    || !['published', 'withdrawn'].includes(String(value.status))
  ) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid published resource`);
  }
  return {
    publicationId,
    packageId,
    version,
    digest,
    status: value.status as TeamPublishedResource['status'],
    metadata: parseResourceMetadata(value.metadata, operation, packageId, version),
    publishedByUserId,
    publishedAtMs,
    updatedAtMs,
  };
}

function parseResourceCatalogEntry(value: unknown, operation: string): TeamResourceCatalogEntry {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid resource catalog entry`);
  const packageId = boundedString(value.packageId || value.id);
  const version = value.version === null || value.version === undefined ? null : boundedString(value.version);
  if (!packageId || (value.version !== null && value.version !== undefined && !version)) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid resource catalog entry`);
  }
  return {
    ...parseResourceMetadata(value, operation, packageId, version),
    packageId,
    version,
  };
}

function parseResourceMetadata(
  value: unknown,
  operation: string,
  packageId: string,
  version: string | null,
): TeamResourceMetadata {
  const metadata = isRecord(value) ? value : {};
  const source = isRecord(metadata.source) ? metadata.source : {};
  const displayName = boundedString(metadata.displayName) || packageId;
  const sourceKind = boundedString(source.kind) || 'bundled';
  const sourceLabel = boundedString(source.label) || 'Product bundle';
  const permissions = stringArray(metadata.permissions).map((item) => item.trim()).filter(Boolean).slice(0, 64);
  const compatibility = isRecord(metadata.compatibility) ? { ...metadata.compatibility } : {};
  const security = isRecord(metadata.security) ? { ...metadata.security } : {};
  const distribution = boundedString(metadata.distribution) || 'team_staged_source';
  const normalizedVersion = version ?? (metadata.version === null || metadata.version === undefined ? null : boundedString(metadata.version) || null);
  const result: TeamResourceMetadata = {
    displayName,
    description: boundedPlainText(metadata.description, 4_096) ?? '',
    publisher: boundedString(metadata.publisher),
    source: { kind: sourceKind, label: sourceLabel },
    permissions,
    compatibility,
    security,
    installable: metadata.installable === true,
    distribution,
    version: normalizedVersion,
  };
  const releasedAt = boundedString(metadata.releasedAt);
  const notes = boundedPlainText(metadata.notes, 4_096);
  const manifestName = boundedString(metadata.manifestName);
  if (releasedAt) result.releasedAt = releasedAt;
  if (notes !== null && notes) result.notes = notes;
  if (manifestName) result.manifestName = manifestName;
  if (isRecord(metadata.resources)) {
    const resources: Record<string, string[]> = {};
    for (const [key, entries] of Object.entries(metadata.resources)) {
      if (Array.isArray(entries)) resources[key] = stringArray(entries).slice(0, 128);
    }
    if (Object.keys(resources).length) result.resources = resources;
  }
  if (isRecord(metadata.extensionApp)) result.extensionApp = { ...metadata.extensionApp };
  if (!result.displayName) throw new TypeError(`TeamGateway ${operation} returned invalid resource metadata`);
  return result;
}

function parseResourceSelection(value: unknown, operation: string): TeamResourceSelection {
  const root = isRecord(value) && isRecord(value.selection) ? value.selection : value;
  if (!isRecord(root)) throw new TypeError(`TeamGateway ${operation} returned an invalid resource selection`);
  const spaceId = boundedString(root.spaceId);
  const revision = nonNegativeInteger(root.revision);
  const publicationIds = boundedIdentifierList(root.publicationIds, 16, operation, 'publicationIds');
  const updatedByUserId = boundedString(root.updatedByUserId);
  const updatedAtMs = nonNegativeInteger(root.updatedAtMs);
  if (!spaceId || revision === null || !publicationIds || updatedAtMs === null || !Array.isArray(root.items)) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid resource selection`);
  }
  return {
    spaceId,
    revision,
    publicationIds,
    items: root.items.map((item) => parsePublishedResource(item, operation)),
    updatedByUserId,
    updatedAtMs,
  };
}

function parseSessionResourceSnapshot(value: unknown, operation: string): TeamSessionResourceSnapshot {
  const root = isRecord(value) && isRecord(value.snapshot) ? value.snapshot : value;
  if (!isRecord(root)) throw new TypeError(`TeamGateway ${operation} returned an invalid Session resource snapshot`);
  const sessionId = boundedString(root.sessionId);
  const spaceId = boundedString(root.spaceId);
  const selectionRevision = nonNegativeInteger(root.selectionRevision);
  const publicationIds = boundedIdentifierList(root.publicationIds, 16, operation, 'publicationIds');
  const createdAtMs = nonNegativeInteger(root.createdAtMs);
  if (!sessionId || !spaceId || selectionRevision === null || !publicationIds || createdAtMs === null || !Array.isArray(root.items)) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid Session resource snapshot`);
  }
  return {
    sessionId,
    spaceId,
    selectionRevision,
    publicationIds,
    items: root.items.map((item) => parsePublishedResource(item, operation)),
    createdAtMs,
  };
}

function parseProjectMember(value: unknown, operation: string): TeamProjectMember {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid project member`);
  const id = boundedString(value.id);
  const username = boundedString(value.username);
  const displayName = typeof value.displayName === 'string' && value.displayName.trim()
    ? value.displayName.trim()
    : username;
  if (
    !id ||
    !username ||
    !displayName ||
    typeof value.active !== 'boolean' ||
    !['owner', 'maintainer', 'contributor', 'viewer'].includes(String(value.role))
  ) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid project member`);
  }
  return {
    id,
    username,
    displayName,
    active: value.active,
    role: value.role as TeamProjectMember['role'],
  };
}

function parseProjectOverview(value: unknown, operation: string): TeamProjectOverview {
  if (!isRecord(value) || value.ok !== true) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid project overview`);
  }
  if (!Array.isArray(value.briefHistory) || !Array.isArray(value.members) || !Array.isArray(value.rooms) || !Array.isArray(value.drafts)) {
    throw new TypeError(`TeamGateway ${operation} returned an incomplete project overview`);
  }
  return {
    project: parseProjectOverviewProject(value.project, operation),
    brief: parseProjectBrief(value.brief, operation),
    briefHistory: value.briefHistory.map((item) => parseProjectBrief(item, operation)).slice(0, 20),
    members: value.members.map((item) => parseProjectMember(item, operation)),
    rooms: value.rooms.map((item) => parseProjectRoom(item, operation)),
    drafts: value.drafts.map((item) => parseProjectDraft(item, operation)),
    repository: value.repository === null ? null : parseProjectRepository(value.repository, operation),
    runtime: parseProjectRuntime(value.runtime, operation),
    truncated: parseProjectOverviewTruncated(value.truncated, operation),
  };
}

function parseConnectionList(value: unknown, operation: string): TeamConnectionList {
  if (
    !isRecord(value)
    || typeof value.configured !== 'boolean'
    || typeof value.oauthAvailable !== 'boolean'
    || typeof value.canCreateProject !== 'boolean'
    || !Array.isArray(value.items)
    || !Array.isArray(value.grants)
    || !Array.isArray(value.sessions)
  ) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid connection list`);
  }
  return {
    configured: value.configured,
    oauthAvailable: value.oauthAvailable,
    items: value.items.map((item) => parseConnection(item, operation)),
    grants: value.grants.map((item) => parseConnectionGrant(item, operation)),
    sessions: value.sessions.map((item) => parseConnectionSession(item, operation)),
    canCreateProject: value.canCreateProject,
  };
}

function parseConnection(value: unknown, operation: string): TeamConnection {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid connection`);
  const id = boundedString(value.id);
  const ownerId = boundedString(value.ownerId);
  const label = boundedString(value.label);
  const accountLogin = boundedString(value.accountLogin);
  const repositories = parseConnectionRepositories(value.repositories, operation);
  const operations = parseGitHubOperations(value.operations, operation);
  const revision = nonNegativeInteger(value.revision);
  const createdAtMs = nonNegativeInteger(value.createdAtMs);
  if (
    !id
    || value.provider !== 'github'
    || !['personal', 'project'].includes(String(value.scope))
    || !ownerId
    || !label
    || !accountLogin
    || repositories.length === 0
    || operations.length === 0
    || !['active', 'revoked', 'reconnect_required'].includes(String(value.status))
    || revision === null
    || createdAtMs === null
    || typeof value.canManage !== 'boolean'
  ) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid connection`);
  }
  return {
    id,
    provider: 'github',
    scope: value.scope as TeamConnection['scope'],
    ownerId,
    label,
    accountLogin,
    repositories,
    operations,
    status: value.status as TeamConnectionStatus,
    revision,
    canManage: value.canManage,
    createdAtMs,
  };
}

function parseConnectionSession(value: unknown, operation: string): TeamConnectionSession {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid connection Session`);
  const id = boundedString(value.id);
  const title = boundedString(value.title);
  const status = boundedString(value.status);
  const ownerUserId = boundedString(value.ownerUserId);
  if (!id || !title || !status || !ownerUserId) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid connection Session`);
  }
  return { id, title, status, ownerUserId };
}

function parseConnectionGrant(value: unknown, operation: string): TeamConnectionGrant {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid connection grant`);
  const id = boundedString(value.id);
  const connectionId = boundedString(value.connectionId);
  const sessionId = boundedString(value.sessionId);
  const spaceId = boundedString(value.spaceId);
  const repository = normalizeGitHubRepository(value.repository);
  const operations = parseGitHubOperations(value.operations, operation);
  const expiresAtMs = nonNegativeInteger(value.expiresAtMs);
  const connectionLabel = boundedString(value.connectionLabel);
  const accountLogin = boundedString(value.accountLogin);
  if (
    !id
    || !connectionId
    || !sessionId
    || !spaceId
    || !repository
    || operations.length === 0
    || expiresAtMs === null
    || !['active', 'revoked', 'expired'].includes(String(value.status))
    || !connectionLabel
    || !accountLogin
  ) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid connection grant`);
  }
  return {
    id,
    connectionId,
    sessionId,
    spaceId,
    repository,
    operations,
    expiresAtMs,
    status: value.status as TeamGrantStatus,
    connectionLabel,
    accountLogin,
  };
}

function parseConnectionRepositories(value: unknown, operation: string): string[] {
  if (!Array.isArray(value)) throw new TypeError(`TeamGateway ${operation} returned invalid connection repositories`);
  const repositories = value.map((item) => normalizeGitHubRepository(item));
  if (repositories.some((item) => item === null)) {
    throw new TypeError(`TeamGateway ${operation} returned invalid connection repositories`);
  }
  return [...new Set(repositories as string[])];
}

function parseGitHubOperations(value: unknown, operation: string): TeamConnection['operations'] {
  if (!Array.isArray(value)) throw new TypeError(`TeamGateway ${operation} returned invalid GitHub operations`);
  const operations = value.map((item) => typeof item === 'string' && isGitHubOperation(item) ? item : null);
  if (operations.some((item) => item === null)) {
    throw new TypeError(`TeamGateway ${operation} returned invalid GitHub operations`);
  }
  return [...new Set(operations as TeamConnection['operations'])];
}

function parseGitHubAuthorizationUrl(value: unknown, operation: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid OAuth URL`);
  }
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new TypeError(`TeamGateway ${operation} returned an invalid OAuth URL`);
  }
  if (
    parsed.protocol !== 'https:'
    || parsed.hostname !== 'github.com'
    || parsed.port
    || parsed.username
    || parsed.password
    || parsed.hash
    || parsed.pathname !== '/login/oauth/authorize'
  ) {
    throw new TypeError(`TeamGateway ${operation} returned an unsafe OAuth URL`);
  }
  return parsed.toString();
}

function parseProjectPreview(value: unknown, operation: string, baseUrl: URL): TeamProjectPreview {
  if (!isRecord(value) || value.ok !== true) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid project preview`);
  }
  if (
    typeof value.configured !== 'boolean'
    || typeof value.canManage !== 'boolean'
    || nonNegativeInteger(value.currentRequirementsRevision) === null
    || !('active' in value)
    || !('latest' in value)
    || !('repositoryHead' in value)
    || !('openPath' in value)
  ) {
    throw new TypeError(`TeamGateway ${operation} returned an incomplete project preview`);
  }
  const repositoryHead = value.repositoryHead === null ? null : boundedString(value.repositoryHead);
  if (value.repositoryHead !== null && !repositoryHead) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid repository head`);
  }
  const openPath = parseProjectPreviewOpenPath(value.openPath, operation, baseUrl);
  const active = value.active === null ? null : parseProjectDeployment(value.active, operation);
  const latest = value.latest === null ? null : parseProjectDeployment(value.latest, operation);
  return {
    configured: value.configured,
    active,
    latest,
    currentRequirementsRevision: nonNegativeInteger(value.currentRequirementsRevision) as number,
    repositoryHead,
    canManage: value.canManage,
    openPath,
  };
}

function parseProjectDeployment(value: unknown, operation: string): TeamProjectDeployment {
  if (!isRecord(value)) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid project deployment`);
  }
  const id = boundedString(value.id);
  const branch = boundedString(value.branch);
  const commit = boundedString(value.commit);
  const requestedByDisplayName = boundedString(value.requestedByDisplayName);
  const requirementsRevision = nonNegativeInteger(value.requirementsRevision);
  const createdAtMs = nonNegativeInteger(value.createdAtMs);
  const status = value.status;
  if (
    !id
    || !branch
    || !commit
    || !requestedByDisplayName
    || requirementsRevision === null
    || createdAtMs === null
    || !isProjectPreviewStatus(status)
  ) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid project deployment`);
  }
  const parsedReadyAtMs = value.readyAtMs === undefined || value.readyAtMs === null
    ? undefined
    : nonNegativeInteger(value.readyAtMs);
  if (value.readyAtMs !== undefined && value.readyAtMs !== null && parsedReadyAtMs === null) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid project deployment timestamp`);
  }
  const error = value.error === undefined || value.error === null ? undefined : boundedString(value.error);
  if (value.error !== undefined && value.error !== null && typeof value.error !== 'string') {
    throw new TypeError(`TeamGateway ${operation} returned an invalid project deployment error`);
  }
  return {
    id,
    status,
    branch,
    commit,
    requirementsRevision,
    requestedByDisplayName,
    createdAtMs,
    ...(parsedReadyAtMs === undefined || parsedReadyAtMs === null ? {} : { readyAtMs: parsedReadyAtMs }),
    ...(error ? { error } : {}),
  };
}

function isProjectPreviewStatus(value: unknown): value is TeamProjectPreviewStatus {
  return value === 'starting'
    || value === 'ready'
    || value === 'retained'
    || value === 'failed'
    || value === 'stopped'
    || value === 'recovery_required';
}

function parseProjectPreviewOpenPath(value: unknown, operation: string, baseUrl: URL): string | null {
  if (value === null) return null;
  if (typeof value !== 'string' || !value.trim()) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid preview open path`);
  }
  let parsed: URL;
  try {
    parsed = new URL(value, baseUrl);
  } catch {
    throw new TypeError(`TeamGateway ${operation} returned an invalid preview open path`);
  }
  if (
    parsed.origin !== baseUrl.origin
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
    || !/^\/api\/team\/projects\/[^/]+\/preview\/open$/u.test(parsed.pathname)
  ) {
    throw new TypeError(`TeamGateway ${operation} returned an unsafe preview open path`);
  }
  return parsed.toString();
}

function parseProjectOverviewProject(value: unknown, operation: string): TeamProjectOverview['project'] {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid project identity`);
  const id = boundedString(value.id);
  const name = boundedString(value.name);
  if (!id || !name || !['owner', 'maintainer', 'contributor', 'viewer'].includes(String(value.role))) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid project identity`);
  }
  return {
    id,
    name,
    role: value.role as TeamProjectOverview['project']['role'],
  };
}

function parseProjectSession(value: SessionSummary): TeamProjectSession {
  const source = value as unknown as Record<string, unknown>;
  const requirementsRevision = optionalNonNegativeInteger(source.requirementsRevision);
  const currentRequirementsRevision = optionalNonNegativeInteger(source.currentRequirementsRevision);
  const reportedStale = optionalBoolean(source.requirementsStale);
  const derivedStale = requirementsRevision === undefined || currentRequirementsRevision === undefined
    ? undefined
    : (
    requirementsRevision !== undefined
    && currentRequirementsRevision !== undefined
    && requirementsRevision !== currentRequirementsRevision
    );
  const requirementsStale = reportedStale ?? derivedStale;
  return {
    ...value,
    ...(requirementsRevision === undefined ? {} : { requirementsRevision }),
    ...(currentRequirementsRevision === undefined ? {} : { currentRequirementsRevision }),
    ...(requirementsStale === undefined ? {} : { requirementsStale }),
  };
}

function parseProjectBrief(value: unknown, operation: string): TeamProjectBrief {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid project brief`);
  const objective = boundedProjectText(value.objective, 4_000);
  const acceptanceCriteria = Array.isArray(value.acceptanceCriteria)
    ? value.acceptanceCriteria.map((item) => boundedProjectText(item, 500))
    : null;
  const revision = nonNegativeInteger(value.revision);
  const updatedAtMs = nonNegativeInteger(value.updatedAtMs);
  const updatedByUserId = boundedString(value.updatedByUserId);
  const updatedByDisplayName = boundedString(value.updatedByDisplayName);
  if (
    objective === null
    || acceptanceCriteria === null
    || acceptanceCriteria.length > 20
    || acceptanceCriteria.some((item) => item === null)
    || revision === null
    || updatedAtMs === null
    || updatedByUserId === null
    || updatedByDisplayName === null
  ) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid project brief`);
  }
  return {
    revision,
    objective,
    acceptanceCriteria: acceptanceCriteria as string[],
    updatedAtMs,
    updatedByUserId,
    updatedByDisplayName,
  };
}

function parseProjectRoom(value: unknown, operation: string): TeamProjectRoom {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid project Room`);
  const id = boundedString(value.id);
  const title = boundedString(value.title);
  const status = boundedString(value.status);
  const updatedAtMs = nonNegativeInteger(value.updatedAtMs);
  const participantCount = nonNegativeInteger(value.participantCount);
  if (!id || !title || !status || updatedAtMs === null || participantCount === null || !Array.isArray(value.workItems)) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid project Room`);
  }
  return {
    id,
    title,
    status,
    updatedAtMs,
    participantCount,
    workItems: value.workItems.map((item) => parseProjectRoomWorkItem(item, operation)),
  };
}

function parseProjectRoomWorkItem(value: unknown, operation: string): TeamProjectRoomWorkItem {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid project WorkItem`);
  const id = boundedString(value.id);
  const roomId = boundedString(value.roomId);
  const objective = boundedProjectText(value.objective, 4_000);
  const state = boundedString(value.state);
  const currentOwnerParticipantId = boundedString(value.currentOwnerParticipantId);
  const currentOwnerUserId = boundedString(value.currentOwnerUserId);
  const currentOwnerDisplayName = boundedString(value.currentOwnerDisplayName);
  const ownerSessionId = optionalBounded(value.ownerSessionId);
  const ownerRequirementsRevision = optionalNonNegativeInteger(value.ownerRequirementsRevision);
  const requirementsStale = optionalBoolean(value.requirementsStale);
  const expectedOutput = boundedProjectText(value.expectedOutput, 4_000);
  const updatedAtMs = nonNegativeInteger(value.updatedAtMs);
  if (
    !id
    || !roomId
    || objective === null
    || !state
    || currentOwnerParticipantId === null
    || currentOwnerUserId === null
    || currentOwnerDisplayName === null
    || expectedOutput === null
    || updatedAtMs === null
  ) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid project WorkItem`);
  }
  return {
    id,
    roomId,
    objective,
    state,
    currentOwnerParticipantId,
    currentOwnerUserId,
    currentOwnerDisplayName,
    ...(ownerSessionId ? { ownerSessionId } : {}),
    ...(ownerRequirementsRevision === undefined ? {} : { ownerRequirementsRevision }),
    ...(requirementsStale === undefined ? {} : { requirementsStale }),
    expectedOutput,
    updatedAtMs,
  };
}

function parseProjectRepository(value: unknown, operation: string): TeamProjectRepository {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid project repository`);
  const branch = boundedString(value.branch);
  const headCommit = boundedString(value.headCommit);
  const revision = nonNegativeInteger(value.revision);
  if (!branch || headCommit === null || revision === null) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid project repository`);
  }
  return { branch, headCommit, revision };
}

function parseProjectRuntime(value: unknown, operation: string): TeamProjectRuntime {
  if (!isRecord(value) || typeof value.configured !== 'boolean') {
    throw new TypeError(`TeamGateway ${operation} returned an invalid project runtime`);
  }
  return { configured: value.configured };
}

function parseProjectOverviewTruncated(value: unknown, operation: string): TeamProjectOverviewTruncated {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid truncation summary`);
  const flags = ['rooms', 'workItems', 'drafts', 'briefHistory'] as const;
  if (flags.some((flag) => typeof value[flag] !== 'boolean')) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid truncation summary`);
  }
  return {
    rooms: value.rooms as boolean,
    workItems: value.workItems as boolean,
    drafts: value.drafts as boolean,
    briefHistory: value.briefHistory as boolean,
  };
}

function parseProjectDraft(value: unknown, operation: string): TeamProjectDraft {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid project draft`);
  const draftId = boundedString(value.draftId);
  const sessionId = boundedString(value.sessionId);
  const creatorUserId = boundedString(value.creatorUserId || value.ownerUserId);
  const title = boundedString(value.title || value.description);
  const baseCommit = boundedString(value.baseCommit);
  const draftCommit = boundedString(value.draftCommit);
  const manifestHash = boundedString(value.manifestHash || value.manifestSha256);
  const requirementsRevision = optionalNonNegativeInteger(value.requirementsRevision);
  const currentRequirementsRevision = optionalNonNegativeInteger(value.currentRequirementsRevision);
  const requirementsStale = optionalBoolean(value.requirementsStale);
  if (
    !draftId || !sessionId || !creatorUserId || !title || !baseCommit || !draftCommit || !manifestHash
    || !Number.isSafeInteger(value.createdAtMs)
  ) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid project draft`);
  }
  const status = typeof value.status === 'string' ? value.status : undefined;
  return {
    draftId,
    ...(optionalBounded(value.spaceId) ? { spaceId: optionalBounded(value.spaceId) } : {}),
    ...(optionalBounded(value.targetBranch) ? { targetBranch: optionalBounded(value.targetBranch) } : {}),
    ...(optionalBounded(value.workspaceId) ? { workspaceId: optionalBounded(value.workspaceId) } : {}),
    sessionId,
    creatorUserId,
    ...(optionalBounded(value.creatorDisplayName) ? { creatorDisplayName: optionalBounded(value.creatorDisplayName) } : {}),
    title,
    baseCommit,
    ...(Number.isSafeInteger(value.baseRevision) ? { baseRevision: value.baseRevision as number } : {}),
    draftCommit,
    manifestHash,
    ...(Array.isArray(value.manifest) ? { manifest: value.manifest } : {}),
    ...(status ? { status } : {}),
    createdAtMs: value.createdAtMs as number,
    ...(requirementsRevision === undefined ? {} : { requirementsRevision }),
    ...(currentRequirementsRevision === undefined ? {} : { currentRequirementsRevision }),
    ...(requirementsStale === undefined ? {} : { requirementsStale }),
    ...(Number.isSafeInteger(value.integratedAtMs) ? { integratedAtMs: value.integratedAtMs as number } : {}),
    ...(optionalBounded(value.integratedCommit) ? { integratedCommit: optionalBounded(value.integratedCommit) } : {}),
  };
}

function parseProjectIntegration(value: unknown, operation: string): TeamProjectIntegration {
  if (!isRecord(value) || !['integrated', 'conflict', 'verification_failed'].includes(String(value.status))) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid integration result`);
  }
  const result: TeamProjectIntegration = {
    status: value.status as TeamProjectIntegration['status'],
  };
  for (const key of ['draftId', 'spaceId', 'targetBranch', 'expectedHead', 'headCommit', 'message']) {
    const next = optionalBounded(value[key]);
    if (next) result[key] = next;
  }
  if (value.candidateCommit === null) result.candidateCommit = null;
  else if (optionalBounded(value.candidateCommit)) result.candidateCommit = optionalBounded(value.candidateCommit);
  if ('verifier' in value) result.verifier = value.verifier;
  return result;
}

function parseProjectAdoption(value: unknown, operation: string): TeamProjectAdoption {
  if (!isRecord(value) || !['adopted', 'conflict'].includes(String(value.status))) {
    throw new TypeError(`TeamGateway ${operation} returned an invalid adoption result`);
  }
  const result: TeamProjectAdoption = {
    status: value.status as TeamProjectAdoption['status'],
  };
  for (const key of [
    'adoptionId',
    'draftId',
    'spaceId',
    'workspaceId',
    'targetSessionId',
    'sourceDraftCommit',
    'targetTreeCommit',
    'message',
  ]) {
    const next = optionalBounded(value[key]);
    if (next) result[key] = next;
  }
  return result;
}

function parseProjectDiff(value: unknown, operation: string): TeamProjectDiff {
  if (!isRecord(value)) throw new TypeError(`TeamGateway ${operation} returned an invalid draft diff`);
  const draftId = boundedString(value.draftId);
  const baseCommit = boundedString(value.baseCommit);
  const draftCommit = boundedString(value.draftCommit);
  const diff = boundedPlainText(value.diff, 512 * 1024);
  if (!draftId || !baseCommit || !draftCommit || diff === null || typeof value.truncated !== 'boolean') {
    throw new TypeError(`TeamGateway ${operation} returned an invalid draft diff`);
  }
  return { draftId, baseCommit, draftCommit, diff, truncated: value.truncated };
}

function assertId(value: string, label: string): string {
  const normalized = value.trim();
  if (!normalized || normalized.length > 160 || /[\u0000-\u001f\u007f]/u.test(normalized)) {
    throw new TypeError(`TeamGateway ${label} must be a bounded identifier`);
  }
  return normalized;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : [];
}

function boundedIdentifierList(
  value: unknown,
  maximum: number,
  operation: string,
  label: string,
): string[] | null {
  if (!Array.isArray(value) || value.length > maximum) return null;
  const identifiers = value.map((item) => {
    if (typeof item !== 'string') return null;
    try {
      return assertId(item, label);
    } catch {
      return null;
    }
  });
  if (identifiers.some((item) => item === null)) {
    throw new TypeError(`TeamGateway ${operation} returned invalid ${label}`);
  }
  const normalized = identifiers as string[];
  return new Set(normalized).size === normalized.length ? normalized : null;
}

function validateResourceSelectionInput(input: TeamResourceSelectionInput): void {
  if (!Number.isSafeInteger(input.baseRevision) || input.baseRevision < 0) {
    throw new TypeError('TeamGateway resource selection baseRevision must be a non-negative integer');
  }
  const publicationIds = boundedIdentifierList(input.publicationIds, 16, 'team.space-resources.update', 'publicationIds');
  if (!publicationIds) {
    throw new TypeError('TeamGateway resource selection publicationIds must contain at most 16 unique identifiers');
  }
}

function projectConnectionsPath(spaceId: string): string {
  return `/api/team/spaces/${encodeURIComponent(assertId(spaceId, 'spaceId'))}/connections`;
}

function validateConnectionCreateInput(input: TeamConnectionCreateInput): TeamConnectionCreateInput {
  if (!input || !['personal', 'project'].includes(String(input.scope))) {
    throw new TypeError('TeamGateway connection scope is invalid');
  }
  const label = boundedString(input.label);
  if (!label || label.length > 160) throw new TypeError('TeamGateway connection label is invalid');
  const repositories = Array.isArray(input.repositories)
    ? input.repositories.map((repository) => normalizeGitHubRepository(repository))
    : [];
  if (!repositories.length || repositories.some((repository) => repository === null)) {
    throw new TypeError('TeamGateway connection repositories must contain owner/repo values');
  }
  const operations = validateGitHubOperations(input.operations);
  return {
    scope: input.scope,
    label,
    repositories: [...new Set(repositories as string[])],
    operations,
  };
}

function validateConnectionToken(token: string): void {
  // Keep this value opaque: it is only placed in the request body and never
  // interpolated into an error, URL, log, or persisted client state.
  if (typeof token !== 'string' || !token.length || token.length > 16_384 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(token)) {
    throw new TypeError('TeamGateway connection token is invalid');
  }
}

function validateConnectionGrantInput(input: TeamConnectionGrantInput): TeamConnectionGrantInput {
  if (!input || !assertId(input.connectionId, 'connectionId') || !assertId(input.sessionId, 'sessionId')) {
    throw new TypeError('TeamGateway connection grant identifiers are invalid');
  }
  const repository = normalizeGitHubRepository(input.repository);
  if (!repository) throw new TypeError('TeamGateway grant repository must be owner/repo');
  const operations = validateGitHubOperations(input.operations);
  if (!Number.isSafeInteger(input.ttlSeconds) || input.ttlSeconds < 60 || input.ttlSeconds > 86_400) {
    throw new TypeError('TeamGateway grant ttlSeconds must be between 60 and 86400');
  }
  return {
    connectionId: assertId(input.connectionId, 'connectionId'),
    sessionId: assertId(input.sessionId, 'sessionId'),
    repository,
    operations,
    ttlSeconds: input.ttlSeconds,
  };
}

function validateGitHubOperations(value: unknown): TeamConnection['operations'] {
  if (!Array.isArray(value) || value.length === 0) {
    throw new TypeError('TeamGateway GitHub operations must contain at least one operation');
  }
  const operations = value.map((operation) => typeof operation === 'string' && isGitHubOperation(operation) ? operation : null);
  if (operations.some((operation) => operation === null)) {
    throw new TypeError('TeamGateway GitHub operations contain an unsupported operation');
  }
  return [...new Set(operations as TeamConnection['operations'])];
}

function isGitHubOperation(value: string): value is TeamConnection['operations'][number] {
  return (TEAM_GITHUB_OPERATIONS as readonly string[]).includes(value);
}

export function normalizeGitHubRepository(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const repository = value.trim().toLowerCase();
  const parts = repository.split('/');
  return /^[a-z0-9][a-z0-9.-]{0,98}\/[a-z0-9._-]{1,100}$/u.test(repository)
    && !['.', '..'].includes(parts[1]) && !repository.endsWith('.git') ? repository : null;
}

function validateProjectBriefUpdate(input: TeamProjectBriefUpdateInput): void {
  if (!Number.isSafeInteger(input.baseRevision) || input.baseRevision < 0) {
    throw new TypeError('TeamGateway baseRevision must be a non-negative integer');
  }
  if (boundedProjectText(input.objective, 4_000) === null) {
    throw new TypeError('TeamGateway objective must be plain text of at most 4000 characters');
  }
  if (!Array.isArray(input.acceptanceCriteria) || input.acceptanceCriteria.length > 20) {
    throw new TypeError('TeamGateway acceptanceCriteria must contain at most 20 items');
  }
  if (input.acceptanceCriteria.some((item) => boundedProjectText(item, 500) === null)) {
    throw new TypeError('TeamGateway acceptanceCriteria items must be plain text of at most 500 characters');
  }
}

function validateSessionRequirementsInput(input: TeamSessionRequirementsInput): void {
  if (!Number.isSafeInteger(input.baseRevision) || input.baseRevision < 0) {
    throw new TypeError('TeamGateway baseRevision must be a non-negative integer');
  }
  if (!Number.isSafeInteger(input.revision) || input.revision < 0) {
    throw new TypeError('TeamGateway revision must be a non-negative integer');
  }
}

function nonNegativeInteger(value: unknown): number | null {
  return Number.isSafeInteger(value) && Number(value) >= 0 ? Number(value) : null;
}

function optionalNonNegativeInteger(value: unknown): number | undefined {
  if (value === undefined || value === null) return undefined;
  const parsed = nonNegativeInteger(value);
  return parsed === null ? undefined : parsed;
}

function optionalBoolean(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined;
}

function boundedString(value: unknown): string {
  if (typeof value !== 'string') return '';
  const normalized = value.trim();
  return normalized.length <= 512 && !/[\u0000-\u001f\u007f]/u.test(normalized) ? normalized : '';
}

function boundedProjectText(value: unknown, maxLength: number): string | null {
  if (typeof value !== 'string' || value.length > maxLength) return null;
  return /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(value) ? null : value;
}

function optionalBounded(value: unknown): string {
  return boundedString(value);
}

function boundedPlainText(value: unknown, maxLength: number): string | null {
  if (typeof value !== 'string' || value.length > maxLength) return null;
  return /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/u.test(value) ? null : value;
}

async function readPayload(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined;
  const text = await response.text();
  if (!text) return undefined;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
