export type PendingAppMessage = Readonly<{
  sessionId: string;
  ownerAppId: string;
  surfaceKey: string;
  clientMessageId: string;
  retryOfClientMessageId?: string;
  message: string;
}>;

export function createPendingAppMessage(input: {
  sessionId: string;
  ownerAppId: string;
  surfaceKey: string;
  message: string;
  createIdentity?: () => string;
}): PendingAppMessage {
  const createIdentity = input.createIdentity ?? (() => crypto.randomUUID());
  return Object.freeze({
    sessionId: required(input.sessionId, 'sessionId'),
    ownerAppId: required(input.ownerAppId, 'ownerAppId'),
    surfaceKey: required(input.surfaceKey, 'surfaceKey'),
    clientMessageId: `extension:${input.ownerAppId}:${input.surfaceKey}:${required(createIdentity(), 'identity')}`,
    message: required(input.message, 'message'),
  });
}

export function promptRequestFor(pending: PendingAppMessage) {
  return {
    pathId: 'agent.session.prompt' as const,
    params: { sessionId: pending.sessionId },
    body: {
      message: pending.message,
      clientMessageId: pending.clientMessageId,
      ...(pending.retryOfClientMessageId
        ? { retryOfClientMessageId: pending.retryOfClientMessageId }
        : {}),
      delivery: 'prompt' as const,
    },
  };
}

export function successorForDurablyFailedCommand(
  failed: PendingAppMessage,
  createIdentity: () => string = () => crypto.randomUUID(),
): PendingAppMessage {
  return Object.freeze({
    ...failed,
    clientMessageId: createClientMessageId(failed.ownerAppId, failed.surfaceKey, createIdentity),
    retryOfClientMessageId: failed.clientMessageId,
  });
}

export function newExecutionAfterAcceptedTurnFailure(
  accepted: PendingAppMessage,
  createIdentity: () => string = () => crypto.randomUUID(),
): PendingAppMessage {
  const { retryOfClientMessageId: _discardedLineage, ...semantic } = accepted;
  return Object.freeze({
    ...semantic,
    clientMessageId: createClientMessageId(accepted.ownerAppId, accepted.surfaceKey, createIdentity),
  });
}

function createClientMessageId(ownerAppId: string, surfaceKey: string, createIdentity: () => string): string {
  return `extension:${ownerAppId}:${surfaceKey}:${required(createIdentity(), 'identity')}`;
}

function required(value: string, field: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${field} is required`);
  return normalized;
}
