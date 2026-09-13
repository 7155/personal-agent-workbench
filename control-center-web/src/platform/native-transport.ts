import {
  MAX_COMPOSER_ATTACHMENT_BYTES,
  isComposerAttachmentMimeType,
} from '@/contracts/attachment-policy';
import {
  loadContractValidationRuntime,
  type ContractValidationRuntime,
} from '@/contracts/validation-runtime';

import {
  controlRoute,
  isControlPathId,
  type ControlPathId,
  type ControlStreamKind,
} from './routes';
import type {
  NativeBridgeError,
  NativeBridgeMethod,
  NativeBridgeOutboundEnvelope,
  NativeBridgeRequestEnvelope,
  RagImeNativeBridgeReceiver,
  RagImeNativeMessageHandler,
} from './native-bridge';
import {
  assertBrowserSnapshotId,
  assertControlRequest,
  assertControlSubscription,
  managedAgentMediaContentPath,
  controlRequestWirePayload,
  controlSubscriptionWirePayload,
  type AgentImagePasteOptions,
  type ControlEventObserver,
  type ControlRequest,
  type ControlSubscription,
  type ControlTransport,
  type ExternalActionReceipt,
  type ExternalActionRequest,
  type FilePickOptions,
  type FrontendCapabilities,
  type KnowledgeDocumentImportInput,
  type KnowledgeDocumentImportReceipt,
  type KnowledgeAssetPayload,
  type KnowledgeAssetReadInput,
  type KnowledgeDocumentSourcePayload,
  type KnowledgeDocumentSourceReadInput,
  type PickedFile,
  type VoiceCredentialSaveRequest,
  type VoiceCredentialStatus,
  type VoiceNativeActionId,
  type VoiceNativeActionReceipt,
  type VoiceNativeStatus,
  type VoiceProviderId,
} from './transport';

export interface NativeControlTransportOptions {
  bridgeWindow?: Window;
  requestTimeoutMs?: number;
  createId?: () => string;
}

interface PendingRequest {
  resolve(value: unknown): void;
  reject(error: Error): void;
  timer?: ReturnType<typeof setTimeout>;
  removeAbortListener?: () => void;
}

interface NativeSubscription {
  request: ControlSubscription;
  observer: ControlEventObserver<unknown>;
  validationRuntime: ContractValidationRuntime | null;
  lastEventId: string;
  uncommittedEventId?: string;
  deliveryBlocked: boolean;
  reconnectBarrier?: Promise<void>;
  reconnectAttempt: number;
  reconnectTimer?: ReturnType<typeof setTimeout>;
}

export class NativeBridgeUnavailableError extends Error {
  constructor(message = 'ragImeNativeBridge is unavailable') {
    super(message);
    this.name = 'NativeBridgeUnavailableError';
  }
}

export class NativeBridgeCallError extends Error {
  readonly code?: string;
  readonly details?: unknown;

  constructor(error: NativeBridgeError) {
    const payload = typeof error === 'string' ? { message: error } : error;
    super(payload.message);
    this.name = 'NativeBridgeCallError';
    this.code = payload.code;
    this.details = payload.details;
  }
}

export class NativeControlTransport implements ControlTransport {
  readonly kind = 'native' as const;

  private readonly bridgeWindow: Window;
  private readonly handler: RagImeNativeMessageHandler;
  private readonly requestTimeoutMs: number;
  private readonly createId: () => string;
  private readonly pending = new Map<string, PendingRequest>();
  private readonly subscriptions = new Map<string, NativeSubscription>();
  private readonly receiver: RagImeNativeBridgeReceiver;
  private readonly previousReceiver: RagImeNativeBridgeReceiver | undefined;
  private cancellationSequence = 0;
  private disposed = false;

  constructor(options: NativeControlTransportOptions = {}) {
    this.bridgeWindow = options.bridgeWindow ?? window;
    const handler = this.bridgeWindow.webkit?.messageHandlers?.ragImeNativeBridge;
    if (!handler || typeof handler.postMessage !== 'function') {
      throw new NativeBridgeUnavailableError();
    }
    this.handler = handler;
    this.requestTimeoutMs = clamp(options.requestTimeoutMs ?? 15_000, 100, 120_000);
    this.createId = options.createId ?? createBridgeId;
    this.previousReceiver = this.bridgeWindow.__RAG_IME_NATIVE_BRIDGE__;
    this.receiver = { receive: (envelope) => this.receive(envelope) };
    this.bridgeWindow.__RAG_IME_NATIVE_BRIDGE__ = this.receiver;
  }

  async capabilities(): Promise<FrontendCapabilities> {
    const raw = await this.call('capabilities', {});
    return normalizeNativeCapabilities(raw);
  }

