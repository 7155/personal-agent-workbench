import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { previewRoomSnapshot } from '@/app/preview-room-data';
import { roomEventFixture } from '@/test/fixtures/events';
import { MockControlTransport } from '@/test/mock-transport';
import { useRoomLiveSession } from '@/features/rooms/runtime/use-room-live-session';
import { useRoomLiveStore, roomProjection } from '@/features/rooms/state/live-store';
import type { ControlEventObserver, ControlSubscription, ControlRequest } from '@/platform/transport';
import type { UiRoomEvent } from '@/contracts/ui-events';

const callbacks = () => ({ onLoadingChange: vi.fn(), onSnapshot: vi.fn(), onMetadata: vi.fn(),
  onConnectionRestored: vi.fn(), onConnectionError: vi.fn(), onRecoveryState: vi.fn(), onEvents: vi.fn() });
function event(sequence: number, roomId='room-1', type='room_config_changed') {
  return { ...roomEventFixture(sequence, type, {}), roomId, eventId: `${roomId}:${sequence}`, resumeToken: `${roomId}:${sequence}` };
}
function wire<T extends { streamKind?: unknown }>(value: T) { const { streamKind: _, ...rest } = value; return rest; }
function snapshot(roomId='room-1', first=1, cursor=0) {
  const base = previewRoomSnapshot(roomId);
  return { ...base, room: { ...base.room, lastEventSequence: cursor },
    events: Array.from({length: Math.max(0,cursor-first+1)}, (_,i)=>wire(event(first+i, roomId))),
    firstSequence: cursor ? first : 0, lastSequence: cursor, resumeToken: cursor ? `${roomId}:${cursor}` : '', truncated:first>1 };
}
async function flush() { await act(async () => { for(let i=0;i<15;i++) await Promise.resolve(); }); }
afterEach(() => { cleanup(); useRoomLiveStore.getState().reset(); vi.useRealTimers(); });

it('late released Room stream callbacks cannot mutate the next Room or restore connection', async () => {
  vi.useFakeTimers();
  const transport=new MockControlTransport({ routes: { 'agent.room.snapshot': (request:ControlRequest)=>snapshot(String(request.params?.roomId)) } });
  const observers:ControlEventObserver<UiRoomEvent>[]=[];
  const original=transport.subscribe.bind(transport);
  transport.subscribe=((request:ControlSubscription, observer:ControlEventObserver<UiRoomEvent>)=>{ observers.push(observer); return original(request, observer); }) as typeof transport.subscribe;
  const cb=callbacks();
  const hook=renderHook(({roomId})=>useRoomLiveSession({roomId,transport,...cb}),{initialProps:{roomId:'room-a'}});
  await flush(); expect(observers).toHaveLength(1);
  hook.rerender({roomId:'room-b'}); await flush(); expect(observers).toHaveLength(2);
  const before=roomProjection('room-b'); cb.onConnectionRestored.mockClear(); cb.onEvents.mockClear();
  act(()=>{ observers[0].next(event(1,'room-a')); observers[0].stable?.('room-1:11'); observers[0].error?.(new Error('late')); });
  await act(async()=>{await vi.advanceTimersByTimeAsync(100);});
  expect(roomProjection('room-b')).toBe(before); expect(roomProjection('room-a').lastSequence).toBe(0);
  expect(cb.onConnectionRestored).not.toHaveBeenCalled(); expect(cb.onEvents).not.toHaveBeenCalled();
  expect(transport.activeSubscriptionCount()).toBe(1);
});

