/**
 * Shared multi-Agent timeline model. Pure data: Room (planet partners, the
 * shared Room origin and Tool Agent satellites) and a single Agent Session
 * (its Tool Agent tree) both project real receipts into this shape.
 *
 * Nothing here schedules work or invents duration. A segment only exists when
 * a receipt opened it; an open segment ends at "now" only while its source is
 * still live. Gaps longer than GAP_MS are compressed on the time axis so a
 * long idle wait never squeezes the actual work into a sliver.
 */

export type CollabLaneKind = 'origin' | 'partner' | 'satellite' | 'agent';
export type CollabLaneState = 'idle' | 'thinking' | 'working' | 'waiting' | 'reviewing' | 'done' | 'error' | 'stopped';
export type CollabSegmentKind = 'plan' | 'execute' | 'review' | 'synthesize' | 'wait' | 'origin' | 'satellite' | 'chat';
export type CollabHandoffKind = 'request' | 'dispatch' | 'submit' | 'review' | 'accept' | 'return' | 'message' | 'reply' | 'recruit' | 'result';
export type CollabMarkKind = 'tool' | 'tool_failed' | 'accept' | 'return' | 'recruit' | 'model' | 'final' | 'final_unfinished';

export interface CollabLane {
  id: string;
  kind: CollabLaneKind;
  label: string;
  /** Planet ordinal for partner lanes; drives the shared avatar identity. */
  ordinal?: number;
  role: string;
  /** Lane that launched a satellite / child agent. */
  parentId?: string;
  depth: number;
  state: CollabLaneState;
  status: string;
  /** Model actually admitted for this lane's latest dispatch, when known. */
  model?: string;
  sessionId?: string;
  /** Latest Tool Agent run id for satellite / agent lanes. */
  runId?: string;
  /** First receipt that made this lane appear (recruitment animates once). */
  joinedAtMs?: number;
}

export interface CollabSegment {
  id: string;
  laneId: string;
  kind: CollabSegmentKind;
  startMs: number;
  endMs: number;
  open: boolean;
  label: string;
  failed?: boolean;
}

export interface CollabHandoff {
  id: string;
  kind: CollabHandoffKind;
  fromLaneId: string;
  toLaneId: string;
  atMs: number;
  label: string;
  pending?: boolean;
  failed?: boolean;
}

export interface CollabMark {
  id: string;
  laneId: string;
  kind: CollabMarkKind;
  atMs: number;
  label: string;
}

export interface CollabEvent {
  id: string;
  laneId: string;
  atMs: number;
  tone: 'origin' | 'plan' | 'execute' | 'review' | 'wait' | 'done' | 'fail' | 'satellite';
  actor: string;
  text: string;
}

export interface CollabPhase {
  key: string;
  label: string;
  state: 'pending' | 'current' | 'done' | 'failed';
}

export interface CollabTask {
  id: string;
  objective: string;
  ownerLaneId: string;
  state: string;
  stateLabel?: string;
  waitingOn?: string[];
  expectedOutput: string;
  acceptance: string[];
  result: string;
}
export interface CollabDispatch {
  id: string;
  taskId: string;
  fromLaneId: string;
  toLaneId: string;
  objective: string;
  state: string;
  atMs: number;
}

export interface CollabTimeline {
  id: string;
  title: string;
  lanes: CollabLane[];
  segments: CollabSegment[];
  handoffs: CollabHandoff[];
  marks: CollabMark[];
  events: CollabEvent[];
  phases: CollabPhase[];
  tasks?: CollabTask[];
  dispatches?: CollabDispatch[];
  startMs: number;
  endMs: number;
  live: boolean;
  final: boolean;
  stopped: boolean;
  /** Session child completion is not acceptance or the parent Session's final. */
  scope?: 'session' | 'room';
  settled?: boolean;
  failed?: boolean;
  stopping?: boolean;
  counts: { accepted: number; total: number; running: number; tools: number; failed: number; satellites: number; returned?: number };
  /** The item currently being handed around, shown as the travelling capsule. */
  focus?: { laneId: string; label: string; state: 'moving' | 'held' | 'done' };
}

export const COLLAB_GAP_MS = 45_000;
const GAP_VISUAL_MS = 4_000;

export interface CollabTimeScale {
  /** Map a real timestamp to 0..1 along the compressed axis. */
  x: (atMs: number) => number;
  /** Inverse, for scrubbing. */
  t: (fraction: number) => number;
  breaks: { at: number; hiddenMs: number }[];
  ticks: { at: number; label: string }[];
}

/** Piecewise-linear axis: every gap above COLLAB_GAP_MS is shown as a fixed,
 * visibly marked break so the real work keeps readable width. */
