import {
  MAX_COMPOSER_ATTACHMENT_BYTES,
  normalizeComposerAttachmentMimeType,
} from '@/contracts/attachment-policy';
import { CONTROL_ROUTES, controlRoute, type ControlPathId } from '@/platform/routes';
import type {
  AgentImagePasteOptions,
  ControlEventObserver,
  ControlRequest,
  ControlSubscription,
  ControlTransport,
  PickedFile,
} from '@/platform/transport';
import { MockControlTransport, type MockRouteHandler } from '@/test/mock-transport';
import {
  previewAgentEvents,
  previewAgentSnapshot,
  previewBackgroundJobs,
  previewModelCatalog,
  previewPersonas,
  previewTemplates,
  PREVIEW_REPORT_HTML,
} from '@/features/agent/preview-data';
import type { AgentBackgroundJobV1 } from '@/contracts/generated/agent-background-job.v1';
import type { AgentApprovalV1 } from '@/contracts/generated/agent-approval.v1';
import type { AgentPersonaV1 } from '@/contracts/generated/agent-persona.v1';
import type { AgentWorkflowStateV1 } from '@/contracts/generated/agent-workflow-state.v1';
import {
  defaultRoomPermissionPolicy,
  parseRoomPermissionPolicy,
} from '@/features/rooms/room-types';
import { createPreviewHistoryRoutes } from './preview-history-routes';
import { createPreviewWorkDocumentRoutes } from './preview-work-document-routes';
import {
  previewActivityTimeline,
  previewActivityTimelineCalendar,
  previewMemoryApplyPreview,
  previewMemoryCurationRun,
  previewMemoryCurationStatus,
  previewMemoryEntity,
  previewMemoryGraph,
  previewMemoryPage,
  previewMemoryReference,
  previewMemorySummary,
  previewMemoryWorkReceipt,
  previewTimelineDate,
} from './preview-memory-data';
import {
  previewRoomSnapshot,
} from './preview-room-data';
import {
  applyPreviewConfigurationChanges,
  previewConfigurationSchema,
  previewConfigurationSettings,
  previewConfigurationValues,
  previewLexiconReview,
} from './preview-input-data';
import { previewEvalLabEvidence, previewEvalLabRuns } from './preview-eval-lab-data';

/**
 * The mock transport has one broadcast event bus for convenience. Preview
 * sessions still need the production ownership boundary: an event emitted by
 * one Session must never hydrate another Session's reducer. Keep that rule at
 * the preview transport seam so fixtures exercise the same scoped behavior as
 * the native transport.
 */
class PreviewControlTransport extends MockControlTransport {
  override subscribe<Event = unknown>(
    request: ControlSubscription,
    observer: ControlEventObserver<Event>,
  ): () => void {
    const sessionId = request.pathId === 'agent.session.events'
      ? stringValue(request.params?.sessionId)
      : '';
    if (!sessionId) return super.subscribe(request, observer);

    const scopedObserver: ControlEventObserver<Event> = {
      ...observer,
      next: (event) => {
        if (eventSessionId(event) === sessionId) observer.next(event);
      },
      ...(observer.snapshotRequired
        ? {
            snapshotRequired: (event: Event) => {
              if (eventSessionId(event) === sessionId) observer.snapshotRequired?.(event);
            },
          }
        : {}),
    };
    const unsubscribe = super.subscribe(request, scopedObserver);

    // The states fixture is intentionally empty at sequence zero so the UI
    // can demonstrate a genuine running/failed/aborted event stream. Replay
    // only events newer than the caller's cursor and only for this Session.
    if (sessionId === 'session-states') {
      const lastSequence = resumeSequence(request.lastEventId, sessionId);
      queueMicrotask(() => {
        for (const event of previewAgentEvents(sessionId)) {
          if (event.sequence > lastSequence) this.emit('agent.session.events', event);
        }
      });
    }
    return unsubscribe;
  }
}

