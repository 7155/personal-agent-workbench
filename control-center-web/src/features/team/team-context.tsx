import { QueryClientProvider } from '@tanstack/react-query';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { ControlConnectionMonitor } from '@/app/control-connection-monitor';
import { ControlTransportProvider } from '@/app/control-transport';
import { createQueryClient } from '@/app/query-client';
import { useFilePreviewStore } from '@/features/agent/file-preview/file-preview-store';
import { useAgentLiveStore } from '@/features/agent/state/live-store';
import { ProductIdentityProvider } from '@/features/identity/product-identity';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import type { ControlTransport } from '@/platform/transport';
import { TeamApi, TeamApiError, teamApiErrorMessage } from './team-api';
import type { TeamProjectSessionListOptions } from './team-api';
import { createTeamTransport } from '@/features/team/team-transport';
import type {
  TeamDirectoryUser,
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
  TeamProjectPreview,
  TeamProjectSession,
  TeamConnection,
  TeamConnectionCreateInput,
  TeamConnectionGrant,
  TeamConnectionGrantInput,
  TeamConnectionList,
  TeamConnectionTokenInput,
  TeamSessionRequirementsInput,
  TeamSessionRequirementsResult,
  TeamSession,
  TeamSpace,
  TeamStatus,
  TeamUser,
} from './types';

export type TeamPhase = 'checking' | 'anonymous' | 'authenticated' | 'error';

export interface TeamContextValue {
  api: TeamApi;
  phase: TeamPhase;
  status: TeamStatus | null;
  user: TeamUser | null;
  spaces: TeamSpace[];
  activeSpace: TeamSpace | null;
  csrfToken: string | null;
  scopeKey: string | null;
  desktopStorageKey: string | undefined;
  routePrefix: string | undefined;
  busy: string | null;
  error: string | null;
  retry(): Promise<void>;
  login(username: string, password: string): Promise<void>;
  logout(): Promise<void>;
  selectSpace(spaceId: string): void;
  refreshMe(): Promise<TeamSession>;
  createProject(name: string): Promise<TeamSpace>;
  listMembers(): Promise<TeamUser[]>;
  listDirectory(): Promise<TeamDirectoryUser[]>;
  createMember(input: TeamMemberInput): Promise<TeamUser>;
  setMemberStatus(userId: string, active: boolean): Promise<TeamUser>;
  listProjectMembers(spaceId: string): Promise<TeamProjectMember[]>;
  addProjectMember(spaceId: string, input: TeamProjectMemberInput): Promise<TeamProjectMember>;
  removeProjectMember(spaceId: string, userId: string): Promise<void>;
  getProjectOverview(spaceId: string): Promise<TeamProjectOverview>;
  getProjectPreview(spaceId: string): Promise<TeamProjectPreview>;
  startProjectPreview(spaceId: string, clientRequestId: string): Promise<TeamProjectPreview>;
  stopProjectPreview(spaceId: string): Promise<TeamProjectPreview>;
  listConnections(spaceId: string): Promise<TeamConnectionList>;
  createConnectionWithToken(spaceId: string, input: TeamConnectionTokenInput): Promise<TeamConnection>;
  startConnectionOAuth(spaceId: string, input: TeamConnectionCreateInput): Promise<{ authorizationUrl: string }>;
  revokeConnection(spaceId: string, connectionId: string): Promise<TeamConnection>;
  createConnectionGrant(spaceId: string, input: TeamConnectionGrantInput): Promise<TeamConnectionGrant>;
  revokeConnectionGrant(spaceId: string, grantId: string): Promise<TeamConnectionGrant>;
  updateProjectBrief(spaceId: string, input: TeamProjectBriefUpdateInput): Promise<TeamProjectBrief>;
  adoptSessionRequirements(spaceId: string, sessionId: string, input: TeamSessionRequirementsInput): Promise<TeamSessionRequirementsResult>;
  listProjectDrafts(spaceId: string): Promise<TeamProjectDraft[]>;
  listProjectSessions(spaceId: string, options?: TeamProjectSessionListOptions): Promise<TeamProjectSession[]>;
  getProjectDraftDiff(spaceId: string, draftId: string): Promise<TeamProjectDiff>;
  publishProjectDraft(spaceId: string, input: { sessionId: string; title: string }): Promise<TeamProjectDraft>;
  integrateProjectDraft(spaceId: string, draftId: string): Promise<TeamProjectIntegration>;
  adoptProjectDraft(spaceId: string, draftId: string, sessionId: string): Promise<TeamProjectAdoption>;
}