  async request<Response = unknown>(request: ControlRequest): Promise<Response> {
    assertControlRequest(request);
    const slowSessionRequestTimeoutMs = request.pathId === 'agent.session.commands'
      || request.pathId === 'agent.session.models'
      || request.pathId === 'agent.session.forks.list'
      || request.pathId === 'agent.session.forks.create'
      ? Math.max(this.requestTimeoutMs, 45_000)
      : this.requestTimeoutMs;
    const result = await this.call(
      'request',
      controlRequestWirePayload(request),
      request.signal,
      request.timeoutMs ?? slowSessionRequestTimeoutMs,
    );
    const contract = request.responseContract ?? controlRoute(request.pathId).responseContract;
    if (!contract) return result as Response;
    const { parseContract } = await loadContractValidationRuntime();
    return parseContract(contract, result) as Response;
  }

  browserSnapshotImageUrl(snapshotId: string): string {
    assertBrowserSnapshotId(snapshotId);
    return `http://127.0.0.1:8766/api/browser/snapshots/${encodeURIComponent(snapshotId)}/image`;
  }

  agentMediaContentUrl(receiptPath: string): string {
    const managedPath = managedAgentMediaContentPath(receiptPath);
    if (!managedPath) throw new TypeError('Agent media content requires a managed receipt path');
    return new URL(managedPath, 'http://127.0.0.1:8766').toString();
  }

  subscribe<Event = unknown>(
    request: ControlSubscription,
    observer: ControlEventObserver<Event>,
  ): () => void {
    assertControlSubscription(request);
    this.assertActive();
    const subscriptionId = this.createId();
    const subscription: NativeSubscription = {
      request,
      observer: observer as ControlEventObserver<unknown>,
      validationRuntime: null,
      lastEventId: request.lastEventId,
      deliveryBlocked: false,
      reconnectAttempt: 0,
    };
    this.subscriptions.set(subscriptionId, subscription);
    void loadContractValidationRuntime()
      .then(async (validationRuntime) => {
        if (this.disposed || this.subscriptions.get(subscriptionId) !== subscription) return false;
        subscription.validationRuntime = validationRuntime;
        await this.call('subscribe', {
          subscriptionId,
          request: controlSubscriptionWirePayload(request),
        });
        return true;
      })
      .then((opened) => {
        if (!opened) return;
        observer.open?.(request.lastEventId);
        // The native host acknowledges subscribe only after its SSE source is
        // established, so this preserves native open semantics while hooks no
        // longer mistake raw browser HTTP headers for recovery.
        observer.stable?.(request.lastEventId);
      })
      .catch((error) => {
        if (this.subscriptions.get(subscriptionId) === subscription) this.subscriptions.delete(subscriptionId);
        observer.error?.(asError(error));
      });

    return () => this.cancelSubscription(subscriptionId);
  }

  async pickFiles(options: FilePickOptions): Promise<PickedFile[]> {
    assertFilePickOptions(options);
    const { signal, ...payload } = options;
    const result = await this.call('pickFiles', payload, signal, null);
    if (!Array.isArray(result)) throw new NativeBridgeCallError('pickFiles returned a non-array');
    if (result.length > (options.maxFiles ?? (options.multiple ? 8 : 1))) {
      throw new NativeBridgeCallError('pickFiles returned too many file receipts');
    }
    return result.map((value) => parsePickedFile(value, options));
  }

  async pasteImages(options: AgentImagePasteOptions): Promise<PickedFile[]> {
    const maxFiles = assertAgentImagePasteOptions(options);
    const owner = options.roomId
      ? { roomId: options.roomId }
      : { sessionId: options.sessionId };
    const result = await this.call('pasteImages', {
      ...owner,
      maxFiles,
    });
    if (!Array.isArray(result) || result.length > maxFiles) {
      throw new NativeBridgeCallError('pasteImages returned an invalid receipt list');
    }
    return result.map((value) => parsePickedFile(value, {
      purpose: 'attachment',
      ...owner,
      maxFiles,
    }));
  }

  async importKnowledgeDocuments(
    input: KnowledgeDocumentImportInput,
  ): Promise<KnowledgeDocumentImportReceipt[]> {
    const maxFiles = assertNativeKnowledgeDocumentImportInput(input);
    const parserProvider = input.parserProvider ?? (input.parser === 'mineru' ? 'mineru_local_http' : input.parser);
    const result = await this.call(
      'pickFiles',
      {
        purpose: 'knowledge-import',
        kbId: input.kbId,
        accepts: input.accepts ?? [],
        multiple: maxFiles > 1,
        maxFiles,
        ...(parserProvider ? { parserProvider } : {}),
      },
      input.signal,
      null,
    );
    if (!Array.isArray(result) || result.length > maxFiles) {
      throw new NativeBridgeCallError('knowledge import returned an invalid receipt list');
    }
    return result.map((value) => parseKnowledgeDocumentImportReceipt(value, input.kbId));
  }

  async readKnowledgeAsset(input: KnowledgeAssetReadInput): Promise<KnowledgeAssetPayload> {
    assertNativeKnowledgeAssetReadInput(input);
    const result = await this.call(
      'readKnowledgeAsset',
      { kbId: input.kbId, fileId: input.fileId, assetId: input.assetId },
      input.signal,
      120_000,
    );
    return parseNativeKnowledgeAsset(result, input);
  }

