import { LoaderCircle, PencilLine } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { Button } from '@/components/primitives';
import { publicAgentErrorText } from '@/features/agent/public-error';
import type { JevSnapshot, JevTask } from './jev-execution';
import type { JevRevisionControls, JevRevisionInput, JevRevisionOptions, JevRevisionReceipt } from './jev-task-revision';
import './jev-task-revision.css';

const UNAVAILABLE: Record<string, string> = { root_inactive: '本轮已结束，不能修改当前任务。', revision_pending: '已有修改正在等待执行停止。',
  task_not_current: '此任务已由新版本接手，请打开当前任务。', task_not_current_leaf: '请打开当前版本的具体执行任务。',
  phase_unavailable: '执行阶段开始后可修改具体任务。', task_unavailable: '这项任务当前不能修改。',
  reclaim_pending: '相关任务正在改派，交接完成后再修改要求。', container_task: '请在具体执行任务中修改要求。' };

/** The existing task inspector owns editing; no second approval or chat loop. */
export function JevTaskRevision({ graph, task, controls, active }: {
  graph: JevSnapshot; task: JevTask; controls: JevRevisionControls; active: boolean;
}) {
  const id = useId(); const request = useRef<AbortController | null>(null); const mounted = useRef(true);
  const pending = controls.pending(graph.graphId, task.id);
  const [expanded, setExpanded] = useState(Boolean(pending));
  const [options, setOptions] = useState<JevRevisionOptions | null>(null);
  const [objective, setObjective] = useState(pending?.objective ?? task.objective);
  const [output, setOutput] = useState(pending?.expectedOutput ?? task.expectedOutput);
  const [criteria, setCriteria] = useState((pending?.acceptanceCriteria ?? task.acceptance).join('\n'));
  const [reason, setReason] = useState(pending?.reason ?? '');
  const [rootObjective, setRootObjective] = useState(pending?.rootObjective ?? '');
  const [error, setError] = useState(''); const [loading, setLoading] = useState(false); const [sending, setSending] = useState(false);
  const [receipt, setReceipt] = useState<JevRevisionReceipt | null>(null);
  const revision = graph.revisions?.find(item => item.changedTaskId === task.id || item.affectedTaskIds.includes(task.id));
  const unavailable = !active || !pending && (graph.stopped || Boolean(graph.final));
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; request.current?.abort(); }; }, []);
  const load = async () => {
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    setLoading(true); setError(''); setOptions(null);
    try {
      const next = await controls.load(graph.graphId, task.id, controller.signal);
      if (mounted.current && !controller.signal.aborted) setOptions(next);
    } catch (cause) { if (mounted.current && !controller.signal.aborted) setError(publicAgentErrorText(cause, '无法读取修改范围。')); }
    finally { if (mounted.current && !controller.signal.aborted) setLoading(false); }
  };
  const submit = async () => {
    if (unavailable || sending || (!pending && !options?.available)) return;
    const acceptance = criteria.split('\n').map(line => line.trim()).filter(Boolean);
    if (!pending && (!objective.trim() || !output.trim() || !reason.trim() || acceptance.length < 1 || acceptance.length > 8 || acceptance.some(line => line.length > 500))) {
      setError('请填写任务目标、交付物、修改说明，以及 1–8 条验收标准，每条不超过 500 字。'); return;
    }
    const input: JevRevisionInput = pending ?? { action: 'revise_task', graphId: graph.graphId, rootId: options!.rootId,
      taskId: task.id, taskHash: options!.taskHash, expectedTopologyRevision: options!.expectedTopologyRevision,
      expectedRequirementsRevision: options!.expectedRequirementsRevision, objective: objective.trim(), expectedOutput: output.trim(),
      acceptanceCriteria: acceptance, reason: reason.trim(), ...(rootObjective.trim() ? { rootObjective: rootObjective.trim() } : {}) };
    setSending(true); setError('');
    try { const next = await controls.submit(input); if (mounted.current) setReceipt(next); }
    catch (cause) { if (mounted.current) setError(publicAgentErrorText(cause, '任务修改尚未确认，请核实同一次修改。')); }
    finally { if (mounted.current) setSending(false); }
  };
  const title = (taskId: string) => graph.tasks.find(item => item.id === taskId)?.objective ?? '相关任务';
  const status = revision?.status ?? receipt?.status;
  if (status && !pending) return <section className="jev-task-revision" aria-label="任务修改进度"><p role="status">
    {status === 'awaiting_drain' ? <><LoaderCircle className="jev-task-revision__spinner" size={15} /> 修改已收到，等待受影响的旧执行停止。独立任务继续保留。</>
      : status === 'applied' ? '新版本已生效，任务将按更新后的依赖继续。' : '本次修改已停止，请查看当前任务状态。'}
  </p></section>;
  return <section className="jev-task-revision" aria-label="修改当前任务">
    <Button variant="quiet" disabled={unavailable} aria-expanded={expanded} onClick={() => {
      setExpanded(!expanded); if (!expanded && !pending) void load();
    }}><PencilLine size={15} />修改任务要求</Button>
    {expanded ? <div className="jev-task-revision__body">
      <p>更新本任务和受影响的下游，保留其他已验收成果。</p>
      {loading ? <p role="status"><LoaderCircle className="jev-task-revision__spinner" size={15} />正在读取影响范围…</p> : null}
      {options?.available ? <div className="jev-task-revision__impact"><strong>将重做 {options.affectedTaskIds.length} 项任务</strong>
        <ul>{options.affectedTaskIds.map(taskId => <li key={taskId}>{title(taskId)}</li>)}</ul>
        <p>保留 {options.retainedAcceptedTaskIds.length} 项已验收任务</p></div> : null}
      {options && !options.available ? <p role="status">{UNAVAILABLE[options.unavailableReason] || '当前任务暂不支持修改，请同步状态后重试。'}</p> : null}
      {options?.available || pending ? <>
        <fieldset disabled={sending || Boolean(pending) || unavailable}>
          <label htmlFor={`${id}-goal`}>任务目标</label><textarea id={`${id}-goal`} value={objective} maxLength={8000} rows={3} onChange={event => setObjective(event.target.value)} />
          <label htmlFor={`${id}-output`}>交付物</label><textarea id={`${id}-output`} value={output} maxLength={8000} rows={2} onChange={event => setOutput(event.target.value)} />
          <label htmlFor={`${id}-criteria`}>验收标准 · 每行一条</label><textarea id={`${id}-criteria`} value={criteria} maxLength={8000} rows={3} onChange={event => setCriteria(event.target.value)} />
          <label htmlFor={`${id}-reason`}>修改说明</label><textarea id={`${id}-reason`} value={reason} maxLength={2000} rows={2} onChange={event => setReason(event.target.value)} />
          <details><summary>同时更新整体目标（可选）</summary><label htmlFor={`${id}-root`}>新的整体目标</label>
            <textarea id={`${id}-root`} value={rootObjective} maxLength={8000} rows={3} placeholder="留空则保留当前整体目标" onChange={event => setRootObjective(event.target.value)} />
            <p>只重做上方列出的任务；如其他任务也受影响，请一并调整相应任务要求。</p></details>
        </fieldset>
        {pending ? <p>上次修改结果尚未确认，核实时将使用原来的内容。</p> : <p>提交后会停止受影响的旧执行，待其停止后启用新版本。</p>}
        <Button disabled={sending || unavailable} onClick={() => void submit()}>{sending ? '正在提交…' : pending ? '核实这次修改' : '应用修改'}</Button>
      </> : null}
      {error ? <p role="alert">{error}</p> : null}
      {!pending && !loading ? <Button variant="quiet" disabled={unavailable || sending} onClick={() => void load()}>刷新修改范围</Button> : null}
    </div> : null}
  </section>;
}
