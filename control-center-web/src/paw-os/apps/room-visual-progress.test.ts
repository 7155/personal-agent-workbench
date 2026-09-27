import { describe, it } from 'vitest';
import assert from 'node:assert/strict';
import { buildRoomVisualProgress, roomProgressBucket, roomPublicAction, roomTaskAccepted, roomTextExcerpt } from './room-visual-progress';
import { presentRoomParticipant, roomRoleLabel } from './room-participant-presentation';
import { visualFocus, visualPartner, visualWork } from '@/test/fixtures/room-visual';

describe('Known WorkItem count, not elapsed-time progress', () => {
  it('empty and runtime-only rooms have no numeric progress', () => {
    for (const items of [[], [visualWork('dispatch', { source: 'runtime', state: 'completed' })]]) {
      const p = buildRoomVisualProgress(visualFocus(items)); assert.equal(p.total, 0); assert.equal(p.fraction, null);
    }
  });
  it('counts leaf work once and lists aggregate completion separately', () => {
    const p = buildRoomVisualProgress(visualFocus([visualWork('root', { state: 'completed' }),
      visualWork('a', { parentId: 'root', state: 'completed' }), visualWork('b', { parentId: 'root', state: 'review' })]));
    assert.equal(p.total, 2); assert.equal(p.completed, 1); assert.equal(p.fraction, .5); assert.equal(p.aggregates.length, 1);
  });
  it('submitted/review is not completed even with a stale positive verdict', () => {
    const task = visualWork('a', { state: 'review', review: { operability: 'passed', requirement: 'satisfied' } });
    assert.equal(roomTaskAccepted(task), false); assert.equal(buildRoomVisualProgress(visualFocus([task])).completed, 0);
  });
  it('dual verdicts distinguish accepted from merely returned work', () => {
    const p = buildRoomVisualProgress(visualFocus([visualWork('a', { state: 'completed' }), visualWork('b', { state: 'completed', review: { operability: 'passed', requirement: 'satisfied' } })]));
    assert.equal(p.completed, 2); assert.equal(p.accepted, 1);
  });
  it('failure and cancellation remain in the denominator without becoming successes', () => {
    const p = buildRoomVisualProgress(visualFocus([visualWork('ok', { state: 'completed' }), visualWork('stop', { state: 'stopped' }), visualWork('fail', { state: 'failed' })]));
    assert.equal(p.total, 3); assert.equal(p.completed, 1); assert.equal(p.fraction, 1 / 3);
  });
  it('splitting a task changes the denominator honestly instead of smoothing a fake percentage', () => {
    const before = buildRoomVisualProgress(visualFocus([visualWork('a', { state: 'completed' }), visualWork('b')]));
    const after = buildRoomVisualProgress(visualFocus([visualWork('a', { state: 'completed' }), visualWork('b'), visualWork('c')]));
    assert.equal(before.fraction, .5); assert.equal(after.fraction, 1 / 3);
  });
  it('missing parents suppress an overall fraction but keep known leaves readable', () => {
    const p = buildRoomVisualProgress(visualFocus([visualWork('a', { parentId: 'not-loaded', state: 'completed' })]));
    assert.equal(p.incomplete, true); assert.equal(p.fraction, null); assert.equal(p.leaves[0].id, 'a');
  });
  it('self-parent and multi-node cycles do not produce a successful empty graph', () => {
    for (const items of [[visualWork('a', { parentId: 'a' })], [visualWork('a', { parentId: 'b' }), visualWork('b', { parentId: 'a' })]]) {
      const p = buildRoomVisualProgress(visualFocus(items)); assert.equal(p.incomplete, true); assert.equal(p.fraction, null);
    }
  });
  it('duplicate IDs are disclosed and cannot inflate progress', () => {
    const p = buildRoomVisualProgress(visualFocus([visualWork('a'), visualWork('a', { state: 'completed' })]));
    assert.equal(p.total, 1); assert.equal(p.incomplete, true); assert.equal(p.fraction, null);
  });
  it('works on a deep task tree without recursion or input mutation', () => {
    const tasks = Array.from({ length: 2000 }, (_, i) => visualWork(`w${i}`, { parentId: i ? `w${i - 1}` : undefined }));
    const original = JSON.stringify(tasks); const p = buildRoomVisualProgress(visualFocus(tasks));
    assert.equal(p.total, 1); assert.equal(p.aggregates.length, 1999); assert.equal(JSON.stringify(tasks), original);
  });
  it('segment counts and IDs partition exactly the known leaves', () => {
    const states = ['completed', 'review', 'running', 'blocked', 'failed', 'stopped', 'waiting', 'idle', 'disconnected'] as const;
    const p = buildRoomVisualProgress(visualFocus(states.map((state, i) => visualWork(String(i), { state }))));
    assert.equal(p.segments.reduce((n, part) => n + part.count, 0), p.total);
    assert.equal(new Set(p.segments.flatMap((part) => part.taskIds)).size, p.total);
    assert.equal(roomProgressBucket(visualWork('x', { state: 'disconnected' })), 'unknown');
  });
});

