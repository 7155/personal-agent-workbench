import { integer, isCategory, isSpaceKey, record, type CommandBody } from './organization-model';

export type PendingWrite = {
  id: string; spaceKey: string; createdAtMs: number; uncertain: boolean;
} & ({ pathId: 'agent.organization.command'; body: CommandBody }
  | { pathId: 'agent.organization.undo'; body: { receiptId: string } });
export interface JournalStorage { getItem(key: string): string | null; setItem(key: string, value: string): void; removeItem(key: string): void }

function isPending(value: unknown): value is PendingWrite {
  try {
    const p = record(value); const body = record(p.body);
    if (typeof p.id !== 'string' || !p.id || p.id.length > 100 || !isSpaceKey(p.spaceKey)
      || !integer(p.createdAtMs) || typeof p.uncertain !== 'boolean') return false;
    if (p.pathId === 'agent.organization.undo') return body.receiptId === p.id && Object.keys(body).length === 1;
    if (p.pathId !== 'agent.organization.command' || body.commandId !== p.id || body.spaceKey !== p.spaceKey
      || !integer(body.expectedRevision) || Object.keys(body).length !== 5) return false;
    switch (body.operation) {
      case 'category': return isCategory(body.value);
      case 'placement': return body.value === 'desk' || body.value === 'shelf';
      case 'pinned': return typeof body.value === 'boolean';
      case 'group': return typeof body.value === 'string' && body.value.length <= 80;
      case 'proposal': return typeof body.value === 'string' && body.value.length > 0 && body.value.length <= 100;
      default: return false;
    }
  } catch { return false; }
}

/** One unresolved write per connection/tab. It is never replayed automatically.
 * Only metadata intent/IDs are saved, never transcripts, attachments or API keys.
 * On reconnect/reload the user explicitly retries the exact same command ID.
 */
export class OrganizationJournal {
  private pending: PendingWrite | null = null;
  private listeners = new Set<() => void>();
  private persistenceError = false;
  constructor(private key: string, private storage?: JournalStorage) {
    this.persistenceError = !storage;
    try {
      const raw = storage?.getItem(key);
      if (raw) {
        const value: unknown = JSON.parse(raw);
        if (isPending(value)) this.pending = { ...value, uncertain: true };
        else this.persistenceError = true;
      }
    } catch { this.persistenceError = true; }
  }
  getSnapshot = (): PendingWrite | null => this.pending;
  getPersistenceError = () => this.persistenceError;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  begin(value: PendingWrite) {
    if (this.pending) throw new Error('先核实上一项整理操作，再提交新修改。');
    if (!isPending(value)) throw new Error('整理操作参数无效。');
    // Persist before sending; a restored in-flight request is always uncertain.
    this.pending = JSON.parse(JSON.stringify(value)) as PendingWrite;
    this.persist(); this.emit();
  }
  markUncertain(id: string) {
    if (this.pending?.id !== id) return;
    this.pending = { ...this.pending, uncertain: true }; this.persist(); this.emit();
  }
  resolve(id: string) {
    if (this.pending?.id !== id) return;
    this.pending = null; this.persist(); this.emit();
  }
  private persist() {
    try {
      if (this.pending) this.storage?.setItem(this.key, JSON.stringify(this.pending));
      else this.storage?.removeItem(this.key);
    } catch { this.persistenceError = true; }
  }
  private emit() { for (const listener of this.listeners) listener(); }
}
const journals = new Map<string, OrganizationJournal>();
export function journalForConnection(connectionIdentity: string): OrganizationJournal {
  // PAW is a local-user app. A future multi-user host must include the principal
  // identity in this scope and clear it on sign-out; URL alone is not authority.
  let journal = journals.get(connectionIdentity);
  if (!journal) {
    let storage: Storage | undefined;
    try { storage = typeof window === 'undefined' ? undefined : window.sessionStorage; } catch { /* in-memory still usable */ }
    journal = new OrganizationJournal(`paw.organization.pending.v2:${encodeURIComponent(connectionIdentity)}`, storage);
    journals.set(connectionIdentity, journal);
  }
  return journal;
}
const anonymousJournals = new WeakMap<object, OrganizationJournal>();
export function journalForTransport(transport: { readonly connectionIdentity?: string }): OrganizationJournal {
  if (transport.connectionIdentity) return journalForConnection(transport.connectionIdentity);
  // Never combine two anonymous transports or persist under an 'undefined' key.
  let journal = anonymousJournals.get(transport);
  if (!journal) { journal = new OrganizationJournal('volatile'); anonymousJournals.set(transport, journal); }
  return journal;
}
