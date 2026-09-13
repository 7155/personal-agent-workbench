import { describe, expect, it } from 'vitest';
import {
  appendOptimisticRoomMessage,
  createRoomProjection,
  reduceRoomEvent,
  type RoomProjectionState,
} from '@/contracts/room-reducer';
import type { RoomParticipant, RoomSummary, RoomWorkItem } from '@/features/rooms/room-types';
import { parseRoomEvent } from '@/contracts/validators';
import { selectRoomRoundTaskSheets } from './room-round-task-sheet';

describe('selectRoomRoundTaskSheets (UR-170/172)', () => {
  it.each(['facilitator', 'repeated_failure_signature'])('keeps %s in execution metadata instead of the current assignment', (reason) => {
    const projection = runningProjection('工具连续失败，已停止本轮');
    projection.activitiesById['activity-earth']!.payload = { ...projection.activitiesById['activity-earth']!.payload, task: '', reason };
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    const row = selectRoomRoundTaskSheets(room, projection)[0]!.rows[0]!;
    expect(row.task).toBe('完成 Room 任务表');
    expect(row.latestProgress).toBe('工具连续失败，已停止本轮');
    expect(projection.activitiesById['activity-earth']!.payload.reason).toBe(reason);
  });

  it('retains the assigned task when a newer failure event only carries a reason', () => {
    const projection = runningProjection('正在工作');
    projection.activitiesById['failure'] = { ...projection.activitiesById['activity-earth']!, id: 'failure', updatedAtMs: 4, payload: { ...projection.activitiesById['activity-earth']!.payload, task: '', reason: 'repeated_failure_signature' } };
    projection.turnsById['turn-1']!.activityIds.push('failure');
    projection.activityOrder.push('failure');
    const row = selectRoomRoundTaskSheets(roomWith([participant('participant-earth', 'session-earth', 0)]), projection)[0]!.rows[0]!;
    expect(row.task).toBe('检查 Room 事件投影');
  });

  it('keeps one round and planet row identity when the local turn is accepted authoritatively', () => {
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
    ]);
    let projection = appendOptimisticRoomMessage(createRoomProjection('room-a'), {
      clientMessageId: 'client-round-identity',
      text: '保持这一轮不重挂载',
      nowMs: 1,
    });
    const optimistic = selectRoomRoundTaskSheets(room, projection);

    projection = reduceRoomEvent(projection, parseRoomEvent({
      schemaVersion: 'rag-ime.agent-room-event.v1',
      eventId: 'room-a:1',
      roomId: 'room-a',
      sequence: 1,
      turnId: 'room-turn-authoritative',
      eventType: 'user_message',
      participantId: null,
      sourceSessionId: '',
      createdAtMs: 2,
      payload: {
        messageId: 'room-user-authoritative',
        clientMessageId: 'client-round-identity',
        rootId: 'room-turn-authoritative',
        text: '保持这一轮不重挂载',
      },
      resumeToken: 'room-a:1',
    })).state;
    const accepted = selectRoomRoundTaskSheets(room, projection);

    expect(optimistic[0]?.id).toBe('local-room-turn:client-round-identity');
    expect(accepted[0]).toMatchObject({
      id: optimistic[0]?.id,
      turnId: 'room-turn-authoritative',
    });
    expect(accepted[0]?.rows[0]?.key).toBe(
      optimistic[0]?.rows[0]?.key,
    );

    const retried = reduceRoomEvent(projection, parseRoomEvent({
      schemaVersion: 'rag-ime.agent-room-event.v1',
      eventId: 'room-a:2',
      roomId: 'room-a',
      sequence: 2,
      turnId: 'room-turn-retry',
      eventType: 'user_message',
      participantId: null,
      sourceSessionId: '',
      createdAtMs: 3,
      payload: {
        messageId: 'room-user-retry',
        clientMessageId: 'client-round-retry',
        rootId: 'room-turn-retry',
        retryOfRootId: 'room-turn-authoritative',
        text: '重试仍属于这一轮',
      },
      resumeToken: 'room-a:2',
    })).state;
    const retrySheet = selectRoomRoundTaskSheets(room, retried);
    expect(retrySheet).toHaveLength(1);
    expect(retrySheet[0]).toMatchObject({ id: optimistic[0]?.id, turnId: 'room-turn-retry' });

    const newRound = reduceRoomEvent(retried, parseRoomEvent({
      schemaVersion: 'rag-ime.agent-room-event.v1',
      eventId: 'room-a:3',
      roomId: 'room-a',
      sequence: 3,
      turnId: 'room-turn-new',
      eventType: 'user_message',
      participantId: null,
      sourceSessionId: '',
      createdAtMs: 4,
      payload: {
        messageId: 'room-user-new',
        clientMessageId: 'client-round-new',
        rootId: 'room-turn-new',
        text: '这是新的一轮',
      },
      resumeToken: 'room-a:3',
    })).state;
    expect(selectRoomRoundTaskSheets(room, newRound).map((sheet) => sheet.id)).toEqual([
      optimistic[0]?.id,
      'room-turn-new',
    ]);
  });

  it('projects one user input into one stable row per planet and updates progress in place', () => {
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
    ]);
    const projection = runningProjection('正在检查 Room 事件投影');

    const first = selectRoomRoundTaskSheets(room, projection);

    expect(first).toHaveLength(1);
    expect(first[0]).toMatchObject({
      id: 'turn-1',
      objective: '完成 Room 任务表',
      status: 'running',
    });
    expect(first[0]?.rows.map((row) => row.key)).toEqual([
      'turn-1:participant-earth',
      'turn-1:participant-mars',
    ]);
    expect(first[0]?.rows[0]).toMatchObject({
      participantId: 'participant-earth',
      celestialName: 'Earth',
      task: '检查 Room 事件投影',
      state: 'running',
      latestProgress: '正在检查 Room 事件投影',
    });
    expect(first[0]?.rows[1]).toMatchObject({
      participantId: 'participant-mars',
      celestialName: 'Mars',
      state: 'waiting',
    });

    const updated: RoomProjectionState = {
      ...projection,
      activitiesById: {
        ...projection.activitiesById,
        'activity-earth': {
          ...projection.activitiesById['activity-earth']!,
          summary: '已经完成 Room 事件投影',
          status: 'completed',
          updatedAtMs: 4,
        },
      },
      turnsById: {
        ...projection.turnsById,
        'turn-1': {
          ...projection.turnsById['turn-1']!,
          terminalParticipantIds: ['participant-earth'],
          updatedAtMs: 4,
        },
      },
    };

    const second = selectRoomRoundTaskSheets(room, updated);

    expect(second).toHaveLength(1);
    expect(second[0]?.rows.map((row) => row.key)).toEqual(first[0]?.rows.map((row) => row.key));
    expect(second[0]?.rows[0]).toMatchObject({
      state: 'running',
      latestProgress: '已经完成 Room 事件投影',
    });
  });

  it('carries a coordinator progress post kind without calling it a final result', () => {
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    const row = selectRoomRoundTaskSheets(
      room,
      coordinatorPostProjection('progress'),
    )[0]?.rows[0];

    expect(row).toMatchObject({
      state: 'completed',
      postKind: 'progress',
      latestProgress: '主控正在整理汇报',
      report: '主控正在整理汇报',
    });
    expect(row?.result).toBeUndefined();
    expect(row?.finalMessageId).toBeUndefined();
  });

  it('carries a moderator result post kind as the explicit final identity', () => {
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    const row = selectRoomRoundTaskSheets(
      room,
      coordinatorPostProjection('result'),
    )[0]?.rows[0];

    expect(row).toMatchObject({
      state: 'completed',
      postKind: 'result',
      latestProgress: '主控最终汇报',
      result: '主控最终汇报',
      finalMessageId: 'assistant-coordinator-result',
    });
  });

  it.each([
    { turnId: 'turn-previous', rootId: 'turn-1' },
    { turnId: 'turn-1', rootId: 'turn-previous' },
  ])('rejects a result from another turn or root even when its id is indexed in the current turn: %j', (identity) => {
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    const projection = coordinatorPostProjection('result');
    Object.assign(projection.messagesById['assistant-coordinator-result']!, identity);

    const row = selectRoomRoundTaskSheets(room, projection)[0]?.rows[0];

    expect(row?.report).toBeUndefined();
    expect(row?.result).toBeUndefined();
    expect(row?.finalMessageId).toBeUndefined();
  });

  it('uses typed public answer blocks without falling back to flattened reasoning, tools or private text', () => {
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    const projection = coordinatorPostProjection('result');
    const message = projection.messagesById['assistant-coordinator-result']!;
    message.text = '过期的扁平摘要：Planning test seam / 命令退出码 0';
    message.message = {
      schemaVersion: 'rag-ime.agent-message.v1', id: message.id, sessionId: 'session-earth',
      turnId: 'session-turn-1', role: 'assistant', status: 'completed', createdAtMs: 5,
      attachments: [], citations: [],
      blocks: [
        { id: 'reasoning', type: 'reasoning_summary', presentationKind: 'markdown', status: 'completed', data: { text: 'Planning test seam' } },
        { id: 'legacy-reasoning', type: 'text', presentationKind: 'reasoning_summary', status: 'completed', data: { text: 'Checking a tool receipt' } },
        { id: 'tool', type: 'tool_result', presentationKind: 'tool_result', status: 'completed', data: { text: '命令退出码 0' } },
        { id: 'private', type: 'text', presentationKind: 'markdown', visibility: 'private_session', status: 'completed', data: { text: '仅在私有 Session 可见' } },
        { id: 'answer', type: 'text', presentationKind: 'markdown', visibility: 'root_post', status: 'completed', data: { text: '本轮正式结果正文。' } },
      ],
    };

    expect(selectRoomRoundTaskSheets(room, projection)[0]?.rows[0]).toMatchObject({
      report: '本轮正式结果正文。', result: '本轮正式结果正文。', finalMessageId: message.id,
    });

    message.message.blocks = message.message.blocks.filter((block) => block.id !== 'answer');
    const withoutAnswer = selectRoomRoundTaskSheets(room, projection)[0]?.rows[0];
    expect(withoutAnswer?.report).toBeUndefined();
    expect(withoutAnswer?.result).toBeUndefined();
    expect(withoutAnswer?.finalMessageId).toBeUndefined();
  });

  it('does not poison a running planet row after a recoverable tool failure', () => {
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
    ]);
    const base = runningProjection('第一次工具调用失败');
    const failed = base.activitiesById['activity-earth']!;
    const projection: RoomProjectionState = {
      ...base,
      turnsById: {
        ...base.turnsById,
        'turn-1': {
          ...base.turnsById['turn-1']!,
          activityIds: ['activity-earth', 'activity-earth-recovered'],
          updatedAtMs: 5,
        },
      },
      activitiesById: {
        ...base.activitiesById,
        'activity-earth': {
          ...failed,
          status: 'failed',
          summary: '读取文件失败，正在改用正确路径',
          updatedAtMs: 3,
        },
        'activity-earth-recovered': {
          ...failed,
          id: 'activity-earth-recovered',
          status: 'running',
          summary: '已恢复，继续构建结果',
          createdAtMs: 4,
          updatedAtMs: 5,
        },
      },
      activityOrder: ['activity-earth', 'activity-earth-recovered'],
    };

    const row = selectRoomRoundTaskSheets(room, projection)[0]?.rows[0];

    expect(row).toMatchObject({
      state: 'running',
      latestProgress: '已恢复，继续构建结果',
    });
    expect(row?.history.map((event) => event.summary)).toEqual([
      '读取文件失败，正在改用正确路径',
      '已恢复，继续构建结果',
    ]);
  });

  it('keeps retries inside the original round and orders distinct user rounds without interleaving them', () => {
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
    ]);
    const first = runningProjection('第一轮进展');
    const projection: RoomProjectionState = {
      ...first,
      turnOrder: ['turn-1', 'turn-1-retry', 'turn-2'],
      turnsById: {
        ...first.turnsById,
        'turn-1': { ...first.turnsById['turn-1']!, status: 'failed', failure: '需要重试' },
        'turn-1-retry': {
          id: 'turn-1-retry',
          rootId: 'turn-1-retry',
          retryOfRootId: 'turn-1',
          status: 'completed',
          messageIds: ['user-retry'],
          activityIds: [],
          participantIds: ['participant-earth'],
          terminalParticipantIds: ['participant-earth'],
          createdAtMs: 5,
          updatedAtMs: 6,
        },
        'turn-2': {
          id: 'turn-2',
          rootId: 'turn-2',
          status: 'queued',
          messageIds: ['user-2'],
          activityIds: [],
          participantIds: [],
          createdAtMs: 7,
          updatedAtMs: 7,
        },
      },
      messagesById: {
        ...first.messagesById,
        'user-retry': userMessage('user-retry', 'turn-1-retry', '再次完成 Room 任务表', 5),
        'user-2': userMessage('user-2', 'turn-2', '检查第二轮', 7),
      },
      messageOrder: [...first.messageOrder, 'user-retry', 'user-2'],
    };

    const sheets = selectRoomRoundTaskSheets(room, projection);

    expect(sheets.map((sheet) => sheet.id)).toEqual(['turn-1', 'turn-2']);
    expect(sheets[0]).toMatchObject({ turnId: 'turn-1-retry', objective: '再次完成 Room 任务表' });
    expect(sheets[0]?.rows[0]?.key).toBe('turn-1:participant-earth');
    expect(sheets[1]).toMatchObject({ turnId: 'turn-2', objective: '检查第二轮' });
  });

  it('uses explicit retry lineage as the sheet id when the parent is outside a truncated snapshot', () => {
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
    ]);
    const base = runningProjection('retry-only progress');
    const projection: RoomProjectionState = {
      ...base,
      turnOrder: ['turn-retry-only', 'turn-new'],
      turnsById: {
        'turn-retry-only': {
          id: 'turn-retry-only',
          rootId: 'turn-retry-only',
          retryOfRootId: 'turn-original-outside-snapshot',
          status: 'running',
          messageIds: ['user-retry-only'],
          activityIds: [],
          participantIds: [],
          createdAtMs: 5,
          updatedAtMs: 6,
        },
        'turn-new': {
          id: 'turn-new',
          rootId: 'turn-new',
          status: 'queued',
          messageIds: ['user-new'],
          activityIds: [],
          participantIds: [],
          createdAtMs: 7,
          updatedAtMs: 7,
        },
      },
      messagesById: {
        'user-retry-only': userMessage('user-retry-only', 'turn-retry-only', '原始任务的重试', 5),
        'user-new': userMessage('user-new', 'turn-new', '完全新的用户任务', 7),
      },
      messageOrder: ['user-retry-only', 'user-new'],
      activitiesById: {},
      activityOrder: [],
    };

    const sheets = selectRoomRoundTaskSheets(room, projection);

    expect(sheets.map((sheet) => sheet.id)).toEqual([
      'turn-original-outside-snapshot',
      'turn-new',
    ]);
    expect(sheets[0]).toMatchObject({
      turnId: 'turn-retry-only',
      objective: '原始任务的重试',
    });
    expect(sheets[0]?.rows[0]?.key).toBe('turn-original-outside-snapshot:participant-earth');
    expect(sheets[1]).toMatchObject({
      turnId: 'turn-new',
      objective: '完全新的用户任务',
    });
    expect(sheets[1]?.rows[0]?.key).toBe('turn-new:participant-earth');
  });

  it('keeps the original attempt history and evidence in the same retry row', () => {
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
    ]);
    room.workItems = [
      workItem('work-original', 'turn-1', '原尝试结果', ['evidence-original'], 4),
      workItem('work-retry', 'turn-1-retry', '重试结果', ['evidence-retry'], 8),
    ];
    const first = runningProjection('原尝试进展');
    const projection: RoomProjectionState = {
      ...first,
      turnOrder: ['turn-1', 'turn-1-retry'],
      turnsById: {
        ...first.turnsById,
        'turn-1': {
          ...first.turnsById['turn-1']!,
          status: 'failed',
          failure: '原尝试失败',
          updatedAtMs: 4,
          messageIds: ['user-1', 'assistant-original'],
        },
        'turn-1-retry': {
          id: 'turn-1-retry',
          rootId: 'turn-1-retry',
          retryOfRootId: 'turn-1',
          status: 'completed',
          messageIds: ['user-retry', 'assistant-retry'],
          activityIds: ['activity-earth-retry'],
          participantIds: ['participant-earth'],
          terminalParticipantIds: ['participant-earth'],
          createdAtMs: 5,
          updatedAtMs: 8,
        },
      },
      activitiesById: {
        ...first.activitiesById,
        'activity-earth': {
          ...first.activitiesById['activity-earth']!,
          status: 'failed',
          summary: '原尝试：发现问题',
          updatedAtMs: 4,
        },
        'activity-earth-retry': {
          id: 'activity-earth-retry',
          turnId: 'turn-1-retry',
          participantId: 'participant-earth',
          sourceSessionId: 'session-earth',
          kind: 'participant_activity',
          status: 'completed',
          summary: '重试：已完成',
          payload: {
            rootId: 'turn-1-retry',
            dispatchId: 'dispatch-retry',
            sourceEventType: 'current_progress',
            task: '重试任务',
          },
          createdAtMs: 6,
          updatedAtMs: 7,
        },
      },
      messagesById: {
        ...first.messagesById,
        'assistant-original': assistantMessage(
          'assistant-original',
          'turn-1',
          '原尝试结果',
          4,
        ),
        'user-retry': userMessage('user-retry', 'turn-1-retry', '重试 Room 任务表', 5),
        'assistant-retry': assistantMessage(
          'assistant-retry',
          'turn-1-retry',
          '重试结果',
          8,
        ),
      },
      messageOrder: [...first.messageOrder, 'assistant-original', 'user-retry', 'assistant-retry'],
      activityOrder: [...first.activityOrder, 'activity-earth-retry'],
    };

    const sheet = selectRoomRoundTaskSheets(room, projection)[0];
    const row = sheet?.rows[0];

    expect(sheet).toMatchObject({ id: 'turn-1', turnId: 'turn-1-retry', status: 'completed' });
    expect(row).toMatchObject({
      key: 'turn-1:participant-earth',
      task: '重试任务',
      state: 'completed',
      report: '重试结果',
      evidenceRefs: ['evidence-original', 'evidence-retry'],
    });
    expect(row?.result).toBeUndefined();
    expect(row?.history.map((event) => event.summary)).toEqual([
      '原尝试：发现问题',
      '原尝试结果',
      '原尝试失败',
      '重试：已完成',
      '重试结果',
    ]);
    expect(sheet?.rows[1]).toMatchObject({
      participantId: 'participant-mars',
      history: [],
    });
  });

  it('keeps a running retry row current instead of surfacing the prior attempt result', () => {
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
    ]);
    room.workItems = [
      workItem('work-original', 'turn-1', '原尝试结果', ['evidence-original'], 4),
      {
        ...workItem('work-retry', 'turn-1-retry', '', ['evidence-retry'], 8),
        state: 'active',
      },
    ];
    const first = runningProjection('原尝试进展');
    const projection: RoomProjectionState = {
      ...first,
      turnOrder: ['turn-1', 'turn-1-retry'],
      turnsById: {
        ...first.turnsById,
        'turn-1': {
          ...first.turnsById['turn-1']!,
          status: 'failed',
          failure: '原尝试失败',
          updatedAtMs: 4,
          messageIds: ['user-1', 'assistant-original'],
        },
        'turn-1-retry': {
          id: 'turn-1-retry',
          rootId: 'turn-1-retry',
          retryOfRootId: 'turn-1',
          status: 'running',
          messageIds: ['user-retry'],
          activityIds: [],
          participantIds: ['participant-earth'],
          createdAtMs: 5,
          updatedAtMs: 9,
        },
      },
      activitiesById: {
        ...first.activitiesById,
        'activity-earth': {
          ...first.activitiesById['activity-earth']!,
          status: 'failed',
          summary: '原尝试：发现问题',
          updatedAtMs: 4,
        },
      },
      messagesById: {
        ...first.messagesById,
        'assistant-original': assistantMessage(
          'assistant-original',
          'turn-1',
          '原尝试结果',
          4,
        ),
        'user-retry': userMessage('user-retry', 'turn-1-retry', '重试 Room 任务表', 5),
      },
      messageOrder: [...first.messageOrder, 'assistant-original', 'user-retry'],
    };

    const row = selectRoomRoundTaskSheets(room, projection)[0]?.rows[0];

    expect(row).toMatchObject({
      key: 'turn-1:participant-earth',
      task: 'work-retry task',
      state: 'running',
      latestProgress: '正在执行，等待公开进展',
      updatedAtMs: 9,
      evidenceRefs: ['evidence-original', 'evidence-retry'],
    });
    expect(row?.result).toBeUndefined();
    expect(row?.history.map((event) => event.summary)).toEqual([
      '原尝试：发现问题',
      '原尝试结果',
      '原尝试失败',
    ]);
  });

  it('projects a real WorkItem blocker as a blocked row with its recovery hint', () => {
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    room.workItems = [{
      id: 'work-blocked', roomId: 'room-a', topicId: '', rootTurnId: 'turn-1', rootWorkId: 'work-blocked',
      parentWorkId: '', objective: '检查 Trace API', expectedOutput: '可验证回执', acceptanceCriteria: ['路由可读'],
      accountableParticipantId: 'participant-earth', currentOwnerParticipantId: 'participant-earth',
      offeredToParticipantId: '', createdByParticipantId: 'participant-earth', clientMessageId: 'client-1',
      state: 'blocked', depth: 1, revision: 0, resultSummary: '', artifactRefs: [], evidenceRefs: [],
      blocker: { reason: '缺少 Runtime 路由', nextStep: '恢复 Gateway 后重试' }, acceptedTurnId: '',
      createdAtMs: 2, updatedAtMs: 4, completedAtMs: null,
    }];

    const row = selectRoomRoundTaskSheets(room, runningProjection('等待 Runtime'))[0]?.rows[0];

    expect(row).toMatchObject({
      state: 'blocked',
      blockedWorkItemId: 'work-blocked',
      latestProgress: '缺少 Runtime 路由',
      blockerReason: '缺少 Runtime 路由',
      blockerNextStep: '恢复 Gateway 后重试',
    });
  });

  it('expands bounded coalesced progress inside the same stable planet row', () => {
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    const projection = runningProjection('正在等待审阅');
    projection.activitiesById['activity-earth'] = {
      ...projection.activitiesById['activity-earth']!,
      payload: {
        ...projection.activitiesById['activity-earth']!.payload,
        progressHistory: [
          { eventId: 'progress-1', status: 'running', summary: '正在整理结果', createdAtMs: 2 },
          { eventId: 'progress-2', status: 'running', summary: '正在等待审阅', createdAtMs: 3 },
        ],
      },
    };

    const row = selectRoomRoundTaskSheets(room, projection)[0]?.rows[0];

    expect(row?.key).toBe('turn-1:participant-earth');
    expect(row?.latestProgress).toBe('正在等待审阅');
    expect(row?.history).toEqual([
      expect.objectContaining({ id: 'progress-1', summary: '正在整理结果' }),
      expect.objectContaining({ id: 'progress-2', summary: '正在等待审阅' }),
    ]);
  });

  it('does not turn facilitator accountability into a second assigned planet', () => {
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
    ]);
    room.workItems = [{
      ...workItem('work-mars', 'turn-1', '', [], 4),
      accountableParticipantId: 'participant-earth',
      currentOwnerParticipantId: 'participant-mars',
      state: 'active',
      completedAtMs: null,
    }];
    const projection = runningProjection('Mars 正在执行唯一任务');
    projection.turnsById['turn-1'] = {
      ...projection.turnsById['turn-1']!,
      participantIds: ['participant-earth', 'participant-mars'],
    };

    const rows = selectRoomRoundTaskSheets(room, projection)[0]?.rows ?? [];

    expect(rows.find((row) => row.participantId === 'participant-earth')?.assigned).toBe(false);
    expect(rows.find((row) => row.participantId === 'participant-mars')).toMatchObject({
      assigned: true,
      state: 'running',
    });
  });

  it('lets a worker done WorkItem override its lagging running turn and progress receipt', () => {
    const room = roomWith([
      participant('participant-earth', 'session-earth', 0),
      participant('participant-mars', 'session-mars', 1),
    ]);
    room.moderatorParticipantId = 'participant-mars';
    room.workItems = [workItem('work-done', 'turn-1', '唯一实现任务已经交付', [], 8)];
    const row = selectRoomRoundTaskSheets(room, runningProjection('当前任务仍在执行'))[0]?.rows[0];

    expect(row).toMatchObject({
      state: 'completed',
      latestProgress: '唯一实现任务已经交付',
      result: '唯一实现任务已经交付',
    });
  });

  it('lets a latest terminal turn override stale queued WorkItem and running activity state', () => {
    const room = roomWith([participant('participant-earth', 'session-earth', 0)]);
    room.workItems = [{
      ...workItem('work-stale-queued', 'turn-1', '', [], 8),
      state: 'queued',
      completedAtMs: null,
    }];
    const projection = runningProjection('历史运行进度不应覆盖终态');
    projection.turnsById['turn-1'] = {
      ...projection.turnsById['turn-1']!,
      status: 'completed',
      terminalParticipantIds: [],
      updatedAtMs: 9,
    };

    const row = selectRoomRoundTaskSheets(room, projection)[0]?.rows[0];

    expect(row).toMatchObject({
      state: 'completed',
      latestProgress: '本轮工作已结束',
    });
  });
});

