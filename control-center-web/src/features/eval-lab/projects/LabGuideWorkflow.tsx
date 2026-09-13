import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button, Input } from '@/components/primitives';
import { parseContract } from '@/contracts/validators';
import type { AgentWorkflowStateV1, Goal } from '@/contracts/generated/agent-workflow-state.v1';
import { AgentWorkflowPanel } from '@/features/agent/status/AgentWorkflowPanel';
import { projectError } from './api';

/** Native Goal controls for the same Guide Session; no project scheduler. */
export function LabGuideWorkflow({ sessionId }: { sessionId: string }) {
  const [open, setOpen] = useState(false); const [workflow, setWorkflow] = useState<AgentWorkflowStateV1>();
  return <div className="lab-guide-workflow"><Button size="small" aria-expanded={open} onClick={() => setOpen((value) => !value)}>项目 Agent 推进与预算</Button>
    {open ? <div className="lab-guide-workflow__panel"><p>查看同一个项目 Agent 的目标与实际用量；这里的预算只约束其续行 Token 和时间。独立评测各有用量与限制，这不是整个项目的费用上限；暂停项目 Agent 不会取消所有后台任务。</p>
      <AgentWorkflowPanel sessionId={sessionId} onWorkflowResolved={setWorkflow} />
      {workflow?.sessionId === sessionId && workflow.goal.configured ? <GuideBudget key={sessionId} sessionId={sessionId} goal={workflow.goal} /> : null}
    </div> : null}
  </div>;
}

function GuideBudget({ sessionId, goal }: { sessionId: string; goal: Goal }) {
  const transport = useControlTransport(); const client = useQueryClient();
  const [tokens, setTokens] = useState(goal.budget.tokenLimit === null ? '' : String(goal.budget.tokenLimit)); const [minutes, setMinutes] = useState(goal.budget.timeLimitMs === null ? '' : String(goal.budget.timeLimitMs / 60000));
  useEffect(() => { setTokens(goal.budget.tokenLimit === null ? '' : String(goal.budget.tokenLimit)); setMinutes(goal.budget.timeLimitMs === null ? '' : String(goal.budget.timeLimitMs / 60000)); }, [goal.revision, goal.budget.tokenLimit, goal.budget.timeLimitMs]);
  const tokenBudget = tokens.trim() ? Number(tokens) : null;
  const timeBudgetMs = minutes.trim() ? Math.round(Number(minutes) * 60000) : null;
  const valid = (tokenBudget === null || (Number.isSafeInteger(tokenBudget) && tokenBudget > 0 && tokenBudget <= 100000000))
    && (timeBudgetMs === null || (Number.isSafeInteger(timeBudgetMs) && timeBudgetMs > 0 && timeBudgetMs <= 31536000000));
  const changed = tokenBudget !== goal.budget.tokenLimit || timeBudgetMs !== goal.budget.timeLimitMs;
  const mutation = useMutation({ mutationFn: async () => {
    const raw = await transport.request({ pathId: 'agent.session.goal.mutate', params: { sessionId }, body: { action: 'update', expectedRevision: goal.revision, tokenBudget, timeBudgetMs } });
    const next = parseContract('agent-workflow-state.v1', raw);
    if (next.sessionId !== sessionId) throw new Error('预算回执不属于当前项目 Agent。');
    client.setQueryData(['agent', 'workflow', sessionId], next);
  }, onSettled: () => { void client.invalidateQueries({ queryKey: ['agent', 'workflow', sessionId] }); } });
  return <form className="lab-guide-budget" aria-label="项目 Agent 续行预算" onSubmit={(event) => { event.preventDefault(); if (valid && changed && !mutation.isPending) mutation.mutate(); }}>
    <h4>调整此 Agent 的续行预算</h4><p>保留已有用量，不会重新开始目标。留空表示不设置该项上限。</p>
    <label>Token 上限<Input type="number" min={1} max={100000000} step={1} value={tokens} disabled={mutation.isPending} onChange={(event) => setTokens(event.target.value)} /></label>
    <label>时间上限（分钟）<Input type="number" min={0} max={525600} step="any" value={minutes} disabled={mutation.isPending} onChange={(event) => setMinutes(event.target.value)} /></label>
    <Button type="submit" size="small" disabled={!valid || !changed || mutation.isPending}>{mutation.isPending ? '正在保存预算…' : '保存预算'}</Button>
    {mutation.isError ? <p role="alert">{projectError(mutation.error)} 请核对刷新后的原目标预算再继续。</p> : null}
  </form>;
}
