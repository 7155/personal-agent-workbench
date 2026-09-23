import { describe, expect, it } from 'vitest';
import { createRoomProjection, type RoomProjectionState } from '@/contracts/room-reducer';
import type { RoomSummary, RoomWorkItem } from '@/features/rooms/room-types';
import { buildRoomFocusProjection } from './room-focus-projection';
import { buildRoomWorkStatus } from './room-work-status';
import { buildRoomFocusMesh } from './room-focus-mesh';

function participant(id: string, ordinal: number, displayName = `Agent ${ordinal + 1}`) {
  return {
    id,
    sessionId: `session-${id}`,
    roleId: `role-${id}`,
    roleVersion: '1',
    displayName,
    collaborationRole: ordinal === 0 ? 'coordinator' as const : 'implementer' as const,
    status: 'active',
    ordinal,
  };
}

function work(overrides: Partial<RoomWorkItem> & Pick<RoomWorkItem, 'id' | 'objective'>): RoomWorkItem {
  const { id, objective, ...rest } = overrides;
  return {
    id,
    roomId: 'room-sol',
    topicId: 'topic-1',
    rootTurnId: 'turn-root',
    rootWorkId: overrides.id,
    parentWorkId: '',
    objective,
    expectedOutput: '可核验交付',
    acceptanceCriteria: [],
    accountableParticipantId: 'p-earth',
    currentOwnerParticipantId: 'p-earth',
    offeredToParticipantId: '',
    createdByParticipantId: 'p-earth',
    clientMessageId: `client-${overrides.id}`,
    state: 'active',
    depth: 0,
    revision: 1,
    resultSummary: '',
    artifactRefs: [],
    evidenceRefs: [],
    blocker: {},
    acceptedTurnId: '',
    createdAtMs: 10,
    updatedAtMs: 10,
    completedAtMs: null,
    ...rest,
  };
}

function room(workItems: RoomWorkItem[] = []): RoomSummary {
  return {
    id: 'room-sol',
    title: '迁移作战室',
    description: '整合前端并交给独立伙伴复核',
    status: 'active',
    routingPolicy: 'parallel',
    moderatorParticipantId: 'p-earth',
    activeTopicId: 'topic-1',
    updatedAtMs: 20,
    participants: [
      participant('p-earth', 0),
      participant('p-mars', 1),
      participant('p-venus', 2),
    ],
    topics: [{
      id: 'topic-1',
      roomId: 'room-sol',
      title: '任务图依赖验证',
      summary: '两个实现分支汇合后复核。',
      status: 'active',
      ordinal: 0,
      createdAtMs: 1,
      updatedAtMs: 2,
    }],
    workItems,
  };
}

function projectionWithParallelRuntime(): RoomProjectionState {
  const projection = createRoomProjection('room-sol');
  projection.activitiesById = {
    'activity-earth': {
      id: 'activity-earth',
      sequence: 2,
      turnId: 'turn-root',
      participantId: 'p-earth',
      sourceSessionId: 'session-p-earth',
      kind: 'tool',
      status: 'completed',
      summary: '任务图交互已通过测试',
      payload: {
        dispatchId: 'dispatch-earth',
        task: '实现 Room 任务图交互',
        expectedOutput: '可复查的任务图交互',
        toolName: 'vitest',
      },
      createdAtMs: 12,
      updatedAtMs: 13,
    },
    'activity-mars': {
      id: 'activity-mars',
      sequence: 3,
      turnId: 'turn-root',
      participantId: 'p-mars',
      sourceSessionId: 'session-p-mars',
      kind: 'tool',
      status: 'running',
      summary: '正在核对依赖投影',
      payload: {
        dispatchId: 'dispatch-mars',
        task: '实现 Room 依赖数据投影',
        expectedOutput: '可核验的依赖投影',
        toolName: 'read_file',
      },
      createdAtMs: 14,
      updatedAtMs: 15,
    },
  };
  projection.activityOrder = ['activity-earth', 'activity-mars'];
  return projection;
}

