import { emptyPetCounts, normalizePetFact, type PetSourceIdentity, type PetStatePublisherBridge, type PetStateValue } from './desktop-pet-snapshot';

/** A view publisher only: no transport, Session subscription, polling, or history. */
export function createDesktopPetPublisher(bridge: PetStatePublisherBridge, identity: PetSourceIdentity) {
  let active = true;
  let epoch: number | undefined;
  let factsSupported = false;
  const bytes = (value: unknown) => new TextEncoder().encode(JSON.stringify(value)).byteLength;
  function envelope(value: PetStateValue, producerEpoch: number, revision: number) {
    const { facts, ...legacy } = value;
    const result = { ...legacy, schemaVersion: 1 as const, producerEpoch, revision,
      ...(factsSupported ? { facts: [] as NonNullable<PetStateValue['facts']> } : {}) };
    // Reserve the native wrapper keys and its bounded generated visual label.
    const fits = () => bytes(factsSupported ? { ...result, sourceId: identity.sourceId, scopeId: identity.scopeId,
      visual: { signal: 'waiting', motion: 'static', label: '界'.repeat(48), arrivalKey: '9999999999999999:9999999999999999' } } : result) <= 4096;
    if (!fits()) throw new Error('Desktop pet base exceeds IPC budget');
    if (factsSupported) for (const fact of facts ?? []) {
      result.facts!.push(fact);
      if (!fits()) result.facts!.pop();
    }
    return result;
  }
  let revision = 0;
  let pending: PetStateValue | undefined;
  let sending = false;
  let last = '';
  const releaseEpoch = (producerEpoch: number) => { void bridge.release({ producerEpoch }).catch(() => {}); };
  async function flush() {
    if (!active || epoch === undefined || sending || !pending) return;
    const producerEpoch = epoch;
    sending = true;
    try {
      while (active && pending) {
        const value = pending; pending = undefined;
        const packed = envelope(value, producerEpoch, revision + 1);
        const signature = JSON.stringify({ ...packed, revision: 0 });
        if (signature === last) continue;
        last = signature;
        revision += 1;
        const accepted = await bridge.publish(packed);
        if (!accepted) { active = false; pending = undefined; }
      }
    } catch {
      active = false; pending = undefined;
      if (epoch !== undefined) releaseEpoch(epoch);
    } finally { sending = false; }
  }
  void bridge.begin(identity).then(result => {
    if (!active) { releaseEpoch(result.producerEpoch); return; }
    epoch = result.producerEpoch; factsSupported = result.factsVersion === 1; void flush();
  }).catch(() => { active = false; pending = undefined; });
  return {
    update(value: PetStateValue) {
      if (!active) return;
      // Explicit allowlist: consumers may never forward extra object fields.
      const counts = emptyPetCounts();
      for (const key of Object.keys(counts) as (keyof typeof counts)[]) counts[key] = value.counts[key];
      const conversations = value.conversations.slice(0, 8).map(({ id, label, state }) => ({ id, label, state }));
      const ids = new Set(conversations.map(row => row.id));
      const facts = value.facts?.slice(0, 8).flatMap(row => {
        const fact = normalizePetFact(row);
        return fact && ids.has(fact.id) ? [fact] : [];
      });
      pending = { freshness: value.freshness, counts, conversations, ...(facts ? { facts } : {}) };
      void flush();
    },
    release() {
      active = false; pending = undefined;
      if (epoch !== undefined) releaseEpoch(epoch);
    },
  };
}