  async readKnowledgeDocumentSource(
    input: KnowledgeDocumentSourceReadInput,
  ): Promise<KnowledgeDocumentSourcePayload> {
    assertNativeKnowledgeDocumentSourceReadInput(input);
    const result = await this.call(
      'readKnowledgeDocumentSource',
      { kbId: input.kbId, fileId: input.fileId },
      input.signal,
      120_000,
    );
    return parseNativeKnowledgeDocumentSource(result, input);
  }

  async revealPath(path: string): Promise<void> {
    if (!path || path.length > 4096 || path.includes('\u0000')) {
      throw new TypeError('revealPath requires a bounded local path');
    }
    await this.call('revealPath', { path });
  }

  async runApprovedExternalAction(
    request: ExternalActionRequest,
  ): Promise<ExternalActionReceipt> {
    const result = await this.call('runApprovedExternalAction', request, undefined, 120_000);
    if (!isRecord(result) || typeof result.receiptId !== 'string') {
      throw new NativeBridgeCallError('external action returned an invalid receipt');
    }
    return result as unknown as ExternalActionReceipt;
  }

  async voiceCredentialStatus(provider: VoiceProviderId): Promise<VoiceCredentialStatus> {
    const result = await this.call('voiceCredentialStatus', { provider });
    return parseVoiceCredentialStatus(result, provider);
  }

  async saveVoiceCredentials(request: VoiceCredentialSaveRequest): Promise<VoiceCredentialStatus> {
    assertVoiceCredentialSaveRequest(request);
    const result = await this.call('voiceCredentialSave', request);
    return parseVoiceCredentialStatus(result, request.provider);
  }

  async runVoiceAction(action: VoiceNativeActionId): Promise<VoiceNativeActionReceipt> {
    assertVoiceNativeAction(action);
    const result = await this.call('voiceAction', { action });
    if (!isRecord(result) || result.action !== action || typeof result.accepted !== 'boolean') {
      throw new NativeBridgeCallError('voice action returned an invalid receipt');
    }
    return {
      action,
      accepted: result.accepted,
      status: parseVoiceNativeStatus(result.status),
      ...(typeof result.error === 'string' && result.error ? { error: result.error } : {}),
    };
  }

  dispose(): void {
    if (this.disposed) return;
    for (const subscriptionId of [...this.subscriptions.keys()]) {
      this.cancelSubscription(subscriptionId);
    }
    for (const [id, item] of this.pending) {
      if (item.timer !== undefined) globalThis.clearTimeout(item.timer);
      item.removeAbortListener?.();
      this.postCancellation(id);
      item.reject(new NativeBridgeUnavailableError('native transport was disposed'));
      this.pending.delete(id);
    }
    this.disposed = true;
    if (this.bridgeWindow.__RAG_IME_NATIVE_BRIDGE__ === this.receiver) {
      this.bridgeWindow.__RAG_IME_NATIVE_BRIDGE__ = this.previousReceiver;
    }
  }

