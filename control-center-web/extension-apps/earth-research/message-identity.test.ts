import { describe, expect, it } from 'vitest';

import {
  createPendingAppMessage,
  newExecutionAfterAcceptedTurnFailure,
  promptRequestFor,
  successorForDurablyFailedCommand,
} from './message-identity';

describe('App message identity', () => {
  it('reuses one frozen clientMessageId and payload for Retry', () => {
    const pending = createPendingAppMessage({
      sessionId: 'session-1',
      ownerAppId: 'extension:sample-insights',
      surfaceKey: 'overview',
      message: 'Summarize the current evidence.',
      createIdentity: () => 'logical-message-1',
    });

    const firstAttempt = promptRequestFor(pending);
    const retryAttempt = promptRequestFor(pending);

    expect(retryAttempt).toEqual(firstAttempt);
    expect(retryAttempt.body.clientMessageId).toBe(
      'extension:extension:sample-insights:overview:logical-message-1',
    );
    expect(Object.isFrozen(pending)).toBe(true);
  });

  it('creates a new identity only for a new logical message', () => {
    const base = {
      sessionId: 'session-1',
      ownerAppId: 'extension:sample-insights',
      surfaceKey: 'overview',
      message: 'Summarize the current evidence.',
    };

    const first = createPendingAppMessage({ ...base, createIdentity: () => 'one' });
    const second = createPendingAppMessage({ ...base, createIdentity: () => 'two' });

    expect(first.clientMessageId).not.toBe(second.clientMessageId);
  });

  it('uses a new id and failed-command lineage only for a durably failed receipt', () => {
    const failed = createPendingAppMessage({
      sessionId: 'session-1',
      ownerAppId: 'extension:sample-insights',
      surfaceKey: 'overview',
      message: 'Summarize the current evidence.',
      createIdentity: () => 'failed-command',
    });
    const successor = successorForDurablyFailedCommand(failed, () => 'successor');

    expect(successor.clientMessageId).not.toBe(failed.clientMessageId);
    expect(promptRequestFor(successor).body).toMatchObject({
      message: failed.message,
      retryOfClientMessageId: failed.clientMessageId,
    });
  });

  it('uses a new id without failed-command lineage after an accepted turn fails', () => {
    const accepted = successorForDurablyFailedCommand(createPendingAppMessage({
      sessionId: 'session-1',
      ownerAppId: 'extension:sample-insights',
      surfaceKey: 'review',
      message: 'Compare the current evidence.',
      createIdentity: () => 'accepted-command',
    }), () => 'accepted-successor');
    const replay = newExecutionAfterAcceptedTurnFailure(accepted, () => 'new-execution');

    expect(replay.clientMessageId).not.toBe(accepted.clientMessageId);
    expect(promptRequestFor(replay).body).not.toHaveProperty('retryOfClientMessageId');
  });
});