const TeamContext = createContext<TeamContextValue | null>(null);

export function TeamProvider({ children, api: suppliedApi }: { children: ReactNode; api?: TeamApi }) {
  const api = useMemo(() => suppliedApi ?? new TeamApi(), [suppliedApi]);
  const [phase, setPhase] = useState<TeamPhase>('checking');
  const [status, setStatus] = useState<TeamStatus | null>(null);
  const [user, setUser] = useState<TeamUser | null>(null);
  const [spaces, setSpaces] = useState<TeamSpace[]>([]);
  const [activeSpaceId, setActiveSpaceId] = useState<string | null>(null);
  const [csrfToken, setCsrfToken] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const identityGeneration = useRef(0);
  const refreshGeneration = useRef(0);
  const authenticationChange = useRef(false);
  const mountedRef = useRef(false);

  const clearSession = useCallback(() => {
    identityGeneration.current += 1;
    setUser(null);
    setSpaces([]);
    setActiveSpaceId(null);
    setCsrfToken(null);
    setPhase('anonymous');
  }, []);

  const applySession = useCallback((session: TeamSession) => {
    setUser(session.user);
    setSpaces(session.spaces);
    setCsrfToken(session.csrfToken);
    setActiveSpaceId((current) => {
      if (current && session.spaces.some((space) => space.id === current)) return current;
      return preferredSpace(session.spaces)?.id ?? null;
    });
  }, []);

  const refreshMe = useCallback(async (): Promise<TeamSession> => {
    if (authenticationChange.current) throw new Error('登录状态正在更新，请稍候。');
    if (!mountedRef.current) throw new Error('登录状态已结束，已忽略旧请求。');
    const identity = identityGeneration.current;
    const refresh = ++refreshGeneration.current;
    const current = () => mountedRef.current
      && identityGeneration.current === identity
      && refreshGeneration.current === refresh;
    try {
      const session = await api.me();
      if (!current()) throw new Error('登录状态已变化，已忽略旧请求。');
      applySession(session);
      setPhase('authenticated');
      return session;
    } catch (meError) {
      if (current() && isUnauthorizedTeamError(meError)) clearSession();
      throw meError;
    }
  }, [api, applySession, clearSession]);

  const probe = useCallback(async () => {
    if (authenticationChange.current || !mountedRef.current) return;
    const identity = ++identityGeneration.current;
    const isCurrentProbe = () => mountedRef.current && identityGeneration.current === identity;
    setPhase('checking');
    setError(null);
    try {
      const gatewayStatus = await api.status();
      if (!isCurrentProbe()) return;
      setStatus(gatewayStatus);
      // refreshMe advances its own generation synchronously before yielding.
      // Keep the probe's catch path from interpreting a superseded 401 as a
      // logout for the newer same-identity refresh.
      const refresh = refreshGeneration.current + 1;
      try {
        await refreshMe();
      } catch (meError) {
        if (!isCurrentProbe() || refreshGeneration.current !== refresh) return;
        if (isUnauthorizedTeamError(meError)) {
          clearSession();
          return;
        }
        throw meError;
      }
    } catch (probeError) {
      if (!isCurrentProbe()) return;
      setPhase('error');
      setError(teamApiErrorMessage(probeError, '团队服务暂时不可用，请稍后重试。'));
    }
  }, [api, clearSession, refreshMe]);

  useEffect(() => {
    mountedRef.current = true;
    void probe();
    return () => {
      mountedRef.current = false;
      identityGeneration.current += 1;
    };
  }, [probe]);

  const login = useCallback(async (username: string, password: string) => {
    if (authenticationChange.current) throw new Error('登录状态正在更新，请稍候。');
    authenticationChange.current = true;
    clearSession();
    const identity = identityGeneration.current;
    setBusy('login');
    setError(null);
    try {
      const session = await api.login(username, password);
      if (identityGeneration.current !== identity) throw new Error('登录状态已变化，已忽略旧请求。');
      applySession(session);
      setPhase('authenticated');
    } catch (loginError) {
      if (identityGeneration.current === identity) {
        setPhase('anonymous');
        setError(teamApiErrorMessage(loginError, '登录失败，请检查账号和密码。'));
      }
      throw loginError;
    } finally {
      authenticationChange.current = false;
      if (identityGeneration.current === identity) setBusy(null);
    }
  }, [api, applySession, clearSession]);

  const logout = useCallback(async () => {
    if (authenticationChange.current) return;
    authenticationChange.current = true;
    const identity = ++identityGeneration.current;
    setBusy('logout');
    setPhase('checking');
    setError(null);
    try {
      await api.logout(csrfToken ?? undefined);
    } catch (logoutError) {
      if (identityGeneration.current === identity) setError(teamApiErrorMessage(logoutError, '退出登录请求失败，本地会话已清除。'));
    } finally {
      authenticationChange.current = false;
      if (identityGeneration.current === identity) {
        clearSession();
        setBusy(null);
      }
    }
  }, [api, clearSession, csrfToken]);

  const selectSpace = useCallback((spaceId: string) => {
    const next = spaces.find((space) => space.id === spaceId);
    if (!next) {
      setError('这个工作空间不在当前账号的授权范围内。');
      return;
    }
    setError(null);
    setActiveSpaceId(next.id);
  }, [spaces]);

  const createProject = useCallback(async (name: string) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy('create-project');
    setError(null);
    try {
      const created = await api.createProject(name, csrfToken);
      const session = await refreshMe();
      if (session.spaces.some((space) => space.id === created.id)) setActiveSpaceId(created.id);
      return created;
    } catch (projectError) {
      setError(teamApiErrorMessage(projectError, '项目创建失败，请稍后重试。'));
      throw projectError;
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken, refreshMe]);

  const listMembers = useCallback(async () => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    return api.listMembers(csrfToken);
  }, [api, csrfToken]);

  const listDirectory = useCallback(async () => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    return api.listDirectory(csrfToken);
  }, [api, csrfToken]);

  const createMember = useCallback(async (input: TeamMemberInput) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy('create-member');
    try {
      const member = await api.createMember(input, csrfToken);
      await refreshMe();
      return member;
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken, refreshMe]);

  const setMemberStatus = useCallback(async (userId: string, active: boolean) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy(`member-status:${userId}`);
    try {
      const member = await api.setMemberStatus(userId, active, csrfToken);
      await refreshMe();
      return member;
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken, refreshMe]);

  const listProjectMembers = useCallback(async (spaceId: string) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    return api.listProjectMembers(spaceId, csrfToken);
  }, [api, csrfToken]);

  const addProjectMember = useCallback(async (spaceId: string, input: TeamProjectMemberInput) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy('add-project-member');
    try {
      const member = await api.addProjectMember(spaceId, input, csrfToken);
      await refreshMe();
      return member;
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken, refreshMe]);

  const removeProjectMember = useCallback(async (spaceId: string, userId: string) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy('remove-project-member');
    try {
      await api.removeProjectMember(spaceId, userId, csrfToken);
      await refreshMe();
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken, refreshMe]);

  const getProjectOverview = useCallback(async (spaceId: string) => {
    return api.getProjectOverview(spaceId, csrfToken ?? undefined);
  }, [api, csrfToken]);

  const getProjectPreview = useCallback(async (spaceId: string) => {
    return api.getProjectPreview(spaceId, csrfToken ?? undefined);
  }, [api, csrfToken]);

  const startProjectPreview = useCallback(async (spaceId: string, clientRequestId: string) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy(`start-project-preview:${spaceId}`);
    try {
      return await api.startProjectPreview(spaceId, clientRequestId, csrfToken);
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken]);

  const stopProjectPreview = useCallback(async (spaceId: string) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy(`stop-project-preview:${spaceId}`);
    try {
      return await api.stopProjectPreview(spaceId, csrfToken);
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken]);

  const listConnections = useCallback(async (spaceId: string) => {
    return api.listConnections(spaceId, csrfToken ?? undefined);
  }, [api, csrfToken]);

  const createConnectionWithToken = useCallback(async (spaceId: string, input: TeamConnectionTokenInput) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy(`create-connection:${spaceId}`);
    try {
      const { token, ...connectionInput } = input;
      return await api.createConnectionWithToken(spaceId, connectionInput, token, csrfToken);
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken]);

  const startConnectionOAuth = useCallback(async (spaceId: string, input: TeamConnectionCreateInput) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy(`start-connection-oauth:${spaceId}`);
    try {
      return await api.startConnectionOAuth(spaceId, input, csrfToken);
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken]);

  const revokeConnection = useCallback(async (spaceId: string, connectionId: string) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy(`revoke-connection:${connectionId}`);
    try {
      return await api.revokeConnection(spaceId, connectionId, csrfToken);
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken]);

  const createConnectionGrant = useCallback(async (spaceId: string, input: TeamConnectionGrantInput) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy(`create-connection-grant:${input.connectionId}`);
    try {
      return await api.createConnectionGrant(spaceId, input, csrfToken);
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken]);

  const revokeConnectionGrant = useCallback(async (spaceId: string, grantId: string) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy(`revoke-connection-grant:${grantId}`);
    try {
      return await api.revokeConnectionGrant(spaceId, grantId, csrfToken);
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken]);

  const updateProjectBrief = useCallback(async (spaceId: string, input: TeamProjectBriefUpdateInput) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy(`update-project-brief:${spaceId}`);
    try {
      return await api.updateProjectBrief(spaceId, input, csrfToken);
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken]);

  const adoptSessionRequirements = useCallback(async (
    spaceId: string,
    sessionId: string,
    input: TeamSessionRequirementsInput,
  ) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy(`adopt-session-requirements:${sessionId}`);
    try {
      return await api.adoptSessionRequirements(spaceId, sessionId, input, csrfToken);
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken]);

  const listProjectDrafts = useCallback(async (spaceId: string) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    return api.listProjectDrafts(spaceId, csrfToken);
  }, [api, csrfToken]);

  const listProjectSessions = useCallback(async (
    spaceId: string,
    options?: TeamProjectSessionListOptions,
  ): Promise<TeamProjectSession[]> => {
    if (!csrfToken || !user) throw new Error('登录状态已失效，请重新登录。');
    const space = spaces.find((candidate) => candidate.id === spaceId);
    if (!space || space.kind !== 'project') throw new Error('这个工作空间不在当前账号的授权范围内。');
    return api.listProjectSessions(space.id, csrfToken, options);
  }, [api, csrfToken, spaces, user]);

  const getProjectDraftDiff = useCallback(async (spaceId: string, draftId: string) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    return api.getProjectDraftDiff(spaceId, draftId, csrfToken);
  }, [api, csrfToken]);

  const publishProjectDraft = useCallback(async (
    spaceId: string,
    input: { sessionId: string; title: string },
  ) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy('publish-project-draft');
    try {
      const draft = await api.publishProjectDraft(spaceId, input, csrfToken);
      await refreshMe();
      return draft;
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken, refreshMe]);

  const integrateProjectDraft = useCallback(async (spaceId: string, draftId: string) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy(`integrate-project-draft:${draftId}`);
    try {
      const integration = await api.integrateProjectDraft(spaceId, draftId, csrfToken);
      await refreshMe();
      return integration;
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken, refreshMe]);

  const adoptProjectDraft = useCallback(async (spaceId: string, draftId: string, sessionId: string) => {
    if (!csrfToken) throw new Error('登录状态已失效，请重新登录。');
    setBusy(`adopt-project-draft:${draftId}`);
    try {
      const adoption = await api.adoptProjectDraft(spaceId, draftId, sessionId, csrfToken);
      await refreshMe();
      return adoption;
    } finally {
      setBusy(null);
    }
  }, [api, csrfToken, refreshMe]);

  const activeSpace = spaces.find((space) => space.id === activeSpaceId) ?? null;
  const serverIdentity = typeof window === 'undefined' ? 'server' : window.location.origin;
  const scopeKey = user && activeSpace
    ? `team:${serverIdentity}:${user.id}:${activeSpace.id}`
    : null;
  const value = useMemo<TeamContextValue>(() => ({
    api,
    phase,
    status,
    user,
    spaces,
    activeSpace,
    csrfToken,
    scopeKey,
    desktopStorageKey: scopeKey ? `pawos.desktop.team.v1:${scopeKey}` : undefined,
    routePrefix: activeSpace ? `/team/spaces/${encodeURIComponent(activeSpace.id)}` : undefined,
    busy,
    error,
    retry: probe,
    login,
    logout,
    selectSpace,
    refreshMe,
    createProject,
    listMembers,
    listDirectory,
    createMember,
    setMemberStatus,
    listProjectMembers,
    addProjectMember,
    removeProjectMember,
    getProjectOverview,
    getProjectPreview,
    startProjectPreview,
    stopProjectPreview,
    listConnections,
    createConnectionWithToken,
    startConnectionOAuth,
    revokeConnection,
    createConnectionGrant,
    revokeConnectionGrant,
    updateProjectBrief,
    adoptSessionRequirements,
    listProjectDrafts,
    listProjectSessions,
    getProjectDraftDiff,
    publishProjectDraft,
    integrateProjectDraft,
    adoptProjectDraft,
  }), [
    activeSpace,
    api,
    busy,
    createMember,
    createProject,
    csrfToken,
    error,
    listMembers,
    listDirectory,
    listProjectMembers,
    login,
    logout,
    phase,
    probe,
    refreshMe,
    removeProjectMember,
    getProjectOverview,
    getProjectPreview,
    startProjectPreview,
    stopProjectPreview,
    listConnections,
    createConnectionWithToken,
    startConnectionOAuth,
    revokeConnection,
    createConnectionGrant,
    revokeConnectionGrant,
    updateProjectBrief,
    adoptSessionRequirements,
    listProjectDrafts,
    listProjectSessions,
    getProjectDraftDiff,
    publishProjectDraft,
    integrateProjectDraft,
    adoptProjectDraft,
    selectSpace,
    setMemberStatus,
    addProjectMember,
    scopeKey,
    spaces,
    status,
    user,
  ]);

  return <TeamContext.Provider value={value}>{children}</TeamContext.Provider>;
}

