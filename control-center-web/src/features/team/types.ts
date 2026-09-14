import type { SessionSummary } from '@/features/agent/types';

export type TeamUserRole = 'admin' | 'member';
export type TeamSpaceKind = 'personal' | 'project';
export type TeamSpaceRole = 'owner' | 'maintainer' | 'contributor' | 'viewer';
export type TeamProjectMemberRole = TeamSpaceRole;

export type TeamProjectMemberInputRole = Exclude<TeamProjectMemberRole, 'owner'>;

export const TEAM_GITHUB_OPERATIONS = [
  'repo.read',
  'file.read',
  'issues.list',
  'issue.read',
  'issue.create',
  'issue.comment',
] as const;

export const TEAM_GITHUB_READ_OPERATIONS = [
  'repo.read',
  'file.read',
  'issues.list',
  'issue.read',
] as const;

export type TeamGitHubOperation = typeof TEAM_GITHUB_OPERATIONS[number];
export type TeamGitHubConnectionScope = 'personal' | 'project';
export type TeamConnectionStatus = 'active' | 'revoked' | 'reconnect_required';
export type TeamGrantStatus = 'active' | 'revoked' | 'expired';

export interface TeamConnectionCreateInput {
  scope: TeamGitHubConnectionScope;
  label: string;
  repositories: string[];
  operations: TeamGitHubOperation[];
}

export interface TeamConnectionTokenInput extends TeamConnectionCreateInput {
  token: string;
}

export interface TeamConnection {
  id: string;
  provider: 'github';
  scope: TeamGitHubConnectionScope;
  ownerId: string;
  label: string;
  accountLogin: string;
  repositories: string[];
  operations: TeamGitHubOperation[];
  status: TeamConnectionStatus;
  revision: number;
  canManage: boolean;
  createdAtMs: number;
}

export interface TeamConnectionSession {
  id: string;
  title: string;
  status: string;
  ownerUserId: string;
}

export interface TeamConnectionGrant {
  id: string;
  connectionId: string;
  sessionId: string;
  spaceId: string;
  repository: string;
  operations: TeamGitHubOperation[];
  expiresAtMs: number;
  status: TeamGrantStatus;
  connectionLabel: string;
  accountLogin: string;
}

export interface TeamConnectionList {
  configured: boolean;
  oauthAvailable: boolean;
  items: TeamConnection[];
  grants: TeamConnectionGrant[];
  sessions: TeamConnectionSession[];
  canCreateProject: boolean;
}

export interface TeamConnectionGrantInput {
  connectionId: string;
  sessionId: string;
  repository: string;
  operations: TeamGitHubOperation[];
  ttlSeconds: number;
}

export interface TeamUser {
  id: string;
  username: string;
  displayName: string;
  role: TeamUserRole;
  active: boolean;
}

/** The invite directory intentionally exposes only selectable identity fields. */
export interface TeamDirectoryUser {
  id: string;
  username: string;
  displayName: string;
}

export interface TeamSpace {
  id: string;
  kind: TeamSpaceKind;
  name: string;
  role: TeamSpaceRole;
  revision: number;
}

export interface TeamStatus {
  enabled: boolean;
  name: string;
}

export type TeamPublishedResourceStatus = 'published' | 'withdrawn';

