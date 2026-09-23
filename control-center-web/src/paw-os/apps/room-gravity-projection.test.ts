import { describe, expect, it } from 'vitest';
import type { RoomActivityProjection } from '@/contracts/room-reducer';
import {
  roomDispatchPlanFromActivity,
  roomDispatchPlanFromPayload,
  roomDispatchPlans,
  roomDispatchPlanSummary,
  roomDispatchSourceParticipantId,
  roomDispatchWaves,
  roomGravityToolLabel,
  roomToolActivityLine,
  roomToolEvidence,
  roomToolOpLabel,
  roomToolSummaryIsMachine,
} from './room-gravity-projection';

/** Payload shape lifted from the real minecraft-harness route_decision events. */
function routeDecisionPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schemaVersion: 'rag-ime.room-route-decision.v1',
    sourceEventType: 'route_decision',
    routingPolicy: 'parallel',
    roomKind: 'collaboration',
    selectedParticipantIds: ['participant-core'],
    targetParticipantId: 'participant-core',
    targetDisplayName: 'Agent 2',
    reason: 'partner_delegate',
    candidates: [
      { participantId: 'participant-coordinator', displayName: 'Agent 3', score: 0, signals: [] },
      { participantId: 'participant-core', displayName: 'Agent 2', score: 1, signals: ['explicit_invite'] },
    ],
    rootId: 'room-turn-1',
    dispatchId: 'room-child-5',
    parentDispatchId: 'room-dispatch-2',
    child: true,
    waveId: 'room-wave-a',
    phaseName: '并行实现纯逻辑与界面轨道',
    parallelIndex: 0,
    parallelSize: 2,
    workItemId: 'room-work-1',
    workItemState: 'active',
    ...overrides,
  };
}

function activity(id: string, kind: string, payload: Record<string, unknown>): RoomActivityProjection {
  return {
    id, turnId: 'room-turn-1', participantId: 'participant-core', sourceSessionId: 'session-core',
    kind, status: 'completed', summary: '已确认本轮分工', payload, sequence: 1, createdAtMs: 100,
  };
}

describe('roomDispatchPlanFromPayload', () => {
  it('parses the full route decision: reason, wave, parallel track and scored candidates', () => {
    const plan = roomDispatchPlanFromPayload(routeDecisionPayload());

    expect(plan).toBeDefined();
    expect(plan?.reasonLabel).toBe('伙伴委派');
    expect(plan?.routingPolicyLabel).toBe('并行协作');
    expect(plan?.targetParticipantId).toBe('participant-core');
    expect(plan?.waveId).toBe('room-wave-a');
    expect(plan?.phaseName).toBe('并行实现纯逻辑与界面轨道');
    expect(plan?.parallelIndex).toBe(0);
    expect(plan?.parallelSize).toBe(2);
    expect(plan?.workItemId).toBe('room-work-1');
    expect(plan?.candidates).toHaveLength(2);
    expect(plan?.candidates.find((candidate) => candidate.participantId === 'participant-core')?.selected).toBe(true);
    expect(plan?.candidates.find((candidate) => candidate.participantId === 'participant-coordinator')?.selected).toBe(false);
  });

  it('returns nothing for payloads without a routing target or dispatch id', () => {
    expect(roomDispatchPlanFromPayload({ sourceEventType: 'tool_started', toolName: 'read' })).toBeUndefined();
  });

  it('keeps unknown reasons readable instead of echoing machine enums as labels', () => {
    const plan = roomDispatchPlanFromPayload(routeDecisionPayload({ reason: 'custom_signal' }));
    expect(plan?.reasonLabel).toBe('按「custom_signal」选择');
    expect(plan?.reason).toBe('custom_signal');
  });
});

describe('roomDispatchPlanFromActivity', () => {
  it('only treats route/dispatch activities as plans', () => {
    expect(roomDispatchPlanFromActivity(activity('a', 'route_decision', routeDecisionPayload()))).toBeDefined();
    expect(roomDispatchPlanFromActivity(activity('b', 'tool', { toolName: 'read', targetParticipantId: 'x' }))).toBeUndefined();
  });
});