export function createPreviewTransport(): MockControlTransport {
  let nextSessionId = 1;
  let nextRoleId = 1;
  let nextWakeScheduleId = 1;
  let nextRoomId = 1;
  const previewRoomSnapshots = new Map<string, Record<string, unknown>>([
    ['room-preview', previewRoomSnapshot('room-preview') as unknown as Record<string, unknown>],
  ]);
  let previewEvidenceDisposition = 'not_for_memory';
  let previewMemoryRunStatus = 'draft';
  let previewMemoryJob: Record<string, unknown> = {};
  let previewWorkflow = previewWorkflowState('session-preview');
  let previewInstalledExtensions = previewInstalledExtensionItems();
  let previewSkills = previewSkillItems();
  let previewValidatedExtension: Record<string, unknown> = {};
  let previewExtensionChange: Record<string, unknown> = {};
  let previewLifecyclePolicies = previewLifecyclePolicyItems();
  let previewApprovals = previewApprovalItems();
  const previewTimelineStatuses = new Map<string, string>([
    [new Date().toISOString().slice(0, 10), 'draft'],
  ]);
  const previewMemorySelections = new Map<number, boolean>([[1, true], [2, false], [3, true]]);
  let previewConfiguration = previewConfigurationValues();
  let previousPreviewConfiguration: Record<string, unknown> | undefined;
  let previewConfigurationRevision = 12;
  let previewLexiconRollbackId = '';
  let nextKnowledgeBaseId = 1;
  let nextKnowledgeJobId = 1;
  let nextTerminalId = 1;
  let previewTerminals: Record<string, unknown>[] = [];
  const previewTerminalOutput = new Map<string, string>();
  // Keep the preview aligned with the two corpora used in the Knowledge
  // acceptance pass. These are local fixtures: they make the library switcher
  // exercise the same multi-base path as the real service without pretending
  // that an external Google Earth or paper service is connected.
  let previewKnowledgeBases = previewKnowledgeBasesFixture();
  const previewKnowledgeDocuments: Record<string, unknown>[] = [{
    id: 'file:preview-yuxi',
    baseId: 'kb:preview-project-docs',
    fileName: '伙伴运行笔记.md',
    mimeType: 'text/markdown',
    byteSize: 48_320,
    status: 'ready',
    stage: 'ready',
    chunkCount: 36,
    parserProvider: 'builtin',
    revision: 1,
    updatedAtMs: Date.now() - 180_000,
  }, {
    id: 'file:preview-google-earth',
    baseId: 'kb:preview-google-earth',
    fileName: 'Google Earth 地理资料.md',
    mimeType: 'text/markdown',
    byteSize: 72_640,
    status: 'ready',
    stage: 'ready',
    chunkCount: 28,
    parserProvider: 'builtin',
    revision: 1,
    updatedAtMs: Date.now() - 210_000,
  }, {
    id: 'file:preview-antarctic-papers',
    baseId: 'kb:preview-antarctic-papers',
    fileName: 'Zotero 南极论文库（198 个 PDF）.manifest.md',
    mimeType: 'text/markdown',
    byteSize: 198,
    status: 'queued',
    stage: 'queued',
    chunkCount: 0,
    parserProvider: 'auto',
    revision: 1,
    updatedAtMs: Date.now() - 240_000,
  }];
  let previewKnowledgeJobs: Record<string, unknown>[] = [];
  let previewTransport: MockControlTransport | undefined;
  const previewBackgroundJobsBySession: Record<string, AgentBackgroundJobV1[]> = {
    'session-states': previewBackgroundJobs('session-states'),
  };
  const sessions: Record<string, unknown>[] = [
    previewSession('session-preview', '控制中心迁移', 'companion-present-v1', Date.now(), '1', { messageCount: 4, lastMessagePreview: '三条工作线已经收束到同一个控制入口。', workspaceRoots: ['/Users/example/Projects/personal-agent-workbench'] }),
    previewSession('session-input', '等待你的回答', 'companion-present-v1', Date.now() - 30_000, '1', { messageCount: 1, lastMessagePreview: '请把待审问答做成更清楚的协作界面。', workspaceRoots: ['/Users/example/Projects/personal-agent-workbench'] }),
    // Genuinely empty so the preview can exercise the welcome state.
    previewSession('session-fresh', '新对话', 'companion-present-v1', Date.now() - 120_000),
    // Long transcript: gives the timeline a real scroll extent so follow and
    // read-up preservation can be measured, not assumed.
    previewSession('session-long', '长对话回归', 'companion-present-v1', Date.now() - 300_000, '1', { messageCount: 120, lastMessagePreview: '第 60 轮结论：继续向后一段推进。' }),
    previewSession('session-gallery', '渲染族样例', 'companion-present-v1', Date.now() - 240_000, '1', { messageCount: 6, lastMessagePreview: '完整渲染族样例，用于视觉评审。' }),
    previewSession('session-states', '状态与恢复', 'companion-present-v1', Date.now() - 120_000, '1', { messageCount: 3, lastMessagePreview: '运行中、失败与已停止三种状态。' }),
    previewSession('session-report', '报告交付', 'companion-present-v1', Date.now() - 240_000, '1', { messageCount: 2, lastMessagePreview: '生成的 HTML 报告与原始数据一并交付。' }),
    previewSession('session-models', '模型切换', 'companion-present-v1', Date.now() - 180_000, '1', { messageCount: 4, lastMessagePreview: '同一串对话里从快模型换到强模型。' }),
    previewSession('session-memory', '记忆整理', 'companion-present-v1', Date.now() - 360_000, '1', { messageCount: 12, lastMessagePreview: '已把最近输入整理为 3 个主题。' }),
    previewSession('session-work-disclosure', '过程折叠验收', 'companion-present-v1', Date.now() - 90_000, '1', { messageCount: 3, lastMessagePreview: '最终结果保持可见，推理与工具过程可按需展开。', workspaceRoots: ['/Users/example/Projects/personal-agent-workbench'] }),
  ];
  const roomSessions: Record<string, unknown>[] = [
    previewRoomSession('session-room-present', '迁移作战室 · Earth', 'companion-present-v1', 'participant-present'),
    previewRoomSession('session-room-firstlight', '迁移作战室 · Mars', 'companion-firstlight-v1', 'participant-firstlight'),
    previewRoomSession('session-room-future', '迁移作战室 · Venus', 'companion-future-v1', 'participant-future'),
  ];
  let personas: AgentPersonaV1[] = previewPersonas.map((persona) => ({
    ...persona,
    defaults: { ...persona.defaults },
    runtimeCharacteristics: { ...persona.runtimeCharacteristics },
  }));
  let companionConfigurationRevision = 1;
  let modelRouting = previewDefaultModelRouting();
  let skillRouting = previewDefaultSkillRouting();
  let capabilityGlobalPreferences: Record<string, string> = {};
  let capabilityProjectPreferences: Record<string, Record<string, string>> = {};
  const capabilitySessionPreferences = new Map<string, Record<string, string>>();
  const previewModelCatalogs = new Map<string, ReturnType<typeof previewModelCatalog>>();
  let defaultCompanion = {
    roleId: personas.find((persona) => persona.runtimeCharacteristics.isDefault)?.roleId ?? personas[0]?.roleId ?? '',
    roleVersion: '1',
  };
  const wakeSchedules: Record<string, unknown>[] = [];
  let previewEvalRunsByTrace: Record<string, Record<string, unknown>[]> = {};
  let previewEvalSchedules: Record<string, unknown>[] = [
    previewEvalSchedule('eval-schedule:sgg'),
  ];
  let nextPreviewEvalScheduleId = 1;
  const previewEvalScheduleRuns = new Map<string, Record<string, unknown>[]>([
    [
      'eval-schedule:sgg',
      [previewEvalScheduleRun('eval-schedule:sgg', 'eval:sgg:preview', 'trace:turn:preview')],
    ],
  ]);
  const routes = Object.fromEntries(
    (Object.keys(CONTROL_ROUTES) as ControlPathId[])
      .filter((pathId) => !controlRoute(pathId).subscription)
      .map((pathId) => [pathId, previewResponse(pathId)]),
  ) as Partial<Record<ControlPathId, MockRouteHandler>>;
  routes['agent.session.snapshot'] = (request: ControlRequest) => (
    previewAgentSnapshot(stringValue(record(request.params).sessionId) || 'session-preview')
  );
  routes['observability.trace.get'] = (request: ControlRequest) => previewCanonicalTrace(
    stringValue(record(request.params).traceId) || 'trace:turn:preview',
  );
  routes['observability.sandboxRuns.list'] = () => ({
    schemaVersion: 'rag-ime.observability-sandbox-run-list.v1',
    ok: true,
    items: [{
      schemaVersion: 'rag-ime.sandbox-run.v1',
      sandboxRunId: 'sandbox:sgg:preview',
      appId: 'sgg',
      status: 'completed',
      policy: {
        workspaceBindingId: 'workspace-binding:vertical-sandbox:sgg',
        workspaceFingerprint: `sha256:${'0'.repeat(64)}`,
        mutationMode: 'read_only',
        network: 'blocked',
        productionWriteBlocked: true,
      },
      traceIds: ['trace:turn:preview'],
      evalRunIds: ['eval:sgg:preview'],
      createdAtMs: 1_800_000_000_000,
      updatedAtMs: 1_800_000_000_100,
    }],
    total: 1,
  });
  routes['observability.evals.list'] = (request: ControlRequest) => {
    const traceId = stringValue(record(request.query).traceId) || 'trace:turn:preview';
    return previewEvalList(traceId, previewEvalRunsByTrace[traceId] ?? []);
  };
  routes['observability.evals.evidence.run'] = (request: ControlRequest) => {
    const body = record(request.body);
    const traceId = stringValue(body.traceId) || 'trace:turn:preview';
    const evalRun = previewEvidenceEvalRun(
      body,
      traceId,
      previewEvalRunsByTrace[traceId]?.length ?? 0,
    );
    const summary = previewEvalSummary(evalRun);
    previewEvalRunsByTrace = {
      ...previewEvalRunsByTrace,
      [traceId]: [...(previewEvalRunsByTrace[traceId] ?? []), summary],
    };
    return evalRun;
  };
  routes['observability.evalSuites.list'] = () => previewEvalSuiteList();
  routes['observability.evalSchedules.list'] = () => ({
    schemaVersion: 'rag-ime.eval-schedule-list.v1',
    ok: true,
    items: previewEvalSchedules,
  });
  routes['observability.evalSchedule.runs'] = (request: ControlRequest) => {
    const scheduleId = stringValue(record(request.params).scheduleId);
    const schedule = previewEvalSchedules.find((item) => stringValue(item.id) === scheduleId)
      ?? previewEvalSchedules[0];
    if (!schedule) throw new Error('Preview Eval schedule is unavailable.');
    return {
      schemaVersion: 'rag-ime.eval-schedule-run-list.v1',
      ok: true,
      schedule,
      items: previewEvalScheduleRuns.get(stringValue(schedule.id)) ?? [],
    };
  };
  routes['observability.evalSchedules.create'] = (request: ControlRequest) => {
    const body = record(request.body);
    const suiteId = stringValue(body.suiteId) || 'sgg';
    const suiteRevision = stringValue(body.suiteRevision) || 'fixture-v2';
    const scheduleId = stringValue(body.scheduleId)
      || `eval-schedule:${suiteId}:${nextPreviewEvalScheduleId++}`;
    const now = Date.now();
    const requestedNextDueAtMs = Number(body.nextDueAtMs);
    const nextDueAtMs = Number.isFinite(requestedNextDueAtMs)
      ? requestedNextDueAtMs
      : now + 60_000;
    const maxRuns = Number(body.maxRuns) || 30;
    const oneShot = maxRuns === 1 && nextDueAtMs <= now + 1_500;
    const runTraceId = suiteId === 'zhanggui-wenshu'
      ? 'trace:room-turn:preview'
      : 'trace:turn:preview';
    const completedRun = oneShot
      ? previewEvalScheduleRun(scheduleId, `eval:${suiteId}:preview:one-shot`, runTraceId)
      : undefined;
    const schedule = previewEvalSchedule(scheduleId, {
      suiteId,
      suiteRevision,
      recurrenceKind: stringValue(body.recurrenceKind) === 'weekly' ? 'weekly' : 'daily',
      recurrenceInterval: Number(body.recurrenceInterval) || 1,
      maxRuns,
      runCount: oneShot ? 1 : 0,
      status: oneShot ? 'completed' : 'scheduled',
      initialDueAtMs: nextDueAtMs,
      nextDueAtMs,
      ...(completedRun ? { latestRun: previewEvalLatestRun(completedRun) } : {}),
      createdAtMs: now,
      updatedAtMs: now,
    });
    previewEvalSchedules = [...previewEvalSchedules, schedule];
    previewEvalScheduleRuns.set(scheduleId, completedRun ? [completedRun] : []);
    if (completedRun) {
      const summary = previewScheduledEvalSummary({
        evalRunId: stringValue(completedRun.evalRunId),
        suiteId,
        suiteRevision,
      });
      previewEvalRunsByTrace = {
        ...previewEvalRunsByTrace,
        [runTraceId]: [...(previewEvalRunsByTrace[runTraceId] ?? []), summary],
      };
    }
    return {
      schemaVersion: 'rag-ime.eval-schedule-create.v1',
      ok: true,
      schedule,
    };
  };
  routes['terminal.sessions.list'] = () => ({ schemaVersion: 'rag-ime.system-terminal.v1', ok: true, items: previewTerminals });
  routes['terminal.session.create'] = (request: ControlRequest) => {
    const body = record(request.body);
    const terminalId = `term_preview_${nextTerminalId++}`;
    const terminal = {
      terminalId,
      title: stringValue(body.title) || 'System Terminal',
      cwd: stringValue(body.cwd) || '/Users/example/Projects/personal-agent-workbench',
      shell: '/bin/zsh', pid: 42000 + nextTerminalId, cols: Number(body.cols) || 104,
      rows: Number(body.rows) || 30, status: 'running', exitCode: null, baseCursor: 0,
      nextCursor: 0, createdAtMs: Date.now(),
    };
    previewTerminals = [...previewTerminals, terminal];
    previewTerminalOutput.set(terminalId, 'PAWOS system terminal\n❯ ');
    return { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal };
  };
  routes['terminal.session.read'] = (request: ControlRequest) => {
    const body = record(request.body);
    const terminalId = stringValue(body.terminalId);
    const terminal = previewTerminals.find((item) => stringValue(item.terminalId) === terminalId);
    if (!terminal) throw new Error('terminal session was not found');
    const text = previewTerminalOutput.get(terminalId) || '';
    const cursor = Math.max(0, Number(body.cursor) || 0);
    return { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, terminal, cursor, nextCursor: text.length, truncated: false, text: text.slice(cursor) };
  };
  routes['terminal.session.write'] = (request: ControlRequest) => {
    const body = record(request.body);
    const terminalId = stringValue(body.terminalId);
    const text = stringValue(body.text);
    if (!previewTerminalOutput.has(terminalId)) throw new Error('terminal session was not found');
    previewTerminalOutput.set(terminalId, `${previewTerminalOutput.get(terminalId) || ''}${text}preview: command accepted\n❯ `);
    return { schemaVersion: 'rag-ime.system-terminal.v1', ok: true, writtenBytes: text.length };
  };
  routes['terminal.session.resize'] = () => ({ schemaVersion: 'rag-ime.system-terminal.v1', ok: true });
  routes['terminal.session.close'] = (request: ControlRequest) => {
    const terminalId = stringValue(record(request.body).terminalId);
    previewTerminals = previewTerminals.map((item) => stringValue(item.terminalId) === terminalId ? { ...item, status: 'closed', exitCode: 0 } : item);
    return { schemaVersion: 'rag-ime.system-terminal.v1', ok: true };
  };
  routes['agent.session.models'] = (request: ControlRequest) => {
    const sessionId = stringValue(record(request.params).sessionId) || 'session-preview';
    const current = previewModelCatalogs.get(sessionId) ?? previewModelCatalog(sessionId);
    previewModelCatalogs.set(sessionId, current);
    return current;
  };
  routes['agent.session.model.select'] = (request: ControlRequest) => {
    const sessionId = stringValue(record(request.params).sessionId) || 'session-preview';
    const body = record(request.body);
    const providerId = stringValue(body.provider);
    const modelId = stringValue(body.modelId);
    const current = previewModelCatalogs.get(sessionId) ?? previewModelCatalog(sessionId);
    const model = current.providers
      .find((provider) => provider.id === providerId)
      ?.models.find((item) => item.id === modelId);
    if (!model) throw new Error('Preview model is unavailable.');
    previewModelCatalogs.set(sessionId, {
      ...current,
      selected: {
        ...record(current.selected),
        provider: providerId,
        id: modelId,
        modelId,
        name: model.name,
      },
    });
    return { ok: true };
  };
  routes['agent.session.thinking.select'] = (request: ControlRequest) => {
    const sessionId = stringValue(record(request.params).sessionId) || 'session-preview';
    const level = stringValue(record(request.body).level);
    const current = previewModelCatalogs.get(sessionId) ?? previewModelCatalog(sessionId);
    const selected = record(current.selected);
    const providerId = stringValue(selected.provider);
    const modelId = stringValue(selected.id) || stringValue(selected.modelId);
    const model = current.providers
      .find((provider) => provider.id === providerId)
      ?.models.find((item) => item.id === modelId);
    if (!model?.thinkingLevels.some((item) => item === level)) {
      throw new Error('Preview thinking level is unavailable.');
    }
    previewModelCatalogs.set(sessionId, {
      ...current,
      thinkingLevel: level as typeof current.thinkingLevel,
    });
    return { ok: true };
  };
  routes['agent.session.workflow.get'] = (request: ControlRequest) => {
    previewWorkflow = withPreviewWorkflowSession(
      previewWorkflow,
      stringValue(record(request.params).sessionId) || 'session-preview',
    );
    return previewWorkflow;
  };
  routes['agent.session.goal.mutate'] = (request: ControlRequest) => {
    previewWorkflow = mutatePreviewGoal(
      withPreviewWorkflowSession(
        previewWorkflow,
        stringValue(record(request.params).sessionId) || 'session-preview',
      ),
      record(request.body),
    );
    return previewWorkflow;
  };
  routes['agent.extensions.list'] = () => ({ ok: true, items: previewInstalledExtensions });
  routes['agent.extensions.skills.list'] = () => ({
    schemaVersion: 'rag-ime.skill-inventory.v1',
    ok: true,
    runtimeAvailable: true,
    revision: `sha256:${'e'.repeat(64)}`,
    items: previewSkills,
  });
  routes['agent.extensions.skills.get'] = (request: ControlRequest) => {
    const skillId = stringValue(record(request.query).skillId);
    const item = previewSkills.find((candidate) => stringValue(candidate.skillId) === skillId);
    if (!item) throw new Error('Skill 不存在或已经释放。');
    const body = previewSkillBodies[skillId] || `# ${stringValue(item.name)}\n\n${stringValue(item.description)}\n`;
    const bodyBytes = new TextEncoder().encode(body).byteLength;
    return {
      schemaVersion: 'rag-ime.skill-detail.v1',
      ok: true,
      revision: stringValue(item.contentRevision),
      item: {
        ...item,
        body,
        bodyBytes,
        contentBytes: bodyBytes,
        bodyTruncated: false,
      },
    };
  };
  routes['agent.extensions.catalog'] = () => ({
    ok: true,
    items: previewExtensionCatalogItems(previewInstalledExtensions),
  });
  routes['agent.extensions.proposals'] = () => ({
    ok: true,
    items: [previewExtensionProposal()],
  });
  routes['agent.extensions.validate'] = (request: ControlRequest) => {
    const body = record(request.body);
    const packageSource = stringValue(body.packageSource);
    const packageIdentity = previewPiPackageIdentity(packageSource);
    const pluginId = stringValue(body.catalogId) || packageIdentity.id || 'session-review';
    previewValidatedExtension = {
      id: pluginId,
      displayName: packageIdentity.displayName
        || (pluginId === 'session-review' ? 'Session Review' : pluginId),
      version: stringValue(body.catalogVersion) || packageIdentity.version || '1.1.0',
      totalBytes: 18_432,
      permissions: [],
      resources: packageSource
        ? { extensions: [], skills: [`skills/${pluginId}/SKILL.md`], prompts: [], themes: [] }
        : { extensions: [], skills: ['skills/session-review/SKILL.md'], prompts: [], themes: [] },
      source: packageSource
        ? { kind: packageIdentity.kind, requested: packageSource, resolved: packageSource }
        : { kind: 'bundled', requested: pluginId, resolved: pluginId },
    };
    return {
      ok: true,
      validationToken: `validation:${pluginId}:preview`,
      distribution: packageSource ? 'pi_package' : 'bundled',
      extension: previewValidatedExtension,
    };
  };
  routes['agent.extensions.preview'] = (request: ControlRequest) => {
    const body = record(request.body);
    const action = stringValue(body.action) || 'install';
    const validationToken = stringValue(body.validationToken);
    const tokenPluginId = validationToken.split(':')[1] || '';
    const pluginId = stringValue(body.pluginId) || tokenPluginId || 'session-review';
    const installedExtension = previewInstalledExtensions.find(
      (item) => stringValue(item.id) === pluginId,
    );
    if (action === 'rollback' && installedExtension?.rollbackAvailable !== true) {
      throw new Error('这个扩展当前没有可恢复的上一版本。');
    }
    const rollbackVersion = action === 'rollback'
      ? stringValue(installedExtension?.previousVersion)
      : '';
    if (action === 'rollback' && !rollbackVersion) {
      throw new Error('这个扩展缺少可核验的上一版本，未创建恢复预览。');
    }
    previewExtensionChange = {
      action,
      pluginId,
      displayName: stringValue(installedExtension?.displayName)
        || stringValue(previewValidatedExtension.displayName)
        || (pluginId === 'session-review' ? 'Session Review' : pluginId),
      enable: body.enable !== false,
      version: stringValue(previewValidatedExtension.version) || stringValue(installedExtension?.version),
      permissions: previewValidatedExtension.permissions ?? installedExtension?.permissions ?? [],
      resources: previewValidatedExtension.resources ?? installedExtension?.resources ?? {},
      source: previewValidatedExtension.source ?? installedExtension?.source ?? {},
      ...(rollbackVersion ? { version: rollbackVersion } : {}),
    };
    return {
      ok: true,
      previewToken: `preview:${action}:${pluginId}`,
      payloadSha256: 'c'.repeat(64),
      summary: previewExtensionChange,
    };
  };
  routes['agent.extensions.apply'] = (request: ControlRequest) => {
    if (!stringValue(previewExtensionChange.pluginId)
      && stringValue(record(request.body).previewToken) === 'proposal-preview-token') {
      previewExtensionChange = {
        action: 'install',
        pluginId: 'session-review',
        displayName: 'Session Review',
        enable: true,
      };
    }
    previewInstalledExtensions = applyPreviewExtensionChange(
      previewInstalledExtensions,
      previewExtensionChange,
    );
    previewSkills = applyPreviewSkillChange(previewSkills, previewExtensionChange);
    return {
      ok: true,
      receipt: {
        receiptId: `plugin:${stringValue(previewExtensionChange.action) || 'apply'}:preview`,
      },
    };
  };
  routes['agent.approvals.list'] = (request: ControlRequest) => {
    const query = record(request.query);
    const sessionId = stringValue(query.sessionId);
    const state = stringValue(query.state);
    const limit = Math.max(1, Math.min(500, Number(query.limit) || 100));
    return {
      ok: true,
      items: previewApprovals
        .filter((item) => !sessionId || item.sessionId === sessionId)
        .filter((item) => !state || item.state === state)
        .slice(0, limit),
    };
  };
  routes['agent.approval.get'] = (request: ControlRequest) => {
    const approvalId = stringValue(record(request.params).approvalId);
    const approval = previewApprovals.find((item) => item.approvalId === approvalId);
    if (!approval) throw new Error('审批请求不存在或已经释放。');
    return { ok: true, approval };
  };
  routes['agent.approval.decide'] = (request: ControlRequest) => {
    const approvalId = stringValue(record(request.params).approvalId);
    const body = record(request.body);
    const decision = stringValue(body.decision);
    const index = previewApprovals.findIndex((item) => item.approvalId === approvalId);
    const approval = previewApprovals[index];
    if (!approval) throw new Error('审批请求不存在或已经释放。');
    if (approval.state !== 'pending') throw new Error('审批请求已经处理，当前状态不能再次修改。');
    if (stringValue(body.payloadSha256) !== approval.payloadSha256) {
      throw new Error('审批内容已经变化，请刷新后重新核对。');
    }
    const updated: AgentApprovalV1 = {
      ...approval,
      state: decision === 'approve' ? 'approved' : 'rejected',
      decidedBy: 'preview-user',
      decidedAtMs: Date.now(),
    };
    previewApprovals = previewApprovals.map((item, itemIndex) => itemIndex === index ? updated : item);
    return { ok: true, approval: updated };
  };
  routes['agent.lifecycleHooks.get'] = () => ({
    ok: true,
    policies: previewLifecyclePolicies,
    recentEvents: previewLifecycleEventItems(),
  });
  routes['agent.lifecycleHooks.update'] = (request: ControlRequest) => {
    const body = record(request.body);
    const eventType = stringValue(body.eventType);
    previewLifecyclePolicies = previewLifecyclePolicies.map((policy) => (
      stringValue(policy.eventType) === eventType
        ? { ...policy, ...(typeof body.enabled === 'boolean' ? { enabled: body.enabled } : {}) }
        : policy
    ));
    return { ok: true, policies: previewLifecyclePolicies };
  };
  routes['agent.subagent.console'] = (request: ControlRequest) =>
    previewSubagentConsole(stringValue(record(request.params).runId));
  routes['agent.subagent.control'] = (request: ControlRequest) => ({
    ok: true,
    replayed: false,
    action: stringValue(record(request.body).action),
    clientActionId: stringValue(record(request.body).clientActionId),
  });
  routes['memory.summary'] = () => previewMemorySummary(previewTimelineStatuses);
  routes['memory.activityTimeline.get'] = (request: ControlRequest) => {
    const date = stringValue(record(request.query).date) || new Date().toISOString().slice(0, 10);
    return {
      ok: true,
      timeline: previewActivityTimeline(date, previewTimelineStatuses.get(date) || 'draft'),
    };
  };
  routes['memory.activityTimeline.calendar'] = (request: ControlRequest) => {
    const month = stringValue(record(request.query).month) || new Date().toISOString().slice(0, 7);
    return previewActivityTimelineCalendar(month, previewTimelineStatuses);
  };
  routes['memory.activityTimeline.build'] = (request: ControlRequest) => {
    const date = stringValue(record(request.body).date) || new Date().toISOString().slice(0, 10);
    previewTimelineStatuses.set(date, 'draft');
    return {
      schemaVersion: 'rag-ime.daily-activity-timeline-build.v1',
      ok: true,
      created: true,
      timeline: previewActivityTimeline(date, 'draft'),
    };
  };
  routes['memory.activityTimeline.approve'] = (request: ControlRequest) => {
    const body = record(request.body);
    const date = previewTimelineDate(stringValue(body.timelineId));
    previewTimelineStatuses.set(date, 'approved');
    return {
      schemaVersion: 'rag-ime.daily-activity-timeline-decision.v1',
      ok: true,
      decision: 'accepted',
      timeline: previewActivityTimeline(date, 'approved'),
    };
  };
  routes['memory.activityTimeline.reject'] = (request: ControlRequest) => {
    const body = record(request.body);
    const date = previewTimelineDate(stringValue(body.timelineId));
    previewTimelineStatuses.set(date, 'rejected');
    return {
      schemaVersion: 'rag-ime.daily-activity-timeline-decision.v1',
      ok: true,
      decision: 'rejected',
      timeline: previewActivityTimeline(date, 'rejected'),
    };
  };
  routes['memory.graph.get'] = (request: ControlRequest) =>
    previewMemoryGraph(stringValue(record(request.query).plane) === 'tags' ? 'tags' : 'groups');
  routes['memory.entity.get'] = (request: ControlRequest) => previewMemoryEntity(
    stringValue(record(request.params).kind),
    stringValue(record(request.params).entityId),
  );
  routes['memory.reference.get'] = (request: ControlRequest) =>
    previewMemoryReference(
      stringValue(record(request.params).kind),
      stringValue(record(request.params).referenceId),
    );
  routes['agent.memoryMaintenance.run'] = (request: ControlRequest) =>
    stringValue(record(request.query).jobId)
      ? previewMemoryJob
      : stringValue(record(request.query).runId)
      ? previewMemoryCurationRun(previewMemorySelections, previewMemoryRunStatus)
      : previewMemoryCurationStatus(previewMemoryRunStatus);
  routes['agent.memoryMaintenance.trigger'] = (request: ControlRequest) => {
    const body = record(request.body);
    const jobId = `memory-maintenance:preview:${Date.now()}`;
    previewMemoryJob = {
      schemaVersion: 'rag-ime.gateway-memory-maintenance-job.v1',
      ok: true,
      jobId,
      state: 'completed',
      reused: false,
      result: {
        ok: true,
        results: [],
        requestedSourceCount: Number(body.maxSources) || 4,
      },
      error: '',
      createdAtMs: Date.now(),
      updatedAtMs: Date.now(),
      completedAtMs: Date.now(),
    };
    return { ...previewMemoryJob, state: 'queued', completedAtMs: 0 };
  };
  routes['knowledge.database.draft.edit'] = (request: ControlRequest) => {
    const body = record(request.body);
    const diffId = Number(body.diffId);
    if (previewMemoryRunStatus !== 'draft' || !previewMemorySelections.has(diffId)) {
      throw new Error('The preview memory draft is no longer editable.');
    }
    previewMemorySelections.set(diffId, body.selected === true);
    return {
      ok: true,
      runId: 'memory_book_preview',
      diffId,
      selected: body.selected === true,
    };
  };
  routes['knowledge.database.apply.preview'] = () => previewMemoryApplyPreview();
  routes['knowledge.database.apply'] = () => {
    previewMemoryRunStatus = 'applied';
    return previewMemoryWorkReceipt('knowledge.database.apply', true);
  };
  routes['knowledge.database.rollback'] = () => {
    previewMemoryRunStatus = 'rolled_back';
    return previewMemoryWorkReceipt('knowledge.database.rollback', false);
  };
  routes['agent.sessions.list'] = (request: ControlRequest) => ({
    ok: true,
    sessions: [
      ...sessions,
      ...roomSessions,
    ].filter((session) => (
      record(request.query).includeArchived === true || stringValue(session.status) !== 'archived'
    )),
  });
  routes['agent.roles.list'] = () => ({ ok: true, roles: personas });
  routes['agent.roles.create'] = (request: ControlRequest) => {
    const role = previewEditablePersona(
      record(request.body),
      personas[0],
      `companion-custom-${nextRoleId++}`,
    );
    personas = [role, ...personas];
    return { ok: true, role };
  };
  routes['agent.roles.update'] = (request: ControlRequest) => {
    const body = record(request.body);
    const roleId = stringValue(body.roleId);
    const index = personas.findIndex((persona) => persona.roleId === roleId && persona.version === (stringValue(body.roleVersion) || '1'));
    if (index < 0 || previewPersonas.some((persona) => persona.roleId === roleId)) throw new Error('Preview built-in companion is read-only.');
    const role = previewEditablePersona(body, personas[index], roleId);
    personas = personas.map((persona, ordinal) => ordinal === index ? role : persona);
    return { ok: true, role };
  };
  routes['agent.roles.archive'] = (request: ControlRequest) => {
    const body = record(request.body);
    const roleId = stringValue(body.roleId);
    if (previewPersonas.some((persona) => persona.roleId === roleId)) throw new Error('Preview built-in companion cannot be removed.');
    personas = personas.filter((persona) => persona.roleId !== roleId || persona.version !== (stringValue(body.roleVersion) || '1'));
    return { ok: true, roleId };
  };
  routes['agent.role.runtimeDefaults.update'] = (request: ControlRequest) => {
    const body = record(request.body);
    const roleId = stringValue(body.roleId);
    const index = personas.findIndex((persona) => persona.roleId === roleId && persona.version === (stringValue(body.roleVersion) || '1'));
    if (index < 0) throw new Error('Preview companion not found.');
    const role = {
      ...personas[index],
      defaults: {
        ...personas[index].defaults,
        modelProfile: `${stringValue(body.provider)}/${stringValue(body.modelId)}`,
        thinkingLevel: previewThinkingLevel(body.thinkingLevel),
      },
    };
    personas = personas.map((persona, ordinal) => ordinal === index ? role : persona);
    return { ok: true, role };
  };
  routes['agent.configuration.get'] = () => previewCompanionConfiguration(
    companionConfigurationRevision,
    defaultCompanion,
    modelRouting,
    skillRouting,
    capabilityGlobalPreferences,
    capabilityProjectPreferences,
  );
  routes['agent.configuration.update'] = (request: ControlRequest) => {
    const body = record(request.body);
    if (Number(body.expectedRevision) !== companionConfigurationRevision) throw new Error('Preview companion configuration changed.');
    const changes = record(body.changes);
    const modelRouteChange = Object.entries(changes).find(([key]) => key.startsWith('modelRouting.'));
    const skillRouteChange = Object.entries(changes).find(([key]) => key.startsWith('skillRouting.'));
    if (modelRouteChange) {
      const routeId = modelRouteChange[0].slice('modelRouting.'.length);
      if (!Object.hasOwn(modelRouting, routeId)) throw new Error('Preview model route is invalid.');
      const route = record(modelRouteChange[1]);
      const modelProfile = stringValue(route.modelProfile);
      const thinkingLevel = stringValue(route.thinkingLevel);
      if (!modelProfile || !thinkingLevel) throw new Error('Preview model route is incomplete.');
      modelRouting = {
        ...modelRouting,
        [routeId]: { modelProfile, thinkingLevel },
      };
    } else if (skillRouteChange) {
      const scenario = skillRouteChange[0].slice('skillRouting.'.length) as PreviewSkillScenario;
      const requestedSkillRoute = skillRouteChange[1];
      if (!Object.hasOwn(skillRouting, scenario)) throw new Error('Preview Skill scenario is invalid.');
      if (
        !Array.isArray(requestedSkillRoute)
        || requestedSkillRoute.some((name) => typeof name !== 'string' || !name.trim())
      ) {
        throw new Error('Preview Skill route is invalid.');
      }
      const names = requestedSkillRoute.map((name) => name.trim());
      if (
        new Set(names).size !== names.length
        || names.some((name) => {
          const owner = PREVIEW_PRIVATE_SKILL_SCENARIOS[name];
          return owner !== undefined && owner !== scenario;
        })
      ) {
        throw new Error('Preview Skill route contains an invalid private Skill.');
      }
      skillRouting = {
        ...skillRouting,
        [scenario]: [...names].sort(),
      };
    } else if (Object.hasOwn(changes, 'sessionDefaults.capabilityDisclosurePreferences')) {
      capabilityGlobalPreferences = previewCapabilityPreferences(changes['sessionDefaults.capabilityDisclosurePreferences']);
    } else if (Object.hasOwn(changes, 'capabilityDisclosure.projectPreferences')) {
      capabilityProjectPreferences = Object.fromEntries(
        Object.entries(record(changes['capabilityDisclosure.projectPreferences']))
          .map(([projectId, preferences]) => [projectId, previewCapabilityPreferences(preferences)]),
      );
    } else {
      const roleId = stringValue(changes['sessionDefaults.roleId']);
      const roleVersion = stringValue(changes['sessionDefaults.roleVersion']) || '1';
      if (!personas.some((persona) => persona.roleId === roleId && persona.version === roleVersion)) throw new Error('Preview companion not found.');
      defaultCompanion = { roleId, roleVersion };
    }
    companionConfigurationRevision += 1;
    return previewCompanionConfiguration(
      companionConfigurationRevision,
      defaultCompanion,
      modelRouting,
      skillRouting,
      capabilityGlobalPreferences,
      capabilityProjectPreferences,
    );
  };
  routes['agent.sessions.create'] = (request: ControlRequest) => {
    const body = record(request.body);
    const session = previewSession(
      `session-persona-${nextSessionId++}`,
      stringValue(body.title) || '新对话',
      stringValue(body.roleId) || 'companion-future-v1',
      Date.now(),
      stringValue(body.roleVersion) || '1',
    );
    sessions.unshift(session);
    return { ok: true, session };
  };
  routes['agent.tools.list'] = (request: ControlRequest) => {
    const sessionId = stringValue(record(request.query).sessionId);
    const projectId = `workspace-${'b'.repeat(64)}`;
    return previewCapabilityCatalog(
      sessionId,
      capabilitySessionPreferences.get(sessionId) ?? {},
      capabilityGlobalPreferences,
      capabilityProjectPreferences[projectId] ?? {},
    );
  };
  routes['agent.session.capability-policy.update'] = (request: ControlRequest) => {
    const sessionId = stringValue(record(request.params).sessionId);
    if (!sessions.some((session) => stringValue(session.id) === sessionId) && !roomSessions.some((session) => stringValue(session.id) === sessionId)) {
      throw new Error('Preview session not found.');
    }
    capabilitySessionPreferences.set(
      sessionId,
      previewCapabilityPreferences(record(request.body).capabilityDisclosurePreferences),
    );
    return { ok: true };
  };
  routes['agent.session.ui.resolve'] = (request: ControlRequest) => {
    const sessionId = stringValue(record(request.params).sessionId);
    const body = record(request.body);
    const requestId = stringValue(body.requestId);
    if (!sessionId || !requestId) throw new Error('Preview UI response requires its Session and request.');
    const cancelled = body.cancelled === true;
    previewTransport?.emit('agent.session.events', {
      schemaVersion: 'rag-ime.agent-event.v1',
      eventId: `${sessionId}:${requestId}:${cancelled ? 'cancelled' : 'resolved'}`,
      sessionId,
      turnId: `${sessionId}:turn-question`,
      sequence: 5,
      createdAtMs: Date.now(),
      eventType: 'user_input_required',
      payload: {
        requestId,
        requestKind: 'grouped_questions',
        method: 'editor',
        resolutionState: cancelled ? 'cancelled' : 'resolved',
        resolutionSource: stringValue(body.resolutionSource) || 'direct_user',
      },
      resumeToken: `${sessionId}:5`,
    });
    return { ok: true };
  };
  routes['agent.session.archive'] = (request: ControlRequest) => {
    const sessionId = stringValue(record(request.params).sessionId);
    const index = sessions.findIndex((session) => stringValue(session.id) === sessionId);
    const archived = record(request.body).archived === true;
    if (index < 0) throw new Error('Preview session not found.');
    sessions[index] = {
      ...sessions[index],
      status: archived ? 'archived' : 'idle',
      updatedAtMs: Date.now(),
    };
    return { ok: true, session: sessions[index] };
  };
  routes['agent.session.delete'] = (request: ControlRequest) => {
    const sessionId = stringValue(record(request.params).sessionId);
    const index = sessions.findIndex((session) => stringValue(session.id) === sessionId);
    if (index >= 0) sessions.splice(index, 1);
    return { ok: true, sessionId };
  };
  routes['agent.session.forks.list'] = (request: ControlRequest) => ({
    schemaVersion: 'rag-ime.agent-session-fork-candidates.v1',
    ok: true,
    sessionId: stringValue(record(request.params).sessionId) || 'session-preview',
    items: [
      { entryId: 'session-preview:user-architecture', text: '把迁移进度按真实代码链整理一下，别把工具日志当回答。', role: 'user', createdAtMs: 0 },
      { entryId: 'session-preview:assistant-architecture', text: '三条 Lane 已经收束到同一个 Todo。', role: 'assistant', createdAtMs: 0 },
      { entryId: 'session-preview:user-media', text: '把完成状态和附件也保留成结构化块。', role: 'user', createdAtMs: 0 },
      { entryId: 'session-preview:assistant-media', text: '已完成。活动明细仍可追溯，附件也已经登记。', role: 'assistant', createdAtMs: 0 },
    ],
  });
  routes['agent.session.forks.create'] = (request: ControlRequest) => {
    const body = record(request.body);
    const sourceSessionId = stringValue(record(request.params).sessionId) || 'session-preview';
    const entryId = stringValue(body.entryId) || 'session-preview:user-media';
    const selectedText = ({
      'session-preview:user-architecture': '把迁移进度按真实代码链整理一下，别把工具日志当回答。',
      'session-preview:assistant-architecture': '',
      'session-preview:user-media': '把完成状态和附件也保留成结构化块。',
      'session-preview:assistant-media': '',
    } as Record<string, string>)[entryId] ?? '从这里创建分支。';
    const now = Date.now();
    const session = {
      ...previewSession(
        `session-fork-${nextSessionId++}`,
        stringValue(body.title) || '对话分支',
        'companion-present-v1',
        now,
      ),
      schemaVersion: 'rag-ime.agent-session.v1',
      status: 'idle',
      modelProfile: 'session-selected',
      toolProfileVersion: 'control-center-v1',
      createdAtMs: now,
      messageCount: 2,
    };
    sessions.unshift(session);
    return {
      schemaVersion: 'rag-ime.agent-session-fork-create.v1',
      ok: true,
      sourceSessionId,
      entryId,
      selectedText,
      session,
    };
  };
  routes['observability.evalSchedule.action'] = (request: ControlRequest) => {
    const schedule = previewEvalSchedules.find((item) => item.id === record(request.params).scheduleId);
    if (!schedule) throw new Error('评测计划不存在');
    const action = stringValue(record(request.body).action);
    const allowed: Record<string, string[]> = { pause: ['scheduled'], resume: ['paused'], cancel: ['scheduled', 'paused', 'failed'], retry: ['completed', 'failed'] };
    if (!allowed[action]?.includes(stringValue(schedule.status))) throw new Error('当前状态无法执行此操作');
    schedule.status = ({ pause: 'paused', resume: 'scheduled', cancel: 'cancelled', retry: 'scheduled' } as Record<string, string>)[action];
    if (action === 'retry' || action === 'resume') schedule.nextDueAtMs = Math.max(Number(schedule.nextDueAtMs) || 0, Date.now() + 1000);
    return { ok: true, schedule: { ...schedule } };
  };
  routes['agent.wakeSchedules.list'] = () => ({ ok: true, schedulerActive: true, items: [...wakeSchedules] });
  routes['agent.wakeSchedules.create'] = (request: ControlRequest) => {
    const body = record(request.body);
    const now = Date.now();
    const schedule = {
      id: `wake:preview-${nextWakeScheduleId++}`,
      title: stringValue(body.title) || '预览预约',
      instruction: stringValue(body.instruction),
      targetType: stringValue(body.targetType) || 'session',
      targetSessionId: stringValue(body.targetSessionId),
      targetRoleId: stringValue(body.targetRoleId),
      targetRoleVersion: stringValue(body.targetRoleVersion),
      planningTaskId: stringValue(body.planningTaskId),
      timezone: stringValue(body.timezone) || 'Asia/Shanghai',
      recurrenceKind: stringValue(body.recurrenceKind) || 'once',
      recurrenceInterval: Number(body.recurrenceInterval) || 1,
      maxRuns: Number(body.maxRuns) || 1,
      runCount: 0,
      status: 'scheduled',
      nextWakeAtMs: Number(body.wakeAtMs) || now + 30 * 60_000,
      lastWakeAtMs: 0,
      lastError: '',
      createdAtMs: now,
      updatedAtMs: now,
      latestRun: {},
    };
    wakeSchedules.unshift(schedule);
    return { ok: true, schedule };
  };
  routes['agent.wakeSchedule.action'] = (request: ControlRequest) => {
    const scheduleId = stringValue(record(request.params).scheduleId);
    const schedule = wakeSchedules.find((item) => item.id === scheduleId);
    if (!schedule) throw new Error('预约不存在');
    const action = stringValue(record(request.body).action);
    if (action === 'edit') {
      if (!['scheduled', 'paused'].includes(stringValue(schedule.status))) throw new Error('只能编辑等待中或已暂停的安排');
      const definition = record(record(request.body).schedule);
      Object.assign(schedule, definition, { nextWakeAtMs: Number(definition.wakeAtMs) });
    }
    schedule.status = ({ pause: 'paused', resume: 'scheduled', cancel: 'cancelled', retry: 'scheduled' } as Record<string, string>)[action] ?? schedule.status;
    schedule.updatedAtMs = Date.now();
    if (action === 'retry') schedule.nextWakeAtMs = Date.now() + 1_000;
    return { ok: true, schedule: { ...schedule } };
  };
  routes['agent.wakeSchedule.runs'] = (request: ControlRequest) => {
    const scheduleId = stringValue(record(request.params).scheduleId);
    return {
      ok: true,
      schedule: wakeSchedules.find((item) => item.id === scheduleId) ?? {},
      items: [],
    };
  };
  routes['agent.session.backgroundJobs.list'] = (request: ControlRequest) => {
    const sessionId = stringValue(record(request.params).sessionId);
    const requestedLimit = Number(record(request.query).limit) || 50;
    const limit = Math.min(100, Math.max(1, Math.trunc(requestedLimit)));
    const items = (previewBackgroundJobsBySession[sessionId] ?? []).slice(0, limit);
    return {
      schemaVersion: 'rag-ime.agent-background-job-list.v1',
      ok: true,
      sessionId,
      items,
      activeCount: items.filter((job) => (
        job.status === 'queued' || job.status === 'running' || job.status === 'cancelling'
      )).length,
    };
  };
  routes['agent.session.backgroundJob.get'] = (request: ControlRequest) => {
    const sessionId = stringValue(record(request.params).sessionId);
    const jobId = stringValue(record(request.params).jobId);
    const job = (previewBackgroundJobsBySession[sessionId] ?? [])
      .find((item) => item.jobId === jobId);
    if (!job) throw new Error('Preview background job not found.');
    return { ok: true, job };
  };
  routes['agent.session.backgroundJob.logs'] = (request: ControlRequest) => {
    const sessionId = stringValue(record(request.params).sessionId);
    const jobId = stringValue(record(request.params).jobId);
    const job = (previewBackgroundJobsBySession[sessionId] ?? [])
      .find((item) => item.jobId === jobId);
    if (!job) throw new Error('Preview background job not found.');
    const text = job.status === 'failed'
      ? 'test_agent_routes ... FAIL\nAssertionError: expected background job route\n'
      : '> control-center-web@ build\n> tsc -b && vite build\ntransforming modules...\n';
    const requestedCursor = Math.max(0, Math.trunc(Number(record(request.query).cursor) || 0));
    const cursor = Math.max(requestedCursor, job.logStartCursor);
    const nextCursor = cursor + new TextEncoder().encode(text).byteLength;
    return {
      schemaVersion: 'rag-ime.agent-background-job-log.v1',
      ok: true,
      jobId,
      sessionId,
      cursor,
      nextCursor,
      logStartCursor: job.logStartCursor,
      truncatedBeforeCursor: requestedCursor < job.logStartCursor,
      hasMore: nextCursor < job.outputBytes,
      text,
    };
  };
  routes['agent.session.backgroundJob.cancel'] = (request: ControlRequest) => {
    const sessionId = stringValue(record(request.params).sessionId);
    const jobId = stringValue(record(request.params).jobId);
    const jobs = previewBackgroundJobsBySession[sessionId] ?? [];
    const index = jobs.findIndex((item) => item.jobId === jobId);
    if (index < 0) throw new Error('Preview background job not found.');
    if (
      jobs[index]!.status === 'completed'
      || jobs[index]!.status === 'failed'
      || jobs[index]!.status === 'cancelled'
      || jobs[index]!.status === 'orphaned'
    ) {
      return {
        schemaVersion: 'rag-ime.agent-background-job-cancel-receipt.v1',
        ok: true,
        summary: '后台任务已经结束',
        alreadyTerminal: true,
        job: jobs[index]!,
      };
    }
    const nowMs = Date.now();
    const job = {
      ...jobs[index]!,
      status: 'cancelled' as const,
      updatedAtMs: nowMs,
      endedAtMs: nowMs,
      exitCode: 143,
      cancelRequestedAtMs: nowMs,
    };
    jobs[index] = job;
    previewTransport?.emit('agent.session.events', {
      schemaVersion: 'rag-ime.agent-event.v1',
      eventId: `${sessionId}:background-job-cancelled:${jobId}`,
      sessionId,
      turnId: `background-job:${jobId}`,
      sequence: 8,
      createdAtMs: nowMs,
      eventType: 'background_job_cancelled',
      payload: { job },
      resumeToken: `${sessionId}:8`,
    });
    return {
      schemaVersion: 'rag-ime.agent-background-job-cancel-receipt.v1',
      ok: true,
      summary: '已请求停止后台任务',
      alreadyTerminal: false,
      job,
      cancelReceipt: {
        jobId,
        status: 'cancelled',
        reason: stringValue(record(request.body).reason) || 'preview_user_requested',
      },
    };
  };

  routes['memory.pages'] = (request: ControlRequest) =>
    previewMemoryPage(request, previewEvidenceDisposition);
  routes['memory.source.disposition'] = (request: ControlRequest) => {
    const disposition = stringValue(record(request.body).disposition);
    if (disposition === 'pending' || disposition === 'not_for_memory') {
      previewEvidenceDisposition = disposition;
    }
    return { ok: true, changed: true };
  };
  routes['configuration.settings'] = () =>
    previewConfigurationSettings(previewConfiguration, previewConfigurationRevision);
  routes['configuration.settings.preview'] = (request: ControlRequest) => {
    const changes = record(record(request.body).changes);
    const fieldCount = Object.keys(changes).length;
    return {
      ok: true,
      previewToken: 'preview-configuration-settings',
      pathId: 'configuration.settings.apply',
      payloadSha256: `sha256:preview-configuration-${fieldCount}`,
      requiredConfirm: 'apply',
      expiresAtMs: Date.now() + 300_000,
      expectedRevision: {
        runtimeRevision: previewConfigurationRevision,
        subjectRevision: `sha256:preview-settings-${previewConfigurationRevision}`,
      },
      summary: {
        title: '保存这些设置？',
        items: [`将更新 ${fieldCount} 项设置；应用后仍可撤销。`],
        risk: 'R1',
      },
    };
  };
  routes['configuration.settings.apply'] = (request: ControlRequest) => {
    const body = record(request.body);
    previousPreviewConfiguration = previewConfiguration;
    previewConfiguration = applyPreviewConfigurationChanges(
      previewConfiguration,
      record(body.changes),
    );
    previewConfigurationRevision += 1;
    return {
      ok: true,
      receiptId: `preview-configuration-receipt-${previewConfigurationRevision}`,
      pathId: 'configuration.settings.apply',
      payloadSha256: stringValue(body.payloadSha256),
      appliedAtMs: Date.now(),
      rollbackAvailable: true,
      rollbackToken: `preview-configuration-rollback-${previewConfigurationRevision}`,
      result: { runtimeRevision: previewConfigurationRevision },
    };
  };
  routes['configuration.settings.rollback'] = (request: ControlRequest) => {
    const body = record(request.body);
    if (previousPreviewConfiguration) {
      previewConfiguration = previousPreviewConfiguration;
      previousPreviewConfiguration = undefined;
    }
    previewConfigurationRevision += 1;
    return {
      ok: true,
      receiptId: `preview-configuration-rollback-receipt-${previewConfigurationRevision}`,
      pathId: 'configuration.settings.rollback',
      payloadSha256: stringValue(body.payloadSha256),
      appliedAtMs: Date.now(),
      rollbackAvailable: false,
      rollbackToken: '',
      result: { runtimeRevision: previewConfigurationRevision },
    };
  };
  routes['input.lexicon.review'] = () => previewLexiconReview();
  routes['input.lexicon.apply'] = (request: ControlRequest) => {
    const selectedKeys = record(request.body).selectedKeys;
    const entryCount = Array.isArray(selectedKeys)
      ? selectedKeys.filter((item): item is string => typeof item === 'string').length
      : 0;
    previewLexiconRollbackId = `preview-lexicon-rollback-${Date.now()}`;
    return {
      schemaVersion: 'rag-ime.rime-lexicon-review.v1',
      ok: true,
      applied: true,
      rollbackId: previewLexiconRollbackId,
      entryCount,
      requiresRedeploy: true,
    };
  };
  routes['input.lexicon.rollback'] = (request: ControlRequest) => ({
    schemaVersion: 'rag-ime.rime-lexicon-review.v1',
    ok: true,
    rolledBack: true,
    rollbackId: stringValue(record(request.body).rollbackId) || previewLexiconRollbackId,
    entryCount: 0,
    requiresRedeploy: true,
  });
  Object.assign(
    routes,
    createPreviewHistoryRoutes(),
    createPreviewWorkDocumentRoutes(),
  );
  routes['agent.rooms.list'] = (request: ControlRequest) => {
    const includeArchived = record(request.query).includeArchived === true;
    const query = record(request.query);
    const ownerAppId = Object.prototype.hasOwnProperty.call(query, 'ownerAppId')
      ? stringValue(query.ownerAppId)
      : null;
    const surfaceKey = Object.prototype.hasOwnProperty.call(query, 'surfaceKey')
      ? stringValue(query.surfaceKey)
      : null;
    return {
      ok: true,
      rooms: [...previewRoomSnapshots.values()]
        .map((snapshot) => record(snapshot.room))
        .filter((room) => includeArchived || room.status !== 'archived')
        .filter((room) => ownerAppId === null || stringValue(room.ownerAppId) === ownerAppId)
        .filter((room) => surfaceKey === null || stringValue(room.surfaceKey) === surfaceKey),
    };
  };
  routes['agent.rooms.create'] = (request: ControlRequest) => {
    const roomId = `room-preview-${nextRoomId++}`;
    const snapshot = previewCreatedRoomSnapshot(roomId, record(request.body));
    previewRoomSnapshots.set(roomId, snapshot);
    return { ok: true, room: record(snapshot.room) };
  };
  routes['agent.room.get'] = (request: ControlRequest) => {
    const roomId = stringValue(record(request.params).roomId);
    const snapshot = previewRoomSnapshots.get(roomId);
    if (!snapshot) throw new Error('这个协作空间已经不存在，请刷新列表。');
    return { ok: true, room: record(snapshot.room) };
  };
  routes['agent.room.snapshot'] = (request: ControlRequest) => {
    const roomId = stringValue(record(request.params).roomId);
    const snapshot = previewRoomSnapshots.get(roomId);
    if (!snapshot) throw new Error('这个协作空间已经不存在，请刷新列表。');
    return snapshot;
  };
  routes['agent.room.participant.add'] = (request: ControlRequest) => {
    const roomId = stringValue(record(request.params).roomId);
    const snapshot = previewRoomSnapshots.get(roomId);
    if (!snapshot) throw new Error('这个协作空间已经不存在，请刷新列表。');
    const room = record(snapshot.room);
    const members = Array.isArray(room.participants) ? room.participants.map(record) : [];
    if (members.filter((member) => member.status === 'active').length >= 8) throw new Error('Room 已达到 8 位伙伴。');
    const body = record(request.body);
    const persona = personas.find((item) => item.roleId === body.roleId && item.version === body.roleVersion);
    if (!persona) throw new Error('这位伙伴暂时不可用。');
    if (members.some((member) => member.roleId === persona.roleId && member.roleVersion === persona.version && member.status === 'active')) throw new Error('这位伙伴已在 Room 中。');
    const ordinal = Math.max(-1, ...members.map((member) => Number(member.ordinal))) + 1;
    const sessionId = `${roomId}:session-${ordinal + 1}`;
    const participant = { schemaVersion: 'rag-ime.agent-participant.v1', id: `${roomId}:participant-${ordinal + 1}`, roomId, sessionId, roleId: persona.roleId, roleVersion: persona.version, displayName: persona.displayName, collaborationRole: stringValue(body.collaborationRole) || 'implementer', status: 'active', ordinal, createdAtMs: Date.now(), lastSpokeAtMs: null };
    const updated = { ...room, participants: [...members, participant], updatedAtMs: Date.now() };
    previewRoomSnapshots.set(roomId, { ...snapshot, room: updated });
    sessions.unshift({ ...sessions[0]!, id: sessionId, title: persona.displayName, roleId: persona.roleId, roleVersion: persona.version, status: 'idle' });
    return { ok: true, room: updated, participant };
  };
  routes['agent.room.conversationSnapshot'] = (request: ControlRequest) => {
    const roomId = stringValue(record(request.params).roomId);
    const snapshot = previewRoomSnapshots.get(roomId);
    if (!snapshot) throw new Error('这个协作空间已经不存在，请刷新列表。');
    return previewRoomConversationSnapshot(snapshot);
  };
  routes['agent.room.topic.create'] = (request: ControlRequest) => {
    const roomId = stringValue(record(request.params).roomId);
    const snapshot = previewRoomSnapshots.get(roomId);
    if (!snapshot) throw new Error('这个协作空间已经不存在，请刷新列表。');
    const updated = previewRoomTopicMutation(snapshot, record(request.body), true);
    previewRoomSnapshots.set(roomId, updated);
    return { ok: true, room: record(updated.room) };
  };
  routes['agent.room.topic.update'] = (request: ControlRequest) => {
    const roomId = stringValue(record(request.params).roomId);
    const snapshot = previewRoomSnapshots.get(roomId);
    if (!snapshot) throw new Error('这个协作空间已经不存在，请刷新列表。');
    const updated = previewRoomTopicMutation(snapshot, record(request.body), false);
    previewRoomSnapshots.set(roomId, updated);
    return { ok: true, room: record(updated.room) };
  };
  routes['agent.room.topics'] = (request: ControlRequest) => {
    const roomId = stringValue(record(request.params).roomId);
    const snapshot = previewRoomSnapshots.get(roomId);
    if (!snapshot) throw new Error('这个协作空间已经不存在，请刷新列表。');
    const topics = record(snapshot.room).topics;
    return { ok: true, topics: Array.isArray(topics) ? topics : [] };
  };
  routes['agent.media.preview'] = (request: ControlRequest) => previewManagedFile(request);
  routes['knowledgeBases.list'] = () => ({
    ok: true,
    items: previewKnowledgeBases.map((base) => ({ ...base })),
  });
  routes['knowledgeBases.get'] = (request: ControlRequest) => {
    const baseId = stringValue(record(request.params).kbId);
    const base = previewKnowledgeBases.find((item) => stringValue(item.id) === baseId);
    if (!base) throw new Error('这个文档知识库已经不存在，请刷新列表。');
    return { ok: true, base: { ...base } };
  };
  routes['knowledgeBases.create'] = (request: ControlRequest) => {
    const body = record(request.body);
    const name = stringValue(body.name).trim();
    if (!name) throw new Error('知识库名称不能为空。');
    const now = Date.now();
    const base = {
      id: `kb:preview-created-${nextKnowledgeBaseId++}`,
      name,
      description: stringValue(body.description).trim(),
      documentCount: 0,
      chunkCount: 0,
      status: 'ready',
      agentEnabled: body.agentEnabled === true,
      parserMode: stringValue(body.parserProvider) || 'auto',
      revision: 1,
      updatedAtMs: now,
    };
    previewKnowledgeBases = [base, ...previewKnowledgeBases];
    return { ok: true, base: { ...base } };
  };
  routes['knowledgeBases.update'] = (request: ControlRequest) => {
    const baseId = stringValue(record(request.params).kbId);
    const body = record(request.body);
    const index = previewKnowledgeBases.findIndex((item) => stringValue(item.id) === baseId);
    const current = previewKnowledgeBases[index];
    if (!current) throw new Error('这个文档知识库已经不存在，请刷新列表。');
    const expectedRevision = body.expectedRevision;
    if (expectedRevision !== undefined && String(expectedRevision) !== String(current.revision)) {
      throw new Error('知识库已在其他位置更新，请刷新后重试。');
    }
    const parserProvider = stringValue(body.parserProvider);
    const updated = {
      ...current,
      ...(typeof body.name === 'string' ? { name: body.name.trim() } : {}),
      ...(typeof body.description === 'string' ? { description: body.description.trim() } : {}),
      ...(typeof body.agentEnabled === 'boolean' ? { agentEnabled: body.agentEnabled } : {}),
      ...(parserProvider ? { parserMode: parserProvider } : {}),
      ...(record(body.chunkingConfig).strategy ? { chunkingConfig: { ...record(body.chunkingConfig) } } : {}),
      ...(record(body.retrievalConfig).mode ? { retrievalConfig: { ...record(body.retrievalConfig) } } : {}),
      revision: Number(current.revision ?? 0) + 1,
      updatedAtMs: Date.now(),
    };
    previewKnowledgeBases = previewKnowledgeBases.map((item, itemIndex) => itemIndex === index ? updated : item);
    return { ok: true, base: { ...updated } };
  };
  routes['knowledgeBases.documents.list'] = (request: ControlRequest) => {
    const baseId = stringValue(record(request.params).kbId);
    return {
      ok: true,
      items: previewKnowledgeDocuments
        .filter((document) => stringValue(document.baseId) === baseId)
        .map((document) => ({ ...document })),
    };
  };
  routes['knowledgeBases.chunkPreview'] = (request: ControlRequest) => {
    const baseId = stringValue(record(request.params).kbId);
    const documentId = stringValue(record(request.params).fileId);
    const document = previewKnowledgeDocuments.find((item) => (
      stringValue(item.baseId) === baseId && stringValue(item.id) === documentId
    ));
    if (!document) throw new Error('用于预览切分的材料已经不存在。');
    const strategy = stringValue(record(record(request.body).chunkingConfig).strategy) || 'markdown';
    return {
      ok: true,
      fileId: documentId,
      total: 2,
      truncated: false,
      items: [
        {
          chunkId: 'chunk:preview-settings-1',
          ordinal: 0,
          content: `# Agent Runtime\n使用 ${strategy} 策略生成的首个预览片段。`,
          page: 1,
        },
        {
          chunkId: 'chunk:preview-settings-2',
          ordinal: 1,
          content: 'Tool 只按需检索这个文档知识库，不会读取个人记忆。',
          page: 3,
        },
      ],
    };
  };
  routes['knowledgeBases.document.retry'] = (request: ControlRequest) => {
    const baseId = stringValue(record(request.params).kbId);
    const documentId = stringValue(record(request.params).fileId);
    const document = previewKnowledgeDocuments.find((item) => (
      stringValue(item.baseId) === baseId && stringValue(item.id) === documentId
    ));
    if (!document) throw new Error('需要重新解析的材料已经不存在。');
    const now = Date.now();
    const job = {
      id: `job:preview-reparse-${nextKnowledgeJobId++}`,
      baseId,
      fileId: documentId,
      fileName: stringValue(document.fileName),
      kind: 'reparse',
      parserMode: stringValue(record(request.body).parserProvider) || stringValue(document.parserProvider),
      status: 'succeeded',
      stage: 'ready',
      progress: 1,
      cancellable: false,
      revision: Number(document.revision ?? 0),
      createdAtMs: now,
      startedAtMs: now,
      finishedAtMs: now,
      updatedAtMs: now,
    };
    previewKnowledgeJobs = [job, ...previewKnowledgeJobs];
    return { ok: true, job: { ...job } };
  };
  routes['knowledgeBases.jobs.list'] = (request: ControlRequest) => {
    const baseId = stringValue(record(request.params).kbId);
    return {
      ok: true,
      items: previewKnowledgeJobs
        .filter((job) => stringValue(job.baseId) === baseId)
        .map((job) => ({ ...job })),
    };
  };
  const routeIds = Array.from(new Set<ControlPathId>(
    Object.keys(routes) as ControlPathId[],
  ));
  previewTransport = new PreviewControlTransport({
    routes,
    capabilities: {
      routeIds,
      features: {
        configurationSettingsWorkContract: true,
        historyWorkContract: true,
        managementWorkContract: true,
        workDocuments: true,
      },
      raw: {
        client: { remote: false, deviceAuthenticated: false, grantedScopes: [] },
        features: {
          configurationSettingsWorkContract: true,
          historyWorkContract: true,
          managementWorkContract: true,
          workDocuments: true,
        },
        routes: [],
      },
    },
    imagePaste: previewImagePaste,
    pickedFiles: [
      {
        id: 'media_preview_attachment_01',
        name: 'agent-runtime.png',
        mimeType: 'image/png',
        byteSize: 2_048,
        sessionId: 'session-preview',
        sha256: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
      },
    ],
  });
  return previewTransport;
}

