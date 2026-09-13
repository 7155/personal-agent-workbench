import type { GeneratedContractName } from '@/contracts/generated';
import type { UiControlEvent } from '@/contracts/ui-events';

import {
  CONTROL_ROUTES,
  assertAllowedBody,
  assertAllowedQuery,
  controlRoute,
  resolveControlPath,
  type ControlPathId,
  type SubscriptionPathId,
} from './routes';

export type ControlTransportKind = 'http' | 'native' | 'mock';
export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type ControlQueryValue = string | number | boolean;

export interface ControlRequest<Body extends JsonValue = JsonValue> {
  pathId: ControlPathId;
  params?: Readonly<Record<string, string>>;
  query?: Readonly<Record<string, ControlQueryValue>>;
  body?: Body;
  responseContract?: GeneratedContractName;
  signal?: AbortSignal;
  /** Client observation deadline only; never forwarded in the native/backend payload. */
  timeoutMs?: number;
}

export interface ControlSubscription {
  pathId: SubscriptionPathId;
  params?: Readonly<Record<string, string>>;
  query?: Readonly<Record<string, ControlQueryValue>>;
  lastEventId: string;
}

export interface ControlReconnectNotice {
  attempt: number;
  delayMs: number;
  lastEventId: string;
}

export interface ControlEventObserver<Event = UiControlEvent | unknown> {
  next(event: Event): void;
  open?(lastEventId: string): void;
  /** The stream delivered a complete keepalive frame or a validated durable event. */
  stable?(lastEventId: string): void;
  error?(error: Error): void;
  reconnect?(notice: ControlReconnectNotice): void;
  snapshotRequired?(event: Event): void;
}

export interface FrontendCapabilities {
  schemaVersion: string;
  transport: ControlTransportKind;
  routeIds: readonly ControlPathId[];
  features: Readonly<Record<string, boolean>>;
  native: {
    pickFiles: boolean;
    managedAgentImageImport: boolean;
    knowledgeDocumentImport?: boolean;
    knowledgeParserStatus?: boolean;
    knowledgeAssetRead?: boolean;
    knowledgeDocumentSourceRead?: boolean;
    revealPath: boolean;
    approvedExternalActions: boolean;
    keychain: boolean;
    tcc: boolean;
  };
  raw?: unknown;
}

export interface FilePickOptions {
  accepts?: readonly string[];
  multiple?: boolean;
  purpose:
    | 'attachment'
    | 'configuration-import'
    | 'restore'
    | 'export-destination'
    | 'workspace-root'
    | 'knowledge-import'
    | 'plugin-source'
    | 'room-artifact';
  selection?: 'file' | 'directory';
  /** Required for attachment imports; the native host binds every receipt to this Agent session. */
  sessionId?: string;
  /** Required instead of sessionId for Room-owned attachment imports. */
  roomId?: string;
  /** Required only for a native knowledge-import picker. */
  kbId?: string;
  parserProvider?: 'auto' | 'builtin' | 'mineru_local_http';
  maxFiles?: number;
  signal?: AbortSignal;
}

export interface PickedFile {
  id: string;
  name: string;
  mimeType: string;
  byteSize: number;
  path?: string;
  /** Present only for an owner-bound managed-media import receipt. */
  sessionId?: string;
  roomId?: string;
  sha256?: string;
}

export type AgentImagePasteOptions = {
  /**
   * Browser transports import these File bytes through the fixed managed-media
   * endpoint. Native transport treats them only as paste evidence and reads the
   * trusted system pasteboard instead.
   */
  files?: readonly File[];
  maxFiles?: number;
} & (
  | { sessionId: string; roomId?: never }
  | { roomId: string; sessionId?: never }
);

export interface KnowledgeDocumentImportInput {
  kbId: string;
  accepts?: readonly string[];
  maxFiles?: number;
  /** Browser transport streams these File objects; native transport always opens its own picker. */
  files?: readonly File[];
  parserProvider?: 'auto' | 'builtin' | 'mineru_local_http';
  /** UI-facing parser alias; native transport maps mineru to the local HTTP provider. */
  parser?: 'auto' | 'builtin' | 'mineru';
  signal?: AbortSignal;
}

export interface KnowledgeDocumentImportReceipt {
  kbId: string;
  documentId: string;
  fileName: string;
  mimeType: string;
  byteSize: number;
  sha256: string;
  status: string;
}

export interface KnowledgeAssetReadInput {
  kbId: string;
  fileId: string;
  assetId: string;
  signal?: AbortSignal;
}

export interface KnowledgeAssetPayload {
  kbId: string;
  fileId: string;
  assetId: string;
  mimeType: string;
  byteSize: number;
  sha256: string;
  blob: Blob;
}

export interface KnowledgeDocumentSourceReadInput {
  kbId: string;
  fileId: string;
  signal?: AbortSignal;
}

