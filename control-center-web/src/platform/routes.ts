import type { GeneratedContractName } from '@/contracts/generated';

export type ControlHttpMethod = 'GET' | 'POST' | 'PATCH' | 'DELETE';
export type ControlStreamKind = 'agent' | 'room' | 'kernel' | 'control' | 'observation';

export interface ControlRouteDefinition {
  method: ControlHttpMethod;
  path: string;
  params?: Readonly<Record<string, readonly string[] | null>>;
  query?: readonly string[];
  requiredQuery?: readonly string[];
  body?: readonly string[];
  requiredBody?: readonly string[];
  responseContract?: GeneratedContractName;
  subscription?: ControlStreamKind;
  /** Binary responses are available only through the matching typed transport method. */
  binary?: boolean;
}

// These dotted ids are the cross-platform authority. Neither pages nor the
// native bridge may replace them with a URL supplied at runtime.
export const CONTROL_ROUTES = {
  'control.bootstrap': { method: 'GET', path: '/api/agent/control/bootstrap' },
  'control.capabilities': {
    method: 'GET',
    path: '/api/agent/control/capabilities',
  },
  'control.events': {
    method: 'GET',
    path: '/api/agent/events',
    query: ['lastEventId'],
    requiredQuery: ['lastEventId'],
    subscription: 'control',
  },
  'system.health': { method: 'GET', path: '/api/health' },
  'input.source.get': { method: 'GET', path: '/api/input-source' },
  'input.lexicon.review': {
    method: 'GET',
    path: '/api/rime-lexicon/review',
    query: ['limit', 'project'],
  },
  'input.lexicon.apply': {
    method: 'POST',
    path: '/api/rime-lexicon/apply',
    body: ['reviewToken', 'selectedKeys', 'confirmText', 'project', 'limit'],
    requiredBody: ['reviewToken', 'selectedKeys', 'confirmText'],
  },
  'input.lexicon.rollback': {
    method: 'POST',
    path: '/api/rime-lexicon/rollback',
    body: ['rollbackId'],
    requiredBody: ['rollbackId'],
  },
  // This bounded frame feed is intentionally local-only: debug configuration
  // may expose owner-local text in the response, while the input page projects
  // only identity and lane metadata.
  'input.prediction.liveTrace': {
    method: 'GET',
    path: '/api/prediction/live-trace',
    query: ['limit', 'sessionId'],
  },
  'observability.snapshot': {
    method: 'GET',
    path: '/api/observability/snapshot',
    query: [
      'limit',
      'beforeSequence',
      'sessionId',
      'roomId',
      'traceId',
      'runId',
      'category',
      'status',
    ],
    responseContract: 'observation-snapshot.v1',
  },
  'observability.trace.get': {
    method: 'GET',
    path: '/api/observability/traces/:traceId',
    params: { traceId: null },
    query: ['limit', 'beforeSequence'],
    responseContract: 'observability-trace-get.v1',
  },
  'observability.evals.list': {
    method: 'GET',
    path: '/api/observability/evals',
    query: ['traceId', 'limit'],
    requiredQuery: ['traceId'],
    responseContract: 'observability-eval-list.v1',
  },
  'observability.traceDiagnosticReports.list': {
    method: 'GET',
    path: '/api/observability/trace-diagnostic-reports',
    query: ['limit', 'cursor'],
    responseContract: 'trace-diagnostic-report-list.v1',
  },
  'observability.traceDiagnosticReports.create': {
    method: 'POST',
    path: '/api/observability/trace-diagnostic-reports',
    body: ['diagnosticSessionId', 'title', 'targets', 'intent'],
    requiredBody: ['diagnosticSessionId', 'targets'],
    responseContract: 'trace-diagnostic-report.v1',
  },
  'observability.traceDiagnosticReport.get': {
    method: 'GET',
    path: '/api/observability/trace-diagnostic-reports/:reportId',
    params: { reportId: null },
    responseContract: 'trace-diagnostic-report.v1',
  },
  'observability.traceDiagnosticReport.optimizationCommand': {
    method: 'POST',
    path: '/api/observability/trace-diagnostic-reports/:reportId/optimization',
    params: { reportId: null },
    body: ['operation', 'clientRequestId', 'candidateId', 'action', 'input'],
    requiredBody: ['operation', 'clientRequestId'],
    responseContract: 'trace-diagnostic-report.v1',
  },
  'observability.traceOptimization.library': {
    method: 'GET',
    path: '/api/observability/trace-optimization',
    query: ['projectId', 'query', 'patternId', 'revision', 'offset'],
  },
  'observability.traceOptimization.capabilities': {
    method: 'GET',
    path: '/api/observability/trace-optimization/capabilities',
  },
  'observability.traceDiagnosticReport.finalize': {
    method: 'POST',
    path: '/api/observability/trace-diagnostic-reports/:reportId/finalize',
    params: { reportId: null },
    body: ['expectedRevision'],
    requiredBody: ['expectedRevision'],
    responseContract: 'trace-diagnostic-report.v1',
  },
  'observability.traceDiagnosticReport.repairAuthorize': {
    method: 'POST',
    path: '/api/observability/trace-diagnostic-reports/:reportId/repair-authorize',
    params: { reportId: null },
    body: ['expectedRevision', 'findingId', 'sourceScope', 'sourceTraceId', 'failureRef', 'repairSessionId'],
    requiredBody: ['expectedRevision', 'findingId', 'sourceScope', 'sourceTraceId', 'failureRef', 'repairSessionId'],
    responseContract: 'trace-diagnostic-report.v1',
  },
  'observability.traceDiagnosticReport.repairVerify': {
    method: 'POST',
    path: '/api/observability/trace-diagnostic-reports/:reportId/repair-verify',
    params: { reportId: null },
    body: ['expectedRevision', 'repairReceiptId', 'verificationReceiptId'],
    requiredBody: ['expectedRevision', 'repairReceiptId'],
    responseContract: 'trace-diagnostic-report.v1',
  },
  'observability.evalSuites.list': {
    method: 'GET',
    path: '/api/observability/eval-suites',
    query: ['limit'],
    responseContract: 'eval-suite-list.v1',
  },
  'observability.sandboxRuns.list': {
    method: 'GET',
    path: '/api/observability/sandbox-runs',
    query: ['limit'],
    responseContract: 'observability-sandbox-run-list.v1',
  },
  'observability.sandboxRun.get': {
    method: 'GET',
    path: '/api/observability/sandbox-runs/:sandboxRunId',
    params: { sandboxRunId: null },
    responseContract: 'sandbox-run.v1',
  },
  'extension.sandbox.experiment.run': {
    method: 'POST',
    path: '/api/extensions/sandbox/experiments',
    body: ['sessionId', 'ownerAppId', 'experimentId', 'candidateBindingSha256', 'requestedDecision'],
    requiredBody: ['sessionId', 'ownerAppId', 'experimentId', 'candidateBindingSha256', 'requestedDecision'],
  },
  'observability.evals.evidence.run': {
    method: 'POST',
    path: '/api/observability/evals/evidence-ground-truth',
    body: [
      'schemaVersion',
      'traceId',
      'requiredEvidenceIds',
      'datasetId',
      'labelRevision',
      'truthKind',
    ],
    requiredBody: [
      'schemaVersion',
      'traceId',
      'requiredEvidenceIds',
      'datasetId',
      'labelRevision',
      'truthKind',
    ],
    responseContract: 'eval-run.v1',
  },
  'observability.evals.aiJudge.run': {
    method: 'POST',
    path: '/api/observability/evals/ai-judge',
    body: ['traceId', 'evaluator', 'provider', 'model', 'thinking', 'displayName'],
    requiredBody: ['traceId'],
    responseContract: 'eval-run.v1',
  },
  // Trace repair evidence is an ordered, loopback-only write pipeline. The
  // server issues evidence and receipt identities.
  'observability.traceRepair.changeEvidence': {
    method: 'POST',
    path: '/api/observability/trace-repair/evidence/change',
    body: ['schemaVersion', 'repairSessionId', 'repairTraceId'],
    requiredBody: ['schemaVersion', 'repairSessionId', 'repairTraceId'],
  },
  'observability.traceRepair.testEvidence': {
    method: 'POST',
    path: '/api/observability/trace-repair/evidence/test',
    body: ['schemaVersion', 'repairSessionId', 'repairTraceId'],
    requiredBody: ['schemaVersion', 'repairSessionId', 'repairTraceId'],
  },
  'observability.traceRepair.receipt.create': {
    method: 'POST',
    path: '/api/observability/trace-repair/receipts',
    body: [
      'schemaVersion',
      'sourceScope',
      'sourceTraceId',
      'failureRef',
      'changeReceiptId',
      'testEvidenceId',
      'repairTraceId',
      'repairSessionId',
    ],
    requiredBody: [
      'schemaVersion',
      'sourceScope',
      'sourceTraceId',
      'failureRef',
      'changeReceiptId',
      'testEvidenceId',
      'repairTraceId',
      'repairSessionId',
    ],
  },
  'observability.traceRepair.receipt.get': {
    method: 'GET',
    path: '/api/observability/trace-repair/receipts/:repairReceiptId',
    params: { repairReceiptId: null },
  },
  'observability.traceRepair.recheck': {
    method: 'POST',
    path: '/api/observability/trace-repair/recheck',
    body: ['schemaVersion', 'repairReceiptId'],
    requiredBody: ['schemaVersion', 'repairReceiptId'],
  },
  'observability.traceReplay.case.create': {
    method: 'POST',
    path: '/api/observability/trace-replay/cases',
    body: [
      'schemaVersion',
      'sourceScope',
      'failureRef',
      'sourceTraceId',
      'baselineEvalRunId',
      'baselineSandboxRunId',
      'successMetric',
      'successThreshold',
      'rollbackTarget',
    ],
    requiredBody: [
      'schemaVersion',
      'sourceScope',
      'failureRef',
      'sourceTraceId',
      'baselineEvalRunId',
      'baselineSandboxRunId',
      'successMetric',
      'successThreshold',
      'rollbackTarget',
    ],
  },
  'observability.traceReplay.case.get': {
    method: 'GET',
    path: '/api/observability/trace-replay/cases/:replayCaseId',
    params: { replayCaseId: null },
  },
  'observability.traceReplay.verify': {
    method: 'POST',
    path: '/api/observability/trace-replay/verify',
    body: [
      'schemaVersion',
      'replayCaseId',
      'repairReceiptId',
      'repairEvalRunId',
      'repairSandboxRunId',
      'regressionEvalRunIds',
    ],
    requiredBody: [
      'schemaVersion',
      'replayCaseId',
      'repairReceiptId',
      'repairEvalRunId',
      'repairSandboxRunId',
      'regressionEvalRunIds',
    ],
  },
  'observability.traceReplay.verification.get': {
    method: 'GET',
    path: '/api/observability/trace-replay/verifications/:verificationReceiptId',
    params: { verificationReceiptId: null },
  },
  'observability.evalSchedules.list': {
    method: 'GET',
    path: '/api/observability/eval-schedules',
    query: ['limit'],
    responseContract: 'eval-schedule-list.v1',
  },
  'observability.evalSchedules.create': {
    method: 'POST',
    path: '/api/observability/eval-schedules',
    body: [
      'scheduleId',
      'suiteId',
      'suiteRevision',
      'recurrenceKind',
      'recurrenceInterval',
      'maxRuns',
      'nextDueAtMs',
    ],
    requiredBody: ['suiteId', 'suiteRevision', 'recurrenceKind', 'nextDueAtMs'],
    responseContract: 'eval-schedule-create.v1',
  },
  'observability.evalSchedule.runs': {
    method: 'GET',
    path: '/api/observability/eval-schedules/:scheduleId/runs',
    params: { scheduleId: null },
    query: ['limit'],
    responseContract: 'eval-schedule-run-list.v1',
  },
  'observability.evalSchedule.action': {
    method: 'POST', path: '/api/observability/eval-schedules/:scheduleId/action',
    params: { scheduleId: null }, body: ['action'], requiredBody: ['action'],
  },
  'observability.events': {
    method: 'GET',
    path: '/api/observability/events',
    query: [
      'lastEventId',
      'sessionId',
      'roomId',
      'traceId',
      'runId',
      'category',
      'status',
    ],
    requiredQuery: ['lastEventId'],
    subscription: 'observation',
  },
  'overview.get': { method: 'GET', path: '/api/overview' },

  'agent.runtime.get': {
    method: 'GET',
    path: '/api/agent/runtime',
    responseContract: 'agent-runtime.v1',
  },
  'agent.runtime.ensure': {
    method: 'POST',
    path: '/api/agent/runtime/ensure',
    body: ['sessionId'],
    requiredBody: ['sessionId'],
  },
  'agent.providers.get': { method: 'GET', path: '/api/agent/providers' },
  'agent.provider.auth.preview': {
    method: 'POST',
    path: '/api/agent/providers/auth/preview',
    body: ['provider', 'action'],
    requiredBody: ['provider', 'action'],
  },
  'agent.provider.auth.apply': {
    method: 'POST',
    path: '/api/agent/providers/auth/apply',
    body: ['previewToken', 'confirmText', 'apiKey'],
    requiredBody: ['previewToken', 'confirmText'],
  },
  'agent.provider.oauth.status': {
    method: 'GET',
    path: '/api/agent/providers/oauth/status',
    query: ['loginId'],
    requiredQuery: ['loginId'],
  },
  'agent.provider.oauth.cancel': {
    method: 'POST',
    path: '/api/agent/providers/oauth/cancel',
    body: ['loginId'],
    requiredBody: ['loginId'],
  },
  'agent.configuration.get': { method: 'GET', path: '/api/agent/configuration' },
  'agent.configuration.update': {
    method: 'POST',
    path: '/api/agent/configuration',
    body: ['expectedRevision', 'changes', 'updatedBy'],
    requiredBody: ['expectedRevision', 'changes'],
  },
  'agent.sessions.list': {
    method: 'GET',
    path: '/api/agent/sessions',
    query: [
      'includeArchived',
      'includeInternal',
      'limit',
      'beforeUpdatedAtMs',
      'beforeId',
      'surfaceKind',
      'ownerAppId',
      'surfaceKey',
      'projectionOnly',
    ],
  },
  'agent.eval-lab.runs': {
    method: 'GET',
    path: '/api/agent/eval-lab/runs',
    // The local feature guard owns this seam until the generated contract is
    // added; the route must still remain visible to native/HTTP capability
    // negotiation now.
  },
  'agent.eval-lab.evidence': {
    method: 'GET',
    path: '/api/agent/eval-lab/evidence',
    query: ['runId', 'taskIndex'],
  },
  'agent.eval-lab.golden.get': {
    method: 'GET',
    path: '/api/agent/eval-lab/golden',
    query: ['suiteId'],
  },
  'agent.eval-lab.projects.get': {
    method: 'GET',
    path: '/api/agent/eval-lab/projects',
    query: ['projectId', 'materialSetId', 'artifactId', 'artifactRevision'],
  },
  'agent.eval-lab.projects.command': {
    method: 'POST',
    path: '/api/agent/eval-lab/projects/command',
    body: ['action', 'projectId', 'expectedRevision', 'clientRequestId', 'input'],
    requiredBody: ['action', 'expectedRevision', 'clientRequestId', 'input'],
  },
  'agent.eval-lab.apps.get': { method: 'GET', path: '/api/agent/eval-lab/apps', query: ['appId', 'projectId', 'version', 'callId'] },
  'agent.eval-lab.apps.command': { method: 'POST', path: '/api/agent/eval-lab/apps/command',
    body: ['action', 'appId', 'expectedRevision', 'clientRequestId', 'input'],
    requiredBody: ['action', 'appId', 'expectedRevision', 'clientRequestId', 'input'] },
  'agent.eval-lab.apps.download': { method: 'GET', path: '/api/agent/eval-lab/apps/download', query: ['appId', 'version', 'target'], requiredQuery: ['appId', 'version', 'target'] },
  'agent.eval-lab.trials.get': {
    method: 'GET',
    path: '/api/agent/eval-lab/trials',
    query: ['jobId'],
  },
  'agent.eval-lab.trials.start': {
    method: 'POST',
    path: '/api/agent/eval-lab/trials/start',
    body: ['clientRequestId', 'sceneId', 'spec'],
    requiredBody: ['clientRequestId', 'sceneId', 'spec'],
  },
  'agent.eval-lab.trials.cancel': {
    method: 'POST',
    path: '/api/agent/eval-lab/trials/cancel',
    body: ['jobId'],
    requiredBody: ['jobId'],
  },
  'agent.eval-lab.golden.command': {
    method: 'POST',
    path: '/api/agent/eval-lab/golden/command',
    body: ['action', 'suiteId', 'expectedRevision', 'clientRequestId', 'input'],
    requiredBody: ['action', 'expectedRevision', 'clientRequestId', 'input'],
  },
  'agent.eval-lab.scene-recipes.get': {
    method: 'GET',
    path: '/api/agent/eval-lab/scene-recipes',
    query: ['sceneId', 'experimentId'],
    requiredQuery: ['sceneId'],
  },
  'agent.eval-lab.scene-recipes.apply': {
    method: 'POST',
    path: '/api/agent/eval-lab/scene-recipes/apply',
    body: ['sceneId', 'experimentId', 'expectedRevision', 'clientRequestId'],
    requiredBody: ['sceneId', 'experimentId', 'expectedRevision', 'clientRequestId'],
  },
  'agent.eval-lab.scene-recipes.rollback': {
    method: 'POST',
    path: '/api/agent/eval-lab/scene-recipes/rollback',
    body: ['sceneId', 'expectedRevision', 'clientRequestId'],
    requiredBody: ['sceneId', 'expectedRevision', 'clientRequestId'],
  },
  'agent.sessions.create': {
    method: 'POST',
    path: '/api/agent/sessions',
    body: [
      'title',
      'mode',
      'roleId',
      'roleVersion',
      'modelProfile',
      '_modelRoute',
      'toolProfileVersion',
      'executionMode',
      'workspaceRoots',
      'workspaceScopeConfirmation',
      'dangerousModeConfirmation',
      'toolAllowlistMode',
      'allowedTools',
      'projectContextEnabled',
      'piSkillsEnabled',
      'codexSkillsEnabled',
      'surfaceKind',
      'ownerAppId',
      'surfaceKey',
    ],
  },
  'agent.sessions.surface.ensure': {
    method: 'POST',
    path: '/api/agent/sessions/surface/ensure',
    body: [
      'title',
      'mode',
      'toolProfileVersion',
      'executionMode',
      'workspaceRoots',
      'surfaceKind',
      'ownerAppId',
      'surfaceKey',
    ],
    requiredBody: ['title', 'surfaceKind', 'ownerAppId', 'surfaceKey'],
  },
  'agent.session.snapshot': {
    method: 'GET',
    path: '/api/agent/sessions/:sessionId/messages',
    params: { sessionId: null },
    query: ['view'],
  },
  'agent.session.workspace.list': {
    method: 'GET',
    path: '/api/agent/sessions/:sessionId/workspace',
    params: { sessionId: null },
    query: ['path', 'depth', 'limit'],
  },
  'files.list': {
    method: 'GET',
    path: '/api/files/list',
    query: ['path', 'offset', 'limit'],
  },
  'files.read': {
    method: 'GET',
    path: '/api/files/read',
    query: ['path', 'offset', 'limit', 'sessionId'],
    requiredQuery: ['path'],
  },
  'agent.session.workspace.read': {
    method: 'GET',
    path: '/api/agent/sessions/:sessionId/workspace-file',
    params: { sessionId: null },
    query: ['path', 'offset', 'limit'],
    requiredQuery: ['path'],
  },
  'agent.session.workspace.save': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/workspace-file',
    params: { sessionId: null },
    body: ['path', 'content', 'resourceRevision'],
    requiredBody: ['path', 'content', 'resourceRevision'],
  },
  'agent.session.rename': {
    method: 'PATCH',
    path: '/api/agent/sessions/:sessionId',
    params: { sessionId: null },
    body: ['title'],
    requiredBody: ['title'],
  },
  'agent.session.archive': {
    method: 'PATCH',
    path: '/api/agent/sessions/:sessionId',
    params: { sessionId: null },
    body: ['archived'],
    requiredBody: ['archived'],
  },
  'agent.session.mode.update': {
    method: 'PATCH',
    path: '/api/agent/sessions/:sessionId',
    params: { sessionId: null },
    body: ['mode', 'executionMode', 'workspaceRoots', 'workspaceScopeConfirmation', 'toolProfileVersion', 'toolAllowlistMode', 'allowedTools', 'dangerousModeConfirmation', 'projectContextEnabled', 'piSkillsEnabled', 'codexSkillsEnabled'],
    requiredBody: ['mode'],
  },
  'agent.session.capability-policy.update': {
    method: 'PATCH',
    path: '/api/agent/sessions/:sessionId',
    params: { sessionId: null },
    body: ['capabilityDisclosurePreferences'],
    requiredBody: ['capabilityDisclosurePreferences'],
  },
  'agent.session.delete': {
    method: 'DELETE',
    path: '/api/agent/sessions/:sessionId',
    params: { sessionId: null },
  },
  'agent.session.prompt': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/prompt',
    params: { sessionId: null },
    body: ['message', 'attachments', 'clientMessageId', 'retryOfClientMessageId', 'delivery', 'screenContext'],
    requiredBody: ['message'],
  },
  'agent.session.rewrite': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/rewrite',
    params: { sessionId: null },
    body: ['entryId', 'message', 'attachments', 'clientMessageId'],
    requiredBody: ['entryId', 'message'],
  },
  'agent.session.forks.list': {
    method: 'GET',
    path: '/api/agent/sessions/:sessionId/forks',
    params: { sessionId: null },
    responseContract: 'agent-session-fork-candidates.v1',
  },
  'agent.session.forks.create': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/forks',
    params: { sessionId: null },
    body: ['entryId', 'title'],
    requiredBody: ['entryId'],
    responseContract: 'agent-session-fork-create.v1',
  },
  'agent.session.abort': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/abort',
    params: { sessionId: null },
  },
  'agent.session.review.resolve': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/review',
    params: { sessionId: null },
    body: ['runId', 'decision'],
    requiredBody: ['runId', 'decision'],
  },
  'agent.session.ui.resolve': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/ui-response',
    params: { sessionId: null },
    body: ['requestId', 'value', 'confirmed', 'cancelled', 'resolutionSource'],
    requiredBody: ['requestId'],
  },
  'agent.session.compact': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/compact',
    params: { sessionId: null },
    body: ['instructions'],
  },
  'agent.session.workflow.get': {
    method: 'GET',
    path: '/api/agent/sessions/:sessionId/workflow',
    params: { sessionId: null },
    responseContract: 'agent-workflow-state.v1',
  },
  'agent.session.goal.mutate': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/goal',
    params: { sessionId: null },
    body: ['action', 'expectedRevision', 'confirmed', 'objective', 'successCriteria', 'evidenceExpectations', 'tokenBudget', 'timeBudgetMs', 'summary', 'reason', 'evidence'],
    requiredBody: ['action'],
    responseContract: 'agent-workflow-state.v1',
  },
  'agent.session.commands': {
    method: 'GET',
    path: '/api/agent/sessions/:sessionId/commands',
    params: { sessionId: null },
  },
  'agent.session.command.invoke': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/commands',
    params: { sessionId: null },
    body: ['command'],
    requiredBody: ['command'],
  },
  'agent.session.models': {
    method: 'GET',
    path: '/api/agent/sessions/:sessionId/models',
    params: { sessionId: null },
    responseContract: 'agent-model-catalog.v1',
  },
  'agent.session.model.select': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/model',
    params: { sessionId: null },
    body: ['provider', 'modelId'],
    requiredBody: ['provider', 'modelId'],
  },
  'agent.session.thinking.select': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/thinking',
    params: { sessionId: null },
    body: ['level'],
    requiredBody: ['level'],
  },
  'agent.session.events': {
    method: 'GET',
    path: '/api/agent/sessions/:sessionId/events',
    params: { sessionId: null },
    query: ['lastEventId'],
    requiredQuery: ['lastEventId'],
    subscription: 'agent',
  },
  'agent.session.backgroundJobs.list': {
    method: 'GET',
    path: '/api/agent/sessions/:sessionId/background-jobs',
    params: { sessionId: null },
    query: ['limit', 'status'],
  },
  'agent.session.backgroundJob.get': {
    method: 'GET',
    path: '/api/agent/sessions/:sessionId/background-jobs/:jobId',
    params: { sessionId: null, jobId: null },
  },
  'agent.session.backgroundJob.logs': {
    method: 'GET',
    path: '/api/agent/sessions/:sessionId/background-jobs/:jobId/logs',
    params: { sessionId: null, jobId: null },
    query: ['cursor', 'limitBytes'],
  },
  'agent.session.backgroundJob.cancel': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/background-jobs/:jobId/cancel',
    params: { sessionId: null, jobId: null },
    body: ['reason', 'roomTurnId'],
  },
  'agent.session.intercom.list': {
    method: 'GET',
    path: '/api/agent/sessions/:sessionId/intercom',
    params: { sessionId: null },
    query: ['status', 'limit'],
  },
  'agent.session.intercom.send': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/intercom',
    params: { sessionId: null },
    body: ['kind', 'targetParticipantId', 'clientMessageId', 'replyTo', 'content'],
    requiredBody: ['kind', 'clientMessageId', 'content'],
  },
  'agent.session.contextItems.list': {
    method: 'GET',
    path: '/api/agent/sessions/:sessionId/context-items',
    params: { sessionId: null },
    query: ['status', 'limit'],
  },
  'agent.session.contextItems.ack': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/context-items/:itemId/ack',
    params: { sessionId: null, itemId: null },
  },
  'agent.session.contextTraces.list': {
    method: 'GET',
    path: '/api/agent/sessions/:sessionId/context-traces',
    params: { sessionId: null },
    query: ['limit'],
  },
  'agent.session.contextTrace.get': {
    method: 'GET',
    path: '/api/agent/sessions/:sessionId/context-traces/:traceId',
    params: { sessionId: null, traceId: null },
    responseContract: 'agent-context-trace.v1',
  },
  'agent.session.debugContext.get': {
    method: 'GET',
    path: '/api/agent/sessions/:sessionId/debug-context',
    params: { sessionId: null },
    query: ['turnId'],
  },
  'agent.artifact.get': {
    method: 'GET',
    path: '/api/agent/artifacts/:artifactId',
    params: { artifactId: null },
    query: ['sessionId', 'limit'],
    requiredQuery: ['sessionId'],
  },
  'agent.media.list': {
    method: 'GET',
    path: '/api/agent/media',
    query: ['sessionId', 'limit'],
    requiredQuery: ['sessionId'],
  },
  'agent.media.preview': {
    method: 'GET',
    path: '/api/agent/media/:mediaId/preview',
    params: { mediaId: null },
    query: ['sessionId', 'sha256'],
    requiredQuery: ['sessionId'],
    responseContract: 'agent-file-preview.v1',
  },
  'agent.deep-search': {
    method: 'POST',
    path: '/api/agent/deep-search',
    body: ['query', 'privacyDisposition', 'context', 'frontAppBundleId', 'contextSource', 'evidence'],
    requiredBody: ['query', 'privacyDisposition'],
  },
  'agent.rooms.list': {
    method: 'GET',
    path: '/api/agent/rooms',
    query: ['includeArchived', 'limit', 'beforeUpdatedAtMs', 'beforeId', 'projectionOnly', 'ownerAppId', 'surfaceKey'],
  },
  'agent.rooms.create': {
    method: 'POST',
    path: '/api/agent/rooms',
    body: [
      'title',
      'roomKind',
      'avatar',
      'description',
      'scenarioPrompt',
      'participants',
      'routingPolicy',
      'routingConfig',
      'moderatorRoleId',
      'workspaceRoots',
      'permissionPolicy',
      'workspaceScopeConfirmation',
      'dangerousModeConfirmation',
      'ownerAppId',
      'surfaceKey',
    ],
    requiredBody: ['participants'],
  },
  'agent.room.get': {
    method: 'GET',
    path: '/api/agent/rooms/:roomId',
    params: { roomId: null },
  },
  'agent.room.snapshot': {
    method: 'GET',
    path: '/api/agent/rooms/:roomId/snapshot',
    params: { roomId: null },
    responseContract: 'agent-room-snapshot.v1',
  },
  'agent.room.conversationSnapshot': {
    method: 'GET',
    path: '/api/agent/rooms/:roomId/conversation',
    params: { roomId: null },
    responseContract: 'agent-room-conversation-snapshot.v1',
  },
  'agent.room.history': {
    method: 'GET',
    path: '/api/agent/rooms/:roomId/history',
    params: { roomId: null },
    query: ['beforeSequence', 'limit'],
    responseContract: 'agent-room-event-page.v1',
  },
  'agent.room.archive': {
    method: 'PATCH',
    path: '/api/agent/rooms/:roomId',
    params: { roomId: null },
    body: [
      'archived',
      'title',
      'roomKind',
      'avatar',
      'description',
      'scenarioPrompt',
      'routingPolicy',
      'routingConfig',
      'moderatorParticipantId',
      'permissionPolicy',
      'workspaceRoots',
      'workspaceScopeConfirmation',
      'dangerousModeConfirmation',
    ],
  },
  'agent.room.participant.add': {
    method: 'POST',
    path: '/api/agent/rooms/:roomId/participants',
    params: { roomId: null },
    body: ['roleId', 'roleVersion', 'collaborationRole'],
    requiredBody: ['roleId'],
  },
  'agent.room.participant.remove': {
    method: 'PATCH',
    path: '/api/agent/rooms/:roomId/participants',
    params: { roomId: null },
    body: ['participantId'],
    requiredBody: ['participantId'],
  },
  'agent.room.participant.update': {
    method: 'PATCH',
    path: '/api/agent/rooms/:roomId/participants',
    params: { roomId: null },
    body: ['participantId', 'collaborationRole'],
    requiredBody: ['participantId', 'collaborationRole'],
  },
  'agent.room.delete': {
    method: 'DELETE',
    path: '/api/agent/rooms/:roomId',
    params: { roomId: null },
    body: ['confirmTitle'],
    requiredBody: ['confirmTitle'],
  },
  'agent.room.message': {
    method: 'POST',
    path: '/api/agent/rooms/:roomId/messages',
    params: { roomId: null },
    body: [
      'message',
      'clientMessageId',
      'retryOfRootId',
      'participantIds',
      'workItemId',
      'attachmentIds',
      'answerToPostId',
      'answerToRootId',
    ],
    requiredBody: ['message'],
  },
  'agent.room.startGate.get': {
    method: 'GET',
    path: '/api/agent/rooms/:roomId/start-gate',
    params: { roomId: null },
  },
  'agent.room.startGate.confirm': {
    method: 'POST',
    path: '/api/agent/rooms/:roomId/start-gate',
    params: { roomId: null },
    body: ['gateId', 'decision', 'action'],
  },
  'agent.room.participant.steer': {
    method: 'POST',
    path: '/api/agent/rooms/:roomId/steer',
    params: { roomId: null },
    body: ['action', 'rootId', 'participantId', 'clientActionId', 'message'],
    requiredBody: ['action', 'rootId', 'clientActionId', 'message'],
  },
  'agent.room.abort': {
    method: 'POST',
    path: '/api/agent/rooms/:roomId/abort',
    params: { roomId: null },
    body: ['roomTurnId', 'clientRequestId'],
    requiredBody: ['roomTurnId', 'clientRequestId'],
  },
  'agent.room.events': {
    method: 'GET',
    path: '/api/agent/rooms/:roomId/events',
    params: { roomId: null },
    query: ['lastEventId'],
    requiredQuery: ['lastEventId'],
    subscription: 'room',
  },
  'agent.collaborationProfile.get': {
    method: 'GET',
    path: '/api/agent/collaboration-profiles/:profileId',
    params: { profileId: null },
    responseContract: 'collaboration-profile-projection.v1',
  },
  'agent.collaborationProfile.command': {
    method: 'POST',
    path: '/api/agent/collaboration-profiles/commands',
    body: ['schemaVersion', 'commandId', 'action', 'idempotencyKey', 'actorRef', 'profileId', 'candidateId', 'contentHash', 'expectedPointerRevision', 'activationScope', 'adminConfirmation', 'payload', 'createdAtMs'],
    requiredBody: ['schemaVersion', 'commandId', 'action', 'idempotencyKey', 'actorRef', 'payload', 'createdAtMs'],
    responseContract: 'collaboration-profile-command-receipt.v1',
  },
  'agent.knowledge.search': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/knowledge-search',
    params: { sessionId: null },
    body: ['query', 'limit', 'retrievalReceiptId', 'createdAtMs'],
    requiredBody: ['query'],
  },
  'agent.governance.read': {
    method: 'GET',
    path: '/api/agent/governance',
    query: ['scopeKey'],
  },
  'agent.knowledgeGovernance.read': {
    method: 'GET',
    path: '/api/agent/knowledge-governance',
  },
  'agent.knowledge.read': {
    method: 'POST',
    path: '/api/agent/sessions/:sessionId/knowledge-read',
    params: { sessionId: null },
    body: ['retrievalReceiptId', 'claimRef', 'expectedHash'],
    requiredBody: ['retrievalReceiptId', 'claimRef', 'expectedHash'],
  },
  'agent.room.topics': {
    method: 'GET',
    path: '/api/agent/rooms/:roomId/topics',
    params: { roomId: null },
    query: ['includeArchived'],
  },
  'agent.room.topic.create': {
    method: 'POST',
    path: '/api/agent/rooms/:roomId/topics',
    params: { roomId: null },
    body: ['title', 'summary'],
    requiredBody: ['title'],
  },
  'agent.room.topic.update': {
    method: 'PATCH',
    path: '/api/agent/rooms/:roomId/topics',
    params: { roomId: null },
    body: ['topicId', 'title', 'summary', 'activate', 'archived'],
    requiredBody: ['topicId'],
  },
  'agent.room.artifacts': {
    method: 'GET',
    path: '/api/agent/rooms/:roomId/artifacts',
    params: { roomId: null },
    query: ['includeArchived', 'topicId', 'limit'],
  },
  'agent.room.artifact.add': {
    method: 'POST',
    path: '/api/agent/rooms/:roomId/artifacts',
    params: { roomId: null },
    body: ['path', 'displayName', 'topicId', 'mediaType', 'participantId'],
    requiredBody: ['path'],
  },
  'agent.room.artifact.update': {
    method: 'PATCH',
    path: '/api/agent/rooms/:roomId/artifacts',
    params: { roomId: null },
    body: ['artifactId', 'archived'],
    requiredBody: ['artifactId', 'archived'],
  },
  'agent.room.workItems.list': {
    method: 'GET',
    path: '/api/agent/rooms/:roomId/work-items',
    params: { roomId: null },
    query: ['state', 'ownerParticipantId', 'limit'],
  },
  'agent.room.workItem.create': {
    method: 'POST',
    path: '/api/agent/rooms/:roomId/work-items',
    params: { roomId: null },
    body: ['objective', 'expectedOutput', 'currentOwnerParticipantId', 'createdByParticipantId', 'clientMessageId', 'accountableParticipantId', 'topicId', 'rootTurnId', 'parentWorkId', 'acceptanceCriteria', 'state', 'depth'],
    requiredBody: ['objective', 'expectedOutput', 'currentOwnerParticipantId', 'clientMessageId'],
  },
  'agent.room.workItem.get': {
    method: 'GET',
    path: '/api/agent/rooms/:roomId/work-items/:workItemId',
    params: { roomId: null, workItemId: null },
  },
  'agent.room.workItem.reassign': {
    method: 'POST',
    path: '/api/agent/rooms/:roomId/work-items/:workItemId/reassign',
    params: { roomId: null, workItemId: null },
    body: ['actorParticipantId', 'targetParticipantId', 'reason'],
    requiredBody: ['actorParticipantId', 'targetParticipantId'],
  },
  'agent.room.workItem.resume': {
    method: 'POST',
    path: '/api/agent/rooms/:roomId/work-items/:workItemId/resume',
    params: { roomId: null, workItemId: null },
    body: ['actorParticipantId', 'clientActionId', 'phase', 'timeoutSeconds'],
    requiredBody: ['actorParticipantId'],
  },
  'agent.roles.list': { method: 'GET', path: '/api/agent/roles' },
  'agent.roles.create': {
    method: 'POST',
    path: '/api/agent/roles',
    body: ['displayName', 'tagline', 'summary', 'traits', 'timelineModel', 'selectableModes', 'suitableTasks', 'unsuitableTasks'],
    requiredBody: ['displayName', 'tagline', 'summary', 'traits', 'timelineModel', 'selectableModes', 'suitableTasks', 'unsuitableTasks'],
  },
  'agent.roles.update': {
    method: 'PATCH',
    path: '/api/agent/roles',
    body: ['roleId', 'roleVersion', 'displayName', 'tagline', 'summary', 'traits', 'timelineModel', 'selectableModes', 'suitableTasks', 'unsuitableTasks'],
    requiredBody: ['roleId', 'roleVersion', 'displayName', 'tagline', 'summary', 'traits', 'timelineModel', 'selectableModes', 'suitableTasks', 'unsuitableTasks'],
  },
  'agent.roles.archive': {
    method: 'DELETE',
    path: '/api/agent/roles',
    body: ['roleId', 'roleVersion'],
    requiredBody: ['roleId', 'roleVersion'],
  },
  'agent.role.models': {
    method: 'GET',
    path: '/api/agent/roles/models',
  },
  'agent.role.runtimeDefaults.update': {
    method: 'POST',
    path: '/api/agent/roles/runtime-defaults',
    body: ['roleId', 'roleVersion', 'provider', 'modelId', 'thinkingLevel'],
    requiredBody: ['roleId', 'roleVersion', 'provider', 'modelId', 'thinkingLevel'],
  },
  'agent.roleBook.get': {
    method: 'GET',
    path: '/api/agent/role-book',
    query: ['roleId', 'roleVersion', 'limit'],
    requiredQuery: ['roleId', 'roleVersion'],
  },
  'agent.roleBook.activation.preview': {
    method: 'POST',
    path: '/api/agent/role-book/activation/preview',
    body: ['roleId', 'roleVersion', 'revisionId', 'draftId', 'traitIndexes', 'capabilityIndexes', 'lessonIndexes', 'commitmentIndexes'],
    requiredBody: ['roleId', 'roleVersion'],
  },
  'agent.roleBook.activation.apply': {
    method: 'POST',
    path: '/api/agent/role-book/activation/apply',
    body: ['roleId', 'roleVersion', 'revisionId', 'draftId', 'traitIndexes', 'capabilityIndexes', 'lessonIndexes', 'commitmentIndexes', 'previewToken', 'payloadSha256', 'confirmText'],
    requiredBody: ['roleId', 'roleVersion', 'previewToken', 'payloadSha256', 'confirmText'],
  },
  'agent.roleBook.activation.rollback': {
    method: 'POST',
    path: '/api/agent/role-book/activation/rollback',
    body: ['receiptId', 'rollbackToken', 'payloadSha256', 'confirmText'],
    requiredBody: ['receiptId', 'rollbackToken', 'payloadSha256', 'confirmText'],
  },
  'agent.roleBook.draft.decision': {
    method: 'POST',
    path: '/api/agent/role-book/drafts/decision',
    body: ['roleId', 'roleVersion', 'draftId', 'decision'],
    requiredBody: ['roleId', 'roleVersion', 'draftId', 'decision'],
  },
  'agent.personalContext.observability': {
    method: 'GET',
    path: '/api/agent/personal-context/observability',
    query: ['sessionId', 'roleId', 'limit'],
  },
  'agent.tools.list': { method: 'GET', path: '/api/agent/tools', query: ['sessionId'] },
  'agent.extensions.list': { method: 'GET', path: '/api/agent/extensions' },
  'agent.extensions.usage': {
    method: 'GET',
    path: '/api/agent/extensions/usage',
    query: ['packageId', 'resourceKind', 'sessionId', 'sinceMs', 'limit'],
  },
  'agent.extensions.catalog': { method: 'GET', path: '/api/agent/extensions/catalog' },
  'agent.extensions.skills.list': {
    method: 'GET',
    path: '/api/agent/extensions/skills',
  },
  'agent.extensions.skills.get': {
    method: 'GET',
    path: '/api/agent/extensions/skills/detail',
    query: ['skillId'],
    requiredQuery: ['skillId'],
  },
  'agent.extensions.create': {
    method: 'POST',
    path: '/api/agent/extensions/drafts',
    body: ['draftId', 'packageJson', 'files'],
    requiredBody: ['draftId', 'packageJson', 'files'],
  },
  'agent.extensions.proposals': { method: 'GET', path: '/api/agent/extensions/proposals' },
  'agent.extensions.validate': {
    method: 'POST',
    path: '/api/agent/extensions/validate',
    body: ['sourcePath', 'packageSource', 'catalogId', 'catalogVersion'],
  },
  'agent.extensions.preview': {
    method: 'POST',
    path: '/api/agent/extensions/preview',
    body: ['action', 'validationToken', 'pluginId', 'enable'],
    requiredBody: ['action'],
  },
  'agent.extensions.apply': {
    method: 'POST',
    path: '/api/agent/extensions/apply',
    body: ['previewToken', 'payloadSha256', 'confirmText'],
    requiredBody: ['previewToken', 'payloadSha256', 'confirmText'],
  },
  'agent.lifecycleHooks.get': {
    method: 'GET',
    path: '/api/agent/lifecycle-hooks',
    query: ['limit'],
  },
  'agent.lifecycleHooks.update': {
    method: 'PATCH',
    path: '/api/agent/lifecycle-hooks',
    body: ['eventType', 'enabled', 'action', 'tokenLimit', 'cooldownSeconds'],
    requiredBody: ['eventType'],
  },
  'agent.approvals.list': {
    method: 'GET',
    path: '/api/agent/approvals',
    query: ['sessionId', 'state', 'limit'],
  },
  'agent.approval.get': {
    method: 'GET',
    path: '/api/agent/approvals/:approvalId',
    params: { approvalId: null },
  },
  'agent.approval.decide': {
    method: 'POST',
    path: '/api/agent/approvals/:approvalId/decision',
    params: { approvalId: null },
    body: ['decision', 'payloadSha256'],
    requiredBody: ['decision', 'payloadSha256'],
  },
  'agent.memoryMaintenance.run': {
    method: 'GET',
    path: '/api/agent/memory-maintenance',
    query: ['runId', 'jobId', 'project', 'limit', 'projectionOnly'],
  },
  'agent.memoryMaintenance.trigger': {
    method: 'POST',
    path: '/api/agent/memory-maintenance',
    body: ['project', 'ownerKind', 'ownerId', 'instruction', 'manual', 'maxSources', 'catalogOnly'],
  },
  'agent.subagents.templates': {
    method: 'GET',
    path: '/api/agent/subagents/templates',
  },
  'agent.subagents.list': {
    method: 'GET',
    path: '/api/agent/subagents/runs',
    query: ['sessionId', 'limit'],
    requiredQuery: ['sessionId'],
  },
  'agent.subagents.create': {
    method: 'POST',
    path: '/api/agent/subagents/runs',
    body: [
      'sessionId', 'tasks', 'agent', 'version', 'task', 'expectedOutput',
      'acceptanceCriteria', 'outputSchema', 'modelProfile', 'thinkingLevel',
      'access', 'allowedTools', 'piSkillsEnabled', 'codexSkillsEnabled',
      'workspaceRoots', 'todoTask', 'contextMode', 'forkEntryId', 'wait',
    ],
    requiredBody: ['sessionId'],
  },
  'agent.subagent.get': {
    method: 'GET',
    path: '/api/agent/subagents/runs/:runId',
    params: { runId: null },
    query: ['sessionId'],
    requiredQuery: ['sessionId'],
  },
  'agent.subagent.console': {
    method: 'GET',
    path: '/api/agent/subagents/runs/:runId/console',
    params: { runId: null },
    query: ['sessionId'],
    requiredQuery: ['sessionId'],
  },
  'agent.subagent.control': {
    method: 'POST',
    path: '/api/agent/subagents/runs/:runId/control',
    params: { runId: null },
    body: ['sessionId', 'action', 'clientActionId', 'message', 'inboxId'],
    requiredBody: ['sessionId', 'action', 'clientActionId'],
  },
  'agent.subagent.abort': {
    method: 'POST',
    path: '/api/agent/subagents/runs/:runId/abort',
    params: { runId: null },
    body: ['sessionId'],
    requiredBody: ['sessionId'],
  },
  'agent.memorySources.list': {
    method: 'GET',
    path: '/api/agent/memory-sources',
    query: ['sessionId', 'limit'],
    requiredQuery: ['sessionId'],
  },
  'agent.wakeSchedules.list': {
    method: 'GET',
    path: '/api/agent/wake-schedules',
    query: ['status', 'targetType', 'targetId', 'createdBySessionId', 'limit'],
  },
  'agent.wakeSchedules.create': {
    method: 'POST',
    path: '/api/agent/wake-schedules',
    body: ['title', 'instruction', 'targetType', 'targetSessionId', 'targetRoleId', 'targetRoleVersion', 'wakeAtMs', 'timezone', 'recurrenceKind', 'recurrenceInterval', 'maxRuns', 'planningTaskId', 'confirmText'],
    requiredBody: ['instruction', 'targetType', 'wakeAtMs', 'confirmText'],
  },
  'agent.wakeSchedule.runs': {
    method: 'GET',
    path: '/api/agent/wake-schedules/:scheduleId/runs',
    params: { scheduleId: null },
    query: ['limit'],
  },
  'agent.wakeSchedule.action': {
    method: 'POST',
    path: '/api/agent/wake-schedules/:scheduleId/action',
    params: { scheduleId: null },
    body: ['action', 'confirmText', 'schedule'],
    requiredBody: ['action', 'confirmText'],
  },

  'browser.status': { method: 'GET', path: '/api/browser/status' },
  'browser.tabs': { method: 'GET', path: '/api/browser/tabs' },
  'browser.snapshot.latest': {
    method: 'GET',
    path: '/api/browser/snapshots/latest',
    query: ['deviceId', 'tabId', 'includeMarkdown'],
  },
  'browser.snapshot.image': {
    method: 'GET',
    path: '/api/browser/snapshots/:snapshotId/image',
    params: { snapshotId: null },
    binary: true,
  },
  'browser.traces': {
    method: 'GET',
    path: '/api/browser/traces',
    query: ['limit'],
  },
  'browser.command': {
    method: 'POST',
    path: '/api/browser/command',
    body: [
      'action',
      'deviceId',
      'tabId',
      'refId',
      'url',
      'text',
      'script',
      'clear',
      'submit',
      'direction',
      'amount',
      'timeoutMs',
      'timeoutSeconds',
    ],
    requiredBody: ['action'],
  },
  'browser.stop': { method: 'POST', path: '/api/browser/stop' },
  'browser.managed.start': { method: 'POST', path: '/api/browser/managed/start' },
  'browser.managed.stop': { method: 'POST', path: '/api/browser/managed/stop' },

  'terminal.sessions.list': { method: 'GET', path: '/api/terminal/sessions' },
  'terminal.session.create': {
    method: 'POST',
    path: '/api/terminal/sessions',
    body: ['title', 'cwd', 'shell', 'cols', 'rows'],
  },
  'terminal.session.read': {
    method: 'POST',
    path: '/api/terminal/read',
    body: ['terminalId', 'cursor', 'maxBytes'],
    requiredBody: ['terminalId'],
  },
  'terminal.session.write': {
    method: 'POST',
    path: '/api/terminal/write',
    body: ['terminalId', 'text'],
    requiredBody: ['terminalId', 'text'],
  },
  'terminal.session.resize': {
    method: 'POST',
    path: '/api/terminal/resize',
    body: ['terminalId', 'cols', 'rows'],
    requiredBody: ['terminalId', 'cols', 'rows'],
  },
  'terminal.session.close': {
    method: 'POST',
    path: '/api/terminal/close',
    body: ['terminalId'],
    requiredBody: ['terminalId'],
  },

  'planning.dashboard': {
    method: 'GET',
    path: '/api/planning/dashboard',
    query: ['date', 'project'],
  },
  'planning.mutation.preview': {
    method: 'POST',
    path: '/api/planning/mutation/preview',
    body: ['kind', 'payload', 'expectedRuntimeRevision'],
    requiredBody: ['kind', 'payload', 'expectedRuntimeRevision'],
  },
  'planning.task.save': {
    method: 'POST',
    path: '/api/planning/task/save',
    body: ['taskId', 'date', 'title', 'detail', 'priority', 'status', 'dueAtMs', 'goalId', 'project', 'expectedRuntimeRevision', 'previewToken', 'payloadSha256', 'confirmText'],
    requiredBody: ['date', 'title', 'expectedRuntimeRevision', 'previewToken', 'payloadSha256', 'confirmText'],
  },
  'planning.goal.save': {
    method: 'POST',
    path: '/api/planning/goal/save',
    body: ['goalId', 'title', 'detail', 'horizon', 'status', 'priority', 'targetDate', 'project', 'expectedRuntimeRevision', 'previewToken', 'payloadSha256', 'confirmText'],
    requiredBody: ['title', 'expectedRuntimeRevision', 'previewToken', 'payloadSha256', 'confirmText'],
  },
  'planning.task.action': {
    method: 'POST',
    path: '/api/planning/task/action',
    body: ['taskId', 'action', 'expectedRuntimeRevision', 'previewToken', 'payloadSha256', 'confirmText'],
    requiredBody: ['taskId', 'action', 'expectedRuntimeRevision', 'previewToken', 'payloadSha256', 'confirmText'],
  },
  'planning.taskEvent.undo': {
    method: 'POST',
    path: '/api/planning/task-event/undo',
    body: ['eventId', 'receiptId', 'rollbackToken', 'payloadSha256', 'confirmText'],
    requiredBody: ['eventId', 'receiptId', 'rollbackToken', 'payloadSha256', 'confirmText'],
  },
  'planning.mutation.rollback': {
    method: 'POST',
    path: '/api/planning/mutation/rollback',
    body: ['receiptId', 'rollbackToken', 'payloadSha256', 'confirmText'],
    requiredBody: ['receiptId', 'rollbackToken', 'payloadSha256', 'confirmText'],
  },
  'workDocuments.list': {
    method: 'GET',
    path: '/api/agent/work-documents',
    query: ['limit'],
    responseContract: 'work-document-list.v1',
  },
  'workDocuments.history.search': {
    method: 'GET',
    path: '/api/agent/work-documents/history/search',
    query: ['query', 'limit'],
    responseContract: 'work-document-list.v1',
  },
  'workDocuments.get': {
    method: 'GET',
    path: '/api/agent/work-documents/:documentId',
    params: { documentId: null },
    responseContract: 'work-document-detail.v1',
  },
  'workDocuments.register': {
    method: 'POST',
    path: '/api/agent/work-documents',
    body: ['authorityKind', 'authorityId', 'authorityRevision', 'workspaceRoot', 'sourcePath', 'title'],
    requiredBody: ['authorityKind', 'authorityId', 'authorityRevision', 'workspaceRoot', 'sourcePath'],
    responseContract: 'work-document-command.v1',
  },
  'workDocuments.archive': {
    method: 'POST',
    path: '/api/agent/work-documents/:documentId/archive',
    params: { documentId: null },
    body: ['terminalReceiptId'],
    requiredBody: ['terminalReceiptId'],
    responseContract: 'work-document-command.v1',
  },
  'workDocuments.repair': {
    method: 'POST',
    path: '/api/agent/work-documents/:documentId/repair',
    params: { documentId: null },
    body: [],
    responseContract: 'work-document-command.v1',
  },
  'workDocuments.reopen': {
    method: 'POST',
    path: '/api/agent/work-documents/:documentId/reopen',
    params: { documentId: null },
    body: ['authorityRevision', 'transitionReceiptId'],
    requiredBody: ['authorityRevision', 'transitionReceiptId'],
    responseContract: 'work-document-command.v1',
  },
  'workDocuments.erase.preview': {
    method: 'POST',
    path: '/api/agent/work-documents/:documentId/erase-preview',
    params: { documentId: null },
    body: ['sessionId'],
    requiredBody: ['sessionId'],
    responseContract: 'work-document-command.v1',
  },
  'workDocuments.erase': {
    method: 'POST',
    path: '/api/agent/work-documents/:documentId/erase',
    params: { documentId: null },
    body: ['sessionId', 'approvalId', 'payloadSha256'],
    requiredBody: ['sessionId', 'approvalId', 'payloadSha256'],
    responseContract: 'work-document-command.v1',
  },
  'memory.summary': { method: 'GET', path: '/api/memory/summary' },
  'memory.pages': {
    method: 'GET',
    path: '/api/memory/:kind',
    params: { kind: ['apps', 'books', 'atoms', 'tags', 'phrases', 'evidence', 'groups', 'negative'] },
    query: ['limit', 'cursor', 'query', 'status', 'ownerKind', 'ownerId'],
  },
  'memory.reference.get': {
    method: 'GET',
    path: '/api/memory/references/:kind/:referenceId',
    params: {
      kind: ['event', 'evidence', 'atom', 'book', 'timeline', 'role_book_revision'],
      referenceId: null,
    },
    responseContract: 'memory-reference.v1',
  },
  'memory.graph.get': {
    method: 'GET',
    path: '/api/memory/graph',
    query: ['plane', 'project', 'status', 'query', 'focusId', 'depth', 'nodeLimit', 'edgeLimit', 'minWeight'],
    requiredQuery: ['plane'],
    responseContract: 'memory-graph.v1',
  },
  'memory.entity.get': {
    method: 'GET',
    path: '/api/memory/entities/:kind/:entityId',
    params: { kind: ['tag', 'group', 'book'], entityId: null },
    query: ['project', 'connectionsLimit', 'connectionsCursor', 'membersLimit', 'membersCursor'],
    responseContract: 'memory-entity.v1',
  },
  'memory.edit': {
    method: 'POST',
    path: '/api/memory/edit',
    body: ['kind', 'id', 'title', 'text', 'summary', 'note', 'description', 'tags', 'aliases', 'type', 'color', 'reason', 'active'],
    requiredBody: ['kind', 'id'],
  },
  'memory.source.disposition': {
    method: 'POST',
    path: '/api/memory/source/disposition',
    body: ['sourceId', 'evidenceId', 'disposition'],
    requiredBody: ['disposition'],
  },
  'memory.lifecycle.status': {
    method: 'GET',
    path: '/api/memory/lifecycle/status',
    query: ['project'],
  },
  'memory.lifecycle.refresh': {
    method: 'POST',
    path: '/api/memory/lifecycle/refresh',
    body: ['operation', 'project', 'date', 'timezone', 'scheduled'],
    requiredBody: ['operation', 'project'],
  },
  'memory.lifecycle.report': {
    method: 'GET',
    path: '/api/memory/lifecycle/report',
    query: ['project', 'date', 'timezone', 'noTimeline'],
    requiredQuery: ['project', 'date', 'timezone'],
  },
  'memory.lifecycle.forget.preview': {
    method: 'POST',
    path: '/api/memory/lifecycle/forget/preview',
    body: ['project', 'sourceId'],
    requiredBody: ['project', 'sourceId'],
  },
  'memory.lifecycle.forget.apply': {
    method: 'POST',
    path: '/api/memory/lifecycle/forget/apply',
    body: ['project', 'sourceId', 'expectedPlanDigest', 'confirmText'],
    requiredBody: ['project', 'sourceId', 'expectedPlanDigest', 'confirmText'],
  },
  'memory.book.archive.preview': {
    method: 'POST',
    path: '/api/memory/book/archive/preview',
    body: ['bookId', 'archived', 'reason', 'expectedRuntimeRevision'],
    requiredBody: ['bookId', 'archived', 'expectedRuntimeRevision'],
  },
  'memory.book.archive.apply': {
    method: 'POST',
    path: '/api/memory/book/archive/apply',
    body: ['bookId', 'archived', 'reason', 'expectedRuntimeRevision', 'previewToken', 'payloadSha256', 'confirmText'],
    requiredBody: ['bookId', 'archived', 'reason', 'expectedRuntimeRevision', 'previewToken', 'payloadSha256', 'confirmText'],
  },
  'memory.book.archive.rollback': {
    method: 'POST',
    path: '/api/memory/book/archive/rollback',
    body: ['receiptId', 'rollbackToken', 'payloadSha256', 'confirmText'],
    requiredBody: ['receiptId', 'rollbackToken', 'payloadSha256', 'confirmText'],
  },
  'memory.activityTimeline.get': {
    method: 'GET',
    path: '/api/memory/activity-timeline',
    query: ['timelineId', 'date', 'status'],
  },
  'memory.activityTimeline.calendar': {
    method: 'GET',
    path: '/api/memory/activity-timeline/calendar',
    query: ['month'],
    requiredQuery: ['month'],
  },
  'memory.activityTimeline.build': {
    method: 'POST',
    path: '/api/memory/activity-timeline/build',
    body: ['date', 'throughToday', 'rangeStartDate'],
    requiredBody: ['date'],
  },
  'memory.activityTimeline.approve': {
    method: 'POST',
    path: '/api/memory/activity-timeline/approve',
    body: ['timelineId', 'expectedSourceEventHash', 'confirmText'],
    requiredBody: ['timelineId', 'expectedSourceEventHash', 'confirmText'],
  },
  'memory.activityTimeline.reject': {
    method: 'POST',
    path: '/api/memory/activity-timeline/reject',
    body: ['timelineId', 'reason', 'confirmText'],
    requiredBody: ['timelineId', 'reason', 'confirmText'],
  },
  'history.page': {
    method: 'GET',
    path: '/api/history/page',
    query: ['limit', 'cursor', 'query', 'filter'],
  },
  'history.detail': {
    method: 'GET',
    path: '/api/history/detail',
    query: ['eventId'],
    requiredQuery: ['eventId'],
  },
  'history.tombstone.preview': {
    method: 'POST',
    path: '/api/history/tombstone/preview',
    body: ['eventId', 'reason', 'expectedRuntimeRevision'],
    requiredBody: ['eventId', 'expectedRuntimeRevision'],
  },
  'history.tombstone.apply': {
    method: 'POST',
    path: '/api/history/tombstone/apply',
    body: ['eventId', 'reason', 'expectedRuntimeRevision', 'previewToken', 'payloadSha256', 'confirmText'],
    requiredBody: ['eventId', 'reason', 'expectedRuntimeRevision', 'previewToken', 'payloadSha256', 'confirmText'],
  },
  'history.tombstone.rollback': {
    method: 'POST',
    path: '/api/history/tombstone/rollback',
    body: ['receiptId', 'rollbackToken', 'payloadSha256', 'confirmText'],
    requiredBody: ['receiptId', 'rollbackToken', 'payloadSha256', 'confirmText'],
  },
  'knowledge.start': {
    method: 'POST',
    path: '/api/knowledge/start',
    body: ['question', 'context', 'mode', 'includeNotion', 'generation', 'contextHash', 'clientId', 'project', 'app', 'maxChars', 'latencyBudgetMs'],
    requiredBody: ['question'],
  },
  'knowledge.cancel': {
    method: 'POST',
    path: '/api/knowledge/cancel',
    body: ['sessionId', 'id'],
  },
  'knowledge.status': {
    method: 'GET',
    path: '/api/knowledge/status',
    query: ['sessionId', 'id'],
  },
  'knowledge.routeStatus': { method: 'GET', path: '/api/knowledge/route-status' },
  'knowledge.database.apply.preview': {
    method: 'POST',
    path: '/api/knowledge/database/apply-preview',
    body: ['runId', 'expectedRuntimeRevision'],
    requiredBody: ['runId'],
  },
  'knowledge.database.draft.edit': {
    method: 'POST',
    path: '/api/knowledge/database/draft-edit',
    body: ['runId', 'diffId', 'selected', 'payload'],
    requiredBody: ['runId', 'diffId', 'selected'],
  },
  'knowledge.database.apply': {
    method: 'POST',
    path: '/api/knowledge/database/apply',
    body: ['runId', 'confirm', 'previewToken', 'payloadSha256', 'expectedRuntimeRevision'],
    requiredBody: ['runId', 'confirm', 'previewToken', 'payloadSha256', 'expectedRuntimeRevision'],
  },
  'knowledge.database.rollback': {
    method: 'POST',
    path: '/api/knowledge/database/rollback',
    body: ['runId', 'confirm', 'receiptId', 'rollbackToken', 'payloadSha256'],
    requiredBody: ['runId', 'confirm', 'receiptId', 'rollbackToken', 'payloadSha256'],
  },
  'knowledgeBases.list': {
    method: 'GET',
    path: '/api/knowledge-bases',
    query: ['limit', 'cursor', 'query', 'status'],
  },
  'knowledgeBases.create': {
    method: 'POST',
    path: '/api/knowledge-bases',
    body: ['name', 'description', 'agentEnabled', 'parserProvider', 'chunkingConfig', 'retrievalConfig'],
    requiredBody: ['name'],
  },
  'knowledgeBases.get': {
    method: 'GET',
    path: '/api/knowledge-bases/:kbId',
    params: { kbId: null },
  },
  'knowledgeBases.update': {
    method: 'PATCH',
    path: '/api/knowledge-bases/:kbId',
    params: { kbId: null },
    body: ['name', 'description', 'agentEnabled', 'parserProvider', 'chunkingConfig', 'retrievalConfig', 'expectedRevision'],
    requiredBody: ['expectedRevision'],
  },
  'knowledgeBases.delete.preview': {
    method: 'POST',
    path: '/api/knowledge-bases/:kbId/delete/preview',
    params: { kbId: null },
    body: ['expectedRevision'],
    requiredBody: ['expectedRevision'],
  },
  'knowledgeBases.delete.apply': {
    method: 'POST',
    path: '/api/knowledge-bases/:kbId/delete/apply',
    params: { kbId: null },
    body: ['expectedRevision', 'previewToken', 'payloadSha256', 'confirmText'],
    requiredBody: ['expectedRevision', 'previewToken', 'payloadSha256', 'confirmText'],
  },
  'knowledgeBases.documents.list': {
    method: 'GET',
    path: '/api/knowledge-bases/:kbId/documents',
    params: { kbId: null },
    query: ['limit', 'cursor', 'query', 'status'],
  },
  'knowledgeBases.document.import': {
    method: 'POST',
    path: '/api/knowledge-bases/:kbId/documents/import',
    params: { kbId: null },
    query: ['fileName', 'mimeType', 'parserProvider'],
    requiredQuery: ['fileName', 'mimeType'],
  },
  'knowledgeBases.document.retry': {
    method: 'POST',
    path: '/api/knowledge-bases/:kbId/documents/:fileId/retry',
    params: { kbId: null, fileId: null },
    body: ['stage', 'parserProvider', 'expectedRevision'],
    requiredBody: ['stage', 'expectedRevision'],
  },
  'knowledgeBases.document.delete': {
    method: 'DELETE',
    path: '/api/knowledge-bases/:kbId/documents/:fileId',
    params: { kbId: null, fileId: null },
  },
  'knowledgeBases.document.get': {
    method: 'GET',
    path: '/api/knowledge-bases/:kbId/documents/:fileId',
    params: { kbId: null, fileId: null },
    query: ['offset', 'limit', 'lineOffset', 'lineLimit'],
  },
  'knowledgeBases.document.source': {
    method: 'GET',
    path: '/api/knowledge-bases/:kbId/documents/:fileId/source',
    params: { kbId: null, fileId: null },
    binary: true,
  },
  'knowledgeBases.asset.get': {
    method: 'GET',
    path: '/api/knowledge-bases/:kbId/documents/:fileId/assets/:assetId',
    params: { kbId: null, fileId: null, assetId: null },
    binary: true,
  },
  'knowledgeBases.jobs.list': {
    method: 'GET',
    path: '/api/knowledge-bases/:kbId/jobs',
    params: { kbId: null },
    query: ['limit', 'cursor', 'status'],
  },
  'knowledgeBases.job.cancel': {
    method: 'POST',
    path: '/api/knowledge-bases/:kbId/jobs/:jobId/cancel',
    params: { kbId: null, jobId: null },
    body: [],
  },
  'knowledgeBases.chunkPreview': {
    method: 'POST',
    path: '/api/knowledge-bases/:kbId/documents/:fileId/chunk-preview',
    params: { kbId: null, fileId: null },
    body: ['chunkingConfig', 'limit'],
    requiredBody: ['chunkingConfig'],
  },
  'knowledgeBases.search': {
    method: 'POST',
    path: '/api/knowledge-bases/:kbId/search',
    params: { kbId: null },
    body: ['query', 'topK', 'mode', 'threshold', 'fileIds', 'fileName'],
    requiredBody: ['query'],
  },
  'knowledgeBases.find': {
    method: 'POST',
    path: '/api/knowledge-bases/:kbId/documents/:fileId/find',
    params: { kbId: null, fileId: null },
    body: ['query', 'regex', 'lineWindow'],
    requiredBody: ['query'],
  },
  'knowledgeBases.open': {
    method: 'GET',
    path: '/api/knowledge-bases/:kbId/documents/:fileId/content',
    params: { kbId: null, fileId: null },
    query: ['chunkId', 'page', 'startLine', 'lines'],
  },
  'knowledgeBases.reindexPreview': {
    method: 'GET',
    path: '/api/knowledge-bases/:kbId/reindex-preview',
    params: { kbId: null },
  },
  'knowledgeBases.rebuild': {
    method: 'POST',
    path: '/api/knowledge-bases/:kbId/rebuild',
    params: { kbId: null },
    body: ['previewToken', 'payloadSha256', 'expectedRevision', 'confirmText'],
    requiredBody: ['previewToken', 'payloadSha256', 'expectedRevision', 'confirmText'],
  },
  'knowledgeBases.graph.get': {
    method: 'GET',
    path: '/api/knowledge-bases/:kbId/graph',
    params: { kbId: null },
    query: ['documentId', 'query', 'kinds', 'limit', 'depth', 'excludeChunks', 'focusId'],
    responseContract: 'knowledge-graph.v1',
  },
  'knowledgeBases.graph.rebuild': {
    method: 'POST',
    path: '/api/knowledge-bases/:kbId/graph/rebuild',
    params: { kbId: null },
    body: ['expectedRevision', 'documentIds', 'extractorMode', 'modelId', 'batchSize', 'extractionConcurrency', 'maxEntitiesPerChunk', 'maxRelationsPerChunk', 'maxTopicsPerChunk'],
    requiredBody: ['expectedRevision'],
  },
  'knowledgeWorker.health': { method: 'GET', path: '/api/knowledge-bases/health' },
  'knowledgeParsers.list': { method: 'GET', path: '/api/knowledge-bases/parsers' },
  'knowledgeEmbedding.profile': { method: 'GET', path: '/api/knowledge-bases/embedding-profile' },
  'knowledgeEmbedding.probe': {
    method: 'POST',
    path: '/api/knowledge-bases/embedding-probe',
    body: ['profile'],
    requiredBody: ['profile'],
  },
  'knowledgeEmbedding.impact': {
    method: 'POST',
    path: '/api/knowledge-bases/embedding-impact',
    body: ['profile'],
    requiredBody: ['profile'],
  },
  'diagnostics.runtime': { method: 'GET', path: '/api/runtime/status' },
  'diagnostics.predictor': { method: 'GET', path: '/api/predictor/status' },
  'diagnostics.models': { method: 'GET', path: '/api/models/status' },
  'diagnostics.action.preview': {
    method: 'POST',
    path: '/api/runtime/action/preview',
    body: ['action', 'expectedRuntimeRevision'],
    requiredBody: ['action', 'expectedRuntimeRevision'],
  },
  'diagnostics.action.start': {
    method: 'POST',
    path: '/api/runtime/action/start',
    body: ['action', 'expectedRuntimeRevision', 'previewToken', 'payloadSha256', 'commandSha256', 'confirmText'],
    requiredBody: ['action', 'expectedRuntimeRevision', 'previewToken', 'payloadSha256', 'commandSha256', 'confirmText'],
  },
  'diagnostics.action.job': {
    method: 'GET',
    path: '/api/runtime/job/:jobId',
    params: { jobId: null },
  },
  'configuration.settings': { method: 'GET', path: '/api/settings' },
  'configuration.schema': { method: 'GET', path: '/api/settings/schema' },
  'configuration.settings.preview': {
    method: 'POST',
    path: '/api/settings/preview',
    body: ['changes', 'expectedRuntimeRevision'],
    requiredBody: ['changes', 'expectedRuntimeRevision'],
  },
  'configuration.settings.apply': {
    method: 'POST',
    path: '/api/settings/apply',
    body: ['changes', 'expectedRuntimeRevision', 'previewToken', 'payloadSha256', 'confirmText'],
    requiredBody: ['changes', 'expectedRuntimeRevision', 'previewToken', 'payloadSha256', 'confirmText'],
  },
  'configuration.settings.rollback': {
    method: 'POST',
    path: '/api/settings/rollback',
    body: ['receiptId', 'rollbackToken', 'payloadSha256', 'confirmText'],
    requiredBody: ['receiptId', 'rollbackToken', 'payloadSha256', 'confirmText'],
  },
  'configuration.import.preview': {
    method: 'POST',
    path: '/api/configuration/import-preview',
    body: ['path'],
    requiredBody: ['path'],
  },
  'configuration.import.apply': {
    method: 'POST',
    path: '/api/configuration/import-apply',
    body: ['path', 'expectedRuntimeRevision', 'previewToken', 'confirmText', 'confirmRemoteModel'],
    requiredBody: ['path', 'expectedRuntimeRevision', 'previewToken', 'confirmText'],
  },
  'configuration.backup.export': {
    method: 'POST',
    path: '/api/configuration/backup-export',
    body: ['destination'],
    requiredBody: ['destination'],
  },
  'configuration.restore.preview': {
    method: 'POST',
    path: '/api/configuration/restore-preview',
    body: ['path'],
    requiredBody: ['path'],
  },
  'configuration.restore.apply': {
    method: 'POST',
    path: '/api/configuration/restore-apply',
    body: ['path', 'restoreToken', 'confirmText', 'expectedRuntimeRevision'],
    requiredBody: ['path', 'restoreToken', 'confirmText', 'expectedRuntimeRevision'],
  },
} as const satisfies Record<string, ControlRouteDefinition>;

