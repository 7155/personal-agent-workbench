import { describe, expect, it } from 'vitest';
import { describeDebugTurn, deriveToolBatches, normalizeDebugContextResponse } from './model';

describe('context debug model', () => {
  it('preserves omission receipts and counts while keeping real empty context distinct', () => {
    const omission = { omitted: true, reason: 'retained_byte_budget', limitBytes: 16_777_216, path: 'modelCalls.0.contextMessages' };
    const response = normalizeDebugContextResponse({ available: true, context: {
      sessionId: 'session-omitted', turnId: 'original-turn', clientMessageId: 'original-command',
      modelCalls: [
        { index: 7, contextMessages: omission, providerContext: omission,
          contextDelta: { omitted: true, addedMessageCount: 4, addedMessages: [] } },
        { index: 8, contextMessages: [], providerContext: { messages: [] },
          contextDelta: { addedMessageCount: 0, addedMessages: [] } },
      ],
    } });
    expect(response.context?.modelCalls[0]).toMatchObject({ index: 7, contextMessages: [],
      contextMessagesOmission: omission, contextDelta: { omitted: true, addedMessageCount: 4, addedMessages: [] } });
    expect(response.context?.modelCalls[1].contextMessagesOmission).toBeUndefined();
    expect(response.context?.modelCalls[1].contextDelta.omitted).not.toBe(true);
    expect(response.context?.raw).toMatchObject({ turnId: 'original-turn', clientMessageId: 'original-command' });
  });

  it('keeps model-call deltas and explicit runtime tool batches', () => {
    const response = normalizeDebugContextResponse({
      available: true,
      transient: true,
      availableTurns: [{ turnId: 'turn-1', modelCallCount: 2, toolCallCount: 2 }],
      context: {
        sessionId: 'session-1',
        turnId: 'turn-1',
        modelCalls: [{
          index: 2,
          contextMessages: [{ role: 'user', content: 'hello' }, { role: 'toolResult', content: 'done' }],
          contextDelta: {
            baseCallIndex: 1,
            commonPrefixMessages: 1,
            removedMessageCount: 0,
            addedMessageCount: 1,
            addedMessages: [{ role: 'toolResult', content: 'done' }],
          },
          providerExchanges: [{ index: 2, status: 200, payload: { model: 'test' } }],
        }],
        toolExecutions: [
          { toolCallId: 'a', toolName: 'read', modelCallIndex: 2, startSequence: 1, endSequence: 4, status: 'completed' },
          { toolCallId: 'b', toolName: 'search', modelCallIndex: 2, startSequence: 2, endSequence: 3, status: 'completed' },
        ],
        toolBatches: [{ id: 'batch-1', modelCallIndex: 2, stage: 1, executionMode: 'parallel', status: 'completed', toolCallIds: ['a', 'b'] }],
      },
    });

    expect(response.availableTurns[0]).toMatchObject({ turnId: 'turn-1', modelCallCount: 2 });
    expect(response.context?.modelCalls[0]?.contextDelta.addedMessages).toEqual([{ role: 'toolResult', content: 'done' }]);
    expect(response.context?.toolBatches[0]).toMatchObject({ executionMode: 'parallel', toolCallIds: ['a', 'b'] });
  });

  it('derives parallel and serial stages from lifecycle sequence overlap', () => {
    const batches = deriveToolBatches([
      { toolCallId: 'a', toolName: 'read', modelCallIndex: 1, startedAtMs: 1, startSequence: 1, endSequence: 4, endedAtMs: 4, args: {}, status: 'completed', updates: [] },
      { toolCallId: 'b', toolName: 'read', modelCallIndex: 1, startedAtMs: 2, startSequence: 2, endSequence: 3, endedAtMs: 3, args: {}, status: 'completed', updates: [] },
      { toolCallId: 'c', toolName: 'read', modelCallIndex: 1, startedAtMs: 5, startSequence: 5, endSequence: 6, endedAtMs: 6, args: {}, status: 'completed', updates: [] },
    ]);

    expect(batches.map((batch) => [batch.executionMode, batch.toolCallIds])).toEqual([
      ['parallel', ['a', 'b']],
      ['serial', ['c']],
    ]);
  });

  it('upgrades the legacy context-window shape into model calls', () => {
    const response = normalizeDebugContextResponse({
      available: true,
      context: {
        sessionId: 'legacy',
        contextWindows: [
          { index: 1, messages: [{ role: 'user', content: 'one' }] },
          { index: 2, messages: [{ role: 'user', content: 'one' }, { role: 'toolResult', content: 'two' }] },
        ],
        providerRequests: [{ index: 1, payload: { input: 'one' } }, { index: 2, payload: { input: 'two' } }],
      },
    });

    expect(response.context?.modelCalls).toHaveLength(2);
    expect(response.context?.modelCalls[1]?.contextDelta).toMatchObject({
      commonPrefixMessages: 1,
      addedMessageCount: 1,
    });
  });

  it('distinguishes an explicit first turn from compaction recovery evidence', () => {
    const first = normalizeDebugContextResponse({
      available: true,
      availableTurns: [{
        turnId: 'turn-first',
        turnOrdinal: 1,
        assemblyPhase: 'initial',
      }],
      context: {
        sessionId: 'session-1',
        turnId: 'turn-first',
        modelCalls: [{ index: 1, contextMessages: [], contextDelta: { addedMessages: [] } }],
      },
    });
    expect(describeDebugTurn(first.context!, first.availableTurns)).toMatchObject({
      label: '首轮装配',
      phase: 'initial',
    });

    const recovered = normalizeDebugContextResponse({
      available: true,
      availableTurns: [{ turnId: 'turn-recovered' }],
      context: {
        sessionId: 'session-1',
        turnId: 'turn-recovered',
        modelCalls: [{
          index: 1,
          contextMessages: [],
          contextDelta: {
            addedMessages: [{
              role: 'custom',
              customType: 'rag-ime-compaction-recovery',
              content: '<compaction-recovery>保留原始愿景</compaction-recovery>',
            }],
          },
        }],
      },
    });
    expect(describeDebugTurn(recovered.context!, recovered.availableTurns)).toMatchObject({
      label: '压缩后恢复',
      phase: 'compaction_recovery',
    });
  });
});