export interface KnowledgeDocumentSourcePayload {
  kbId: string;
  fileId: string;
  mimeType: string;
  byteSize: number;
  sha256: string;
  blob: Blob;
}

export type ApprovedExternalActionId =
  | 'open_accessibility_settings'
  | 'register_input_source'
  | 'restart_sidecar'
  | 'restart_predictor'
  | 'redeploy_rime';

export type VoiceProviderId =
  | 'native_streaming'
  | 'realtime_websocket'
  | 'http_transcription';

export type VoiceNativeActionId =
  | 'start_agent'
  | 'stop_agent'
  | 'reload_configuration'
  | 'request_microphone_permission'
  | 'request_accessibility_permission'
  | 'open_microphone_settings'
  | 'open_accessibility_settings';

export interface VoiceCredentialSaveRequest {
  provider: VoiceProviderId;
  accessToken?: string;
  appId: string;
  resourceId: string;
  endpoint: string;
  model: string;
  headersJson: string;
}

export interface VoiceCredentialStatus {
  provider: VoiceProviderId;
  configured: boolean;
}

export interface VoiceNativeStatus {
  running: boolean;
  state: string;
  statusText: string;
  microphoneAuthorization: string;
  accessibilityTrusted: boolean;
  hotkeyInstalled: boolean;
  hotkeyMode: string;
  updatedAtMs: number;
}

export interface VoiceNativeActionReceipt {
  action: VoiceNativeActionId;
  accepted: boolean;
  status: VoiceNativeStatus;
  error?: string;
}

export interface ExternalActionRequest {
  action: ApprovedExternalActionId;
  receiptId: string;
  payloadSha256: string;
  commandSha256: string;
}

export interface ExternalActionReceipt {
  receiptId: string;
  action: ApprovedExternalActionId;
  accepted: boolean;
  completed: boolean;
  exitCode?: number;
  error?: string;
}

export interface ControlTransport {
  readonly kind: ControlTransportKind;
  /** Stable, non-secret endpoint identity for connection-scoped UI recovery. */
  readonly connectionIdentity?: string;
  capabilities(): Promise<FrontendCapabilities>;
  request<Response = unknown>(request: ControlRequest): Promise<Response>;
  browserSnapshotImageUrl?(snapshotId: string): string;
  agentMediaContentUrl?(receiptPath: string): string;
  subscribe<Event = UiControlEvent | unknown>(
    request: ControlSubscription,
    observer: ControlEventObserver<Event>,
  ): () => void;
  pickFiles?(options: FilePickOptions): Promise<PickedFile[]>;
  pasteImages?(options: AgentImagePasteOptions): Promise<PickedFile[]>;
  importKnowledgeDocuments?(
    input: KnowledgeDocumentImportInput,
  ): Promise<KnowledgeDocumentImportReceipt[]>;
  readKnowledgeAsset?(input: KnowledgeAssetReadInput): Promise<KnowledgeAssetPayload>;
  readKnowledgeDocumentSource?(
    input: KnowledgeDocumentSourceReadInput,
  ): Promise<KnowledgeDocumentSourcePayload>;
  revealPath?(path: string): Promise<void>;
  runApprovedExternalAction?(request: ExternalActionRequest): Promise<ExternalActionReceipt>;
  voiceCredentialStatus?(provider: VoiceProviderId): Promise<VoiceCredentialStatus>;
  saveVoiceCredentials?(request: VoiceCredentialSaveRequest): Promise<VoiceCredentialStatus>;
  voiceStatus?(): Promise<VoiceNativeStatus>;
  runVoiceAction?(action: VoiceNativeActionId): Promise<VoiceNativeActionReceipt>;
  dispose?(): void;
}

export function assertBrowserSnapshotId(snapshotId: unknown): asserts snapshotId is string {
  if (
    typeof snapshotId !== 'string'
    || !/^[A-Za-z0-9][A-Za-z0-9:._-]{0,159}$/.test(snapshotId)
  ) {
    throw new TypeError('Browser snapshot image requires a bounded snapshotId');
  }
}

export function managedAgentMediaContentPath(value: unknown): string | null {
  if (typeof value !== 'string' || !value.startsWith('/api/agent/media/')) return null;
  try {
    const url = new URL(value, 'http://rag-ime.local');
    const match = /^\/api\/agent\/media\/([^/]+)\/content$/u.exec(url.pathname);
    if (!match || url.hash) return null;
    const mediaId = decodeURIComponent(match[1] ?? '');
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/u.test(mediaId)) return null;
    const sessionIds = url.searchParams.getAll('sessionId');
    const roomIds = url.searchParams.getAll('roomId');
    const owner = sessionIds.length === 1 && roomIds.length === 0
      ? { key: 'sessionId', value: sessionIds[0] ?? '' }
      : roomIds.length === 1 && sessionIds.length === 0
        ? { key: 'roomId', value: roomIds[0] ?? '' }
        : null;
    if (
      !owner
      || !/^[A-Za-z0-9._:-]{1,240}$/u.test(owner.value)
      || [...url.searchParams.keys()].some((key) => key !== owner.key)
    ) {
      return null;
    }
    return `/api/agent/media/${encodeURIComponent(mediaId)}/content?${owner.key}=${encodeURIComponent(owner.value)}`;
  } catch {
    return null;
  }
}

