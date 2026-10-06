import {
  Circle,
  CircleCheck,
  CirclePause,
  CirclePlay,
  ChevronRight,
  Clock3,
  FileText,
  Flag,
  ListChecks,
  LoaderCircle,
  ShieldCheck,
} from 'lucide-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button, Disclosure, TextArea } from '@/components/primitives';
import type { AgentTodoProjection } from '@/contracts/agent-reducer';
import type {
  ActGate,
  AgentWorkflowStateV1,
  Goal,
  Todo,
  TodoTask,
} from '@/contracts/generated/agent-workflow-state.v1';
import type { ControlTransport, JsonValue } from '@/platform/transport';
import { agentProjectionKey, agentSessionAddress, type AgentSessionTarget } from '../state/live-store';

type MutationInput = {
  sessionId: string;
  address: AgentSessionTarget;
  transport: ControlTransport;
  body: { [key: string]: JsonValue };
};

export function AgentWorkflowPanel({
  active = true,
  sessionId,
  fallbackTodo,
  fallbackGoal,
  fallbackActGate,
  compactEmpty = false,
  onWorkflowResolved,
}: {
  active?: boolean;
  sessionId: string;
  fallbackTodo?: AgentTodoProjection;
  fallbackGoal?: Goal;
  fallbackActGate?: ActGate;
  compactEmpty?: boolean;
  onWorkflowResolved?: (workflow: AgentWorkflowStateV1) => void;
}) {
  const transport = useControlTransport();
  const address = agentSessionAddress(transport, sessionId);
  const queryClient = useQueryClient();
  const workflowQuery = useQuery({
    queryKey: ['agent', 'workflow', agentProjectionKey(address)],
    queryFn: async ({ signal }) => {
      const next = await transport.request<AgentWorkflowStateV1>({
        pathId: 'agent.session.workflow.get',
        params: { sessionId },
        signal,
      });
      return assertWorkflowOwner(next, sessionId);
    },
    enabled: active && Boolean(sessionId),
    retry: false,
    staleTime: 500,
    refetchInterval: active ? 2_000 : false,
    refetchOnWindowFocus: active,
  });
  const hasLiveTodo = Boolean(fallbackTodo);
  const hasLiveGoal = Boolean(fallbackGoal);
  const hasLiveActGate = Boolean(fallbackActGate);
  const hasLiveWorkflow = hasLiveTodo || hasLiveGoal || hasLiveActGate;
  const workflow = useMemo(() => {
    const liveWorkflow = fallbackWorkflow(
      sessionId,
      fallbackTodo,
      fallbackGoal,
      fallbackActGate,
    );
    return mergeWorkflowState(
      workflowQuery.data,
      liveWorkflow,
      hasLiveTodo,
      hasLiveGoal,
      hasLiveActGate,
    );
  }, [
    fallbackActGate,
    fallbackGoal,
    fallbackTodo,
    hasLiveActGate,
    hasLiveGoal,
    hasLiveTodo,
    sessionId,
    workflowQuery.data,
  ]);
  useEffect(() => {
    onWorkflowResolved?.(workflow);
  }, [address, onWorkflowResolved, workflow]);
  const mutation = useMutation({
    mutationKey: ['agent', 'workflow', agentProjectionKey(address)],
    mutationFn: async ({ sessionId: ownerSessionId, transport: ownerTransport, body }: MutationInput) => {
      const next = await ownerTransport.request<AgentWorkflowStateV1>({
        pathId: 'agent.session.goal.mutate',
        params: { sessionId: ownerSessionId },
        body,
      });
      return assertWorkflowOwner(next, ownerSessionId);
    },
    onSuccess: (next, variables) => {
      queryClient.setQueryData(['agent', 'workflow', agentProjectionKey(variables.address)], next);
    },
  });

  if (workflowQuery.isPending && !workflowQuery.data && !hasLiveWorkflow) {
    return (
      <div className="agent-workflow-panel" aria-label="Todo 与长期目标">
        <section className="agent-workflow-section">
          <p role="status">正在读取 Todo 与长期目标</p>
        </section>
      </div>
    );
  }

  if (
    workflowQuery.error
    && !workflowQuery.data
    && !hasLiveWorkflow
    && !isAbsentWorkflow(workflowQuery.error)
  ) {
    return (
      <div className="agent-workflow-panel" aria-label="Todo 与长期目标">
        <section className="agent-workflow-section">
          <div className="agent-workflow-recovery" role="alert">
            <span>暂时无法读取最新 Todo 与长期目标。为避免误用旧状态，相关操作已暂停。</span>
            <Button
              size="small"
              loading={workflowQuery.isFetching}
              onClick={() => void workflowQuery.refetch()}
            >
              重新读取
            </Button>
          </div>
        </section>
      </div>
    );
  }

  if (
    workflowQuery.error
    && !workflowQuery.data
    && !hasLiveWorkflow
    && isAbsentWorkflow(workflowQuery.error)
  ) {
    return (
      <div className="agent-workflow-panel" aria-label="Todo 与长期目标">
        <section className="agent-workflow-section">
          <div className="agent-workflow-recovery" data-state="room-managed" role="status">
            <span>当前没有 Todo。如果这段工作来自协作空间，分工、公开进度和最终回复会继续在那里显示。</span>
          </div>
        </section>
      </div>
    );
  }

  if (compactEmpty && workflow.todo.counts.total === 0 && !workflow.goal.configured) {
    return (
      <p className="agent-workflow-panel agent-workflow-panel--compact-empty" role="status">
        当前没有 Todo 或长期目标；需要时可直接在对话中说明目标。
      </p>
    );
  }

  const goalMode = (
    <GoalMode
      key={`goal:${agentProjectionKey(address)}`}
      goal={workflow.goal}
      pending={mutation.isPending && mutation.variables?.address === address}
      error={mutation.variables?.address === address ? mutation.error : null}
      mutate={(body) => mutation.mutateAsync({ sessionId, address, transport, body })}
    />
  );

  return (
    <div className="agent-workflow-panel" aria-label="Todo 与长期目标">
      {workflowQuery.error && !isAbsentWorkflow(workflowQuery.error) ? (
        <div className="agent-workflow-recovery" role="alert">
          <span>
            {workflowQuery.data
              ? '状态刷新失败，当前显示上一次确认的结果。'
              : '实时读取失败，当前继续显示这段对话已经确认的 Todo 与目标。'}
          </span>
          <Button
            size="small"
            loading={workflowQuery.isFetching}
            onClick={() => void workflowQuery.refetch()}
          >
            重新读取
          </Button>
        </div>
      ) : null}
      {workflow.goal.configured ? goalMode : null}
      <TodoProgress todo={workflow.todo} />
      <ExecutionGate gate={workflow.actGate} />
      {!workflow.goal.configured ? goalMode : null}
    </div>
  );
}

