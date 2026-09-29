import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { readRoomEntryMode, rememberRoomEntryMode, useRoomEntryMode } from './room-entry-mode';

beforeEach(() => localStorage.clear());
afterEach(cleanup);
const catalog = (roomId: string, hasGraph = false) => ({ ok: true, mode: 'jev', items: hasGraph ? [{ graph_id: `graph:${roomId}`, room_id: roomId }] : [] });
function transport(identity = 'backend-one', hasGraph = false) {
  const value = new MockControlTransport({ routes: {
    'agent.jev.get': (request: ControlRequest) => catalog(String(request.params?.roomId), hasGraph),
    'agent.room.get': (request: ControlRequest) => ({ ok: true, room: { id: request.params?.roomId, roomKind: 'collaboration' } }),
  } });
  Object.defineProperty(value, 'connectionIdentity', { value: identity });
  return value;
}
describe('Room entry presentation recovery', () => {
  it('discovers a graph created while the empty Room is open and coalesces its event burst', async () => {
    let hasGraph = false;
    const value = new MockControlTransport({ routes: { 'agent.jev.get': () => catalog('open-room', hasGraph) } });
    const { result } = renderHook(() => useRoomEntryMode({ roomId: 'open-room', roomKind: 'collaboration', transport: value }));
    await waitFor(() => expect(result.current.mode).toBe('traditional'));
    const before = value.requests.length;
    hasGraph = true;
    const event = { roomId: 'open-room', eventType: 'participant_status', payload: { status: 'jev_updated', graphId: 'new-graph' } };
    act(() => { result.current.onEvents([{ ...event, roomId: 'other-room' }]); result.current.onEvents([event]); result.current.onEvents([event]); });
    await waitFor(() => expect(result.current.mode).toBe('jev'));
    expect(value.requests).toHaveLength(before + 1);
    expect(value.requests.every(call => call.request.pathId === 'agent.jev.get')).toBe(true);
  });
  it('restores an explicit new Room choice before its first graph and isolates backends and Rooms', async () => {
    rememberRoomEntryMode(transport(), 'new-room', 'jev');
    const reopened = transport();
    expect(readRoomEntryMode(reopened, 'new-room')).toBe('jev');
    expect(readRoomEntryMode(transport('another-backend'), 'new-room')).toBeUndefined();
    expect(readRoomEntryMode(reopened, 'another-room')).toBeUndefined();
    const { result } = renderHook(() => useRoomEntryMode({ roomId: 'new-room', transport: reopened }));
    expect(result.current.mode).toBe('jev');
    await waitFor(() => expect(result.current.checking).toBe(false));
    expect(result.current.mode).toBe('jev');
    expect(reopened.requests.every(call => ['agent.room.get', 'agent.jev.get'].includes(call.request.pathId))).toBe(true);
  });
  it('keeps old empty Rooms traditional and prefers actual graphs over an earlier local hint', async () => {
    const old = transport();
    const first = renderHook(() => useRoomEntryMode({ roomId: 'old-room', transport: old }));
    await waitFor(() => expect(first.result.current.mode).toBe('traditional'));
    first.unmount();
    const graphOwner = transport('backend-one', true);
    expect(readRoomEntryMode(graphOwner, 'old-room')).toBe('traditional');
    const next = renderHook(() => useRoomEntryMode({ roomId: 'old-room', transport: graphOwner }));
    await waitFor(() => expect(next.result.current.mode).toBe('jev'));
  });
  it('does not guess an owner on read failure and keeps an already known choice while retrying', async () => {
    let fails = true;
    const value = transport(); const original = value.request.bind(value);
    value.request = async <Response,>(request: ControlRequest): Promise<Response> => {
      if (request.pathId === 'agent.jev.get' && fails) throw new Error('连接中断');
      return original<Response>(request);
    };
    const { result } = renderHook(() => useRoomEntryMode({ roomId: 'unknown-room', transport: value }));
    await waitFor(() => expect(result.current.error).not.toBe(''));
    expect(result.current.mode).toBeUndefined();
    fails = false; act(() => result.current.refresh());
    await waitFor(() => expect(result.current.mode).toBe('traditional'));
    fails = true; act(() => result.current.refresh());
    await waitFor(() => expect(result.current.error).not.toBe(''));
    expect(result.current.mode).toBe('traditional');
  });
  it('ignores a late graph response from a previously selected Room', async () => {
    let release!: (value: unknown) => void;
    const value = transport(); const original = value.request.bind(value);
    value.request = async <Response,>(request: ControlRequest): Promise<Response> => request.pathId === 'agent.jev.get' && request.params?.roomId === 'first'
      ? new Promise(resolve => { release = resolve as (value: unknown) => void; })
      : original<Response>(request);
    const { result, rerender } = renderHook(({ roomId }) => useRoomEntryMode({ roomId, roomKind: 'collaboration', transport: value }), { initialProps: { roomId: 'first' } });
    await waitFor(() => expect(release).toBeDefined());
    rerender({ roomId: 'second' });
    await waitFor(() => expect(result.current.mode).toBe('traditional'));
    await act(async () => release(catalog('first', true)));
    expect(result.current.mode).toBe('traditional');
    expect(readRoomEntryMode(value, 'first')).toBeUndefined();
  });
  it('always preserves roleplay even when an earlier UI hint says Jev', async () => {
    const value = transport(); rememberRoomEntryMode(value, 'roleplay', 'jev');
    const original = value.request.bind(value);
    value.request = async <Response,>(request: ControlRequest): Promise<Response> => request.pathId === 'agent.room.get'
      ? { ok: true, room: { id: 'roleplay', roomKind: 'roleplay' } } as Response : original<Response>(request);
    const { result } = renderHook(() => useRoomEntryMode({ roomId: 'roleplay', transport: value }));
    await waitFor(() => expect(result.current.mode).toBe('traditional'));
    expect(value.requests.some(call => call.request.pathId === 'agent.jev.get')).toBe(false);
  });
});
