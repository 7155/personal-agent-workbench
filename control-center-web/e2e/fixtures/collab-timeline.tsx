import { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { CollabTimelineOverlay, CollabTimelinePeek, CollabTimelineStage } from '../../src/features/collab-timeline/CollabTimelineStage';
import { roomPlanetAvatarRenderer } from '../../src/features/collab-timeline/RoomCollabTimeline';
import { buildRoomCollabTimeline } from '../../src/features/collab-timeline/room-timeline';
import { buildSessionCollabTimeline } from '../../src/features/collab-timeline/session-timeline';
import { collabDemoRoom, subagentRun } from '../../src/features/collab-timeline/fixtures';
import '../../src/design/tokens.css';
import '../../src/components/primitives/primitives.css';
import './collab-timeline.css';

/** Synthetic receipts with real production components. No model or mutations. */
function Demo() {
  const query = new URLSearchParams(location.search);
  const [cut, setCut] = useState(Number(query.get('cut') ?? 80));
  const [theme, setTheme] = useState(query.get('theme') === 'dark' ? 'dark' : 'light');
  const [open, setOpen] = useState(false);
  const [selection, setSelection] = useState('');
  const view = query.get('view') ?? 'room';
  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);
  const room = useMemo(() => {
    const demo = collabDemoRoom({ cut });
    return buildRoomCollabTimeline({ room: demo.room, projection: demo.projection, satellites: demo.satellites, nowMs: demo.nowMs });
  }, [cut]);
  const session = useMemo(() => {
    const base = Date.UTC(2026, 8, 29, 9);
    return buildSessionCollabTimeline({ sessionId: 's', title: '重构 Room 投影', nowMs: base + 50_000, runs: [
      subagentRun({ id: 'r1', parent: 's', task: '规划拆分步骤', template: 'planner', createdAtMs: base, completedAtMs: base + 12_000 }),
      subagentRun({ id: 'r2', parent: 's', task: '实现投影函数', template: 'worker', parentRunId: 'r1', depth: 2, createdAtMs: base + 6_000, completedAtMs: base + 16_000, failed: true }),
      subagentRun({ id: 'r2b', nodeId: 'r2', attempt: 2, parent: 's', task: '实现投影函数', template: 'worker', parentRunId: 'r1', depth: 2, createdAtMs: base + 17_000, completedAtMs: base + 34_000, tools: 9 }),
      subagentRun({ id: 'r3', parent: 's', task: '检索既有测试', template: 'researcher', createdAtMs: base + 3_000, completedAtMs: base + 22_000 }),
      subagentRun({ id: 'r4', parent: 's', task: '审阅改动', template: 'reviewer', createdAtMs: base + 36_000, completedAtMs: null }),
    ] });
  }, []);
  const timeline = view === 'session' ? session : room;
  const title = view === 'session' ? '卫星协作时间线' : '协作全景';
  return <main className="collab-demo">
    <header className="collab-demo-header"><h1>{view === 'session' ? 'Agent · 子任务协作' : 'Room · 行星协作'}</h1>
      <span>合成回执 · 不连接模型</span>
      <button type="button" onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}>切换明暗主题</button>
    </header>
    <div className="collab-demo-layout">
      <div className="collab-demo-stage"><CollabTimelineStage timeline={timeline} active={!open} renderAvatar={roomPlanetAvatarRenderer(!open)} onOpenLane={lane => setSelection(lane.label)} /></div>
      <aside className="collab-demo-aside">
        <CollabTimelinePeek timeline={timeline} active={!open} renderAvatar={roomPlanetAvatarRenderer(false)} onExpand={() => setOpen(true)} title={title} />
        {view === 'room' ? <label>快照截至 {cut} 秒<input aria-label="合成快照时间" type="range" min={0} max={80} value={cut} onChange={event => setCut(Number(event.target.value))} /></label> : null}
        <p role="status">{selection ? `已选择 ${selection}` : '可打开全景，使用键盘回放并选择伙伴。'}</p>
      </aside>
    </div>
    <CollabTimelineOverlay open={open} onClose={() => setOpen(false)} title={title}>
      <CollabTimelineStage timeline={timeline} active={open} renderAvatar={roomPlanetAvatarRenderer(open)} onOpenLane={lane => { setSelection(lane.label); setOpen(false); }} />
    </CollabTimelineOverlay>
  </main>;
}
createRoot(document.getElementById('root')!).render(<Demo />);
