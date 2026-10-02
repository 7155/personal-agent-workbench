import { LoaderCircle } from 'lucide-react';
import type { JevSnapshot } from '@/features/semantic-workspace/jev-execution';

export function roomResponsePhase({ submitting, busy, stopping, graph }: {
  submitting: boolean; busy: boolean; stopping: boolean; graph?: JevSnapshot | null;
}): string {
  if (stopping) return '正在停止';
  if (submitting) return '正在提交消息';
  if (!busy || graph?.final || graph?.stopped) return '';
  if (graph && ['awaiting_input', 'awaiting_approval', 'deferred'].includes(graph.phase)) return '';
  if (!graph || graph.phase === 'route') return '正在准备下一步';
  if (graph.phase === 'plan') return '正在整理任务方案';
  if (graph.phase === 'synthesize') return '正在汇总回复';
  const live = [...graph.effects].reverse().find(effect => effect.operation === 'dispatch'
    && !['completed', 'drained', 'idle', 'failed', 'cancelled', 'rejected', 'not_sent'].includes(effect.executionStatus)
    && !['completed', 'drained', 'idle', 'failed', 'cancelled', 'rejected', 'not_sent'].includes(effect.state)
    && ['prepared', 'pending', 'sending', 'accepted', 'admitted', 'running', 'unknown'].includes(effect.executionStatus || effect.state));
  if (live?.executionStatus === 'unknown' || live?.state === 'unknown') return '正在核实执行回执';
  if (live?.request.purpose === 'verify') return '正在核对结果';
  if (graph.review.length) return '正在判断下一步';
  return live || graph.running.length ? '正在执行任务' : '正在准备下一步';
}

/** This lives outside the transcript: feedback never waits for SSE or row measurement. */
export function PawRoomResponseStatus(props: Parameters<typeof roomResponsePhase>[0]) {
  const detail = roomResponsePhase(props);
  if (!detail) return null;
  return <div className="agent-first-response paw-room-response-status" role="status" aria-live="polite">
    <LoaderCircle size={15} aria-hidden className="ui-spin" />
    <strong>{props.stopping ? '正在停止' : 'Thinking'}</strong>
    {!props.stopping ? <span>{detail}</span> : null}
  </div>;
}