export function collabTimeScale(timeline: Pick<CollabTimeline, 'startMs' | 'endMs' | 'segments' | 'handoffs' | 'marks'>): CollabTimeScale {
  const start = timeline.startMs;
  const end = Math.max(timeline.endMs, start + 1);
  const stamps = [...new Set([
    start, end,
    ...timeline.segments.flatMap((segment) => [segment.startMs, segment.endMs]),
    ...timeline.handoffs.map((handoff) => handoff.atMs),
    ...timeline.marks.map((mark) => mark.atMs),
  ].filter((value) => value >= start && value <= end))].sort((a, b) => a - b);
  // Covered intervals: segments count as continuous activity.
  const covered = timeline.segments.filter((segment) => segment.kind !== 'origin' && segment.kind !== 'wait').map((segment) => [segment.startMs, segment.endMs] as const).sort((a, b) => a[0] - b[0]);
  const isCovered = (from: number, to: number) => covered.some(([a, b]) => a <= from && b >= to);
  const knots: { real: number; visual: number }[] = [{ real: start, visual: 0 }];
  const breaks: { realAt: number; hiddenMs: number }[] = [];
  let visual = 0;
  for (let index = 1; index < stamps.length; index += 1) {
    const from = stamps[index - 1]!;
    const to = stamps[index]!;
    const span = to - from;
    const compress = span > COLLAB_GAP_MS && !isCovered(from, to);
    visual += compress ? GAP_VISUAL_MS : span;
    if (compress) breaks.push({ realAt: from, hiddenMs: span - GAP_VISUAL_MS });
    knots.push({ real: to, visual });
  }
  const total = Math.max(1, visual);
  const x = (atMs: number) => {
    if (atMs <= start) return 0;
    if (atMs >= end) return 1;
    for (let index = 1; index < knots.length; index += 1) {
      const a = knots[index - 1]!;
      const b = knots[index]!;
      if (atMs <= b.real) {
        const local = b.real === a.real ? 0 : (atMs - a.real) / (b.real - a.real);
        return (a.visual + local * (b.visual - a.visual)) / total;
      }
    }
    return 1;
  };
  const t = (fraction: number) => {
    const target = Math.min(1, Math.max(0, fraction)) * total;
    for (let index = 1; index < knots.length; index += 1) {
      const a = knots[index - 1]!;
      const b = knots[index]!;
      if (target <= b.visual) {
        const local = b.visual === a.visual ? 0 : (target - a.visual) / (b.visual - a.visual);
        return a.real + local * (b.real - a.real);
      }
    }
    return end;
  };
  const tickCount = 5;
  const ticks = Array.from({ length: tickCount }, (_, index) => {
    const at = index / (tickCount - 1);
    return { at, label: clockLabel(t(at), end - start < 10 * 60_000) };
  });
  return {
    x, t, ticks,
    breaks: breaks.map((item) => ({ at: x(item.realAt + 1) , hiddenMs: item.hiddenMs })),
  };
}

export function clockLabel(atMs: number, seconds = false): string {
  if (!Number.isFinite(atMs) || atMs <= 0) return '--:--';
  const date = new Date(atMs);
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}${seconds ? `:${pad(date.getSeconds())}` : ''}`;
}

export function durationLabel(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分${seconds % 60 ? ` ${seconds % 60} 秒` : ''}`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

/** The lane state at an arbitrary replay instant, derived from segments only. */
export function collabLaneStateAt(timeline: CollabTimeline, lane: CollabLane, atMs: number): { state: CollabLaneState; label: string } {
  if (atMs >= timeline.endMs - 1) return { state: lane.state, label: lane.status };
  if (lane.joinedAtMs && atMs < lane.joinedAtMs) return { state: 'idle', label: '尚未加入' };
  const active = timeline.segments.filter((segment) => segment.laneId === lane.id && segment.startMs <= atMs && segment.endMs > atMs);
  const segment = active.find((item) => item.kind !== 'wait') ?? active[0];
  if (!segment) {
    const ended = timeline.segments.filter((item) => item.laneId === lane.id && item.endMs <= atMs);
    if (!ended.length) return { state: 'idle', label: '待命' };
    const last = ended.reduce((a, b) => (a.endMs >= b.endMs ? a : b));
    return last.failed ? { state: 'error', label: `${last.label} · 失败` } : { state: 'done', label: `${last.label} · 已交回` };
  }
  const state: CollabLaneState = segment.kind === 'wait' ? 'waiting'
    : segment.kind === 'plan' || segment.kind === 'synthesize' ? 'thinking'
      : segment.kind === 'review' ? 'reviewing' : 'working';
  return { state, label: segment.label };
}