it('Room recovery reset drops old prefix and rejects callbacks queued by the old stream', async () => {
  vi.useFakeTimers(); let cursor=10;
  const transport=new MockControlTransport({ routes: { 'agent.room.snapshot': ()=>snapshot('room-1',1,cursor) } });
  const observers:ControlEventObserver<UiRoomEvent>[]=[];
  const original=transport.subscribe.bind(transport);
  transport.subscribe=((request:ControlSubscription, observer:ControlEventObserver<UiRoomEvent>)=>{ observers.push(observer); return original(request,observer); }) as typeof transport.subscribe;
  const cb=callbacks(); renderHook(()=>useRoomLiveSession({roomId:'room-1',transport,...cb})); await flush();
  cursor=5;
  act(()=>{ observers[0].next({...event(6,'room-1','snapshot_required'),eventId:'room-1:snapshot-required:5',resumeToken:'room-1:5'}); });
  await act(async()=>{await vi.advanceTimersByTimeAsync(100);}); await flush();
  expect(roomProjection('room-1')).toMatchObject({lastSequence:5,needsSnapshot:false});
  expect(useRoomLiveStore.getState().historyByRoomId['room-1'].events.map(e=>e.sequence)).toEqual([1,2,3,4,5]);
  act(()=>{observers[0].next(event(11));observers[0].stable?.('room-1:11');}); await flush();
  expect(roomProjection('room-1').lastSequence).toBe(5); expect(transport.activeSubscriptionCount()).toBe(1);
});

it.each(['fallback', 'deferred'])('Room %s full-prefix hydration propagates the declared permanent retention floor', async mode => {
  vi.useFakeTimers();
  const transport=new MockControlTransport({ routes:{
    ...(mode === 'deferred' ? { 'agent.room.conversationSnapshot': {
      schemaVersion: 'rag-ime.agent-room-conversation-snapshot.v1', ok: true, room: snapshot('room-1',4,6).room,
      events: [], firstEventSequence: 0, cursorSequence: 6, resumeToken: 'room-1:6', deferredEventCount: 6, truncated: true,
    } } : {}),
    'agent.room.snapshot':snapshot('room-1',4,6),
    'agent.room.history':{schemaVersion:'rag-ime.agent-room-event-page.v1',ok:true,roomId:'room-1',items:[wire(event(3))],
      firstSequence:3,lastSequence:3,nextBeforeSequence:0,hasMore:false,retainedFirstSequence:3,retainedLastSequence:6,retainedPrefixTruncated:true},
  } });
  renderHook(()=>useRoomLiveSession({roomId:'room-1',transport,...callbacks()})); await flush();
  if (mode === 'deferred') { await act(async()=>{await vi.advanceTimersByTimeAsync(120);}); await flush(); }
  const history=useRoomLiveStore.getState().historyByRoomId['room-1'];
  expect(history.events.map(e=>e.sequence)).toEqual([3,4,5,6]);
  expect(roomProjection('room-1')).toMatchObject({lastSequence:6,needsSnapshot:false});
  expect(history).toMatchObject({retainedFirstSequence:3,retainedPrefixTruncated:true,hasMore:false});
});



it('a cold Room snapshot finishing after a Room switch cannot publish to the new owner', async () => {
  vi.useFakeTimers();
  let resolve!: (value: unknown) => void;
  const pending = new Promise<unknown>(yes => { resolve = yes; });
  const transport = new MockControlTransport({ routes: {
    'agent.room.snapshot': (request: ControlRequest) => snapshot(String(request.params?.roomId)),
  } });
  const request = transport.request.bind(transport);
  transport.request = ((input: ControlRequest) => input.pathId === 'agent.room.conversationSnapshot'
    && input.params?.roomId === 'room-a' ? pending : request(input)) as typeof transport.request;
  const cb = callbacks();
  const hook = renderHook(({ roomId }) => useRoomLiveSession({ roomId, transport, ...cb }), { initialProps: { roomId: 'room-a' } });
  await flush(); hook.rerender({ roomId: 'room-b' }); await flush();
  const before = roomProjection('room-b'); cb.onSnapshot.mockClear();
  await act(async () => { resolve({ schemaVersion: 'rag-ime.agent-room-conversation-snapshot.v1', ok: true,
    room: snapshot('room-a').room, events: [], firstEventSequence: 0, cursorSequence: 0,
    resumeToken: '', deferredEventCount: 0, truncated: false }); }); await flush();
  expect(roomProjection('room-b')).toBe(before); expect(cb.onSnapshot).not.toHaveBeenCalled();
  expect(transport.activeSubscriptionCount()).toBe(1);
});