export type ControlPathId = keyof typeof CONTROL_ROUTES;
export type SubscriptionPathId = {
  [PathId in ControlPathId]: (typeof CONTROL_ROUTES)[PathId] extends {
    subscription: ControlStreamKind;
  }
    ? PathId
    : never;
}[ControlPathId];

export function controlRoute(pathId: ControlPathId): ControlRouteDefinition {
  if (!Object.hasOwn(CONTROL_ROUTES, pathId)) {
    throw new ControlRoutePolicyError(String(pathId), 'pathId is not allowlisted');
  }
  return CONTROL_ROUTES[pathId];
}

export function isControlPathId(value: unknown): value is ControlPathId {
  return typeof value === 'string' && Object.hasOwn(CONTROL_ROUTES, value);
}

export function resolveControlPath(
  pathId: ControlPathId,
  params: Readonly<Record<string, string>> = {},
): string {
  const route = controlRoute(pathId);
  const policy = route.params ?? {};
  const suppliedKeys = Object.keys(params);
  const requiredKeys = Object.keys(policy);
  if (
    suppliedKeys.length !== requiredKeys.length ||
    suppliedKeys.some((key) => !Object.hasOwn(policy, key))
  ) {
    throw new ControlRoutePolicyError(pathId, 'path parameters do not match the route policy');
  }

  let resolved = route.path;
  for (const key of requiredKeys) {
    const value = params[key];
    if (!isSafeRouteParameter(value, key)) {
      throw new ControlRoutePolicyError(pathId, `invalid ${key} path parameter`);
    }
    const allowedValues = policy[key];
    if (allowedValues && !allowedValues.includes(value)) {
      throw new ControlRoutePolicyError(pathId, `${key} is not allowlisted`);
    }
    resolved = resolved.replace(`:${key}`, encodeURIComponent(value));
  }
  return resolved;
}