  private call(
    method: NativeBridgeMethod,
    payload: unknown,
    signal?: AbortSignal,
    timeoutMs: number | null = this.requestTimeoutMs,
  ): Promise<unknown> {
    this.assertActive();
    if (signal?.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'));
    const id = this.createId();

    return new Promise((resolve, reject) => {
      const timer = timeoutMs === null ? undefined : globalThis.setTimeout(() => {
        const item = this.pending.get(id);
        if (!item) return;
        item.removeAbortListener?.();
        this.pending.delete(id);
        this.postCancellation(id);
        reject(new NativeBridgeCallError({ code: 'timeout', message: `${method} timed out` }));
      }, timeoutMs);

      const pending: PendingRequest = { resolve, reject, timer };
      if (signal) {
        const abort = (): void => {
          if (timer !== undefined) globalThis.clearTimeout(timer);
          this.pending.delete(id);
          this.postCancellation(id);
          reject(new DOMException('Aborted', 'AbortError'));
        };
        signal.addEventListener('abort', abort, { once: true });
        pending.removeAbortListener = () => signal.removeEventListener('abort', abort);
      }
      this.pending.set(id, pending);

      const envelope: NativeBridgeRequestEnvelope = { id, method, payload };
      try {
        this.handler.postMessage(envelope);
      } catch (error) {
        if (timer !== undefined) globalThis.clearTimeout(timer);
        pending.removeAbortListener?.();
        this.pending.delete(id);
        reject(asError(error));
      }
    });
  }

  private postCancellation(requestId: string): void {
    this.cancellationSequence += 1;
    try {
      this.handler.postMessage({
        id: `cancel:${this.cancellationSequence}`,
        method: 'cancelRequest',
        payload: { requestId },
      });
    } catch {
      // Cancellation remains best effort if the native host is already gone.
    }
  }

  private receive(envelope: NativeBridgeOutboundEnvelope): void {
    if (this.disposed) return;
    if ('id' in envelope) {
      const pending = this.pending.get(envelope.id);
      if (!pending) return;
      if (pending.timer !== undefined) globalThis.clearTimeout(pending.timer);
      pending.removeAbortListener?.();
      this.pending.delete(envelope.id);
      if (envelope.ok) pending.resolve(envelope.result);
      else pending.reject(new NativeBridgeCallError(envelope.error));
      return;
    }
    const subscription = this.subscriptions.get(envelope.subscriptionId);
    if (!subscription) return;
    // Once a consumer rejects event N, no later frame from that native stream
    // is safe to project. Wait until the host has cancelled that stream and a
    // fresh subscribe from the prior cursor has completed.
    if (subscription.deliveryBlocked) return;
    if (envelope.kind === 'error') {
      subscription.observer.error?.(new NativeBridgeCallError(envelope.error));
      if (isRetryableBridgeError(envelope.error)) {
        this.scheduleSubscriptionReconnect(envelope.subscriptionId, subscription);
      } else {
        this.subscriptions.delete(envelope.subscriptionId);
      }
      return;
    }
    if (envelope.kind === 'complete') {
      // Completion repeats the host cursor even when the preceding event was
      // not committed by the observer. Keep replay anchored before that event.
      if (envelope.lastEventId !== subscription.uncommittedEventId) {
        subscription.lastEventId = envelope.lastEventId || subscription.lastEventId;
      }
      this.scheduleSubscriptionReconnect(envelope.subscriptionId, subscription);
      return;
    }
    try {
      const streamKind = controlRoute(subscription.request.pathId).subscription;
      if (!subscription.validationRuntime) {
        throw new NativeBridgeCallError('contract validation runtime is not ready');
      }
      subscription.uncommittedEventId = envelope.lastEventId || undefined;
      const event = parseNativeEvent(subscription.validationRuntime, streamKind, envelope.event);
      const hostEventId = envelope.lastEventId || eventResumeToken(event);
      subscription.uncommittedEventId = hostEventId || undefined;
      const deliveredEventId = isSnapshotRequired(event)
        ? subscription.lastEventId
        : hostEventId || subscription.lastEventId;
      subscription.observer.next(event);
      if (isSnapshotRequired(event)) subscription.observer.snapshotRequired?.(event);
      // The reconnect cursor acknowledges projection delivery, not merely
      // receipt from the native host. Leave it unchanged when a consumer
      // throws so replay can redeliver the event.
      subscription.lastEventId = deliveredEventId;
      if (!isSnapshotRequired(event)) {
        subscription.uncommittedEventId = undefined;
        subscription.observer.stable?.(subscription.lastEventId);
      }
      subscription.reconnectAttempt = 0;
    } catch (error) {
      subscription.observer.error?.(asError(error));
      this.interruptSubscriptionAfterDeliveryFailure(envelope.subscriptionId, subscription);
    }
  }

  private cancelSubscription(subscriptionId: string): void {
    const subscription = this.subscriptions.get(subscriptionId);
    if (!subscription) return;
    this.subscriptions.delete(subscriptionId);
    if (subscription.reconnectTimer) globalThis.clearTimeout(subscription.reconnectTimer);
    if (this.disposed) return;
    void this.call('cancelSubscription', {
      subscriptionId,
      lastEventId: subscription.lastEventId,
    }).catch(() => {
      // Cancellation is idempotent and best-effort during host teardown.
    });
  }

  private assertActive(): void {
    if (this.disposed) throw new NativeBridgeUnavailableError('native transport was disposed');
  }

  private scheduleSubscriptionReconnect(
    subscriptionId: string,
    subscription: NativeSubscription,
  ): void {
    if (this.disposed || subscription.reconnectTimer) return;
    subscription.reconnectAttempt += 1;
    const delayMs = Math.min(5_000, 250 * 2 ** Math.min(5, subscription.reconnectAttempt - 1));
    subscription.observer.reconnect?.({
      attempt: subscription.reconnectAttempt,
      delayMs,
      lastEventId: subscription.lastEventId,
    });
    subscription.reconnectTimer = globalThis.setTimeout(() => {
      subscription.reconnectTimer = undefined;
      if (this.disposed || this.subscriptions.get(subscriptionId) !== subscription) return;
      void Promise.resolve(subscription.reconnectBarrier)
        .then(() => {
          if (this.disposed || this.subscriptions.get(subscriptionId) !== subscription) return false;
          const request = { ...subscription.request, lastEventId: subscription.lastEventId };
          return this.call('subscribe', {
            subscriptionId,
            request: controlSubscriptionWirePayload(request),
          }).then(() => true);
        })
        .then((opened) => {
          if (!opened || this.disposed || this.subscriptions.get(subscriptionId) !== subscription) return;
          subscription.deliveryBlocked = false;
          subscription.uncommittedEventId = undefined;
          subscription.observer.open?.(subscription.lastEventId);
          subscription.observer.stable?.(subscription.lastEventId);
        })
        .catch((error) => {
          subscription.observer.error?.(asError(error));
          this.scheduleSubscriptionReconnect(subscriptionId, subscription);
        });
    }, delayMs);
  }

  private interruptSubscriptionAfterDeliveryFailure(
    subscriptionId: string,
    subscription: NativeSubscription,
  ): void {
    if (subscription.deliveryBlocked) return;
    subscription.deliveryBlocked = true;
    subscription.reconnectBarrier = this.call('cancelSubscription', {
      subscriptionId,
      lastEventId: subscription.lastEventId,
    }).then(
      () => undefined,
      () => undefined,
    ).finally(() => {
      if (this.subscriptions.get(subscriptionId) === subscription) {
        subscription.reconnectBarrier = undefined;
      }
    });
    this.scheduleSubscriptionReconnect(subscriptionId, subscription);
  }
}

function parseNativeEvent(
  validationRuntime: ContractValidationRuntime,
  streamKind: ControlStreamKind | undefined,
  value: unknown,
): unknown {
  switch (streamKind) {
    case 'agent':
      return validationRuntime.parseAgentEvent(value);
    case 'room':
      return validationRuntime.parseRoomEvent(value);
    case 'kernel':
      return value;
    case 'control':
      return value;
    case 'observation':
      return validationRuntime.parseObservationEvent(value);
    default:
      throw new NativeBridgeCallError('event arrived for a non-subscription route');
  }
}

function normalizeNativeCapabilities(value: unknown): FrontendCapabilities {
  const payload = isRecord(value) ? value : {};
  const rawRouteIds = Array.isArray(payload.routeIds)
    ? payload.routeIds
    : Array.isArray(payload.routes)
      ? payload.routes.map((route) => (isRecord(route) ? route.pathId : undefined))
      : [];
  const routeIds = rawRouteIds.filter(isControlPathId);
  const rawFeatures = isRecord(payload.features) ? payload.features : {};
  const rawNative = isRecord(payload.native) ? payload.native : {};
  return {
    schemaVersion:
      typeof payload.schemaVersion === 'string'
        ? payload.schemaVersion
        : 'rag-ime.control-frontend-capabilities.v1',
    transport: 'native',
    routeIds,
    features: booleanRecord(rawFeatures),
    native: {
      pickFiles: rawNative.pickFiles === true || rawNative.filePicker === true,
      managedAgentImageImport: rawNative.managedAgentImageImport === true,
      knowledgeDocumentImport: rawNative.knowledgeDocumentImport === true,
      knowledgeParserStatus: rawNative.knowledgeParserStatus === true,
      knowledgeAssetRead: rawNative.knowledgeAssetRead === true,
      knowledgeDocumentSourceRead: rawNative.knowledgeDocumentSourceRead === true,
      revealPath: rawNative.revealPath === true,
      approvedExternalActions: rawNative.approvedExternalActions === true,
      keychain: rawNative.keychain === true || rawNative.keychainStatus === true,
      tcc: rawNative.tcc === true || rawNative.tccStatus === true,
    },
    raw: value,
  };
}

function parsePickedFile(value: unknown, options: FilePickOptions): PickedFile {
  if (
    !isRecord(value) ||
    typeof value.id !== 'string' ||
    typeof value.name !== 'string' ||
    typeof value.mimeType !== 'string' ||
    typeof value.byteSize !== 'number'
  ) {
    throw new NativeBridgeCallError('pickFiles returned an invalid file receipt');
  }
  if (
    !Number.isSafeInteger(value.byteSize)
    || value.byteSize < 0
    || (value.byteSize === 0
      && !['export-destination', 'workspace-root'].includes(options.purpose)
      && options.selection !== 'directory')
  ) {
    throw new NativeBridgeCallError('pickFiles returned an invalid byte size');
  }
  if (options.purpose === 'attachment') {
    if (
      !isComposerAttachmentMimeType(value.mimeType) ||
      value.byteSize > MAX_COMPOSER_ATTACHMENT_BYTES ||
      !managedReceiptMatchesOwner(value, options) ||
      typeof value.sha256 !== 'string' ||
      !/^[a-f0-9]{64}$/.test(value.sha256) ||
      !/^media_[A-Za-z0-9_-]{12,80}$/.test(value.id) ||
      'path' in value
    ) {
      throw new NativeBridgeCallError('pickFiles returned an invalid managed attachment receipt');
    }
  } else if (
    typeof value.path !== 'string'
    || !value.path.startsWith('/')
    || value.path.includes('\0')
  ) {
    throw new NativeBridgeCallError('pickFiles returned an invalid local path receipt');
  }
  return value as unknown as PickedFile;
}

function parseKnowledgeDocumentImportReceipt(
  value: unknown,
  kbId: string,
): KnowledgeDocumentImportReceipt {
  if (
    !isRecord(value) ||
    value.kbId !== kbId ||
    typeof value.documentId !== 'string' ||
    !/^[A-Za-z0-9][A-Za-z0-9:._-]{0,159}$/.test(value.documentId) ||
    typeof value.fileName !== 'string' ||
    !value.fileName ||
    value.fileName.length > 512 ||
    typeof value.mimeType !== 'string' ||
    typeof value.byteSize !== 'number' ||
    !Number.isSafeInteger(value.byteSize) ||
    value.byteSize <= 0 ||
    value.byteSize > 200 * 1024 * 1024 ||
    typeof value.sha256 !== 'string' ||
    !/^[a-f0-9]{64}$/.test(value.sha256) ||
    typeof value.status !== 'string' ||
    value.status.length === 0 ||
    'path' in value
  ) {
    throw new NativeBridgeCallError('knowledge import returned an invalid receipt');
  }
  return value as unknown as KnowledgeDocumentImportReceipt;
}

const KNOWLEDGE_ASSET_MIME_TYPES = new Set([
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
  'image/bmp',
]);
const MAX_KNOWLEDGE_ASSET_BYTES = 25 * 1024 * 1024;
const KNOWLEDGE_SOURCE_MIME_TYPES = new Set([
  'application/pdf',
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
  'image/bmp',
  'image/tiff',
  'text/plain',
  'text/markdown',
  'text/csv',
  'application/json',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
]);
const MAX_KNOWLEDGE_SOURCE_BYTES = 50 * 1024 * 1024;

function assertNativeKnowledgeAssetReadInput(input: KnowledgeAssetReadInput): void {
  if (!isRecord(input) || Object.keys(input).some((key) => !['kbId', 'fileId', 'assetId', 'signal'].includes(key))) {
    throw new TypeError('Knowledge asset read input contained an unsupported field');
  }
  if (!isSafeKnowledgeId(input.kbId) || !isSafeKnowledgeId(input.fileId)) {
    throw new TypeError('Knowledge asset read requires bounded kbId and fileId values');
  }
  if (!/^[a-f0-9]{64}$/.test(input.assetId)) {
    throw new TypeError('Knowledge asset read requires a sha256 assetId');
  }
}

function parseNativeKnowledgeAsset(
  value: unknown,
  input: KnowledgeAssetReadInput,
): KnowledgeAssetPayload {
  if (
    !isRecord(value) ||
    value.kbId !== input.kbId ||
    value.fileId !== input.fileId ||
    value.assetId !== input.assetId ||
    value.sha256 !== input.assetId ||
    typeof value.mimeType !== 'string' ||
    !KNOWLEDGE_ASSET_MIME_TYPES.has(value.mimeType) ||
    typeof value.byteSize !== 'number' ||
    !Number.isSafeInteger(value.byteSize) ||
    value.byteSize <= 0 ||
    value.byteSize > MAX_KNOWLEDGE_ASSET_BYTES ||
    !(value.blob instanceof Blob) ||
    value.blob.size !== value.byteSize ||
    value.blob.type !== value.mimeType
  ) {
    throw new NativeBridgeCallError('knowledge asset returned an invalid binary receipt');
  }
  return value as unknown as KnowledgeAssetPayload;
}

function assertNativeKnowledgeDocumentSourceReadInput(
  input: KnowledgeDocumentSourceReadInput,
): void {
  if (!isRecord(input) || Object.keys(input).some((key) => !['kbId', 'fileId', 'signal'].includes(key))) {
    throw new TypeError('Knowledge document source input contained an unsupported field');
  }
  if (!isSafeKnowledgeId(input.kbId) || !isSafeKnowledgeId(input.fileId)) {
    throw new TypeError('Knowledge document source requires bounded kbId and fileId values');
  }
}

function parseNativeKnowledgeDocumentSource(
  value: unknown,
  input: KnowledgeDocumentSourceReadInput,
): KnowledgeDocumentSourcePayload {
  if (
    !isRecord(value) ||
    value.kbId !== input.kbId ||
    value.fileId !== input.fileId ||
    typeof value.sha256 !== 'string' ||
    !/^[a-f0-9]{64}$/.test(value.sha256) ||
    typeof value.mimeType !== 'string' ||
    !KNOWLEDGE_SOURCE_MIME_TYPES.has(value.mimeType) ||
    typeof value.byteSize !== 'number' ||
    !Number.isSafeInteger(value.byteSize) ||
    value.byteSize <= 0 ||
    value.byteSize > MAX_KNOWLEDGE_SOURCE_BYTES ||
    !(value.blob instanceof Blob) ||
    value.blob.size !== value.byteSize ||
    value.blob.type !== value.mimeType
  ) {
    throw new NativeBridgeCallError('knowledge document source returned an invalid binary receipt');
  }
  return value as unknown as KnowledgeDocumentSourcePayload;
}

function isSafeKnowledgeId(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9:._-]{0,159}$/.test(value);
}