describe('Role and execution presentation', () => {
  it('supports every role declared by the current Room contract', () => {
    assert.equal(roomRoleLabel('researcher'), '调研'); assert.equal(roomRoleLabel('implementer'), '实现');
    assert.equal(roomRoleLabel('coordinator'), '统筹'); assert.equal(roomRoleLabel('reviewer'), '复核');
    assert.equal(roomRoleLabel('specialist'), '执行'); assert.equal(roomRoleLabel(), '协作伙伴');
  });
  it('accountability does not inherit executor work or completion count', () => {
    const f = visualFocus([visualWork('a', { state: 'completed' })]);
    const v = presentRoomParticipant(f.partners[0], f); assert.equal(v.total, 0); assert.equal(v.completed, 0); assert.equal(v.role, '统筹');
  });
  it('offered tasks are shown separately and are not accepted ownership', () => {
    const f = visualFocus([visualWork('a', { ownerParticipantId: undefined, offeredToParticipantId: 'mars' })]);
    const v = presentRoomParticipant(f.partners[1], f); assert.equal(v.total, 0); assert.equal(v.offered, 1);
  });
  it('reviewers see their review work without claiming the executor tasks', () => {
    const f = visualFocus([visualWork('a', { verifierParticipantId: 'earth', state: 'review' })]);
    const v = presentRoomParticipant(f.partners[0], f); assert.equal(v.total, 0); assert(v.roles.includes('复核')); assert.equal(v.taskId, 'a');
  });
  it('an aggregate owned by the coordinator is not counted as a leaf when children have other owners', () => {
    const f = visualFocus([visualWork('parent', { ownerParticipantId: 'earth', state: 'completed' }), visualWork('child', { parentId: 'parent' })]);
    assert.equal(presentRoomParticipant(f.partners[0], f).total, 0); assert.equal(presentRoomParticipant(f.partners[1], f).total, 1);
  });
  for (const freshness of ['offline', 'recovering', 'paused'] as const) it(`${freshness} cannot claim live execution`, () => {
    const f = visualFocus(); assert.match(presentRoomParticipant(f.partners[1], f, freshness).execution, /上次/);
    assert.equal(presentRoomParticipant(f.partners[1], f, 'last-known').execution, '上次执行中');
  });
  it('participant execution can finish while assigned work still awaits review', () => {
    const f = visualFocus([visualWork('a', { state: 'review' })]); f.partners[1].state = 'completed';
    const v = presentRoomParticipant(f.partners[1], f); assert.equal(v.execution, '本轮执行结束'); assert.equal(v.review, 1); assert.equal(v.completed, 0);
  });
  it('missing Session disables navigation rather than creating one', () => {
    const p = visualPartner('mars', { sessionId: '' }); assert.equal(presentRoomParticipant(p, visualFocus()).canOpen, false);
  });
});

describe('Public wording and bounded excerpts', () => {
  for (const [raw, label] of [['turn_failed', '本轮执行失败'], ['turn_completed', '本轮执行已结束'], ['tool_failed', '工具调用失败'], ['snapshot_required', '需要恢复协作状态']]) {
    it(`names ${raw} without hiding its meaning`, () => assert.equal(roomPublicAction(raw), label));
  }
  it('keeps original natural language intact and does not invent an unknown event status', () => {
    assert.equal(roomPublicAction('工具调用失败，但另一个任务仍在运行'), '工具调用失败，但另一个任务仍在运行');
    assert.equal(roomPublicAction('future_owner_signal'), '执行记录已更新，展开可查看原始事件');
  });
  it('summarizes a recovered terminal receipt without flooding the task card with a Runtime id', () => {
    assert.equal(roomPublicAction('Partner Session completed; recovered from durable Runtime terminal event agent:abc'), '伙伴执行已结束（根据运行时回执恢复）');
  });
  it('clips Unicode by codepoint, preserves short text, and is not a generated translation', () => {
    assert.equal(roomTextExcerpt('a😀b', 2), 'a😀…'); assert.equal(roomTextExcerpt('简短'), '简短');
    assert.equal(roomTextExcerpt('Fix\nthis\tmodule', 40), 'Fix this module');
  });
});
