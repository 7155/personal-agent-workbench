import { AnimatePresence, MotionConfig, motion } from 'motion/react';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/primitives/Dialog';
import { usePresentationMotion } from '@/features/conversation-ui/reading/reading-preferences';
import { Check, CircleAlert, Cpu, FastForward, Pause, Play, RotateCcw, Satellite, SkipBack, SkipForward, Sparkles, UserPlus } from 'lucide-react';
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from 'react';
import {
  clockLabel,
  collabFocusAt,
  collabLaneStateAt,
  collabPhasesAt,
  collabTimeScale,
  durationLabel,
  type CollabEvent,
  type CollabHandoff,
  type CollabLane,
  type CollabLaneState,
  type CollabMark,
  type CollabSegment,
  type CollabTimeline,
} from './model';
import './collab-timeline.css';

export type CollabAvatarRenderer = (lane: CollabLane, size: number, state: CollabLaneState) => ReactNode;

const LANE_HEAD = 196;
const RIGHT_PAD = 28;
const TOP_PAD = 36;
const HANDOFF_MS = 700;

/**
 * The shared multi-Agent stage: one lane per planet / satellite / Tool Agent,
 * work drawn as it really happened, handoffs flying between lanes, and a
 * replay head that can scrub the whole round. When the source is live the
 * head sticks to "now" and new receipts animate in; history stays still.
 */
