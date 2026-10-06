import { useEffect, useState } from 'react';
import { useOptionalControlTransport } from '@/app/control-transport';
import type { AgentProjectionState } from '@/contracts/agent-reducer';
import { agentSessionAddress, selectAgentProjection, useAgentLiveStore } from '../state/live-store';
import { isRoomPublicPostMessage } from './AgentTimeline';

const ACTIVE_TURN_STATUSES = new Set(['queued', 'running', 'waiting']);
const TERMINAL_TURN_STATUSES = new Set(['completed', 'failed', 'aborted']);

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function latestTurnId(projection: AgentProjectionState | undefined): string {
  return projection?.turnOrder.at(-1) ?? '';
}

function turnStatus(projection: AgentProjectionState | undefined, turnId: string): string {
  return turnId ? projection?.turnsById[turnId]?.status ?? '' : '';
}

/**
 * One sentence describing a turn that just reached a terminal status.
 *
 * It deliberately reports that the turn ended and how much there is to read,
 * and never echoes the answer. The reply already exists in the log one node
 * away; copying it here would duplicate it in select-all, in find-in-page and
 * in every text query, and would put a truncated second copy of the answer in
 * a transcript whose whole premise is that it is the record.
 */
export function settledTurnAnnouncement(
  projection: AgentProjectionState | undefined,
  turnId: string,
): string {
  const turn = projection?.turnsById[turnId];
  if (!projection || !turn) return '';
  if (turn.status === 'aborted') return '本轮已停止。';
  if (turn.status === 'failed') return '本轮未完成，时间线里有失败说明与恢复操作。';
  const characters = turn.messageIds
    .map((messageId) => projection.messagesById[messageId])
    .filter((message) => (
      message?.role === 'assistant' && !isRoomPublicPostMessage(message)
    ))
    .flatMap((message) => message?.blocks ?? [])
    .map((block) => text(block.data.text ?? block.data.markdown).replace(/\s+/gu, ''))
    .reduce((total, value) => total + value.length, 0);
  return characters > 0
    ? `Agent 回复已完成，共 ${characters} 字。`
    : 'Agent 本轮没有文本回复，工作明细在时间线中。';
}

/**
 * Settle-only announcement for the transcript.
 *
 * The timeline is a `log` whose body is rewritten on every batched token
 * commit. Left as an implicit live region it made a screen reader read the
 * answer aloud dozens of times as it grew. The log is therefore silent and
 * this element speaks exactly once per settled turn.
 */
export function SettledTurnAnnouncer({ sessionId }: { sessionId: string }) {
  const transport = useOptionalControlTransport();
  const address = agentSessionAddress(transport, sessionId);
  const [announcement, setAnnouncement] = useState('');
  useEffect(() => {
    setAnnouncement('');
    const initialProjection = selectAgentProjection(useAgentLiveStore.getState(), address);
    let previousTurnId = latestTurnId(initialProjection);
    let previousStatus = turnStatus(
      initialProjection,
      previousTurnId,
    );
    return useAgentLiveStore.subscribe((state, previousState) => {
      const projection = selectAgentProjection(state, address);
      if (projection === selectAgentProjection(previousState, address)) return;
      const turnId = latestTurnId(projection);
      const status = turnStatus(projection, turnId);
      const settled = turnId === previousTurnId
        && ACTIVE_TURN_STATUSES.has(previousStatus)
        && TERMINAL_TURN_STATUSES.has(status);
      previousTurnId = turnId;
      previousStatus = status;
      if (!settled) return;
      const next = settledTurnAnnouncement(projection, turnId);
      // A repeated string in a live region is not re-announced; the trailing
      // space makes two identical consecutive replies distinguishable.
      setAnnouncement((current) => (current === next ? `${next} ` : next));
    });
  }, [address]);
  return (
    /* Its own live region: the enclosing log is explicitly `off`, and a
       descendant live root is not suppressed by a silent ancestor. No
       `role="status"` — the transcript already carries visible status
       elements, and a second one would compete with them in the a11y tree. */
    <p
      aria-atomic="true"
      aria-live="polite"
      className="agent-timeline__announcer"
      data-transcript-announcer=""
    >
      {announcement}
    </p>
  );
}
