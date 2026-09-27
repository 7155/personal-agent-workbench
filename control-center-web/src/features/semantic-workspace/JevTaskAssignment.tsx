import { ArrowRight, Check, LoaderCircle, RefreshCw, UserRoundCog } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { Button } from '@/components/primitives';
import { publicAgentErrorText } from '@/features/agent/public-error';
import { roomPlanetName } from '@/features/rooms/room-copy';
import type { RoomSummary } from '@/features/rooms/room-types';
import type { JevSnapshot, JevTask } from './jev-execution';
import type { JevAssignmentInput, JevAssignmentOptions, JevTaskControls } from './jev-task-assignment';

const UNAVAILABLE: Record<string, string> = {
  root_inactive: '本轮任务已结束或停止。', phase_unavailable: '当前阶段暂不支持改派，方案确认后可调整执行任务。',
  container_task: '请在具体执行任务中调整负责伙伴。', reclaim_pending: '该任务正在回收，等待执行停止后完成改派。',
  task_unavailable: '该任务已提交或结束，当前不能直接改派。', execution_unknown: '执行回执尚待核实，请同步状态后重试。',
  owner_locked: '当前方案已指定负责伙伴，需调整方案后更换。', no_eligible_partner: '暂无其他伙伴满足该任务的工具与工作区要求。',
};