export function CollabTimelineStage({
  timeline,
  renderAvatar,
  active = true,
  onOpenLane,
  compact = false,
  eventsPanel = true,
  className,
}: {
  timeline: CollabTimeline;
  renderAvatar?: CollabAvatarRenderer;
  active?: boolean;
  onOpenLane?: (lane: CollabLane) => void;
  compact?: boolean;
  eventsPanel?: boolean;
  className?: string;
}) {
  const animate = usePresentationMotion(active);
  const scale = useMemo(() => collabTimeScale(timeline), [timeline]);
  const stageRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const feedRef = useRef<HTMLOListElement>(null);
  const size = useElementSize(stageRef);
  const uid = useId().replace(/:/gu, '');

  // Playback: 'follow' tracks the live edge; 'replay' animates from start.
  const [mode, setMode] = useState<'follow' | 'replay' | 'paused'>('follow');
  const [cursor, setCursor] = useState(1);
  const [speed, setSpeed] = useState(1);
  const [hoverLane, setHoverLane] = useState('');
  const [selectedEvent, setSelectedEvent] = useState('');
  const identity = timeline.id;
  useEffect(() => {
    setMode('follow');
    setCursor(1);
    seen.current = null;
    setFresh(new Set());
    setSelectedEvent('');
  }, [identity]); // eslint-disable-line react-hooks/exhaustive-deps

  const replayDurationMs = Math.min(16_000, Math.max(6_000, timeline.events.length * 900));
  useAnimationFrame(animate && mode === 'replay', (dt) => {
    setCursor((value) => {
      const next = Math.min(1, value + (dt * speed) / replayDurationMs);
      if (next >= 1) setMode('follow');
      return next;
    });
  });
  const atMs = mode === 'follow' ? timeline.endMs : scale.t(cursor);
  const fraction = mode === 'follow' ? 1 : cursor;

  // Geometry.
  const lanes = timeline.lanes;
  const laneHeight = compact ? 54 : Math.max(58, Math.min(92, ((size.height || 480) - TOP_PAD - 12) / Math.max(1, lanes.length)));
  const satHeight = Math.round(laneHeight * 0.66);
  const laneTops = useMemo(() => {
    const tops = new Map<string, { y: number; h: number }>();
    let y = TOP_PAD;
    for (const lane of lanes) {
      const h = lane.kind === 'satellite' || lane.kind === 'agent' && lane.depth > 1 ? satHeight : laneHeight;
      tops.set(lane.id, { y, h });
      y += h;
    }
    return tops;
  }, [lanes, laneHeight, satHeight]);
  const contentHeight = TOP_PAD + [...laneTops.values()].reduce((sum, item) => sum + item.h, 0) + 8;
  const width = Math.max(640, size.width);
  const plotWidth = Math.max(120, width - LANE_HEAD - RIGHT_PAD);
  const X = useCallback((ms: number) => LANE_HEAD + scale.x(ms) * plotWidth, [plotWidth, scale]);
  const Y = useCallback((laneId: string) => {
    const lane = laneTops.get(laneId);
    return lane ? lane.y + lane.h / 2 : TOP_PAD;
  }, [laneTops]);

  // Fresh receipts (live mode) — arrivals after first paint get a burst.
  const seen = useRef<Set<string> | null>(null);
  const [fresh, setFresh] = useState<ReadonlySet<string>>(() => new Set());
  useEffect(() => {
    const ids = new Set([...timeline.handoffs.map((item) => item.id), ...timeline.marks.map((item) => item.id)]);
    const before = seen.current;
    seen.current = ids;
    if (!before || !animate || mode !== 'follow') return;
    const added = [...ids].filter((id) => !before.has(id));
    if (!added.length) return;
    setFresh(new Set(added));
  }, [timeline, animate, mode]);
  useEffect(() => {
    if (!fresh.size) return;
    const timer = window.setTimeout(() => setFresh(new Set()), 1600);
    return () => window.clearTimeout(timer);
  }, [fresh]);

  // Particle bursts on acceptance / final reply / recruitment crossings.
  const particles = useParticles(canvasRef, size, animate);
  const lastAt = useRef(atMs);
  const burstMarks = useRef(new Set<string>());
  useEffect(() => { burstMarks.current.clear(); }, [identity]);
  useEffect(() => {
    const previous = lastAt.current;
    lastAt.current = atMs;
    if (!animate || timeline.failed || timeline.stopping || timeline.stopped) return;
    const crossing = (at: number) => (mode !== 'follow' && previous < at && atMs >= at) || (mode === 'follow' && false);
    for (const mark of timeline.marks) {
      const arrived = fresh.has(mark.id) && !burstMarks.current.has(mark.id);
      if ((mark.kind === 'accept' || mark.kind === 'final' || mark.kind === 'recruit') && (crossing(mark.atMs) || arrived)) {
        particles.burst(X(mark.atMs), Y(mark.laneId), mark.kind === 'recruit' ? 'recruit' : 'celebrate');
      }
      if (mark.kind === 'tool_failed' && (crossing(mark.atMs) || arrived)) particles.burst(X(mark.atMs), Y(mark.laneId) - 14, 'fail');
      if (arrived) burstMarks.current.add(mark.id);
    }
  }, [atMs, fresh]); // eslint-disable-line react-hooks/exhaustive-deps

  const visibleEvents = timeline.events.filter((event) => event.atMs <= atMs + 1);
  useEffect(() => {
    const list = feedRef.current;
    if (list && !selectedEvent) list.scrollTop = list.scrollHeight;
  }, [visibleEvents.length, selectedEvent]);

  const phases = collabPhasesAt(timeline, atMs);
  const focus = collabFocusAt(timeline, atMs);
  const focusMoving = focus && mode !== 'follow' ? Math.min(1, (atMs - focus.sinceMs) / (HANDOFF_MS * 6)) : 1;
  const stateAt = (lane: CollabLane) => (mode === 'follow' ? { state: lane.state, label: lane.status } : collabLaneStateAt(timeline, lane, atMs));
  const counts = mode === 'follow' ? timeline.counts : {
    ...timeline.counts,
    accepted: timeline.marks.filter((mark) => mark.kind === 'accept' && mark.atMs <= atMs).length,
    tools: timeline.marks.filter((mark) => (mark.kind === 'tool' || mark.kind === 'tool_failed') && mark.atMs <= atMs).length,
    failed: timeline.marks.filter((mark) => mark.kind === 'tool_failed' && mark.atMs <= atMs).length,
    running: timeline.lanes.filter((lane) => lane.kind !== 'origin' && ['working', 'thinking', 'reviewing'].includes(collabLaneStateAt(timeline, lane, atMs).state)).length,
  };
  const jumpTo = (ms: number) => { setMode('paused'); setCursor(scale.x(ms)); };
  const togglePlay = () => {
    if (mode === 'replay') setMode('paused');
    else if (animate) { if (fraction >= 0.999) setCursor(0); setMode('replay'); }
  };
  const step = (direction: 1 | -1) => {
    const list = direction > 0 ? timeline.events.filter((event) => event.atMs > atMs + 5) : [...timeline.events].reverse().filter((event) => event.atMs < atMs - 5);
    const next = list[0];
    if (next) { setSelectedEvent(next.id); jumpTo(next.atMs); }
  };
  const onKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest('input,textarea,select,button,a,[contenteditable="true"]')) return;
    if (event.key === ' ') { event.preventDefault(); togglePlay(); }
    if (event.key === 'ArrowRight') { event.preventDefault(); step(1); }
    if (event.key === 'ArrowLeft') { event.preventDefault(); step(-1); }
    if (event.key === 'End') { event.preventDefault(); setMode('follow'); setCursor(1); }
  };

  const doneAll = timeline.final && !timeline.failed && fraction >= 0.999;
  const acceptMark = [...timeline.marks].filter((mark) => (mark.kind === 'accept' || mark.kind === 'final') && mark.atMs <= atMs).sort((a, b) => a.atMs - b.atMs).at(-1);
  const stamped = Boolean(acceptMark && acceptMark.atMs <= atMs);

  return <MotionConfig reducedMotion={animate ? 'never' : 'always'} transition={animate ? undefined : { duration: 0 }}><section
    className={['ctl', className].filter(Boolean).join(' ')}
    data-compact={compact || undefined}
    data-live={timeline.live || undefined}
    data-motion={animate ? 'on' : 'off'}
    data-final={doneAll || undefined}
    aria-label="多 Agent 协作时间线"
    tabIndex={0}
    onKeyDown={onKey}
  >
    {!compact ? <header className="ctl-head">
      <div className="ctl-head__title">
        <span className="ctl-head__scope"><i data-live={timeline.live && animate || undefined} />{timeline.live ? mode === 'follow' ? '实时 · 跟随最新回执' : `回放 · ${clockLabel(atMs, true)}` : timeline.stopping ? '停止中 · 等待排空' : timeline.stopped ? '已停止 · 保留回执' : timeline.failed ? '本轮结束 · 有未完成项' : timeline.final ? '本轮已结束' : timeline.settled ? '子任务已结束 · 不代表主对话已验收' : '当前快照'}</span>
        <strong title={timeline.title}>{timeline.title}</strong>
      </div>
      <dl className="ctl-kpis">
        <Kpi label={timeline.scope === 'session' ? '已返回' : '已验收'} value={`${timeline.scope === 'session' ? counts.returned ?? 0 : counts.accepted}/${counts.total}`} tone="done" />
        <Kpi label="执行中" value={counts.running} tone="active" />
        <Kpi label="工具调用" value={counts.tools} />
        {timeline.counts.satellites ? <Kpi label="卫星" value={timeline.counts.satellites} tone="satellite" /> : null}
        <Kpi label="失败" value={counts.failed} tone={counts.failed ? 'fail' : undefined} />
      </dl>
    </header> : null}

    {!compact ? <ol className="ctl-phases" aria-label="协作阶段">
      <span className="ctl-phases__track" aria-hidden><motion.span className="ctl-phases__fill" initial={false}
        animate={{ scaleX: Math.max(0, phases.findIndex((phase) => phase.state === 'current') === -1 ? phases.every((phase) => phase.state === 'done') ? 1 : 0 : phases.findIndex((phase) => phase.state === 'current') / Math.max(1, phases.length - 1)) }}
        transition={{ type: 'spring', stiffness: 120, damping: 20 }} /></span>
      {phases.map((phase, index) => <li key={phase.key} data-state={phase.state}>
        <motion.span className="ctl-phases__dot" initial={false} animate={phase.state === 'current' && animate ? { scale: [1, 1.12, 1] } : { scale: 1 }} transition={phase.state === 'current' ? { repeat: Infinity, duration: 1.8 } : { duration: 0.2 }}>
          {phase.state === 'done' ? <Check size={12} strokeWidth={3} aria-hidden /> : index + 1}
        </motion.span>
        <span>{phase.label}</span>
      </li>)}
    </ol> : null}

    <div className="ctl-body" data-events={eventsPanel && !compact || undefined}>
      <div className="ctl-stage" ref={stageRef} style={{ '--ctl-head': `${LANE_HEAD}px` } as CSSProperties}>
        <div className="ctl-stage__scroll" style={{ minWidth: width, height: compact ? contentHeight : undefined }}> 
          <div className="ctl-axis" aria-hidden>
            {scale.ticks.map((tick, index) => <span key={index} style={{ left: LANE_HEAD + tick.at * plotWidth }}>{tick.label}</span>)}
          </div>
          <div className="ctl-lanes" style={{ height: contentHeight }}>
            <AnimatePresence initial={false}>
              {lanes.map((lane, index) => {
                const box = laneTops.get(lane.id)!;
                const current = stateAt(lane);
                const joinedLate = lane.joinedAtMs && lane.joinedAtMs > atMs;
                return <motion.div
                  key={lane.id}
                  className="ctl-lane"
                  data-kind={lane.kind}
                  data-state={current.state}
                  data-hot={focus?.laneId === lane.id || undefined}
                  data-hover={hoverLane === lane.id || undefined}
                  data-pending={joinedLate || undefined}
                  style={{ top: box.y, height: box.h, '--ctl-depth': lane.depth } as CSSProperties}
                  initial={animate ? { opacity: 0, x: -24, filter: 'blur(6px)' } : false}
                  animate={{ opacity: joinedLate ? 0.35 : 1, x: 0, filter: 'blur(0px)' }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ type: 'spring', stiffness: 260, damping: 26, delay: animate ? index * 0.04 : 0 }}
                  onPointerEnter={() => setHoverLane(lane.id)}
                  onPointerLeave={() => setHoverLane('')}
                >
                  <button type="button" className="ctl-lane__who" disabled={!onOpenLane || lane.kind === 'origin'} onClick={() => onOpenLane?.(lane)}
                    aria-label={`${lane.label}，${lane.role}，${current.label}${onOpenLane && lane.kind !== 'origin' ? '，打开 Session' : ''}`}>
                    {lane.kind !== 'origin' && lane.depth > 0 ? <span className="ctl-lane__tether" aria-hidden /> : null}
                    <LaneAvatar lane={lane} state={current.state} renderAvatar={renderAvatar} size={lane.kind === 'satellite' || lane.kind === 'agent' && lane.depth > 1 ? 26 : compact ? 30 : 38} />
                    <span className="ctl-lane__text">
                      <strong>{lane.label}</strong>
                      <small><span>{lane.role}</span><AnimatePresence mode="popLayout" initial={false}>
                        <motion.em key={current.label} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.22 }}>{current.label}</motion.em>
                      </AnimatePresence></small>
                      {lane.model && !compact ? <span className="ctl-lane__model" title="本次执行实际使用的模型"><Cpu size={10} aria-hidden />{lane.model}</span> : null}
                    </span>
                  </button>
                </motion.div>;
              })}
            </AnimatePresence>
          </div>

          <svg className="ctl-svg" width={width} height={contentHeight} aria-hidden>
            <defs>
              <filter id={`${uid}-glow`} x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="3.2" /></filter>
              <linearGradient id={`${uid}-comet`} x1="0" x2="1"><stop offset="0" stopColor="currentColor" stopOpacity="0" /><stop offset="1" stopColor="currentColor" stopOpacity="1" /></linearGradient>
              <pattern id={`${uid}-hatch`} width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="3" height="7" fill="currentColor" opacity=".3" /></pattern>
            </defs>
            {scale.breaks.map((item, index) => <g key={index} className="ctl-break" transform={`translate(${LANE_HEAD + item.at * plotWidth},0)`}>
              <line y1={TOP_PAD - 6} y2={contentHeight} /><text y={TOP_PAD - 10} textAnchor="middle">≈ {durationLabel(item.hiddenMs)}</text>
            </g>)}
            {timeline.handoffs.map((handoff) => <HandoffPath key={handoff.id} handoff={handoff} atMs={atMs} X={X} Y={Y} animate={animate} fresh={fresh.has(handoff.id)} replay={mode !== 'follow'} uid={uid} dim={Boolean(hoverLane) && hoverLane !== handoff.fromLaneId && hoverLane !== handoff.toLaneId} />)}
            {timeline.segments.map((segment) => <SegmentBar key={segment.id} segment={segment} atMs={atMs} X={X} y={Y(segment.laneId)} h={(laneTops.get(segment.laneId)?.h ?? laneHeight)} uid={uid} animate={animate} live={mode === 'follow'} />)}
            {timeline.marks.map((mark) => <MarkGlyph key={mark.id} mark={mark} atMs={atMs} x={X(mark.atMs)} y={Y(mark.laneId)} fresh={fresh.has(mark.id)} animate={animate} />)}
            {timeline.live && mode === 'follow' ? <g className="ctl-now" transform={`translate(${X(timeline.endMs)},0)`}>
              <line y1={TOP_PAD - 4} y2={contentHeight} /><circle r="3.5" cy={TOP_PAD - 4} />
            </g> : null}
          </svg>

          {mode !== 'follow' || !timeline.live ? <motion.div className="ctl-playhead" aria-hidden style={{ height: contentHeight - TOP_PAD + 12 }}
            animate={{ x: X(atMs) }} transition={mode === 'replay' ? { duration: 0 } : { type: 'spring', stiffness: 300, damping: 30 }}>
            <span>{clockLabel(atMs, true)}</span>
          </motion.div> : null}

          {focus && timeline.focus && !compact ? <FocusCapsule
            x={Math.min(X(atMs), width - 190)} fromY={Y(focus.fromLaneId)} toY={Y(focus.laneId)} progress={focusMoving}
            label="最近交接" lane={lanes.find((lane) => lane.id === focus.laneId)} done={stamped && doneAll} animate={animate && mode === 'follow'} /> : null}

          <AnimatePresence>{stamped && acceptMark && !compact ? <motion.div key={`stamp:${acceptMark.id}`} className="ctl-stamp" aria-hidden
            style={{ left: Math.min(X(acceptMark.atMs), width - 150), top: Y(acceptMark.laneId) }}
            initial={animate ? { scale: 1.04, rotate: -12, opacity: 0 } : false} animate={{ scale: 1, rotate: -12, opacity: 0.9 }} exit={{ opacity: 0 }}
            transition={{ duration: 0.2, ease: 'easeOut' }}>{acceptMark.kind === 'final' ? '已答复' : '已验收'}</motion.div> : null}</AnimatePresence>

          <canvas className="ctl-fx" ref={canvasRef} aria-hidden />
          {!lanes.some((lane) => lane.kind !== 'origin' && timeline.segments.some((segment) => segment.laneId === lane.id)) ? <div className="ctl-empty">
            <Sparkles size={22} aria-hidden /><strong>还没有分派回执</strong><p>发送需求后，行星协调会分派伙伴；伙伴的执行、交接和卫星会出现在这里。</p>
          </div> : null}
        </div>
      </div>

      {eventsPanel && !compact ? <aside className="ctl-feed" aria-label="协作事件">
        <header><strong>协作事件</strong><small>{visibleEvents.length} / {timeline.events.length}</small></header>
        <ol ref={feedRef}>
          <AnimatePresence initial={false}>
            {visibleEvents.map((event) => <motion.li key={event.id}
              initial={animate ? { opacity: 0, x: 18, filter: 'blur(4px)' } : false} animate={{ opacity: 1, x: 0, filter: 'blur(0px)' }}
              transition={{ type: 'spring', stiffness: 320, damping: 28 }}
              data-tone={event.tone} data-current={(selectedEvent ? selectedEvent === event.id : event === visibleEvents.at(-1)) || undefined}>
              <button type="button" onClick={() => { setSelectedEvent(event.id); jumpTo(event.atMs); }} onPointerEnter={() => setHoverLane(event.laneId)} onPointerLeave={() => setHoverLane('')}>
                <span className="ctl-feed__icon" aria-hidden><EventIcon event={event} /></span>
                <span className="ctl-feed__text"><strong>{event.actor}</strong> {event.text}<small>{clockLabel(event.atMs, true)}</small></span>
              </button>
            </motion.li>)}
          </AnimatePresence>
          {!visibleEvents.length ? <li className="ctl-feed__empty">回放开始后，事件会按真实顺序出现。</li> : null}
        </ol>
      </aside> : null}
    </div>

    {!compact ? <footer className="ctl-controls">
      <button type="button" className="ctl-controls__play" disabled={!animate} title={!animate ? '动态效果已关闭，可用进度条或逐条查看' : undefined} onClick={togglePlay} aria-label={mode === 'replay' ? '暂停回放' : fraction >= 0.999 ? '从头回放' : '播放回放'}>
        {mode === 'replay' ? <Pause size={15} aria-hidden /> : fraction >= 0.999 ? <RotateCcw size={15} aria-hidden /> : <Play size={15} aria-hidden />}
      </button>
      <button type="button" className="ctl-controls__icon" onClick={() => step(-1)} aria-label="上一条事件"><SkipBack size={14} aria-hidden /></button>
      <button type="button" className="ctl-controls__icon" onClick={() => step(1)} aria-label="下一条事件"><SkipForward size={14} aria-hidden /></button>
      <input type="range" min={0} max={1000} value={Math.round(fraction * 1000)} aria-label="回放进度" style={{ '--ctl-progress': `${fraction * 100}%` } as CSSProperties}
        onChange={(event) => { setMode('paused'); setCursor(Number(event.target.value) / 1000); }} />
      <button type="button" className="ctl-controls__speed" onClick={() => setSpeed((value) => (value === 1 ? 2 : value === 2 ? 4 : value === 4 ? 0.5 : 1))} aria-label={`回放速度 ${speed} 倍`}><FastForward size={12} aria-hidden />{speed}×</button>
      {timeline.live ? <button type="button" className="ctl-controls__live" data-on={mode === 'follow' || undefined} onClick={() => { setMode('follow'); setCursor(1); }}><i />实时</button> : null}
      <span className="ctl-legend" aria-hidden>
        <span data-k="plan">分工/汇总</span><span data-k="execute">执行</span><span data-k="review">复核</span><span data-k="wait">等待</span>{timeline.counts.satellites ? <span data-k="satellite">卫星</span> : null}
      </span>
    </footer> : null}
  </section></MotionConfig>;
}