async function previewImagePaste(
  options: AgentImagePasteOptions,
): Promise<PickedFile[]> {
  const ownerId = options.roomId ?? options.sessionId;
  if (
    !ownerId
    || !/^[A-Za-z0-9][A-Za-z0-9:._-]{0,159}$/.test(ownerId)
    || (options.roomId !== undefined && options.sessionId !== undefined)
  ) {
    throw new TypeError('Preview file paste requires exactly one bounded sessionId or roomId');
  }
  const owner = options.roomId ? { roomId: ownerId } : { sessionId: ownerId };
  const files = Array.from(options.files ?? []);
  if (!files.length) return [];
  const maxFiles = options.maxFiles ?? files.length;
  if (!Number.isSafeInteger(maxFiles) || maxFiles < 1 || maxFiles > 8 || files.length > maxFiles) {
    throw new TypeError('Preview file paste requires between 1 and 8 files within maxFiles');
  }
  const receipts: PickedFile[] = [];
  for (const file of files.slice(0, maxFiles)) {
    if (file.size <= 0 || file.size > MAX_COMPOSER_ATTACHMENT_BYTES) {
      throw new TypeError('Preview file paste accepts only non-empty files up to 20 MiB');
    }
    const sha256 = await previewSha256(file);
    receipts.push({
      id: `media_preview_${sha256.slice(0, 24)}`,
      name: file.name || 'clipboard-file',
      mimeType: normalizeComposerAttachmentMimeType(file.type),
      byteSize: file.size,
      ...owner,
      sha256,
    });
  }
  return receipts;
}

