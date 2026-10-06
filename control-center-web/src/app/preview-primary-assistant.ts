import type { ControlPathId } from '@/platform/routes';
import type { ControlRequest } from '@/platform/transport';
import type { MockRouteHandler } from '@/test/mock-transport';
import { ControlTransportHttpError } from '@/platform/http-transport';
import type { AgentWorkflowStateV1 } from '@/contracts/generated/agent-workflow-state.v1';
import type { PersonalProfile } from '@/features/memory/MemoryProfile';

/** Explicit demo data only; the live transport never falls back to this. */
export function installPrimaryAssistantPreview(routes: Partial<Record<ControlPathId, MockRouteHandler>>, sessions: Record<string, unknown>[], emit: (event: unknown) => void) {
  const primaryByRoots = new Map<string, Record<string, unknown>>();
  const tasksByRequest = new Map<string, { signature: string; session: Record<string, unknown> }>();
  const histories = new Map<string, { messages: unknown[]; events: Record<string, unknown>[]; active?: string; timer?: ReturnType<typeof setTimeout>; requests: Set<string> }>();
  const originalSnapshot = routes['agent.session.snapshot'];
  const originalPrompt = routes['agent.session.prompt'];
  const originalAbort = routes['agent.session.abort'];
  const originalWorkflow = routes['agent.session.workflow.get'];
  const originalGoalMutation = routes['agent.session.goal.mutate'];
  // Desktop windows can survive a renderer reload; in-memory demo histories
  // cannot. Never recycle their identities into another preview instance.
  const instanceId = crypto.randomUUID();
  let nextId = 1;
  function makeSession(title: string, roots: string[], metadata: Record<string, unknown>, execute: boolean) {
    const id = `session-primary-preview-${instanceId}-${nextId++}`;
    const session: Record<string, unknown> = { schemaVersion: 'rag-ime.agent-session.v1', id, title,
      mode: execute || roots.length ? 'coordinator' : 'assistant', status: 'idle', runtimeEngine: 'classic',
      roleId: 'companion-present-v1', roleVersion: '1', roleBookRevisionId: '', surfaceKind: 'agent',
      executionMode: execute ? 'workspace_managed' : 'read_only', workspaceScopeGranted: execute,
      toolProfileVersion: execute ? 'control-center-v1' : 'subagent-readonly-v1', toolAllowlistMode: 'profile',
      workspaceRoots: roots, metadata, messageCount: 0, createdAtMs: Date.now(), updatedAtMs: Date.now(),
      ...(execute ? { goal: { goalId: `goal:${id}`, revision: 1, status: 'active', objective: title, successCriteria: '' } } : {}) };
    sessions.unshift(session);
    histories.set(id, { messages: [], events: [], requests: new Set() });
    return session;
  }
  function event(sessionId: string, turnId: string, eventType: string, payload: Record<string, unknown>) {
    const history = histories.get(sessionId)!;
    const sequence = history.events.length + 1;
    const value = { schemaVersion: 'rag-ime.agent-event.v1', eventId: `${sessionId}:${sequence}`, sessionId, turnId,
      eventType, payload, sequence, createdAtMs: Date.now(), resumeToken: `${sessionId}:${sequence}`, streamKind: 'agent' };
    history.events.push(value); emit(value);
  }
  function message(sessionId: string, turnId: string, role: string, text: string, clientMessageId: string) {
    const value = { schemaVersion: 'rag-ime.agent-message.v1', id: `${turnId}:${role}`, sessionId, turnId, role,
      status: 'completed', blocks: [{ id: `${turnId}:${role}:text`, type: 'text', status: 'completed', presentationKind: 'markdown', data: { text } }],
      attachments: [], citations: [], clientMessageId, createdAtMs: Date.now(), completedAtMs: Date.now() };
    histories.get(sessionId)!.messages.push(value); event(sessionId, turnId, 'message_completed', { message: value });
  }
  routes['agent.primary.ensure'] = (request: ControlRequest) => {
    const roots = strings(record(request.body).workspaceRoots); const key = JSON.stringify([...roots].sort());
    const cached = primaryByRoots.get(key);
    // Archive replaces a directory entry; cached objects are not authority.
    let session = cached && sessions.find(item => item.id === cached.id && item.status !== 'archived'
      && record(item.metadata).primaryAssistant === true
      && JSON.stringify([...strings(item.workspaceRoots)].sort()) === key);
    const created = !session;
    if (!session) { session = makeSession('我的助手', roots, { assistantId: 'primary:preview', primaryAssistant: true }, false); primaryByRoots.set(key, session); }
    const sourceSessionId = session.id;
    const tasks = sessions.filter(item => {
      const metadata = record(item.metadata);
      return item.status !== 'archived' && metadata.primaryTask === true
        && metadata.assistantId === 'primary:preview' && metadata.sourceSessionId === sourceSessionId;
    });
    return { ok: true, assistantId: 'primary:preview', created, session, tasks };
  };
  routes['agent.primary.tasks.create'] = (request: ControlRequest) => {
    const body = record(request.body); const id = String(body.clientRequestId); const signature = JSON.stringify(body);
    const existing = tasksByRequest.get(id);
    if (existing && existing.signature !== signature) throw new Error('primary_task_request_conflict');
    // Like the live contract, an exact admitted request can be replayed after
    // its discussion retires. Fresh requests must validate the current source.
    const source = sessions.find(item => item.id === body.sourceSessionId && record(item.metadata).primaryAssistant === true);
    if (!existing) {
      if (!source) throw new Error('primary_source_session_required');
      if (source.status === 'archived') throw new Error('这段讨论已归档，请返回我的助手再交办。');
      const sourceRoots = strings(source.workspaceRoots);
      if (sourceRoots.length && JSON.stringify(sourceRoots) !== JSON.stringify(strings(body.workspaceRoots))) {
        throw new Error('本次工作目录需要与讨论项目一致。');
      }
    }
    const roots = strings(body.workspaceRoots);
    if (body.workspaceScopeConfirmation !== 'APPROVE_WORKSPACE_SCOPE' || !roots.length) throw new Error('workspace_scope_confirmation_required');
    const replaySession = existing && sessions.find(item => item.id === existing.session.id);
    if (existing && !replaySession) throw new Error('这项演示任务已删除，无法恢复原请求。');
    const session = replaySession ?? makeSession(String(body.objective).slice(0, 80), roots,
      { assistantId: 'primary:preview', primaryTask: true, sourceSessionId: body.sourceSessionId, clientRequestId: id }, true);
    if (!existing) {
      session.goal = { ...record(session.goal), objective: String(body.objective), successCriteria: strings(body.acceptanceCriteria).join('\n') };
      tasksByRequest.set(id, { signature, session });
    }
    return { ok: true, assistantId: 'primary:preview', created: !existing, session,
      authorization: { ...body, workspaceScopeSha256: 'preview-scope', workspaceScopeGrantedAtMs: Date.now() } };
  };
  routes['agent.session.snapshot'] = (request: ControlRequest) => {
    const id = String(request.params?.sessionId); const history = histories.get(id);
    if (!history && id.startsWith('session-primary-preview-')) return { sessionId: id, runtimeEngine: 'classic', messages: [], liveEvents: [], lastSequence: 0, status: 'idle', runtimeQuiescent: true };
    if (!history) return call(originalSnapshot, request);
    const session = sessions.find(item => item.id === id);
    const goal = record(session?.goal);
    return { sessionId: id, runtimeEngine: 'classic', messages: [...history.messages], liveEvents: [...history.events],
      lastSequence: history.events.length, resumeToken: `${id}:${history.events.length}`, status: history.active ? 'busy' : 'idle',
      ...(goal.goalId ? { goal: { ...goal, schemaVersion: 'rag-ime.agent-goal.v1', sessionId: id, configured: true,
        evidenceExpectations: [], budget: { tokenLimit: null, timeLimitMs: null }, usage: { tokens: 0, elapsedMs: 0 },
        remaining: { tokens: null, timeMs: null }, budgetExceeded: false, completionAudit: null, cancellationAudit: null,
        updatedAtMs: session?.updatedAtMs } } : {}), runtimeQuiescent: !history.active };
  };
  // Fresh primary demo tasks own their goal. Never transplant the shared
  // sample Session's Todo, budget or goal into an unrelated task identity.
  routes['agent.session.workflow.get'] = (request: ControlRequest) => {
    const id = String(request.params?.sessionId);
    const session = sessions.find(item => item.id === id);
    if (!histories.has(id) || !session) return call(originalWorkflow, request);
    const source = record(session.goal);
    const goal: AgentWorkflowStateV1['goal'] = {
      schemaVersion: 'rag-ime.agent-goal.v1', sessionId: id, configured: Boolean(source.goalId),
      goalId: String(source.goalId ?? ''), revision: Number(source.revision ?? 0),
      objective: String(source.objective ?? ''), successCriteria: String(source.successCriteria ?? ''),
      evidenceExpectations: [], status: (source.status ?? 'cleared') as AgentWorkflowStateV1['goal']['status'],
      budget: { tokenLimit: null, timeLimitMs: null }, usage: { tokens: 0, elapsedMs: 0 },
      remaining: { tokens: null, timeMs: null }, budgetExceeded: false, completionAudit: null,
      cancellationAudit: null, updatedAtMs: Number(session.updatedAtMs),
    };
    return { schemaVersion: 'rag-ime.agent-workflow-state.v1', ok: true, sessionId: id, goal,
      todo: { schemaVersion: 'rag-ime.agent-todo.v1', id: `todo:${id}`, sessionId: id,
        revision: 0, actor: 'agent', updatedAtMs: Number(session.updatedAtMs), roomLineage: null,
        phases: [], counts: { total: 0, pending: 0, inProgress: 0, blocked: 0, completed: 0, abandoned: 0 } },
      actGate: { allowed: goal.status !== 'completed', reason: goal.status === 'completed' ? 'goal_completed' : session.workspaceScopeGranted ? 'user_execution_request' : 'approved',
        message: goal.status === 'completed' ? '演示目标已结束，没有真实模型的验收依据。' : session.workspaceScopeGranted ? '演示任务已获得目录授权。' : '演示讨论保持只读。', todoRevision: 0, goalRevision: goal.revision },
    } satisfies AgentWorkflowStateV1;
  };
  routes['agent.session.goal.mutate'] = (request: ControlRequest) => {
    if (!histories.has(String(request.params?.sessionId))) return call(originalGoalMutation, request);
    throw new ControlTransportHttpError('agent.session.goal.mutate', 422, '演示任务不支持修改目标；请在真实服务中使用此操作。');
  };
  routes['agent.session.prompt'] = (request: ControlRequest) => {
    const id = String(request.params?.sessionId); const history = histories.get(id);
    if (!history && id.startsWith('session-primary-preview-')) throw new Error('演示会话已重置，请从我的助手重新开始。');
    if (!history) return call(originalPrompt, request);
    const body = record(request.body); const clientId = String(body.clientMessageId);
    if (history.requests.has(clientId)) return { ok: true, accepted: true, deduplicated: true };
    history.requests.add(clientId);
    const turnId = `${id}:turn:${clientId}`; history.active = turnId;
    const session = sessions.find(item => item.id === id)!; const task = record(session.metadata).primaryTask === true;
    session.status = 'busy'; session.updatedAtMs = Date.now();
    message(id, turnId, 'user', String(body.message), clientId);
    event(id, turnId, 'reasoning_summary', { summary: task ? '演示任务：正在核对目标和完成标准' : '正在整理你的问题', state: 'running' });
    if (task) event(id, turnId, 'tool_started', { toolCallId: `${turnId}:tool`, toolId: 'read', summary: '演示：读取工作目录中的材料', args: { path: strings(session.workspaceRoots)[0] } });
    history.timer = setTimeout(() => {
      if (history.active !== turnId) return;
      if (task) event(id, turnId, 'tool_finished', { toolCallId: `${turnId}:tool`, toolId: 'read', summary: '演示材料已读取', status: 'completed' });
      const text = task ? '这是演示任务的结果：工作过程和结果保留在当前对话中。实际运行时会显示模型与工具的真实回执。' : '我们可以先把目标和顾虑说清楚。需要我动手时，点「交给助手做」，确认本次工作范围即可。';
      message(id, turnId, 'assistant', text, clientId);
      history.active = undefined; session.status = 'idle'; session.lastMessagePreview = text; session.lastTerminalTurnId = turnId;
      session.messageCount = history.messages.length;
      if (task) session.goal = { ...record(session.goal), status: 'completed', revision: Number(record(session.goal).revision) + 1 };
      event(id, turnId, 'turn_completed', { summary: '演示完成', status: 'completed' });
    }, task ? 4000 : 150);
    return { ok: true, accepted: true, sessionId: id, clientMessageId: clientId };
  };
  routes['agent.session.abort'] = (request: ControlRequest) => {
    const id = String(request.params?.sessionId); const history = histories.get(id);
    if (!history) return call(originalAbort, request);
    const turnId = history.active;
    if (turnId) {
      clearTimeout(history.timer); history.active = undefined;
      const session = sessions.find(item => item.id === id)!; session.status = 'idle';
      event(id, turnId, 'turn_completed', { summary: '已停止', status: 'aborted', aborted: true });
    }
    return { ok: true, sessionId: id, backgroundJobs: { drained: true, pendingJobIds: [] } };
  };

  let profile: PersonalProfile = { schemaVersion: 'paw.personal-profile.v1', revision: 'profile-demo-1', truncated: false,
    text: '我在做一个本地优先的个人工作台。\n\n我希望先把事情说清楚，再确认执行范围。', paragraphs: [
      { id: 'profile-demo-project', memoryIds: ['profile-demo-project'], revision: 'card-demo-1', text: '我在做一个本地优先的个人工作台。', sourceCount: 1, sourceRefs: [{ kind: 'evidence', id: 'evidence:preview-1' }] },
      { id: 'profile-demo-preference', memoryIds: ['profile-demo-preference'], revision: 'card-demo-2', text: '我希望先把事情说清楚，再确认执行范围。', sourceCount: 1, sourceRefs: [{ kind: 'evidence', id: 'evidence:preview-2' }] },
    ] };
  let profileRevision = 1;
  const profileWrites = new Map<string, { signature: string; profile: PersonalProfile }>();
  routes['memory.profile'] = () => structuredClone(profile);
  routes['memory.profile.save'] = (request: ControlRequest) => {
    const body = record(request.body); const id = String(body.clientRequestId); const signature = JSON.stringify(body);
    const previous = profileWrites.get(id);
    if (previous?.signature === signature) return { ok: true, profile: previous.profile, replayed: true };
    if (previous || body.expectedRevision !== profile.revision) throw new ControlTransportHttpError('memory.profile.save', 409,
      'memory_profile_revision_conflict', { code: 'memory_profile_revision_conflict', current: structuredClone(profile) });
    profileRevision += 1;
    const paragraphs = (Array.isArray(body.paragraphs) ? body.paragraphs : []).map(record);
    const next = profile.paragraphs.filter(item => !paragraphs.some(change => change.id === item.id));
    for (const change of paragraphs) {
      if (!String(change.text).trim()) continue;
      const old = profile.paragraphs.find(item => item.id === change.id); const cardId = old?.id ?? `profile-demo-${nextId++}`;
      next.push({ id: cardId, memoryIds: [cardId], text: String(change.text), revision: `card-demo-${profileRevision}`,
        sourceCount: old?.sourceCount ?? 1, sourceRefs: old?.sourceRefs ?? [{ kind: 'evidence', id: `evidence:${cardId}` }] });
    }
    profile = { ...profile, revision: `profile-demo-${profileRevision}`, paragraphs: next, text: next.map(item => item.text).join('\n\n') };
    profileWrites.set(id, { signature, profile: structuredClone(profile) });
    return { ok: true, profile: structuredClone(profile), changes: [] };
  };
}
function record(value: unknown): Record<string, unknown> { return value && typeof value === 'object' ? value as Record<string, unknown> : {}; }
function strings(value: unknown): string[] { return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []; }
function call(handler: MockRouteHandler, request: ControlRequest): unknown { return typeof handler === 'function' ? handler(request) : handler; }
