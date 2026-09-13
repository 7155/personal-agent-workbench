import { describe, expect, it } from 'vitest';

import {
  CONTROL_ROUTES,
  ControlRoutePolicyError,
  resolveControlPath,
} from './routes';
import { assertControlRequest, assertControlSubscription } from './transport';

const canonicalPathIds = [
  'control.bootstrap',
  'control.capabilities',
  'control.events',
  'system.health',
  'overview.get',
  'input.source.get',
  'input.lexicon.review',
  'input.lexicon.apply',
  'input.lexicon.rollback',
  'input.prediction.liveTrace',
  'observability.snapshot',
  'observability.trace.get',
  'observability.evals.list',
  'observability.traceDiagnosticReports.list',
  'observability.traceDiagnosticReports.create',
  'observability.traceDiagnosticReport.get',
  'observability.traceDiagnosticReport.finalize',
  'observability.traceDiagnosticReport.optimizationCommand',
  'observability.traceOptimization.library',
  'observability.traceOptimization.capabilities',
  'observability.traceDiagnosticReport.repairAuthorize',
  'observability.traceDiagnosticReport.repairVerify',
  'observability.evals.aiJudge.run',
  'observability.traceRepair.changeEvidence',
  'observability.traceRepair.testEvidence',
  'observability.traceRepair.receipt.create',
  'observability.traceRepair.receipt.get',
  'observability.traceRepair.recheck',
  'observability.traceReplay.case.create',
  'observability.traceReplay.case.get',
  'observability.traceReplay.verify',
  'observability.traceReplay.verification.get',
  'observability.evalSuites.list',
  'observability.sandboxRuns.list',
  'observability.sandboxRun.get',
  'extension.sandbox.experiment.run',
  'files.list',
  'files.read',
  'observability.evals.evidence.run',
  'observability.evalSchedules.list',
  'observability.evalSchedules.create',
  'observability.evalSchedule.runs',
  'observability.evalSchedule.action',
  'observability.events',
  'agent.runtime.get',
  'agent.runtime.ensure',
  'agent.providers.get',
  'agent.provider.auth.preview',
  'agent.provider.auth.apply',
  'agent.provider.oauth.status',
  'agent.provider.oauth.cancel',
  'agent.configuration.get',
  'agent.configuration.update',
  'agent.eval-lab.runs',
  'agent.eval-lab.evidence',
  'agent.eval-lab.golden.get',
  'agent.eval-lab.projects.get',
  'agent.eval-lab.projects.command',
  'agent.eval-lab.apps.get',
  'agent.eval-lab.apps.command',
  'agent.eval-lab.apps.download',
  'agent.eval-lab.golden.command',
  'agent.eval-lab.scene-recipes.get',
  'agent.eval-lab.scene-recipes.apply',
  'agent.eval-lab.scene-recipes.rollback',
  'agent.eval-lab.trials.get',
  'agent.eval-lab.trials.start',
  'agent.eval-lab.trials.cancel',
  'agent.sessions.list',
  'agent.sessions.create',
  'agent.sessions.surface.ensure',
  'agent.session.snapshot',
  'agent.session.workspace.list',
  'agent.session.workspace.read',
  'agent.session.workspace.save',
  'agent.session.rename',
  'agent.session.archive',
  'agent.session.mode.update',
  'agent.session.capability-policy.update',
  'agent.session.delete',
  'agent.session.prompt',
  'agent.session.rewrite',
  'agent.session.forks.list',
  'agent.session.forks.create',
  'agent.session.abort',
  'agent.session.review.resolve',
  'agent.session.ui.resolve',
  'agent.session.compact',
  'agent.session.commands',
  'agent.session.command.invoke',
  'agent.session.models',
  'agent.session.model.select',
  'agent.session.thinking.select',
  'agent.session.events',
  'agent.session.backgroundJobs.list',
  'agent.session.backgroundJob.get',
  'agent.session.backgroundJob.logs',
  'agent.session.backgroundJob.cancel',
  'agent.session.workflow.get',
  'agent.session.goal.mutate',
  'agent.session.intercom.list',
  'agent.session.intercom.send',
  'agent.session.contextItems.list',
  'agent.session.contextItems.ack',
  'agent.session.contextTraces.list',
  'agent.session.contextTrace.get',
  'agent.session.debugContext.get',
  'agent.artifact.get',
  'agent.collaborationProfile.get',
  'agent.collaborationProfile.command',
  'agent.knowledge.search',
  'agent.governance.read',
  'agent.knowledgeGovernance.read',
  'agent.knowledge.read',
  'agent.media.list',
  'agent.media.preview',
  'agent.deep-search',
  'agent.rooms.list',
  'agent.rooms.create',
  'agent.room.get',
  'agent.room.snapshot',
  'agent.room.conversationSnapshot',
  'agent.room.startGate.get',
  'agent.room.startGate.confirm',
  'agent.room.archive',
  'agent.room.participant.add',
  'agent.room.participant.remove',
  'agent.room.participant.update',
  'agent.room.delete',
  'agent.room.message',
  'agent.room.participant.steer',
  'agent.room.abort',
  'agent.room.events',
  'agent.room.history',
  'agent.room.topics',
  'agent.room.topic.create',
  'agent.room.topic.update',
  'agent.room.artifacts',
  'agent.room.artifact.add',
  'agent.room.artifact.update',
  'agent.room.workItems.list',
  'agent.room.workItem.create',
  'agent.room.workItem.get',
  'agent.room.workItem.reassign',
  'agent.room.workItem.resume',
  'agent.roles.list',
  'agent.roles.create',
  'agent.roles.update',
  'agent.roles.archive',
  'agent.role.models',
  'agent.role.runtimeDefaults.update',
  'agent.roleBook.get',
  'agent.roleBook.activation.preview',
  'agent.roleBook.activation.apply',
  'agent.roleBook.activation.rollback',
  'agent.roleBook.draft.decision',
  'agent.personalContext.observability',
  'agent.tools.list',
  'agent.extensions.list',
  'agent.extensions.usage',
  'agent.extensions.catalog',
  'agent.extensions.skills.list',
  'agent.extensions.skills.get',
  'agent.extensions.create',
  'agent.extensions.proposals',
  'agent.extensions.validate',
  'agent.extensions.preview',
  'agent.extensions.apply',
  'agent.lifecycleHooks.get',
  'agent.lifecycleHooks.update',
  'agent.approvals.list',
  'agent.approval.get',
  'agent.approval.decide',
  'agent.memoryMaintenance.run',
  'agent.memoryMaintenance.trigger',
  'agent.subagents.templates',
  'agent.subagents.list',
  'agent.subagents.create',
  'agent.subagent.get',
  'agent.subagent.console',
  'agent.subagent.control',
  'agent.subagent.abort',
  'agent.memorySources.list',
  'agent.wakeSchedules.list',
  'agent.wakeSchedules.create',
  'agent.wakeSchedule.runs',
  'agent.wakeSchedule.action',
  'browser.status',
  'browser.tabs',
  'browser.snapshot.latest',
  'browser.snapshot.image',
  'browser.traces',
  'browser.command',
  'browser.stop',
  'browser.managed.start',
  'browser.managed.stop',
  'terminal.sessions.list',
  'terminal.session.create',
  'terminal.session.read',
  'terminal.session.write',
  'terminal.session.resize',
  'terminal.session.close',
  'planning.dashboard',
  'planning.mutation.preview',
  'planning.task.save',
  'planning.goal.save',
  'planning.task.action',
  'planning.taskEvent.undo',
  'planning.mutation.rollback',
  'workDocuments.list',
  'workDocuments.history.search',
  'workDocuments.get',
  'workDocuments.register',
  'workDocuments.archive',
  'workDocuments.repair',
  'workDocuments.reopen',
  'workDocuments.erase.preview',
  'workDocuments.erase',
  'memory.summary',
  'memory.pages',
  'memory.reference.get',
  'memory.graph.get',
  'memory.entity.get',
  'memory.edit',
  'memory.source.disposition',
  'memory.lifecycle.status',
  'memory.lifecycle.refresh',
  'memory.lifecycle.report',
  'memory.lifecycle.forget.preview',
  'memory.lifecycle.forget.apply',
  'memory.book.archive.preview',
  'memory.book.archive.apply',
  'memory.book.archive.rollback',
  'memory.activityTimeline.get',
  'memory.activityTimeline.calendar',
  'memory.activityTimeline.build',
  'memory.activityTimeline.approve',
  'memory.activityTimeline.reject',
  'history.page',
  'history.detail',
  'history.tombstone.preview',
  'history.tombstone.apply',
  'history.tombstone.rollback',
  'knowledge.start',
  'knowledge.cancel',
  'knowledge.status',
  'knowledge.routeStatus',
  'knowledge.database.apply.preview',
  'knowledge.database.draft.edit',
  'knowledge.database.apply',
  'knowledge.database.rollback',
  'knowledgeBases.list',
  'knowledgeBases.create',
  'knowledgeBases.get',
  'knowledgeBases.update',
  'knowledgeBases.delete.preview',
  'knowledgeBases.delete.apply',
  'knowledgeBases.documents.list',
  'knowledgeBases.document.import',
  'knowledgeBases.document.retry',
  'knowledgeBases.document.delete',
  'knowledgeBases.document.get',
  'knowledgeBases.document.source',
  'knowledgeBases.asset.get',
  'knowledgeBases.jobs.list',
  'knowledgeBases.job.cancel',
  'knowledgeBases.chunkPreview',
  'knowledgeBases.search',
  'knowledgeBases.find',
  'knowledgeBases.open',
  'knowledgeBases.reindexPreview',
  'knowledgeBases.rebuild',
  'knowledgeBases.graph.get',
  'knowledgeBases.graph.rebuild',
  'knowledgeWorker.health',
  'knowledgeParsers.list',
  'knowledgeEmbedding.profile',
  'knowledgeEmbedding.probe',
  'knowledgeEmbedding.impact',
  'diagnostics.runtime',
  'diagnostics.predictor',
  'diagnostics.models',
  'diagnostics.action.preview',
  'diagnostics.action.start',
  'diagnostics.action.job',
  'configuration.settings',
  'configuration.schema',
  'configuration.settings.preview',
  'configuration.settings.apply',
  'configuration.settings.rollback',
  'configuration.import.preview',
  'configuration.import.apply',
  'configuration.backup.export',
  'configuration.restore.preview',
  'configuration.restore.apply',
] as const;