async function previewSha256(file: File): Promise<string> {
  const bytes = typeof file.arrayBuffer === 'function'
    ? await file.arrayBuffer()
    : await new Promise<ArrayBuffer>((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(reader.error ?? new Error('Unable to read preview image bytes'));
        reader.onload = () => {
          if (reader.result instanceof ArrayBuffer) resolve(reader.result);
          else reject(new TypeError('Preview image reader returned non-binary data'));
        };
        reader.readAsArrayBuffer(file);
      });
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes);
  return Array.from(
    new Uint8Array(digest),
    (value) => value.toString(16).padStart(2, '0'),
  ).join('');
}


interface PreviewManagedFile {
  fileName: string;
  mimeType: string;
  previewKind: 'markdown' | 'code' | 'diff' | 'image' | 'html' | 'unsupported';
  content: string;
  language?: string;
}

const PREVIEW_MANAGED_FILES: Record<string, PreviewManagedFile> = {
  media_previewdoc01: {
    fileName: 'room-runtime-handoff.md',
    mimeType: 'text/markdown',
    previewKind: 'markdown',
    content: [
      '# Room Runtime 交接',
      '',
      '这份文件来自受控 `file` Rich Block，不会把整份产物塞进对话上下文。',
      '',
      '- Session 私有过程保持私有',
      '- Room 只接收显式提交的 Post',
      '- 文件内容按回执和摘要按需读取',
    ].join('\n'),
  },
  media_previewreport01: {
    fileName: 'lexicon-health-report.html',
    mimeType: 'text/html',
    previewKind: 'html',
    content: PREVIEW_REPORT_HTML,
  },
};

function previewManagedFile(request: ControlRequest): Record<string, unknown> {
  const mediaId = stringValue(record(request.params).mediaId);
  const sessionId = stringValue(record(request.query).sessionId);
  const sha256 = 'c'.repeat(64);
  // Exercises the dialog's error + retry path against a receipt that genuinely
  // cannot be read, rather than against a mocked-out success.
  if (mediaId === 'media_previewbroken01') {
    throw new Error('Preview file receipt is unavailable.');
  }
  const file = PREVIEW_MANAGED_FILES[mediaId];
  if (!file || !sessionId) {
    throw new Error('Preview file receipt is unavailable.');
  }
  const expectedSha256 = stringValue(record(request.query).sha256);
  if (expectedSha256 && expectedSha256 !== sha256) {
    throw new Error('Preview file digest changed.');
  }
  const content = file.content;
  return {
    schemaVersion: 'rag-ime.agent-file-preview.v1',
    descriptor: {
      schemaVersion: 'rag-ime.agent-file-descriptor.v1',
      mediaId,
      sessionId,
      fileName: file.fileName,
      mimeType: file.mimeType,
      byteSize: new TextEncoder().encode(content).byteLength,
      sha256,
      previewKind: file.previewKind,
      language: file.language ?? '',
      contentUrl: `/api/agent/media/${mediaId}/content?sessionId=${encodeURIComponent(sessionId)}`,
    },
    content,
    previewByteSize: new TextEncoder().encode(content).byteLength,
    truncated: false,
  };
}

function previewResponse(pathId: ControlPathId): unknown {
  switch (pathId) {
    case 'agent.eval-lab.runs':
      return () => previewEvalLabRuns();
    case 'agent.eval-lab.evidence':
      return (request: ControlRequest) => previewEvalLabEvidence({ query: record(request.query) as Record<string, string> });
    case 'observability.snapshot':
      return (request: ControlRequest) =>
        previewObservationSnapshot(record(request.query));
    case 'agent.runtime.get':
      return {
        schemaVersion: 'rag-ime.agent-runtime.v1',
        enabled: true,
        managed: true,
        status: 'ready',
        driverId: 'pi-managed',
        runtimeKind: 'pi',
        runtimeVersion: 'preview',
        piVersion: 'preview',
        idleTimeoutSeconds: 900,
        activeSessionId: 'session-preview',
        lastError: '',
        capabilities: { conversationFork: true, conversationRewrite: true },
      };
    case 'agent.session.models':
      return {
        schemaVersion: 'rag-ime.agent-model-catalog.v1',
        ok: true,
        sessionId: 'session-preview',
        selected: { provider: 'openai', id: 'gpt-5.4' },
        thinkingLevel: 'medium',
        providers: [
          {
            id: 'openai',
            displayName: 'OpenAI',
            models: [
              {
                provider: 'openai',
                id: 'gpt-5.4',
                name: 'GPT-5.4',
                api: 'responses',
                reasoning: true,
                thinkingLevels: ['off', 'low', 'medium', 'high'],
                supportsImages: true,
                contextWindow: 1_000_000,
                maxTokens: 128_000,
              },
            ],
          },
        ],
      };
    case 'agent.role.models':
      return previewModelCatalog('role-default');
    case 'agent.session.contextItems.list':
      return { ok: true, items: [] };
    case 'agent.session.contextTraces.list':
      return (request: ControlRequest) => {
        const sessionId = stringValue(record(request.params).sessionId) || 'session-preview';
        const now = Date.now();
        return {
          ok: true,
          items: ['turn-initial', 'turn-steady', 'turn-recovered'].map((turnId, index) => ({
            traceId: `context-trace:${turnId}`,
            sessionId,
            turnId,
            sourceKind: 'user',
            status: 'accepted',
            finalFingerprint: 'sha256:0123456789abcdef',
            nodeCount: 6,
            createdAtMs: now - (18_000 - index * 2_000),
            updatedAtMs: now - (17_000 - index * 2_000),
          })),
        };
      };
    case 'agent.session.contextTrace.get':
      return (request: ControlRequest) => previewContextTrace(
        stringValue(record(request.params).sessionId) || 'session-preview',
        stringValue(record(request.params).traceId) || 'context-trace:preview',
      );
    case 'agent.session.debugContext.get':
      return (request: ControlRequest) => previewDebugContext(
        stringValue(record(request.params).sessionId) || 'session-preview',
        stringValue(record(request.query).turnId) || 'turn-recovered',
      );
    case 'agent.session.workspace.list':
      return (request: ControlRequest) => previewWorkspaceList(
        stringValue(record(request.query).path) || '/Volumes/work/wisdom-weasel-rag-ime',
      );
    case 'agent.session.workspace.read':
      return (request: ControlRequest) => previewWorkspaceRead(
        stringValue(record(request.query).path) || '/Volumes/work/wisdom-weasel-rag-ime/README.md',
      );
    case 'agent.rooms.list':
      return { ok: true, rooms: [previewRoomSnapshot('room-preview').room] };
    case 'agent.room.snapshot':
      return (request: ControlRequest) => previewRoomSnapshot(String(request.params?.roomId ?? 'room-preview'));
    case 'agent.room.conversationSnapshot':
      return (request: ControlRequest) => previewRoomConversationSnapshot(
        previewRoomSnapshot(String(request.params?.roomId ?? 'room-preview')) as unknown as Record<string, unknown>,
      );
    case 'agent.roles.list':
      return { ok: true, roles: [] };
    case 'agent.tools.list':
      return (request: ControlRequest) => previewCapabilityCatalog(
        stringValue(record(request.query).sessionId),
      );
    case 'agent.subagents.templates':
      return {
        schemaVersion: 'rag-ime.agent-template-list.v1',
        ok: true,
        maxParallel: 2,
        maxDepth: 2,
        items: previewTemplates,
      };
    case 'agent.subagents.create':
      return (request: ControlRequest) => {
        const sessionId = stringValue(record(request.body).sessionId) || 'session-preview';
        return {
          schemaVersion: 'rag-ime.agent-delegation.v1',
          ok: true,
          accepted: true,
          acceptanceScope: 'delegation_request',
          waited: false,
          batch: previewSubagentBatch(sessionId),
        };
      };
    case 'agent.subagents.list':
      return (request: ControlRequest) => {
        const sessionId = stringValue(record(request.query).sessionId) || 'session-preview';
        const batch = previewSubagentBatch(sessionId);
        return {
          ok: true,
          items: [batch],
          tree: previewSubagentTree(batch),
        };
      };
    case 'agent.subagent.get':
      return (request: ControlRequest) => ({
        ok: true,
        batch: previewSubagentBatch(
          stringValue(record(request.query).sessionId) || 'session-preview',
        ),
      });
    case 'agent.artifact.get':
      return previewSubagentArtifact();
    case 'planning.dashboard':
      return { ok: true, date: new Date().toISOString().slice(0, 10), tasks: [], goals: [] };
    case 'memory.summary':
      return {
        ok: true,
        runtimeRevision: 43,
        eventCount: 1_284,
        memoryItemCount: 326,
        evidenceSourceCount: 1_108,
        memoryBookCount: 12,
        memoryAtomCount: 248,
        forgottenSourceCount: 37,
        needsReviewSourceCount: 4,
        owners: [
          { ownerKind: 'user', ownerId: 'default', itemCount: 292 },
          { ownerKind: 'agent', ownerId: 'companion-present-v1', itemCount: 34 },
        ],
      };
    case 'memory.pages':
      return { ok: true, items: [], nextCursor: '' };
    case 'memory.source.disposition':
      return { ok: true, changed: true };
    case 'knowledgeBases.list':
      return { ok: true, items: [previewKnowledgeBase()] };
    case 'knowledgeBases.get':
    case 'knowledgeBases.create':
    case 'knowledgeBases.update':
      return { ok: true, base: previewKnowledgeBase() };
    case 'knowledgeBases.documents.list':
      return {
        ok: true,
        items: [
          {
            id: 'file:preview-yuxi',
            baseId: 'kb:preview-project-docs',
            fileName: '伙伴运行笔记.md',
            mimeType: 'text/markdown',
            byteSize: 48_320,
            status: 'ready',
            stage: 'ready',
            chunkCount: 36,
            parserProvider: 'builtin',
            revision: 1,
            updatedAtMs: Date.now() - 180_000,
          },
        ],
      };
    case 'knowledgeBases.jobs.list':
      return { ok: true, items: [] };
    case 'knowledgeBases.search':
      return {
        ok: true,
        retrieval: {
          mode: 'hybrid', effectiveMode: 'hybrid', lexicalAvailable: true,
          config: { mode: 'hybrid', topK: 10, threshold: 0.2, lexicalWeight: 1, denseWeight: 1, graphEnabled: false, graphWeight: 0.7, rrfK: 60, candidateMultiplier: 4, rerankEnabled: false, rerankCandidateDepth: 40 },
          libraries: [{ kbId: 'kb:preview-project-docs', candidateLimit: 40, lexicalCandidates: 1, denseCandidates: 1, graphCandidates: 0, graphStatus: 'disabled', rerankApplied: false, rerankCandidates: 0, returned: 1 }],
          dense: { available: true, degraded: false, provider: { provider: 'preview', model: '演示向量模型' } },
          reranker: { configured: false, provider: 'none' },
        },
        items: [
          {
            chunkId: 'chunk:preview-agent-loop',
            documentId: 'file:preview-yuxi',
            documentName: '伙伴运行笔记.md',
            heading: 'Agent Tool 边界',
            content: '文档知识库通过只读 Tool 按需检索，不会进入输入法候选热路径。',
            score: 0.92,
            diagnostics: { effectiveMode: 'hybrid', lexicalRank: 1, denseRank: 1, lexicalScore: 0.86, denseScore: 0.92, retrievalRank: 1, retrievalScore: 0.92 },
            citation: { page: 3, heading: 'Agent Tool 边界' },
          },
        ],
      };
    case 'knowledgeBases.graph.get':
      return {
        schemaVersion: 'rag-ime.knowledge-graph.v1',
        kbId: 'kb:preview-project-docs',
        revision: 1,
        sourceRevision: `sha256:${'a'.repeat(64)}`,
        status: 'ready',
        updatedAtMs: Date.now() - 60_000,
        nodes: [
          { id: 'doc:runtime', label: '伙伴运行笔记.md', kind: 'document', documentId: 'file:preview-yuxi', documentName: '伙伴运行笔记.md', weight: 1 },
          { id: 'topic:tools', label: 'Agent Tool 边界', kind: 'topic', weight: .9 },
          { id: 'entity:worker', label: 'Knowledge Worker', kind: 'entity', weight: .84 },
        ],
        edges: [
          { id: 'edge:doc-topic', source: 'doc:runtime', target: 'topic:tools', kind: 'contains', label: '包含', weight: .9 },
          { id: 'edge:topic-worker', source: 'topic:tools', target: 'entity:worker', kind: 'mentions', label: '提及', weight: .84 },
        ],
        stats: {
          nodeCount: 4,
          edgeCount: 3,
          documentCount: 1,
          chunkCount: 1,
          indexedDocumentCount: 1,
          pendingDocumentCount: 0,
        },
        truncated: false,
      };
    case 'knowledgeBases.graph.rebuild':
      return { ok: true, jobId: 'graph:preview-rebuild', status: 'queued' };
    case 'knowledgeBases.open':
      return { ok: true, items: [] };
    case 'knowledgeWorker.health':
      return { ok: true, available: true, status: 'ready', readyDocumentCount: 1 };
    case 'knowledgeParsers.list':
      return {
        ok: true,
        items: [
          { id: 'auto', name: '自动', available: true },
          { id: 'builtin', name: '内置解析', available: true },
          { id: 'mineru_local_http', name: 'MinerU', available: false, status: 'disabled' },
        ],
      };
    case 'browser.status':
      return {
        ok: true,
        connected: true,
        clients: [{
          deviceId: 'paw-browser',
          displayName: 'PAW Browser',
          clientKind: 'managed',
          connected: true,
          activeTabId: 23,
        }],
        latestSnapshot: previewBrowserSnapshot(),
        managedBrowser: {
          running: true,
          connected: true,
          controlProtocol: 'ego-browser',
          browserTransport: 'cdp',
          profilePath: '~/Library/Application Support/RagIme/Browser/runtime-profile',
          egoBrowser: {
            available: true,
            hostRunning: true,
            taskSpacesPath: '~/Library/Application Support/RagIme/Browser/ego-browser/spaces.json',
            secondBrowserProcess: false,
          },
        },
      };
    case 'browser.tabs':
      return {
        ok: true,
        items: [{
          deviceId: 'paw-browser',
          tabId: 23,
          title: '浏览器协作指南',
          url: 'https://docs.example.com/browser-guide',
          active: true,
        }],
      };
    case 'browser.snapshot.latest':
      return previewBrowserSnapshot();
    case 'browser.traces':
      return {
        ok: true,
        items: [{
          commandId: 'bcmd-preview',
          action: 'snapshot',
          sourceKind: 'agent',
          status: 'completed',
          target: '浏览器协作指南',
          createdAtMs: Date.now() - 624,
          completedAtMs: Date.now() - 440,
          durationMs: 184,
          result: { summary: '已读取 3 个页面区域和 18 个可交互元素' },
        }],
      };
    case 'browser.command':
    case 'browser.stop':
    case 'browser.managed.start':
    case 'browser.managed.stop':
      return { ok: true };
    case 'input.source.get':
      return {
        ok: true,
        selected: true,
        typingReady: true,
        readinessState: 'ready',
        inputSourceId: 'im.rime.inputmethod.Squirrel.Rime',
      };
    case 'overview.get':
      return {
        ok: true,
        profile: '标准模式',
        components: {
          sidecar: { ok: true, status: 'ready', detail: '后台服务已连接' },
          predictor: { ok: true, status: 'ready', detail: '本机模型已载入' },
          foregroundContext: { ok: true, status: 'ready', detail: '前台上下文按授权读取' },
        },
      };
    case 'diagnostics.models':
      return {
        ok: true,
        configurationPending: false,
        activeConfig: {
          modelId: 'minimind-ime-v2',
          profileId: 'minimind_ime_v2',
          path: '/Library/Application Support/RAG-IME/models/minimind-ime-v2',
          promptMode: 'base-completion',
          maxTokens: 8,
          temperature: 0.15,
          topP: 0.85,
        },
        availableModels: [{
          id: 'minimind-ime-v2',
          active: true,
          profileId: 'minimind_ime_v2',
          path: '/Library/Application Support/RAG-IME/models/minimind-ime-v2',
          promptMode: 'base-completion',
          maxTokens: 8,
          temperature: 0.15,
          topP: 0.85,
        }],
        healthAgreement: { ok: true },
      };
    case 'configuration.settings':
      return previewConfigurationSettings();
    case 'configuration.schema':
      return previewConfigurationSchema();
    default:
      return { ok: true, schemaVersion: 'rag-ime.control-preview.v1' };
  }
}


function previewBrowserSnapshot(): Record<string, unknown> {
  return {
    ok: true,
    snapshotId: 'snap-preview-runtime',
    deviceId: 'paw-browser',
    tabId: 23,
    url: 'https://docs.example.com/browser-guide',
    title: '浏览器协作指南',
    summary: '3 个页面区域 · 18 个可交互元素',
    markdown: [
      '# 浏览器协作指南',
      '网址：https://docs.example.com/browser-guide',
      '当前页面说明如何安全地共享页面内容与操作。',
      '## 页面操作',
      '- [0:e1] 按钮“运行验证”',
      '- [0:e2] 链接“查看执行轨迹”',
      '- [0:e3] 输入框“输入检索问题”',
      '## 安全边界',
      '密码字段不会进入页面快照，跨站导航需要单独批准。',
    ].join('\n'),
    interactiveCount: 18,
    hasScreenshot: false,
    createdAtMs: Date.now() - 2_000,
  };
}