/** One task operation inside the existing inspector, never another approval dialog. */
export function JevTaskAssignment({ graph, task, room, controls, active = true }: {
  graph: JevSnapshot; task: JevTask; room?: RoomSummary; controls: JevTaskControls; active?: boolean;
}) {
  const id = useId(); const mounted = useRef(true); const read = useRef<AbortController | null>(null);
  const [options, setOptions] = useState<JevAssignmentOptions | null>(null);
  const [expanded, setExpanded] = useState(false); const [target, setTarget] = useState('');
  const [loading, setLoading] = useState(false); const [sending, setSending] = useState(false); const [error, setError] = useState('');
  const [receipt, setReceipt] = useState<{ status: 'requested' | 'applied'; input: JevAssignmentInput } | null>(null);
  const pending = controls.pending(graph.graphId, task.id);
  const reclaim = graph.reclaims?.find(item => item.taskId === task.id && item.taskRevision === task.revision && item.dispatchId === task.acceptedTurnId);
  const finished = graph.stopped || Boolean(graph.final) || !['active', 'queued'].includes(task.state)
    || graph.revisions?.some(item => item.status === 'awaiting_drain' && item.affectedTaskIds.includes(task.id));
  const name = (participantId: string) => {
    const participant = room?.participants.find(item => item.id === participantId);
    return participant ? roomPlanetName(participant.ordinal) : '伙伴信息待同步';
  };
  const choices = room?.participants.filter(participant => options?.targetParticipantIds.includes(participant.id)) ?? [];
  const taskIdentity = JSON.stringify([graph.graphId, task]);
  useEffect(() => {
    read.current?.abort(); setLoading(false); setOptions(null); setTarget(''); setExpanded(false);
  }, [taskIdentity]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; read.current?.abort(); }; }, []);
  const load = async () => {
    read.current?.abort(); const controller = new AbortController(); read.current = controller;
    setExpanded(true); setLoading(true); setError(''); setReceipt(null);
    try {
      const result = await controls.load(graph.graphId, task.id, controller.signal);
      if (result.ownerId !== task.ownerId || (task.taskHash && result.taskHash !== task.taskHash)) throw new Error('任务状态已变化，请同步后重新读取伙伴。');
      if (!controller.signal.aborted) { setOptions(result); setTarget(''); }
    } catch (reason) {
      if (!controller.signal.aborted) setError(publicAgentErrorText(reason, '伙伴列表暂时无法读取，请重试。'));
    } finally { if (!controller.signal.aborted) setLoading(false); }
  };
  const submit = async () => {
    const input = pending ?? (options?.action && target ? {
      action: options.action, graphId: graph.graphId, taskId: task.id, taskHash: options.taskHash,
      targetParticipantId: target, reason: '用户在任务详情中调整分工',
    } : undefined);
    if (!input || sending) return;
    setSending(true); setError('');
    try {
      const status = await controls.submit(input);
      if (mounted.current) { setReceipt({ status, input }); setExpanded(false); }
    } catch (reason) {
      if (mounted.current) { setError(publicAgentErrorText(reason, '任务操作尚未确认，请核实后再继续。')); setOptions(null); }
    } finally { if (mounted.current) setSending(false); }
  };
  const spinner = active && !graph.stopped ? <LoaderCircle size={14} className="paw-jev-partner__spinner" aria-hidden /> : null;
  const waiting = reclaim || (receipt?.status === 'requested' && task.ownerId !== receipt.input.targetParticipantId);
  return <section className="jev-task-assignment" aria-label="调整任务分工" aria-busy={loading || sending}>
    <h3>负责伙伴</h3>
    <div className="jev-task-assignment__owner"><strong>{name(task.ownerId)}</strong>{reclaim ? <><ArrowRight size={14} aria-hidden /><span>{name(reclaim.targetParticipantId)} · 待接手</span></> : null}
      {!finished && !waiting && !pending && !expanded ? <Button variant="quiet" leadingIcon={<UserRoundCog size={14} />} onClick={() => void load()} disabled={!active || sending}>更换伙伴</Button> : null}
    </div>
    {waiting ? <p className="jev-task-assignment__notice" role="status">{spinner}{graph.stopped ? '本轮已停止，正在核实原执行的停止回执。' : reclaim?.stage === 'awaiting_assignment' ? '执行已停止，等待目标伙伴满足接手条件。' : `回收请求已受理，等待原执行停止后交给 ${name(reclaim?.targetParticipantId || receipt!.input.targetParticipantId)}。`}</p>
      : receipt?.status === 'applied' ? <p className="jev-task-assignment__notice" role="status"><Check size={14} aria-hidden />责任已改派，执行进度以最新回执为准。</p> : null}
    {pending ? <div className="jev-task-assignment__recovery"><p>上次操作尚未确认；核实会使用原请求，不会重复创建任务。</p><Button variant="quiet" leadingIcon={sending ? spinner : <RefreshCw size={14} />} disabled={sending || !active} onClick={() => void submit()}>{sending ? '正在核实' : '核实同一次操作'}</Button></div> : null}
    {expanded && !pending && !waiting && !finished ? <div className="jev-task-assignment__form">
      {loading ? <p role="status">{spinner}正在读取可接手的伙伴…</p> : options?.action ? <>
        <label htmlFor={id}>接手伙伴</label><select id={id} value={target} onChange={event => setTarget(event.target.value)} disabled={sending || !active}>
          <option value="">选择伙伴</option>{choices.map(participant => <option key={participant.id} value={participant.id}>{roomPlanetName(participant.ordinal)}</option>)}
        </select>
        <p>{options.action === 'request_reclaim' ? '先停止当前执行，收到真实停止回执后再交接。已有产物会保留。' : '保留当前任务与依赖；接手伙伴空闲后继续执行。'}</p>
        <div className="jev-task-assignment__actions"><Button variant="primary" disabled={!target || sending || !active} leadingIcon={sending ? spinner : undefined} onClick={() => void submit()}>{sending ? '正在提交' : options.action === 'request_reclaim' ? '回收并改派' : '改派任务'}</Button><Button variant="quiet" disabled={sending} onClick={() => { read.current?.abort(); setExpanded(false); setError(''); }}>取消</Button></div>
      </> : options ? <p>{UNAVAILABLE[options.unavailableReason] || '暂时没有可用的任务操作，请同步后重试。'}</p> : null}
      {!loading && !options ? <Button variant="quiet" onClick={() => void load()} disabled={!active}>重新读取伙伴</Button> : null}
    </div> : null}
    {error ? <p className="jev-task-assignment__error" role="alert">{error}</p> : null}
  </section>;
}