const allowedRequestKeys = new Set([
  'pathId',
  'params',
  'query',
  'body',
  'responseContract',
  'signal',
  'timeoutMs',
]);

export function assertControlRequest(request: ControlRequest): void {
  for (const key of Object.keys(request)) {
    if (!allowedRequestKeys.has(key)) {
      throw new TypeError(`ControlRequest field is not allowed: ${key}`);
    }
  }
  if (request.timeoutMs !== undefined && (!Number.isSafeInteger(request.timeoutMs) || request.timeoutMs < 1 || request.timeoutMs > 300_000)) {
    throw new TypeError('ControlRequest timeoutMs must be an integer between 1 and 300000');
  }
  const route = controlRoute(request.pathId);
  if (route.binary) {
    throw new TypeError(`Use the typed binary transport method for ${request.pathId}`);
  }
  resolveControlPath(request.pathId, request.params);
  assertAllowedQuery(request.pathId, request.query);
  assertAllowedBody(request.pathId, request.body);
}

export function assertControlSubscription(request: ControlSubscription): void {
  const route = controlRoute(request.pathId);
  if (!route.subscription) {
    throw new TypeError(`Control path is not subscribable: ${request.pathId}`);
  }
  if (typeof request.lastEventId !== 'string') {
    throw new TypeError('ControlSubscription.lastEventId is required');
  }
  resolveControlPath(request.pathId, request.params);
  assertAllowedQuery(request.pathId, {
    ...(request.query ?? {}),
    lastEventId: request.lastEventId,
  });
}

export function controlRequestWirePayload(request: ControlRequest): object {
  assertControlRequest(request);
  return {
    pathId: request.pathId,
    ...(request.params ? { params: request.params } : {}),
    ...(request.query ? { query: stringifyControlQuery(request.query) } : {}),
    ...(request.body !== undefined ? { body: request.body } : {}),
  };
}

export function controlSubscriptionWirePayload(request: ControlSubscription): object {
  assertControlSubscription(request);
  return {
    pathId: request.pathId,
    ...(request.params ? { params: request.params } : {}),
    ...(request.query ? { query: stringifyControlQuery(request.query) } : {}),
    lastEventId: request.lastEventId,
  };
}

function stringifyControlQuery(
  query: Readonly<Record<string, ControlQueryValue>>,
): Record<string, string> {
  return Object.fromEntries(
    Object.entries(query).map(([key, value]) => [key, String(value)]),
  );
}

export function browserCapabilities(raw?: unknown): FrontendCapabilities {
  const payload = isRecord(raw) ? raw : {};
  const featureSource = isRecord(payload.features) ? payload.features : {};
  const features = Object.fromEntries(
    Object.entries(featureSource).filter((entry): entry is [string, boolean] => {
      return typeof entry[1] === 'boolean';
    }),
  );
  return {
    schemaVersion:
      typeof payload.schemaVersion === 'string'
        ? payload.schemaVersion
        : 'rag-ime.control-capabilities.v1',
    transport: 'http',
    routeIds: capabilityRouteIds(payload),
    features,
    native: {
      pickFiles: false,
      managedAgentImageImport: false,
      knowledgeDocumentImport: true,
      knowledgeParserStatus: true,
      knowledgeAssetRead: true,
      knowledgeDocumentSourceRead: true,
      revealPath: false,
      approvedExternalActions: false,
      keychain: false,
      tcc: false,
    },
    raw,
  };
}

function capabilityRouteIds(payload: Record<string, unknown>): ControlPathId[] {
  if (Array.isArray(payload.routes)) {
    return payload.routes
      .map((route) => (isRecord(route) ? route.pathId : undefined))
      .filter(
        (pathId): pathId is ControlPathId =>
          typeof pathId === 'string' && Object.hasOwn(CONTROL_ROUTES, pathId),
      );
  }
  return Object.keys(CONTROL_ROUTES) as ControlPathId[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}


declare global {
  interface Window {
    pawVoiceHost?: {
      status(): Promise<VoiceNativeStatus>;
      credentialStatus(provider: VoiceProviderId): Promise<VoiceCredentialStatus>;
      saveCredentials(request: VoiceCredentialSaveRequest): Promise<VoiceCredentialStatus>;
      action(action: VoiceNativeActionId): Promise<VoiceNativeActionReceipt>;
    };
  }
}