function Kpi({ label, value, tone }: { label: string; value: number | string; tone?: string }) {
  return <div data-tone={tone}><dt>{label}</dt><dd><AnimatePresence mode="popLayout" initial={false}>
    <motion.span key={String(value)} initial={{ y: 10, opacity: 0 }} animate={{ y: 0, opacity: 1 }} exit={{ y: -10, opacity: 0 }} transition={{ type: 'spring', stiffness: 400, damping: 30 }}>{value}</motion.span>
  </AnimatePresence></dd></div>;
}

function LaneAvatar({ lane, state, renderAvatar, size }: { lane: CollabLane; state: CollabLaneState; renderAvatar?: CollabAvatarRenderer; size: number }) {
  return <span className="ctl-avatar" data-state={state} data-kind={lane.kind} style={{ width: size, height: size }}>
    {lane.kind === 'origin' ? <span className="ctl-avatar__sun" /> : renderAvatar && lane.kind === 'partner' ? renderAvatar(lane, size, state)
      : lane.kind === 'satellite' || lane.kind === 'agent' ? <span className="ctl-avatar__sat"><Satellite size={Math.round(size * 0.55)} aria-hidden /></span>
        : <span className="ctl-avatar__dot">{lane.label.slice(0, 1)}</span>}
    <span className="ctl-avatar__ring" aria-hidden />
    {lane.kind === 'satellite' || lane.kind === 'agent' ? <span className="ctl-avatar__orbit" aria-hidden><i /></span> : null}
    <span className="ctl-avatar__badge" aria-hidden>{state === 'done' ? <Check size={9} strokeWidth={3.4} /> : state === 'error' ? '!' : null}</span>
  </span>;
}