export function useTeam(): TeamContextValue {
  const context = useContext(TeamContext);
  if (!context) throw new Error('useTeam must be used inside TeamProvider');
  return context;
}

export function useOptionalTeam(): TeamContextValue | null {
  return useContext(TeamContext);
}

export function TeamScopedProviders({ children }: { children: ReactNode }) {
  const team = useTeam();
  const { activeSpace, csrfToken, scopeKey, user } = team;
  const activeSpaceId = activeSpace?.id;
  const csrfRef = useRef(csrfToken);
  csrfRef.current = csrfToken;
  const queryClient = useMemo(() => createQueryClient(), [scopeKey]);
  const transport = useMemo<ControlTransport | null>(() => {
    if (!activeSpaceId || !scopeKey || !user) return null;
    return createTeamTransport({
      baseUrl: window.location.origin,
      routePrefix: `/team/spaces/${encodeURIComponent(activeSpaceId)}`,
      credentials: 'same-origin',
      getHeaders: () => csrfRef.current ? { 'X-CSRF-Token': csrfRef.current } : undefined,
      connectionIdentity: `${scopeKey}`,
    });
  }, [activeSpaceId, scopeKey, user]);

  useEffect(() => () => {
    transport?.dispose?.();
    queryClient.clear();
    useFilePreviewStore.getState().reset();
    useAgentLiveStore.getState().reset();
    useRoomLiveStore.getState().reset();
  }, [queryClient, transport]);

  if (!transport) return null;
  return (
    <ControlTransportProvider transport={transport}>
      <ControlConnectionMonitor />
      <QueryClientProvider client={queryClient}>
        <ProductIdentityProvider>{children}</ProductIdentityProvider>
      </QueryClientProvider>
    </ControlTransportProvider>
  );
}

function preferredSpace(spaces: readonly TeamSpace[]): TeamSpace | undefined {
  return spaces.find((space) => space.kind === 'personal') ?? spaces[0];
}

function isUnauthorizedTeamError(error: unknown): boolean {
  return error instanceof TeamApiError
    ? error.status === 401
    : typeof error === 'object'
      && error !== null
      && 'status' in error
      && (error as { status?: unknown }).status === 401;
}