function TodoProgress({ todo }: { todo: Todo }) {
  const settled = todo.counts.completed + todo.counts.abandoned;
  return (
    <section className="agent-workflow-section agent-workflow-todo" aria-label="Todo">
      <header>
        <span className="agent-workflow-section__icon"><ListChecks size={16} /></span>
        <span>
          <strong>Todo</strong>
          <small>{todo.counts.total ? `${settled}/${todo.counts.total} 已收束` : '暂无任务'}</small>
        </span>
      </header>
      {todo.phases.length ? (
        <div className="agent-todo-phases">
          {todo.phases.map((phase) => {
            const phaseSettled = phase.tasks.filter(
              (task) => task.status === 'completed' || task.status === 'abandoned',
            ).length;
            return (
              <section className="agent-todo-phase" key={phase.name} aria-label={phase.name}>
                <header>
                  <strong>{phase.name}</strong>
                  <small>{phaseSettled}/{phase.tasks.length}</small>
                </header>
                <ul>
                  {phase.tasks.map((task) => (
                    <li key={task.content} data-state={task.status}>
                      {todoTaskIcon(task.status)}
                      <span>
                        {task.content}
                        {task.reason ? <small>{task.reason}</small> : null}
                      </span>
                      <small>{todoTaskStatusLabel(task.status)}</small>
                    </li>
                  ))}
                </ul>
              </section>
            );
          })}
        </div>
      ) : (
        <div className="agent-todo-empty">
          <strong>当前没有 Todo</strong>
          <p>需要多步执行时，伙伴会创建 Todo；状态变化会在这里同步刷新。</p>
        </div>
      )}
      {todo.updatedAtMs > 0 ? (
        <small className="agent-todo-updated">
          {todo.actor ? `${todoActorLabel(todo.actor)} · ` : ''}{formatWorkflowTime(todo.updatedAtMs)}
        </small>
      ) : null}
    </section>
  );
}