function previewObservationSnapshot(filters: Record<string, unknown> = {}) {
  const now = Date.now();
  const source = [
    {
      category: 'runtime',
      phase: 'turn_completed',
      name: 'turn_completed',
      status: 'completed',
      summary: 'Agent 回合已完成',
      durationMs: 3_842,
      metrics: { messageCount: 4 },
      sequence: 8,
    },
    {
      category: 'memory',
      phase: 'draft_ready',
      name: 'memory_curation',
      status: 'waiting',
      summary: '记忆整理草案已生成，等待审阅',
      durationMs: 1_620,
      metrics: { eventCount: 48, changeCount: 7 },
      sequence: 7,
    },
    {
      category: 'tool',
      phase: 'tool_finished',
      name: 'tool_finished',
      status: 'completed',
      summary: 'ime.memory 已完成',
      durationMs: 486,
      metrics: { argumentFieldCount: 3, resultFieldCount: 5 },
      sequence: 6,
    },
    {
      category: 'retrieval',
      phase: 'retrieval_complete',
      name: 'active_rag_retrieval',
      status: 'completed',
      summary: '闪电联想检索已完成',
      durationMs: 72,
      metrics: { evidenceCount: 9 },
      sequence: 5,
    },
    {
      category: 'context',
      phase: 'started',
      name: 'active_rag_context',
      status: 'completed',
      summary: '闪电联想已捕获上下文元数据',
      durationMs: 0,
      metrics: { selectedChars: 18, contextChars: 126 },
      sequence: 4,
    },
    {
      category: 'intercom',
      phase: 'delivered',
      name: 'participant_activity',
      status: 'completed',
      summary: 'Agent 私信已送达',
      durationMs: 118,
      metrics: {},
      sequence: 3,
    },
    {
      category: 'approval',
      phase: 'approval_required',
      name: 'approval_required',
      status: 'waiting',
      summary: '运行步骤等待用户确认',
      durationMs: null,
      metrics: {},
      sequence: 2,
    },
    {
      category: 'agent',
      phase: 'status_changed',
      name: 'status_changed',
      status: 'running',
      summary: 'Agent 正在分析',
      durationMs: null,
      metrics: {},
      sequence: 1,
    },
  ] as const;
  const allItems = source.map((item) => ({
    schemaVersion: 'rag-ime.observation-event.v1',
    eventType: 'observation',
    eventId: `observation:preview:${item.sequence}`,
    sequence: item.sequence,
    resumeToken: `observation:${item.sequence}`,
    traceId: item.category === 'context' || item.category === 'retrieval'
      ? 'trace:active-rag:preview'
      : item.category === 'intercom'
        ? 'trace:room-turn:preview'
        : 'trace:turn:preview',
    spanId: `span:preview:${item.sequence}`,
    parentSpanId: item.sequence === 1 ? '' : `span:preview:${Math.max(1, item.sequence - 1)}`,
    sessionId: item.category === 'context' || item.category === 'retrieval'
      ? 'active-rag:preview'
      : 'session-preview',
    roomId: item.category === 'intercom' ? 'room-preview' : '',
    turnId: item.category === 'context' || item.category === 'retrieval' ? '' : 'turn-preview',
    runId: item.category === 'memory' ? 'memory_book_preview' : '',
    category: item.category,
    phase: item.phase,
    name: item.name,
    status: item.status,
    summary: item.summary,
    createdAtMs: now - (8 - item.sequence) * 19_000,
    startedAtMs: now - (8 - item.sequence) * 19_000,
    endedAtMs: ['completed', 'failed', 'cancelled'].includes(item.status)
      ? now - (8 - item.sequence) * 19_000
      : null,
    durationMs: item.durationMs,
    privacyClass: 'redacted',
    metrics: item.metrics,
    attributes: { rawTextStored: false },
    refs: [],
  }));
  const items = allItems.filter((item) => (
    (!stringValue(filters.category) || item.category === stringValue(filters.category))
    && (!stringValue(filters.status) || item.status === stringValue(filters.status))
    && (!stringValue(filters.sessionId) || item.sessionId === stringValue(filters.sessionId))
    && (!stringValue(filters.roomId) || item.roomId === stringValue(filters.roomId))
    && (!stringValue(filters.traceId) || item.traceId === stringValue(filters.traceId))
    && (!stringValue(filters.runId) || item.runId === stringValue(filters.runId))
  ));
  const byCategory: Record<string, number> = {};
  const byStatus: Record<string, number> = {};
  for (const item of items) {
    byCategory[item.category] = (byCategory[item.category] ?? 0) + 1;
    byStatus[item.status] = (byStatus[item.status] ?? 0) + 1;
  }
  return {
    schemaVersion: 'rag-ime.observation-snapshot.v1',
    generatedAtMs: now,
    firstSequence: 1,
    lastSequence: 8,
    resumeToken: 'observation:8',
    truncated: false,
    filters: Object.fromEntries(
      Object.entries(filters).filter(([, value]) => stringValue(value)),
    ),
    counts: {
      total: items.length,
      byCategory,
      byStatus,
    },
    items,
  };
}

function previewCanonicalTrace(traceId: string): Record<string, unknown> {
  const now = Date.now();
  const isRag = traceId.includes('active-rag');
  const isRoom = traceId.includes('room-turn');
  const binding = isRag
    ? { sessionId: 'active-rag:preview', caseId: 'case:rag-memory:preview' }
    : isRoom
      ? {
          sessionId: 'session-room-present',
          roomId: 'room-preview',
          turnId: 'turn-room-preview',
          workItemId: 'work:room-preview',
        }
      : {
          sessionId: 'session-preview',
          turnId: 'turn-preview',
          workItemId: 'work:sgg-preview',
          caseId: 'case:sgg-preview',
        };
  const relatedTraceId = isRag ? 'trace:turn:preview' : 'trace:active-rag:preview';
  return {
    schemaVersion: 'rag-ime.observability-trace-get.v1',
    traceId,
    trace: {
      schemaVersion: 'rag-ime.trace-envelope.v1',
      traceId,
      sourceKind: isRag ? 'active_rag' : isRoom ? 'room' : 'agent',
      status: 'completed',
      binding,
      parentTraceId: null,
      links: [{ traceId: relatedTraceId, relation: 'related', targetKind: 'trace' }],
      input: {
        fingerprint: `sha256:${'1'.repeat(64)}`,
        contentPolicy: 'redacted',
        normalization: 'unicode_nfc',
      },
      spans: [
        {
          spanId: `${traceId}:span:turn`,
          name: 'agent.turn',
          parentSpanId: null,
          status: 'completed',
          startedAtMs: now - 2_200,
          endedAtMs: now - 400,
          durationMs: 1_800,
          recorded: true,
          unavailableReason: '',
          metrics: { inputTokens: 1_024, outputTokens: 312 },
          attributes: { provider: 'preview-provider', model: 'preview-agent-v1' },
        },
        {
          spanId: `${traceId}:span:retrieval`,
          name: 'active_rag.retrieve',
          parentSpanId: `${traceId}:span:turn`,
          status: 'completed',
          startedAtMs: now - 1_900,
          endedAtMs: now - 1_600,
          durationMs: 300,
          recorded: true,
          unavailableReason: '',
          metrics: { candidateCount: 12, selectedCount: 3 },
          attributes: { provider: 'preview-rag', lane: 'hybrid' },
        },
        {
          spanId: `${traceId}:span:memory`,
          name: 'memory.recall',
          parentSpanId: `${traceId}:span:retrieval`,
          status: 'completed',
          startedAtMs: now - 1_500,
          endedAtMs: now - 1_300,
          durationMs: 200,
          recorded: true,
          unavailableReason: '',
          metrics: { recalledCount: 2 },
          attributes: { store: 'preview-memory' },
        },
      ],
      evidence: [
        {
          evidenceId: 'evidence:rag:1',
          sourceKind: 'knowledge',
          sourceRef: 'kb:preview-project-docs/chunk:12',
          sourceLane: 'hybrid',
          evidenceStage: 'retrieval_output',
          disposition: 'included',
          scores: { hybrid: 0.91 },
          rankBefore: 2,
          rankAfter: 1,
          omissionReason: '',
        },
        {
          evidenceId: 'evidence:memory:1',
          sourceKind: 'memory',
          sourceRef: 'memory:preview:room-semantics',
          sourceLane: 'lexical',
          evidenceStage: 'retrieval_output',
          disposition: 'omitted',
          scores: { lexical: 0.42 },
          rankBefore: 4,
          rankAfter: null,
          omissionReason: '低于当前回答阈值',
        },
      ],
      artifacts: [
        {
          artifactId: `artifact:${traceId}:eval-report`,
          kind: 'eval_report',
          mediaType: 'application/json',
          sha256: 'b'.repeat(64),
          byteSize: 2_048,
          recordCount: 3,
        },
      ],
      createdAtMs: now - 2_200,
      updatedAtMs: now - 400,
    },
    truncated: false,
    projectionSource: 'observation_journal',
    observationWindow: {
      firstSequence: 1,
      lastSequence: 8,
      resumeToken: 'observation:8',
      nextBeforeSequence: null,
    },
  };
}

function previewEvalList(
  traceId: string,
  additionalItems: Record<string, unknown>[] = [],
): Record<string, unknown> {
  const now = Date.now();
  const suite = previewSuiteForTrace(traceId);
  const items = [
    ...(suite ? [{
        evalRunId: `eval:${suite.suiteId}:preview`,
        mode: 'ground_truth',
        metricAuthority: 'ground_truth',
        truthStatus: 'frozen',
        datasetId: `dataset:${suite.suiteId}:fixture`,
        labelRevision: suite.suiteRevision,
        evaluatorDisplayName: suite.displayName,
        suiteBinding: {
          suiteId: suite.suiteId,
          suiteRevision: suite.suiteRevision,
        },
        metrics: { evidenceRecall: 0.92, answerGrounding: 0.88 },
        status: 'completed',
        createdAtMs: now - 1_800,
        updatedAtMs: now - 1_700,
      }] : []),
    {
      evalRunId: suite ? `eval:ai-judge:${suite.suiteId}:preview` : 'eval:ai-judge:rag-preview',
      mode: 'ai_judge',
      metricAuthority: 'ai_judge_estimate',
      truthStatus: 'none',
      datasetId: suite ? `dataset:${suite.suiteId}:judge` : 'dataset:rag-memory:trace-evidence',
      labelRevision: 'judge:preview',
      evaluatorDisplayName: suite ? 'Preview AI Judge' : 'Preview AI Judge · RAG/Memory',
      metrics: { qualityEstimate: 0.84 },
      status: 'completed',
      createdAtMs: now - 1_600,
      updatedAtMs: now - 1_500,
    },
    ...additionalItems,
  ];
  return {
    schemaVersion: 'rag-ime.observability-eval-list.v1',
    traceId,
    total: items.length,
    truncated: false,
    items,
  };
}

function previewEvidenceEvalRun(
  body: Record<string, unknown>,
  traceId: string,
  existingCount: number,
): Record<string, unknown> {
  const now = Date.now();
  const datasetId = stringValue(body.datasetId) || `manual:${traceId}`;
  const labelRevision = stringValue(body.labelRevision) || 'manual:1';
  return {
    schemaVersion: 'rag-ime.eval-run.v1',
    evalRunId: `eval:human:${traceId}:${existingCount + 1}`,
    traceIds: [traceId],
    mode: 'ground_truth',
    metricAuthority: 'ground_truth',
    truth: { status: 'human', datasetId, labelRevision },
    evaluator: {
      provider: 'human',
      model: 'manual',
      thinking: 'manual',
      displayName: '人工标注',
    },
    metrics: { evidenceRecall: 1, evidencePrecision: 1 },
    status: 'completed',
    createdAtMs: now,
    updatedAtMs: now,
  };
}

function previewEvalSummary(run: Record<string, unknown>): Record<string, unknown> {
  const truth = record(run.truth);
  const evaluator = record(run.evaluator);
  const mode = stringValue(run.mode) === 'ai_judge' ? 'ai_judge' : 'ground_truth';
  return {
    evalRunId: stringValue(run.evalRunId),
    mode,
    metricAuthority: mode === 'ai_judge' ? 'ai_judge_estimate' : 'ground_truth',
    truthStatus: stringValue(truth.status) || 'human',
    datasetId: stringValue(truth.datasetId),
    labelRevision: stringValue(truth.labelRevision),
    evaluatorDisplayName: stringValue(evaluator.displayName) || '人工标注',
    metrics: record(run.metrics),
    status: stringValue(run.status) || 'completed',
    createdAtMs: Number(run.createdAtMs) || Date.now(),
    updatedAtMs: Number(run.updatedAtMs) || Date.now(),
  };
}

function previewScheduledEvalSummary({
  evalRunId,
  suiteId,
  suiteRevision,
}: {
  evalRunId: string;
  suiteId: string;
  suiteRevision: string;
}): Record<string, unknown> {
  const now = Date.now();
  const evaluatorDisplayName = suiteId === 'zhanggui-wenshu'
    ? '掌柜问数'
    : suiteId === 'sgg'
      ? 'SGG 示例垂直 Agent'
      : '垂直 Agent 自测';
  return {
    evalRunId,
    mode: 'ground_truth',
    metricAuthority: 'ground_truth',
    truthStatus: 'frozen',
    datasetId: `dataset:${suiteId}:fixture`,
    labelRevision: suiteRevision,
    evaluatorDisplayName,
    suiteBinding: { suiteId, suiteRevision },
    metrics: { evidencePrecision: 1, evidenceRecall: 1, evidenceF1: 1 },
    status: 'completed',
    createdAtMs: now,
    updatedAtMs: now,
  };
}

function previewSuiteForTrace(traceId: string): {
  suiteId: string;
  suiteRevision: string;
  displayName: string;
} | null {
  if (traceId.includes('active-rag')) {
    return null;
  }
  if (traceId.includes('room-turn')) {
    return { suiteId: 'zhanggui-wenshu', suiteRevision: 'fixture-v2', displayName: '掌柜问数' };
  }
  return { suiteId: 'sgg', suiteRevision: 'fixture-v2', displayName: 'SGG 示例垂直 Agent' };
}

function previewEvalSuiteList(): Record<string, unknown> {
  return {
    schemaVersion: 'rag-ime.eval-suite-list.v1',
    ok: true,
    items: [
      {
        suiteId: 'sgg',
        suiteRevision: 'fixture-v2',
        displayName: 'SGG 示例垂直 Agent',
        fixtureCount: 2,
        capabilities: ['trace.emit', 'rag.retrieval', 'memory.recall', 'sandbox.self_test', 'eval.ground_truth'],
      },
      {
        suiteId: 'zhanggui-wenshu',
        suiteRevision: 'fixture-v2',
        displayName: '掌柜问数',
        fixtureCount: 2,
        capabilities: ['trace.emit', 'rag.retrieval', 'memory.recall', 'sandbox.self_test', 'eval.ground_truth'],
      },
    ],
  };
}

function previewEvalSchedule(
  id: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  const now = Date.now();
  const nextDueAtMs = now + 60 * 60 * 1_000;
  const suiteId = stringValue(overrides.suiteId) || 'sgg';
  const suiteRevision = stringValue(overrides.suiteRevision) || 'fixture-v2';
  const latestRun = suiteId === 'sgg' && id === 'eval-schedule:sgg'
    ? {
        id: `${id}:run:1`,
        scheduleId: id,
        attempt: 1,
        state: 'succeeded',
        dueAtMs: now - 3_600_000,
        claimedAtMs: now - 3_599_000,
        finishedAtMs: now - 3_598_000,
        evalRunId: 'eval:sgg:preview',
        errorCode: '',
      }
    : {};
  const requestedLatestRun = record(overrides.latestRun);
  const resolvedLatestRun = Object.keys(requestedLatestRun).length
    ? requestedLatestRun
    : latestRun;
  const requestedStatus = stringValue(overrides.status);
  const status = requestedStatus === 'running'
    || requestedStatus === 'completed'
    || requestedStatus === 'failed'
    ? requestedStatus
    : 'scheduled';
  return {
    id,
    suiteId,
    suiteRevision,
    recurrenceKind: overrides.recurrenceKind === 'weekly' ? 'weekly' : 'daily',
    recurrenceInterval: Number(overrides.recurrenceInterval) || 1,
    maxRuns: Number(overrides.maxRuns) || 30,
    runCount: Number(overrides.runCount) || (Object.keys(latestRun).length ? 1 : 0),
    status,
    initialDueAtMs: Number(overrides.initialDueAtMs) || nextDueAtMs,
    nextDueAtMs: Number(overrides.nextDueAtMs) || nextDueAtMs,
    lastErrorCode: '',
    createdAtMs: Number(overrides.createdAtMs) || now - 3_600_000,
    updatedAtMs: Number(overrides.updatedAtMs) || now,
    latestRun: resolvedLatestRun,
  };
}

function previewEvalScheduleRun(
  scheduleId: string,
  evalRunId: string,
  traceId: string,
): Record<string, unknown> {
  const now = Date.now();
  return {
    id: `${scheduleId}:run:1`,
    scheduleId,
    attempt: 1,
    state: 'succeeded',
    dueAtMs: now - 3_600_000,
    claimedAtMs: now - 3_599_000,
    finishedAtMs: now - 3_598_000,
    evalRunId,
    errorCode: '',
    traceIds: [traceId],
    traceIdsTruncated: false,
  };
}

function previewEvalLatestRun(run: Record<string, unknown>): Record<string, unknown> {
  return {
    id: run.id,
    scheduleId: run.scheduleId,
    attempt: run.attempt,
    state: run.state,
    dueAtMs: run.dueAtMs,
    claimedAtMs: run.claimedAtMs,
    finishedAtMs: run.finishedAtMs,
    evalRunId: run.evalRunId,
    errorCode: run.errorCode,
  };
}


function previewKnowledgeBase(): Record<string, unknown> {
  return {
    id: 'kb:preview-project-docs',
    name: '伙伴运行资料',
    description: '独立加载的项目文档与上游源码笔记。',
    documentCount: 1,
    chunkCount: 36,
    status: 'ready',
    agentEnabled: true,
    parserMode: 'auto',
    revision: 1,
    updatedAtMs: Date.now() - 120_000,
  };
}

function previewKnowledgeBasesFixture(): Record<string, unknown>[] {
  return [
    previewKnowledgeBase(),
    {
      id: 'kb:preview-google-earth',
      name: '谷歌地球资料库',
      description: 'Google Earth 地理、遥感与地图资料测试库。',
      documentCount: 1,
      chunkCount: 28,
      status: 'ready',
      agentEnabled: true,
      parserMode: 'auto',
      revision: 1,
      updatedAtMs: Date.now() - 210_000,
    },
    {
      id: 'kb:preview-antarctic-papers',
      name: '南极论文资料库',
      description: '本周从 Zotero 接入的南极冰盖、冰架、冰川与冰流速论文；198 个 PDF 附件，等待逐项解析。',
      documentCount: 198,
      chunkCount: 0,
      status: 'building',
      agentEnabled: true,
      parserMode: 'auto',
      revision: 1,
      updatedAtMs: Date.now() - 240_000,
    },
  ];
}

function previewWorkflowState(sessionId: string): AgentWorkflowStateV1 {
  const now = Date.now();
  return {
    schemaVersion: 'rag-ime.agent-workflow-state.v1',
    ok: true,
    sessionId,
    todo: {
      schemaVersion: 'rag-ime.agent-todo.v1',
      id: `todo:${sessionId}`,
      sessionId,
      revision: 2,
      actor: 'agent',
      updatedAtMs: now,
      roomLineage: null,
      phases: [
        {
          name: '实现',
          tasks: [
            { content: '核对 Todo 与 Goal 契约', status: 'completed' },
            { content: '完成前端状态同步', status: 'in_progress' },
          ],
        },
        {
          name: '验证',
          tasks: [
            { content: '核对真实 Provider 载荷', status: 'blocked', reason: '等待真实 Provider 环境' },
          ],
        },
      ],
      counts: {
        total: 3,
        pending: 0,
        inProgress: 1,
        blocked: 1,
        completed: 1,
        abandoned: 0,
      },
    },
    goal: {
      schemaVersion: 'rag-ime.agent-goal.v1',
      sessionId,
      configured: true,
      goalId: `goal:${sessionId}`,
      revision: 1,
      objective: '在明确预算内完成 Agent 工作流，并留下可复现的验证证据。',
      successCriteria: 'Todo 全部收束，并通过用户验收。',
      evidenceExpectations: ['聚焦测试结果', '可定位的产物或变更记录'],
      status: 'active',
      budget: { tokenLimit: 48_000, timeLimitMs: 3_600_000 },
      usage: { tokens: 14_600, elapsedMs: 1_080_000 },
      remaining: { tokens: 33_400, timeMs: 2_520_000 },
      budgetExceeded: false,
      completionAudit: null,
      cancellationAudit: null,
      updatedAtMs: now,
    },
    actGate: {
      allowed: true,
      reason: 'user_execution_request',
      message: '用户已请求执行，可在已授权工作区内继续；高风险操作仍需逐项审批。',
      todoRevision: 2,
      goalRevision: 1,
    },
  };
}

function withPreviewWorkflowSession(
  workflow: AgentWorkflowStateV1,
  sessionId: string,
): AgentWorkflowStateV1 {
  if (workflow.sessionId === sessionId) return workflow;
  return {
    ...workflow,
    sessionId,
    todo: {
      ...workflow.todo,
      id: `todo:${sessionId}`,
      sessionId,
    },
    goal: {
      ...workflow.goal,
      sessionId,
      goalId: workflow.goal.configured ? `goal:${sessionId}` : '',
    },
  };
}

function mutatePreviewGoal(
  workflow: AgentWorkflowStateV1,
  body: Record<string, unknown>,
): AgentWorkflowStateV1 {
  const action = stringValue(body.action);
  if (action === 'clear') {
    const empty = previewWorkflowState(workflow.sessionId).goal;
    const goal: AgentWorkflowStateV1['goal'] = {
      ...empty,
      configured: false,
      goalId: '',
      revision: workflow.goal.revision + 1,
      objective: '',
      successCriteria: '',
      evidenceExpectations: [],
      status: 'cleared' as const,
      budget: { tokenLimit: null, timeLimitMs: null },
      usage: { tokens: 0, elapsedMs: 0 },
      remaining: { tokens: null, timeMs: null },
      completionAudit: null,
      cancellationAudit: null,
    };
    return { ...workflow, goal, actGate: previewActGate(workflow.todo, goal) };
  }
  const now = Date.now();
  let status = workflow.goal.status;
  if (action === 'confirm_setup' || action === 'update' || action === 'resume') status = 'active';
  if (action === 'pause') status = 'paused';
  if (action === 'complete') status = 'completed';
  if (action === 'cancel') status = 'cancelled';
  const tokenLimit = optionalPreviewNumber(body.tokenBudget, workflow.goal.budget.tokenLimit);
  const timeLimitMs = optionalPreviewNumber(body.timeBudgetMs, workflow.goal.budget.timeLimitMs);
  const evidence = Array.isArray(body.evidence)
    ? body.evidence.map((item) => {
      const value = record(item);
      const kind = stringValue(value.kind);
      return {
        kind: ['test', 'artifact', 'commit', 'receipt', 'note'].includes(kind) ? kind : 'note',
        summary: stringValue(value.summary) || 'Preview evidence',
        reference: stringValue(value.reference) || 'preview',
      };
    })
    : [];
  const evidenceExpectations: AgentWorkflowStateV1['goal']['evidenceExpectations'] = Array.isArray(body.evidenceExpectations)
    ? (body.evidenceExpectations.map(stringValue).filter(Boolean).slice(0, 20) as AgentWorkflowStateV1['goal']['evidenceExpectations'])
    : workflow.goal.evidenceExpectations;
  const goal: AgentWorkflowStateV1['goal'] = {
    ...workflow.goal,
    configured: true,
    goalId: workflow.goal.goalId || `goal:${workflow.sessionId}`,
    revision: workflow.goal.revision + 1,
    objective: stringValue(body.objective) || workflow.goal.objective || 'Preview Goal',
    successCriteria: stringValue(body.successCriteria) || workflow.goal.successCriteria,
    evidenceExpectations,
    status,
    budget: { tokenLimit, timeLimitMs },
    remaining: {
      tokens: tokenLimit === null ? null : Math.max(0, tokenLimit - workflow.goal.usage.tokens),
      timeMs: timeLimitMs === null ? null : Math.max(0, timeLimitMs - workflow.goal.usage.elapsedMs),
    },
    completionAudit: action === 'complete'
      ? {
        auditId: `goal-audit:${now}`,
        summary: stringValue(body.summary) || 'Preview Goal 已完成。',
        evidence: (evidence.length ? evidence : [{
          kind: 'note',
          summary: 'Preview 完成回执',
          reference: 'preview',
        }]) as NonNullable<AgentWorkflowStateV1['goal']['completionAudit']>['evidence'],
        completedBy: 'user',
        createdAtMs: now,
      }
      : workflow.goal.completionAudit,
    cancellationAudit: action === 'cancel'
      ? {
        auditId: `goal-cancellation:${now}`,
        reason: stringValue(body.reason) || 'Preview 中取消。',
        cancelledBy: 'user',
        createdAtMs: now,
      }
      : workflow.goal.cancellationAudit,
    updatedAtMs: now,
  };
  return { ...workflow, goal, actGate: previewActGate(workflow.todo, goal) };
}