function SegmentBar({ segment, atMs, X, y, h, uid, animate, live }: { segment: CollabSegment; atMs: number; X: (ms: number) => number; y: number; h: number; uid: string; animate: boolean; live: boolean }) {
  if (segment.startMs > atMs) return null;
  const x1 = X(segment.startMs);
  const end = Math.min(segment.endMs, atMs);
  const x2 = Math.max(x1 + 3, X(end));
  const growing = segment.open ? live : end < segment.endMs;
  if (segment.kind === 'origin') {
    return <rect className="ctl-seg" data-kind="origin" x={x1} y={y - 1.5} width={x2 - x1} height={3} rx={1.5} />;
  }
  if (segment.kind === 'wait') {
    return <g className="ctl-seg" data-kind="wait">
      <line x1={x1} x2={x2} y1={y} y2={y} className="ctl-seg__dash" data-flow={animate || undefined} />
      <text x={x1 + 8} y={y - 7}>{x2 - x1 > 110 ? segment.label : ''}</text>
    </g>;
  }
  const barH = Math.max(10, Math.min(18, h * 0.26));
  return <g className="ctl-seg" data-kind={segment.kind} data-failed={segment.failed || undefined} data-open={growing || undefined}>
    <rect x={x1} y={y - barH / 2} width={x2 - x1} height={barH} rx={barH / 2} className="ctl-seg__bar" />
    {segment.kind === 'review' ? <rect x={x1} y={y - barH / 2} width={x2 - x1} height={barH} rx={barH / 2} fill={`url(#${uid}-hatch)`} className="ctl-seg__hatch" /> : null}
    {growing && animate ? <rect x={x1} y={y - barH / 2} width={x2 - x1} height={barH} rx={barH / 2} className="ctl-seg__shimmer" /> : null}
    <title>{segment.label}</title>
    {x2 - x1 > 100 ? <text x={x1 + 10} y={y + 4} className="ctl-seg__label">{segment.label.length > Math.floor((x2 - x1 - 20) / 13) ? `${segment.label.slice(0, Math.max(1, Math.floor((x2 - x1 - 20) / 13) - 1))}…` : segment.label}</text> : null}
    {growing ? <>
      <circle cx={x2} cy={y} r={barH * 0.7} className="ctl-seg__head-glow" filter={`url(#${uid}-glow)`} />
      <circle cx={x2} cy={y} r={3.2} className="ctl-seg__head" />
    </> : null}
    {segment.failed && end >= segment.endMs ? <g transform={`translate(${x2},${y})`} className="ctl-seg__fail"><circle r="6" /><path d="M-2.4 -2.4 L2.4 2.4 M2.4 -2.4 L-2.4 2.4" /></g> : null}
  </g>;
}