describe('buildRoomFocusProjection', () => {
  it('shows a woken partner as executing after an earlier dispatch ended', () => {
    const projection = createRoomProjection('room-sol');
    projection.turnOrder = ['turn-root'];
    projection.turnsById = {
      'turn-root': {
        id: 'turn-root', rootId: 'turn-root', status: 'running',
        messageIds: [], activityIds: [], participantIds: ['p-earth', 'p-mars'],
        dispatchIds: ['first', 'wake'], terminalDispatchIds: ['first'],
        dispatchParticipantIds: { first: 'p-mars', wake: 'p-mars' },
        terminalParticipantIds: ['p-mars'], createdAtMs: 10, updatedAtMs: 20,
      },
    };
    const focus = buildRoomFocusProjection(room([work({
      id: 'wake-work', objective: '继续核对结果', currentOwnerParticipantId: 'p-mars', state: 'done',
    })]), projection);

    expect(focus.partners.find((partner) => partner.participantId === 'p-mars')?.state).toBe('running');
    const status = buildRoomWorkStatus({ focus, projection, recoveryState: 'synced', visible: true });
    expect(status.executingParticipantIds).toContain('p-mars');
    expect(status.animate).toBe(true);
  });

  it('keeps a failed execution visible without exposing its raw event code', () => {
    const projection = projectionWithParallelRuntime();
    const activity = projection.activitiesById['activity-mars'];
    projection.activitiesById['activity-mars'] = {
      ...activity,
      status: 'failed',
      summary: 'turn_failed',
    };
    const focus = buildRoomFocusProjection(room([work({
      id: 'work-review', objective: '核对实际失败回执', currentOwnerParticipantId: 'p-mars',
    })]), projection);

    expect(focus.workItems.find((item) => item.id === 'work-review')?.currentAction).toBe('本轮执行失败');
    expect(focus.partners.find((partner) => partner.participantId === 'p-mars')?.currentAction).toBe('本轮执行失败');
  });

  it('reads production nested intercom endpoints and updates one message across delivery receipts', () => {
    const projection = createRoomProjection('room-sol');
    const message = { id: 'peer-ask', kind: 'ask', sourceParticipantId: 'p-earth', targetParticipantId: 'p-mars', content: '请核对接口', replyTo: '' };
    for (const [index, phase] of ['queued', 'delivered', 'replied'].entries()) {
      const id = `intercom-event-${index}`;
      projection.activityOrder.push(id);
      projection.activitiesById[id] = {
        id, turnId: 'peer-turn', kind: 'dispatch', sourceSessionId: 's-mars', participantId: 'p-mars', status: 'completed', summary: 'participant_activity',
        payload: { activityKind: 'intercom', phase, message: { ...message, status: phase } }, createdAtMs: index + 10, updatedAtMs: index + 10, sequence: index + 1,
      };
    }
    const packets = buildRoomFocusProjection(room(), projection).flow;
    expect(packets).toHaveLength(1);
    expect(packets[0]).toMatchObject({
      id: 'intercom:peer-ask', kind: 'question', sourceParticipantId: 'p-earth', targetParticipantIds: ['p-mars'],
      summary: '请核对接口', status: 'replied', createdAtMs: 10,
      receiptIds: ['intercom-event-0', 'intercom-event-1', 'intercom-event-2'],
    });
  });

  it('keeps the real goal and projects runtime branches beneath the explicit root work item', () => {
    const root = work({
      id: 'work-root',
      objective: '并行实现 Room 任务图，整合后独立复核',
      currentOwnerParticipantId: 'p-venus',
      accountableParticipantId: 'p-venus',
      state: 'review',
      resultSummary: '两个实现分支已汇合，等待独立复核。',
      evidenceRefs: ['test:room-graph-ui', 'test:room-graph-data'],
    });

    const focus = buildRoomFocusProjection(room([root]), projectionWithParallelRuntime());

    expect(focus.goal.title).toBe('任务图依赖验证');
    expect(focus.goal.state).toBe('review');
    expect(focus.workItems.map((item) => [item.id, item.parentId, item.ownerParticipantId, item.state])).toEqual([
      ['work-root', undefined, 'p-venus', 'review'],
      ['runtime:dispatch-earth', 'work-root', 'p-earth', 'completed'],
      ['runtime:dispatch-mars', 'work-root', 'p-mars', 'running'],
    ]);
    expect(focus.workItems[0]?.evidence.map((item) => item.ref)).toEqual([
      'test:room-graph-ui',
      'test:room-graph-data',
    ]);
  });

  it('keeps participant ids behind stable planet presentation names', () => {
    const focus = buildRoomFocusProjection(room(), createRoomProjection('room-sol'));

    expect(focus.partners.map((item) => [item.participantId, item.celestialName, item.displayName])).toEqual([
      ['p-earth', 'Earth', 'Agent 1'],
      ['p-mars', 'Mars', 'Agent 2'],
      ['p-venus', 'Venus', 'Agent 3'],
    ]);
  });

  it('projects the chronological flow ledger from public messages and transfers without inventing WorkItem traffic', () => {
    const withWork = room([work({
      id: 'work-root',
      objective: '并行实现 Room 任务图',
      artifactRefs: ['docs/plan.md'],
      updatedAtMs: 400,
    })]);
    const projection = createRoomProjection('room-sol');
    projection.activityOrder = ['approval-a'];
    projection.activitiesById = {
      'approval-a': {
        id: 'approval-a', sequence: 5, turnId: 'turn-root', participantId: 'p-mars', sourceSessionId: 'session-p-mars',
        kind: 'approval_required', status: 'waiting', summary: '批准发布前检查',
        payload: { sourceEventType: 'approval_required', approvalId: 'approval:a', payloadSha256: 'a'.repeat(64) },
        createdAtMs: 120, updatedAtMs: 120,
      },
    };
    projection.messageOrder = ['message-user'];
    projection.messagesById = {
      'message-user': {
        id: 'message-user', roomId: 'room-sol', turnId: 'turn-root', participantId: null, sourceSessionId: '',
        role: 'user', status: 'completed', text: '请实现主线迁移', projectionKind: 'post',
        mentionedParticipantIds: ['p-earth'], sequence: 4, createdAtMs: 110,
      },
    };

    const focus = buildRoomFocusProjection(withWork, projection);

    expect(focus.flow.map((packet) => [packet.id, packet.kind, packet.sourceParticipantId, packet.targetParticipantIds])).toEqual([
      ['message:message-user', 'request', 'root', ['p-earth']],
      ['activity:approval-a', 'approval', 'p-mars', ['root']],
    ]);
    expect(focus.flow.some((packet) => packet.id.startsWith('work:'))).toBe(false);
    expect(focus.flow.at(-1)?.refs).toEqual([]);
  });

  it('projects only explicitly addressed review requests and conclusions with evidence refs', () => {
    const projection = createRoomProjection('room-sol');
    projection.activityOrder = ['review-request', 'review-conclusion', 'unaddressed-review'];
    projection.activitiesById = {
      'review-request': {
        id: 'review-request', sequence: 5, turnId: 'turn-root', participantId: 'p-earth', sourceSessionId: 'session-earth',
        kind: 'participant_activity', status: 'waiting', summary: '等待 Venus 复核',
        payload: {
          sourceEventType: 'user_input_required', requestKind: 'plan_review', requestId: 'review:request:1',
          sourceParticipantId: 'p-earth', targetParticipantId: 'p-venus', workItemId: 'work-review',
          evidenceRefs: ['trace:request-evidence'],
        },
        createdAtMs: 20, updatedAtMs: 20,
      },
      'review-conclusion': {
        id: 'review-conclusion', sequence: 6, turnId: 'turn-root', participantId: 'p-venus', sourceSessionId: 'session-venus',
        kind: 'participant_activity', status: 'completed', summary: '复核通过',
        payload: {
          sourceEventType: 'review_concluded', requestId: 'review:request:1',
          sourceParticipantId: 'p-venus', targetParticipantId: 'p-earth', workItemId: 'work-review',
          evidenceRefs: ['trace:conclusion-evidence'], reviewState: 'accepted',
        },
        createdAtMs: 21, updatedAtMs: 21,
      },
      'unaddressed-review': {
        id: 'unaddressed-review', sequence: 7, turnId: 'turn-root', participantId: 'p-earth', sourceSessionId: 'session-earth',
        kind: 'participant_activity', status: 'waiting', summary: '等待复核',
        payload: { requestKind: 'plan_review', requestId: 'review:unaddressed' },
        createdAtMs: 22, updatedAtMs: 22,
      },
    };

    const focus = buildRoomFocusProjection(room(), projection);
    expect(focus.flow.filter((packet) => packet.kind === 'review')).toEqual([
      expect.objectContaining({
        id: 'activity:review-request', sourceParticipantId: 'p-earth', targetParticipantIds: ['p-venus'],
        status: 'waiting', refs: ['trace:request-evidence'], workItemId: 'work-review',
      }),
      expect.objectContaining({
        id: 'activity:review-conclusion', sourceParticipantId: 'p-venus', targetParticipantIds: ['p-earth'],
        status: 'completed', refs: ['trace:conclusion-evidence'], workItemId: 'work-review',
      }),
    ]);
    expect(focus.flow).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'activity:unaddressed-review' }),
    ]));
  });

  it('projects the real WorkItem producer phases with actor/owner provenance and nested refs', () => {
    const producerWork = {
      id: 'work-child',
      parentWorkId: 'work-root',
      createdByParticipantId: 'p-earth',
      accountableParticipantId: 'p-venus',
      currentOwnerParticipantId: 'p-mars',
      artifactRefs: ['artifact:room-ui'],
      evidenceRefs: ['trace:room-ui'],
    };
    const projection = createRoomProjection('room-sol');
    projection.activityOrder = ['work-submitted', 'work-completed', 'work-returned'];
    projection.activitiesById = {
      'work-submitted': {
        id: 'work-submitted', sequence: 10, turnId: 'turn-root', participantId: 'p-mars', sourceSessionId: 'session-mars',
        kind: 'participant_activity', status: 'completed', summary: 'producer submitted',
        payload: { activityKind: 'work', phase: 'submitted', workItemId: 'work-child', work: producerWork },
        createdAtMs: 10, updatedAtMs: 10,
      },
      'work-completed': {
        id: 'work-completed', sequence: 11, turnId: 'turn-root', participantId: 'p-venus', sourceSessionId: 'session-venus',
        kind: 'participant_activity', status: 'completed', summary: 'producer completed',
        payload: { activityKind: 'work', phase: 'completed', workItemId: 'work-child', work: producerWork },
        createdAtMs: 11, updatedAtMs: 11,
      },
      'work-returned': {
        id: 'work-returned', sequence: 12, turnId: 'turn-root', participantId: 'p-venus', sourceSessionId: 'session-venus',
        kind: 'participant_activity', status: 'completed', summary: 'producer returned',
        payload: { activityKind: 'work', phase: 'returned', workItemId: 'work-child', work: producerWork },
        createdAtMs: 12, updatedAtMs: 12,
      },
    };

    const focus = buildRoomFocusProjection(room(), projection);

    expect(focus.flow.filter((packet) => packet.kind === 'review')).toEqual([
      expect.objectContaining({
        id: 'activity:work-submitted', sourceParticipantId: 'p-mars', targetParticipantIds: ['p-earth'],
        status: 'waiting', workItemId: 'work-child', refs: ['artifact:room-ui', 'trace:room-ui'],
      }),
      expect.objectContaining({
        id: 'activity:work-completed', sourceParticipantId: 'p-venus', targetParticipantIds: ['p-mars'],
        status: 'completed', workItemId: 'work-child', refs: ['artifact:room-ui', 'trace:room-ui'],
      }),
      expect.objectContaining({
        id: 'activity:work-returned', sourceParticipantId: 'p-venus', targetParticipantIds: ['p-mars'],
        status: 'waiting', workItemId: 'work-child', refs: ['artifact:room-ui', 'trace:room-ui'],
      }),
    ]);

    const mesh = buildRoomFocusMesh(focus);
    expect(mesh.edges).toEqual([
      expect.objectContaining({
        kind: 'review', sourceId: 'partner:p-venus', targetId: 'partner:p-mars', state: 'completed',
        provenance: expect.objectContaining({
          eventIds: ['activity:work-completed'], workItemIds: ['work-child'],
          refs: ['artifact:room-ui', 'trace:room-ui'],
        }),
      }),
    ]);
    expect(mesh.nonDagRelations).toEqual(expect.arrayContaining([
      expect.objectContaining({
        kind: 'review', sourceId: 'partner:p-mars', targetId: 'partner:p-earth', state: 'waiting',
        provenance: expect.objectContaining({ eventIds: ['activity:work-submitted'] }),
      }),
      expect.objectContaining({
        kind: 'review', sourceId: 'partner:p-venus', targetId: 'partner:p-mars', state: 'waiting',
        provenance: expect.objectContaining({ eventIds: ['activity:work-returned'] }),
      }),
    ]));
  });

  it.each([
    { label: 'dispatch（activity.kind）', kind: 'dispatch', payload: { targetParticipantId: 'p-earth' }, expectedKind: 'dispatch', project: true },
    { label: 'route_decision（activity.kind）', kind: 'route_decision', payload: { targetParticipantId: 'p-earth' }, expectedKind: 'dispatch', project: true },
    { label: 'intercom（payload.activityKind）', kind: 'participant_activity', payload: { activityKind: 'intercom', targetParticipantId: 'p-earth' }, expectedKind: 'intercom', project: true },
    { label: 'ContextRef 移交', kind: 'participant_activity', payload: { contextRefs: ['context://room-sol/brief'], targetParticipantId: 'p-earth' }, expectedKind: 'context', project: true },
    { label: '未知 activity 不投影', kind: 'participant_activity', payload: { sourceEventType: 'not_a_flow_event', targetParticipantId: 'p-earth' }, expectedKind: 'dispatch', project: false },
    { label: '无目标 dispatch 不投影', kind: 'dispatch', payload: {}, participantId: null, expectedKind: 'dispatch', project: false },
  ] as const)('projects $label into the ledger only when it is an authoritative targeted transfer', ({ kind, payload, participantId = 'p-mars', expectedKind, project }) => {
    const projection = createRoomProjection('room-sol');
    projection.activityOrder = ['activity-1'];
    projection.activitiesById = {
      'activity-1': {
        id: 'activity-1', turnId: 'turn-root', participantId, sourceSessionId: 'session-x',
        kind, status: 'completed', summary: 'activity-1', payload: payload as Record<string, unknown>,
        createdAtMs: 200, updatedAtMs: 200,
      },
    };

    const focus = buildRoomFocusProjection(room(), projection);
    const packet = focus.flow.find((candidate) => candidate.id === 'activity:activity-1');

    if (project) {
      expect(packet).toEqual(expect.objectContaining({ kind: expectedKind, targetParticipantIds: ['p-earth'] }));
    } else {
      expect(packet).toBeUndefined();
    }
  });

  it.each([
    {
      label: 'current_progress',
      kind: 'participant_activity',
      participantId: 'p-earth',
      payload: {
        sourceEventType: 'current_progress',
        targetParticipantId: 'p-mars',
        contextRefs: ['doc:progress-notice'],
      },
    },
    {
      label: 'tool activity',
      kind: 'tool',
      participantId: 'p-earth',
      payload: {
        targetParticipantId: 'p-mars',
        toolName: 'read_file',
      },
    },
    {
      label: 'source-less handoff',
      kind: 'participant_activity',
      participantId: '',
      payload: {
        sourceEventType: 'handoff',
        targetParticipantId: 'p-mars',
      },
    },
  ] as const)('does not turn a $label target into a handoff', ({ kind, participantId, payload }) => {
    const projection = createRoomProjection('room-sol');
    projection.activityOrder = ['activity-1'];
    projection.activitiesById = {
      'activity-1': {
        id: 'activity-1', turnId: 'turn-root', participantId, sourceSessionId: 'session-earth',
        kind, status: 'completed', summary: '普通运行记录', payload,
        createdAtMs: 200, updatedAtMs: 200,
      },
    };

    const focus = buildRoomFocusProjection(room(), projection);

    expect(focus.handoffs).toEqual([]);
  });

  it.each(['handoff', 'reassigned', 'transfer', 'offer'] as const)(
    'projects an explicit %s event as a handoff',
    (sourceEventType) => {
      const projection = createRoomProjection('room-sol');
      projection.activityOrder = ['activity-1'];
      projection.activitiesById = {
        'activity-1': {
          id: 'activity-1', turnId: 'turn-root', participantId: 'p-earth', sourceSessionId: 'session-earth',
          kind: 'participant_activity', status: 'completed', summary: `${sourceEventType} Room 工作`,
          payload: { sourceEventType, sourceParticipantId: 'p-earth', targetParticipantId: 'p-mars', task: '恢复验收' },
          createdAtMs: 200, updatedAtMs: 200,
        },
      };

      const focus = buildRoomFocusProjection(room(), projection);

      expect(focus.handoffs).toContainEqual(expect.objectContaining({
        id: 'activity:activity-1',
        sourceParticipantId: 'p-earth',
        targetParticipantId: 'p-mars',
        task: '恢复验收',
        state: 'completed',
      }));
      expect(focus.flow).toEqual([]);
    },
  );

  it('projects the real work reassignment activity from its nested WorkItem target', () => {
    const projection = createRoomProjection('room-sol');
    projection.activityOrder = ['activity-1'];
    projection.activitiesById = {
      'activity-1': {
        id: 'activity-1', turnId: 'turn-root', participantId: 'p-earth', sourceSessionId: 'session-earth',
        kind: 'participant_activity', status: 'completed', summary: '已重新分配工作',
        payload: {
          activityKind: 'work',
          phase: 'reassigned',
          work: {
            currentOwnerParticipantId: 'p-mars',
            offeredToParticipantId: '',
            objective: '恢复 Room 验收',
          },
        },
        createdAtMs: 200, updatedAtMs: 200,
      },
    };

    const focus = buildRoomFocusProjection(room(), projection);

    expect(focus.handoffs).toContainEqual(expect.objectContaining({
      id: 'activity:activity-1',
      sourceParticipantId: 'p-earth',
      targetParticipantId: 'p-mars',
      task: '恢复 Room 验收',
    }));
  });

  it('uses the parsed dispatch plan target when selectedParticipantIds is the only target signal', () => {
    const projection = createRoomProjection('room-sol');
    projection.activityOrder = ['dispatch-selected'];
    projection.activitiesById = {
      'dispatch-selected': {
        id: 'dispatch-selected', turnId: 'turn-root', participantId: 'p-earth', sourceSessionId: 'session-earth',
        kind: 'route_decision', status: 'completed', summary: '已选择并行伙伴',
        payload: {
          sourceEventType: 'route_decision',
          dispatchId: 'dispatch-selected',
          selectedParticipantIds: ['p-mars'],
          targetDisplayName: 'Agent 2',
          reason: 'parallel',
        },
        createdAtMs: 200, updatedAtMs: 200,
      },
    };

    const focus = buildRoomFocusProjection(room(), projection);
    const packet = focus.flow.find((candidate) => candidate.id === 'activity:dispatch-selected');

    expect(packet).toEqual(expect.objectContaining({
      kind: 'dispatch',
      targetParticipantIds: ['p-mars'],
      dispatchPlan: expect.objectContaining({ targetParticipantId: 'p-mars' }),
    }));
  });

  it('keeps an unknown dispatch target neutral instead of exposing its Runtime display name', () => {
    const projection = createRoomProjection('room-sol');
    projection.activityOrder = ['dispatch-unknown-target'];
    projection.activitiesById = {
      'dispatch-unknown-target': {
        id: 'dispatch-unknown-target', turnId: 'turn-root', participantId: 'p-earth', sourceSessionId: 'session-earth',
        kind: 'route_decision', status: 'completed', summary: '已确定本轮分工',
        payload: {
          sourceEventType: 'route_decision', dispatchId: 'dispatch-unknown-target',
          targetParticipantId: 'p-unknown', targetDisplayName: '不应出现的目标人名', reason: 'parallel',
        },
        createdAtMs: 200, updatedAtMs: 200,
      },
    };

    const packet = buildRoomFocusProjection(room(), projection).flow
      .find((candidate) => candidate.id === 'activity:dispatch-unknown-target');

    expect(packet?.summary).toContain('协作行星');
    expect(packet?.summary).not.toContain('不应出现的目标人名');
  });

  it('keeps a structural WorkItem offer in task detail without fabricating a handoff', () => {
    const blocked = work({
      id: 'work-blocked',
      objective: '恢复前端验收',
      currentOwnerParticipantId: 'p-earth',
      offeredToParticipantId: 'p-mars',
      accountableParticipantId: 'p-venus',
      state: 'blocked',
      blocker: { reason: '等待真实 Browser 权限', nextStep: '恢复连接后重跑前台验收' },
      updatedAtMs: 18,
    });

    const focus = buildRoomFocusProjection(room([blocked]), createRoomProjection('room-sol'));

    expect(focus.goal.state).toBe('blocked');
    expect(focus.workItems[0]?.blocker).toEqual({
      reason: '等待真实 Browser 权限',
      nextStep: '恢复连接后重跑前台验收',
    });
    expect(focus.handoffs).toEqual([]);
    expect(focus.flow).toEqual([]);
  });

  it('projects current Room focus from the latest public root without leaking historical failures', () => {
    const historical = work({
      id: 'work-historical',
      objective: '上一轮失败任务',
      rootTurnId: 'turn-old',
      currentOwnerParticipantId: 'p-earth',
      accountableParticipantId: 'p-earth',
      state: 'blocked',
      resultSummary: '上一轮旧结果',
      evidenceRefs: ['evidence:old'],
      blocker: { reason: '上一轮阻塞' },
      updatedAtMs: 20,
    });
    const current = work({
      id: 'work-current',
      objective: '本轮继续修复 Room',
      rootTurnId: 'turn-current',
      currentOwnerParticipantId: 'p-mars',
      accountableParticipantId: 'p-mars',
      state: 'active',
      evidenceRefs: ['evidence:current'],
      updatedAtMs: 40,
    });
    const projection = createRoomProjection('room-sol');
    projection.turnOrder = ['turn-old', 'turn-current'];
    projection.turnsById = {
      'turn-old': {
        id: 'turn-old', rootId: 'turn-old', status: 'failed',
        messageIds: ['message-old'], activityIds: ['activity-old'], participantIds: ['p-earth'],
        failedParticipantIds: ['p-earth'], createdAtMs: 10, updatedAtMs: 20,
      },
      'turn-current': {
        id: 'turn-current', rootId: 'turn-current', status: 'running',
        messageIds: ['message-current'], activityIds: ['activity-current'], participantIds: ['p-mars'],
        createdAtMs: 30, updatedAtMs: 40,
      },
    };
    projection.messageOrder = ['message-old', 'message-current'];
    projection.messagesById = {
      'message-old': {
        id: 'message-old', roomId: 'room-sol', turnId: 'turn-old', participantId: 'p-earth',
        sourceSessionId: 'session-p-earth', role: 'assistant', status: 'completed',
        postKind: 'result', text: '上一轮旧回执', createdAtMs: 20,
      },
      'message-current': {
        id: 'message-current', roomId: 'room-sol', turnId: 'turn-current', participantId: 'p-mars',
        sourceSessionId: 'session-p-mars', role: 'assistant', status: 'streaming',
        postKind: 'progress', text: '本轮正在继续', createdAtMs: 40,
      },
    };
    projection.activityOrder = ['activity-old', 'activity-current'];
    projection.activitiesById = {
      'activity-old': {
        id: 'activity-old', turnId: 'turn-old', participantId: 'p-earth', sourceSessionId: 'session-p-earth',
        kind: 'participant_activity', status: 'failed', summary: '上一轮失败进展',
        payload: { sourceEventType: 'handoff', sourceParticipantId: 'p-earth', targetParticipantId: 'p-mars' },
        createdAtMs: 19, updatedAtMs: 20,
      },
      'activity-current': {
        id: 'activity-current', turnId: 'turn-current', participantId: 'p-mars', sourceSessionId: 'session-p-mars',
        kind: 'participant_activity', status: 'running', summary: '本轮正在修复',
        payload: { sourceEventType: 'current_progress' }, createdAtMs: 39, updatedAtMs: 40,
      },
    };

    const focus = buildRoomFocusProjection(room([historical, current]), projection);

    expect(focus.goal).toMatchObject({ rootId: 'turn-current', state: 'running' });
    expect(focus.goal.rootResult).toBeUndefined();
    expect(focus.workItems.map((item) => item.id)).toEqual(['work-current']);
    expect(focus.rootEvidence).toEqual([{ ref: 'evidence:current', kind: 'evidence' }]);
    expect(focus.counts).toEqual({ active: 1, review: 0, blocked: 0, completed: 0 });
    expect(focus.partners.find((partner) => partner.participantId === 'p-earth')).toMatchObject({
      state: 'idle',
      currentAction: '等待新的工作项',
    });
    expect(focus.partners.find((partner) => partner.participantId === 'p-mars')).toMatchObject({
      state: 'running',
      currentAction: '本轮正在修复',
      latestReceipt: '本轮正在继续',
    });
    expect(focus.handoffs).toEqual([]);
    expect(focus.flow.map((packet) => packet.id)).toEqual([
      'message:message-current',
    ]);
  });

  it.each([
    { turnStatus: 'running' as const, expected: 'running' as const },
    { turnStatus: 'completed' as const, expected: 'completed' as const },
  ])('does not let a recoverable failed tool receipt override an authoritative $turnStatus turn', ({ turnStatus, expected }) => {
    const projection = createRoomProjection('room-sol');
    projection.turnOrder = ['turn-root'];
    projection.turnsById = {
      'turn-root': {
        id: 'turn-root',
        rootId: 'turn-root',
        status: turnStatus,
        messageIds: [],
        activityIds: ['tool-failed'],
        participantIds: ['p-mars'],
        ...(turnStatus === 'completed' ? { terminalParticipantIds: ['p-mars'] } : {}),
        createdAtMs: 10,
        updatedAtMs: 20,
      },
    };
    projection.activityOrder = ['tool-failed'];
    projection.activitiesById = {
      'tool-failed': {
        id: 'tool-failed',
        turnId: 'turn-root',
        participantId: 'p-mars',
        sourceSessionId: 'session-p-mars',
        kind: 'tool',
        status: 'failed',
        summary: '工具调用失败，但 Pi 已继续本轮',
        payload: { toolName: 'read_file', error: 'temporary failure' },
        createdAtMs: 19,
        updatedAtMs: 20,
      },
    };

    const focus = buildRoomFocusProjection(room(), projection);

    expect(focus.partners.find((partner) => partner.participantId === 'p-mars')).toMatchObject({
      state: expected,
      currentAction: '工具调用失败，但 Pi 已继续本轮',
    });
  });

  it('keeps an authoritative participant failure visible despite a recoverable activity receipt', () => {
    const projection = createRoomProjection('room-sol');
    projection.turnOrder = ['turn-root'];
    projection.turnsById = {
      'turn-root': {
        id: 'turn-root',
        rootId: 'turn-root',
        status: 'running',
        messageIds: [],
        activityIds: ['tool-failed'],
        participantIds: ['p-mars'],
        failedParticipantIds: ['p-mars'],
        createdAtMs: 10,
        updatedAtMs: 20,
      },
    };
    projection.activityOrder = ['tool-failed'];
    projection.activitiesById = {
      'tool-failed': {
        id: 'tool-failed',
        turnId: 'turn-root',
        participantId: 'p-mars',
        sourceSessionId: 'session-p-mars',
        kind: 'tool',
        status: 'failed',
        summary: '子步骤失败',
        payload: { toolName: 'read_file', error: 'permanent failure' },
        createdAtMs: 19,
        updatedAtMs: 20,
      },
    };

    const focus = buildRoomFocusProjection(room(), projection);

    expect(focus.partners.find((partner) => partner.participantId === 'p-mars')?.state).toBe('failed');
  });

  it('lets a participant terminal receipt override a lagging active WorkItem without completing the Room Root', () => {
    const marsWork = work({
      id: 'work-mars',
      objective: '验证 Mars 的终态回执',
      currentOwnerParticipantId: 'p-mars',
      accountableParticipantId: 'p-mars',
      state: 'active',
      resultSummary: 'Mars 已提交实现结果',
    });
    const projection = createRoomProjection('room-sol');
    projection.turnOrder = ['turn-root'];
    projection.turnsById = {
      'turn-root': {
        id: 'turn-root',
        rootId: 'turn-root',
        status: 'running',
        messageIds: [],
        activityIds: [],
        participantIds: ['p-earth', 'p-mars'],
        terminalParticipantIds: ['p-mars'],
        createdAtMs: 10,
        updatedAtMs: 20,
      },
    };

    const focus = buildRoomFocusProjection(room([marsWork]), projection);

    expect(focus.partners.find((partner) => partner.participantId === 'p-mars')?.state).toBe('completed');
    expect(focus.goal.state).toBe('running');
    expect(focus.goal.rootResult).toBeUndefined();
  });
});
