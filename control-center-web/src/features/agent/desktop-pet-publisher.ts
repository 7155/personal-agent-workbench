import { emptyPetCounts, type PetSourceIdentity, type PetStatePublisherBridge, type PetStateValue } from './desktop-pet-snapshot';

/** A view publisher only: no transport, Session subscription, polling, or history. */
export function createDesktopPetPublisher(bridge: PetStatePublisherBridge, identity: PetSourceIdentity) {
  let active = true;
  let epoch: number | undefined;
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
        const signature = JSON.stringify(value);
        if (signature === last) continue;
        last = signature;
        const accepted = await bridge.publish({ ...value, schemaVersion: 1, producerEpoch, revision: ++revision });
        if (!accepted) { active = false; pending = undefined; }
      }
    } catch {
      active = false; pending = undefined;
      if (epoch !== undefined) releaseEpoch(epoch);
    } finally { sending = false; }
  }
  void bridge.begin(identity).then(result => {
    if (!active) { releaseEpoch(result.producerEpoch); return; }
    epoch = result.producerEpoch; void flush();
  }).catch(() => { active = false; pending = undefined; });
  return {
    update(value: PetStateValue) {
      if (!active) return;
      // Explicit allowlist: consumers may never forward extra object fields.
      const counts = emptyPetCounts();
      for (const key of Object.keys(counts) as (keyof typeof counts)[]) counts[key] = value.counts[key];
      pending = { freshness: value.freshness, counts, conversations: value.conversations.slice(0, 8).map(({ id, label, state }) => ({ id, label, state })) };
      void flush();
    },
    release() {
      active = false; pending = undefined;
      if (epoch !== undefined) releaseEpoch(epoch);
    },
  };
}