function HandoffPath({ handoff, atMs, X, Y, animate, fresh, replay, uid, dim }: {
  handoff: CollabHandoff; atMs: number; X: (ms: number) => number; Y: (id: string) => number; animate: boolean; fresh: boolean; replay: boolean; uid: string; dim: boolean;
}) {
  if (handoff.atMs > atMs || handoff.fromLaneId === handoff.toLaneId) return null;
  const x = X(handoff.atMs);
  const y1 = Y(handoff.fromLaneId);
  const y2 = Y(handoff.toLaneId);
  const bend = Math.min(44, Math.abs(y2 - y1) * 0.35 + 14);
  const d = `M${x},${y1} C${x + bend},${y1} ${x + bend},${y2} ${x + 2},${y2}`;
  const progress = replay ? Math.min(1, (atMs - handoff.atMs) / HANDOFF_MS) : 1;
  const travelling = animate && ((replay && progress < 1) || fresh);
  const length = Math.abs(y2 - y1) * 1.25 + bend;
  // Replay has one clock: the cursor. A second SMIL clock retargeted on
  // every frame jumps ahead, and even keeps moving after the user pauses.
  const inverse = 1 - progress;
  const packetX = x + 3 * inverse * progress * bend + 2 * progress ** 3;
  const packetY = y1 + (y2 - y1) * (3 * progress ** 2 - 2 * progress ** 3);
  return <g className="ctl-handoff" data-kind={handoff.kind} data-failed={handoff.failed || undefined} data-pending={handoff.pending || undefined} data-dim={dim || undefined}>
    <path d={d} className="ctl-handoff__line" strokeDasharray={length} strokeDashoffset={length * (1 - progress)} markerEnd="" />
    {progress >= 1 ? <circle cx={x + 2} cy={y2} r={2.6} className="ctl-handoff__end" /> : null}
    {travelling ? <g>
      <circle cx={replay ? packetX : 0} cy={replay ? packetY : 0} r={4.6} className="ctl-handoff__packet" filter={`url(#${uid}-glow)`} />
      <circle cx={replay ? packetX : 0} cy={replay ? packetY : 0} r={3} className="ctl-handoff__packet-core" />
      {!replay ? <animateMotion dur="0.9s" path={d} fill="freeze" keyPoints="0;1" keyTimes="0;1" calcMode="spline" keySplines=".22 .8 .3 1" /> : null}
    </g> : null}
    {Math.abs(y2 - y1) > 30 && !['submit', 'result'].includes(handoff.kind) ? <text x={x + bend * 0.55 + 6} y={y1 + Math.sign(y2 - y1) * Math.min(Math.abs(y2 - y1) / 2, 30) + 3} className="ctl-handoff__label" style={{ opacity: Math.min(1, progress * 2) }}>{handoff.label}</text> : null}
  </g>;
}

