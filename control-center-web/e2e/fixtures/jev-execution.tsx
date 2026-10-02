import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ControlTransportProvider } from '../../src/app/control-transport';
import { createPreviewTransport } from '../../src/app/preview-control-transport';
import { previewRoomSnapshot } from '../../src/app/preview-room-data';
import { TooltipProvider } from '../../src/components/primitives';
import type { ControlRequest } from '../../src/platform/transport';
import type { RoomSummary } from '../../src/features/rooms/room-types';
import type { AgentActivityProjection } from '../../src/contracts/agent-reducer';
import { PawRoomWorkspace } from '../../src/paw-os/apps/PawRoomWorkspace';
import { PawRoomRemovalProgress, type ParticipantRemoval } from '../../src/paw-os/apps/PawRoomRemovalProgress';
import { PawJevToolRecordDialog } from '../../src/paw-os/apps/PawJevToolRecords';
import { ToolCard } from '../../src/features/conversation-ui/components/ToolCard';
import { FxActivityStack } from '../../src/features/agent/timeline/ActivitySummary';
import { RoomPlanetAvatar, type RoomPlanetActivity } from '../../src/features/rooms/RoomPlanetAvatar';
import { ROOM_PLANET_NAMES } from '../../src/features/rooms/room-copy';
import '../../src/design/tokens.css';
import '../../src/design/typography.css';
import '../../src/components/primitives/primitives.css';
import '../../src/features/conversation-ui/conversation-ui.css';
import '../../src/paw-os/styles/paw-os.css';
import '../../src/paw-os/styles/paw-os-motion.css';
import '../../src/paw-os/apps/paw-apps.css';
import '../../src/paw-os/styles/paw-os-room.css';
import '../../src/paw-os/styles/paw-os-agent.css';
import '../../src/paw-os/styles/paw-os-agent-fx.css';
import '../../src/paw-os/styles/paw-os-shell.css';
import '../../src/paw-os/styles/paw-os-controls.css';
import '../../src/paw-os/styles/paw-os-stellar.css';
import '../../src/paw-os/styles/paw-os-stellar-dark.css';
import './jev-execution.css';