function coordinatorPostProjection(
  postKind: 'progress' | 'result',
): RoomProjectionState {
  const projection = runningProjection(
    postKind === 'result' ? '主控最终汇报' : '主控正在整理汇报',
  );
  const messageId = `assistant-coordinator-${postKind}`;
  projection.turnsById['turn-1'] = {
    ...projection.turnsById['turn-1']!,
    status: 'completed',
    messageIds: ['user-1', messageId],
    terminalParticipantIds: ['participant-earth'],
    updatedAtMs: 5,
  };
  projection.activitiesById['activity-earth'] = {
    ...projection.activitiesById['activity-earth']!,
    status: 'completed',
    updatedAtMs: 4,
  };
  projection.messagesById[messageId] = assistantMessage(
    messageId,
    'turn-1',
    postKind === 'result' ? '主控最终汇报' : '主控正在整理汇报',
    5,
    postKind,
  );
  projection.messageOrder = ['user-1', messageId];
  return projection;
}

function runningProjection(summary: string): RoomProjectionState {
  return {
    ...createRoomProjection('room-a'),
    turnOrder: ['turn-1'],
    turnsById: {
      'turn-1': {
        id: 'turn-1',
        rootId: 'turn-1',
        status: 'running',
        messageIds: ['user-1'],
        activityIds: ['activity-earth'],
        participantIds: ['participant-earth'],
        createdAtMs: 1,
        updatedAtMs: 3,
      },
    },
    messagesById: {
      'user-1': userMessage('user-1', 'turn-1', '完成 Room 任务表', 1),
    },
    messageOrder: ['user-1'],
    activitiesById: {
      'activity-earth': {
        id: 'activity-earth',
        turnId: 'turn-1',
        participantId: 'participant-earth',
        sourceSessionId: 'session-earth',
        kind: 'participant_activity',
        status: 'running',
        summary,
        payload: {
          rootId: 'turn-1',
          dispatchId: 'dispatch-earth',
          sourceEventType: 'current_progress',
          task: '检查 Room 事件投影',
        },
        createdAtMs: 2,
        updatedAtMs: 3,
      },
    },
    activityOrder: ['activity-earth'],
  };
}