function MarkGlyph({ mark, atMs, x, y, fresh, animate }: { mark: CollabMark; atMs: number; x: number; y: number; fresh: boolean; animate: boolean }) {
  if (mark.atMs > atMs) return null;
  const age = atMs - mark.atMs;
  const pop = animate && (fresh || age < 600);
  if (mark.kind === 'model') {
    return <g className="ctl-mark" data-kind="model" transform={`translate(${x},${y + 16})`}><title>{mark.label}</title><rect x={-3} y={-3} width={6} height={6} rx={1.5} transform="rotate(45)" /></g>;
  }
  const glyph = mark.kind === 'tool' ? 'M-2.4 0 L-0.6 1.9 L2.6 -1.8' : mark.kind === 'tool_failed' ? 'M0 -2.8 L0 0.8 M0 2.6 L0 2.8' : mark.kind === 'recruit' ? 'M0 -2.6 L0 2.6 M-2.6 0 L2.6 0' : mark.kind === 'return' ? 'M2.4 -1.6 A2.6 2.6 0 1 0 2.2 1.8 M2.4 -3 L2.4 -1.2 L0.6 -1.2' : 'M-2.6 0 L-0.8 2 L2.8 -2';
  const lift = mark.kind === 'tool' || mark.kind === 'tool_failed' ? -18 : mark.kind === 'accept' || mark.kind === 'final' ? 0 : 0;
  return <g className="ctl-mark" data-kind={mark.kind} data-pop={pop || undefined} data-shake={mark.kind === 'tool_failed' && pop || undefined} transform={`translate(${x},${y + lift})`}>
    <title>{mark.label}</title>
    {lift ? <line y1={5} y2={-lift - 7} className="ctl-mark__stem" /> : null}
    {pop ? <circle r={11} className="ctl-mark__ripple" /> : null}
    <circle r={mark.kind === 'accept' || mark.kind === 'final' ? 8 : 6} className="ctl-mark__dot" />
    <path d={glyph} className="ctl-mark__glyph" />
  </g>;
}