function ExecutionGate({ gate }: { gate: ActGate }) {
  const settled = ['goal_completed', 'goal_cancelled'].includes(gate.reason);
  return (
    <div className="agent-act-gate" data-open={gate.allowed || undefined} data-settled={settled || undefined}>
      {gate.allowed ? <ShieldCheck size={15} /> : <CirclePause size={15} />}
      <span>
        <strong>{gate.allowed ? '当前请求可以继续' : gate.reason === 'goal_completed' ? '目标已结束' : gate.reason === 'goal_cancelled' ? '目标已取消' : '执行条件未满足'}</strong>
        <small>{gate.message}</small>
      </span>
    </div>
  );
}

function todoTaskIcon(status: TodoTask['status']) {
  if (status === 'completed') return <CircleCheck size={15} aria-hidden="true" />;
  if (status === 'in_progress') return <LoaderCircle size={15} aria-hidden="true" />;
  if (status === 'blocked' || status === 'abandoned') return <CirclePause size={15} aria-hidden="true" />;
  return <Circle size={15} aria-hidden="true" />;
}

function todoTaskStatusLabel(status: TodoTask['status']): string {
  return {
    pending: '待处理',
    in_progress: '进行中',
    blocked: '已阻塞',
    completed: '已完成',
    abandoned: '已放弃',
  }[status];
}

function todoActorLabel(actor: string): string {
  const normalized = actor.trim().toLowerCase();
  if (!normalized) return '';
  if (normalized === 'agent') return '伙伴更新';
  if (normalized.includes('control-center')) return '你在这里更新';
  if (normalized === 'migration') return '已迁移';
  return '系统更新';
}