describe('roomDispatchWaves + source resolution', () => {
  const activities = [
    activity('root', 'route_decision', routeDecisionPayload({
      dispatchId: 'room-dispatch-2', parentDispatchId: '', child: false, reason: 'facilitator',
      targetParticipantId: 'participant-coordinator', targetDisplayName: 'Agent 3',
      waveId: '', phaseName: '', parallelIndex: undefined, parallelSize: undefined, workItemId: '',
    })),
    activity('core', 'route_decision', routeDecisionPayload()),
    activity('ui', 'route_decision', routeDecisionPayload({
      dispatchId: 'room-child-6', targetParticipantId: 'participant-ui', targetDisplayName: 'Agent 4',
      parallelIndex: 1, workItemId: 'room-work-5',
    })),
  ];
  const plans = roomDispatchPlans(activities);

  it('groups the parallel dispatches of one wave in track order', () => {
    const waves = roomDispatchWaves(plans);
    expect(waves).toHaveLength(1);
    expect(waves[0]?.waveId).toBe('room-wave-a');
    expect(waves[0]?.phaseName).toBe('并行实现纯逻辑与界面轨道');
    expect(waves[0]?.parallelSize).toBe(2);
    expect(waves[0]?.dispatches.map((dispatch) => dispatch.targetParticipantId))
      .toEqual(['participant-core', 'participant-ui']);
  });

  it('resolves the dispatching planet through the parent dispatch, Sol for roots', () => {
    const [root, core] = plans;
    expect(roomDispatchSourceParticipantId(root!, plans)).toBe('');
    expect(roomDispatchSourceParticipantId(core!, plans)).toBe('participant-coordinator');
  });

  it('summarizes a dispatch as reason · target · track · phase', () => {
    expect(roomDispatchPlanSummary(plans[1]!, 'Venus'))
      .toBe('伙伴委派 · 交给 Venus · 并行轨道 1/2 · 并行实现纯逻辑与界面轨道');
  });
});

describe('roomGravityToolLabel', () => {
  it('maps opaque Runtime tool ids to reader-facing labels', () => {
    expect(roomGravityToolLabel('room_partner')).toBe('行星协调');
    expect(roomGravityToolLabel('agents')).toBe('子 Agent 编排');
    expect(roomGravityToolLabel('workspace_job')).toBe('后台任务');
    expect(roomGravityToolLabel('read')).toBe('读取文件');
  });

  it('keeps unknown tools and the generic fallback intact', () => {
    expect(roomGravityToolLabel('my_custom_tool')).toBe('my_custom_tool');
    expect(roomGravityToolLabel('')).toBe('工具');
  });
});

