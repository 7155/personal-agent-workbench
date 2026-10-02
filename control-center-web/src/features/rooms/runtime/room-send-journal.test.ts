import { afterEach, describe, expect, it } from 'vitest';
import { createPreviewTransport } from '@/app/preview-control-transport';
import { roomSendJournal, type RoomSendAttempt } from './room-send-journal';

const attempt = (id = 'message-1'): RoomSendAttempt => ({
  clientMessageId: id, rawValue: 'Do this once', attachments: [], status: 'sending',
  request: { pathId: 'agent.room.message', params: { roomId: 'room-a' }, body: {
    clientMessageId: id, message: 'Do this once', attachmentIds: ['media_123456789012'],
    participantIds: ['partner-a'], workItemId: 'work-a', retryOfRootId: 'old-root',
    answerToPostId: 'question-a', answerToRootId: 'root-a',
  } },
});
afterEach(() => sessionStorage.clear());

describe('Room send admission journal', () => {
  it('retains every original binding through a Room view switch and blocks a second in-flight send', () => {
    const transport = createPreviewTransport();
    const first = roomSendJournal(transport, 'room-a');
    const input = attempt();
    expect(first.start(input)).toBe(true);
    expect(first.start(attempt('new-id'))).toBe(false);
    first.fail(input, new TypeError('lost ACK'));
    expect(roomSendJournal(transport, 'room-b').getSnapshot()).toBeUndefined();
    const reopened = roomSendJournal(transport, 'room-a');
    expect(reopened.getSnapshot()?.request).toEqual(input.request);
    expect(reopened.start(reopened.getSnapshot()!)).toBe(true);
  });

  it('restores an uncertain request after a transport is reconstructed without replaying it', () => {
    const transport = Object.assign(createPreviewTransport(), { connectionIdentity: 'https://paw.test' });
    const journal = roomSendJournal(transport, 'room-a');
    const original = attempt();
    journal.start(original);
    const restored = roomSendJournal(Object.assign(createPreviewTransport(), { connectionIdentity: 'https://paw.test' }), 'room-a');
    expect(restored.getSnapshot()).toEqual({ ...original, status: 'uncertain' });
    expect(roomSendJournal(Object.assign(createPreviewTransport(), { connectionIdentity: 'https://other.test' }), 'room-a').getSnapshot()).toBeUndefined();
  });

  it('preserves the composer policy along with a retried inline answer', () => {
    const original = { ...attempt(), preserveDraft: true };
    const connectionIdentity = 'https://paw.test';
    const journal = roomSendJournal(Object.assign(createPreviewTransport(), { connectionIdentity }), 'room-a');
    journal.start(original);
    journal.fail(original, new TypeError('ACK lost'));
    const restored = roomSendJournal(Object.assign(createPreviewTransport(), { connectionIdentity }), 'room-a');
    expect(restored.getSnapshot()?.preserveDraft).toBe(true);
    expect(restored.getSnapshot()?.request).toEqual(original.request);
  });

  it('does not replace an unresolved request with edited input or let an unrelated receipt clear it', () => {
    const journal = roomSendJournal(createPreviewTransport(), 'room-a');
    journal.start(attempt());
    journal.fail(attempt(), new TypeError('network'));
    expect(journal.start(attempt('edited'))).toBe(false);
    journal.resolve('other-request');
    journal.fail(attempt('other-request'), { status: 400 });
    expect(journal.getSnapshot()?.clientMessageId).toBe('message-1');
  });

  it('retains pending and mismatched failed receipts, but permits a new send after exact failure', () => {
    const journal = roomSendJournal(createPreviewTransport(), 'room-a');
    const original = attempt();
    journal.start(original);
    const failure = (id: string, state: string) => ({ payload: {
      code: state === 'failed' ? 'AGENT_COMMAND_FAILED' : 'AGENT_COMMAND_PENDING',
      commandReceipt: { clientMessageId: id, state },
    } });
    journal.fail(original, failure('other-id', 'failed'));
    expect(journal.getSnapshot()?.status).toBe('uncertain');
    journal.fail(original, failure('message-1', 'pending'));
    expect(journal.getSnapshot()?.status).toBe('uncertain');
    journal.fail(original, failure('message-1', 'failed'));
    expect(journal.getSnapshot()).toBeUndefined();
    expect(journal.start(attempt('new-id'))).toBe(true);
    journal.resolve('message-1');
    expect(journal.getSnapshot()?.clientMessageId).toBe('new-id');
  });

  it('does not treat a retry rejection as proof that the earlier request never executed', () => {
    const journal = roomSendJournal(createPreviewTransport(), 'room-a');
    journal.start(attempt());
    journal.fail(attempt(), new TypeError('ACK lost'));
    const retry = journal.getSnapshot()!;
    journal.start(retry);
    journal.fail(retry, { status: 404 });
    expect(journal.getSnapshot()?.clientMessageId).toBe('message-1');
  });

  it('allows correcting a first-attempt pre-admission rejection', () => {
    const journal = roomSendJournal(createPreviewTransport(), 'room-a');
    journal.start(attempt());
    journal.fail(attempt(), { status: 413 });
    expect(journal.getSnapshot()).toBeUndefined();
  });

  it('does not treat a follow-up HTTP failure as rejection of an already acknowledged command', () => {
    const journal = roomSendJournal(createPreviewTransport(), 'room-a');
    journal.start(attempt());
    journal.fail(attempt(), { status: 404 }, true);
    expect(journal.getSnapshot()?.clientMessageId).toBe('message-1');
    expect(journal.getSnapshot()?.status).toBe('uncertain');
  });
});