function GoalMode({
  goal,
  pending,
  error,
  mutate,
}: {
  goal: Goal;
  pending: boolean;
  error: unknown;
  mutate: (body: { [key: string]: JsonValue }) => Promise<unknown>;
}) {
  const [cancelling, setCancelling] = useState(false);
  const [confirmingClear, setConfirmingClear] = useState(false);
  const [cancelReason, setCancelReason] = useState('');
  useEffect(() => {
    setCancelling(false);
    setConfirmingClear(false);
    setCancelReason('');
  }, [goal.revision]);

  const configured = goal.configured;
  const budgetRows = goalBudgetRows(goal);
  const persistedExpectations = goal.evidenceExpectations ?? [];
  const runMutation = (
    body: { [key: string]: JsonValue },
    onSuccess?: () => void,
  ) => {
    void mutate(body).then(onSuccess).catch(() => undefined);
  };

  return (
    <section className="agent-workflow-section agent-goal-mode" aria-label="长期目标">
      <header>
        <span className="agent-workflow-section__icon"><Flag size={16} /></span>
        <span><strong>长期目标</strong><small>{configured ? goalStatusLabel(goal.status) : '未设置'}</small></span>
      </header>

      {!configured ? (
        <div className="agent-goal-empty">
          <strong>直接在对话中描述你想完成的事</strong>
          <p>伙伴会逐项询问目标、交付物、验收方式与禁区，并提供可选择的答案；你只需选择或回复。</p>
          <small>澄清完成后，确认结果会显示在这里，不需要填写表单。</small>
        </div>
      ) : (
        <div className="agent-goal-summary" data-state={goal.status}>
          <p>{goal.objective}</p>
          {goal.completionAudit ? (
            <div className="agent-goal-audit">
              <strong><FileText size={14} />完成依据</strong>
              <p>{goal.completionAudit.summary}</p>
              <Disclosure className="agent-goal-evidence" summary={<><ChevronRight size={14} aria-hidden="true" /><span>查看 {goal.completionAudit.evidence.length} 项依据</span></>}>
                <ul>{goal.completionAudit.evidence.map((item) => <li key={`${item.kind}:${item.reference}`}><span>{item.summary}</span><code>{item.reference}</code></li>)}</ul>
              </Disclosure>
            </div>
          ) : goal.status === 'completed' ? (
            <div className="agent-goal-audit agent-goal-audit--missing">
              <strong><Clock3 size={14} />尚无完成依据</strong>
              <p>目标状态已标记为完成，但没有可核对的完成依据。请结合对话中的结果复核。</p>
            </div>
          ) : null}
          {goal.cancellationAudit ? (
            <div className="agent-goal-audit agent-goal-audit--cancelled">
              <strong><CirclePause size={14} />取消记录</strong>
              <p>{goal.cancellationAudit.reason}</p>
              <small>{goal.cancellationAudit.cancelledBy} · {formatWorkflowTime(goal.cancellationAudit.createdAtMs)}</small>
            </div>
          ) : null}
          {cancelling ? (
            <div className="agent-goal-cancel-editor">
              <label>
                <span>取消原因</span>
                <TextArea aria-label="目标取消原因" autoFocus rows={3} maxLength={1_000} value={cancelReason} onChange={(event) => setCancelReason(event.target.value)} />
              </label>
              <small>取消会保留目标和审计记录；“删除目标”才会清除这些信息。</small>
              <div className="agent-workflow-actions">
                <Button size="small" variant="quiet" disabled={pending} onClick={() => {
                  setCancelling(false);
                  setCancelReason('');
                }}>继续保留目标</Button>
                <Button size="small" variant="danger" loading={pending} disabled={!cancelReason.trim()} onClick={() => runMutation(
                  { action: 'cancel', expectedRevision: goal.revision, reason: cancelReason.trim() },
                  () => setCancelling(false),
                )}>确认取消目标</Button>
              </div>
            </div>
          ) : (
            <div className="agent-workflow-actions">
              {goal.status === 'active' ? <Button size="small" variant="quiet" leadingIcon={<CirclePause size={15} />} loading={pending} onClick={() => runMutation({ action: 'pause', expectedRevision: goal.revision })}>暂停</Button> : null}
              {goal.status === 'paused' ? <Button size="small" leadingIcon={<CirclePlay size={15} />} loading={pending} onClick={() => runMutation({ action: 'resume', expectedRevision: goal.revision })}>恢复</Button> : null}
              {['active', 'paused'].includes(goal.status) ? <Button size="small" variant="quiet" disabled={pending} onClick={() => setCancelling(true)}>取消目标</Button> : null}
              {confirmingClear ? (
                <>
                  <span className="agent-workflow-confirm">永久删除目标、预算和审计记录？</span>
                  <Button size="small" variant="quiet" disabled={pending} onClick={() => setConfirmingClear(false)}>保留</Button>
                  <Button
                    size="small"
                    variant="danger"
                    loading={pending}
                    onClick={() => runMutation(
                      { action: 'clear', expectedRevision: goal.revision },
                      () => setConfirmingClear(false),
                    )}
                  >
                    确认删除
                  </Button>
                </>
              ) : (
                <Button size="small" variant="quiet" disabled={pending} onClick={() => setConfirmingClear(true)}>删除目标</Button>
              )}
            </div>
          )}
          <Disclosure className="agent-goal-details" defaultOpen={goal.budgetExceeded} summary={<><ChevronRight size={14} aria-hidden="true" /><span>完成标准与预算</span></>}>
            {goal.successCriteria ? <div className="agent-goal-criteria"><strong>完成标准</strong><span>{goal.successCriteria}</span></div> : <small className="agent-goal-unbounded">未设置完成标准</small>}
            {persistedExpectations.length ? (
              <div className="agent-goal-criteria">
                <strong>证据预期</strong>
                <ul>{persistedExpectations.map((item, index) => <li key={`${index}:${item}`}>{item}</li>)}</ul>
              </div>
            ) : null}
            {budgetRows.length ? <div className="agent-goal-budget">{budgetRows.map((row) => (
              <div key={row.label}>
                <span><small>{row.label}</small><strong>{row.value}</strong></span>
                <i><b style={{ width: `${row.percent}%` }} /></i>
              </div>
            ))}</div> : <small className="agent-goal-unbounded">未设置预算上限</small>}
          </Disclosure>
        </div>
      )}
      {error ? <p className="agent-workflow-error" role="alert">{publicError(error)}</p> : null}
    </section>
  );
}