function previewActGate(
  todo: AgentWorkflowStateV1['todo'],
  goal: AgentWorkflowStateV1['goal'],
): AgentWorkflowStateV1['actGate'] {
  const revisions = {
    todoRevision: todo.revision,
    goalRevision: goal.revision,
  };
  if (goal.status === 'paused') {
    return { allowed: false, reason: 'goal_paused', message: 'Goal 已暂停。', ...revisions };
  }
  if (goal.status === 'completed') {
    return { allowed: false, reason: 'goal_completed', message: 'Goal 已完成。', ...revisions };
  }
  if (goal.status === 'cancelled') {
    return { allowed: false, reason: 'goal_cancelled', message: 'Goal 已取消。', ...revisions };
  }
  if (goal.budgetExceeded) {
    return { allowed: false, reason: 'goal_budget_exhausted', message: 'Goal 预算已用尽。', ...revisions };
  }
  return {
    allowed: true,
    reason: 'user_execution_request',
    message: '用户已请求执行，可在已授权工作区内继续；高风险操作仍需逐项审批。',
    ...revisions,
  };
}

function optionalPreviewNumber(value: unknown, fallback: number | null): number | null {
  if (value === null) return null;
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : fallback;
}

const previewSkillBodies: Record<string, string> = {
  'timeline-inspector.skill': [
    '# Timeline Inspector',
    '',
    'Use this Skill to inspect a session timeline before writing a handoff.',
    '',
    '## Steps',
    '',
    '1. Read the timeline summary.',
    '2. Identify the first user-visible regression.',
    '3. Link the relevant trace evidence in the handoff.',
    '',
  ].join('\n'),
  'incident-response': [
    '# Incident Response',
    '',
    'Use this Skill to turn an incident into a bounded, evidence-backed repair plan.',
    '',
    '## Guardrails',
    '',
    '- Preserve the original evidence before changing state.',
    '- Separate observed facts from hypotheses.',
    '',
  ].join('\n'),
  'release-check': [
    '# Release Check',
    '',
    'Use this project Skill to review release evidence before publishing.',
    '',
    '## Checklist',
    '',
    '- Confirm the changed surface was exercised.',
    '- Record the exact artifact and revision.',
    '',
  ].join('\n'),
};

function previewSkillItems(): Record<string, unknown>[] {
  return [
    {
      skillId: 'timeline-inspector.skill',
      name: 'Timeline Inspector',
      description: '检查会话时间线，并把首个用户可见回归连接到交接证据。',
      sourceKind: 'package',
      packageId: 'timeline-inspector',
      packageVersion: '1.0.0',
      resourcePath: 'skills/timeline-inspector/SKILL.md',
      enabled: true,
      installed: true,
      installState: 'enabled',
      digest: `sha256:${'1'.repeat(64)}`,
      contentRevision: `sha256:${'1'.repeat(64)}`,
      sizeBytes: 412,
      management: 'package',
      managementReason: 'Skill 生命周期属于整个 Pi Package；变更会在 Package 范围内预览并确认。',
      actions: ['enable', 'disable', 'update', 'uninstall'],
    },
    {
      skillId: 'incident-response',
      name: 'Incident Response',
      description: '把事故整理为有边界、可核验的修复计划。',
      sourceKind: 'bundled',
      resourcePath: 'integrations/pi/skills/incident-response/SKILL.md',
      enabled: null,
      installed: true,
      installState: 'bundled',
      digest: `sha256:${'2'.repeat(64)}`,
      contentRevision: `sha256:${'2'.repeat(64)}`,
      sizeBytes: 328,
      management: 'inspect_only',
      managementReason: 'Bundled Skill 由 Pi Runtime 提供，没有独立的启用开关。',
      actions: [],
    },
    {
      skillId: 'release-check',
      name: 'Release Check',
      description: '在发布前检查项目 Skill 的变更证据。',
      sourceKind: 'project',
      resourcePath: 'skills/release-check/SKILL.md',
      enabled: null,
      installed: true,
      installState: 'project',
      digest: `sha256:${'3'.repeat(64)}`,
      contentRevision: `sha256:${'3'.repeat(64)}`,
      sizeBytes: 286,
      management: 'inspect_only',
      managementReason: 'Project Skill 由工作区发现，没有独立的 Package 生命周期。',
      actions: [],
    },
    {
      skillId: 'systematic-debugging',
      name: 'systematic-debugging',
      description: '通过可复现证据定位跨层或间歇性故障。',
      sourceKind: 'bundled',
      resourcePath: 'integrations/pi/skills/systematic-debugging/SKILL.md',
      enabled: null,
      installed: true,
      installState: 'bundled',
      digest: `sha256:${'4'.repeat(64)}`,
      contentRevision: `sha256:${'4'.repeat(64)}`,
      sizeBytes: 364,
      management: 'inspect_only',
      managementReason: 'Bundled Skill 由 Pi Runtime 提供，没有独立的启用开关。',
      actions: [],
    },
    {
      skillId: 'facilitate-room',
      name: 'facilitate-room',
      description: '负责 Room 内的协作编排、工作分配与结果收束。',
      sourceKind: 'bundled',
      resourcePath: 'integrations/pi/skills/facilitate-room/SKILL.md',
      enabled: null,
      installed: true,
      installState: 'bundled',
      digest: `sha256:${'5'.repeat(64)}`,
      contentRevision: `sha256:${'5'.repeat(64)}`,
      sizeBytes: 392,
      management: 'inspect_only',
      managementReason: 'Bundled Skill 由 Pi Runtime 提供，没有独立的启用开关。',
      actions: [],
    },
    {
      skillId: 'trace-agent-diagnostics',
      name: 'trace-agent-diagnostics',
      description: '为 Trace Agent 提供诊断、证据核对与受控修复流程。',
      sourceKind: 'bundled',
      resourcePath: 'integrations/pi/skills/trace-agent-diagnostics/SKILL.md',
      enabled: null,
      installed: true,
      installState: 'bundled',
      digest: `sha256:${'6'.repeat(64)}`,
      contentRevision: `sha256:${'6'.repeat(64)}`,
      sizeBytes: 408,
      management: 'inspect_only',
      managementReason: 'Bundled Skill 由 Pi Runtime 提供，没有独立的启用开关。',
      actions: [],
    },
    {
      skillId: 'agent-eval-room-optimizer',
      name: 'agent-eval-room-optimizer',
      description: '为 Agent Lab 的评测实验优化协作策略与证据回路。',
      sourceKind: 'bundled',
      resourcePath: 'integrations/pi/skills/agent-eval-room-optimizer/SKILL.md',
      enabled: null,
      installed: true,
      installState: 'bundled',
      digest: `sha256:${'7'.repeat(64)}`,
      contentRevision: `sha256:${'7'.repeat(64)}`,
      sizeBytes: 416,
      management: 'inspect_only',
      managementReason: 'Bundled Skill 由 Pi Runtime 提供，没有独立的启用开关。',
      actions: [],
    },
  ];
}

function previewInstalledExtensionItems(): Record<string, unknown>[] {
  return [{
    id: 'timeline-inspector',
    displayName: 'Timeline Inspector',
    version: '1.0.0',
    previousVersion: '0.9.0',
    enabled: true,
    rollbackAvailable: true,
    resources: { extensions: ['extensions/timeline-inspector.ts'], skills: [], prompts: [], themes: [] },
    source: { kind: 'bundled', requested: 'timeline-inspector', resolved: 'timeline-inspector@1.0.0' },
  }, {
    // The source-isolated Extension App is present in the checkout and its
    // Pi package is bundled by the managed-runtime builder. Keep the preview
    // activation inventory in sync so Launchpad/App Center can discover it.
    id: '@paw/zhanggui-wenshu',
    displayName: '掌柜问数',
    description: '对经营数据提问、对账并解释差异',
    version: '0.2.0',
    enabled: true,
    installed: true,
    rollbackAvailable: false,
    resources: {
      extensions: [],
      skills: ['skills/zhanggui-wenshu/SKILL.md'],
      prompts: [],
      themes: [],
    },
    source: { kind: 'bundled', requested: '@paw/zhanggui-wenshu', resolved: '@paw/zhanggui-wenshu@0.2.0' },
  }];
}

function previewPiPackageIdentity(source: string): {
  id: string;
  displayName: string;
  version: string;
  kind: 'npm' | 'git' | 'local';
} {
  if (!source) return { id: '', displayName: '', version: '', kind: 'local' };
  const kind = source.startsWith('/') || source.startsWith('./') || source.startsWith('../') || source.startsWith('~/')
    ? 'local'
    : source.startsWith('git:') || source.startsWith('git+') || source.startsWith('github:') || source.includes('github.com/')
      ? 'git'
      : 'npm';
  let name = source.replace(/^npm:/u, '').replace(/\/$/u, '');
  let version = '';
  if (kind === 'npm') {
    const versionSeparator = name.startsWith('@')
      ? name.indexOf('@', name.indexOf('/') + 1)
      : name.lastIndexOf('@');
    if (versionSeparator > 0) {
      version = name.slice(versionSeparator + 1);
      name = name.slice(0, versionSeparator);
    }
  } else {
    name = name.split('/').filter(Boolean).at(-1)?.replace(/\.git$/u, '') || 'pi-package-preview';
  }
  const id = name
    .toLocaleLowerCase('en-US')
    .replace(/^@/u, '')
    .replaceAll('/', '.')
    .replace(/[^a-z0-9._-]+/gu, '-')
    .replace(/^[^a-z0-9]+|[^a-z0-9]+$/gu, '')
    .slice(0, 64) || 'pi-package-preview';
  return { id, displayName: name, version, kind };
}

function previewExtensionCatalogItems(
  installed: Record<string, unknown>[],
): Record<string, unknown>[] {
  const isInstalled = installed.some((item) => stringValue(item.id) === 'session-review');
  return [{
    id: 'session-review',
    displayName: 'Session Review',
    description: '在项目完成时整理可核验事实，并生成下一轮可消费的复盘建议。',
    publisher: 'Personal Agent Workbench',
    source: { kind: 'bundled', label: 'Product bundle' },
    permissions: ['session.read', 'memory.review'],
    security: { notes: '仅生成审阅建议，不直接写入长期记忆。' },
    versions: [{ version: '1.1.0' }, { version: '1.0.0' }],
    latestVersion: '1.1.0',
    installed: isInstalled,
    updateAvailable: false,
    actionable: true,
    enabled: isInstalled,
  }];
}

function previewExtensionProposal(): Record<string, unknown> {
  return {
    proposalId: 'proposal:preview-session-review',
    previewToken: 'proposal-preview-token',
    payloadSha256: 'd'.repeat(64),
    summary: {
      action: 'install',
      pluginId: 'session-review',
      displayName: 'Session Review',
    },
  };
}

function applyPreviewExtensionChange(
  installed: Record<string, unknown>[],
  change: Record<string, unknown>,
): Record<string, unknown>[] {
  const action = stringValue(change.action);
  const pluginId = stringValue(change.pluginId);
  if (!pluginId) return installed;
  const existing = installed.find((item) => stringValue(item.id) === pluginId);
  if (action === 'install' || action === 'update') {
    const next = {
      id: pluginId,
      displayName: stringValue(change.displayName) || pluginId,
      version: '1.1.0',
      previousVersion: action === 'update' ? stringValue(existing?.version) : '',
      enabled: change.enable !== false,
      rollbackAvailable: action === 'update' && Boolean(existing),
    };
    return existing
      ? installed.map((item) => stringValue(item.id) === pluginId ? next : item)
      : [...installed, next];
  }
  if (!existing) return installed;
  if (action === 'uninstall') {
    return installed.filter((item) => stringValue(item.id) !== pluginId);
  }
  if (action === 'enable' || action === 'disable') {
    return installed.map((item) => stringValue(item.id) === pluginId
      ? { ...item, enabled: action === 'enable' }
      : item);
  }
  if (action === 'rollback') {
    if (existing.rollbackAvailable !== true) {
      throw new Error('这个扩展当前没有可恢复的上一版本。');
    }
    const rollbackVersion = stringValue(change.version)
      || stringValue(existing.previousVersion);
    if (!rollbackVersion) {
      throw new Error('这个扩展缺少可核验的上一版本，未执行恢复。');
    }
    return installed.map((item) => stringValue(item.id) === pluginId
      ? {
        ...item,
        version: rollbackVersion,
        previousVersion: '',
        rollbackAvailable: false,
      }
      : item);
  }
  return installed;
}
function applyPreviewSkillChange(
  skills: Record<string, unknown>[],
  change: Record<string, unknown>,
): Record<string, unknown>[] {
  const action = stringValue(change.action);
  const packageId = stringValue(change.pluginId);
  if (!packageId) return skills;
  return skills.map((skill) => {
    if (stringValue(skill.packageId) !== packageId || stringValue(skill.sourceKind) !== 'package') {
      return skill;
    }
    if (action === 'uninstall') {
      return { ...skill, enabled: false, installed: false, installState: 'uninstalled' };
    }
    if (action === 'enable' || action === 'disable') {
      return {
        ...skill,
        enabled: action === 'enable',
        installed: true,
        installState: action === 'enable' ? 'enabled' : 'disabled',
      };
    }
    if (action === 'update') {
      return {
        ...skill,
        packageVersion: stringValue(change.version) || '1.1.0',
        enabled: true,
        installed: true,
        installState: 'enabled',
      };
    }
    if (action === 'install') {
      return { ...skill, enabled: change.enable !== false, installed: true, installState: change.enable === false ? 'disabled' : 'enabled' };
    }
    return skill;
  });
}


function previewLifecyclePolicyItems(): Record<string, unknown>[] {
  return [
    ['session_start', false, 'audit_only', 0, 0],
    ['turn_end', false, 'audit_only', 0, 0],
    ['compaction', true, 'context_checkpoint', 192, 60],
    ['project_complete', true, 'memory_review_suggestion', 256, 300],
    ['tool_failed', true, 'context_checkpoint', 128, 60],
    ['idle', false, 'memory_review_suggestion', 160, 1_800],
  ].map(([eventType, enabled, action, tokenLimit, cooldownSeconds]) => ({
    eventType,
    enabled,
    action,
    tokenLimit,
    cooldownSeconds,
  }));
}

function previewLifecycleEventItems(): Record<string, unknown>[] {
  return [{
    eventId: 'lifecycle:preview-project-complete',
    eventType: 'project_complete',
    sessionId: 'session-preview',
    status: 'suggested',
    createdAtMs: Date.now() - 120_000,
  }];
}

function previewSubagentConsole(runId: string): Record<string, unknown> {
  const batch = previewSubagentBatch();
  const runs = Array.isArray(batch.runs) ? batch.runs.map(record) : [];
  const run = runs.find((item) => stringValue(item.id) === runId) ?? runs[0] ?? {};
  const running = stringValue(run.state) === 'running';
  return {
    schemaVersion: 'rag-ime.agent-subagent-console.v1',
    ok: true,
    run,
    capabilities: {
      steer: { available: running, reason: running ? '' : '任务已经结束' },
      retry: { available: !running, reason: running ? '等待当前任务结束' : '' },
      resume: { available: !running, reason: running ? '任务仍在运行' : '' },
      abort: { available: running, reason: running ? '' : '任务已经结束' },
      reply: { available: true, reason: '' },
    },
    conversation: {
      availability: 'available',
      source: 'active_runtime',
      items: [
        {
          id: 'preview-subagent:user',
          role: 'user',
          createdAtMs: Date.now() - 70_000,
          blocks: [{ type: 'text', data: { text: stringValue(run.task) } }],
        },
        {
          id: 'preview-subagent:assistant',
          role: 'assistant',
          createdAtMs: Date.now() - 12_000,
          blocks: [{ type: 'text', data: { text: '已完成契约核对，正在补齐前端验证证据。' } }],
        },
      ],
    },
    activity: [
      {
        id: 'preview-subagent:activity:1',
        eventType: 'tool_started',
        createdAtMs: Date.now() - 40_000,
        payload: { toolName: 'read' },
      },
      {
        id: 'preview-subagent:activity:2',
        eventType: running ? 'checkpoint' : 'completed',
        createdAtMs: Date.now() - 8_000,
        payload: { summary: running ? '已保存一次可恢复进度' : '任务已经交付' },
      },
    ],
    inbox: [{
      id: 'preview-subagent:inbox:1',
      kind: 'progress',
      title: '前端核对进度',
      message: 'Preview fixture 已接通，等待主 Agent 验收。',
      status: 'recorded',
      createdAtMs: Date.now() - 8_000,
    }],
    controls: [],
  };
}

function previewSubagentBatch(sessionId = 'session-preview'): Record<string, unknown> {
  const now = Date.now();
  const rootId = 'room-preview:root-preview';
  const roomTask = sessionId === 'session-room-present'
    ? {
        taskId: `${rootId}:task-integration`,
        dispatchId: `${rootId}:dispatch-integration`,
        templateId: 'worker' as const,
        task: '整合并行检查结果并核对 Control API 边界',
        state: 'running' as const,
      }
    : sessionId === 'session-room-firstlight'
      ? {
          taskId: `${rootId}:task-data`,
          dispatchId: `${rootId}:dispatch-data`,
          templateId: 'researcher' as const,
          task: '核对数据投影与状态响应边界',
          state: 'completed' as const,
        }
      : sessionId === 'session-room-future'
        ? {
            taskId: `${rootId}:task-review`,
            dispatchId: `${rootId}:dispatch-review`,
            templateId: 'reviewer' as const,
            task: '独立复核多端回放证据',
            state: 'queued' as const,
          }
        : null;
  const batchId = roomTask
    ? `subagent-batch:preview:${sessionId}`
    : 'subagent-batch:preview';
  const budget = {
    maxTurns: 10,
    maxToolCalls: 18,
    maxTotalTokens: 32_000,
    maxDurationMs: 300_000,
    maxOutputChars: 24_000,
  };
  const run = (
    id: string,
    templateId: 'researcher' | 'worker' | 'reviewer',
    task: string,
    state: 'queued' | 'running' | 'completed',
    ordinal: number,
    lineage: { parentRunId?: string; depth?: 1 | 2 } = {},
  ) => ({
    schemaVersion: 'rag-ime.agent-subagent-run.v1',
    id,
    nodeId: `node:${id}`,
    attemptId: `attempt:${id}:1`,
    attemptNumber: 1,
    predecessorAttemptId: '',
    ownerRunId: lineage.parentRunId || (roomTask ? `room-parent:${sessionId}` : `session:${sessionId}`),
    parentRunId: lineage.parentRunId || (roomTask ? `room-parent:${sessionId}` : ''),
    depth: lineage.depth ?? 1,
    batchId,
    childSessionId: `subagent-runtime:${id}`,
    todoTask: roomTask ? '完成当前协作任务' : '核对研究证据',
    todoPhase: roomTask ? '协作' : '验证',
    templateId,
    templateVersion: '1',
    ordinal,
    task,
    expectedOutput: roomTask ? '可复核的任务内协作结论' : '可复核的状态投影证据',
    acceptanceCriteria: roomTask
      ? ['结果归入当前 Room 任务', '公开投影不包含私有会话内容']
      : ['状态与工具生命周期边界清晰'],
    outputSchema: {},
    launchDigest: {
      schemaVersion: 'rag-ime.agent-subagent-launch-digest.v1',
      contextMode: lineage.parentRunId || roomTask ? 'fork' : 'fresh',
      templateId,
      templateVersion: '1',
      modelProfile: 'gpt-5.4',
      thinkingLevel: 'medium',
      toolProfileVersion: templateId === 'worker' ? 'control-center-v1' : 'subagent-readonly-v1',
      toolAllowlistMode: 'profile',
      tools: templateId === 'worker'
        ? ['workspace_read', 'workspace_shell']
        : ['workspace_read', 'knowledge_search'],
      piSkillsEnabled: true,
      codexSkillsEnabled: true,
      workspaceAccess: templateId === 'worker' ? 'write' : 'read_only',
      workspaceRootCount: 1,
      outputContract: { required: true, schemaSha256: 'd'.repeat(64) },
      extensionRuntime: 'pi_host_managed',
    },
    contract: {
      status: state === 'completed' ? 'valid' : 'pending',
      error: '',
      toolCallId: state === 'completed' ? `tool:${id}:structured-output` : '',
      validatedAtMs: state === 'completed' ? now - 92_000 : null,
    },
    structuredOutput: state === 'completed'
      ? { summary: '状态、来源与交付边界已经通过结构化校验。' }
      : undefined,
    state,
    budget,
    usage: state === 'completed'
      ? { turnCount: 3, toolCount: 5, totalTokens: 4_820 }
      : state === 'running'
        ? { turnCount: 2, toolCount: 3, totalTokens: 2_140 }
        : { turnCount: 0, toolCount: 0, totalTokens: 0 },
    result: state === 'completed' ? { summary: '已核对来源与结论，结果已经交回负责人。' } : {},
    error: '',
    artifact: {
      schemaVersion: 'rag-ime.agent-artifact-ref.v1',
      artifactId: `artifact:${id}`,
      ownerKind: 'subagent_run',
      ownerId: id,
      kind: 'lifecycle',
      sha256: 'a'.repeat(64),
    },
    supervision: { phase: 'none', reason: '', requestedAtMs: null, graceMs: 0 },
    resultContextScheduledAtMs: state === 'completed' ? now - 90_000 : null,
    createdAtMs: now - (state === 'queued' ? 20_000 : state === 'running' ? 68_000 : 180_000),
    startedAtMs: state === 'queued' ? null : now - (state === 'running' ? 64_000 : 176_000),
    updatedAtMs: now - (state === 'queued' ? 1_000 : state === 'running' ? 2_000 : 92_000),
    completedAtMs: state === 'completed' ? now - 92_000 : null,
  });
  const runs = roomTask
    ? [run(
        `subagent-run:room:${sessionId}`,
        roomTask.templateId,
        roomTask.task,
        roomTask.state,
        0,
      )]
    : [
        run('subagent-run:research', 'researcher', '检索 Agent 状态投影和知识来源证据', 'running', 0),
        run(
          'subagent-run:review',
          'reviewer',
          '审阅前端交互与工具生命周期边界',
          'completed',
          0,
          { parentRunId: 'subagent-run:research', depth: 2 },
        ),
      ];
  const state = runs.some((item) => item.state === 'running')
    ? 'running'
    : runs.some((item) => item.state === 'queued') ? 'queued' : 'completed';
  return {
    schemaVersion: 'rag-ime.agent-subagent-batch.v1',
    id: batchId,
    parentSessionId: sessionId,
    parentRunId: roomTask ? `room-parent:${sessionId}` : 'run-parent:preview',
    contextMode: roomTask ? 'fork' : 'fresh',
    resultDeliveryMode: 'inline',
    state,
    depth: roomTask ? 1 : 0,
    maxDepth: 2,
    abortRequested: false,
    causalMetadata: {
      todoId: roomTask ? `todo:${roomTask.taskId}` : `todo:${sessionId}`,
      todoRevision: 1,
      goalId: roomTask ? `goal:${rootId}` : `goal:${sessionId}`,
      goalRevision: 1,
      roomBound: Boolean(roomTask),
      roomId: roomTask ? 'room-preview' : '',
      rootId: roomTask ? rootId : '',
      taskId: roomTask?.taskId ?? '',
      dispatchId: roomTask?.dispatchId ?? '',
      generation: roomTask ? 1 : 0,
    },
    createdAtMs: now - 180_000,
    updatedAtMs: now - 2_000,
    completedAtMs: state === 'completed' ? now - 90_000 : null,
    runs,
  };
}