export function assertAllowedQuery(
  pathId: ControlPathId,
  query: Readonly<Record<string, unknown>> | undefined,
): void {
  const values = query ?? {};
  const allowed = new Set<string>(controlRoute(pathId).query ?? []);
  for (const key of Object.keys(values)) {
    if (!allowed.has(key)) {
      throw new ControlRoutePolicyError(pathId, `query field is not allowlisted: ${key}`);
    }
    const value = values[key];
    if (
      (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean') ||
      (typeof value === 'number' && !Number.isFinite(value)) ||
      (typeof value === 'string' &&
        (value.length > 2_048 || /[\u0000-\u001f]/u.test(value)))
    ) {
      throw new ControlRoutePolicyError(pathId, `query field is invalid: ${key}`);
    }
  }
  for (const key of controlRoute(pathId).requiredQuery ?? []) {
    if (!Object.hasOwn(values, key)) {
      throw new ControlRoutePolicyError(pathId, `required query field is missing: ${key}`);
    }
  }
}

export function assertAllowedBody(
  pathId: ControlPathId,
  body: unknown,
): void {
  const route = controlRoute(pathId);
  if (body === undefined) {
    if ((route.requiredBody?.length ?? 0) > 0) {
      throw new ControlRoutePolicyError(pathId, 'required request body is missing');
    }
    return;
  }
  if (typeof body !== 'object' || body === null || Array.isArray(body)) {
    throw new ControlRoutePolicyError(pathId, 'request body must be an object');
  }
  const allowed = new Set<string>(route.body ?? []);
  for (const key of Object.keys(body)) {
    if (!allowed.has(key)) {
      throw new ControlRoutePolicyError(pathId, `body field is not allowlisted: ${key}`);
    }
  }
  for (const key of route.requiredBody ?? []) {
    if (!Object.hasOwn(body, key)) {
      throw new ControlRoutePolicyError(pathId, `required body field is missing: ${key}`);
    }
  }
}

export class ControlRoutePolicyError extends Error {
  readonly pathId: string;

  constructor(pathId: string, message: string) {
    super(`Rejected ${pathId}: ${message}`);
    this.name = 'ControlRoutePolicyError';
    this.pathId = pathId;
  }
}

function isSafeRouteParameter(value: unknown, key: string): value is string {
  const maximumTailLength = key === 'traceId' ? 159 : 127;
  return typeof value === 'string'
    && new RegExp(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,${maximumTailLength}}$`, 'u').test(value);
}
