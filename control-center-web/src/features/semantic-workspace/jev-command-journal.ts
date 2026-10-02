import type { ControlTransport } from '@/platform/transport';

type Attempt<Input> = {
  input: Input;
  clientMessageId: string;
  uncertain: boolean;
  signature?: string;
};

type Entry<Input> = {
  attempt: Attempt<Input>;
  inFlight?: Promise<unknown>;
};

/** Own one original command across callers, explicit retries and reloads.
 * Business modules still validate their own inputs and receipt semantics.
 * Neither a lost reply nor ending observation proves an effect was cancelled.
 */
export class JevCommandJournal<Input> {
  private entries = new WeakMap<ControlTransport, Map<string, Entry<Input>>>();
  private connections = new Map<string, Map<string, Entry<Input>>>();

  constructor(private options: {
    storagePrefix: string;
    requestPrefix: string;
    conflictMessage: string;
    restoreInput(value: unknown, key: string): Input | undefined;
    signature?(input: Input): string;
    rejectionStatuses: readonly number[];
    localRejection?(error: unknown): boolean;
  }) {}

  read(transport: ControlTransport, key: string): Attempt<Input> | undefined {
    const entry = this.entry(transport, key);
    return entry ? structuredClone(entry.attempt) : undefined;
  }

  acknowledge(
    transport: ControlTransport, key: string,
    matches: (attempt: Attempt<Input>) => boolean,
  ): Input | undefined {
    const entry = this.entry(transport, key);
    if (!entry?.attempt.uncertain || !matches(structuredClone(entry.attempt))) return;
    this.save(transport, key);
    return structuredClone(entry.attempt.input);
  }

  async execute<Result>(
    transport: ControlTransport, key: string, input: Input,
    deliver: (attempt: Attempt<Input>) => Promise<Result>,
  ): Promise<Result> {
    const previous = this.entry(transport, key);
    const signature = this.options.signature ?? JSON.stringify;
    if (previous && signature(previous.attempt.input) !== signature(input)) {
      throw new Error(this.options.conflictMessage);
    }
    if (previous?.inFlight) return previous.inFlight as Promise<Result>;
    const entry = previous ?? { attempt: {
      input: structuredClone(input),
      clientMessageId: `${this.options.requestPrefix}${crypto.randomUUID()}`,
      uncertain: false,
      // Keep the admission v1 shape readable by older clients. Other v1
      // command readers ignore this additional identity metadata.
      ...(this.options.signature ? { signature: signature(input) } : {}),
    } };
    this.save(transport, key, entry);
    const wasUncertain = entry.attempt.uncertain;
    // Publish the shared promise before a synchronous adapter can settle.
    const promise = Promise.resolve().then(() => deliver(structuredClone(entry.attempt))).then(
      result => {
        if (this.entry(transport, key) === entry) this.save(transport, key);
        return result;
      },
      error => {
        if (this.entry(transport, key) === entry) {
          const status = typeof error === 'object' && error !== null && 'status' in error ? error.status : undefined;
          const rejected = !wasUncertain && (this.options.localRejection?.(error)
            || typeof status === 'number' && this.options.rejectionStatuses.includes(status));
          if (rejected) this.save(transport, key);
          else {
            entry.attempt = { ...entry.attempt, uncertain: true };
            this.save(transport, key, entry);
          }
        }
        throw error;
      },
    ).finally(() => { entry.inFlight = undefined; });
    entry.inFlight = promise;
    return promise;
  }

  private connectionIdentity(transport: ControlTransport): string {
    return transport.connectionIdentity || (transport.kind === 'native' ? 'native-local' : '');
  }

  private storageKey(transport: ControlTransport, key: string): string {
    const identity = this.connectionIdentity(transport);
    return identity ? `${this.options.storagePrefix}:${encodeURIComponent(identity)}:${encodeURIComponent(key)}` : '';
  }

  private entry(transport: ControlTransport, key: string): Entry<Input> | undefined {
    const cached = this.memory(transport).get(key);
    if (cached) return cached;
    const storage = this.storageKey(transport, key);
    if (!storage) return;
    try {
      const value: unknown = JSON.parse(sessionStorage.getItem(storage) || 'null');
      if (typeof value !== 'object' || value === null || !('input' in value)
        || !('clientMessageId' in value) || typeof value.clientMessageId !== 'string' || !value.clientMessageId) return;
      const input = this.options.restoreInput(value.input, key);
      if (input === undefined) return;
      const entry: Entry<Input> = { attempt: {
        input: structuredClone(input), clientMessageId: value.clientMessageId,
        // A restored record has no live promise, even if the process died
        // before persisting its observation state.
        uncertain: true,
        ...('signature' in value && typeof value.signature === 'string' ? { signature: value.signature } : {}),
      } };
      this.memory(transport).set(key, entry);
      return entry;
    } catch { /* Disabled storage leaves in-memory recovery available. */ }
  }

  private memory(transport: ControlTransport): Map<string, Entry<Input>> {
    const identity = this.connectionIdentity(transport);
    if (identity) {
      let entries = this.connections.get(identity);
      if (!entries) { entries = new Map(); this.connections.set(identity, entries); }
      return entries;
    }
    let entries = this.entries.get(transport);
    if (!entries) { entries = new Map(); this.entries.set(transport, entries); }
    return entries;
  }

  private save(transport: ControlTransport, key: string, entry?: Entry<Input>): void {
    const entries = this.memory(transport);
    if (entry) entries.set(key, entry); else entries.delete(key);
    const storage = this.storageKey(transport, key);
    if (!storage) return;
    try {
      if (entry) sessionStorage.setItem(storage, JSON.stringify({ ...entry.attempt, uncertain: true }));
      else sessionStorage.removeItem(storage);
    } catch { /* The exact command remains owned in memory. */ }
  }
}