function hasExactlyOneManagedOwner(
  options: { sessionId?: unknown; roomId?: unknown },
): boolean {
  const valid = (value: unknown) => (
    typeof value === 'string'
    && /^[A-Za-z0-9][A-Za-z0-9:._-]{0,159}$/.test(value)
  );
  return valid(options.sessionId) !== valid(options.roomId)
    && (options.sessionId === undefined || valid(options.sessionId))
    && (options.roomId === undefined || valid(options.roomId));
}

function managedReceiptMatchesOwner(
  value: Record<string, unknown>,
  options: FilePickOptions,
): boolean {
  return options.roomId
    ? value.roomId === options.roomId && value.sessionId === undefined
    : value.sessionId === options.sessionId && value.roomId === undefined;
}

function assertAgentImagePasteOptions(options: AgentImagePasteOptions): number {
  if (!isRecord(options) || Object.keys(options).some((key) => !['sessionId', 'roomId', 'files', 'maxFiles'].includes(key))) {
    throw new TypeError('Agent image paste options contained an unsupported field');
  }
  if (!hasExactlyOneManagedOwner(options)) {
    throw new TypeError('Agent image paste requires exactly one bounded sessionId or roomId');
  }
  const files = options.files;
  if (files !== undefined && (!Array.isArray(files) || files.length < 1 || files.length > 8)) {
    throw new TypeError('Agent image paste files must contain between 1 and 8 images');
  }
  const maxFiles = options.maxFiles ?? files?.length;
  if (!Number.isSafeInteger(maxFiles) || !maxFiles || maxFiles < 1 || maxFiles > 8) {
    throw new TypeError('Agent image paste requires maxFiles between 1 and 8');
  }
  if (files && files.length > maxFiles) {
    throw new TypeError('Agent image paste files exceed maxFiles');
  }
  for (const file of files ?? []) {
    // Native paste reads the trusted system pasteboard; browser File objects
    // are evidence only, so any named non-empty file within the byte cap is
    // acceptable regardless of its (often missing) browser MIME type.
    if (
      typeof file?.name !== 'string' ||
      !file.name ||
      file.name.length > 512 ||
      file.name.includes('\u0000') ||
      !Number.isSafeInteger(file.size) ||
      file.size <= 0 ||
      file.size > MAX_COMPOSER_ATTACHMENT_BYTES
    ) {
      throw new TypeError('Agent file paste received an invalid file');
    }
  }
  return maxFiles;
}

