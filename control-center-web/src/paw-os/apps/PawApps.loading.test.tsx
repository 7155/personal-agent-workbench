import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { lazy, Suspense } from 'react';

afterEach(() => {
  cleanup();
  for (const path of ['./PawAppsRuntime', './PawRoomWorkspace', './PawSessionWorkspace']) vi.doUnmock(path);
  vi.resetModules();
});

function pendingRuntime() {
  const evaluated = { room: 0, session: 0 };
  let release!: () => void;
  const ready = new Promise<void>(resolve => { release = resolve; });
  const entry = vi.fn();
  vi.resetModules();
  vi.doMock('./PawAppsRuntime', async () => {
    await ready;
    return { warmPawAppBody: entry, PawAppBody: () => null };
  });
  vi.doMock('./PawRoomWorkspace', () => {
    evaluated.room += 1;
    return { PawRoomWorkspace: () => null };
  });
  vi.doMock('./PawSessionWorkspace', () => {
    evaluated.session += 1;
    return { PawSessionWorkspace: () => null };
  });
  return { evaluated, entry, release };
}

describe('workspace launch intent', () => {
  it('starts a precise Room intent before the App Runtime/Entry is ready', async () => {
    const state = pendingRuntime();
    try {
      const { warmPawAppProcess } = await import('./PawApps');
      warmPawAppProcess('agent', 'room');
      await waitFor(() => expect(state.evaluated).toEqual({ room: 1, session: 0 }));
      expect(state.entry).not.toHaveBeenCalled();
    } finally { state.release(); }
  });

  it('starts a direct Room route while the outer App boundary is pending', async () => {
    const state = pendingRuntime();
    try {
      const { PawAppProcess } = await import('./PawApps');
      render(<PawAppProcess appId="agent" initialRoute="/agent?room=room-first" />);
      await waitFor(() => expect(state.evaluated).toEqual({ room: 1, session: 0 }));
      expect(state.entry).not.toHaveBeenCalled();
    } finally { await act(async () => state.release()); }
  });

  it('shares the module result across repeated intent and the lazy loader', async () => {
    const state = pendingRuntime();
    try {
      const loader = await import('./agent-workspace-loader');
      const { warmPawAppProcess } = await import('./PawApps');
      warmPawAppProcess('agent', 'room');
      const first = loader.loadRoomWorkspace();
      warmPawAppProcess('agent', 'room');
      warmPawAppProcess('agent', 'room');
      expect(loader.loadRoomWorkspace()).toBe(first);
      await first;
      expect(state.evaluated).toEqual({ room: 1, session: 0 });
    } finally { state.release(); }
  });

  it('allows a later first render to recover after a failed prefetch', async () => {
    const state = pendingRuntime();
    vi.doMock('./PawRoomWorkspace', () => { throw new Error('prefetch connection failed'); });
    try {
      const loader = await import('./agent-workspace-loader');
      loader.warmAgentWorkspace('room');
      const failed = loader.loadRoomWorkspace();
      await expect(failed).rejects.toMatchObject({ cause: { message: 'prefetch connection failed' } });
      vi.doMock('./PawRoomWorkspace', () => ({ PawRoomWorkspace: () => <main>Recovered Room workspace</main> }));
      const retry = loader.loadRoomWorkspace();
      expect(retry).not.toBe(failed);
      expect((await retry).default).toBeTypeOf('function');
      const Room = lazy(loader.loadRoomWorkspace);
      render(<Suspense fallback={<div>Loading retry</div>}><Room personas={[]} recordId="room-retry" onRoomUpdated={vi.fn()} /></Suspense>);
      expect(await screen.findByText('Recovered Room workspace')).toBeInTheDocument();
    } finally { state.release(); }
  });

  it.each([
    { initialRoute: '/rooms?room=room-exact&draft=hello', expected: 'room' },
    { initialRoute: '/agent?sessionId=session-exact', expected: 'session' },
    { entityId: 'session-exact', expected: 'session' },
    { initialRoute: '/agent?session=session-exact&draft=new-work', expected: undefined },
    { target: { kind: 'room', id: 'room-exact', title: 'Room' } as const, expected: 'room' },
    { target: { kind: 'room', id: 'room-exact', title: 'Room', panel: 'focus' } as const, expected: undefined },
    { target: { kind: 'participant', id: 'earth', roomId: 'room-exact', sessionId: 'session-exact', title: 'Earth' } as const, expected: undefined },
  ])('keeps exact route/target admission separate from satellite or new-work intent: $expected', async input => {
    const { agentWorkspaceIntent } = await import('./agent-workspace-loader');
    expect(agentWorkspaceIntent({ appId: 'agent', ...input })).toBe(input.expected);
  });

  it('evaluates the real Room module without making transport or Provider requests', async () => {
    vi.resetModules();
    vi.doUnmock('./PawRoomWorkspace');
    vi.doMock('@/app/control-transport', () => import('@/app/control-transport.http'));
    const fetch = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('module warmup must not issue requests'));
    try {
      const { loadRoomWorkspace } = await import('./agent-workspace-loader');
      expect((await loadRoomWorkspace()).default).toBeTypeOf('function');
      expect(fetch).not.toHaveBeenCalled();
    } finally {
      fetch.mockRestore();
      vi.doUnmock('@/app/control-transport');
    }
  });

  it('keeps a generic Agent launcher limited to its Home entry', async () => {
    const state = pendingRuntime();
    const { warmPawAppProcess } = await import('./PawApps');
    warmPawAppProcess('agent');
    await act(async () => state.release());
    await waitFor(() => expect(state.entry).toHaveBeenCalledWith('agent'));
    expect(state.evaluated).toEqual({ room: 0, session: 0 });
  });
});