describe('roomToolEvidence', () => {
  it('turns a room_partner list call into labeled facts with the real partner roster', () => {
    const evidence = roomToolEvidence({
      toolName: 'room_partner',
      arguments: { op: 'list' },
      result: {
        operation: 'list',
        partners: [
          { participantId: 'p2', displayName: 'Alice', ordinal: 1, collaborationRole: 'reviewer' },
          { participantId: 'p3', displayName: 'Bob', ordinal: 2, collaborationRole: 'specialist' },
        ],
      },
    });

    expect(evidence?.label).toBe('行星协调');
    expect(evidence?.headline).toBe('行星协调 · 查看伙伴名册');
    expect(evidence?.facts.find((fact) => fact.label === '操作')?.value).toBe('查看伙伴名册');
    expect(evidence?.facts.find((fact) => fact.label === '伙伴')?.value)
      .toBe('Mars · reviewer；Venus · specialist');
    expect(evidence?.facts.find((fact) => fact.label === '伙伴')?.value)
      .not.toMatch(/Alice|Bob/);
  });

  it('labels delegate_batch and keeps primitive arguments compact', () => {
    const evidence = roomToolEvidence({
      toolName: 'room_partner',
      arguments: { op: 'delegate_batch', tasks: [{ a: 1 }, { b: 2 }] },
    });
    expect(evidence?.headline).toBe('行星协调 · 批量并行委派');
    expect(evidence?.facts.find((fact) => fact.label === 'tasks')?.value).toBe('2 项');
  });

  it('returns nothing when there is no tool identity in the payload', () => {
    expect(roomToolEvidence({ sourceEventType: 'reasoning_summary' })).toBeUndefined();
  });

  it('scopes op labels to the owning tool — workspace_job/browser status is not 查看伙伴状态', () => {
    // Real harness payloads: workspace_job and browser both use op "status"
    // for their own state polls; only room_partner status is a partner check.
    expect(roomToolOpLabel('room_partner', 'status')).toBe('查看伙伴状态');
    expect(roomToolOpLabel('workspace_job', 'status')).toBe('status');
    expect(roomToolOpLabel('browser', 'status')).toBe('status');

    const evidence = roomToolEvidence({ toolName: 'workspace_job', arguments: { op: 'status', jobId: 'bg_1' } });
    expect(evidence?.headline).toBe('后台任务 · status');
    expect(evidence?.facts.find((fact) => fact.label === '操作')?.value).toBe('status');
  });

  it('derives the headline op from the result when a snapshot page dropped the arguments', () => {
    // tool_finished pages in the harness carry only { result: { operation } }.
    const evidence = roomToolEvidence({
      toolName: 'room_partner',
      result: { operation: 'delegate', status: 'accepted' },
    });
    expect(evidence?.headline).toBe('行星协调 · 委派任务');
  });
});

describe('roomToolActivityLine', () => {
  it('recognizes machine-id summaries the Runtime echoes for tool activities', () => {
    expect(roomToolSummaryIsMachine('room_partner', { toolName: 'room_partner' })).toBe(true);
    expect(roomToolSummaryIsMachine('agents', { toolName: 'agents' })).toBe(true);
    expect(roomToolSummaryIsMachine('skill_load', { toolName: 'skill_load' })).toBe(true);
    expect(roomToolSummaryIsMachine('', { toolName: 'read' })).toBe(true);
    expect(roomToolSummaryIsMachine('已创建 ROOT.md', { toolName: 'write' })).toBe(false);
    expect(roomToolSummaryIsMachine('命令执行完成，退出码 0', { toolName: 'bash' })).toBe(false);
  });

  it('keeps real prose summaries untouched', () => {
    expect(roomToolActivityLine('已读取 ROOT.md 第 1-30 行', { toolName: 'read' }, 'completed'))
      .toBe('已读取 ROOT.md 第 1-30 行');
  });

  it('replaces a machine-id summary with the evidence headline and honest state', () => {
    const payload = { toolName: 'room_partner', arguments: { op: 'delegate_batch' }, result: { status: 'accepted' } };
    expect(roomToolActivityLine('room_partner', payload, 'completed')).toBe('行星协调 · 批量并行委派 已完成');
    expect(roomToolActivityLine('room_partner', payload, 'running')).toBe('行星协调 · 批量并行委派 正在执行');
    expect(roomToolActivityLine('bash', { toolName: 'bash' }, 'failed')).toBe('终端命令 执行失败');
    expect(roomToolActivityLine('browser', { toolName: 'browser' }, 'aborted')).toBe('浏览器操作 已停止');
  });
});

it('preserves gateway failure evidence instead of presenting bash as the failure reason', () => {
  const payload = { toolName: 'bash', approvalId: 'approved-command', error: 'Tool gateway request timed out after 30000ms', result: { outputPreview: 'Tool gateway request timed out after 30000ms' }, arguments: { command: 'pnpm typecheck', timeout: 180 } };
  expect(roomToolActivityLine('bash', payload, 'failed')).toBe(payload.error);
  expect(roomToolEvidence(payload)?.facts).toContainEqual({ label: '失败原因', value: payload.error });
});
