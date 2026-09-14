import { describe, expect, it, vi } from 'vitest';

import { agentEventFixture } from '@/test/fixtures/events';

import {
  ControlTransportHttpError,
  HttpControlTransport,
} from './http-transport';

describe('HttpControlTransport', () => {
  it('exposes voice controls only through a same-origin installed preload', async () => {
    const voice = {
      status: vi.fn(), credentialStatus: vi.fn(), saveCredentials: vi.fn(), action: vi.fn(),
    };
    window.pawVoiceHost = voice;
    const fetch = vi.fn(async () => new Response(JSON.stringify({ features: {} }), { status: 200 })) as typeof globalThis.fetch;
    try {
      const desktop = new HttpControlTransport({ baseUrl: window.location.origin, fetch });
      expect((await desktop.capabilities()).native).toMatchObject({ keychain: true, tcc: true });
      await desktop.runVoiceAction!('reload_configuration');
      expect(voice.action).toHaveBeenCalledWith('reload_configuration');
      const scoped = new HttpControlTransport({ baseUrl: window.location.origin, routePrefix: '/team/spaces/project-7', fetch });
      expect(scoped.runVoiceAction).toBeUndefined();
      expect((await scoped.capabilities()).native).toMatchObject({ keychain: false, tcc: false });
      const remote = new HttpControlTransport({ baseUrl: 'https://gateway.example.test', fetch });
      expect(remote.runVoiceAction).toBeUndefined();
      expect((await remote.capabilities()).native.tcc).toBe(false);
      delete window.pawVoiceHost;
      const browser = new HttpControlTransport({ baseUrl: window.location.origin, fetch });
      expect(browser.runVoiceAction).toBeUndefined();
      expect((await browser.capabilities()).native.keychain).toBe(false);
    } finally { delete window.pawVoiceHost; }
  });

  it('resolves browser snapshot images against the configured HTTP origin', () => {
    const transport = new HttpControlTransport({
      baseUrl: 'https://gateway.example.test/control/',
      fetch: vi.fn() as typeof fetch,
    });

    expect(transport.browserSnapshotImageUrl('snap_remote-1')).toBe(
      'https://gateway.example.test/api/browser/snapshots/snap_remote-1/image',
    );
    expect(transport.agentMediaContentUrl(
      '/api/agent/media/media_remote_fixture_01/content?roomId=room:remote-1',
    )).toBe(
      'https://gateway.example.test/api/agent/media/media_remote_fixture_01/content?roomId=room%3Aremote-1',
    );
    expect(() => transport.agentMediaContentUrl(
      'https://evil.example.test/api/agent/media/media_remote_fixture_01/content?roomId=room:remote-1',
    )).toThrow(/managed receipt path/);
    expect(() => transport.browserSnapshotImageUrl('../private')).toThrow(/bounded snapshotId/);
  });

  it('keeps TeamGateway requests, media URLs, cookies and CSRF scoped', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ sessions: [] }), {
      headers: { 'Content-Type': 'application/json' },
      status: 200,
    }));
    const transport = new HttpControlTransport({
      baseUrl: window.location.origin,
      routePrefix: '/team/spaces/project-7/',
      credentials: 'same-origin',
      getHeaders: () => ({ 'X-CSRF-Token': 'csrf-memory-only' }),
      connectionIdentity: 'team:user-1:project-7',
      fetch: fetchMock as typeof globalThis.fetch,
    });

    await transport.request({ pathId: 'agent.sessions.list' });

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      `${window.location.origin}/team/spaces/project-7/api/agent/sessions`,
    );
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ credentials: 'same-origin' });
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-memory-only');
    expect(transport.browserSnapshotImageUrl('snap_team-1')).toBe(
      `${window.location.origin}/team/spaces/project-7/api/browser/snapshots/snap_team-1/image`,
    );
    expect(transport.agentMediaContentUrl(
      '/api/agent/media/media_team_fixture_01/content?sessionId=session:team-1',
    )).toBe(
      `${window.location.origin}/team/spaces/project-7/api/agent/media/media_team_fixture_01/content?sessionId=session%3Ateam-1`,
    );
    expect(transport.connectionIdentity).toBe('team:user-1:project-7');
  });

  it('rejects unsafe TeamGateway route prefixes', () => {
    expect(() => new HttpControlTransport({
      baseUrl: window.location.origin,
      routePrefix: '/team/../api',
      fetch: vi.fn() as typeof globalThis.fetch,
    })).toThrow(/routePrefix/);
  });

  it('opens scoped SSE streams with the same cookie and CSRF policy', async () => {
    const payload = agentEventFixture(1, 'turn_completed', {});
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(
      `id: session-team:1\ndata: ${JSON.stringify(payload)}\n\n`,
      { headers: { 'Content-Type': 'text/event-stream' }, status: 200 },
    ));
    const transport = new HttpControlTransport({
      baseUrl: window.location.origin,
      routePrefix: '/team/spaces/project-7',
      getHeaders: () => ({ 'X-CSRF-Token': 'csrf-memory-only' }),
      fetch: fetchMock as typeof globalThis.fetch,
    });
    const done = new Promise<void>((resolve) => {
      let cancel = () => {};
      cancel = transport.subscribe(
        {
          pathId: 'agent.session.events',
          params: { sessionId: 'session-team' },
          lastEventId: 'session-team:0',
        },
        {
          next() {
            cancel();
            resolve();
          },
        },
      );
    });
    await done;

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      `${window.location.origin}/team/spaces/project-7/api/agent/sessions/session-team/events?lastEventId=session-team%3A0`,
    );
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ credentials: 'same-origin' });
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get('X-CSRF-Token')).toBe('csrf-memory-only');
  });

  it('uploads browser clipboard Files to the fixed owner-scoped managed media route', async () => {
    const calls: { url: URL; init?: RequestInit }[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const file = init?.body as File;
      const ownerKey = url.searchParams.has('roomId') ? 'roomId' : 'sessionId';
      const ownerId = url.searchParams.get(ownerKey) ?? '';
      calls.push({ url, init });
      return new Response(JSON.stringify({
        schemaVersion: 'rag-ime.agent-media-import.v1',
        ok: true,
        media: {
          schemaVersion: 'rag-ime.agent-media.v1',
          mediaId: 'media_http_fixture_01',
          [ownerKey]: ownerId,
          ownerType: ownerKey === 'roomId' ? 'room' : 'session',
          ownerId,
          fileName: file.name,
          mimeType: file.type,
          byteSize: file.size,
          sha256: 'a'.repeat(64),
          origin: 'user_attachment',
        },
      }), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      });
    }) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
    });
    const image = new File(['png'], 'clipboard.png', { type: 'image/png' });

    await expect(transport.pasteImages({
      sessionId: 'session:http-1',
      files: [image],
    })).resolves.toEqual([
      expect.objectContaining({
        id: 'media_http_fixture_01',
        sessionId: 'session:http-1',
        sha256: 'a'.repeat(64),
      }),
    ]);
    await expect(transport.pasteImages({
      roomId: 'room:http-1',
      files: [image],
    })).resolves.toEqual([
      expect.objectContaining({
        id: 'media_http_fixture_01',
        roomId: 'room:http-1',
      }),
    ]);

    expect(calls.map((call) => call.url.toString())).toEqual([
      'http://127.0.0.1:8766/api/agent/media/import?sessionId=session%3Ahttp-1&fileName=clipboard.png',
      'http://127.0.0.1:8766/api/agent/media/import?roomId=room%3Ahttp-1&fileName=clipboard.png',
    ]);
    expect(calls.every((call) => call.init?.body === image)).toBe(true);
    expect(calls.every((call) => new Headers(call.init?.headers).get('Cache-Control') === 'no-store')).toBe(true);
  });

  it('does not pretend HTTP can recover clipboard images when WebKit exposes no File', async () => {
    const fetchMock = vi.fn() as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
    });

    await expect(transport.pasteImages({
      sessionId: 'session:http-1',
      maxFiles: 1,
    })).rejects.toThrow(/requires clipboard File objects/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('uses a fixed route mapping rather than caller-provided URLs', async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return new Response(JSON.stringify({ ok: true }), {
        headers: { 'Content-Type': 'application/json' },
      });
    }) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
    });

    await expect(transport.request({ pathId: 'system.health' })).resolves.toEqual({ ok: true });
    expect(calls[0]).toMatchObject({
      url: 'http://127.0.0.1:8766/api/health',
      init: { method: 'GET' },
    });
  });

  it('encodes trace IDs in the observation detail request path', async () => {
    const calls: string[] = [];
    const response = {
      schemaVersion: 'rag-ime.observability-trace-get.v1',
      traceId: 'trace:active-rag:alpha',
      trace: {
        schemaVersion: 'rag-ime.trace-envelope.v1',
        traceId: 'trace:active-rag:alpha',
        sourceKind: 'retrieval',
        status: 'completed',
        binding: {},
        input: {
          fingerprint: `sha256:${'0'.repeat(64)}`,
          contentPolicy: 'hash_only',
          normalization: 'observation-redacted',
        },
        spans: [],
        evidence: [],
        artifacts: [],
        createdAtMs: 1,
        updatedAtMs: 1,
      },
      truncated: false,
      projectionSource: 'observation_journal',
      observationWindow: {
        firstSequence: 1,
        lastSequence: 1,
        resumeToken: 'observation:1',
        nextBeforeSequence: null,
      },
    };
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return new Response(JSON.stringify(response), {
        headers: { 'Content-Type': 'application/json' },
      });
    }) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
    });

    await expect(transport.request({
      pathId: 'observability.trace.get',
      params: { traceId: 'trace:active-rag:alpha' },
    })).resolves.toEqual(response);

    expect(calls).toEqual([
      'http://127.0.0.1:8766/api/observability/traces/trace%3Aactive-rag%3Aalpha',
    ]);
  });

  it('marks bodyless delete requests as JSON management writes', async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return new Response(JSON.stringify({ ok: true }), {
        headers: { 'Content-Type': 'application/json' },
      });
    }) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8768',
      fetch: fetchMock,
    });

    await transport.request({
      pathId: 'agent.session.delete',
      params: { sessionId: 'agent:test-delete' },
    });

    expect(calls[0]?.url).toBe('http://127.0.0.1:8768/api/agent/sessions/agent%3Atest-delete');
    expect(calls[0]?.init?.method).toBe('DELETE');
    expect(calls[0]?.init?.body).toBeUndefined();
    expect(new Headers(calls[0]?.init?.headers).get('Content-Type')).toBe('application/json');
  });

  it('validates requested response contracts at the HTTP boundary', async () => {
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: vi.fn(async () => new Response(JSON.stringify({ id: 'missing-fields' }))) as typeof fetch,
    });
    await expect(
      transport.request({
        pathId: 'system.health',
        responseContract: 'agent-session.v1',
      }),
    ).rejects.toThrow(/Invalid agent-session.v1/);
  });

  it('preserves typed command receipt payloads on HTTP errors', async () => {
    const payload = {
      ok: false,
      code: 'AGENT_COMMAND_PENDING',
      error: 'still pending',
      commandReceipt: {
        state: 'pending',
        clientMessageId: 'client-1',
      },
    };
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: vi.fn(async () => new Response(
        JSON.stringify(payload),
        {
          status: 409,
          headers: { 'Content-Type': 'application/json' },
        },
      )) as typeof fetch,
    });

    await expect(transport.request({
      pathId: 'agent.session.prompt',
      params: { sessionId: 'session-1' },
      body: {
        message: 'hello',
        clientMessageId: 'client-1',
      },
    })).rejects.toMatchObject({
      status: 409,
      payload,
    } satisfies Partial<ControlTransportHttpError>);
  });

  it('falls back to the fixed 8766 route catalog when the facade is not mounted yet', async () => {
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: vi.fn(async () => new Response('not found', { status: 404 })) as typeof fetch,
    });

    await expect(transport.capabilities()).resolves.toMatchObject({
      transport: 'http',
      features: { legacyEndpointAdapter: true },
    });
  });

  it('streams browser-selected knowledge files only to the fixed kb import route', async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    const file = new File(['manual'], 'manual.pdf', { type: 'application/pdf' });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return new Response(JSON.stringify({
        schemaVersion: 'rag-ime.knowledge-document-import.v1',
        ok: true,
        receipt: {
          kbId: 'kb_docs',
          documentId: 'doc_manual_01',
          fileName: 'manual.pdf',
          mimeType: 'application/pdf',
          byteSize: file.size,
          sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
          status: 'queued',
        },
      }), { status: 201, headers: { 'Content-Type': 'application/json' } });
    }) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
    });

    await expect(transport.importKnowledgeDocuments({
      kbId: 'kb_docs',
      files: [file],
      parserProvider: 'auto',
    })).resolves.toEqual([expect.objectContaining({ documentId: 'doc_manual_01' })]);
    expect(calls[0]?.url).toBe(
      'http://127.0.0.1:8766/api/knowledge-bases/kb_docs/documents/import?fileName=manual.pdf&mimeType=application%2Fpdf&parserProvider=auto',
    );
    expect(calls[0]?.init?.body).toBe(file);
    expect(new Headers(calls[0]?.init?.headers).get('Content-Type')).toBe('application/pdf');
  });

  it('reads a bounded knowledge image only from the fixed three-id asset route', async () => {
    const bytes = new Uint8Array([137, 80, 78, 71]);
    const assetId = await sha256(bytes);
    const calls: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return new Response(bytes, {
        headers: {
          'Content-Type': 'image/png',
          'Content-Length': String(bytes.byteLength),
          ETag: `"${assetId}"`,
          'X-Content-Type-Options': 'nosniff',
          'Content-Disposition': 'inline; filename="asset.png"',
        },
      });
    }) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
    });

    await expect(transport.readKnowledgeAsset({
      kbId: 'kb_docs',
      fileId: 'file_manual',
      assetId,
    })).resolves.toMatchObject({
      kbId: 'kb_docs',
      fileId: 'file_manual',
      assetId,
      mimeType: 'image/png',
      byteSize: bytes.byteLength,
    });
    expect(calls).toEqual([
      `http://127.0.0.1:8766/api/knowledge-bases/kb_docs/documents/file_manual/assets/${assetId}`,
    ]);
    await expect(transport.readKnowledgeAsset({
      kbId: 'kb_docs',
      fileId: 'file_manual',
      assetId: '../private',
    })).rejects.toThrow(/sha256 assetId/);
    expect(calls).toHaveLength(1);
  });

  it('reads an original document only from the fixed two-id source route', async () => {
    const bytes = new TextEncoder().encode('%PDF-source');
    const sha = await sha256(bytes);
    const calls: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return new Response(bytes, {
        headers: {
          'Content-Type': 'application/pdf',
          'Content-Length': String(bytes.byteLength),
          ETag: `"${sha}"`,
          'X-Content-Type-Options': 'nosniff',
          'Content-Disposition': 'inline; filename="manual.pdf"',
        },
      });
    }) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
    });

    await expect(transport.readKnowledgeDocumentSource({
      kbId: 'kb_docs',
      fileId: 'file_manual',
    })).resolves.toMatchObject({
      kbId: 'kb_docs',
      fileId: 'file_manual',
      mimeType: 'application/pdf',
      byteSize: bytes.byteLength,
      sha256: sha,
    });
    expect(calls).toEqual([
      'http://127.0.0.1:8766/api/knowledge-bases/kb_docs/documents/file_manual/source',
    ]);
    await expect(transport.readKnowledgeDocumentSource({
      kbId: 'kb_docs',
      fileId: 'file_manual',
      path: '/Users/private/manual.pdf',
    } as never)).rejects.toThrow(/unsupported field/);
    expect(calls).toHaveLength(1);
  });

  it('reconnects SSE with the latest Last-Event-ID and validates envelopes', async () => {
    const headers: string[] = [];
    const payloads = [agentEventFixture(1, 'text_delta', { delta: 'A' }), agentEventFixture(2, 'turn_completed', {})];
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      headers.push(new Headers(init?.headers).get('Last-Event-ID') ?? '<missing>');
      const event = payloads.shift();
      return new Response(event ? sse(event) : ': heartbeat\n\n', {
        headers: { 'Content-Type': 'text/event-stream' },
      });
    });
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock as typeof fetch,
      reconnectBaseDelayMs: 0,
      reconnectMaxDelayMs: 0,
      random: () => 0,
    });
    const received: string[] = [];
    let cancel = () => {};
    const done = new Promise<void>((resolve) => {
      cancel = transport.subscribe(
        {
          pathId: 'agent.session.events',
          params: { sessionId: 'session-1' },
          lastEventId: 'session-1:0',
        },
        {
          next(event) {
            const typed = event as ReturnType<typeof agentEventFixture>;
            received.push(typed.eventId);
            if (received.length === 2) {
              cancel();
              resolve();
            }
          },
        },
      );
    });
    await done;

    expect(received).toEqual(['session-1:1', 'session-1:2']);
    expect(headers.slice(0, 2)).toEqual(['session-1:0', 'session-1:1']);
    expect(fetchMock.mock.calls[1]?.[0].toString()).toContain('lastEventId=session-1%3A1');
  });

  it('backs off early 200 EOF responses until a complete SSE frame proves stability', async () => {
    const responses = ['', '', ': heartbeat\n\n'];
    const reconnects: { attempt: number; delayMs: number }[] = [];
    const fetchMock = vi.fn(async () => new Response(responses.shift() ?? '', {
      headers: { 'Content-Type': 'text/event-stream; charset=utf-8' },
    })) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
      reconnectBaseDelayMs: 0,
      reconnectMaxDelayMs: 0,
      random: () => 0.5,
    });
    let cancel = () => {};
    const observed = new Promise<void>((resolve) => {
      cancel = transport.subscribe(
        {
          pathId: 'agent.session.events',
          params: { sessionId: 'session-1' },
          lastEventId: 'session-1:0',
        },
        {
          next: vi.fn(),
          reconnect(notice) {
            reconnects.push(notice);
            if (reconnects.length === 3) {
              cancel();
              resolve();
            }
          },
        },
      );
    });

    await observed;
    expect(reconnects.map(({ attempt }) => attempt)).toEqual([1, 2, 1]);
  });

  it('does not report an HTTP SSE connection stable until a complete heartbeat arrives', async () => {
    let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
    const fetchMock = vi.fn(async () => new Response(
      new ReadableStream<Uint8Array>({
        start(controller) {
          streamController = controller;
        },
      }),
      { headers: { 'Content-Type': 'text/event-stream' } },
    )) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
    });
    const opened = vi.fn();
    const stable = vi.fn();
    const cancel = transport.subscribe(
      {
        pathId: 'agent.session.events',
        params: { sessionId: 'session-1' },
        lastEventId: 'session-1:0',
      },
      {
        next: vi.fn(),
        open: opened,
        stable,
      },
    );

    await vi.waitFor(() => expect(opened).toHaveBeenCalledTimes(1));
    expect(stable).not.toHaveBeenCalled();
    streamController?.enqueue(new TextEncoder().encode(': heartbeat\n'));
    await Promise.resolve();
    expect(stable).not.toHaveBeenCalled();
    streamController?.enqueue(new TextEncoder().encode('\n'));
    await vi.waitFor(() => expect(stable).toHaveBeenCalledTimes(1));
    expect(stable).toHaveBeenLastCalledWith('session-1:0');
    cancel();
  });

  it('parses and caps a 503 Retry-After hint for the owning recovery loop', async () => {
    const fetchMock = vi.fn(async () => new Response(
      JSON.stringify({ error: 'event stream capacity exhausted' }),
      {
        status: 503,
        headers: {
          'Content-Type': 'application/json',
          'Retry-After': '999999',
        },
      },
    )) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
      reconnectBaseDelayMs: 0,
      reconnectMaxDelayMs: 0,
    });
    let failure: Error | undefined;
    let cancel = () => {};
    const observed = new Promise<void>((resolve) => {
      cancel = transport.subscribe(
        {
          pathId: 'agent.session.events',
          params: { sessionId: 'session-1' },
          lastEventId: 'session-1:0',
        },
        {
          next: vi.fn(),
          error(error) {
            failure = error;
          },
          reconnect() {
            cancel();
            resolve();
          },
        },
      );
    });

    await observed;
    expect(failure).toMatchObject({
      status: 503,
      retryAfterMs: 60_000,
    });
  });

  it('rejects a 200 response that is not an SSE stream', async () => {
    const fetchMock = vi.fn(async () => new Response('<html>not an event stream</html>', {
      headers: { 'Content-Type': 'text/html; charset=utf-8' },
    })) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
      reconnectBaseDelayMs: 0,
      reconnectMaxDelayMs: 0,
    });
    let failure: Error | undefined;
    let cancel = () => {};
    const stopped = new Promise<void>((resolve) => {
      cancel = transport.subscribe(
        {
          pathId: 'agent.session.events',
          params: { sessionId: 'session-1' },
          lastEventId: 'session-1:0',
        },
        {
          next: vi.fn(),
          error(error) {
            failure = error;
          },
          reconnect() {
            cancel();
            resolve();
          },
        },
      );
    });

    await stopped;
    expect(failure?.message).toMatch(/text\/event-stream/);
  });

  it('reports validation-runtime loading failures without opening the network stream', async () => {
    const fetchMock = vi.fn(async () => {
      throw new Error('fetch must not run');
    }) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
      validationRuntimeLoader: async () => {
        throw new Error('validator chunk unavailable');
      },
    });
    let cancel = () => {};
    const failure = new Promise<Error>((resolve) => {
      cancel = transport.subscribe(
        {
          pathId: 'agent.session.events',
          params: { sessionId: 'session-1' },
          lastEventId: 'session-1:0',
        },
        {
          next: vi.fn(),
          error(error) {
            cancel();
            resolve(error);
          },
        },
      );
    });

    await expect(failure).resolves.toMatchObject({ message: 'validator chunk unavailable' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('does not replace the durable reconnect cursor with a transient snapshot-required id', async () => {
    const headers: string[] = [];
    let resolveSecondRequest = () => {};
    const secondRequest = new Promise<void>((resolve) => {
      resolveSecondRequest = resolve;
    });
    const control = {
      ...agentEventFixture(42, 'snapshot_required', {
        reason: 'event_replay_gap',
        afterEventId: 'session-1:1',
      }),
      eventId: 'session-1:snapshot-required:41',
      resumeToken: 'session-1:snapshot-required:41',
    };
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      headers.push(new Headers(init?.headers).get('Last-Event-ID') ?? '<missing>');
      if (headers.length === 1) {
        return new Response(sse(control), {
          headers: { 'Content-Type': 'text/event-stream' },
        });
      }
      resolveSecondRequest();
      return new Response(': heartbeat\n\n', {
        headers: { 'Content-Type': 'text/event-stream' },
      });
    });
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock as typeof fetch,
      reconnectBaseDelayMs: 0,
      reconnectMaxDelayMs: 0,
      random: () => 0,
    });
    const received: string[] = [];
    const cancel = transport.subscribe(
      {
        pathId: 'agent.session.events',
        params: { sessionId: 'session-1' },
        lastEventId: 'session-1:1',
      },
      {
        next(event) {
          received.push((event as ReturnType<typeof agentEventFixture>).eventId);
        },
      },
    );

    await secondRequest;
    cancel();

    expect(received).toEqual(['session-1:snapshot-required:41']);
    expect(headers.slice(0, 2)).toEqual(['session-1:1', 'session-1:1']);
  });

  it('replays an event when delivery fails before advancing Last-Event-ID', async () => {
    const terminal = agentEventFixture(1, 'turn_completed', { status: 'completed' });
    const headers: string[] = [];
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      headers.push(new Headers(init?.headers).get('Last-Event-ID') ?? '<missing>');
      return new Response(sse(terminal), {
        headers: { 'Content-Type': 'text/event-stream' },
      });
    });
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock as typeof fetch,
      reconnectBaseDelayMs: 0,
      reconnectMaxDelayMs: 0,
      random: () => 0,
    });
    let deliveries = 0;
    let cancel = () => {};
    const delivered = new Promise<void>((resolve) => {
      cancel = transport.subscribe(
        {
          pathId: 'agent.session.events',
          params: { sessionId: 'session-1' },
          lastEventId: 'session-1:0',
        },
        {
          next() {
            deliveries += 1;
            if (deliveries === 1) throw new Error('projection commit failed');
            cancel();
            resolve();
          },
        },
      );
    });

    await delivered;

    expect(deliveries).toBe(2);
    expect(headers.slice(0, 2)).toEqual(['session-1:0', 'session-1:0']);
  });

  it('terminates the failed SSE response before it can deliver a later event', async () => {
    const first = agentEventFixture(1, 'text_delta', { delta: 'first' });
    const later = agentEventFixture(2, 'turn_completed', { status: 'completed' });
    const headers: string[] = [];
    const cancelledBodies: unknown[] = [];
    let requestCount = 0;
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      headers.push(new Headers(init?.headers).get('Last-Event-ID') ?? '<missing>');
      requestCount += 1;
      if (requestCount === 1) {
        const bytes = new TextEncoder().encode(`${sse(first)}${sse(later)}`);
        return new Response(new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(bytes);
          },
          cancel(reason) {
            cancelledBodies.push(reason);
          },
        }), {
          headers: { 'Content-Type': 'text/event-stream' },
        });
      }
      return new Response(sse(first), {
        headers: { 'Content-Type': 'text/event-stream' },
      });
    });
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock as typeof fetch,
      reconnectBaseDelayMs: 0,
      reconnectMaxDelayMs: 0,
      random: () => 0,
    });
    const attempted: string[] = [];
    let cancel = () => {};
    const replayed = new Promise<void>((resolve) => {
      cancel = transport.subscribe(
        {
          pathId: 'agent.session.events',
          params: { sessionId: 'session-1' },
          lastEventId: 'session-1:0',
        },
        {
          next(event) {
            attempted.push((event as ReturnType<typeof agentEventFixture>).eventId);
            if (attempted.length === 1) throw new Error('projection commit failed');
            cancel();
            resolve();
          },
        },
      );
    });

    await replayed;

    expect(attempted).toEqual(['session-1:1', 'session-1:1']);
    expect(headers.slice(0, 2)).toEqual(['session-1:0', 'session-1:0']);
    expect(cancelledBodies).toHaveLength(1);
  });

  it('cancels the active SSE response body when a subscription is released', async () => {
    let markOpened = () => {};
    const streamOpened = new Promise<void>((resolve) => {
      markOpened = resolve;
    });
    const cancelledBodies: unknown[] = [];
    const fetchMock = vi.fn(async (): Promise<Response> => new Response(
      new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode(': connected\n\n'));
        },
        cancel(reason) {
          cancelledBodies.push(reason);
        },
      }),
      { headers: { 'Content-Type': 'text/event-stream' } },
    )) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
    });

    const cancel = transport.subscribe(
      {
        pathId: 'agent.session.events',
        params: { sessionId: 'session-1' },
        lastEventId: 'session-1:0',
      },
      { open: markOpened, next: vi.fn() },
    );
    await streamOpened;
    cancel();
    await vi.waitFor(() => expect(cancelledBodies).toHaveLength(1));
  });

  it('cancels an SSE body when the observer releases it during open', async () => {
    const cancelledBodies: unknown[] = [];
    const fetchMock = vi.fn(async (): Promise<Response> => new Response(
      new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode(': connected\n\n'));
        },
        cancel(reason) {
          cancelledBodies.push(reason);
        },
      }),
      { headers: { 'Content-Type': 'text/event-stream' } },
    )) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
    });
    let cancel = () => {};
    const opened = new Promise<void>((resolve) => {
      cancel = transport.subscribe(
        {
          pathId: 'agent.session.events',
          params: { sessionId: 'session-1' },
          lastEventId: 'session-1:0',
        },
        {
          next: vi.fn(),
          open() {
            cancel();
            resolve();
          },
        },
      );
    });

    await opened;
    await vi.waitFor(() => expect(cancelledBodies).toHaveLength(1));
  });

  it('aborts an in-flight request and cancels reconnect work', async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, init?: RequestInit): Promise<Response> =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
        }),
    ) as typeof fetch;
    const transport = new HttpControlTransport({
      baseUrl: 'http://127.0.0.1:8766',
      fetch: fetchMock,
    });
    const controller = new AbortController();
    const pending = transport.request({ pathId: 'system.health', signal: controller.signal });
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
  });
});

function sse(event: { eventId: string; eventType: string }): string {
  return `id: ${event.eventId}\nevent: ${event.eventType}\ndata: ${JSON.stringify(event)}\n\n`;
}

async function sha256(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', Uint8Array.from(bytes).buffer);
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, '0')).join('');
}
