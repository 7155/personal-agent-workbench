import { useMemo, useState } from 'react';
import type { AgentSubagentRunV1 } from '@/contracts/generated/agent-subagent-run.v1';
import { CollabTimelineOverlay, CollabTimelinePeek, CollabTimelineStage } from './CollabTimelineStage';
import type { CollabLane } from './model';
import { useLiveClock } from './RoomCollabTimeline';
import { buildSessionCollabTimeline } from './session-timeline';

/** Agent app: the Session and its Tool Agent satellites on one stage. */
export function SessionCollabTimeline({ sessionId, title, runs, busy = false, active, onOpenRun }: {
  sessionId: string;
  title: string;
  runs: readonly AgentSubagentRunV1[];
  busy?: boolean;
  active: boolean;
  onOpenRun?: (run: AgentSubagentRunV1) => void;
}) {
  const [open, setOpen] = useState(false);
  const live = runs.some((run) => run.state === 'queued' || run.state === 'running');
  const now = useLiveClock(live && active);
  const timeline = useMemo(() => buildSessionCollabTimeline({ sessionId, title, runs, busy, nowMs: now }), [sessionId, title, runs, busy, now]);
  if (!runs.length) return null;
  const openLane = (lane: CollabLane) => {
    const nodeId = lane.id.replace(/^run:/u, '');
    const run = [...runs].filter((item) => (item.nodeId || item.id) === nodeId).sort((a, b) => b.attemptNumber - a.attemptNumber)[0];
    if (run) { setOpen(false); onOpenRun?.(run); }
  };
  return <>
    <CollabTimelinePeek timeline={timeline} active={active} title="卫星协作时间线" onExpand={() => setOpen(true)} />
    <CollabTimelineOverlay open={open} onClose={() => setOpen(false)} title="卫星协作时间线">
      <CollabTimelineStage timeline={timeline} active={active && open} onOpenLane={onOpenRun ? openLane : undefined} />
    </CollabTimelineOverlay>
  </>;
}