describe('control route policy', () => {
  it('mirrors the canonical Lane F pathId manifest exactly', () => {
    expect(Object.keys(CONTROL_ROUTES).sort()).toEqual([...canonicalPathIds].sort());
    expect(Object.keys(CONTROL_ROUTES)).toHaveLength(canonicalPathIds.length);
  });

  it('keeps Pi credentials behind preview/apply and never accepts secrets on preview', () => {
    expect(() =>
      assertControlRequest({
        pathId: 'agent.provider.auth.preview',
        body: { provider: 'openai-codex', action: 'set_api_key', apiKey: 'not-here' },
      } as never),
    ).toThrow(/body field/);
    expect(() =>
      assertControlRequest({
        pathId: 'agent.provider.auth.apply',
        body: { previewToken: 'preview-token', confirmText: 'replace', apiKey: 'secret' },
      }),
    ).not.toThrow();
    expect(() =>
      assertControlRequest({
        pathId: 'agent.provider.auth.apply',
        body: { previewToken: 'preview-token', confirmText: 'replace', refreshToken: 'secret' },
      } as never),
    ).toThrow(/body field/);
  });

  it('resolves an encoded traceId through the local trace detail route', () => {
    expect(CONTROL_ROUTES['observability.trace.get']).toMatchObject({
      method: 'GET',
      path: '/api/observability/traces/:traceId',
      params: { traceId: null },
      query: ['limit', 'beforeSequence'],
      responseContract: 'observability-trace-get.v1',
    });
    expect(resolveControlPath('observability.trace.get', {
      traceId: 'trace:active-rag:alpha',
    })).toBe('/api/observability/traces/trace%3Aactive-rag%3Aalpha');
    expect(() => resolveControlPath('observability.trace.get', {
      traceId: 'trace/escape',
    })).toThrow(/invalid traceId/);
    const maximumTraceId = `t${'a'.repeat(159)}`;
    expect(resolveControlPath('observability.trace.get', { traceId: maximumTraceId }))
      .toBe(`/api/observability/traces/${maximumTraceId}`);
    expect(() => resolveControlPath('observability.trace.get', {
      traceId: `${maximumTraceId}a`,
    })).toThrow(/invalid traceId/);
  });

  it('keeps Eval reads trace-scoped and manual ground-truth runs versioned', () => {
    expect(CONTROL_ROUTES['observability.evals.list']).toMatchObject({
      method: 'GET',
      path: '/api/observability/evals',
      query: ['traceId', 'limit'],
      requiredQuery: ['traceId'],
      responseContract: 'observability-eval-list.v1',
    });
    expect(() => assertControlRequest({
      pathId: 'observability.evals.list',
      query: { traceId: 'trace:active-rag:alpha' },
    })).not.toThrow();
    expect(() => assertControlRequest({
      pathId: 'observability.evals.evidence.run',
      body: {
        schemaVersion: 'rag-ime.observability-evidence-eval-request.v1',
        traceId: 'trace:active-rag:alpha',
        requiredEvidenceIds: ['knowledge:answer'],
        datasetId: 'manual:alpha',
        labelRevision: 'review:1',
        truthKind: 'human',
      },
    })).not.toThrow();
    expect(() => assertControlRequest({
      pathId: 'observability.evals.evidence.run',
      body: {
        traceId: 'trace:active-rag:alpha',
        requiredEvidenceIds: [],
        datasetId: 'manual:alpha',
        labelRevision: 'review:1',
        truthKind: 'human',
      },
    } as never)).toThrow(/required body field/);
  });

  it('exposes prediction trace only as a bounded local input feed', () => {
    expect(CONTROL_ROUTES['input.prediction.liveTrace']).toEqual({
      method: 'GET',
      path: '/api/prediction/live-trace',
      query: ['limit', 'sessionId'],
    });
  });

  it('allows the bounded recent view without changing full Session snapshot callers', () => {
    expect(() => assertControlRequest({
      pathId: 'agent.session.snapshot',
      params: { sessionId: 'session-1' },
      query: { view: 'recent' },
    })).not.toThrow();
    expect(() => assertControlRequest({
      pathId: 'agent.session.snapshot',
      params: { sessionId: 'session-1' },
    })).not.toThrow();
  });

  it('allows only the exact Room turn authority on the existing background-job cancel route', () => {
    expect(() => assertControlRequest({
      pathId: 'agent.session.backgroundJob.cancel',
      params: { sessionId: 'session-room-worker', jobId: 'bg_123' },
      body: { reason: 'control_center_requested', roomTurnId: 'room-root-1' },
    })).not.toThrow();
    expect(() => assertControlRequest({
      pathId: 'agent.session.backgroundJob.cancel',
      params: { sessionId: 'session-room-worker', jobId: 'bg_123' },
      body: { reason: 'control_center_requested', roomId: 'room-1' },
    } as never)).toThrow(/body field/);
  });

  it('keeps configuration file paths behind the five local migration contracts', () => {
    expect(() => assertControlRequest({
      pathId: 'configuration.import.preview',
      body: { path: '/trusted/from-native.yaml' },
    })).not.toThrow();
    expect(() => assertControlRequest({
      pathId: 'configuration.import.apply',
      body: {
        path: '/trusted/from-native.yaml',
        expectedRuntimeRevision: 9,
        previewToken: 'sha256:preview',
        confirmText: 'IMPORT RAG-IME CONFIGURATION',
      },
    })).not.toThrow();
    expect(() => assertControlRequest({
      pathId: 'configuration.import.apply',
      body: {
        path: '/trusted/from-native.yaml',
        expectedRuntimeRevision: 9,
        previewToken: 'sha256:preview',
      },
    } as never)).toThrow(/required body/);
    expect(() => assertControlRequest({
      pathId: 'configuration.restore.apply',
      body: {
        path: '/trusted/backup.ragime-backup',
        restoreToken: 'a'.repeat(64),
        confirmText: 'RESTORE RAG-IME',
        expectedRuntimeRevision: 9,
        arbitrary: true,
      },
    } as never)).toThrow(/body field/);
  });

  it('requires the typed binary transport for bounded knowledge assets', () => {
    expect(CONTROL_ROUTES['knowledgeBases.asset.get']).toMatchObject({
      method: 'GET',
      binary: true,
      params: { kbId: null, fileId: null, assetId: null },
    });
    expect(() => assertControlRequest({
      pathId: 'knowledgeBases.asset.get',
      params: {
        kbId: 'kb_docs',
        fileId: 'file_manual',
        assetId: 'a'.repeat(64),
      },
    })).toThrow(/typed binary transport/);
  });

  it('binds memory archive apply and rollback to server receipts', () => {
    expect(() =>
      assertControlRequest({
        pathId: 'memory.book.archive.apply',
        body: {
          bookId: 'book-1',
          archived: true,
          reason: 'control_center_archive',
          expectedRuntimeRevision: 4,
          previewToken: 'preview-token',
          payloadSha256: 'sha256:payload',
          confirmText: 'apply',
        },
      }),
    ).not.toThrow();
    expect(() =>
      assertControlRequest({
        pathId: 'memory.book.archive.rollback',
        body: { receiptId: 'receipt-1', rollbackToken: 'rollback-token' },
      } as never),
    ).toThrow(/required body/);
  });

  it('allows only the public goal contract on planning goal save', () => {
    const body = {
      goalId: 'goal-1',
      title: '完成控制中心切换',
      detail: '验证真实规划写入',
      horizon: 'medium_term',
      status: 'active',
      priority: 3,
      targetDate: '2026-07-31',
      project: 'wisdom-weasel-rag-ime',
      expectedRuntimeRevision: 7,
      previewToken: 'preview-token',
      payloadSha256: 'sha256:payload',
      confirmText: 'apply',
    };
    expect(() => assertControlRequest({ pathId: 'planning.goal.save', body })).not.toThrow();
    expect(() => assertControlRequest({
      pathId: 'planning.goal.save',
      body: { ...body, metadata: { systemPrompt: 'client-owned' } },
    } as never)).toThrow(/body field/);
  });

  it('keeps active, history, archive, reopen, and erase work-document routes distinct', () => {
    expect(CONTROL_ROUTES['workDocuments.list']).toMatchObject({
      method: 'GET',
      path: '/api/agent/work-documents',
      query: ['limit'],
      responseContract: 'work-document-list.v1',
    });
    expect(CONTROL_ROUTES['workDocuments.history.search']).toMatchObject({
      method: 'GET',
      path: '/api/agent/work-documents/history/search',
      query: ['query', 'limit'],
      responseContract: 'work-document-list.v1',
    });
    expect(CONTROL_ROUTES['workDocuments.get'].responseContract).toBe('work-document-detail.v1');
    expect(CONTROL_ROUTES['workDocuments.archive'].responseContract).toBe('work-document-command.v1');
    expect(CONTROL_ROUTES['workDocuments.repair'].responseContract).toBe('work-document-command.v1');
    expect(CONTROL_ROUTES['workDocuments.reopen'].responseContract).toBe('work-document-command.v1');
    expect(CONTROL_ROUTES['workDocuments.erase.preview'].responseContract).toBe('work-document-command.v1');
    expect(CONTROL_ROUTES['workDocuments.erase'].responseContract).toBe('work-document-command.v1');
    expect(() => assertControlRequest({
      pathId: 'workDocuments.list',
      query: { limit: 100, scope: 'archived' },
    } as never)).toThrow(/query field/);
    expect(() => assertControlRequest({
      pathId: 'workDocuments.archive',
      params: { documentId: 'work-document-1' },
      body: { terminalReceiptId: 'terminal-receipt-1' },
    })).not.toThrow();
    expect(() => assertControlRequest({
      pathId: 'workDocuments.archive',
      params: { documentId: 'work-document-1' },
      body: {},
    } as never)).toThrow(/required body/);
    expect(() => assertControlRequest({
      pathId: 'workDocuments.reopen',
      params: { documentId: 'work-document-1' },
      body: { authorityRevision: 8, transitionReceiptId: 'archive-receipt-1' },
    })).not.toThrow();
    expect(() => assertControlRequest({
      pathId: 'workDocuments.erase',
      params: { documentId: 'work-document-1' },
      body: {
        sessionId: 'session-1',
        approvalId: 'approval-1',
        payloadSha256: 'sha256:payload',
      },
    })).not.toThrow();
    expect(() => assertControlRequest({
      pathId: 'workDocuments.erase',
      params: { documentId: 'work-document-1' },
      body: { sessionId: 'session-1', approvalId: 'approval-1' },
    } as never)).toThrow(/required body/);
  });

  it('allows only stable-id local memory edit fields', () => {
    expect(() =>
      assertControlRequest({
        pathId: 'memory.edit',
        body: { kind: 'books', id: 'book-1', title: '长期计划', summary: '已复核', tags: ['计划'] },
      }),
    ).not.toThrow();
    expect(() =>
      assertControlRequest({ pathId: 'memory.edit', body: { kind: 'books', title: '缺少稳定标识' } } as never),
    ).toThrow(/required body/);
    expect(() =>
      assertControlRequest({
        pathId: 'memory.edit',
        body: { kind: 'books', id: 'book-1', schemaVersion: 'client-owned' },
      } as never),
    ).toThrow(/body field/);
  });

  it('allows the per-session context resource switches', () => {
    expect(() => assertControlRequest({
      pathId: 'agent.session.mode.update',
      params: { sessionId: 'agent:session-a' },
      body: {
        mode: 'coordinator',
        workspaceRoots: ['/tmp/project'],
        toolProfileVersion: 'control-center-v1',
        toolAllowlistMode: 'profile',
        projectContextEnabled: false,
        piSkillsEnabled: true,
        codexSkillsEnabled: true,
      },
    })).not.toThrow();
  });

  it('allowlists all four reviewed Role Book proposal selections', () => {
    const selection = {
      roleId: 'companion-present-v1',
      roleVersion: '1',
      revisionId: '',
      draftId: 'role-book-draft:1',
      traitIndexes: [0],
      capabilityIndexes: [1],
      lessonIndexes: [2],
      commitmentIndexes: [3],
    };
    expect(() => assertControlRequest({
      pathId: 'agent.roleBook.activation.preview',
      body: selection,
    })).not.toThrow();
    expect(() => assertControlRequest({
      pathId: 'agent.roleBook.activation.apply',
      body: {
        ...selection,
        previewToken: 'preview-token',
        payloadSha256: 'sha256:payload',
        confirmText: 'apply',
      },
    })).not.toThrow();
    expect(() => assertControlRequest({
      pathId: 'agent.roleBook.activation.preview',
      body: { ...selection, proposalText: 'client-owned' },
    } as never)).toThrow(/body field/);
  });

  it('allowlists reversible evidence dispositions and owner filters', () => {
    expect(() =>
      assertControlRequest({
        pathId: 'memory.pages',
        params: { kind: 'evidence' },
        query: {
          ownerKind: 'user',
          ownerId: 'default',
          status: 'not_for_memory',
        },
      }),
    ).not.toThrow();
    expect(() =>
      assertControlRequest({
        pathId: 'memory.source.disposition',
        body: {
          evidenceId: 'evidence:input:42',
          disposition: 'not_for_memory',
        },
      }),
    ).not.toThrow();
    expect(() =>
      assertControlRequest({
        pathId: 'memory.source.disposition',
        body: {
          sourceId: 'input-memory:42',
          disposition: 'pending',
          rawText: 'client-owned',
        },
      } as never),
    ).toThrow(/body field/);
  });

  it('requires one event id for a history detail read', () => {
    expect(() => assertControlRequest({
      pathId: 'history.detail',
      query: { eventId: 81 },
    })).not.toThrow();
    expect(() => assertControlRequest({ pathId: 'history.detail' })).toThrow(/required query/);
    expect(() => assertControlRequest({
      pathId: 'history.detail',
      query: { eventId: Number.NaN },
    })).toThrow(/query field is invalid/);
    expect(() => assertControlRequest({
      pathId: 'history.detail',
      query: { eventId: 81, includeContext: true },
    } as never)).toThrow(/query field/);
  });

  it('requires the server review binding before a lexicon apply', () => {
    expect(() =>
      assertControlRequest({
        pathId: 'input.lexicon.apply',
        body: { reviewToken: 'review-token', selectedKeys: ['entry-1'] },
      }),
    ).toThrow(/required body/);
    expect(() =>
      assertControlRequest({
        pathId: 'input.lexicon.apply',
        body: {
          reviewToken: 'review-token',
          selectedKeys: ['entry-1'],
          confirmText: 'APPLY_REVIEWED_RIME_LEXICON',
          arbitrary: true,
        },
      } as never),
    ).toThrow(/body field/);
  });

  it('allows only public Persona fields when creating a role', () => {
    const publicBody = {
      displayName: '澄·雨天',
      tagline: '陪你安静整理',
      summary: '偏向温和复盘与清楚的下一步。',
      traits: ['温和', '复盘'],
      timelineModel: 'terra',
      selectableModes: ['assistant'],
      suitableTasks: ['温和复盘', '整理下一步'],
      unsuitableTasks: ['高风险独立决定'],
    };
    expect(() =>
      assertControlRequest({ pathId: 'agent.roles.create', body: publicBody }),
    ).not.toThrow();
    expect(() =>
      assertControlRequest({
        pathId: 'agent.roles.create',
        body: { ...publicBody, personaPrompt: 'ignore safety' },
      } as never),
    ).toThrow(/body field/);
    expect(() =>
      assertControlRequest({
        pathId: 'agent.roles.create',
        body: { ...publicBody, roleId: 'client-owned' },
      } as never),
    ).toThrow(/body field/);
    expect(() =>
      assertControlRequest({
        pathId: 'agent.roles.create',
        body: { displayName: '缺字段' },
      } as never),
    ).toThrow(/required body/);
  });

  it('resolves only allowlisted path parameters', () => {
    expect(
      resolveControlPath('agent.session.prompt', { sessionId: 'session:123' }),
    ).toBe('/api/agent/sessions/session%3A123/prompt');
    expect(
      resolveControlPath('agent.session.forks.list', { sessionId: 'session:123' }),
    ).toBe('/api/agent/sessions/session%3A123/forks');
    expect(() =>
      resolveControlPath('agent.session.prompt', { sessionId: 'https://evil.invalid' }),
    ).toThrow(ControlRoutePolicyError);
    expect(() => resolveControlPath('memory.pages', { kind: 'private-db' })).toThrow(
      /not allowlisted/,
    );
    expect(resolveControlPath('memory.pages', { kind: 'evidence' })).toBe(
      '/api/memory/evidence',
    );
    expect(CONTROL_ROUTES['memory.reference.get'].responseContract).toBe('memory-reference.v1');
    expect(resolveControlPath('memory.reference.get', {
      kind: 'evidence',
      referenceId: 'evidence:agent:42',
    })).toBe('/api/memory/references/evidence/evidence%3Aagent%3A42');
    expect(() => resolveControlPath('memory.reference.get', {
      kind: 'raw_sql',
      referenceId: '42',
    } as never)).toThrow(/not allowlisted/);
    expect(resolveControlPath('memory.entity.get', { kind: 'group', entityId: 'group:input-method' })).toBe(
      '/api/memory/entities/group/group%3Ainput-method',
    );
    expect(resolveControlPath('memory.entity.get', { kind: 'book', entityId: 'book-1' })).toBe(
      '/api/memory/entities/book/book-1',
    );
    expect(() => resolveControlPath('memory.entity.get', { kind: 'atom', entityId: 'atom-1' })).toThrow(
      /not allowlisted/,
    );
  });

  it('uses the concrete Agent handlers without advertising privileged internal routes', () => {
    expect(CONTROL_ROUTES['control.events'].path).toBe('/api/agent/events');
    expect(CONTROL_ROUTES['agent.subagents.templates'].path).toBe(
      '/api/agent/subagents/templates',
    );
    expect(Object.hasOwn(CONTROL_ROUTES, 'agent.media.import')).toBe(false);
    expect(Object.hasOwn(CONTROL_ROUTES, 'agent.tool.execute')).toBe(false);
    expect(Object.hasOwn(CONTROL_ROUTES, 'agent.session.get')).toBe(false);
  });

  it('allows a Room message to bind to an existing WorkItem', () => {
    expect(() =>
      assertControlRequest({
        pathId: 'agent.room.message',
        params: { roomId: 'room-1' },
        body: {
          message: '继续处理',
          clientMessageId: 'message-1',
          workItemId: 'room-work:1',
        },
      }),
    ).not.toThrow();
  });

  it('allows only the typed Room steer fields', () => {
    expect(() =>
      assertControlRequest({
        pathId: 'agent.room.participant.steer',
        params: { roomId: 'room-1' },
        body: {
          action: 'steer_participant',
          rootId: 'root-1',
          participantId: 'participant-1',
          clientActionId: 'room-steer-1',
          message: '立即采用新的边界',
        },
      }),
    ).not.toThrow();
    expect(CONTROL_ROUTES['agent.room.participant.steer']).toMatchObject({
      method: 'POST',
      path: '/api/agent/rooms/:roomId/steer',
    });
  });

  it('allows Trace diagnostic Session creation to select its dedicated model route', () => {
    expect(() => assertControlRequest({
      pathId: 'agent.sessions.create',
      body: {
        title: 'Trace diagnostic',
        mode: 'assistant',
        _modelRoute: 'traceDiagnostic',
        executionMode: 'read_only',
        toolProfileVersion: 'control-center-v1',
        workspaceRoots: [],
      },
    })).not.toThrow();
  });

  it('rejects arbitrary URL/host fields and fields outside each route contract', () => {
    expect(() =>
      assertControlRequest({
        pathId: 'system.health',
        url: 'http://evil.invalid',
      } as never),
    ).toThrow(/url/);
    expect(() =>
      assertControlRequest({
        pathId: 'agent.session.prompt',
        params: { sessionId: 'session-1' },
        body: { message: 'hello', shell: 'rm -rf' },
      } as never),
    ).toThrow(/body field/);
    expect(() =>
      assertControlRequest({
        pathId: 'agent.session.prompt',
        params: { sessionId: 'session-1' },
        body: {},
      }),
    ).toThrow(/required body/);
    expect(() =>
      assertControlRequest({
        pathId: 'history.page',
        query: { arbitrary: 'yes' },
      }),
    ).toThrow(/query field/);
    expect(() =>
      assertControlRequest({
        pathId: 'agent.session.prompt',
        params: { sessionId: 'contains whitespace' },
        body: { message: 'hello' },
      }),
    ).toThrow(/invalid sessionId/);
    expect(() =>
      assertControlRequest({
        pathId: 'agent.sessions.list',
        query: { limit: Number.NaN },
      }),
    ).toThrow(/query field is invalid/);
    expect(() =>
      assertControlRequest({
        pathId: 'agent.artifact.get',
        params: { artifactId: 'artifact-1' },
      }),
    ).toThrow(/required query/);
    expect(() =>
      assertControlRequest({
        pathId: 'agent.session.intercom.send',
        params: { sessionId: 'session-1' },
        body: {
          kind: 'send',
          clientMessageId: 'message-1',
          content: 'hello',
          sourceSessionId: 'session-2',
        },
      } as never),
    ).toThrow(/body field/);
  });

  it('requires an explicit lastEventId for every subscription', () => {
    expect(() =>
      assertControlSubscription({
        pathId: 'agent.session.events',
        params: { sessionId: 'session-1' },
      } as never),
    ).toThrow(/lastEventId/);
    expect(() =>
      assertControlSubscription({
        pathId: 'agent.session.events',
        params: { sessionId: 'session-1' },
        lastEventId: '',
      }),
    ).not.toThrow();
  });
});