function FocusCapsule({ x, fromY, toY, progress, label, lane, done, animate }: { x: number; fromY: number; toY: number; progress: number; label: string; lane?: CollabLane; done: boolean; animate: boolean }) {
  const eased = 1 - Math.pow(1 - Math.min(1, progress), 3);
  const y = fromY + (toY - fromY) * eased;
  const hop = Math.sin(Math.min(1, progress) * Math.PI) * 14;
  return <motion.div className="ctl-capsule" data-done={done || undefined} data-moving={progress < 1 || undefined} aria-hidden
    animate={{ x: x + 16, y: y - 30 - hop }} transition={animate ? { type: 'spring', stiffness: 220, damping: 22, mass: 0.7 } : { duration: 0 }}>
    <span className="ctl-capsule__who">{lane?.label ?? ''}</span><span>{label}</span>
  </motion.div>;
}

function EventIcon({ event }: { event: CollabEvent }) {
  if (event.tone === 'fail') return <CircleAlert size={13} />;
  if (event.tone === 'done') return <Check size={13} strokeWidth={3} />;
  if (event.tone === 'satellite') return <Satellite size={13} />;
  if (event.text.startsWith('招募')) return <UserPlus size={13} />;
  if (event.tone === 'origin') return <Sparkles size={13} />;
  return <span className="ctl-feed__pip" />;
}

/* ---------------- Peek card for the task rail / Agent list ---------------- */

export function CollabTimelinePeek({ timeline, renderAvatar, onExpand, active = true, title = '协作全景' }: {
  timeline: CollabTimeline; renderAvatar?: CollabAvatarRenderer; onExpand?: () => void; active?: boolean; title?: string;
}) {
  const animate = usePresentationMotion(active);
  const reduced = !animate;
  const scale = useMemo(() => collabTimeScale(timeline), [timeline]);
  const people = timeline.lanes.filter((lane) => lane.kind === 'partner' || lane.kind === 'agent' && lane.depth <= 1);
  const rows = people.slice(0, 5);
  const busy = people.filter((lane) => ['working', 'thinking', 'reviewing'].includes(lane.state));
  const waiting = people.filter((lane) => lane.state === 'waiting');
  const sentence = timeline.stopping ? '停止中 · 等待执行排空' : timeline.failed ? '本轮已结束 · 有未完成项'
    : timeline.scope === 'session' && timeline.settled ? `子任务已结束 · 返回 ${timeline.counts.returned ?? 0}/${timeline.counts.total} · 验收以主对话为准`
    : timeline.final ? `本轮已结束 · 已验收 ${timeline.counts.accepted}/${timeline.counts.total}`
    : timeline.stopped ? '已停止 · 回执已保留'
      : busy.length ? `${busy.map((lane) => `${lane.label} ${lane.status}`).slice(0, 2).join(' · ')}${waiting.length ? ` · ${waiting[0]!.label} ${waiting[0]!.status}` : ''}`
        : waiting.length ? `${waiting[0]!.label} ${waiting[0]!.status}` : '谁在执行，谁在等待，结果如何交接';
  return <button type="button" className="ctl-peek" onClick={onExpand} disabled={!onExpand} aria-label={`打开${title}`} data-live={timeline.live && active && !reduced || undefined}>
    <span className="ctl-peek__top">
      <span className="ctl-peek__people">{rows.map((lane, index) => <motion.span key={lane.id} initial={reduced ? false : { y: 8, opacity: 0 }} animate={{ y: 0, opacity: 1 }} transition={{ delay: index * 0.05, type: 'spring', stiffness: 300, damping: 22 }}>
        <LaneAvatar lane={lane} state={lane.state} renderAvatar={renderAvatar} size={28} />
      </motion.span>)}{people.length > 5 ? <small>+{people.length - 5}</small> : null}</span>
      <span className="ctl-peek__expand" aria-hidden><svg viewBox="0 0 24 24" width="15" height="15"><path d="M9 3H3v6M15 3h6v6M3 15v6h6M21 15v6h-6" /></svg></span>
    </span>
    <strong>{title}<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden><path d="M5 12h14M14 7l5 5-5 5" /></svg></strong>
    <AnimatePresence mode="wait" initial={false}><motion.span key={sentence} className="ctl-peek__copy" initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }} transition={{ duration: 0.2 }}>{sentence}</motion.span></AnimatePresence>
    <svg className="ctl-peek__mini" viewBox={`0 0 100 ${rows.length * 9 + 2}`} preserveAspectRatio="none" aria-hidden>
      {rows.map((lane, row) => <g key={lane.id}>
        <line x1="0" x2="100" y1={row * 9 + 5} y2={row * 9 + 5} className="ctl-peek__track" />
        {timeline.segments.filter((segment) => segment.laneId === lane.id || timeline.lanes.find((item) => item.id === segment.laneId)?.parentId === lane.id).map((segment) => <line key={segment.id}
          x1={scale.x(segment.startMs) * 100} x2={Math.max(scale.x(segment.startMs) * 100 + 1.2, scale.x(segment.endMs) * 100)} y1={row * 9 + 5} y2={row * 9 + 5}
          className="ctl-peek__seg" data-kind={segment.kind} data-open={segment.open || undefined} />)}
      </g>)}
      {timeline.live && active && !reduced ? <line x1="100" x2="100" y1="0" y2={rows.length * 9 + 2} className="ctl-peek__now" /> : null}
    </svg>
  </button>;
}