function assertFilePickOptions(options: FilePickOptions): void {
  const allowedKeys = new Set([
    'accepts',
    'multiple',
    'purpose',
    'selection',
    'roomId',
    'sessionId',
    'kbId',
    'parserProvider',
    'maxFiles',
    'signal',
  ]);
  for (const key of Object.keys(options)) {
    if (!allowedKeys.has(key)) throw new TypeError(`FilePickOptions field is not allowed: ${key}`);
  }
  if (!['attachment', 'configuration-import', 'restore', 'export-destination', 'workspace-root', 'knowledge-import', 'plugin-source', 'room-artifact'].includes(options.purpose)) {
    throw new TypeError('FilePickOptions purpose is not allowlisted');
  }
  if (options.selection !== undefined && !['file', 'directory'].includes(options.selection)) {
    throw new TypeError('FilePickOptions selection is not allowlisted');
  }
  if (options.purpose === 'plugin-source' && options.selection !== 'directory') {
    throw new TypeError('plugin-source selection must be a directory');
  }
  if (options.accepts !== undefined && (!Array.isArray(options.accepts) || options.accepts.some((value) => typeof value !== 'string'))) {
    throw new TypeError('FilePickOptions accepts must contain strings');
  }
  if (options.multiple !== undefined && typeof options.multiple !== 'boolean') {
    throw new TypeError('FilePickOptions multiple must be a boolean');
  }
  if (options.maxFiles !== undefined && (!Number.isSafeInteger(options.maxFiles) || options.maxFiles < 1 || options.maxFiles > 8)) {
    throw new TypeError('FilePickOptions maxFiles must be between 1 and 8');
  }
  if (options.purpose === 'attachment') {
    if (!hasExactlyOneManagedOwner(options)) {
      throw new TypeError('attachment file selection requires exactly one bounded sessionId or roomId');
    }
    if (options.maxFiles === undefined) {
      throw new TypeError('attachment file selection requires maxFiles');
    }
  } else if (options.sessionId !== undefined || options.roomId !== undefined) {
    throw new TypeError('sessionId and roomId are only accepted for managed attachments');
  }
  if (options.purpose === 'knowledge-import') {
    if (typeof options.kbId !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$/.test(options.kbId)) {
      throw new TypeError('knowledge-import file selection requires a bounded kbId');
    }
  } else if (options.kbId !== undefined || options.parserProvider !== undefined) {
    throw new TypeError('kbId and parserProvider are only accepted for knowledge imports');
  }
}

