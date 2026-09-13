import { describe, expect, it, vi } from 'vitest';

import sessionFixture from '../../../tests/fixtures/agent/agent-session.json';

import type {
  NativeBridgeOutboundEnvelope,
  NativeBridgeRequestEnvelope,
} from './native-bridge';
import { NativeControlTransport } from './native-transport';
import { agentEventFixture } from '@/test/fixtures/events';

describe('NativeControlTransport', () => {
  it('honors a bounded per-request observation timeout without changing the native wire payload', async () => {
    vi.useFakeTimers();
    const sent: NativeBridgeRequestEnvelope[] = [];
    const bridgeWindow = fakeBridgeWindow((envelope) => sent.push(envelope));
    const transport = new NativeControlTransport({ bridgeWindow, createId: () => 'long-prepare' });
    try {
      const pending = transport.request({ pathId: 'agent.eval-lab.projects.command', timeoutMs: 300_000,
        body: { action: 'prepare_app', projectId: 'project-one', expectedRevision: 14,
          clientRequestId: 'lab-project:original', input: { directory: 'app' } } });
      const result = expect(pending).resolves.toEqual({ ok: true });
      await vi.advanceTimersByTimeAsync(202_000);
      expect(sent).toHaveLength(1); expect(sent[0]?.payload).not.toHaveProperty('timeoutMs');
      bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({ id: 'long-prepare', ok: true, result: { ok: true } });
      await result;
      const expired = transport.request({ pathId: 'system.health', timeoutMs: 300_000 });
      const rejected = expect(expired).rejects.toMatchObject({ code: 'timeout' });
      await vi.advanceTimersByTimeAsync(300_000); await rejected;
      expect(sent.at(-1)?.method).toBe('cancelRequest');
      await expect(transport.request({ pathId: 'system.health', timeoutMs: 300_001 })).rejects.toThrow('timeoutMs');
    } finally { transport.dispose(); vi.useRealTimers(); }
  });

  it('uses the allowlisted loopback image route without loading the HTTP transport', () => {
    const transport = new NativeControlTransport({
      bridgeWindow: fakeBridgeWindow(() => {}),
    });

    expect(transport.browserSnapshotImageUrl('snap_native-1')).toBe(
      'http://127.0.0.1:8766/api/browser/snapshots/snap_native-1/image',
    );
    expect(transport.agentMediaContentUrl(
      '/api/agent/media/media_native_fixture_01/content?sessionId=session:native-1',
    )).toBe(
      'http://127.0.0.1:8766/api/agent/media/media_native_fixture_01/content?sessionId=session%3Anative-1',
    );
    expect(() => transport.agentMediaContentUrl(
      '/api/agent/media/media_native_fixture_01/content?sessionId=session:native-1&next=private',
    )).toThrow(/managed receipt path/);
    expect(() => transport.browserSnapshotImageUrl('../private')).toThrow(/bounded snapshotId/);
    transport.dispose();
  });

  it('matches the fixed WKWebView bridge envelope and validates results', async () => {
    const sent: NativeBridgeRequestEnvelope[] = [];
    let nextId = 1;
    const bridgeWindow = fakeBridgeWindow((envelope) => {
      sent.push(envelope);
      queueMicrotask(() => {
        bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
          id: envelope.id,
          ok: true,
          result:
            envelope.method === 'capabilities'
              ? capabilitiesFixture()
              : envelope.method === 'request'
                ? sessionFixture
                : {},
        });
      });
    });
    const transport = new NativeControlTransport({
      bridgeWindow,
      createId: () => `bridge-${nextId++}`,
    });

    const capabilities = await transport.capabilities();
    expect(capabilities).toMatchObject({
      transport: 'native',
      native: {
        pickFiles: true,
        managedAgentImageImport: true,
        knowledgeDocumentImport: true,
        knowledgeParserStatus: true,
        knowledgeAssetRead: true,
        knowledgeDocumentSourceRead: true,
        revealPath: true,
        keychain: true,
        tcc: true,
      },
    });
    await expect(
      transport.request({
        pathId: 'system.health',
        responseContract: 'agent-session.v1',
      }),
    ).resolves.toMatchObject({ id: 'agent-session-1' });

    expect(sent[0]).toEqual({ id: 'bridge-1', method: 'capabilities', payload: {} });
    expect(sent[1]).toEqual({
      id: 'bridge-2',
      method: 'request',
      payload: { pathId: 'system.health' },
    });
    expect(JSON.stringify(sent[1])).not.toMatch(/url|host|responseContract/);
    transport.dispose();
  });

  it('serializes numeric and boolean query values for the Swift bridge', async () => {
    const sent: NativeBridgeRequestEnvelope[] = [];
    const bridgeWindow = fakeBridgeWindow((envelope) => {
      sent.push(envelope);
      queueMicrotask(() => bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        id: envelope.id,
        ok: true,
        result: { ok: true, sessions: [] },
      }));
    });
    const transport = new NativeControlTransport({
      bridgeWindow,
      createId: () => 'query-call',
    });

    await transport.request({
      pathId: 'agent.sessions.list',
      query: { limit: 100, includeArchived: false },
    });

    expect(sent).toEqual([{
      id: 'query-call',
      method: 'request',
      payload: {
        pathId: 'agent.sessions.list',
        query: { limit: '100', includeArchived: 'false' },
      },
    }]);
    transport.dispose();
  });

  it('does not cancel a conversation fork at the ordinary native request timeout', async () => {
    vi.useFakeTimers();
    try {
      const sent: NativeBridgeRequestEnvelope[] = [];
      const bridgeWindow = fakeBridgeWindow((envelope) => {
        sent.push(envelope);
        globalThis.setTimeout(() => bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
          id: envelope.id,
          ok: true,
          result: {
            schemaVersion: 'rag-ime.agent-session-fork-create.v1',
            ok: true,
            sourceSessionId: 'agent:source',
            entryId: 'entry-user-1',
            selectedText: '从这里继续',
            session: sessionFixture,
          },
        }), 150);
      });
      const transport = new NativeControlTransport({
        bridgeWindow,
        createId: () => 'fork-call',
        requestTimeoutMs: 100,
      });

      const fork = transport.request({
        pathId: 'agent.session.forks.create',
        params: { sessionId: 'agent:source' },
        body: { entryId: 'entry-user-1', title: '新分支' },
      });
      const result = expect(fork).resolves.toMatchObject({
        sourceSessionId: 'agent:source',
        entryId: 'entry-user-1',
      });

      await vi.advanceTimersByTimeAsync(150);
      await result;
      expect(sent).toHaveLength(1);
      expect(sent[0]).toMatchObject({ method: 'request' });
      transport.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it('multiplexes subscribe/event/cancelSubscription with resume cursors', async () => {
    const sent: NativeBridgeRequestEnvelope[] = [];
    let nextId = 1;
    const bridgeWindow = fakeBridgeWindow((envelope) => {
      sent.push(envelope);
      queueMicrotask(() => {
        bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
          id: envelope.id,
          ok: true,
          result: {},
        });
      });
    });
    const transport = new NativeControlTransport({
      bridgeWindow,
      createId: () => `native-${nextId++}`,
    });
    const events: unknown[] = [];
    const cancel = transport.subscribe(
      {
        pathId: 'agent.session.events',
        params: { sessionId: 'session-1' },
        lastEventId: 'session-1:8',
      },
      { next: (event) => events.push(event) },
    );
    await Promise.resolve();
    const subscribe = sent.find((item) => item.method === 'subscribe');
    expect(subscribe?.payload).toEqual({
      subscriptionId: 'native-1',
      request: {
        pathId: 'agent.session.events',
        params: { sessionId: 'session-1' },
        lastEventId: 'session-1:8',
      },
    });

    const outbound: NativeBridgeOutboundEnvelope = {
      subscriptionId: 'native-1',
      kind: 'event',
      event: agentEventFixture(9, 'turn_completed', {}),
      lastEventId: 'session-1:9',
    };
    bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive(outbound);
    expect(events).toHaveLength(1);
    cancel();
    await Promise.resolve();
    expect(sent.find((item) => item.method === 'cancelSubscription')?.payload).toEqual({
      subscriptionId: 'native-1',
      lastEventId: 'session-1:9',
    });
    transport.dispose();
  });

  it('reconnects a completed native stream from the last acknowledged cursor', async () => {
    vi.useFakeTimers();
    try {
      const sent: NativeBridgeRequestEnvelope[] = [];
      let nextId = 1;
      const bridgeWindow = fakeBridgeWindow((envelope) => {
        sent.push(envelope);
        queueMicrotask(() => {
          bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
            id: envelope.id,
            ok: true,
            result: {},
          });
        });
      });
      const reconnects: unknown[] = [];
      const transport = new NativeControlTransport({
        bridgeWindow,
        createId: () => `resume-${nextId++}`,
      });
      const cancel = transport.subscribe(
        {
          pathId: 'agent.session.events',
          params: { sessionId: 'session-1' },
          lastEventId: 'session-1:8',
        },
        {
          next: () => undefined,
          reconnect: (state) => reconnects.push(state),
        },
      );
      await Promise.resolve();

      bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        subscriptionId: 'resume-1',
        kind: 'complete',
        lastEventId: 'session-1:12',
      });
      expect(reconnects).toEqual([
        { attempt: 1, delayMs: 250, lastEventId: 'session-1:12' },
      ]);

      await vi.advanceTimersByTimeAsync(250);
      const subscribeCalls = sent.filter((item) => item.method === 'subscribe');
      expect(subscribeCalls).toHaveLength(2);
      expect(subscribeCalls[1]?.payload).toEqual({
        subscriptionId: 'resume-1',
        request: {
          pathId: 'agent.session.events',
          params: { sessionId: 'session-1' },
          lastEventId: 'session-1:12',
        },
      });
      cancel();
      await Promise.resolve();
      transport.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it('reconnects from the previous cursor when observer delivery fails', async () => {
    vi.useFakeTimers();
    try {
      const sent: NativeBridgeRequestEnvelope[] = [];
      let nextId = 1;
      const bridgeWindow = fakeBridgeWindow((envelope) => {
        sent.push(envelope);
        queueMicrotask(() => {
          bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
            id: envelope.id,
            ok: true,
            result: {},
          });
        });
      });
      const reconnects: unknown[] = [];
      let deliveries = 0;
      const transport = new NativeControlTransport({
        bridgeWindow,
        createId: () => `delivery-${nextId++}`,
      });
      const cancel = transport.subscribe(
        {
          pathId: 'agent.session.events',
          params: { sessionId: 'session-1' },
          lastEventId: 'session-1:8',
        },
        {
          next: () => {
            deliveries += 1;
            if (deliveries === 1) throw new Error('projection commit failed');
          },
          reconnect: (state) => reconnects.push(state),
        },
      );
      await Promise.resolve();

      bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        subscriptionId: 'delivery-1',
        kind: 'event',
        event: agentEventFixture(9, 'turn_completed', {}),
        lastEventId: 'session-1:9',
      });
      bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        subscriptionId: 'delivery-1',
        kind: 'event',
        event: agentEventFixture(10, 'turn_completed', {}),
        lastEventId: 'session-1:10',
      });

      expect(deliveries).toBe(1);
      expect(reconnects).toEqual([
        { attempt: 1, delayMs: 250, lastEventId: 'session-1:8' },
      ]);
      expect(sent.filter((item) => item.method === 'cancelSubscription')).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(250);
      const subscribeCalls = sent.filter((item) => item.method === 'subscribe');
      expect(subscribeCalls[1]?.payload).toEqual({
        subscriptionId: 'delivery-1',
        request: {
          pathId: 'agent.session.events',
          params: { sessionId: 'session-1' },
          lastEventId: 'session-1:8',
        },
      });

      bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        subscriptionId: 'delivery-1',
        kind: 'event',
        event: agentEventFixture(9, 'turn_completed', {}),
        lastEventId: 'session-1:9',
      });
      expect(deliveries).toBe(2);
      cancel();
      await Promise.resolve();
      transport.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not reconnect from a transient native snapshot-required id', async () => {
    vi.useFakeTimers();
    try {
      const sent: NativeBridgeRequestEnvelope[] = [];
      let nextId = 1;
      const bridgeWindow = fakeBridgeWindow((envelope) => {
        sent.push(envelope);
        queueMicrotask(() => {
          bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
            id: envelope.id,
            ok: true,
            result: {},
          });
        });
      });
      const reconnects: unknown[] = [];
      const transport = new NativeControlTransport({
        bridgeWindow,
        createId: () => `snapshot-${nextId++}`,
      });
      const cancel = transport.subscribe(
        {
          pathId: 'agent.session.events',
          params: { sessionId: 'session-1' },
          lastEventId: 'session-1:8',
        },
        {
          next: () => undefined,
          reconnect: (state) => reconnects.push(state),
        },
      );
      await Promise.resolve();

      const control = {
        ...agentEventFixture(10, 'snapshot_required', {
          reason: 'event_replay_gap',
          afterEventId: 'session-1:8',
        }),
        eventId: 'session-1:snapshot-required:9',
        resumeToken: 'session-1:snapshot-required:9',
      };
      bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        subscriptionId: 'snapshot-1',
        kind: 'event',
        event: control,
        lastEventId: 'session-1:snapshot-required:9',
      });
      bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        subscriptionId: 'snapshot-1',
        kind: 'complete',
        lastEventId: 'session-1:snapshot-required:9',
      });

      expect(reconnects).toEqual([
        { attempt: 1, delayMs: 250, lastEventId: 'session-1:8' },
      ]);
      cancel();
      await Promise.resolve();
      transport.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it('fails closed before posting an unknown pathId or arbitrary URL field', async () => {
    const sent: NativeBridgeRequestEnvelope[] = [];
    const bridgeWindow = fakeBridgeWindow((envelope) => sent.push(envelope));
    const transport = new NativeControlTransport({ bridgeWindow });
    await expect(
      transport.request({ pathId: 'system.openUrl' } as never),
    ).rejects.toThrow(/not allowlisted/);
    await expect(
      transport.request({ pathId: 'system.health', host: 'evil.invalid' } as never),
    ).rejects.toThrow(/host/);
    expect(sent).toEqual([]);
    transport.dispose();
  });

  it('requires a session-bound managed image receipt without exposing a local path', async () => {
    const sent: NativeBridgeRequestEnvelope[] = [];
    const bridgeWindow = fakeBridgeWindow((envelope) => {
      sent.push(envelope);
      queueMicrotask(() => bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        id: envelope.id,
        ok: true,
        result: [{
          id: 'media_native_attachment_01',
          name: 'screen.png',
          mimeType: 'image/png',
          byteSize: 68,
          sessionId: 'agent:session-1',
          sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
        }],
      }));
    });
    const transport = new NativeControlTransport({ bridgeWindow, createId: () => 'media-call' });
    await expect(transport.pickFiles({
      accepts: ['image/png', 'image/jpeg', 'image/gif', 'image/webp'],
      multiple: true,
      purpose: 'attachment',
      sessionId: 'agent:session-1',
      maxFiles: 3,
    })).resolves.toEqual([expect.objectContaining({ id: 'media_native_attachment_01' })]);
    expect(sent).toEqual([{
      id: 'media-call',
      method: 'pickFiles',
      payload: {
        accepts: ['image/png', 'image/jpeg', 'image/gif', 'image/webp'],
        multiple: true,
        purpose: 'attachment',
        sessionId: 'agent:session-1',
        maxFiles: 3,
      },
    }]);
    await expect(transport.pickFiles({ purpose: 'attachment', maxFiles: 1 })).rejects.toThrow(/sessionId/);
    expect(sent).toHaveLength(1);
    transport.dispose();
  });

  it('keeps a managed attachment receipt bound to its Room owner', async () => {
    const sent: NativeBridgeRequestEnvelope[] = [];
    const bridgeWindow = fakeBridgeWindow((envelope) => {
      sent.push(envelope);
      queueMicrotask(() => bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        id: envelope.id,
        ok: true,
        result: [{
          id: 'media_native_room_image01',
          name: 'room.png',
          mimeType: 'image/png',
          byteSize: 68,
          roomId: 'room:collaboration-1',
          sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
        }],
      }));
    });
    const transport = new NativeControlTransport({ bridgeWindow, createId: () => 'room-media-call' });
    await expect(transport.pickFiles({
      purpose: 'attachment',
      roomId: 'room:collaboration-1',
      maxFiles: 1,
    })).resolves.toEqual([expect.objectContaining({
      id: 'media_native_room_image01',
      roomId: 'room:collaboration-1',
    })]);
    expect(sent[0]).toMatchObject({
      method: 'pickFiles',
      payload: { roomId: 'room:collaboration-1' },
    });
    transport.dispose();
  });

  it('rejects a native attachment receipt that leaks a selected local path', async () => {
    const bridgeWindow = fakeBridgeWindow((envelope) => {
      queueMicrotask(() => bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        id: envelope.id,
        ok: true,
        result: [{
          id: 'media_native_attachment_02',
          name: 'screen.png',
          mimeType: 'image/png',
          byteSize: 68,
          path: '/Users/private/screen.png',
          sessionId: 'agent:session-1',
          sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
        }],
      }));
    });
    const transport = new NativeControlTransport({ bridgeWindow });
    await expect(transport.pickFiles({
      purpose: 'attachment',
      sessionId: 'agent:session-1',
      maxFiles: 1,
    })).rejects.toThrow(/invalid managed attachment receipt/);
    transport.dispose();
  });

  it('accepts only an absolute native directory receipt for backup export', async () => {
    const bridgeWindow = fakeBridgeWindow((envelope) => {
      queueMicrotask(() => bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        id: envelope.id,
        ok: true,
        result: [{
          id: 'backup-directory-1',
          name: 'Backups',
          mimeType: 'application/octet-stream',
          byteSize: 0,
          path: '/Users/example/Backups',
        }],
      }));
    });
    const transport = new NativeControlTransport({ bridgeWindow });
    await expect(transport.pickFiles({
      purpose: 'export-destination',
      maxFiles: 1,
    })).resolves.toEqual([expect.objectContaining({ path: '/Users/example/Backups' })]);
    transport.dispose();
  });

  it('accepts bounded absolute directory receipts for Agent workspace roots', async () => {
    const sent: NativeBridgeRequestEnvelope[] = [];
    const bridgeWindow = fakeBridgeWindow((envelope) => {
      sent.push(envelope);
      queueMicrotask(() => bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        id: envelope.id,
        ok: true,
        result: [{
          id: 'workspace-directory-1',
          name: 'learnA',
          mimeType: 'application/octet-stream',
          byteSize: 0,
          path: '/Users/example/Projects/personal-agent-workbench',
        }],
      }));
    });
    const transport = new NativeControlTransport({ bridgeWindow, createId: () => 'workspace-call' });

    await expect(transport.pickFiles({
      purpose: 'workspace-root',
      multiple: true,
      maxFiles: 4,
    })).resolves.toEqual([
      expect.objectContaining({
        path: '/Users/example/Projects/personal-agent-workbench',
      }),
    ]);
    expect(sent).toEqual([{
      id: 'workspace-call',
      method: 'pickFiles',
      payload: { purpose: 'workspace-root', multiple: true, maxFiles: 4 },
    }]);
    transport.dispose();
  });

  it('selects plugin sources only as native directories', async () => {
    const sent: NativeBridgeRequestEnvelope[] = [];
    const bridgeWindow = fakeBridgeWindow((envelope) => {
      sent.push(envelope);
      queueMicrotask(() => bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        id: envelope.id,
        ok: true,
        result: [{
          id: 'plugin-directory-1',
          name: 'guided-plugin',
          mimeType: 'application/octet-stream',
          byteSize: 0,
          path: '/Users/example/Plugins/guided-plugin',
        }],
      }));
    });
    const transport = new NativeControlTransport({ bridgeWindow });

    await expect(transport.pickFiles({
      purpose: 'plugin-source',
      selection: 'directory',
      maxFiles: 1,
    })).resolves.toEqual([
      expect.objectContaining({
        path: '/Users/example/Plugins/guided-plugin',
      }),
    ]);
    expect(sent[0]?.payload).toEqual({
      purpose: 'plugin-source',
      selection: 'directory',
      maxFiles: 1,
    });
    await expect(transport.pickFiles({
      purpose: 'plugin-source',
      selection: 'file',
      maxFiles: 1,
    })).rejects.toThrow(/directory/);
    transport.dispose();
  });

  it('imports pasted clipboard images through the session-bound native media bridge', async () => {
    const sent: NativeBridgeRequestEnvelope[] = [];
    const bridgeWindow = fakeBridgeWindow((envelope) => {
      sent.push(envelope);
      queueMicrotask(() => bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        id: envelope.id,
        ok: true,
        result: [{
          id: 'media_native_pasted_01',
          name: 'pasted-image-1.gif',
          mimeType: 'image/gif',
          byteSize: 68,
          sessionId: 'agent:session-1',
          sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
        }],
      }));
    });
    const transport = new NativeControlTransport({ bridgeWindow, createId: () => 'paste-call' });
    const image = new File([new Uint8Array([71, 73, 70, 56, 57, 97])], 'clipboard.gif', { type: 'image/gif' });

    await expect(transport.pasteImages({
      sessionId: 'agent:session-1',
      files: [image],
    })).resolves.toEqual([expect.objectContaining({ id: 'media_native_pasted_01' })]);
    expect(sent).toEqual([{
      id: 'paste-call',
      method: 'pasteImages',
      payload: { sessionId: 'agent:session-1', maxFiles: 1 },
    }]);
    transport.dispose();
  });

  it('rejects a pasted image receipt owned by a different Session', async () => {
    const bridgeWindow = fakeBridgeWindow((envelope) => {
      queueMicrotask(() => bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        id: envelope.id,
        ok: true,
        result: [{
          id: 'media_native_pasted_02',
          name: 'pasted-image-1.png',
          mimeType: 'image/png',
          byteSize: 68,
          sessionId: 'agent:session-2',
          sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
        }],
      }));
    });
    const transport = new NativeControlTransport({ bridgeWindow });

    await expect(transport.pasteImages({
      sessionId: 'agent:session-1',
      maxFiles: 1,
    })).rejects.toThrow(/invalid managed attachment receipt/);
    transport.dispose();
  });

  it('imports knowledge documents through a kb-bound native picker without accepting paths', async () => {
    const sent: NativeBridgeRequestEnvelope[] = [];
    const bridgeWindow = fakeBridgeWindow((envelope) => {
      sent.push(envelope);
      queueMicrotask(() => bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        id: envelope.id,
        ok: true,
        result: [{
          kbId: 'kb_docs',
          documentId: 'doc_manual_01',
          fileName: 'manual.pdf',
          mimeType: 'application/pdf',
          byteSize: 4096,
          sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
          status: 'queued',
        }],
      }));
    });
    const transport = new NativeControlTransport({ bridgeWindow, createId: () => 'knowledge-call' });

    await expect(transport.importKnowledgeDocuments({
      kbId: 'kb_docs',
      accepts: ['application/pdf'],
      maxFiles: 3,
      parserProvider: 'auto',
    })).resolves.toEqual([expect.objectContaining({ documentId: 'doc_manual_01' })]);
    expect(sent).toEqual([{
      id: 'knowledge-call',
      method: 'pickFiles',
      payload: {
        purpose: 'knowledge-import',
        kbId: 'kb_docs',
        accepts: ['application/pdf'],
        multiple: true,
        maxFiles: 3,
        parserProvider: 'auto',
      },
    }]);

    const browserFile = new File(['private'], 'manual.pdf', { type: 'application/pdf' });
    await expect(transport.importKnowledgeDocuments({
      kbId: 'kb_docs',
      files: [browserFile],
    })).rejects.toThrow(/does not accept browser paths or File objects/);
    expect(sent).toHaveLength(1);
    transport.dispose();
  });

  it('keeps an interactive picker open past the normal timeout and cancels it explicitly', async () => {
    vi.useFakeTimers();
    try {
      const sent: NativeBridgeRequestEnvelope[] = [];
      const controller = new AbortController();
      const bridgeWindow = fakeBridgeWindow((envelope) => sent.push(envelope));
      const transport = new NativeControlTransport({
        bridgeWindow,
        createId: () => 'knowledge-interactive',
        requestTimeoutMs: 100,
      });
      const pending = transport.importKnowledgeDocuments({
        kbId: 'kb_docs',
        maxFiles: 1,
        signal: controller.signal,
      });
      const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' });

      await vi.advanceTimersByTimeAsync(60_000);
      expect(sent).toHaveLength(1);
      expect(sent[0]).toMatchObject({ method: 'pickFiles' });

      controller.abort();
      await rejected;
      expect(sent[1]).toEqual({
        id: 'cancel:1',
        method: 'cancelRequest',
        payload: { requestId: 'knowledge-interactive' },
      });
      transport.dispose();
    } finally {
      vi.useRealTimers();
    }
  });

  it('reads a knowledge asset through the id-only native binary bridge', async () => {
    const sent: NativeBridgeRequestEnvelope[] = [];
    const assetId = 'a'.repeat(64);
    const blob = new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' });
    const bridgeWindow = fakeBridgeWindow((envelope) => {
      sent.push(envelope);
      queueMicrotask(() => bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        id: envelope.id,
        ok: true,
        result: {
          kbId: 'kb_docs',
          fileId: 'file_manual',
          assetId,
          mimeType: 'image/png',
          byteSize: blob.size,
          sha256: assetId,
          blob,
        },
      }));
    });
    const transport = new NativeControlTransport({ bridgeWindow, createId: () => 'asset-call' });

    await expect(transport.readKnowledgeAsset({
      kbId: 'kb_docs',
      fileId: 'file_manual',
      assetId,
    })).resolves.toMatchObject({ assetId, mimeType: 'image/png', byteSize: blob.size });
    expect(sent).toEqual([{
      id: 'asset-call',
      method: 'readKnowledgeAsset',
      payload: { kbId: 'kb_docs', fileId: 'file_manual', assetId },
    }]);
    await expect(transport.readKnowledgeAsset({
      kbId: 'kb_docs',
      fileId: 'file_manual',
      assetId: '../private',
    })).rejects.toThrow(/sha256 assetId/);
    expect(sent).toHaveLength(1);
    transport.dispose();
  });

  it('reads the original knowledge source through the two-id native binary bridge', async () => {
    const sent: NativeBridgeRequestEnvelope[] = [];
    const sha256 = 'b'.repeat(64);
    const blob = new Blob(['%PDF'], { type: 'application/pdf' });
    const bridgeWindow = fakeBridgeWindow((envelope) => {
      sent.push(envelope);
      queueMicrotask(() => bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        id: envelope.id,
        ok: true,
        result: {
          kbId: 'kb_docs',
          fileId: 'file_manual',
          mimeType: 'application/pdf',
          byteSize: blob.size,
          sha256,
          blob,
        },
      }));
    });
    const transport = new NativeControlTransport({ bridgeWindow, createId: () => 'source-call' });

    await expect(transport.readKnowledgeDocumentSource({
      kbId: 'kb_docs',
      fileId: 'file_manual',
    })).resolves.toMatchObject({ mimeType: 'application/pdf', byteSize: blob.size, sha256 });
    expect(sent).toEqual([{
      id: 'source-call',
      method: 'readKnowledgeDocumentSource',
      payload: { kbId: 'kb_docs', fileId: 'file_manual' },
    }]);
    transport.dispose();
  });

  it('asks native pasteImages to inspect the pasteboard when WebKit exposes no File', async () => {
    const sent: NativeBridgeRequestEnvelope[] = [];
    const bridgeWindow = fakeBridgeWindow((envelope) => {
      sent.push(envelope);
      queueMicrotask(() => bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        id: envelope.id,
        ok: true,
        result: [],
      }));
    });
    const transport = new NativeControlTransport({ bridgeWindow, createId: () => 'native-paste-call' });

    await expect(transport.pasteImages({
      roomId: 'room:collaboration-1',
      maxFiles: 4,
    })).resolves.toEqual([]);
    expect(sent).toEqual([{
      id: 'native-paste-call',
      method: 'pasteImages',
      payload: { roomId: 'room:collaboration-1', maxFiles: 4 },
    }]);
    transport.dispose();
  });

  it('uses dedicated bounded voice methods and never receives credential values back', async () => {
    const sent: NativeBridgeRequestEnvelope[] = [];
    let nextId = 1;
    const bridgeWindow = fakeBridgeWindow((envelope) => {
      sent.push(envelope);
      const result = envelope.method === 'voiceAction'
        ? {
            action: 'request_microphone_permission',
            accepted: true,
            status: {
              running: true,
              state: 'idle',
              statusText: '语音代理已待命',
              microphoneAuthorization: 'authorized',
              accessibilityTrusted: true,
              hotkeyInstalled: true,
              hotkeyMode: 'event_tap',
              updatedAtMs: 42,
            },
          }
        : { provider: 'native_streaming', configured: true };
      queueMicrotask(() => bridgeWindow.__RAG_IME_NATIVE_BRIDGE__?.receive({
        id: envelope.id,
        ok: true,
        result,
      }));
    });
    const transport = new NativeControlTransport({
      bridgeWindow,
      createId: () => `voice-${nextId++}`,
    });

    await expect(transport.voiceCredentialStatus('native_streaming')).resolves.toEqual({
      provider: 'native_streaming',
      configured: true,
    });
    await expect(transport.saveVoiceCredentials({
      provider: 'native_streaming',
      accessToken: 'secret-token',
      appId: 'app-id',
      resourceId: 'resource-id',
      endpoint: '',
      model: '',
      headersJson: '',
    })).resolves.toEqual({ provider: 'native_streaming', configured: true });
    await expect(transport.runVoiceAction('request_microphone_permission')).resolves.toMatchObject({
      accepted: true,
      status: { microphoneAuthorization: 'authorized' },
    });

    expect(sent.map((item) => item.method)).toEqual([
      'voiceCredentialStatus',
      'voiceCredentialSave',
      'voiceAction',
    ]);
    expect(sent[0].payload).toEqual({ provider: 'native_streaming' });
    expect(sent[2].payload).toEqual({ action: 'request_microphone_permission' });
    expect(JSON.stringify((await transport.voiceCredentialStatus('native_streaming')))).not.toContain('secret-token');
    await expect(transport.saveVoiceCredentials({
      provider: 'http_transcription',
      accessToken: 'secret',
      appId: '',
      resourceId: '',
      endpoint: 'https://example.test/v1/audio/transcriptions',
      model: 'model',
      headersJson: '{"Authorization":42}',
    })).rejects.toThrow(/string dictionary/);
    transport.dispose();
  });
});

function fakeBridgeWindow(
  postMessage: (envelope: NativeBridgeRequestEnvelope) => void,
): Window {
  return {
    webkit: { messageHandlers: { ragImeNativeBridge: { postMessage } } },
  } as unknown as Window;
}

function capabilitiesFixture() {
  return {
    schemaVersion: 'rag-ime.control-capabilities.v1',
    features: { subscriptions: true },
    native: {
      filePicker: true,
      managedAgentImageImport: true,
      knowledgeDocumentImport: true,
      knowledgeParserStatus: true,
      knowledgeAssetRead: true,
      knowledgeDocumentSourceRead: true,
      revealPath: true,
      keychainStatus: true,
      tccStatus: true,
      approvedExternalActions: true,
    },
    routes: [{ pathId: 'agent.session.events' }, { pathId: 'system.health' }],
  };
}