type PreviewSubagentTreeNode = {
  run: Record<string, unknown>;
  children: PreviewSubagentTreeNode[];
};

function previewSubagentTree(batch: Record<string, unknown>): Record<string, unknown> {
  const runs = Array.isArray(batch.runs)
    ? batch.runs.filter((item): item is Record<string, unknown> => (
      typeof item === 'object' && item !== null && !Array.isArray(item)
    ))
    : [];
  const nodes = new Map<string, PreviewSubagentTreeNode>();
  runs.forEach((run) => nodes.set(stringValue(run.id), { run, children: [] }));
  const roots: PreviewSubagentTreeNode[] = [];
  runs.forEach((run) => {
    const node = nodes.get(stringValue(run.id));
    if (!node) return;
    const parent = nodes.get(stringValue(run.parentRunId));
    if (parent) parent.children.push(node);
    else roots.push(node);
  });
  return {
    schemaVersion: 'rag-ime.agent-subagent-tree.v1',
    rootSessionId: stringValue(batch.parentSessionId),
    nodeCount: runs.length,
    maxDepth: runs.reduce((maximum, run) => Math.max(maximum, Number(run.depth) || 1), 0),
    roots,
  };
}

function previewSubagentArtifact(): Record<string, unknown> {
  const now = Date.now();
  const records = [
    ['queued', '任务已进入并行队列'],
    ['started', '子智能体已经开始执行'],
    ['checkpoint', '已保存一次可恢复进度'],
  ].map(([eventType, summary], index) => ({
    schemaVersion: 'rag-ime.agent-artifact-record.v1',
    recordId: `preview-artifact:${index + 1}`,
    eventType,
    createdAtMs: now - (3 - index) * 20_000,
    payload: { summary },
  }));
  return {
    schemaVersion: 'rag-ime.agent-artifact-inspection.v1',
    artifact: {
      schemaVersion: 'rag-ime.agent-artifact-ref.v1',
      artifactId: 'artifact:subagent-run:research',
      ownerKind: 'subagent_run',
      ownerId: 'subagent-run:research',
      kind: 'lifecycle',
      mediaType: 'application/x-ndjson',
      appendOnly: true,
      byteSize: 512,
      sha256: 'a'.repeat(64),
      recordCount: records.length,
      snapshotRevision: 1,
      snapshotSha256: 'b'.repeat(64),
      createdAtMs: now - 80_000,
      updatedAtMs: now - 20_000,
    },
    records,
    totalRecords: records.length,
    returnedRecords: records.length,
    truncated: false,
    limits: { requestedRecords: 60, maxRecords: 500, maxOutputBytes: 262_144 },
  };
}

function previewContextTrace(
  sessionId: string,
  traceId: string,
): Record<string, unknown> {
  const createdAtMs = Date.now() - 18_000;
  const traceTurnId = traceId.startsWith('context-trace:')
    ? traceId.slice('context-trace:'.length)
    : '';
  const turnId = ['turn-initial', 'turn-steady', 'turn-recovered'].includes(traceTurnId)
    ? traceTurnId
    : 'turn-recovered';
  const nodes = [
    previewContextNode('node:1:input', 1, 'input', '当前消息', 'user', '收到本轮用户输入', 126, 32, 0, createdAtMs),
    previewContextNode('node:2:session', 2, 'session', '角色与会话', 'gateway', '装配角色、模型和会话策略', 860, 215, 2, createdAtMs + 2),
    previewContextNode('node:3:tools', 3, 'tools', '工具目录', 'gateway', '按权限暴露本轮可用工具', 1_420, 355, 4, createdAtMs + 4),
    previewContextNode(
      'node:4:memory-recall',
      4,
      'memory_recall',
      '个人记忆召回',
      'memory_bootstrap',
      '召回 1 本主题书与 2 条已治理事实',
      742,
      186,
      3,
      createdAtMs + 5,
      'included',
      {
        itemCount: 3,
        memoryBookId: 'book-provider-context-order',
        memoryAtomIds: 'atom-context-order-verified,atom-foreground-acceptance',
      },
    ),
    previewContextNode('node:5:inbox', 5, 'inbox', '异步上下文', 'context_runtime', '没有等待注入的异步结果', 0, 0, 1, createdAtMs + 6, 'omitted'),
    previewContextNode(
      'node:6:runtime-request',
      6,
      'runtime_request',
      'Pi Runtime 请求',
      'gateway',
      '已形成受限运行时请求',
      2_406,
      602,
      7,
      createdAtMs + 7,
      'included',
      { contextItemCount: 3, workspaceFile: '/Volumes/work/wisdom-weasel-rag-ime/README.md' },
    ),
  ];
  return {
    schemaVersion: 'rag-ime.agent-context-trace.v1',
    traceId,
    sessionId,
    turnId,
    sourceKind: 'user',
    status: 'accepted',
    finalFingerprint: 'sha256:0123456789abcdef',
    nodes,
    edges: [
      { source: nodes[0].nodeId, target: nodes[1].nodeId },
      { source: nodes[1].nodeId, target: nodes[2].nodeId },
      { source: nodes[1].nodeId, target: nodes[3].nodeId },
      { source: nodes[1].nodeId, target: nodes[4].nodeId },
      { source: nodes[2].nodeId, target: nodes[5].nodeId },
      { source: nodes[3].nodeId, target: nodes[5].nodeId },
      { source: nodes[4].nodeId, target: nodes[5].nodeId },
    ],
    createdAtMs,
    updatedAtMs: createdAtMs + 7,
  };
}

function previewDebugContext(sessionId: string, turnId: string): Record<string, unknown> {
  const baseTime = Date.now() - 540_000;
  const turnSpecs = [
    {
      turnId: 'turn-initial',
      clientMessageId: 'preview-message-initial',
      turnOrdinal: 1,
      assemblyPhase: 'initial',
      capturedAtMs: baseTime,
      summary: '建立角色、项目边界与第一条用户输入',
      prompt: '先帮我理解这个项目的目标与边界',
    },
    {
      turnId: 'turn-steady',
      clientMessageId: 'preview-message-steady',
      turnOrdinal: 2,
      assemblyPhase: 'incremental',
      capturedAtMs: baseTime + 210_000,
      summary: '沿用稳定前缀，追加实现问题与工具结果',
      prompt: '继续核对 Provider 的上下文顺序',
    },
    {
      turnId: 'turn-recovered',
      clientMessageId: 'preview-message-recovered',
      turnOrdinal: 3,
      assemblyPhase: 'compaction_recovery',
      capturedAtMs: baseTime + 420_000,
      summary: '压缩后用恢复胶囊重建方向与最近对话',
      prompt: '压缩以后，检查现在模型实际收到了什么',
    },
  ] as const;
  const normalizedTurnId = turnId === 'turn-preview' ? 'turn-recovered' : turnId;
  const selectedTurn = turnSpecs.find((turn) => turn.turnId === normalizedTurnId) ?? turnSpecs.at(-1)!;
  const now = selectedTurn.capturedAtMs;
  const userPrompt = selectedTurn.prompt;
  const systemPrompt = 'You are the local RagIme coding agent. Follow the stable role and workspace policy.';
  const runtimeContextMessages = [
    {
      role: 'custom',
      customType: 'rag-ime-execution-mode',
      content: '<execution-mode mode="full_trust">已授权操作直接执行；硬安全边界继续生效。</execution-mode>',
      display: false,
    },
    {
      role: 'custom',
      customType: 'rag-ime-memory-recall',
      content: '<rag-ime-context type="memory_recall">已召回：用户偏好真实运行时验证。</rag-ime-context>',
      display: false,
    },
    {
      role: 'custom',
      customType: 'rag-ime-work-state',
      content: '<work-state>当前任务：核对 Provider 上下文顺序。</work-state>',
      display: false,
    },
    {
      role: 'custom',
      customType: 'rag-ime-workflow',
      content: '<workflow-state>Todo 正在执行；完成后提交验证回执。</workflow-state>',
      display: false,
    },
    {
      role: 'custom',
      customType: 'rag-ime-lifecycle',
      content: '<lifecycle-hook>Session 已启动；压缩后刷新一次上下文。</lifecycle-hook>',
      display: false,
    },
    {
      role: 'custom',
      customType: 'rag-ime-turn-context',
      content: `<turn-context>${userPrompt}</turn-context>`,
      display: false,
    },
    ...(selectedTurn.assemblyPhase === 'compaction_recovery' ? [{
      role: 'custom',
      customType: 'rag-ime-compaction-recovery',
      content: '<compaction-recovery>原始愿景保持不变。已确认 Project/Room 边界与当前实现进度；继续核对 Provider 装配证据。</compaction-recovery>',
      display: false,
    }] : []),
  ];
  const providerRuntimeInput = runtimeContextMessages.map((message) => ({
    role: 'user',
    content: [{ type: 'input_text', text: message.content }],
  }));
  const skills = [{
    name: 'context-inspector',
    description: 'Inspect the final provider context',
  }];
  const providerTools = [{
    type: 'function',
    name: 'memory_search',
    description: 'Search approved memory',
  }];
  return {
    schemaVersion: 'rag-ime.pi-debug-context-response.v1',
    sessionId,
    turnId: selectedTurn.turnId,
    available: true,
    transient: true,
    availableTurns: turnSpecs.map((turn) => ({
      turnId: turn.turnId,
      clientMessageId: turn.clientMessageId,
      turnOrdinal: turn.turnOrdinal,
      assemblyPhase: turn.assemblyPhase,
      summary: turn.summary,
      capturedAtMs: turn.capturedAtMs,
      updatedAtMs: turn.capturedAtMs + 420,
      modelCallCount: 2,
      providerRequestCount: 2,
      toolCallCount: 2,
      runningToolCount: 0,
    })),
    context: {
      schemaVersion: 'rag-ime.pi-debug-context.v1',
      sessionId,
      turnId: selectedTurn.turnId,
      clientMessageId: selectedTurn.clientMessageId,
      turnOrdinal: selectedTurn.turnOrdinal,
      assemblyPhase: selectedTurn.assemblyPhase,
      capturedAtMs: now,
      updatedAtMs: now + 120,
      prompt: `<agent-user-query>${userPrompt}</agent-user-query>`,
      systemPrompt,
      systemPromptOptions: {
        customPrompt: systemPrompt,
        cwd: '/Volumes/work/project',
        enabledTools: ['read', 'grep'],
        skills,
      },
      model: { provider: 'openai', id: 'gpt-5.2', name: 'GPT-5.2', api: 'responses' },
      activeTools: ['read', 'grep', 'memory_search'],
      toolSchemas: [
        { name: 'read', description: 'Read a local file', parameters: { type: 'object', properties: { path: { type: 'string' } } } },
        { name: 'memory_search', description: 'Search approved memory', parameters: { type: 'object', properties: { query: { type: 'string' } } } },
      ],
      cacheEvidence: [{
        requestIndex: 2,
        prefixSha256: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        prefixBytes: 18_240,
        deltaBytes: 2_180,
        duplicateBytes: 18_240,
        inputTokens: 7_200,
        outputTokens: 3_562,
        cacheReadTokens: 64_800,
        cacheWriteTokens: 0,
        capability: 'reported',
      }],
      contextWindows: [{
        index: 1,
        capturedAtMs: now + 80,
        messages: [
          ...runtimeContextMessages,
          { role: 'user', content: [{ type: 'text', text: userPrompt }] },
          { role: 'assistant', content: [{ type: 'text', text: '我会读取真实运行时指标。' }] },
        ],
      }],
      providerRequests: [{
        index: 1,
        capturedAtMs: now + 120,
        payload: {
          model: 'gpt-5.2',
          instructions: systemPrompt,
          input: [
            ...providerRuntimeInput,
            { role: 'user', content: [{ type: 'input_text', text: userPrompt }] },
          ],
          tools: providerTools,
          metadata: { skills },
        },
      }],
      modelCalls: [
        {
          index: 1,
          runtimeTurnIndex: 0,
          capturedAtMs: now + 80,
          updatedAtMs: now + 210,
          completedAtMs: now + 210,
          contextMessages: [
            ...runtimeContextMessages,
            { role: 'user', content: [{ type: 'text', text: userPrompt }] },
          ],
          contextDelta: {
            commonPrefixMessages: selectedTurn.assemblyPhase === 'compaction_recovery' ? 2 : 0,
            removedMessageCount: selectedTurn.assemblyPhase === 'compaction_recovery' ? 18 : 0,
            addedMessageCount: runtimeContextMessages.length + 1,
            addedMessages: [
              ...runtimeContextMessages,
              { role: 'user', content: userPrompt },
            ],
          },
          providerExchanges: [{
            index: 1,
            capturedAtMs: now + 120,
            status: 200,
            headers: { 'x-request-id': 'preview-1' },
            payload: {
              model: 'gpt-5.2',
              instructions: systemPrompt,
              input: [
                ...providerRuntimeInput,
                { role: 'user', content: userPrompt },
              ],
              tools: providerTools,
              metadata: { skills },
            },
          }],
          assistantMessage: { role: 'assistant', content: [{ type: 'toolCall', id: 'tool-preview-read', name: 'memory_search', arguments: { query: '上下文缓存' } }] },
        },
        {
          index: 2,
          runtimeTurnIndex: 1,
          capturedAtMs: now + 260,
          updatedAtMs: now + 420,
          completedAtMs: now + 420,
          contextMessages: [
            ...runtimeContextMessages,
            { role: 'user', content: [{ type: 'text', text: userPrompt }] },
            { role: 'assistant', content: [{ type: 'toolCall', id: 'tool-preview-read', name: 'memory_search' }] },
            { role: 'toolResult', content: [{ type: 'text', text: '缓存命中 90%' }] },
          ],
          contextDelta: {
            baseCallIndex: 1,
            commonPrefixMessages: runtimeContextMessages.length + 1,
            removedMessageCount: 0,
            addedMessageCount: 2,
            addedMessages: [
              { role: 'assistant', content: [{ type: 'toolCall', id: 'tool-preview-read', name: 'memory_search' }] },
              { role: 'toolResult', content: '缓存命中 90%' },
            ],
          },
          providerExchanges: [{
            index: 2,
            capturedAtMs: now + 300,
            status: 200,
            headers: { 'x-request-id': 'preview-2' },
            payload: {
              model: 'gpt-5.2',
              instructions: systemPrompt,
              input: [
                ...providerRuntimeInput,
                { role: 'user', content: userPrompt },
                { role: 'assistant', content: '调用 memory_search' },
                { role: 'tool', content: '缓存命中 90%' },
              ],
              tools: providerTools,
              metadata: { skills },
            },
          }],
          assistantMessage: { role: 'assistant', content: [{ type: 'text', text: '当前缓存命中率为 90%。' }] },
        },
      ],
      toolExecutions: [
        { toolCallId: 'tool-preview-read', toolName: 'memory_search', modelCallIndex: 1, runtimeTurnIndex: 0, startedAtMs: now + 140, endedAtMs: now + 205, startSequence: 1, endSequence: 4, args: { query: '上下文缓存' }, result: { items: 2 }, isError: false, status: 'completed', updates: [] },
        { toolCallId: 'tool-preview-status', toolName: 'overview', modelCallIndex: 1, runtimeTurnIndex: 0, startedAtMs: now + 145, endedAtMs: now + 198, startSequence: 2, endSequence: 3, args: { op: 'status' }, result: { healthy: true }, isError: false, status: 'completed', updates: [] },
      ],
      toolBatches: [{ id: 'preview-call-1-stage-1', modelCallIndex: 1, runtimeTurnIndex: 0, stage: 1, executionMode: 'parallel', startedAtMs: now + 140, endedAtMs: now + 205, status: 'completed', toolCallIds: ['tool-preview-read', 'tool-preview-status'] }],
    },
    telemetry: {
      schemaVersion: 'rag-ime.agent-session-telemetry.v1',
      model: { provider: 'openai', id: 'gpt-5.2', name: 'GPT-5.2' },
      context: { tokens: 98_560, contextWindow: 128_000, percent: 77, remainingTokens: 29_440, compactAtTokens: 111_616, tokensUntilCompact: 13_056, reserveTokens: 16_384, keepRecentTokens: 20_000, autoCompactEnabled: true },
      cumulativeUsage: { input: 7_200, output: 3_562, cacheRead: 64_800, cacheWrite: 0, totalTokens: 75_562 },
      latestUsage: { input: 1_400, output: 562, cacheRead: 12_600, cacheWrite: 0, totalTokens: 14_562 },
      latestCacheHitPercent: 90,
      isCompacting: false,
      compactionCount: 2,
      updatedAtMs: now + 120,
    },
  };
}

function previewContextNode(
  nodeId: string,
  ordinal: number,
  stage: string,
  label: string,
  sourceKind: string,
  summary: string,
  charCount: number,
  tokenEstimate: number,
  durationMs: number,
  createdAtMs: number,
  disposition: 'included' | 'omitted' = 'included',
  /* 装配节点记录的证据实体引用。形状与 Runtime `_public_metadata` 允许穿过的
     一致——只有扁平字符串，多值逗号分隔——所以预览里能点开的入口，真机
     在同样的 metadata 下也能点开。 */
  metadata: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    nodeId,
    ordinal,
    stage,
    label,
    sourceKind,
    disposition,
    summary,
    charCount,
    tokenEstimate,
    durationMs,
    fingerprint: charCount ? `sha256:${String(ordinal).repeat(16)}` : '',
    reason: disposition === 'omitted' ? '本轮没有可投递项目' : '',
    metadata: stage === 'tools' ? { toolCount: 18, ...metadata } : metadata,
    createdAtMs,
  };
}

function previewSession(
  id: string,
  title: string,
  roleId: string,
  updatedAtMs: number,
  roleVersion = '1',
  /* Rail summaries were hardcoded to zero, so every preview session claimed
     "0 条消息" while rendering a full transcript. Optional so existing call
     sites keep their exact shape. */
  summary: { messageCount?: number; lastMessagePreview?: string; workspaceRoots?: string[] } = {},
): Record<string, unknown> {
  return {
    id,
    title,
    mode: 'assistant',
    status: 'ready',
    roleId,
    roleVersion,
    executionMode: 'per_action',
    workspaceScopeGranted: false,
    workspaceScopeSha256: '',
    workspaceScopeGrantedAtMs: 0,
    projectContextEnabled: false,
    piSkillsEnabled: false,
    codexSkillsEnabled: false,
    updatedAtMs,
    workspaceRoots: summary.workspaceRoots ?? [],
    modelProfile: 'session-selected',
    messageCount: summary.messageCount ?? 0,
    lastMessagePreview: summary.lastMessagePreview ?? '',
  };
}