/* ---------------- Dialog wrapper with shared-element morph ---------------- */

export function CollabTimelineOverlay({ open, onClose, children, title }: { open: boolean; onClose: () => void; children: ReactNode; title: string }) {
  return <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
    <DialogContent className="ctl-overlay ctl-overlay__panel">
      <DialogHeader className="ctl-overlay__heading">
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>按公开回执查看分工、交接与结果；回放不会重新执行任务。</DialogDescription>
      </DialogHeader>
      {children}
    </DialogContent>
  </Dialog>;
}

/* ---------------- hooks ---------------- */

function useElementSize(ref: React.RefObject<HTMLElement | null>) {
  const [size, setSize] = useState({ width: 0, height: 0 });
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const measure = () => setSize((current) => (current.width === node.clientWidth && current.height === node.clientHeight ? current : { width: node.clientWidth, height: node.clientHeight }));
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [ref]);
  return size;
}

function useAnimationFrame(enabled: boolean, callback: (dt: number) => void) {
  const saved = useRef(callback);
  saved.current = callback;
  useEffect(() => {
    if (!enabled || typeof requestAnimationFrame !== 'function') return;
    let frame = 0;
    let last = performance.now();
    const loop = (now: number) => {
      const dt = Math.min(64, now - last);
      last = now;
      saved.current(dt);
      frame = requestAnimationFrame(loop);
    };
    frame = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(frame);
  }, [enabled]);
}

type Particle = { x: number; y: number; vx: number; vy: number; life: number; size: number; color: string; spin: number; shape: 0 | 1 };
function useParticles(ref: React.RefObject<HTMLCanvasElement | null>, size: { width: number; height: number }, enabled: boolean) {
  const particles = useRef<Particle[]>([]);
  const frame = useRef(0);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, size.width * ratio);
    canvas.height = Math.max(1, size.height * ratio);
    canvas.style.width = `${size.width}px`;
    canvas.style.height = `${size.height}px`;
  }, [ref, size]);
  useEffect(() => {
    if (!enabled) { cancelAnimationFrame(frame.current); particles.current = []; }
    return () => cancelAnimationFrame(frame.current);
  }, [enabled]);
  const run = () => {
    const canvas = ref.current;
    const context = canvas?.getContext?.('2d');
    if (!canvas || !context) return;
    const ratio = window.devicePixelRatio || 1;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, canvas.width, canvas.height);
    particles.current = particles.current.filter((particle) => particle.life > 0);
    for (const particle of particles.current) {
      particle.x += particle.vx; particle.y += particle.vy; particle.vy += 0.12; particle.vx *= 0.985; particle.life -= 0.016; particle.spin += 0.18;
      context.save();
      context.globalAlpha = Math.max(0, particle.life);
      context.translate(particle.x, particle.y);
      context.rotate(particle.spin);
      context.fillStyle = particle.color;
      if (particle.shape) context.fillRect(-particle.size, -particle.size / 2.5, particle.size * 2, particle.size / 1.25);
      else { context.beginPath(); context.arc(0, 0, particle.size / 1.6, 0, Math.PI * 2); context.fill(); }
      context.restore();
    }
    if (particles.current.length) frame.current = requestAnimationFrame(run);
  };
  return {
    burst(x: number, y: number, kind: 'celebrate' | 'recruit' | 'fail') {
      if (!enabled || typeof CanvasRenderingContext2D === 'undefined' || !ref.current?.getContext?.('2d')) return;
      const palette = kind === 'fail' ? ['#c2574c', '#e08b7e'] : kind === 'recruit' ? ['#e9a640', '#f5d28a', '#4f7f95'] : ['#4f8a72', '#4f7f95', '#8a78b8', '#e9a640', '#6fb39a'];
      const count = kind === 'celebrate' ? 64 : kind === 'recruit' ? 28 : 14;
      for (let index = 0; index < count; index += 1) {
        const angle = kind === 'fail' ? -Math.PI / 2 + (Math.random() - 0.5) * 1.6 : Math.random() * Math.PI * 2;
        const velocity = (kind === 'celebrate' ? 2.2 : 1.4) + Math.random() * (kind === 'celebrate' ? 4.6 : 2.4);
        particles.current.push({ x, y, vx: Math.cos(angle) * velocity, vy: Math.sin(angle) * velocity - (kind === 'celebrate' ? 2.2 : 0.6), life: 1, size: 1.8 + Math.random() * 2.6, color: palette[index % palette.length]!, spin: Math.random() * 6, shape: index % 3 === 0 ? 0 : 1 });
      }
      cancelAnimationFrame(frame.current);
      frame.current = requestAnimationFrame(run);
    },
  };
}