function assertNativeKnowledgeDocumentImportInput(
  input: KnowledgeDocumentImportInput,
): number {
  if (!isRecord(input)) throw new TypeError('Knowledge import input is required');
  const allowedKeys = new Set(['kbId', 'accepts', 'maxFiles', 'files', 'parserProvider', 'parser', 'signal']);
  if (Object.keys(input).some((key) => !allowedKeys.has(key))) {
    throw new TypeError('Knowledge import input contained an unsupported field');
  }
  if (!/^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$/.test(input.kbId)) {
    throw new TypeError('Knowledge import requires a valid kbId');
  }
  if (input.files !== undefined) {
    throw new TypeError('Native knowledge import does not accept browser paths or File objects');
  }
  if (input.accepts !== undefined && (!Array.isArray(input.accepts) || input.accepts.length > 32 || input.accepts.some((value) => typeof value !== 'string'))) {
    throw new TypeError('Knowledge import accepts must be a bounded string array');
  }
  if (input.parserProvider !== undefined && !['auto', 'builtin', 'mineru_local_http'].includes(input.parserProvider)) {
    throw new TypeError('Knowledge import parserProvider is not allowlisted');
  }
  if (input.parser !== undefined && !['auto', 'builtin', 'mineru'].includes(input.parser)) {
    throw new TypeError('Knowledge import parser is not allowlisted');
  }
  if (input.parserProvider !== undefined && input.parser !== undefined) {
    throw new TypeError('Knowledge import accepts only one parser selector');
  }
  const maxFiles = input.maxFiles ?? 8;
  if (!Number.isSafeInteger(maxFiles) || maxFiles < 1 || maxFiles > 20) {
    throw new TypeError('Native knowledge import maxFiles must be between 1 and 20');
  }
  return maxFiles;
}