function fallbackWorkflow(
  sessionId: string,
  todo?: AgentTodoProjection,
  goal?: Goal,
  actGate?: ActGate,
): AgentWorkflowStateV1 {
  const projectedTodo: Todo = todo ?? emptyTodo(sessionId);
  const projectedGoal = goal ?? emptyGoal(sessionId);
  return {
    schemaVersion: 'rag-ime.agent-workflow-state.v1',
    ok: true,
    sessionId,
    todo: {
      ...projectedTodo,
      id: projectedTodo.id || `todo:${sessionId}`,
      sessionId: projectedTodo.sessionId || sessionId,
    },
    goal: projectedGoal,
    actGate: actGate && gateMatches(actGate, projectedTodo, projectedGoal)
      ? actGate
      : derivedActGate(projectedTodo, projectedGoal),
  };
}

function mergeWorkflowState(
  queried: AgentWorkflowStateV1 | undefined,
  live: AgentWorkflowStateV1,
  hasLiveTodo: boolean,
  hasLiveGoal: boolean,
  hasLiveActGate: boolean,
): AgentWorkflowStateV1 {
  if (!queried) return live;
  const useLiveTodo = hasLiveTodo && (
    live.todo.revision > queried.todo.revision
    || (
      live.todo.revision === queried.todo.revision
      && live.todo.updatedAtMs > queried.todo.updatedAtMs
    )
  );
  const todo = useLiveTodo ? live.todo : queried.todo;
  const goal = latestWorkflowGoal(queried.goal, hasLiveGoal ? live.goal : undefined)!;
  const liveGateMatches = hasLiveActGate && gateMatches(live.actGate, todo, goal);
  const queriedGateMatches = gateMatches(queried.actGate, todo, goal);
  return {
    ...queried,
    todo,
    goal,
    actGate: liveGateMatches
      ? live.actGate
      : queriedGateMatches
        ? queried.actGate
        : derivedActGate(todo, goal),
  };
}

/** Read the latest owned Goal receipt without inferring Runtime pause state. */
export function latestWorkflowGoal(queried: Goal | undefined, live: Goal | undefined): Goal | undefined {
  if (!queried) return live;
  if (!live) return queried;
  return live.revision > queried.revision
    || (live.revision === queried.revision && live.updatedAtMs > queried.updatedAtMs)
    ? live : queried;
}

function emptyTodo(sessionId: string): Todo {
  return {
    schemaVersion: 'rag-ime.agent-todo.v1',
    id: `todo:${sessionId}`,
    sessionId,
    revision: 0,
    actor: '',
    updatedAtMs: 0,
    roomLineage: null,
    phases: [],
    counts: { total: 0, pending: 0, inProgress: 0, blocked: 0, completed: 0, abandoned: 0 },
  };
}

function emptyGoal(sessionId: string): Goal {
  return {
    schemaVersion: 'rag-ime.agent-goal.v1',
    sessionId,
    configured: false,
    goalId: '',
    revision: 0,
    objective: '',
    successCriteria: '',
    evidenceExpectations: [],
    status: 'cleared',
    budget: { tokenLimit: null, timeLimitMs: null },
    usage: { tokens: 0, elapsedMs: 0 },
    remaining: { tokens: null, timeMs: null },
    budgetExceeded: false,
    completionAudit: null,
    cancellationAudit: null,
    updatedAtMs: 0,
  };
}

