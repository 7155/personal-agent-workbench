import type { RoomSummary } from '../../src/features/rooms/room-types';
import type { JevSnapshot, JevTask, JevEffect } from '../../src/features/semantic-workspace/jev-execution';
import type { JevMissionTask } from '../../src/features/semantic-workspace/jev-mission';

export const collaborationScenes = {
  mixed: '执行与复核', parallel: '任务并行', returned: '退回修改', failed: '局部失败',
  stopped: '已停止', completed: '结果交付', plan: '待确认方案',
  many: '八位伙伴', history: '历史快照', disconnected: '保留快照', cycle: '异常依赖', empty: '尚无分工',
};
export type CollaborationScene = keyof typeof collaborationScenes;
export const DEMO_LABELS: Record<string, string> = { queued: '待调度', running: '执行中', verifying: '复核中', review: '待复核', blocked: '等待依赖', done: '已验收', failed: '未完成', returned: '待返修' };
const names = ['Earth', 'Mars', 'Venus', 'Mercury', 'Jupiter', 'Saturn', 'Uranus', 'Neptune'];

/** Explicit synthetic display input, not a substitute for jevMission or the backend parser. */
export function createCollaborationDemo(scene: CollaborationScene, step = 0): { graph: JevSnapshot; room: RoomSummary; mission: JevMissionTask[] } {
  const room = {
    id: 'room-collaboration-v4-demo', title: '文件预览与阅读恢复', description: '为文件预览增加可靠的恢复能力',
    moderatorParticipantId: 'p0', participants: names.map((name, ordinal) => ({ id: `p${ordinal}`, sessionId: `demo-session-${ordinal}`, ordinal, status: 'active', displayName: name })),
  } as unknown as RoomSummary;
  const task = (id: string, objective: string, owner: number, state: string, expected: string): JevTask => ({
    id, objective, ownerId: `p${owner}`, state, revision: 1, parentId: 'root',
    expectedOutput: expected, acceptance: ['结果可回到本次任务的原始证据', '没有覆盖的场景需要明确保留'],
    result: '', artifacts: [], evidence: [], acceptedTurnId: '',
  });
  const tasks = [
    { ...task('root', room.description ?? room.title, 0, 'queued', '一份可核对的交付'), parentId: '' },
    task('api', '恢复后核实文件读取回执', 1, 'active', '读取恢复实现及回执说明'),
    task('reader', '保留阅读位置与未提交内容', 2, 'review', '阅读与草稿恢复实现'),
    task('formats', '整理附件类型与失败提示', 3, 'done', '文件展示边界文档'),
    task('integration', '联调恢复入口与重复打开', 0, 'queued', '逐项联调结果与未覆盖场景'),
    task('delivery', '整理交付文件与验证依据', 4, 'queued', '文件清单与结果说明'),
  ];
  const effects: JevEffect[] = [];
  const effect = (id: string, taskId: string, owner: number, purpose: string, executionStatus: string, revision = 1) => {
    effects.push({ effectId: id, operation: 'dispatch', state: 'accepted', executionStatus,
      request: { graphId: 'jev-v4-demo', roomId: room.id, rootId: 'demo-root-turn', taskId, taskRevision: revision, ownerId: `p${owner}`, sessionId: `demo-session-${owner}`, purpose, dispatchId: `dispatch-${id}` }, receipt: { state: 'accepted', turnId: `turn-${id}` } });
  };
  const stages: Record<string, string> = { api: 'running', reader: 'verifying', formats: 'done', integration: 'blocked', delivery: 'blocked' };
  effect('api-exec', 'api', 1, 'execute', 'running');
  effect('reader-verify', 'reader', 0, 'verify', 'running');
  effect('reader-exec', 'reader', 2, 'execute', 'completed');
  effect('formats-exec', 'formats', 3, 'execute', 'completed');
  effect('formats-verify', 'formats', 4, 'verify', 'completed');
  tasks[3]!.result = '类型与失败提示已整理；此处为展示用合成回执。';
  tasks[3]!.artifacts = ['docs/file-preview-boundaries.md'];
  tasks[3]!.acceptedTurnId = 'formats-exec';
  tasks[2]!.acceptedTurnId = 'reader-exec';
  if (scene === 'parallel') {
    tasks[2]!.state = 'active'; stages.reader = 'running';
    effects.splice(effects.findIndex(row => row.effectId === 'reader-verify'), 1);
    effects.find(row => row.effectId === 'reader-exec')!.executionStatus = 'running';
  }
  if (scene === 'returned') {
    tasks[2]!.state = 'queued'; tasks[2]!.revision = 2; stages.reader = 'returned';
    tasks[2]!.result = '复核发现重复打开后位置恢复尚未覆盖，已退回补充。（样例）';
    tasks[2]!.acceptedTurnId = '';
    // Old revision execution/verification rows remain in input; the display must reject them.
    effects.find(row => row.effectId === 'reader-verify')!.executionStatus = 'completed';
  }
  if (scene === 'failed') { tasks[1]!.state = 'failed'; stages.api = 'failed'; effects[0]!.executionStatus = 'failed'; tasks[1]!.result = '恢复场景缺少可读取回执，任务尚未完成。（样例）'; }
  if (step >= 1) {
    tasks[2]!.state = 'done'; stages.reader = 'done';
    const verify = effects.find(row => row.effectId === 'reader-verify'); if (verify) verify.executionStatus = 'completed';
    tasks[2]!.artifacts = ['src/reading-recovery.ts']; tasks[2]!.result = '阅读恢复已验收。（合成回执）';
  }
  if (step >= 2) { tasks[1]!.state = 'done'; stages.api = 'done'; effects[0]!.executionStatus = 'completed'; tasks[1]!.acceptedTurnId = 'api-exec'; tasks[1]!.artifacts = ['src/file-receipts.ts']; }
  if (step >= 3) { tasks[4]!.state = 'active'; stages.integration = 'running'; effect('integration-exec', 'integration', 0, 'execute', 'running'); }
  if (scene === 'completed') {
    for (const current of tasks.slice(1)) { current.state = 'done'; stages[current.id] = 'done'; current.artifacts = [`deliveries/${current.id}.md`]; current.result = '本项样例任务已验收；这不是实机测试结果。'; }
    effects.forEach(row => { row.executionStatus = 'completed'; });
    effect('integration-exec', 'integration', 0, 'execute', 'completed');
    effect('delivery-exec', 'delivery', 4, 'execute', 'completed');
  }
  if (scene === 'many') for (let i = 0; i < 7; i++) {
    const current = task(`extra-${i}`, ['梳理恢复错误的用户提示', '核对键盘与焦点路径', '检查长文件名与窄屏展示', '核实停止后的残留状态', '整理部署与接入说明', '核对图示与实际依赖', '检查新版本保留的证据'][i]!, (i + 1) % 8, i < 3 ? 'active' : 'queued', '明确记录结果与未覆盖边界');
    tasks.push(current); stages[current.id] = i < 3 ? 'running' : 'queued';
    if (i < 3) effect(`extra-exec-${i}`, current.id, (i + 1) % 8, 'execute', 'running');
  }
  const edges = [
    { prerequisite: 'api', dependent: 'integration', kind: 'requires' },
    { prerequisite: 'reader', dependent: 'integration', kind: 'requires' },
    { prerequisite: 'integration', dependent: 'delivery', kind: 'requires' },
    { prerequisite: 'formats', dependent: 'delivery', kind: 'requires' },
    { prerequisite: 'formats', dependent: 'reader', kind: 'context' },
  ];
  if (scene === 'cycle') edges.push({ prerequisite: 'delivery', dependent: 'api', kind: 'requires' });
  const graph: JevSnapshot = {
    graphId: 'jev-v4-demo', roomId: room.id, rootId: 'demo-root-turn', version: `${scene}:${step}`, phase: scene === 'plan' ? 'awaiting_approval' : scene === 'completed' ? 'final' : 'execute',
    stopped: scene === 'stopped', requirementsRevision: scene === 'returned' ? 2 : 1,
    tasks: scene === 'empty' ? [tasks[0]!] : tasks,
    edges: scene === 'empty' ? [] : edges, running: tasks.filter(row => stages[row.id] === 'running').map(row => row.id), ready: [], review: ['reader'],
    blocked: [], effects: scene === 'empty' || scene === 'plan' ? [] : effects, events: [],
    final: scene === 'completed' ? { status: 'completed', content: '合成样例已汇总，不是真实执行结果。', evidence: [] } : null,
    modelCards: [], planApproval: scene === 'plan' ? { status: 'awaiting_approval', planHash: 'demo-plan', requirementsRevision: 1, lastActionClientMessageId: '', clarifications: [], tasks: tasks.slice(1).map(current => ({ key: current.id, objective: current.objective, expectedOutput: current.expectedOutput, acceptanceCriteria: current.acceptance, ownerParticipantId: current.ownerId, dependsOn: edges.filter(edge => edge.dependent === current.id && edge.kind !== 'context').map(edge => edge.prerequisite), contextRefs: [], writeTargets: [] })) } : null,
  };
  const mission: JevMissionTask[] = graph.tasks.filter(current => current.parentId).map(current => {
    const stage = stages[current.id] || 'queued';
    const waitingOn = ['done', 'failed'].includes(stage) ? [] : edges.filter(edge => edge.dependent === current.id && edge.kind !== 'context').flatMap(edge => tasks.filter(prerequisite => prerequisite.id === edge.prerequisite && prerequisite.state !== 'done'));
    return { task: current, stage: stage as JevMissionTask['stage'], lane: ['done', 'failed'].includes(stage) ? 'ended' : stage === 'returned' ? 'attention' : ['queued', 'blocked'].includes(stage) ? 'waiting' : 'active',
      tone: stage === 'done' ? 'done' : stage === 'failed' ? 'failed' : stage === 'returned' ? 'attention' : stage === 'verifying' ? 'review' : stage === 'running' ? 'active' : 'waiting', waitingOn,
      reasons: waitingOn.length ? ['前置任务尚未全部验收'] : stage === 'returned' ? ['复核要求补充重复打开场景'] : [],
    };
  });
  graph.blocked = mission.filter(item => item.waitingOn.length).map(item => ({ taskId: item.task.id, reasons: ['prerequisite_not_done'] }));
  return { graph, room, mission };
}