const VOICE_PROVIDERS = new Set<VoiceProviderId>([
  'native_streaming',
  'realtime_websocket',
  'http_transcription',
]);
const VOICE_NATIVE_ACTIONS = new Set<VoiceNativeActionId>([
  'start_agent',
  'stop_agent',
  'reload_configuration',
  'request_microphone_permission',
  'request_accessibility_permission',
  'open_microphone_settings',
  'open_accessibility_settings',
]);

function parseVoiceCredentialStatus(
  value: unknown,
  provider: VoiceProviderId,
): VoiceCredentialStatus {
  if (!isRecord(value) || value.provider !== provider || typeof value.configured !== 'boolean') {
    throw new NativeBridgeCallError('voice credential status returned an invalid receipt');
  }
  return { provider, configured: value.configured };
}

function assertVoiceCredentialSaveRequest(request: VoiceCredentialSaveRequest): void {
  if (!isRecord(request)) throw new TypeError('Voice credentials are required');
  const allowedKeys = new Set([
    'provider',
    'accessToken',
    'appId',
    'resourceId',
    'endpoint',
    'model',
    'headersJson',
  ]);
  if (Object.keys(request).some((key) => !allowedKeys.has(key))) {
    throw new TypeError('Voice credentials contained an unsupported field');
  }
  if (!VOICE_PROVIDERS.has(request.provider)) {
    throw new TypeError('Voice provider is not allowlisted');
  }
  for (const [key, value, limit] of [
    ['accessToken', request.accessToken ?? '', 16_384],
    ['appId', request.appId, 512],
    ['resourceId', request.resourceId, 512],
    ['endpoint', request.endpoint, 2_048],
    ['model', request.model, 512],
    ['headersJson', request.headersJson, 16_384],
  ] as const) {
    if (typeof value !== 'string' || value.length > limit || value.includes('\u0000')) {
      throw new TypeError(`Voice credential field ${key} is invalid`);
    }
  }
  if (request.headersJson.trim()) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(request.headersJson);
    } catch {
      throw new TypeError('Voice request headers must be valid JSON');
    }
    if (!isRecord(parsed) || Object.values(parsed).some((value) => typeof value !== 'string')) {
      throw new TypeError('Voice request headers must be a string dictionary');
    }
  }
}

function assertVoiceNativeAction(action: VoiceNativeActionId): void {
  if (!VOICE_NATIVE_ACTIONS.has(action)) {
    throw new TypeError('Voice action is not allowlisted');
  }
}

function parseVoiceNativeStatus(value: unknown): VoiceNativeStatus {
  if (
    !isRecord(value)
    || typeof value.running !== 'boolean'
    || typeof value.state !== 'string'
    || typeof value.statusText !== 'string'
    || typeof value.microphoneAuthorization !== 'string'
    || typeof value.accessibilityTrusted !== 'boolean'
    || typeof value.hotkeyInstalled !== 'boolean'
    || typeof value.hotkeyMode !== 'string'
    || typeof value.updatedAtMs !== 'number'
    || !Number.isSafeInteger(value.updatedAtMs)
  ) {
    throw new NativeBridgeCallError('voice action returned an invalid status');
  }
  return value as unknown as VoiceNativeStatus;
}

function booleanRecord(value: Record<string, unknown>): Record<string, boolean> {
  return Object.fromEntries(
    Object.entries(value).filter((entry): entry is [string, boolean] => {
      return typeof entry[1] === 'boolean';
    }),
  );
}

function eventResumeToken(event: unknown): string {
  if (!isRecord(event)) return '';
  if (typeof event.resumeToken === 'string') return event.resumeToken;
  if (typeof event.eventId === 'string') return event.eventId;
  return '';
}

function isSnapshotRequired(event: unknown): boolean {
  return isRecord(event) && event.eventType === 'snapshot_required';
}

function isRetryableBridgeError(error: NativeBridgeError): boolean {
  return typeof error === 'object' && error !== null && error.retryable === true;
}

function createBridgeId(): string {
  return globalThis.crypto.randomUUID();
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}
