import { describe, expect, it } from 'vitest';
import { acceptPetSnapshot, acceptPetVisualSnapshot, emptyPetCounts, petPresentation, petVisualSignal, unavailablePetSnapshot, type PetSnapshot } from './desktop-pet-snapshot';

describe('companion retained presentation', () => {
  it('maps only known directory facts and never infers the missing three signals', () => {
    const snapshot: PetSnapshot = { ...unavailablePetSnapshot(), producerEpoch: 1, revision: 1, sourceId: 'work-directory', scopeId: 'public', freshness: 'synced' };
    expect(petVisualSignal({ ...snapshot, counts: { ...emptyPetCounts(), running: 1 } })).toEqual({ signal: 'working', motion: 'full' });
    expect(petVisualSignal({ ...snapshot, counts: { ...emptyPetCounts(), error: 1 } })).toEqual({ signal: 'error', motion: 'full' });
    expect(petVisualSignal(snapshot)).toEqual({ signal: 'idle', motion: 'full' });
    for (const state of ['attention', 'paused', 'unknown', 'terminal'] as const) {
      expect(petVisualSignal({ ...snapshot, counts: { ...emptyPetCounts(), [state]: 1 } })).toEqual({ signal: 'idle', motion: 'static' });
    }
    for (const freshness of ['unavailable', 'recovering'] as const) {
      expect(petVisualSignal({ ...snapshot, freshness, counts: { ...emptyPetCounts(), running: 1, error: 1 } })).toEqual({ signal: 'idle', motion: 'static' });
    }
  });

  it('binds one error arrival to a continuous accepted epoch and rejects stale or foreign replay', () => {
    const running: PetSnapshot = { ...unavailablePetSnapshot(), producerEpoch: 1, revision: 1, sourceId: 'work-directory', scopeId: 'public', freshness: 'synced', counts: { ...emptyPetCounts(), running: 1 } };
    const error = { ...running, revision: 2, counts: { ...emptyPetCounts(), error: 1 } };
    const seed = acceptPetVisualSnapshot({ snapshot: unavailablePetSnapshot(), arrivalKey: null }, running);
    expect(seed.arrivalKey).toBeNull();
    const notice = acceptPetVisualSnapshot(seed, error);
    expect(notice.arrivalKey).toBeTruthy();
    const refresh = acceptPetVisualSnapshot(notice, { ...error, revision: 3, counts: { ...emptyPetCounts(), error: 2 } });
    expect(refresh.arrivalKey).toBe(notice.arrivalKey);
    expect(acceptPetVisualSnapshot(refresh, error)).toBe(refresh);
    expect(acceptPetVisualSnapshot(refresh, { ...error, revision: 4, scopeId: 'foreign' })).toBe(refresh);
    const recovering = acceptPetVisualSnapshot(refresh, { ...error, revision: 4, freshness: 'recovering' });
    expect(acceptPetVisualSnapshot(recovering, { ...error, revision: 5 }).arrivalKey).toBeNull();
    expect(acceptPetVisualSnapshot(refresh, { ...error, producerEpoch: 2, revision: 1 }).arrivalKey).toBeNull();
    expect(acceptPetVisualSnapshot({ snapshot: unavailablePetSnapshot(), arrivalKey: null }, error).arrivalKey).toBeNull();
  });

  it('does not promote historical completion counts into just-completed activity', () => {
    expect(petPresentation({ ...unavailablePetSnapshot(), freshness: 'synced', counts: { ...emptyPetCounts(), terminal: 40 } }))
      .toEqual({ state: 'idle', label: '当前没有运行中的对话' });
  });
  it('prioritizes unsynced, attention, running and paused states truthfully', () => {
    const snapshot = { ...unavailablePetSnapshot(), counts: { running: 2, attention: 1, error: 0, paused: 1, terminal: 4, idle: 2, unknown: 0 } };
    expect(petPresentation(snapshot).label).toBe('状态未同步');
    expect(petPresentation({ ...snapshot, freshness: 'recovering' }).state).toBe('unknown');
    expect(petPresentation({ ...snapshot, freshness: 'synced' }).state).toBe('attention');
    expect(petPresentation({ ...snapshot, freshness: 'synced', counts: { ...snapshot.counts, attention: 0 } }).state).toBe('running');
    expect(petPresentation({ ...snapshot, freshness: 'synced', counts: { ...emptyPetCounts(), paused: 1 } }).label).toBe('1 个对话已暂停');
  });
  it('separates explicit failure from generic attention and keeps concurrent work in the label', () => {
    const snapshot = { ...unavailablePetSnapshot(), freshness: 'synced' as const,
      counts: { ...emptyPetCounts(), error: 1, running: 2 } };
    expect(petPresentation(snapshot)).toEqual({ state: 'error', label: '1 个对话出错 · 2 个进行中' });
    expect(petPresentation({ ...snapshot, counts: { ...snapshot.counts, attention: 1 } }))
      .toEqual({ state: 'attention', label: '1 个对话待查看 · 2 个进行中' });
    expect(petPresentation({ ...snapshot, freshness: 'recovering' }))
      .toEqual({ state: 'unknown', label: '正在重新同步' });
  });
  it('accepts new epochs but rejects stale revisions, unknown schemas and wrong owner scopes', () => {
    const previous = { ...unavailablePetSnapshot(), producerEpoch: 5, revision: 3, sourceId: 'work-directory' as const, scopeId: 's' };
    for (const next of [{ ...previous, revision: 2 }, { ...previous, producerEpoch: 4, revision: 99 }, { ...previous, scopeId: 'other', revision: 4 }, { ...previous, schemaVersion: 2 as 1, revision: 4 }]) expect(acceptPetSnapshot(previous, next)).toBe(previous);
    const next = { ...previous, producerEpoch: 6, revision: 0, scopeId: 'other' };
    expect(acceptPetSnapshot(previous, next)).toBe(next);
  });
});