/** Who holds the focus item at an instant: the destination of the last handoff. */
export function collabFocusAt(timeline: CollabTimeline, atMs: number): { laneId: string; fromLaneId: string; sinceMs: number } | undefined {
  const moves = timeline.handoffs.filter((handoff) => handoff.atMs <= atMs && !handoff.failed && !handoff.pending && !['message', 'reply', 'recruit'].includes(handoff.kind)).sort((a, b) => a.atMs - b.atMs);
  const last = moves.at(-1);
  if (!last) {
    const origin = timeline.lanes.find((lane) => lane.kind === 'origin') ?? timeline.lanes[0];
    return origin ? { laneId: origin.id, fromLaneId: origin.id, sinceMs: timeline.startMs } : undefined;
  }
  return { laneId: last.toLaneId, fromLaneId: last.fromLaneId, sinceMs: last.atMs };
}

/** Replay is one deterministic clock. Interrupt a handoff at its current
 * position instead of teleporting back to the next sender's lane. */
export function collabFocusPositionAt(
  timeline: CollabTimeline,
  atMs: number,
  laneY: (id: string) => number,
  durationMs = 4200,
): number {
  const origin = timeline.lanes.find((lane) => lane.kind === 'origin') ?? timeline.lanes[0];
  let from = origin ? laneY(origin.id) : 0;
  let to = from;
  let since = timeline.startMs;
  const position = (time: number) => {
    const progress = Math.min(1, Math.max(0, (time - since) / durationMs));
    return from + (to - from) * (1 - (1 - progress) ** 3);
  };
  const moves = timeline.handoffs.filter((item) => item.atMs <= atMs && !item.failed && !item.pending && !['message', 'reply', 'recruit'].includes(item.kind)).sort((a, b) => a.atMs - b.atMs);
  for (const move of moves) {
    from = position(move.atMs);
    to = laneY(move.toLaneId);
    since = move.atMs;
  }
  return position(atMs);
}

export function collabPhasesAt(timeline: CollabTimeline, atMs: number): CollabPhase[] {
  if (atMs >= timeline.endMs - 1) return timeline.phases;
  const reached = new Set<string>();
  const failed = new Set<string>();
  for (const segment of timeline.segments) {
    if (segment.startMs > atMs) continue;
    if (segment.kind === 'plan') reached.add('plan');
    if (segment.kind === 'execute' || segment.kind === 'satellite' || segment.kind === 'chat') reached.add('execute');
    if (segment.kind === 'review') reached.add('review');
    if (segment.kind === 'synthesize') reached.add('reply');
    if (segment.failed && segment.endMs <= atMs) {
      if (segment.kind === 'plan' || segment.kind === 'execute' || segment.kind === 'review') failed.add(segment.kind);
      if (segment.kind === 'synthesize') failed.add('reply');
    }
  }
  for (const mark of timeline.marks) if (mark.atMs <= atMs && mark.kind === 'accept') reached.add('accept');
  for (const mark of timeline.marks) if (mark.atMs <= atMs && (mark.kind === 'final' || mark.kind === 'final_unfinished')) {
    reached.add('reply');
    if (mark.kind === 'final_unfinished') failed.add('reply');
  }
  const order = timeline.phases.map((phase) => phase.key);
  const observed = order.filter((key) => reached.has(key) || failed.has(key));
  const lastIndex = observed.length - 1;
  return observed.map((key, index) => {
    const phase = timeline.phases.find((candidate) => candidate.key === key)!;
    return { ...phase, state: failed.has(key) ? 'failed' : index < lastIndex ? 'done' : 'current' };
  });
}

export const COLLAB_PHASES: readonly { key: string; label: string }[] = [
  { key: 'plan', label: '分工' },
  { key: 'execute', label: '执行' },
  { key: 'review', label: '复核' },
  { key: 'accept', label: '验收' },
  { key: 'reply', label: '答复' },
];

/** Phases from observed evidence at the end of the timeline. */
export function collabPhasesFromEvidence(input: {
  reached: ReadonlySet<string>;
  final: boolean;
  failed?: ReadonlySet<string>;
}): CollabPhase[] {
  const order = COLLAB_PHASES.map((phase) => phase.key);
  const failed = input.failed ?? new Set<string>();
  const observed = order.filter((key) => input.reached.has(key) || failed.has(key));
  const lastIndex = observed.length - 1;
  return observed.map((key, index) => {
    const phase = COLLAB_PHASES.find((candidate) => candidate.key === key)!;
    return {
      ...phase,
      state: failed.has(key) ? 'failed' : input.final || index < lastIndex ? 'done' : 'current',
    };
  });
}