function derivedActGate(todo: Todo, goal: Goal): ActGate {
  const base = {
    todoRevision: todo.revision,
    goalRevision: goal.revision,
  };
  if (goal.configured && goal.status === 'paused') {
    return { ...base, allowed: false, reason: 'goal_paused', message: '当前长期目标已暂停，恢复后才能继续写入。' };
  }
  if (goal.configured && goal.status === 'completed') {
    return { ...base, allowed: false, reason: 'goal_completed', message: '当前长期目标已结束，请清除或设置新目标后继续执行。' };
  }
  if (goal.configured && goal.status === 'cancelled') {
    return { ...base, allowed: false, reason: 'goal_cancelled', message: '当前长期目标已取消，清除后才能开始新目标。' };
  }
  if (goal.configured && goal.budgetExceeded) {
    return { ...base, allowed: false, reason: 'goal_budget_exhausted', message: '长期目标的 Token 或时间预算已经耗尽。' };
  }
  return {
    ...base,
    allowed: true,
    reason: 'user_execution_request',
    message: '用户的执行请求允许在已授权工作区内继续。',
  };
}

function gateMatches(gate: ActGate, todo: Todo, goal: Goal): boolean {
  return gate.todoRevision === todo.revision && gate.goalRevision === goal.revision;
}

function assertWorkflowOwner(
  workflow: AgentWorkflowStateV1,
  ownerSessionId: string,
): AgentWorkflowStateV1 {
  if (workflow.sessionId !== ownerSessionId) {
    throw new Error('返回的是另一段对话的结果，当前对话没有更新。');
  }
  return workflow;
}

function formatWorkflowTime(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return '时间未记录';
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value));
}

export function goalStatusLabel(status: Goal['status']): string {
  return {
    active: '进行中',
    paused: '已暂停',
    completed: '已完成',
    cancelled: '已取消',
    cleared: '未设置',
  }[status];
}

function goalBudgetRows(goal: Goal): { label: string; value: string; percent: number }[] {
  const rows = [];
  if (goal.budget.tokenLimit) rows.push({
    label: '模型用量',
    value: `${formatCompact(goal.usage.tokens)} / ${formatCompact(goal.budget.tokenLimit)}`,
    percent: Math.min(100, Math.round((goal.usage.tokens / goal.budget.tokenLimit) * 100)),
  });
  if (goal.budget.timeLimitMs) rows.push({
    label: '时间',
    value: `${formatDuration(goal.usage.elapsedMs)} / ${formatDuration(goal.budget.timeLimitMs)}`,
    percent: Math.min(100, Math.round((goal.usage.elapsedMs / goal.budget.timeLimitMs) * 100)),
  });
  return rows;
}

function formatCompact(value: number): string {
  return new Intl.NumberFormat('zh-CN', { notation: 'compact', maximumFractionDigits: 1 }).format(value);
}

function formatDuration(value: number): string {
  const minutes = Math.round(value / 60_000);
  return minutes >= 60 ? `${Math.floor(minutes / 60)}时${minutes % 60}分` : `${minutes}分钟`;
}

function isAbsentWorkflow(error: unknown): boolean {
  if (typeof error !== 'object' || error === null) return false;
  const status = 'status' in error && typeof error.status === 'number'
    ? error.status
    : 'statusCode' in error && typeof error.statusCode === 'number'
      ? error.statusCode
      : 0;
  if (status === 404) return true;
  const message = error instanceof Error ? error.message : '';
  return /\b404\b|workflow (?:is )?not found|no workflow state/i.test(message);
}

function publicError(error: unknown): string {
  if (!(error instanceof Error)) return '操作失败，请刷新后重试。';
  if (/agent goal changed; refresh before saving/i.test(error.message)) {
    return '目标已由其他操作更新，当前已显示最新状态，请重试。';
  }
  return error.message.slice(0, 240);
}