function userMessage(id: string, turnId: string, text: string, createdAtMs: number) {
  return {
    id,
    roomId: 'room-a',
    turnId,
    participantId: null,
    sourceSessionId: '',
    role: 'user' as const,
    status: 'completed' as const,
    text,
    createdAtMs,
  };
}

function assistantMessage(
  id: string,
  turnId: string,
  text: string,
  createdAtMs: number,
  postKind?: string,
) {
  return {
    id,
    roomId: 'room-a',
    turnId,
    participantId: 'participant-earth',
    sourceSessionId: 'session-earth',
    role: 'assistant' as const,
    status: 'completed' as const,
    text,
    ...(postKind ? { postKind } : {}),
    createdAtMs,
  };
}

function workItem(
  id: string,
  rootTurnId: string,
  resultSummary: string,
  evidenceRefs: string[],
  updatedAtMs: number,
): RoomWorkItem {
  return {
    id,
    roomId: 'room-a',
    topicId: '',
    rootTurnId,
    rootWorkId: id,
    parentWorkId: '',
    objective: `${id} task`,
    expectedOutput: '',
    acceptanceCriteria: [],
    accountableParticipantId: 'participant-earth',
    currentOwnerParticipantId: 'participant-earth',
    offeredToParticipantId: '',
    createdByParticipantId: 'participant-earth',
    clientMessageId: id,
    state: 'done',
    depth: 1,
    revision: 0,
    resultSummary,
    artifactRefs: [],
    evidenceRefs,
    blocker: {},
    acceptedTurnId: rootTurnId,
    createdAtMs: updatedAtMs - 1,
    updatedAtMs,
    completedAtMs: updatedAtMs,
  };
}

function participant(id: string, sessionId: string, ordinal: number): RoomParticipant {
  return {
    id,
    sessionId,
    roleId: 'implementer',
    roleVersion: '1',
    displayName: `伙伴 ${id}`,
    collaborationRole: ordinal === 0 ? 'coordinator' : 'implementer',
    status: 'active',
    ordinal,
  };
}

function roomWith(participants: RoomParticipant[]): RoomSummary {
  return {
    id: 'room-a',
    title: 'Room A',
    status: 'active',
    routingPolicy: 'natural',
    moderatorParticipantId: participants[0]?.id ?? '',
    updatedAtMs: 1,
    participants,
  };
}
