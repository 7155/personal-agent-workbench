import type { RoomAttachmentReceipt } from '@/contracts/room-reducer';
import type { ControlRequest, ControlTransport } from '@/platform/transport';
import { agentCommandReceiptFailure } from '@/features/agent/public-error';

export interface RoomSendAttempt {
  request: ControlRequest;
  clientMessageId: string;
  rawValue: string;
  attachments: RoomAttachmentReceipt[];
  status: 'sending' | 'uncertain';
  preserveDraft?: boolean;
}

/** One unresolved Room command per connection and Room. Reconnect only reads
 * this record; replay always requires an explicit user action. */
class RoomSendJournal {
  private attempt: RoomSendAttempt | undefined;
  private listeners = new Set<() => void>();
  constructor(roomId: string, private key: string) {
    if (!key) return;
    try {
      const value = JSON.parse(sessionStorage.getItem(key) || 'null') as RoomSendAttempt | null;
      if (value && typeof value.clientMessageId === 'string' && value.clientMessageId
        && typeof value.rawValue === 'string'
        && Array.isArray(value.attachments) && value.attachments.every(item => item
          && typeof item.mediaId === 'string' && item.roomId === roomId
          && typeof item.fileName === 'string' && typeof item.mimeType === 'string'
          && typeof item.byteSize === 'number' && typeof item.sha256 === 'string')
        && value.request?.params?.roomId === roomId
        && ['agent.room.message', 'agent.room.participant.steer'].includes(value.request.pathId)) {
        const body = value.request.body as Record<string, unknown> | undefined;
        if (body && typeof body.message === 'string'
          && (body.clientMessageId ?? body.clientActionId) === value.clientMessageId) {
          this.attempt = { ...value, status: 'uncertain', request: {
            pathId: value.request.pathId, params: { roomId }, body: value.request.body,
          } };
        }
      }
    } catch { /* In-memory identity still survives Room view changes. */ }
  }
  getSnapshot = () => this.attempt;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  start(attempt: RoomSendAttempt): boolean {
    if (this.attempt?.status === 'sending'
      || this.attempt && this.attempt.clientMessageId !== attempt.clientMessageId) return false;
    // Copy before transport or callers can mutate the admission payload.
    this.update({ ...structuredClone(attempt), status: 'sending' });
    return true;
  }
  resolve(clientMessageId: string): void {
    if (this.attempt?.clientMessageId === clientMessageId) this.update(undefined);
  }
  fail(attempt: RoomSendAttempt, error: unknown, responseReceived = false): void {
    if (this.attempt?.clientMessageId !== attempt.clientMessageId) return;
    const receipt = agentCommandReceiptFailure(error);
    const status = (error as { status?: number } | null)?.status;
    // Only an exact failed receipt, or a first-attempt pre-admission rejection,
    // permits a new identity. A retry's 4xx cannot disprove an earlier commit.
    // Nor can a follow-up gate/local failure invalidate a received response.
    const rejected = receipt?.clientMessageId === attempt.clientMessageId && receipt.state === 'failed'
      || !responseReceived && attempt.status !== 'uncertain' && typeof status === 'number'
        && [400, 401, 403, 404, 413, 422].includes(status);
    this.update(rejected ? undefined : { ...this.attempt, status: 'uncertain' });
  }
  private update(attempt: RoomSendAttempt | undefined): void {
    this.attempt = attempt;
    if (this.key) try {
      if (attempt) sessionStorage.setItem(this.key, JSON.stringify({ ...attempt, status: 'uncertain' }));
      else sessionStorage.removeItem(this.key);
    } catch { /* Storage can be disabled; live Room switches remain protected. */ }
    for (const listener of this.listeners) listener();
  }
}

const journals = new WeakMap<ControlTransport, Map<string, RoomSendJournal>>();
export function roomSendJournal(transport: ControlTransport, roomId: string): RoomSendJournal {
  let rooms = journals.get(transport);
  if (!rooms) { rooms = new Map(); journals.set(transport, rooms); }
  let journal = rooms.get(roomId);
  if (!journal) {
    const identity = transport.connectionIdentity || (transport.kind === 'native' ? 'native-local' : '');
    journal = new RoomSendJournal(roomId, identity
      ? `paw.room.send.v1:${encodeURIComponent(identity)}:${encodeURIComponent(roomId)}` : '');
    rooms.set(roomId, journal);
  }
  return journal;
}
