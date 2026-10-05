import { describe, expect, it } from 'vitest';
import { acceptPetSnapshot, emptyPetCounts, petPresentation, unavailablePetSnapshot } from './desktop-pet-snapshot';

describe('companion retained presentation', () => {
  it('does not promote historical completion counts into just-completed activity', () => {
    expect(petPresentation({ ...unavailablePetSnapshot(), freshness: 'synced', counts: { ...emptyPetCounts(), terminal: 40 } }))
      .toEqual({ activity: 'idle', label: '当前没有运行中的对话' });
  });
  it('prioritizes unsynced, attention, running and paused states truthfully', () => {
    const snapshot = { ...unavailablePetSnapshot(), counts: { running: 2, attention: 1, paused: 1, terminal: 4, idle: 2, unknown: 0 } };
    expect(petPresentation(snapshot).label).toBe('状态未同步');
    expect(petPresentation({ ...snapshot, freshness: 'recovering' }).activity).toBe('static');
    expect(petPresentation({ ...snapshot, freshness: 'synced' }).activity).toBe('error');
    expect(petPresentation({ ...snapshot, freshness: 'synced', counts: { ...snapshot.counts, attention: 0 } }).activity).toBe('working');
    expect(petPresentation({ ...snapshot, freshness: 'synced', counts: { ...emptyPetCounts(), paused: 1 } }).label).toBe('1 个对话已暂停');
  });
  it('accepts new epochs but rejects stale revisions, unknown schemas and wrong owner scopes', () => {
    const previous = { ...unavailablePetSnapshot(), producerEpoch: 5, revision: 3, sourceId: 'work-directory' as const, scopeId: 's' };
    for (const next of [{ ...previous, revision: 2 }, { ...previous, producerEpoch: 4, revision: 99 }, { ...previous, scopeId: 'other', revision: 4 }, { ...previous, schemaVersion: 2 as 1, revision: 4 }]) expect(acceptPetSnapshot(previous, next)).toBe(previous);
    const next = { ...previous, producerEpoch: 6, revision: 0, scopeId: 'other' };
    expect(acceptPetSnapshot(previous, next)).toBe(next);
  });
});
