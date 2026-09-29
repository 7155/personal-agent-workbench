import { describe, expect, it } from 'vitest';
import { applyAgentSnapshot, createAgentProjection } from '@/contracts/agent-reducer';
import { createPreviewTransport } from './preview-control-transport';
import { previewAgentSnapshot } from '@/features/agent/preview-data';

describe('preview control transport', () => {
  it('exposes Jev preview cards without fabricating provider decisions or writes', async () => {
    const transport = createPreviewTransport();
    const result = record(await transport.request({ pathId: 'agent.organization.read', body: { keys: ['session:session-preview'] } }));
    expect(result.items).toEqual([expect.objectContaining({ key: 'session:session-preview', category: 'unknown', revision: 0 })]);
    expect(result.readOnlyReason).toContain('演示数据');
    expect(record(await transport.request({ pathId: 'agent.organization.suggest', body: { spaceKey: 'session:session-preview' } })).proposal).toBeNull();
    await expect(transport.request({ pathId: 'agent.organization.undo', body: { receiptId: 'missing' } })).rejects.toThrow('演示模式不能写入');
  });

  it('hydrates each preview session snapshot from its session fixture', async () => {
    const transport = createPreviewTransport();

    const populated = record(await transport.request({
      pathId: 'agent.session.snapshot',
      params: { sessionId: 'session-preview' },
    }));
    const fresh = record(await transport.request({
      pathId: 'agent.session.snapshot',
      params: { sessionId: 'session-fresh' },
    }));

    expect(populated.messages).toEqual(expect.arrayContaining([
      expect.objectContaining({
        id: 'session-preview:assistant-architecture',
        role: 'assistant',
      }),
    ]));
    expect(populated.messages).not.toHaveLength(0);
    expect(fresh.messages).toEqual([]);
    expect(fresh.lastSequence).toBe(0);
  });

  it('exposes a dedicated completed multi-stage turn for progressive disclosure', async () => {
    const sessionId = 'session-work-disclosure';
    const snapshot = previewAgentSnapshot(sessionId);
    const projection = applyAgentSnapshot(createAgentProjection(sessionId), snapshot);
    const turn = projection.turnsById[`${sessionId}:turn-implementation`];

    expect(turn?.status).toBe('completed');
    expect(turn?.messageIds).toEqual([
      `${sessionId}:work-user`,
      `${sessionId}:work-intermediate`,
      `${sessionId}:work-final`,
    ]);
    expect(projection.messagesById[`${sessionId}:work-intermediate`]?.role).toBe('assistant');
    expect(projection.messagesById[`${sessionId}:work-final`]?.blocks.map((block) => block.type)).toEqual([
      'text',
      'diff',
      'file',
    ]);
    expect(turn?.activityIds).toHaveLength(3);
    expect(turn?.activityIds.map((id) => projection.activitiesById[id]?.kind)).toEqual([
      'reasoning_summary',
      'tool_finished',
      'tool_finished',
    ]);
    expect(turn?.activityIds.every((id) => projection.activitiesById[id]?.status === 'completed')).toBe(true);
    expect(snapshot.liveEvents.map((event) => (event as { eventType: string }).eventType)).toEqual([
      'reasoning_summary',
      'tool_started',
      'tool_finished',
      'tool_started',
      'tool_finished',
      'turn_completed',
    ]);
  });

  it('lists the progressive-disclosure fixture as an enterable Session', async () => {
    const transport = createPreviewTransport();
    const response = record(await transport.request({
      pathId: 'agent.sessions.list',
      query: { limit: 100 },
    }));

    expect(arrayRecords(response.sessions)).toEqual(expect.arrayContaining([
      expect.objectContaining({
        id: 'session-work-disclosure',
        title: '过程折叠验收',
        messageCount: 3,
      }),
    ]));
  });

  it('replays live preview events only to the owning session subscription', async () => {
    const transport = createPreviewTransport();
    const stateEvents: unknown[] = [];
    const unrelatedEvents: unknown[] = [];
    const unsubscribeStates = transport.subscribe(
      {
        pathId: 'agent.session.events',
        params: { sessionId: 'session-states' },
        lastEventId: 'session-states:0',
      },
      { next: (event) => stateEvents.push(event) },
    );
    const unsubscribeUnrelated = transport.subscribe(
      {
        pathId: 'agent.session.events',
        params: { sessionId: 'session-preview' },
        lastEventId: 'session-preview:12',
      },
      { next: (event) => unrelatedEvents.push(event) },
    );

    // The state fixture is delivered after subscription registration, which
    // mirrors the first live tick after a real snapshot has been hydrated.
    await Promise.resolve();

    expect(stateEvents).toHaveLength(7);
    expect(unrelatedEvents).toEqual([]);
    unsubscribeStates();
    unsubscribeUnrelated();
  });

  it('keeps context trace, debug context, and trace detail turn ids aligned', async () => {
    const transport = createPreviewTransport();
    const list = record(await transport.request({
      pathId: 'agent.session.contextTraces.list',
      params: { sessionId: 'session-preview' },
    }));
    const items = arrayRecords(list.items);
    const turnIds = items.map((item) => String(item.turnId));

    expect(turnIds).toEqual(['turn-initial', 'turn-steady', 'turn-recovered']);
    for (const item of items) {
      const detail = record(await transport.request({
        pathId: 'agent.session.contextTrace.get',
        params: {
          sessionId: 'session-preview',
          traceId: String(item.traceId),
        },
      }));
      expect(detail.turnId).toBe(item.turnId);
    }

    const debug = record(await transport.request({
      pathId: 'agent.session.debugContext.get',
      params: { sessionId: 'session-preview' },
    }));
    expect(record(debug.context).turnId).toBe('turn-recovered');
    expect(arrayRecords(debug.availableTurns).map((item) => String(item.turnId))).toEqual(turnIds);
  });

  it('persists role-based model routing with the same optimistic revision contract', async () => {
    const transport = createPreviewTransport();
    const initial = record(await transport.request({ pathId: 'agent.configuration.get' }));
    const initialSnapshot = record(initial.configuration);
    expect(record(record(initialSnapshot.configuration).modelRouting)).toMatchObject({
      primary: { modelProfile: 'inherit', thinkingLevel: 'inherit' },
      traceDiagnostic: { modelProfile: 'inherit', thinkingLevel: 'inherit' },
      toolAgent: { modelProfile: 'inherit', thinkingLevel: 'inherit' },
    });

    const updated = record(await transport.request({
      pathId: 'agent.configuration.update',
      body: {
        expectedRevision: Number(initialSnapshot.revision),
        changes: {
          'modelRouting.toolAgent': {
            modelProfile: 'openai-codex/gpt-5.6-luna',
            thinkingLevel: 'low',
          },
        },
        updatedBy: 'models-ui',
      },
    }));
    const updatedSnapshot = record(updated.configuration);
    expect(updatedSnapshot.revision).toBe(Number(initialSnapshot.revision) + 1);
    expect(record(record(updatedSnapshot.configuration).modelRouting)).toMatchObject({
      toolAgent: {
        modelProfile: 'openai-codex/gpt-5.6-luna',
        thinkingLevel: 'low',
      },
    });
  });

  it('keeps Memory preview labels product-facing without changing stable identifiers', async () => {
    const transport = createPreviewTransport();
    const graph = record(await transport.request({
      pathId: 'memory.graph.get',
      query: { plane: 'tags' },
    }));
    expect(arrayRecords(graph.nodes)).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'tag:agent-runtime', label: '伙伴运行' }),
    ]));
    expect(JSON.stringify(graph)).not.toMatch(/\bAgent\b|\bRuntime\b/);

    const timelineEnvelope = record(await transport.request({
      pathId: 'memory.activityTimeline.get',
      query: { date: '2026-08-10' },
    }));
    const timeline = record(timelineEnvelope.timeline);
    expect(arrayRecords(timeline.segments)[1]?.summary).toContain(
      '来源记录、已整理记忆、主题、伙伴记忆和时间线',
    );
    expect(JSON.stringify(timeline)).not.toMatch(/Evidence|Current Fact|Topic Book|Role Book|Timeline/);

    const curationRun = record(await transport.request({
      pathId: 'agent.memoryMaintenance.run',
      query: { runId: 'memory_book_preview' },
    }));
    expect(JSON.stringify(curationRun)).toContain('不再用于伙伴上下文或长期记忆');
    expect(JSON.stringify(curationRun)).not.toContain('Agent 上下文');
  });

  it('projects the latest Memory job without starting a new preview task', async () => {
    const transport = createPreviewTransport();
    const empty = record(await transport.request({ pathId: 'agent.memoryMaintenance.run', query: { projectionOnly: true } }));
    expect(empty.job).toEqual({});
    const started = record(await transport.request({ pathId: 'agent.memoryMaintenance.trigger', body: { manual: true } }));
    const projection = record(await transport.request({ pathId: 'agent.memoryMaintenance.run', query: { projectionOnly: true } }));
    expect(record(projection.job)).toMatchObject({ jobId: started.jobId, state: 'completed' });
    const refreshed = record(await transport.request({ pathId: 'agent.memoryMaintenance.run', query: { projectionOnly: true } }));
    expect(refreshed.job).toEqual(projection.job);
  });

  it('imports real browser clipboard Files into owner-scoped preview media receipts', async () => {
    const transport = createPreviewTransport();
    const image = new File(
      [new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])],
      'clipboard.png',
      { type: 'image/png' },
    );

    await expect(transport.pasteImages?.({
      sessionId: 'session-preview',
      files: [image],
      maxFiles: 1,
    })).resolves.toEqual([
      expect.objectContaining({
        id: expect.stringMatching(/^media_preview_[a-f0-9]{24}$/),
        name: 'clipboard.png',
        mimeType: 'image/png',
        byteSize: image.size,
        sessionId: 'session-preview',
        sha256: expect.stringMatching(/^[a-f0-9]{64}$/),
      }),
    ]);
    const roomReceipts = await transport.pasteImages?.({
      roomId: 'room-preview',
      files: [image],
      maxFiles: 1,
    });
    expect(roomReceipts).toEqual([
      expect.objectContaining({
        roomId: 'room-preview',
        sha256: expect.stringMatching(/^[a-f0-9]{64}$/),
      }),
    ]);
    expect(roomReceipts?.[0]).not.toHaveProperty('sessionId');
  });

  it('projects Room ownership onto the participant Sessions listed to Agent', async () => {
    const transport = createPreviewTransport();
    const response = record(await transport.request({
      pathId: 'agent.sessions.list',
      query: { limit: 100 },
    }));
    const sessions = response.sessions as Record<string, unknown>[];

    expect(sessions.find((session) => session.id === 'session-room-present')).toMatchObject({
      roomParticipant: {
        roomId: 'room-preview',
        participantId: 'participant-present',
        status: 'active',
      },
    });
    expect(sessions.find((session) => session.id === 'session-room-firstlight')).toMatchObject({
      roomParticipant: {
        roomId: 'room-preview',
        participantId: 'participant-firstlight',
        status: 'active',
      },
    });
    expect(sessions.find((session) => session.id === 'session-room-future')).toMatchObject({
      roomParticipant: {
        roomId: 'room-preview',
        participantId: 'participant-future',
        status: 'active',
      },
    });
    expect(sessions.find((session) => session.id === 'session-preview')).not.toHaveProperty('roomParticipant');
  });

  it('keeps native Pi Package inspection representative in preview mode', async () => {
    const transport = createPreviewTransport();
    const validation = record(await transport.request({
      pathId: 'agent.extensions.validate',
      body: { packageSource: 'npm:@example/context-helper@2.1.0' },
    }));
    const extension = record(validation.extension);
    expect(validation).toMatchObject({ distribution: 'pi_package' });
    expect(extension).toMatchObject({
      id: 'example.context-helper',
      displayName: '@example/context-helper',
      version: '2.1.0',
      resources: { skills: ['skills/example.context-helper/SKILL.md'] },
      source: { kind: 'npm', requested: 'npm:@example/context-helper@2.1.0' },
    });

    const preview = record(await transport.request({
      pathId: 'agent.extensions.preview',
      body: {
        action: 'install',
        validationToken: String(validation.validationToken),
        enable: true,
      },
    }));
    expect(record(preview.summary)).toMatchObject({
      pluginId: 'example.context-helper',
      resources: { skills: ['skills/example.context-helper/SKILL.md'] },
      source: { kind: 'npm' },
    });
  });

  it('keeps the Preview Trace, Eval, suite, and schedule chain coherent', async () => {
    const transport = createPreviewTransport();
    const traceId = 'trace:turn:preview';

    const sandboxRuns = record(await transport.request({
      pathId: 'observability.sandboxRuns.list',
      query: { limit: 20 },
    }));
    expect(sandboxRuns).toMatchObject({
      schemaVersion: 'rag-ime.observability-sandbox-run-list.v1',
      total: 1,
      items: [expect.objectContaining({
        sandboxRunId: 'sandbox:sgg:preview',
        traceIds: [traceId],
        evalRunIds: ['eval:sgg:preview'],
      })],
    });

    const trace = record(await transport.request({
      pathId: 'observability.trace.get',
      params: { traceId },
      query: { limit: 500 },
    }));
    expect(trace).toMatchObject({
      schemaVersion: 'rag-ime.observability-trace-get.v1',
      traceId,
      trace: expect.objectContaining({
        traceId,
        status: 'completed',
        spans: expect.arrayContaining([
          expect.objectContaining({ name: 'agent.turn' }),
          expect.objectContaining({ name: 'active_rag.retrieve' }),
        ]),
        evidence: expect.arrayContaining([
          expect.objectContaining({ evidenceId: 'evidence:rag:1' }),
        ]),
      }),
    });

    const evals = record(await transport.request({
      pathId: 'observability.evals.list',
      query: { traceId, limit: 100 },
    }));
    expect(arrayRecords(evals.items)).toEqual(expect.arrayContaining([
      expect.objectContaining({
        evalRunId: 'eval:sgg:preview',
        status: 'completed',
        suiteBinding: { suiteId: 'sgg', suiteRevision: 'fixture-v2' },
      }),
    ]));
    expect(arrayRecords(evals.items).find((item) => item.evalRunId === 'eval:ai-judge:sgg:preview'))
      .not.toHaveProperty('suiteBinding');

    const submitted = record(await transport.request({
      pathId: 'observability.evals.evidence.run',
      body: {
        schemaVersion: 'rag-ime.observability-evidence-eval-request.v1',
        traceId,
        requiredEvidenceIds: ['evidence:rag:1'],
        datasetId: 'dataset:sgg-preview-reviewed',
        labelRevision: 'labels:2',
        truthKind: 'human',
      },
    }));
    expect(submitted).toMatchObject({
      schemaVersion: 'rag-ime.eval-run.v1',
      traceIds: [traceId],
      status: 'completed',
      truth: { datasetId: 'dataset:sgg-preview-reviewed', labelRevision: 'labels:2' },
    });
    const refreshedEvals = record(await transport.request({
      pathId: 'observability.evals.list',
      query: { traceId, limit: 100 },
    }));
    expect(arrayRecords(refreshedEvals.items)).toEqual(expect.arrayContaining([
      expect.objectContaining({ evalRunId: submitted.evalRunId }),
    ]));
    expect(arrayRecords(refreshedEvals.items).find((item) => item.evalRunId === submitted.evalRunId))
      .not.toHaveProperty('suiteBinding');

    const suites = record(await transport.request({
      pathId: 'observability.evalSuites.list',
      query: { limit: 100 },
    }));
    expect(arrayRecords(suites.items).map((item) => item.suiteId)).toEqual(
      expect.arrayContaining(['sgg', 'zhanggui-wenshu']),
    );
    expect(arrayRecords(suites.items).map((item) => item.suiteId)).not.toContain('rag-memory');

    const ragTrace = record(await transport.request({
      pathId: 'observability.trace.get',
      params: { traceId: 'trace:active-rag:preview' },
      query: { limit: 500 },
    }));
    expect(ragTrace.trace).toEqual(expect.objectContaining({
      sourceKind: 'active_rag',
      evidence: expect.arrayContaining([
        expect.objectContaining({ evidenceId: 'evidence:memory:1' }),
      ]),
    }));
    const ragEvals = record(await transport.request({
      pathId: 'observability.evals.list',
      query: { traceId: 'trace:active-rag:preview', limit: 100 },
    }));
    expect(arrayRecords(ragEvals.items).find((item) => item.evalRunId === 'eval:ai-judge:rag-preview'))
      .not.toHaveProperty('suiteBinding');

    const schedules = record(await transport.request({
      pathId: 'observability.evalSchedules.list',
      query: { limit: 100 },
    }));
    const firstSchedule = arrayRecords(schedules.items)[0];
    expect(firstSchedule).toMatchObject({ suiteId: 'sgg', suiteRevision: 'fixture-v2' });

    const runs = record(await transport.request({
      pathId: 'observability.evalSchedule.runs',
      params: { scheduleId: String(firstSchedule.id) },
      query: { limit: 100 },
    }));
    expect(runs).toMatchObject({
      schedule: { id: firstSchedule.id },
      items: expect.arrayContaining([
        expect.objectContaining({ evalRunId: 'eval:sgg:preview', traceIds: [traceId] }),
      ]),
    });

    const created = record(await transport.request({
      pathId: 'observability.evalSchedules.create',
      body: {
        suiteId: 'zhanggui-wenshu',
        suiteRevision: 'fixture-v2',
        recurrenceKind: 'weekly',
        recurrenceInterval: 1,
        maxRuns: 4,
        nextDueAtMs: Date.now() + 60_000,
      },
    }));
    expect(created).toMatchObject({
      schemaVersion: 'rag-ime.eval-schedule-create.v1',
      schedule: { suiteId: 'zhanggui-wenshu', suiteRevision: 'fixture-v2', status: 'scheduled' },
    });
    const schedulesAfterCreate = record(await transport.request({
      pathId: 'observability.evalSchedules.list',
      query: { limit: 100 },
    }));
    expect(arrayRecords(schedulesAfterCreate.items)).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: created.schedule && record(created.schedule).id, suiteId: 'zhanggui-wenshu' }),
    ]));

    const oneShot = record(await transport.request({
      pathId: 'observability.evalSchedules.create',
      body: {
        suiteId: 'sgg',
        suiteRevision: 'fixture-v2',
        recurrenceKind: 'daily',
        recurrenceInterval: 1,
        maxRuns: 1,
        nextDueAtMs: Date.now() + 1_000,
      },
    }));
    expect(oneShot).toMatchObject({
      schedule: { status: 'completed', runCount: 1, maxRuns: 1 },
    });
    const oneShotRuns = record(await transport.request({
      pathId: 'observability.evalSchedule.runs',
      params: { scheduleId: String(record(oneShot.schedule).id) },
      query: { limit: 100 },
    }));
    expect(oneShotRuns.items).toEqual([
      expect.objectContaining({
        state: 'succeeded',
        evalRunId: 'eval:sgg:preview:one-shot',
        traceIds: [traceId],
      }),
    ]);
    const oneShotEvals = record(await transport.request({
      pathId: 'observability.evals.list',
      query: { traceId, limit: 100 },
    }));
    expect(arrayRecords(oneShotEvals.items)).toEqual(expect.arrayContaining([
      expect.objectContaining({
        evalRunId: 'eval:sgg:preview:one-shot',
        suiteBinding: { suiteId: 'sgg', suiteRevision: 'fixture-v2' },
        status: 'completed',
      }),
    ]));
  });


  it('returns verifiable Room projections for preview creation and topic mutations', async () => {
    const transport = createPreviewTransport();
    const created = record(await transport.request({
      pathId: 'agent.rooms.create',
      body: {
        title: '发布前检查',
        roomKind: 'collaboration',
        avatar: 'briefcase',
        description: '核对发布边界',
        scenarioPrompt: '',
        routingPolicy: 'natural',
        routingConfig: { maxResponders: 1, naturalJitter: 0, fallbackParticipantId: '' },
        workspaceRoots: ['/Volumes/work/learnA'],
        permissionPolicy: {
          schemaVersion: 'rag-ime.room-permission-policy.v1',
          room: { executionMode: 'workspace_managed' },
          partner: { executionMode: 'inherit' },
          toolAgent: { executionMode: 'inherit' },
        },
        participants: [
          {
            roleId: 'companion-present-v1',
            roleVersion: '1',
            displayName: '澄·今',
            collaborationRole: 'coordinator',
          },
          {
            roleId: 'companion-firstlight-v1',
            roleVersion: '1',
            displayName: '澄·初',
            collaborationRole: 'researcher',
          },
        ],
      },
    }));
    const createdRoom = record(created.room);
    expect(createdRoom).toMatchObject({
      schemaVersion: 'rag-ime.agent-room.v1',
      id: 'room-preview-1',
      title: '发布前检查',
      status: 'active',
      workspaceRoots: ['/Volumes/work/learnA'],
      executionMode: 'workspace_managed',
      permissionPolicy: {
        schemaVersion: 'rag-ime.room-permission-policy.v1',
        room: { executionMode: 'workspace_managed' },
        partner: { executionMode: 'inherit' },
        toolAgent: { executionMode: 'inherit' },
      },
      participants: [
        expect.objectContaining({ displayName: '澄·今', collaborationRole: 'coordinator' }),
        expect.objectContaining({ displayName: '澄·初', collaborationRole: 'researcher' }),
      ],
    });

    const snapshot = record(await transport.request({
      pathId: 'agent.room.snapshot',
      params: { roomId: 'room-preview-1' },
    }));
    expect(snapshot).toMatchObject({
      schemaVersion: 'rag-ime.agent-room-snapshot.v1',
      ok: true,
      room: { id: 'room-preview-1', title: '发布前检查' },
      events: [],
      lastSequence: 0,
    });

    const topicCreated = record(await transport.request({
      pathId: 'agent.room.topic.create',
      params: { roomId: 'room-preview-1' },
      body: { title: '发布风险', summary: '核对上线边界' },
    }));
    expect(record(topicCreated.room).topics).toEqual([
      expect.objectContaining({
        id: 'room-preview-1:topic-1',
        title: '发布风险',
        summary: '核对上线边界',
        status: 'active',
      }),
    ]);

    const topicActivated = record(await transport.request({
      pathId: 'agent.room.topic.update',
      params: { roomId: 'room-preview-1' },
      body: { topicId: 'room-preview-1:topic-1', activate: true },
    }));
    expect(record(topicActivated.room)).toMatchObject({
      id: 'room-preview-1',
      activeTopicId: 'room-preview-1:topic-1',
    });
  });

  it('dismisses preview Ask cards only after a durable resolution event', async () => {
    const transport = createPreviewTransport();
    const sessionId = 'session-input';
    const events: Record<string, unknown>[] = [];
    const unsubscribe = transport.subscribe(
      {
        pathId: 'agent.session.events',
        params: { sessionId },
        lastEventId: `${sessionId}:4`,
      },
      { next: (event) => events.push(record(event)) },
    );

    await transport.request({
      pathId: 'agent.session.ui.resolve',
      params: { sessionId },
      body: {
        requestId: 'preview-grouped-question',
        value: '{"answers":[]}',
        resolutionSource: 'direct_user',
      },
    });

    expect(events).toEqual([
      expect.objectContaining({
        eventType: 'user_input_required',
        sequence: 5,
        payload: expect.objectContaining({
          requestId: 'preview-grouped-question',
          resolutionState: 'resolved',
          resolutionSource: 'direct_user',
        }),
      }),
    ]);
    unsubscribe();
  });

  it('exercises background job logs and cancellation through production routes', async () => {
    const transport = createPreviewTransport();
    const sessionId = 'session-states';
    const events: Record<string, unknown>[] = [];
    const unsubscribe = transport.subscribe(
      {
        pathId: 'agent.session.events',
        params: { sessionId },
        lastEventId: `${sessionId}:7`,
      },
      { next: (event) => events.push(record(event)) },
    );
    const listed = record(await transport.request({
      pathId: 'agent.session.backgroundJobs.list',
      params: { sessionId },
    }));
    const jobs = listed.items as Record<string, unknown>[];
    const running = jobs.find((job) => job.status === 'running');
    expect(running).toMatchObject({ label: '前端生产构建' });

    const jobId = String(running?.jobId ?? '');
    const logs = record(await transport.request({
      pathId: 'agent.session.backgroundJob.logs',
      params: { sessionId, jobId },
      query: { cursor: 0 },
    }));
    expect(logs.text).toContain('vite build');

    const cancelled = record(await transport.request({
      pathId: 'agent.session.backgroundJob.cancel',
      params: { sessionId, jobId },
      body: { reason: 'preview-test' },
    }));
    expect(cancelled.job).toMatchObject({ jobId, status: 'cancelled' });
    expect(events).toEqual(expect.arrayContaining([
      expect.objectContaining({
        eventType: 'background_job_cancelled',
        payload: expect.objectContaining({
          job: expect.objectContaining({ jobId, status: 'cancelled' }),
        }),
      }),
    ]));

    const refreshed = record(await transport.request({
      pathId: 'agent.session.backgroundJobs.list',
      params: { sessionId },
    }));
    expect(refreshed.items).toEqual(expect.arrayContaining([
      expect.objectContaining({ jobId, status: 'cancelled' }),
    ]));
    unsubscribe();
  });

  it('keeps the Input preview on real settings and lexicon contracts', async () => {
    const transport = createPreviewTransport();
    const capabilities = await transport.capabilities();
    expect(capabilities.features).toMatchObject({
      configurationSettingsWorkContract: true,
      managementWorkContract: true,
    });

    const source = record(await transport.request({ pathId: 'input.source.get' }));
    expect(source).toMatchObject({ typingReady: true, readinessState: 'ready' });

    const schema = record(await transport.request({ pathId: 'configuration.schema' }));
    const sections = schema.sections as Record<string, unknown>[];
    expect(sections.map((section) => section.id)).toEqual(expect.arrayContaining([
      'interaction',
      'models',
      'lexiconOrganization',
    ]));

    const review = record(await transport.request({ pathId: 'input.lexicon.review' }));
    expect(review).toMatchObject({
      schemaVersion: 'rag-ime.rime-lexicon-review.v1',
      ok: true,
      applySupported: true,
      organization: expect.objectContaining({
        schemaVersion: 'rag-ime.lexicon-organization-status.v1',
        owner: 'maintenance_poll',
        decoderOwner: 'rime',
        enabled: true,
      }),
    });

    const preview = record(await transport.request({
      pathId: 'configuration.settings.preview',
      body: {
        changes: { 'lexiconOrganization.runsPerDay': 4 },
        expectedRuntimeRevision: 12,
      },
    }));
    const applied = record(await transport.request({
      pathId: 'configuration.settings.apply',
      body: {
        changes: { 'lexiconOrganization.runsPerDay': 4 },
        expectedRuntimeRevision: 12,
        previewToken: preview.previewToken as string,
        payloadSha256: preview.payloadSha256 as string,
        confirmText: 'apply',
      },
    }));
    expect(applied).toMatchObject({
      ok: true,
      pathId: 'configuration.settings.apply',
      payloadSha256: preview.payloadSha256,
      rollbackAvailable: true,
    });
    expect(record(record(
      record(await transport.request({ pathId: 'configuration.settings' })).settings,
    ).lexiconOrganization).runsPerDay).toBe(4);

    const rolledBack = record(await transport.request({
      pathId: 'configuration.settings.rollback',
      body: {
        receiptId: applied.receiptId as string,
        rollbackToken: applied.rollbackToken as string,
        payloadSha256: preview.payloadSha256 as string,
        confirmText: 'rollback',
      },
    }));
    expect(rolledBack).toMatchObject({
      ok: true,
      pathId: 'configuration.settings.rollback',
      payloadSha256: preview.payloadSha256,
      rollbackAvailable: false,
    });
  });

  it('keeps History preview data and tombstone receipts on production routes', async () => {
    const transport = createPreviewTransport();
    const page = record(await transport.request({
      pathId: 'history.page',
      query: { query: 'TextEdit', filter: '' },
    }));
    expect(page.items).toEqual([
      expect.objectContaining({ id: 201, source: 'rime_commit' }),
    ]);

    const detail = record(await transport.request({
      pathId: 'history.detail',
      query: { eventId: 201 },
    }));
    expect(record(detail.item)).toMatchObject({
      id: 201,
      auxiliaryContext: expect.objectContaining({ available: true }),
      feedback: expect.objectContaining({ acceptedCount: 1 }),
    });

    const preview = record(await transport.request({
      pathId: 'history.tombstone.preview',
      body: { eventId: 201, reason: 'control-center-history', expectedRuntimeRevision: 12 },
    }));
    const applied = record(await transport.request({
      pathId: 'history.tombstone.apply',
      body: {
        eventId: 201,
        reason: 'control-center-history',
        expectedRuntimeRevision: 12,
        previewToken: preview.previewToken as string,
        payloadSha256: preview.payloadSha256 as string,
        confirmText: 'apply',
      },
    }));
    expect(applied).toMatchObject({
      ok: true,
      pathId: 'history.tombstone.apply',
      rollbackAvailable: true,
    });
  });

  it('persists Knowledge preview mutations and projects reparse work through jobs', async () => {
    const transport = createPreviewTransport();
    const created = record(await transport.request({
      pathId: 'knowledgeBases.create',
      body: {
        name: '迁移证据库',
        description: '只包含外部项目文档',
        agentEnabled: false,
        parserProvider: 'auto',
      },
    }));
    const createdBase = record(created.base);
    const createdId = String(createdBase.id);
    expect(createdBase).toMatchObject({ name: '迁移证据库', description: '只包含外部项目文档' });

    const listed = arrayRecords(record(await transport.request({
      pathId: 'knowledgeBases.list',
    })).items);
    expect(listed).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: createdId, name: '迁移证据库' }),
    ]));

    const updated = record(await transport.request({
      pathId: 'knowledgeBases.update',
      params: { kbId: createdId },
      body: {
        name: '迁移证据库 · 已核验',
        description: '服务端已确认',
        expectedRevision: Number(createdBase.revision),
      },
    }));
    expect(record(updated.base)).toMatchObject({
      id: createdId,
      name: '迁移证据库 · 已核验',
      description: '服务端已确认',
      revision: 2,
    });
    expect(record(record(await transport.request({
      pathId: 'knowledgeBases.get',
      params: { kbId: createdId },
    })).base)).toMatchObject({ name: '迁移证据库 · 已核验' });

    await transport.request({
      pathId: 'knowledgeBases.document.retry',
      params: { kbId: 'kb:preview-project-docs', fileId: 'file:preview-yuxi' },
      body: { stage: 'parse', parserProvider: 'builtin', expectedRevision: 1 },
    });
    const jobs = arrayRecords(record(await transport.request({
      pathId: 'knowledgeBases.jobs.list',
      params: { kbId: 'kb:preview-project-docs' },
    })).items);
    expect(jobs).toEqual([
      expect.objectContaining({
        fileId: 'file:preview-yuxi',
        fileName: '伙伴运行笔记.md',
        kind: 'reparse',
        status: 'succeeded',
        progress: 1,
      }),
    ]);
  });

  it('honors temporary Knowledge retrieval budgets without mutating the saved base', async () => {
    const transport = createPreviewTransport();
    const params = { kbId: 'kb:preview-project-docs' };
    const before = await transport.request({ pathId: 'knowledgeBases.get', params });
    const result = record(await transport.request({ pathId: 'knowledgeBases.search', params, body: { query: '工具', mode: 'lexical', topK: 3, threshold: .95, rerank: false } }));
    expect(result.items).toEqual([]);
    expect(record(result.retrieval)).toMatchObject({ mode: 'lexical', config: { mode: 'lexical', topK: 3, threshold: .95 }, libraries: [{ returned: 0, denseCandidates: 0 }] });
    expect(await transport.request({ pathId: 'knowledgeBases.get', params })).toEqual(before);
    await expect(transport.request({ pathId: 'knowledgeBases.search', params, body: { query: '工具', rerank: true } })).rejects.toThrow('未配置重排模型');
    const other = record(await transport.request({ pathId: 'knowledgeBases.search', params: { kbId: 'kb:preview-antarctic-papers' }, body: { query: '工具' } }));
    expect(other.items).toEqual([]);
  });

  it('uses the current Antarctic corpus fixture instead of the retired RL fixture', async () => {
    const transport = createPreviewTransport();
    const bases = arrayRecords(record(await transport.request({
      pathId: 'knowledgeBases.list',
    })).items);
    expect(bases).toEqual(expect.arrayContaining([
      expect.objectContaining({
        id: 'kb:preview-antarctic-papers',
        name: '南极论文资料库',
        documentCount: 198,
        status: 'building',
      }),
    ]));
    expect(JSON.stringify(bases)).not.toContain('强化学习论文测试库');

    const documents = arrayRecords(record(await transport.request({
      pathId: 'knowledgeBases.documents.list',
      params: { kbId: 'kb:preview-antarctic-papers' },
    })).items);
    expect(documents).toEqual(expect.arrayContaining([
      expect.objectContaining({
        id: 'file:preview-antarctic-papers',
        fileName: 'Zotero 南极论文库（198 个 PDF）.manifest.md',
        status: 'queued',
        chunkCount: 0,
      }),
    ]));
  });

  it('keeps Work Documents preview data and terminal receipts on production routes', async () => {
    const transport = createPreviewTransport();
    const listed = record(await transport.request({
      pathId: 'workDocuments.list',
    }));
    expect(listed.items).toEqual([
      expect.objectContaining({
        documentId: 'workdoc_0123456789abcdef0123456789abcdef',
        state: 'active',
      }),
    ]);

    const archived = record(await transport.request({
      pathId: 'workDocuments.archive',
      params: { documentId: 'workdoc_0123456789abcdef0123456789abcdef' },
      body: { terminalReceiptId: 'terminal-receipt-preview' },
    }));
    expect(archived).toMatchObject({
      ok: true,
      document: expect.objectContaining({ state: 'archived' }),
      receipt: expect.objectContaining({ operation: 'archive', status: 'applied' }),
    });

    const history = record(await transport.request({
      pathId: 'workDocuments.history.search',
      query: { query: '' },
    }));
    expect(history.items).toEqual(expect.arrayContaining([
      expect.objectContaining({ state: 'archived', terminalReceiptId: expect.any(String) }),
    ]));
  });
  it('rolls an installed extension back once with a truthful version and display name', async () => {
    const transport = createPreviewTransport();
    const before = arrayRecords(record(await transport.request({
      pathId: 'agent.extensions.list',
    })).items);
    expect(before).toEqual(expect.arrayContaining([
      expect.objectContaining({
        id: 'timeline-inspector',
        displayName: 'Timeline Inspector',
        version: '1.0.0',
        rollbackAvailable: true,
      }),
    ]));

    const preview = record(await transport.request({
      pathId: 'agent.extensions.preview',
      body: { action: 'rollback', pluginId: 'timeline-inspector' },
    }));
    expect(record(preview.summary)).toMatchObject({
      action: 'rollback',
      pluginId: 'timeline-inspector',
      displayName: 'Timeline Inspector',
      version: '0.9.0',
    });

    await expect(transport.request({
      pathId: 'agent.extensions.apply',
      body: {
        previewToken: preview.previewToken as string,
        payloadSha256: preview.payloadSha256 as string,
        confirmText: 'apply',
      },
    })).resolves.toMatchObject({
      ok: true,
      receipt: { receiptId: 'plugin:rollback:preview' },
    });

    const after = arrayRecords(record(await transport.request({
      pathId: 'agent.extensions.list',
    })).items);
    expect(after).toEqual(expect.arrayContaining([
      expect.objectContaining({
        id: 'timeline-inspector',
        displayName: 'Timeline Inspector',
        version: '0.9.0',
        rollbackAvailable: false,
      }),
    ]));
    await expect(transport.request({
      pathId: 'agent.extensions.preview',
      body: { action: 'rollback', pluginId: 'timeline-inspector' },
    })).rejects.toThrow('这个扩展当前没有可恢复的上一版本。');
  });

  it('uninstalls an installed extension only after the reviewed preview is applied', async () => {
    const transport = createPreviewTransport();
    const preview = record(await transport.request({
      pathId: 'agent.extensions.preview',
      body: { action: 'uninstall', pluginId: 'timeline-inspector' },
    }));

    expect(record(preview.summary)).toMatchObject({
      action: 'uninstall',
      pluginId: 'timeline-inspector',
      displayName: 'Timeline Inspector',
    });
    expect(arrayRecords(record(await transport.request({
      pathId: 'agent.extensions.list',
    })).items)).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'timeline-inspector' }),
    ]));

    await transport.request({
      pathId: 'agent.extensions.apply',
      body: {
        previewToken: preview.previewToken as string,
        payloadSha256: preview.payloadSha256 as string,
        confirmText: 'apply',
      },
    });

    const after = arrayRecords(record(await transport.request({
      pathId: 'agent.extensions.list',
    })).items);
    expect(after).toEqual(expect.not.arrayContaining([
      expect.objectContaining({ id: 'timeline-inspector' }),
    ]));
  });

});

function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function arrayRecords(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(record) : [];
}
