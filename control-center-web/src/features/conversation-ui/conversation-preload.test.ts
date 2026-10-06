import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ControlRequest } from '@/platform/transport';
import { MockControlTransport } from '@/test/mock-transport';
import { agentSessionAddress, selectAgentProjection, useAgentLiveStore } from '@/features/agent/state/live-store';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import { preloadRecentConversations } from './conversation-preload';

afterEach(() => {
  vi.useRealTimers();
  useAgentLiveStore.setState({ projections: {} });
  useRoomLiveStore.getState().reset();
});

describe('conversation preload', () => {
  it('hydrates separate transport projections for the same protocol Session without raw-key fallback', async () => {
    const sessionId = 'shared-preload-session';
    const snapshot = (label: string) => ({
      sessionId,
      lastSequence: 1,
      resumeToken: `${sessionId}:1`,
      status: 'idle',
      messages: [{
        schemaVersion: 'rag-ime.agent-message.v1', id: 'same-message', sessionId, turnId: 'same-turn',
        role: 'assistant', status: 'completed', createdAtMs: 1, completedAtMs: 2, attachments: [], citations: [],
        blocks: [{ id: 'same-block', type: 'text', status: 'completed', presentationKind: 'markdown', data: { text: label } }],
      }],
      liveEvents: [],
    });
    const transportA = new MockControlTransport({ routes: { 'agent.session.snapshot': snapshot('Transport A answer') } });
    const transportB = new MockControlTransport({ routes: { 'agent.session.snapshot': snapshot('Transport B answer') } });
    const addressA = agentSessionAddress(transportA, sessionId);
    const addressB = agentSessionAddress(transportB, sessionId);
    const target = [{ kind: 'session' as const, id: sessionId }];

    await preloadRecentConversations(transportA, target).promise;
    const projectionA = selectAgentProjection(useAgentLiveStore.getState(), addressA);
    expect(projectionA?.messagesById['same-message'].blocks[0].data.text).toBe('Transport A answer');
    expect(selectAgentProjection(useAgentLiveStore.getState(), addressB)).toBeUndefined();

    await preloadRecentConversations(transportB, target).promise;
    expect(selectAgentProjection(useAgentLiveStore.getState(), addressA)).toBe(projectionA);
    expect(selectAgentProjection(useAgentLiveStore.getState(), addressB)?.messagesById['same-message'].blocks[0].data.text).toBe('Transport B answer');
    expect(useAgentLiveStore.getState().projections[sessionId]).toBeUndefined();

    await expect(preloadRecentConversations(transportA, target).promise).resolves.toEqual([
      { kind: 'session', id: sessionId, status: 'cached' },
    ]);
    expect(selectAgentProjection(useAgentLiveStore.getState(), addressA)?.messagesById['same-message'].blocks[0].data.text).toBe('Transport A answer');
    expect(transportA.requests).toHaveLength(1);
    expect(transportB.requests).toHaveLength(1);
  });

  it('keeps recent reads bounded and never opens an event stream', async () => {
    let active = 0;
    let maximumActive = 0;
    const releases: Array<() => void> = [];
    const started: string[] = [];
    const snapshot = (id: string) => ({
      lastSequence: 0,
      resumeToken: `${id}:0`,
      status: 'idle',
      messages: [],
      liveEvents: [],
    });
    const transport = new MockControlTransport({ routes: {
      'agent.session.snapshot': (request: ControlRequest) => {
        const id = String(request.params?.sessionId);
        started.push(id);
        active += 1;
        maximumActive = Math.max(maximumActive, active);
        return new Promise((resolve) => {
          releases.push(() => {
            active -= 1;
            resolve(snapshot(id));
          });
        });
      },
    } });
    const warmup = preloadRecentConversations(transport, [
      { kind: 'session', id: 'preload-a' },
      { kind: 'session', id: 'preload-b' },
      { kind: 'session', id: 'preload-c' },
      { kind: 'session', id: 'preload-too-many' },
    ]);
    await Promise.resolve();
    expect(started).toEqual(['preload-a', 'preload-b']);
    expect(maximumActive).toBe(2);
    expect(transport.requests.every(({ request }) => request.pathId === 'agent.session.snapshot')).toBe(true);
    expect(transport.requests.every(({ request }) => request.query?.view === 'recent')).toBe(true);
    expect(transport.subscriptionCalls).toHaveLength(0);

    releases.shift()?.();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(started).toContain('preload-c');
    releases.shift()?.();
    releases.shift()?.();
    await expect(warmup.promise).resolves.toHaveLength(3);
    expect(maximumActive).toBe(2);
    expect(started).not.toContain('preload-too-many');
  });

  it('cancels pending warmup reads and settles each target instead of hanging forever', async () => {
    let requestSignal: AbortSignal | undefined;
    const transport = new MockControlTransport({ routes: {
      'agent.session.snapshot': (request: ControlRequest) => {
        requestSignal = request.signal;
        return new Promise(() => undefined);
      },
    } });
    const warmup = preloadRecentConversations(transport, [{ kind: 'session', id: 'preload-a' }]);
    await Promise.resolve();
    warmup.cancel();
    await expect(warmup.promise).resolves.toEqual([
      expect.objectContaining({ kind: 'session', id: 'preload-a', status: 'cancelled' }),
    ]);
    expect(requestSignal?.aborted).toBe(true);
  });

  it('bounds request caches and keeps recently used projections after request-cache expiry', async () => {
    vi.useFakeTimers({ now: 1_000 });
    let sessionRequestCount = 0;
    let roomRequestCount = 0;
    const snapshot = (id: string) => ({
      lastSequence: 0,
      resumeToken: `${id}:0`,
      status: 'idle',
      messages: [],
      liveEvents: [],
    });
    const roomSnapshot = (id: string) => ({
      schemaVersion: 'rag-ime.agent-room-conversation-snapshot.v1',
      ok: true,
      room: {
        schemaVersion: 'rag-ime.agent-room.v1',
        id,
        title: 'Bounded Room',
        status: 'active',
        routingPolicy: 'moderator',
        moderatorParticipantId: 'participant-1',
        workspaceRoots: ['/tmp'],
        executionMode: 'workspace_managed',
        permissionPolicy: {
          schemaVersion: 'rag-ime.room-permission-policy.v1',
          room: { executionMode: 'workspace_managed' },
          partner: { executionMode: 'inherit' },
          toolAgent: { executionMode: 'inherit' },
        },
        createdAtMs: 0,
        updatedAtMs: 0,
        lastEventSequence: 0,
        participants: [
          {
            schemaVersion: 'rag-ime.agent-participant.v1',
            id: `${id}:participant-1`,
            roomId: id,
            sessionId: `${id}:session-1`,
            roleId: 'companion-present-v1',
            roleVersion: '1',
            displayName: 'Coordinator',
            collaborationRole: 'coordinator',
            status: 'active',
            ordinal: 0,
            createdAtMs: 0,
            lastSpokeAtMs: null,
          },
          {
            schemaVersion: 'rag-ime.agent-participant.v1',
            id: `${id}:participant-2`,
            roomId: id,
            sessionId: `${id}:session-2`,
            roleId: 'companion-firstlight-v1',
            roleVersion: '1',
            displayName: 'Researcher',
            collaborationRole: 'researcher',
            status: 'active',
            ordinal: 1,
            createdAtMs: 0,
            lastSpokeAtMs: null,
          },
        ],
      },
      events: [],
      firstEventSequence: 0,
      cursorSequence: 0,
      resumeToken: '',
      deferredEventCount: 0,
      truncated: false,
    });
    const transport = new MockControlTransport({ routes: {
      'agent.session.snapshot': (request: ControlRequest) => {
        sessionRequestCount += 1;
        return snapshot(String(request.params?.sessionId));
      },
      'agent.room.conversationSnapshot': (request: ControlRequest) => {
        roomRequestCount += 1;
        return roomSnapshot(String(request.params?.roomId));
      },
    } });

    for (let index = 0; index < 25; index += 1) {
      await preloadRecentConversations(transport, [{ kind: 'session', id: `bounded-${index}` }]).promise;
    }

    expect(sessionRequestCount).toBe(25);
    // Request caching and inactive settled projection retention have separate
    // bounds. Recent history stays warm; an evicted projection can be re-read.
    expect(Object.keys(useAgentLiveStore.getState().projections)).toHaveLength(24);
    expect(selectAgentProjection(useAgentLiveStore.getState(), agentSessionAddress(transport, 'bounded-0'))).toBeUndefined();
    expect(selectAgentProjection(useAgentLiveStore.getState(), agentSessionAddress(transport, 'bounded-24'))).toBeDefined();
    await preloadRecentConversations(transport, [{ kind: 'session', id: 'bounded-24' }]).promise;
    expect(sessionRequestCount).toBe(25);
    await preloadRecentConversations(transport, [{ kind: 'session', id: 'bounded-0' }]).promise;
    expect(sessionRequestCount).toBe(26);
    expect(selectAgentProjection(useAgentLiveStore.getState(), agentSessionAddress(transport, 'bounded-0'))).toBeDefined();

    for (let index = 0; index < 25; index += 1) {
      const result = await preloadRecentConversations(transport, [{ kind: 'room', id: `bounded-room-${index}` }]).promise;
      expect(result[0]).toMatchObject({ status: 'ready' });
    }
    expect(roomRequestCount).toBe(25);
    expect(useRoomLiveStore.getState().projections['bounded-room-0']).toBeDefined();
    await preloadRecentConversations(transport, [{ kind: 'room', id: 'bounded-room-24' }]).promise;
    expect(roomRequestCount).toBe(25);
    await preloadRecentConversations(transport, [{ kind: 'room', id: 'bounded-room-0' }]).promise;
    expect(roomRequestCount).toBe(26);

    vi.setSystemTime(22_000);
    await preloadRecentConversations(transport, [{ kind: 'session', id: 'bounded-24' }]).promise;
    expect(sessionRequestCount).toBe(27);
    await preloadRecentConversations(transport, [{ kind: 'session', id: 'bounded-24' }]).promise;
    expect(sessionRequestCount).toBe(27);
    await preloadRecentConversations(transport, [{ kind: 'room', id: 'bounded-room-24' }]).promise;
    expect(roomRequestCount).toBe(27);
    await preloadRecentConversations(transport, [{ kind: 'room', id: 'bounded-room-24' }]).promise;
    expect(roomRequestCount).toBe(27);
    expect(selectAgentProjection(useAgentLiveStore.getState(), agentSessionAddress(transport, 'bounded-0'))).toBeDefined();
    expect(useRoomLiveStore.getState().projections['bounded-room-0']).toBeDefined();
  });
});