function previewApprovalItems(now = Date.now()): AgentApprovalV1[] {
  const causalMetadata: AgentApprovalV1['causalMetadata'] = {
    todoId: '',
    todoRevision: 0,
    goalId: 'goal:control-center',
    goalRevision: 4,
    turnId: 'turn:preview-approval',
    roomBound: false,
    roomId: '',
    rootId: '',
    dispatchId: '',
    generation: 0,
  };
  return [
    {
      schemaVersion: 'rag-ime.agent-approval.v1',
      approvalId: 'approval:preview-release',
      sessionId: 'session-preview',
      toolCallId: 'tool-call:preview-release',
      toolId: 'workspace_shell',
      operation: 'run',
      payloadSha256: 'a'.repeat(64),
      preview: {
        summary: '构建并安装 Control Center 开发版本',
        command: 'scripts/install_product_stack.sh --include-pi --skip-mlx',
        path: '/Users/example/personal-agent-workbench',
        target: 'Control Center.app',
        scope: '当前用户的开发安装',
        changes: ['重新构建前端', '替换开发版应用', '保留现有个人数据'],
        rollback: '保留当前安装包，可按安装回执恢复',
        apiToken: 'PRIVATE_APPROVAL_TOKEN',
      },
      riskLevel: 'R3',
      state: 'pending',
      requestedAtMs: now - 82_000,
      expiresAtMs: now + 168_000,
      decidedBy: '',
      causalMetadata,
    },
    {
      schemaVersion: 'rag-ime.agent-approval.v1',
      approvalId: 'approval:preview-config',
      sessionId: 'session-input',
      toolCallId: 'tool-call:preview-config',
      toolId: 'configuration',
      operation: 'apply_settings',
      payloadSha256: 'b'.repeat(64),
      preview: {
        summary: '允许 Room 伙伴使用 Session 子 Agent 模板',
        scope: '当前项目',
        changes: ['启用 reviewer 模板', '保持只读工作区'],
      },
      riskLevel: 'R2',
      state: 'pending',
      requestedAtMs: now - 34_000,
      expiresAtMs: now + 526_000,
      decidedBy: '',
      causalMetadata: { ...causalMetadata, turnId: 'turn:preview-config' },
    },
    {
      schemaVersion: 'rag-ime.agent-approval.v1',
      approvalId: 'approval:preview-applied',
      sessionId: 'session-states',
      toolCallId: 'tool-call:preview-applied',
      toolId: 'workspace_patch',
      operation: 'apply',
      payloadSha256: 'c'.repeat(64),
      preview: {
        summary: '更新 Session 子 Agent 可视化样式',
        files: ['SessionSubagentPanel.tsx', 'session-subagent.css'],
      },
      riskLevel: 'R1',
      state: 'applied',
      requestedAtMs: now - 18 * 60_000,
      expiresAtMs: now - 8 * 60_000,
      decidedBy: 'preview-user',
      decidedAtMs: now - 17 * 60_000,
      receipt: { receiptId: 'receipt:preview-applied', status: 'applied' },
      causalMetadata: { ...causalMetadata, turnId: 'turn:preview-applied' },
    },
  ];
}

function previewRoomSession(
  id: string,
  title: string,
  roleId: string,
  participantId: string,
): Record<string, unknown> {
  return {
    ...previewSession(id, title, roleId, Date.now()),
    roomParticipant: {
      roomId: 'room-preview',
      participantId,
      status: 'active',
    },
    mode: 'coordinator',
    toolProfileVersion: 'control-center-v1',
    executionMode: 'workspace_managed',
    workspaceScopeGranted: true,
    toolAllowlistMode: 'profile',
    allowedTools: [],
    workspaceRoots: ['/Volumes/work/wisdom-weasel-rag-ime'],
  };
}

function previewTool(
  id: string,
  displayName: string,
  description: string,
  domain: string,
  riskLevel: string,
  operations: string[],
): Record<string, unknown> {
  return {
    schemaVersion: 'rag-ime.control-tool-manifest.v1',
    id,
    displayName,
    description,
    domain,
    category: domain,
    riskLevel,
    sessionModes: ['assistant', 'coordinator'],
    operations,
    operationRisks: Object.fromEntries(operations.map((operation) => [operation, riskLevel])),
    resultPresentation: 'tool_result',
    availability: 'online',
    version: 'preview',
    enabled: true,
    effectiveOperations: operations,
  };
}

function previewCapabilityCatalog(
  sessionId: string,
  sessionPreferences: Record<string, string> = {},
  globalPreferences: Record<string, string> = {},
  projectPreferences: Record<string, string> = {},
): Record<string, unknown> {
  const effectiveAtMs = Date.now();
  const manifests = [
    previewTool('overview', '控制中心概览', '查看输入法、模型、记忆和最近活动的整体状态', 'control', 'R0', ['status', 'capabilities', 'recent_activity']),
    {
      ...previewTool('ask', 'Ask', '向用户提出仍需其决定的结构化选择', 'planning', 'R0', ['ask']),
      alwaysAvailable: true,
    },
    {
      ...previewTool('todo', 'Todo', '维护当前 Session 的分阶段执行清单', 'planning', 'R0', ['init', 'start', 'done', 'drop', 'block', 'unblock', 'append', 'view', 'rm']),
      alwaysAvailable: true,
    },
    previewTool('input', '输入法', '查看输入设置、方案与候选解释，并在批准后调整配置或词表', 'input', 'R1', ['get_settings', 'apply_settings', 'rollback_settings', 'profile', 'candidate_explain']),
    previewTool('voice', '语音输入', '查看语音状态，并在批准后切换已配置的语音 Provider', 'voice', 'R1', ['status', 'privacy_policy', 'provider_status', 'provider_preview', 'provider_apply']),
    previewTool('planning', '规划与任务', '查看每日计划，并在确认后更新任务状态', 'planning', 'R1', ['dashboard', 'task_action', 'undo_task_event']),
    previewTool('memory', '个人上下文记忆', '查询已治理的长期记忆与来源链路', 'memory', 'R1', ['catalog', 'read', 'recent', 'trace', 'search']),
    previewTool('knowledge', '文档知识库', '检索用户明确启用的独立文档知识库', 'knowledge', 'R0', ['list_bases', 'search', 'find', 'open', 'status']),
    previewTool('browser', 'PAW Browser', '通过开放 ego-browser 内核操作 PAW 内置 Chromium 与 Task Space', 'browser', 'R0', ['status', 'tabs', 'snapshot', 'run', 'navigate', 'click', 'type', 'stop']),
    {
      ...previewTool(
        'workspace_lsp',
        '代码智能',
        '通过工作区语言服务器读取语义信息，并在审批后执行重命名或代码动作',
        'workspace',
        'R2',
        ['status', 'symbols', 'hover', 'definition', 'references', 'diagnostics', 'rename', 'code_action_apply'],
      ),
      runtimeProjection: {
        schemaVersion: 'rag-ime.workspace-lsp-status.v1',
        runtimeInstanceId: 'workspace-lsp-11111111111111111111111111111111',
        runtimeEpoch: 1,
        observedAtMs: effectiveAtMs,
        heartbeatExpiresAtMs: effectiveAtMs + 60_000,
        current: true,
        summary: '预览 Runtime 已连接工作区语言服务器',
        state: 'ready',
        roots: [{
          root: '/Users/example/Projects/personal-agent-workbench',
          state: 'ready',
          servers: [{
            name: 'typescript-language-server',
            state: 'ready',
            languageIds: ['typescript', 'typescriptreact'],
            fileExtensions: ['.ts', '.tsx'],
          }],
        }],
      },
    },
    previewTool('workspace_job', '后台任务', '启动并观察有界后台命令', 'workspace', 'R2', ['start', 'status', 'cancel']),
  ];
  const items = manifests.map((manifest) => {
    const id = stringValue(manifest.id);
    const canonicalId = `tool:${id}`;
    const fixed = manifest.alwaysAvailable === true;
    const sessionPreference = fixed ? 'inherit' : sessionPreferences[canonicalId] ?? 'inherit';
    const projectPreference = fixed ? 'inherit' : projectPreferences[canonicalId] ?? 'inherit';
    const globalPreference = fixed ? 'inherit' : globalPreferences[canonicalId] ?? 'inherit';
    const effective = sessionPreference !== 'inherit'
      ? sessionPreference
      : projectPreference !== 'inherit'
        ? projectPreference
        : globalPreference !== 'inherit'
          ? globalPreference
          : id === 'memory' ? 'disabled' : 'enabled';
    const effectiveScope = sessionPreference !== 'inherit'
      ? 'session'
      : projectPreference !== 'inherit'
        ? 'project_default'
        : globalPreference !== 'inherit'
          ? 'global_default'
          : 'built_in_default';
    const risk = stringValue(manifest.riskLevel) || 'R0';
    return {
      ...manifest,
      canonicalId,
      kind: 'tool',
      source: { kind: 'product', label: 'Personal Agent Workbench' },
      status: stringValue(manifest.availability) || 'offline',
      risk,
      requiredPermissions: [
        ...(risk === 'R0' ? [] : ['native_approval']),
        ...(id.startsWith('workspace_') ? ['workspace_scope'] : []),
      ],
      authorization: {
        state: sessionId ? 'authorized' : 'not_applicable',
        reason: sessionId ? 'existing_session_policy_authorizes_tool' : 'session_context_required',
      },
      disclosure: {
        preference: sessionPreference,
        effective,
        state: effective === 'enabled' ? 'disclosed' : 'hidden',
        reason: fixed ? 'required_session_tool' : effectiveScope === 'session' ? 'session_preference' : `inherited_${effectiveScope}`,

        scope: effectiveScope,
      },
      effectiveScope,
      reasons: [fixed ? 'required_session_tool' : effectiveScope === 'session' ? 'session_preference' : `inherited_${effectiveScope}`],
      revision: `preview:${id}:1`,
      effectiveAtMs,
    };
  });
  const projectId = `workspace-${'b'.repeat(64)}`;
  return {
    schemaVersion: 'rag-ime.capability-catalog.v1',
    ok: true,
    revision: `sha256:${'a'.repeat(64)}`,
    effectiveAtMs,
    projectScope: sessionId
      ? { supported: true, identityKind: 'workspace_scope_sha256', projectId, reason: 'session_workspace_scope' }
      : { supported: false, identityKind: 'none', reason: 'stable_project_identity_unavailable' },
    ...(sessionId ? {
      sessionPolicy: {
        sessionId,
        mode: 'coordinator',
        executionMode: 'workspace_managed',
        workspaceScopeGranted: true,
        toolProfileVersion: 'control-center-v1',
        toolAllowlistMode: 'profile',
        allowedTools: [],
        policyRevision: 1,
        disclosurePreferences: {
          globalDefault: globalPreferences,
          projectDefault: projectPreferences,
          session: sessionPreferences,
          effective: Object.fromEntries(items.map((item) => [item.canonicalId, record(item.disclosure).effective])),
        },
        effectiveAtMs,
      },
    } : {}),
    items,
  };
}

function previewCapabilityPreferences(value: unknown): Record<string, string> {
  return Object.fromEntries(
    Object.entries(record(value)).filter(([, preference]) => (
      preference === 'inherit' || preference === 'enabled' || preference === 'disabled'
    )),
  ) as Record<string, string>;
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function previewEditablePersona(
  body: Record<string, unknown>,
  base: AgentPersonaV1 | undefined,
  roleId: string,
): AgentPersonaV1 {
  const source = base ?? previewPersonas[0]!;
  const requestedModes = Array.isArray(body.selectableModes)
    ? body.selectableModes.filter((value): value is 'assistant' | 'coordinator' => value === 'assistant' || value === 'coordinator')
    : ['assistant'];
  const selectableModes: AgentPersonaV1['selectableModes'] = requestedModes.includes('coordinator')
    ? ['assistant', 'coordinator']
    : ['assistant'];
  const requestedTraits = Array.isArray(body.traits)
    ? body.traits.map(stringValue).filter(Boolean).slice(0, 5)
    : [...source.traits];
  const traits = (requestedTraits.length ? requestedTraits : [source.traits[0]]) as AgentPersonaV1['traits'];
  const requestedSuitableTasks = Array.isArray(body.suitableTasks)
    ? body.suitableTasks.map(stringValue).filter(Boolean).slice(0, 4)
    : [...source.runtimeCharacteristics.suitableTasks];
  const requestedUnsuitableTasks = Array.isArray(body.unsuitableTasks)
    ? body.unsuitableTasks.map(stringValue).filter(Boolean).slice(0, 4)
    : [...source.runtimeCharacteristics.unsuitableTasks];
  return {
    ...source,
    roleId,
    version: '1',
    displayName: stringValue(body.displayName) || source.displayName,
    tagline: stringValue(body.tagline) || source.tagline,
    summary: stringValue(body.summary) || source.summary,
    traits,
    visualProfile: {
      ...source.visualProfile,
      avatarAssetId: 'rag-ime-timeline-custom-v1',
      accentToken: source.visualProfile.accentToken,
    },
    defaults: { ...source.defaults },
    runtimeCharacteristics: {
      ...source.runtimeCharacteristics,
      suitableTasks: (requestedSuitableTasks.length ? requestedSuitableTasks : [stringValue(body.summary) || source.summary]) as AgentPersonaV1['runtimeCharacteristics']['suitableTasks'],
      unsuitableTasks: (requestedUnsuitableTasks.length ? requestedUnsuitableTasks : ['未明确边界或需要独立高风险决策的任务']) as AgentPersonaV1['runtimeCharacteristics']['unsuitableTasks'],
      isDefault: false,
    },
    selectableModes,
  };
}


function previewThinkingLevel(value: unknown): NonNullable<AgentPersonaV1['defaults']['thinkingLevel']> {
  const level = stringValue(value);
  return level === 'minimal' || level === 'low' || level === 'medium' || level === 'high' || level === 'xhigh' || level === 'max'
    ? level
    : 'off';
}

type PreviewSkillScenario = 'ordinary' | 'room' | 'trace' | 'agentLab';

const PREVIEW_PRIVATE_SKILL_SCENARIOS: Readonly<Partial<Record<string, PreviewSkillScenario>>> = {
  'facilitate-room': 'room',
  'trace-agent-diagnostics': 'trace',
  'agent-eval-room-optimizer': 'agentLab',
};

function previewCompanionConfiguration(
  revision: number,
  defaults: { roleId: string; roleVersion: string },
  modelRouting: Record<string, { modelProfile: string; thinkingLevel: string }>,
  skillRouting: Record<PreviewSkillScenario, string[]>,
  capabilityGlobalPreferences: Record<string, string>,
  capabilityProjectPreferences: Record<string, Record<string, string>>,
): Record<string, unknown> {
  return {
    ok: true,
    configuration: {
      revision,
      configuration: {
        sessionDefaults: {
          roleId: defaults.roleId,
          roleVersion: defaults.roleVersion,
          capabilityDisclosurePreferences: capabilityGlobalPreferences,
        },
        modelRouting,
        skillRouting,
        capabilityDisclosure: {
          projectPreferences: capabilityProjectPreferences,
        },
      },
    },
  };
}

function previewDefaultModelRouting(): Record<string, { modelProfile: string; thinkingLevel: string }> {
  return Object.fromEntries([
    'primary',
    'traceDiagnostic',
    'toolAgent',
    'subagent',
    'roomCoordinator',
  ].map((routeId) => [routeId, {
    modelProfile: 'inherit',
    thinkingLevel: 'inherit',
  }]));
}

function previewDefaultSkillRouting(): Record<PreviewSkillScenario, string[]> {
  const general = ['systematic-debugging'];
  return {
    ordinary: [...general],
    room: [...general, 'facilitate-room'].sort(),
    trace: [...general, 'trace-agent-diagnostics'].sort(),
    agentLab: [...general, 'agent-eval-room-optimizer'].sort(),
  };
}

function previewCreatedRoomSnapshot(
  roomId: string,
  body: Record<string, unknown>,
): Record<string, unknown> {
  const now = Date.now();
  const roomKind = stringValue(body.roomKind) === 'roleplay' ? 'roleplay' : 'collaboration';
  const requestedPermissionPolicy = parseRoomPermissionPolicy(body.permissionPolicy, roomKind);
  if (body.permissionPolicy !== undefined && !requestedPermissionPolicy) {
    throw new Error('Room 分层权限策略无效。');
  }
  const permissionPolicy = requestedPermissionPolicy ?? defaultRoomPermissionPolicy(roomKind);
  const requestedParticipants = Array.isArray(body.participants)
    ? body.participants.map(record)
    : [];
  if (requestedParticipants.length < 2 || requestedParticipants.length > 4) {
    throw new Error('请选择 2 至 4 位伙伴。');
  }
  const participants = requestedParticipants.map((participant, ordinal) => ({
    schemaVersion: 'rag-ime.agent-participant.v1',
    id: `${roomId}:participant-${ordinal + 1}`,
    roomId,
    sessionId: `${roomId}:session-${ordinal + 1}`,
    roleId: stringValue(participant.roleId),
    roleVersion: stringValue(participant.roleVersion) || '1',
    displayName: stringValue(participant.displayName) || `伙伴 ${ordinal + 1}`,
    collaborationRole: stringValue(participant.collaborationRole)
      || (ordinal === 0 ? 'coordinator' : 'implementer'),
    status: 'active',
    ordinal,
    createdAtMs: now,
    lastSpokeAtMs: null,
  }));
  const workspaceRoots = Array.isArray(body.workspaceRoots)
    ? body.workspaceRoots.filter((value): value is string => typeof value === 'string')
    : [];
  const room = {
    schemaVersion: 'rag-ime.agent-room.v1',
    id: roomId,
    title: stringValue(body.title),
    status: 'active',
    roomKind,
    avatar: stringValue(body.avatar),
    description: stringValue(body.description),
    scenarioPrompt: stringValue(body.scenarioPrompt),
    ownerAppId: stringValue(body.ownerAppId),
    surfaceKey: stringValue(body.surfaceKey),
    routingPolicy: stringValue(body.routingPolicy) || 'natural',
    routingConfig: record(body.routingConfig),
    moderatorParticipantId: participants[0]!.id,
    workspaceRoots,
    executionMode: permissionPolicy.room.executionMode,
    permissionPolicy,
    createdAtMs: now,
    updatedAtMs: now,
    lastEventSequence: 0,
    participants,
    topics: [],
    artifacts: [],
    workItems: [],
  };
  return {
    schemaVersion: 'rag-ime.agent-room-snapshot.v1',
    ok: true,
    room,
    events: [],
    firstSequence: 0,
    lastSequence: 0,
    resumeToken: '',
    truncated: false,
  };
}

function previewRoomTopicMutation(
  snapshot: Record<string, unknown>,
  body: Record<string, unknown>,
  create: boolean,
): Record<string, unknown> {
  const room = record(snapshot.room);
  const roomId = stringValue(room.id);
  const currentTopics = Array.isArray(room.topics) ? room.topics.map(record) : [];
  const now = Date.now();
  let topics: Record<string, unknown>[];
  let activeTopicId = stringValue(room.activeTopicId);
  if (create) {
    const title = stringValue(body.title);
    if (!title) throw new Error('话题名称不能为空。');
    const topic = {
      schemaVersion: 'rag-ime.agent-room-topic.v1',
      id: `${roomId}:topic-${currentTopics.length + 1}`,
      roomId,
      title,
      summary: stringValue(body.summary),
      status: 'active',
      ordinal: currentTopics.length,
      createdAtMs: now,
      updatedAtMs: now,
    };
    topics = [...currentTopics, topic];
  } else {
    const topicId = stringValue(body.topicId);
    if (!currentTopics.some((topic) => topic.id === topicId)) {
      throw new Error('这个话题已经不存在，请刷新后重试。');
    }
    topics = currentTopics.map((topic) => topic.id === topicId
      ? {
          ...topic,
          ...(typeof body.title === 'string' ? { title: stringValue(body.title) } : {}),
          ...(typeof body.summary === 'string' ? { summary: stringValue(body.summary) } : {}),
          ...(body.archived === true ? { status: 'archived' } : {}),
          updatedAtMs: now,
        }
      : topic);
    if (body.activate === true) activeTopicId = topicId;
    if (body.archived === true && activeTopicId === topicId) activeTopicId = '';
  }
  return {
    ...snapshot,
    room: {
      ...room,
      topics,
      activeTopicId,
      updatedAtMs: now,
    },
  };
}

function previewWorkspaceList(path: string): Record<string, unknown> {
  const root = '/Users/example/Projects/personal-agent-workbench';
  const items = path.endsWith('/control-center-web')
    ? [
        { path: `${path}/src`, name: 'src', kind: 'directory' },
        { path: `${path}/package.json`, name: 'package.json', kind: 'file', byteSize: 3_842 },
      ]
    : [
        { path: `${path}/control-center-web`, name: 'control-center-web', kind: 'directory' },
        { path: `${path}/rag_ime`, name: 'rag_ime', kind: 'directory' },
        { path: `${path}/README.md`, name: 'README.md', kind: 'file', byteSize: 12_480 },
      ];
  return {
    schemaVersion: 'rag-ime.agent-workspace-list.v1',
    ok: true,
    sessionId: 'session-preview',
    root,
    path,
    items,
    truncated: false,
  };
}

function previewWorkspaceRead(path: string): Record<string, unknown> {
  const content = path.endsWith('.md')
    ? '# Personal Agent Workbench\n\n这是工作区文件预览。\n'
    : 'export function previewWorkspace() {\n  return "ready";\n}\n';
  return {
    schemaVersion: 'rag-ime.agent-workspace-read.v1',
    ok: true,
    sessionId: 'session-preview',
    summary: `已读取 ${path.split('/').at(-1)}`,
    path,
    root: '/Users/example/Projects/personal-agent-workbench',
    offset: 0,
    byteSize: new TextEncoder().encode(content).byteLength,
    content,
    contentBytes: new TextEncoder().encode(content).byteLength,
    truncated: false,
    nextOffset: new TextEncoder().encode(content).byteLength,
  };
}

function previewRoomConversationSnapshot(
  snapshot: Record<string, unknown>,
): Record<string, unknown> {
  const room = record(snapshot.room);
  const allEvents = Array.isArray(snapshot.events)
    ? snapshot.events.map(record)
    : [];
  const events = allEvents.filter((event) => event.eventType !== 'participant_activity');
  const cursorSequence = Number(room.lastEventSequence ?? snapshot.lastSequence ?? 0);
  return {
    schemaVersion: 'rag-ime.agent-room-conversation-snapshot.v1',
    ok: true,
    room,
    events,
    firstEventSequence: Number(events[0]?.sequence ?? 0),
    cursorSequence,
    resumeToken: cursorSequence > 0 ? `${String(room.id ?? '')}:${cursorSequence}` : '',
    deferredEventCount: allEvents.length - events.length,
    truncated: snapshot.truncated === true,
  };
}


function record(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function eventSessionId(value: unknown): string {
  return stringValue(record(value).sessionId);
}

function resumeSequence(value: string, sessionId: string): number {
  const prefix = `${sessionId}:`;
  if (!value.startsWith(prefix)) return 0;
  const sequence = Number(value.slice(prefix.length));
  return Number.isInteger(sequence) && sequence >= 0 ? sequence : 0;
}