/** Public metadata copied from the configured Package catalog. */
export interface TeamResourceMetadata {
  displayName: string;
  description: string;
  publisher: string;
  source: { kind: string; label: string };
  permissions: string[];
  compatibility: Record<string, unknown>;
  security: Record<string, unknown>;
  installable: boolean;
  distribution: string;
  version?: string | null;
  releasedAt?: string;
  notes?: string;
  manifestName?: string;
  resources?: Record<string, string[]>;
  extensionApp?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface TeamResourceCatalogEntry extends TeamResourceMetadata {
  packageId: string;
  version: string | null;
}

export interface TeamPublishedResource {
  publicationId: string;
  packageId: string;
  version: string;
  digest: string;
  status: TeamPublishedResourceStatus;
  metadata: TeamResourceMetadata;
  publishedByUserId: string;
  publishedAtMs: number;
  updatedAtMs: number;
}

export interface TeamResourceSelection {
  spaceId: string;
  revision: number;
  publicationIds: string[];
  items: TeamPublishedResource[];
  updatedByUserId: string;
  updatedAtMs: number;
}

export interface TeamResourceSelectionInput {
  baseRevision: number;
  publicationIds: string[];
}

export interface TeamSessionResourceSnapshot {
  sessionId: string;
  spaceId: string;
  selectionRevision: number;
  publicationIds: string[];
  items: TeamPublishedResource[];
  createdAtMs: number;
}

export interface TeamSession {
  user: TeamUser;
  csrfToken: string;
  spaces: TeamSpace[];
}

export interface TeamProjectMember {
  id: string;
  username: string;
  displayName: string;
  active: boolean;
  role: TeamProjectMemberRole;
}

export interface TeamMemberInput {
  username: string;
  password: string;
  displayName?: string;
  role?: TeamUserRole;
}

export interface TeamProjectMemberInput {
  userId: string;
  role: TeamProjectMemberInputRole;
}

export interface TeamProjectBrief {
  revision: number;
  objective: string;
  acceptanceCriteria: string[];
  updatedAtMs: number;
  updatedByUserId: string;
  updatedByDisplayName: string;
}

export interface TeamProjectBriefUpdateInput {
  baseRevision: number;
  objective: string;
  acceptanceCriteria: string[];
}

export interface TeamSessionRequirementsInput {
  baseRevision: number;
  revision: number;
}

export interface TeamSessionRequirementsResult {
  sessionId: string;
  requirementsRevision: number;
  previousRequirementsRevision: number;
}

/** Project Session metadata retained by TeamGateway for requirements adoption. */
export type TeamProjectSession = SessionSummary & {
  requirementsRevision?: number;
  currentRequirementsRevision?: number;
  requirementsStale?: boolean;
};

export interface TeamProjectRoomWorkItem {
  id: string;
  roomId: string;
  objective: string;
  state: string;
  currentOwnerParticipantId: string;
  currentOwnerUserId: string;
  currentOwnerDisplayName: string;
  ownerSessionId?: string;
  ownerRequirementsRevision?: number;
  requirementsStale?: boolean;
  expectedOutput: string;
  updatedAtMs: number;
}

export interface TeamProjectRoom {
  id: string;
  title: string;
  status: string;
  updatedAtMs: number;
  participantCount: number;
  workItems: TeamProjectRoomWorkItem[];
}

export interface TeamProjectRepository {
  branch: string;
  headCommit: string;
  revision: number;
}

export interface TeamProjectRuntime {
  configured: boolean;
}

export type TeamProjectPreviewStatus =
  | 'starting'
  | 'ready'
  | 'retained'
  | 'failed'
  | 'stopped'
  | 'recovery_required';

export interface TeamProjectDeployment {
  id: string;
  status: TeamProjectPreviewStatus;
  branch: string;
  commit: string;
  requirementsRevision: number;
  requestedByDisplayName: string;
  createdAtMs: number;
  readyAtMs?: number;
  error?: string;
}

export interface TeamProjectPreview {
  configured: boolean;
  active: TeamProjectDeployment | null;
  latest: TeamProjectDeployment | null;
  currentRequirementsRevision: number;
  repositoryHead: string | null;
  canManage: boolean;
  openPath: string | null;
}

export interface TeamProjectOverviewTruncated {
  rooms: boolean;
  workItems: boolean;
  drafts: boolean;
  briefHistory: boolean;
}

export interface TeamProjectOverview {
  project: Pick<TeamSpace, 'id' | 'name' | 'role'>;
  brief: TeamProjectBrief;
  briefHistory: TeamProjectBrief[];
  members: TeamProjectMember[];
  rooms: TeamProjectRoom[];
  drafts: TeamProjectDraft[];
  repository: TeamProjectRepository | null;
  runtime: TeamProjectRuntime;
  truncated: TeamProjectOverviewTruncated;
}

/** An immutable workspace snapshot that a project member can review. */
export interface TeamProjectDraft {
  draftId: string;
  spaceId?: string;
  targetBranch?: string;
  workspaceId?: string;
  sessionId: string;
  creatorUserId: string;
  creatorDisplayName?: string;
  title: string;
  baseCommit: string;
  baseRevision?: number;
  draftCommit: string;
  manifestHash: string;
  manifest?: unknown[];
  status?: 'pending' | 'integrated' | 'conflict' | 'verification_failed' | string;
  createdAtMs: number;
  requirementsRevision?: number;
  currentRequirementsRevision?: number;
  requirementsStale?: boolean;
  integratedAtMs?: number;
  integratedCommit?: string;
}

export type TeamIntegrationStatus = 'integrated' | 'conflict' | 'verification_failed';

export interface TeamProjectIntegration {
  status: TeamIntegrationStatus;
  draftId?: string;
  spaceId?: string;
  targetBranch?: string;
  expectedHead?: string;
  headCommit?: string;
  candidateCommit?: string | null;
  message?: string;
  verifier?: unknown;
  [key: string]: unknown;
}

export type TeamAdoptionStatus = 'adopted' | 'conflict';

export interface TeamProjectAdoption {
  status: TeamAdoptionStatus;
  adoptionId?: string;
  draftId?: string;
  spaceId?: string;
  workspaceId?: string;
  targetSessionId?: string;
  sourceDraftCommit?: string;
  targetTreeCommit?: string;
  message?: string;
  [key: string]: unknown;
}

/** Bounded plain-text diff returned by the project delivery review endpoint. */
export interface TeamProjectDiff {
  draftId: string;
  baseCommit: string;
  draftCommit: string;
  diff: string;
  truncated: boolean;
}