const scenes = { removal: '伙伴移出与停止回执', revision: '运行中任务修改', assignment: '任务回收与改派', clarify: 'Grill 待补充', approval: '整份方案待确认', plan: '规划运行', longplan: '长目标规划', single: '单人执行', parallel: '两项并行', review: '独立复核', returned: '退回修改', abstained: '调度暂未选出下一步', synthesize: '汇总结果', final: '最终答复', failed: '执行未完成', tools: 'Session / Room 工具回执', avatars: '八星球头像尺寸' };
type Scene = keyof typeof scenes;
let scene = (new URLSearchParams(location.search).get('scene') || 'parallel') as Scene;
if (!(scene in scenes)) scene = 'parallel';
const roomId = 'room-jev-ui-fixture';
const transport = createPreviewTransport();
const original = transport.request.bind(transport);
let stopped = false;
let planState = scene === 'clarify' ? 'awaiting_input' : 'awaiting_approval';
let lastActionId = '';
let reclaimTarget = '';
let revisionRequested = false;
transport.request = async <Response,>(request: ControlRequest): Promise<Response> => {
  if (request.pathId === 'agent.room.get') return { ok: true, room: fixtureRoomSnapshot(String(request.params?.roomId)).room } as Response;
  if (request.pathId === 'agent.room.snapshot') return fixtureRoomSnapshot(String(request.params?.roomId)) as Response;
  if (request.pathId === 'agent.jev.get') return (request.query?.graphId ? snapshot(String(request.params?.roomId)) : { ok: true, mode: 'jev', items: [{ graph_id: 'jev-fixture', root_turn_id: `${request.params?.roomId}:turn-1`, room_id: request.params?.roomId, objective: '为文件预览增加可靠的恢复能力', phase: snapshot(String(request.params?.roomId)).phase, stopped }] }) as Response;
  if (request.pathId === 'agent.jev.command') {
    const body = request.body as Record<string, unknown>;
    if (scene === 'revision' && body.action === 'revision_options') return { ok: true, graphId: 'jev-fixture', rootId: `${request.params?.roomId}:turn-1`, taskId: body.taskId, taskHash: 'fixture-revision-v1', expectedTopologyRevision: 1, expectedRequirementsRevision: 1, affectedTaskIds: ['api', 'integration'], downstreamTaskIds: ['integration'], retainedAcceptedTaskIds: ['ui'], available: true, unavailableReason: '' } as Response;
    if (scene === 'revision' && body.action === 'revise_task') { revisionRequested = true; return { ok: true, graphId: 'jev-fixture', revisionId: 'fixture-revision', changedTaskId: body.taskId, status: 'awaiting_drain' } as Response; }

    if (scene === 'assignment' && body.action === 'assignment_options') return { ok: true, graphId: 'jev-fixture', taskId: 'api', taskHash: 'fixture-assignment-v1', ownerId: 'participant-firstlight', action: 'request_reclaim', targetParticipantIds: ['participant-present', 'participant-future'], unavailableReason: '' } as Response;
    if (scene === 'assignment' && body.action === 'request_reclaim') { reclaimTarget = String(body.targetParticipantId); return { status: 'requested', reclaimId: body.clientMessageId } as Response; }

    if (body.action === 'stop') stopped = true;
    if (body.action === 'approve_plan') planState = 'approved';
    if (body.action === 'adjust_plan') planState = 'planning';
    if (body.action === 'defer_plan') planState = 'deferred';
    lastActionId = String(body.clientMessageId || '');
    return { ok: true, accepted: true, graphId: 'jev-fixture', rootId: `${request.params?.roomId}:turn-1` } as Response;
  }
  if (request.pathId === 'agent.runtime.ensure') return { ok: true } as Response;
  return original<Response>(request);
};
function task(id: string, objective: string, owner: string, state: string, parent = 'root') { return { id, objective, owner_id: owner, state, parent_id: parent, revision: 1, acceptance: ['通过针对性行为测试', '说明尚未验证的边界'], result: '', evidence: [], artifacts: [] }; }
function effect(id: string, taskId: string, purpose: string, executionStatus: string, ownerId: string) {
  const call = ({ a: 'api-fixture', b: 'ui-fixture', plan: 'planning-read-fixture', verify: 'verify-fixture', synth: 'synth-fixture' } as Record<string, string>)[id] || id;
  return { effectId: id, operation: 'dispatch', state: 'accepted', executionStatus, request: { taskId, taskRevision: 1, purpose, ownerId, dispatchId: `dispatch-${call}`, sessionId: ownerId.replace('participant-', 'session-room-') }, receipt: { turnId: `pi-${id}` } };
}
function snapshot(currentRoomId: string) {
  const hasApproval = scene === 'approval' || scene === 'clarify';
  const planning = scene === 'plan' || scene === 'longplan' || hasApproval && planState === 'planning';
  const parallel = scene === 'parallel' || hasApproval && planState === 'approved';
  const root = task('root', '为文件预览增加可靠的恢复能力', 'participant-present', 'queued', '');
  if (scene === 'longplan') root.objective = '合成长度边界，不是真实任务。' + '请检查文件恢复、附件读取、草稿保留、重复打开以及断线重连后的回执一致性。每项结果必须包含验证依据和未验证边界，并保留当前模型、工具权限与记忆设置。'.repeat(8);
  const a = task('api', '恢复后重新核实文件读取回执', 'participant-firstlight', parallel || scene === 'single' || scene === 'assignment' || scene === 'revision' ? 'active' : 'done');
  const assignment = scene === 'assignment' ? { taskHash: 'fixture-assignment-v1', accepted_turn_id: 'a' } : {};
  const b = { ...task('ui', '保留阅读位置与未提交内容', 'participant-future', parallel ? 'active' : scene === 'review' ? 'review' : scene === 'returned' ? 'queued' : scene === 'failed' ? 'failed' : 'done'), revision: scene === 'returned' ? 2 : 1 };
  const result = scene === 'review' ? { ...b, result: '已提交恢复界面与针对性回归。等待独立伙伴核对。' } : scene === 'final' ? { ...b, artifacts: ['delivery/synthetic-reading-report.md'], result: '合成成果记录，仅验证原 Room 的来源跳转，不读取真实文件。' } : b;
  const phase = planning ? 'plan' : hasApproval && planState !== 'approved' ? planState : scene === 'synthesize' ? 'synthesize' : scene === 'final' || scene === 'failed' ? 'final' : 'execute';
  const effects = planning ? [effect('plan', 'root', 'plan', 'running', 'participant-present')]
    : parallel ? [effect('a', 'api', 'execute', 'running', 'participant-firstlight'), effect('b', 'ui', 'execute', 'running', 'participant-future')]
    : scene === 'single' || scene === 'assignment' || scene === 'revision' ? [effect('a', 'api', 'execute', 'running', 'participant-firstlight')]
    : scene === 'review' ? [effect('verify', 'ui', 'verify', 'running', 'participant-present')]
    : scene === 'synthesize' ? [effect('synth', 'root', 'synthesize', 'running', 'participant-present')] : [];
  return { ok: true, mode: 'jev', graphId: 'jev-fixture', rootId: `${currentRoomId}:turn-1`, snapshotVersion: `${scene}:${stopped}:${planState}`, phase, stopped, requirementsRevision: scene === 'returned' ? 2 : 1,
    ...(scene === 'revision' ? { activeTaskIds: ['root', 'api', 'ui', 'integration'], revisions: revisionRequested ? [{ revisionId: 'fixture-revision', status: 'awaiting_drain', changedTaskId: 'api', affectedTaskIds: ['api', 'integration'], retainedAcceptedTaskIds: ['ui'], successorTaskIds: [] }] : [] } : {}),
    tasks: planning || hasApproval && planState !== 'approved' ? [root] : [root, { ...a, ...assignment, expected_output: '最新文件回执' }, result, ...(scene === 'revision' ? [task('integration', '集成并验证恢复行为', 'participant-present', 'queued')] : [])], reclaims: reclaimTarget ? [{ reclaimId: 'fixture-reclaim', taskId: 'api', taskRevision: 1, dispatchId: 'a', targetParticipantId: reclaimTarget, stage: 'awaiting_stop' }] : [], edges: scene === 'revision' ? [{ prerequisite: 'api', dependent: 'integration', kind: 'requires' }] : scene === 'review' || scene === 'returned' ? [{ prerequisite: 'api', dependent: 'ui', kind: 'context' }] : [],
    running: parallel && !stopped ? ['api', 'ui'] : (scene === 'single' || scene === 'assignment' || scene === 'revision') && !stopped ? ['api'] : [], review: scene === 'review' ? ['ui'] : [], ready: [], blocked: [], effects: stopped ? [] : effects,
    events: scene === 'returned' ? [{ source_id: 'return-1', kind: 'work_reviewed', state: 'done', result_json: JSON.stringify({ status: 'applied', receipt: { application: { operation: 'return', status: 'applied', task: b } } }) }] : scene === 'abstained' ? [{ source_id: 'abstain-1', kind: 'work_reviewed', state: 'done', result_json: JSON.stringify({ status: 'abstained', receipt: { decision: { answer: { choice: 'insufficient_evidence' } } } }) }] : [],
    final: scene === 'final' ? { status: 'completed', content: '已完成两项改动，并核对了断线恢复与重复打开的行为。\n\n- 文件读取使用当前回执，不把旧状态当成已完成。\n- 阅读位置与未提交内容会保留。\n\n这是合成 UI 验证内容，不是实测交付。' } : scene === 'failed' ? { status: 'failed', content: '阅读恢复仍有未通过的场景，尚未完成验收。（合成回执）' } : {},
    ...(hasApproval ? { planApproval: { status: planState, planHash: planState === 'planning' ? '' : 'fixture-plan-sha256', requirementsRevision: planState === 'planning' ? 2 : 1, lastActionClientMessageId: lastActionId,
      clarifications: scene === 'clarify' ? [{ id: 'scope', question: '恢复后需要保留哪些阅读状态？', options: ['阅读位置与未提交内容', '仅阅读位置'] }] : [],
      proposal: { tasks: scene === 'clarify' ? [] : [{ key: 'api', objective: a.objective, expectedOutput: '可验证的文件读取恢复结果', acceptanceCriteria: ['断线后重新读取最新回执'], ownerParticipantId: a.owner_id, writeTargets: ['文件读取入口'] }, { key: 'ui', objective: b.objective, expectedOutput: '阅读位置与草稿恢复', acceptanceCriteria: ['刷新后阅读位置和草稿保留', '重复打开保持当前内容'], ownerParticipantId: b.owner_id, writeTargets: ['阅读界面'] }] },
    } } : {}),
  };
}
function fixtureRoomSnapshot(id: string) {
  const base = previewRoomSnapshot(id);
  const rootId = `${id}:turn-1`;
  const source = (type: string) => structuredClone(base.events.find(event => event.eventType === type)!);
  const start = base.events.find(event => event.payload.sourceEventType === 'tool_started')!;
  const user = source('user_message'); user.payload.text = '为文件预览增加可靠的恢复能力：恢复后核实读取回执，同时保留阅读位置与未提交内容。';
  const events = [user];
  if (scene === 'parallel') {
    const prior = source('room_post'); prior.participantId = 'participant-firstlight'; prior.sourceSessionId = 'session-room-firstlight';
    prior.payload.post = { ...(prior.payload.post as Record<string, unknown>), postId: `${id}:previous-plan`, authorActorRef: 'participant:participant-firstlight', dispatchId: 'previous-plan-dispatch', content: '待整份方案确认后执行（上一轮规划的合成记录）。' };
    events.push(prior);
  }
  const addTool = (actor: string, session: string, call: string, command: string, done: boolean) => {
    const event = structuredClone(start); event.participantId = actor; event.sourceSessionId = session;
    event.payload = { rootId, dispatchId: `dispatch-${call}`, sourceEventId: `tool-${call}`, sourceEventType: done ? 'tool_finished' : 'tool_started', toolCallId: call, toolName: 'workspace_shell', args: { command }, summary: done ? '回归结果已返回' : '正在核对恢复行为', ...(done ? { result: { content: [{ type: 'text', text: '3 tests passed（合成回执）' }] } } : {}) };
    events.push(event);
  };
  if (!['plan', 'longplan', 'approval', 'clarify'].includes(scene)) {
    addTool('participant-firstlight', 'session-room-firstlight', 'api-fixture', 'pnpm test file-receipts', scene !== 'parallel');
    addTool('participant-future', 'session-room-future', 'ui-fixture', 'pnpm test reading-recovery', scene !== 'parallel');
  }
  const post = source('room_post');
  if (scene === 'longplan') addTool('participant-present', 'session-room-present', 'planning-read-fixture', 'rg -n recovery src', false);
  const content = scene === 'revision' ? '文件回执任务仍在执行，阅读状态任务已验收。可以修改正在执行的任务要求，集成任务将随之更新。（合成 UI 场景）' : scene === 'plan' || scene === 'longplan' ? '我会先核对恢复入口，把文件回执和阅读状态拆成可以并行验证的两项任务。'
    : scene === 'approval' ? '目标与验收标准已明确。下方是完整方案，确认后才开始执行。'
    : scene === 'clarify' ? '开始前需要明确恢复范围。请补充下方问题，或者直接在输入框中说明。'
    : scene === 'single' ? '当前由 Mars 核对文件回执，已完成的阅读状态结果保留在交付记录。'
    : scene === 'parallel' ? '两个任务正在并行推进：Mars 核对读取回执，Venus 处理阅读状态。我会等两项结果返回后安排独立复核。'
    : scene === 'review' ? '两项实现结果已返回，正在独立复核恢复后的状态和可操作性。'
    : scene === 'returned' ? '复核发现重复打开的场景尚未覆盖，已把阅读状态任务退回补充。已有文件回执结果保留。'
    : scene === 'abstained' ? '当前没有继续执行的派发，调度回执暂未选出下一步。（合成状态）'
    : scene === 'synthesize' ? '两项任务已验收，正在汇总改动、验证结果和仍需核实的边界。'
    : '已完成两项恢复能力，并核对了恢复入口。\n\n这是合成 UI 验证内容，不是实测交付。';
  post.payload.post = { ...(post.payload.post as Record<string, unknown>), content, ...(['plan', 'longplan'].includes(scene) ? { dispatchId: 'dispatch-planning-read-fixture' } : scene === 'synthesize' ? { dispatchId: 'dispatch-synth-fixture' } : {}) };
  events.push(post);
  if (scene === 'review') addTool('participant-present', 'session-room-present', 'verify-fixture', 'pnpm test recovery-scenarios', false);
  if (scene === 'final') { const terminal = base.events.find(event => event.eventType === 'turn_completed' && !event.participantId); if (terminal) events.push(structuredClone(terminal)); }
  const normalized = events.map((event, index) => ({ ...event, eventId: `${id}:${index + 1}`, sequence: index + 1, resumeToken: `${id}:${index + 1}` }));
  return { ...base, room: { ...base.room, description: '为文件预览增加可靠的恢复能力', workItems: [], lastEventSequence: normalized.length }, events: normalized, lastSequence: normalized.length, resumeToken: `${id}:${normalized.length}` };
}
function Fixture() {
  const [selected, setSelected] = useState(scene);
  const [narrow, setNarrow] = useState(false);
  const [dark, setDark] = useState(false);
  const [avatarActivity, setAvatarActivity] = useState<RoomPlanetActivity>('working');
  const [reduceMotion, setReduceMotion] = useState(false);
  useEffect(() => { document.documentElement.dataset.theme = dark ? 'dark' : 'light'; }, [dark]);
  const currentRoomId = `${roomId}-${selected}`;
  const room = fixtureRoomSnapshot(currentRoomId).room as unknown as RoomSummary;
  return <QueryClientProvider client={client}><ControlTransportProvider transport={transport}><TooltipProvider><div className="paw-desktop-root jev-fixture-shell" data-paw-visual="stellar" data-reduce-motion={reduceMotion}>
    <header className="jev-fixture-toolbar"><strong>合成 UI 验证 · 不调用模型</strong><label>场景<select value={selected} onChange={event => { scene = event.target.value as Scene; stopped = false; planState = scene === 'clarify' ? 'awaiting_input' : 'awaiting_approval'; lastActionId = ''; revisionRequested = false; setSelected(scene); history.replaceState(null, '', `?scene=${scene}`); }}>{Object.entries(scenes).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label><button type="button" onClick={() => setNarrow(!narrow)}>{narrow ? '恢复宽窗口' : '模拟窄窗口'}</button><button type="button" onClick={() => setDark(!dark)}>{dark ? '浅色主题' : '深色主题'}</button></header>
    <main className="jev-fixture-stage" data-narrow={narrow}>{selected === 'removal' ? <RemovalFixture room={room} /> : selected === 'tools' ? <ToolFixtures /> : selected === 'avatars' ? <section className="jev-fixture-avatars"><div className="jev-fixture-avatar-controls"><label>头像状态<select aria-label="头像状态" value={avatarActivity} onChange={event => setAvatarActivity(event.target.value as RoomPlanetActivity)}>{['static', 'idle', 'thinking', 'working', 'waiting', 'done', 'error', 'stopped'].map(value => <option key={value} value={value}>{value}</option>)}</select></label><button type="button" aria-pressed={reduceMotion} onClick={() => setReduceMotion(!reduceMotion)}>模拟减少动态：{reduceMotion ? '开' : '关'}</button></div>{ROOM_PLANET_NAMES.map((name, ordinal) => <article key={name}><strong>{name}</strong>{[20, 28, 40, 72].map(size => <RoomPlanetAvatar key={size} ordinal={ordinal} size={size} activity={avatarActivity} />)}</article>)}</section> : <PawRoomWorkspace key={selected} interfaceMode="jev" personas={[]} record={room} recordId={currentRoomId} onRoomUpdated={() => undefined} />}</main>
  </div></TooltipProvider></ControlTransportProvider></QueryClientProvider>;
}
function RemovalFixture({ room }: { room: RoomSummary }) {
  const [items, setItems] = useState<ParticipantRemoval[]>([
    { participantId: room.participants[0].id, status: 'blocked', stage: 'controller_requires_stop', targetParticipantId: '' },
    { participantId: room.participants[1].id, status: 'pending', stage: 'awaiting_stop', targetParticipantId: '' },
  ]);
  return <section style={{ maxWidth: 640, margin: '32px auto', padding: 24 }}><h2>伙伴移交 · 合成状态</h2><PawRoomRemovalProgress room={room} items={items} error="" busy={false} onRemove={(participantId, extra) => setItems(previous => previous.map(item => item.participantId !== participantId ? item : { ...item, status: 'pending', stage: 'awaiting_stop', stopRoot: extra?.stopRoot || item.stopRoot, targetParticipantId: extra?.replacementParticipantId ?? item.targetParticipantId }))} /></section>;
}
function ToolFixtures() {
  const [status, setStatus] = useState<'running' | 'success' | 'error' | 'cancelled'>('running');
  const [startedAt] = useState(Date.now() - 8000);
  const [recordsOpen, setRecordsOpen] = useState(false);
  const activity: AgentActivityProjection = { id: 'fixture-tool', turnId: 'fixture-turn', kind: status === 'running' ? 'tool_started' : 'tool_finished', status: status === 'running' ? 'running' : status === 'error' ? 'failed' : 'completed', ...(status === 'cancelled' ? { settledByTurnStatus: 'aborted' as const } : {}), summary: '核对恢复行为', payload: { toolCallId: 'fixture-tool', toolName: 'workspace_shell', args: { command: 'pnpm test file-recovery' }, result: status === 'success' ? { content: [{ type: 'text', text: '3 tests passed' }] } : status === 'error' ? { error: '文件尚未重新载入' } : undefined }, createdAtMs: startedAt, updatedAtMs: Date.now() };
  return <section className="jev-fixture-tools"><button type="button" onClick={() => setRecordsOpen(true)}>查看活动列表样式</button><PawJevToolRecordDialog open={recordsOpen} onClose={() => setRecordsOpen(false)} renderDetail={() => null} blocks={[
    { id: 'skill', kind: 'tool', name: '加载 Skill', summary: 'Impeccable · 界面细节检查', status: 'success', output: '合成示例：技能已载入' },
    { id: 'read', kind: 'tool', name: '读取文件', summary: 'README.md · 第 1–40 行', status: 'success', output: '合成示例：文件正文' },
    { id: 'search', kind: 'tool', name: '搜索内容', summary: 'Room 任务与进度组件', status: 'success', output: '合成示例：检索结果' },
    { id: 'dispatch', kind: 'tool', name: 'Jev 任务分派', summary: 'Earth → Mars · 检查恢复行为', status: 'success', output: '合成示例：分派回执' },
    { id: 'shell', kind: 'tool', name: '终端命令', summary: '运行文件恢复测试', status, output: status === 'success' ? '合成示例：3 tests passed' : undefined },
  ]} /><div role="group" aria-label="工具回执状态">{(['running', 'success', 'error', 'cancelled'] as const).map(value => <button type="button" aria-pressed={status === value} key={value} onClick={() => setStatus(value)}>{value}</button>)}</div><h2>Session · 真实工具行组件</h2><div className="paw-chatfx"><FxActivityStack activities={[activity]} sessionId="fixture-session" /></div><h2>Room · 共享工具回执组件</h2><div className="ccui-conversation-surface"><ToolCard block={{ id: 'fixture-tool', kind: 'tool', name: '运行命令', summary: '核对恢复行为', input: 'pnpm test file-recovery', output: status === 'success' ? '3 tests passed' : status === 'error' ? '文件尚未重新载入' : undefined, status }} /></div></section>;
}
const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
createRoot(document.getElementById('root')!).render(<Fixture />);
