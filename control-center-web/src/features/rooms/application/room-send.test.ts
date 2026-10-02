import { afterEach, describe, expect, it, vi } from 'vitest';
import { parseRoomEvent } from '@/contracts/validators';
import { parseRoomEventSnapshot } from '@/contracts/room-reducer';
import { previewRoomSnapshot } from '@/app/preview-room-data';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { roomSendJournal, type RoomSendAttempt } from '../runtime/room-send-journal';
import { useRoomLiveStore } from '../state/live-store';
import { startRoomSend } from './room-send';

function attempt(roomId = 'room-a', id = 'message-1'): RoomSendAttempt {
  return {
    clientMessageId: id, rawValue: 'original draft', status: 'sending', preserveDraft: true,
    attachments: [{ mediaId: 'media_original1234', roomId, fileName: 'original.png',
      mimeType: 'image/png', byteSize: 12, sha256: 'a'.repeat(64) }],
    request: { pathId: 'agent.room.message', params: { roomId }, body: {
      message: 'original draft', clientMessageId: id, attachmentIds: ['media_original1234'],
      participantIds: ['partner-a'], workItemId: 'work-a', retryOfRootId: 'prior-root',
    } },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

afterEach(() => {
  vi.restoreAllMocks();
  useRoomLiveStore.getState().reset();
  sessionStorage.clear();
});

describe('shared Room send application contract', () => {
  it('keeps an SSE-completed Root unchanged throughout an exact lost-ACK retry and its duplicate receipt', async () => {
    const userWire = {
      schemaVersion: 'rag-ime.agent-room-event.v1', eventId: 'room-a:1', roomId: 'room-a', sequence: 1,
      turnId: 'root-a', eventType: 'user_message', participantId: null, sourceSessionId: '',
      topicId: '', createdAtMs: 1, resumeToken: 'room-a:1',
      payload: { messageId: 'post-accepted', clientMessageId: 'message-1', text: 'original draft' },
    };
    const user = parseRoomEvent(userWire);
    const completed = parseRoomEvent({ ...userWire, eventId: 'room-a:2', sequence: 2,
      eventType: 'turn_completed', createdAtMs: 2, resumeToken: 'room-a:2',
      payload: { rootId: 'root-a', status: 'completed' },
    });
    let calls = 0;
    const transport = new MockControlTransport({ routes: { 'agent.room.message': () => {
      if (++calls === 1) {
        useRoomLiveStore.getState().applyEvents('room-a', [user, completed]);
        throw new TypeError('lost ACK after completed SSE');
      }
      return { ok: true, idempotentReplay: true, timelineEvents: [userWire] };
    } } });
    expect((await startRoomSend(transport, 'room-a', attempt())!.settled).status).toBe('uncertain');
    const before = useRoomLiveStore.getState().projections['room-a'];
    expect(before.turnsById['root-a'].status).toBe('completed');
    expect(before.turnOrder).toEqual(['root-a']);
    const retry = startRoomSend(transport, 'room-a', attempt())!;
    // The pending HTTP lookup itself must not create another queued UI round.
    expect(useRoomLiveStore.getState().projections['room-a']).toBe(before);
    expect((await retry.settled).status).toBe('accepted');
    const after = useRoomLiveStore.getState().projections['room-a'];
    expect(after).toBe(before);
    expect(after.lastSequence).toBe(2);
    expect(after.messageOrder).toEqual(['post-accepted']);
    expect(after.turnOrder).toEqual(['root-a']);
    expect(after.turnsById['root-a'].status).toBe('completed');
    expect(after.optimisticByClientMessageId).toEqual({});
    expect(roomSendJournal(transport, 'room-a').getSnapshot()).toBeUndefined();
    expect(transport.requests[1].request).toEqual(transport.requests[0].request);
  });

  it('claims one command synchronously across surfaces while allowing a different Room to send', async () => {
    const pending = deferred<{ ok: true }>();
    const transport = new MockControlTransport({ routes: { 'agent.room.message': pending.promise } });
    const first = startRoomSend(transport, 'room-a', attempt())!;
    expect(startRoomSend(transport, 'room-a', attempt())).toBeUndefined();
    expect(startRoomSend(transport, 'room-a', attempt('room-a', 'new-draft'))).toBeUndefined();
    const other = startRoomSend(transport, 'room-b', attempt('room-b'))!;
    expect(transport.requests).toHaveLength(0);
    await Promise.resolve();
    expect(transport.requests).toHaveLength(2);
    expect(useRoomLiveStore.getState().projections['room-a'].messageOrder).toHaveLength(1);
    expect(useRoomLiveStore.getState().projections['room-b'].messageOrder).toHaveLength(1);
    pending.resolve({ ok: true });
    expect((await first.settled).status).toBe('accepted');
    expect((await other.settled).status).toBe('accepted');
    expect(roomSendJournal(transport, 'room-a').getSnapshot()).toBeUndefined();
  });

  it('keeps the full original binding for explicit recovery and never replays on subscription or Room lookup', async () => {
    let calls = 0;
    const transport = new MockControlTransport({ routes: { 'agent.room.message': () => {
      if (++calls === 1) throw new TypeError('lost ACK after commit');
      return { ok: true, idempotentReplay: true };
    } } });
    const input = attempt();
    const expected = structuredClone(input);
    const first = startRoomSend(transport, 'room-a', input)!;
    (input.request.body as Record<string, unknown>).message = 'mutated input';
    input.attachments.length = 0;
    expect((await first.settled).status).toBe('uncertain');
    const journal = roomSendJournal(transport, 'room-a');
    const unsubscribe = journal.subscribe(() => {});
    expect(journal.getSnapshot()).toEqual({ ...expected, status: 'uncertain' });
    expect(startRoomSend(transport, 'room-a', attempt('room-a', 'second-command'))).toBeUndefined();
    expect(calls).toBe(1);
    // Even a caller offering the same identity with changed text cannot rebind it.
    const retry = startRoomSend(transport, 'room-a', input)!;
    expect(retry.attempt).toEqual({ ...expected, status: 'uncertain' });
    expect((await retry.settled).status).toBe('accepted');
    expect(transport.requests[1].request).toEqual(transport.requests[0].request);
    expect(journal.getSnapshot()).toBeUndefined();
    unsubscribe();
  });

  it('isolates a late failure to its original Room projection', async () => {
    const pending = deferred<{ ok: true }>();
    const transport = new MockControlTransport({ routes: { 'agent.room.message': (request: ControlRequest) => (
      request.params?.roomId === 'room-a' ? pending.promise : { ok: true }
    ) } });
    const first = startRoomSend(transport, 'room-a', attempt())!;
    await startRoomSend(transport, 'room-b', attempt('room-b'))!.settled;
    const other = useRoomLiveStore.getState().projections['room-b'];
    pending.reject(new TypeError('late failure'));
    expect((await first.settled).status).toBe('uncertain');
    expect(useRoomLiveStore.getState().projections['room-a'].messageOrder).toEqual([]);
    expect(useRoomLiveStore.getState().projections['room-b']).toBe(other);
    expect(roomSendJournal(transport, 'room-b').getSnapshot()).toBeUndefined();
  });

  it.each([400, 401, 403, 404, 413, 422])('releases a first pre-admission rejection (%s) without claiming completion', async status => {
    const error = Object.assign(new Error('rejected'), { status });
    const transport = new MockControlTransport({ routes: { 'agent.room.message': () => { throw error; } } });
    expect(await startRoomSend(transport, 'room-a', attempt())!.settled).toEqual({ status: 'rejected', error });
    expect(roomSendJournal(transport, 'room-a').getSnapshot()).toBeUndefined();
    expect(useRoomLiveStore.getState().projections['room-a'].turnOrder).toEqual([]);
  });

  it('does not turn a later retry rejection into proof of an earlier non-execution', async () => {
    let calls = 0;
    const transport = new MockControlTransport({ routes: { 'agent.room.message': () => {
      if (++calls === 1) throw new TypeError('lost ACK');
      throw Object.assign(new Error('Root no longer available'), { status: 404 });
    } } });
    await startRoomSend(transport, 'room-a', attempt())!.settled;
    expect((await startRoomSend(transport, 'room-a', attempt())!.settled).status).toBe('uncertain');
    expect(roomSendJournal(transport, 'room-a').getSnapshot()?.clientMessageId).toBe('message-1');
  });

  it('keeps an unconfirmed response uncertain until an exact failed receipt releases it', async () => {
    let calls = 0;
    const transport = new MockControlTransport({ routes: { 'agent.room.message': () => {
      if (++calls === 1) return { ok: false };
      throw { payload: { code: 'AGENT_COMMAND_FAILED', commandReceipt: { clientMessageId: 'message-1', state: 'failed' } } };
    } } });
    expect((await startRoomSend(transport, 'room-a', attempt())!.settled).status).toBe('uncertain');
    expect((await startRoomSend(transport, 'room-a', attempt())!.settled).status).toBe('rejected');
    expect(roomSendJournal(transport, 'room-a').getSnapshot()).toBeUndefined();
  });

  it('keeps transport and presentation mutations outside the retained answer binding', async () => {
    const pending = deferred<Record<string, unknown>>();
    const transport = new MockControlTransport({ routes: { 'agent.room.message': pending.promise } });
    const input = attempt();
    input.request.body = { ...(input.request.body as Record<string, string | string[]>),
      answerToPostId: 'question-1', answerToRootId: 'root-1' };
    const expected = structuredClone(input);
    const delivery = startRoomSend(transport, 'room-a', input)!;
    (delivery.attempt.request.body as Record<string, unknown>).answerToPostId = 'question-2';
    await Promise.resolve();
    (transport.requests[0].request.body as Record<string, unknown>).answerToRootId = 'root-2';
    pending.reject(new TypeError('lost ACK'));
    expect((await delivery.settled).status).toBe('uncertain');
    expect(roomSendJournal(transport, 'room-a').getSnapshot()).toEqual({ ...expected, status: 'uncertain' });
  });

  it('confirms an old durable gate inside the same receipt transaction and preserves identity if it fails', async () => {
    let gates = 0;
    const transport = new MockControlTransport({ routes: {
      'agent.room.message': { ok: true, startConfirmation: { status: 'pending', gateId: 'old-gate' } },
      'agent.room.startGate.confirm': () => {
        if (++gates === 1) throw Object.assign(new Error('gate unavailable'), { status: 404 });
        return { ok: true };
      },
    } });
    expect((await startRoomSend(transport, 'room-a', attempt())!.settled).status).toBe('uncertain');
    expect((await startRoomSend(transport, 'room-a', attempt())!.settled).status).toBe('accepted');
    expect(transport.requests.map(call => call.request.pathId)).toEqual([
      'agent.room.message', 'agent.room.startGate.confirm', 'agent.room.message', 'agent.room.startGate.confirm',
    ]);
    expect(transport.requests[0].request).toEqual(transport.requests[2].request);
    expect(transport.requests[1].request.body).toEqual({ gateId: 'old-gate', decision: 'confirm' });
  });

  it('keeps an accepted command recoverable if the local projection cannot apply its ACK', async () => {
    const transport = new MockControlTransport({ routes: { 'agent.room.message': { ok: true } } });
    vi.spyOn(useRoomLiveStore.getState(), 'acceptMessage').mockImplementationOnce(() => { throw new Error('projection unavailable'); });
    expect((await startRoomSend(transport, 'room-a', attempt())!.settled).status).toBe('uncertain');
    expect((await startRoomSend(transport, 'room-a', attempt())!.settled).status).toBe('accepted');
    expect(transport.requests[1].request).toEqual(transport.requests[0].request);
  });

  it('projects an accepted HTTP event once when its SSE copy arrives first', async () => {
    const pending = deferred<Record<string, unknown>>();
    const transport = new MockControlTransport({ routes: { 'agent.room.message': pending.promise } });
    const snapshot = previewRoomSnapshot('room-a');
    useRoomLiveStore.getState().replaySnapshot('room-a', parseRoomEventSnapshot({
      ...snapshot, events: [], firstSequence: 0, lastSequence: 0, resumeToken: '',
      room: { ...snapshot.room, lastEventSequence: 0 },
    }));
    const delivery = startRoomSend(transport, 'room-a', attempt())!;
    const event = parseRoomEvent({
      schemaVersion: 'rag-ime.agent-room-event.v1', eventId: 'room-a:1', roomId: 'room-a', sequence: 1,
      turnId: 'root-a', eventType: 'user_message', participantId: null, sourceSessionId: '',
      topicId: '', createdAtMs: 1, resumeToken: 'room-a:1',
      payload: { postId: 'post-1', messageId: 'post-1', clientMessageId: 'message-1', rootId: 'root-a', text: 'original draft' },
    });
    useRoomLiveStore.getState().applyEvents('room-a', [event]);
    pending.resolve({ ok: true, timelineEvents: [event] });
    expect((await delivery.settled).status).toBe('accepted');
    const projection = useRoomLiveStore.getState().projections['room-a'];
    expect(projection.messageOrder).toEqual(['post-1']);
    expect(projection.turnsById['root-a'].status).not.toBe('completed');
    expect(useRoomLiveStore.getState().historyByRoomId['room-a'].events).toEqual([event]);
  });

  it('does not invent a user turn for a targeted steer', async () => {
    const transport = new MockControlTransport({ routes: { 'agent.room.participant.steer': { ok: true } } });
    const input = attempt();
    input.attachments = [];
    input.request = { pathId: 'agent.room.participant.steer', params: { roomId: 'room-a' }, body: {
      action: 'steer_participant', rootId: 'root-a', participantId: 'partner-a', clientActionId: input.clientMessageId, message: 'adjust',
    } };
    expect((await startRoomSend(transport, 'room-a', input)!.settled).status).toBe('accepted');
    expect(useRoomLiveStore.getState().projections['room-a']?.messageOrder ?? []).toEqual([]);
  });

  it('lets the caller register its admitted presentation before synchronous transport events', async () => {
    let presentationReady = false;
    const transport = new MockControlTransport({ routes: { 'agent.room.message': () => {
      expect(presentationReady).toBe(true);
      return { ok: true };
    } } });
    const delivery = startRoomSend(transport, 'room-a', attempt())!;
    expect(roomSendJournal(transport, 'room-a').getSnapshot()?.status).toBe('sending');
    presentationReady = true;
    expect((await delivery.settled).status).toBe('accepted');
  });

  it('does not send a command through a different Room owner', () => {
    const transport = new MockControlTransport();
    expect(startRoomSend(transport, 'room-b', attempt('room-a'))).toBeUndefined();
    expect(transport.requests).toEqual([]);
    expect(roomSendJournal(transport, 'room-b').getSnapshot()).toBeUndefined();
  });

  it('settles the journal even if creating and removing an optimistic projection both fail', async () => {
    const transport = new MockControlTransport({ routes: { 'agent.room.message': { ok: true } } });
    const error = new Error('optimistic projection unavailable');
    vi.spyOn(useRoomLiveStore.getState(), 'appendOptimistic').mockImplementationOnce(() => { throw error; });
    vi.spyOn(useRoomLiveStore.getState(), 'discardOptimistic').mockImplementationOnce(() => { throw new Error('cleanup unavailable'); });
    expect(await startRoomSend(transport, 'room-a', attempt())!.settled).toEqual({ status: 'uncertain', error });
    expect(transport.requests).toHaveLength(0);
    expect(roomSendJournal(transport, 'room-a').getSnapshot()?.status).toBe('uncertain');
    expect((await startRoomSend(transport, 'room-a', attempt())!.settled).status).toBe('accepted');
  });
});
