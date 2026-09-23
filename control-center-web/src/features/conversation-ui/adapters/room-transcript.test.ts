import { describe, expect, it } from 'vitest';
import { appendOptimisticRoomMessage, createRoomProjection } from '@/contracts/room-reducer';
import { MODEL_AUTH_FAILURE_TEXT } from '@/features/agent/public-error';
import type { AssistantMessage, TranscriptMessage, UserMessage } from '../model/types';
import {
  roomApprovalDecision,
  roomPhase,
  roomTranscript,
  roomTranscriptRetrySource,
} from './room-transcript';

const NAMES: Record<string, string> = {
  'participant-a': 'Mars',
  'participant-b': 'Venus',
};

const options = {
  actorName: (participantId?: string | null) => participantId ? NAMES[participantId] ?? '伙伴' : 'Sol',
  actorRole: (participantId?: string | null) => participantId === 'participant-a' ? '实现' : '',
};

describe('roomTranscript', () => {
  it.each(['模型服务未能生成最终回复。请继续当前对话，或切换模型后继续。', '已完成文件检查。'])
  ('recovers persisted OAuth errors while preserving useful partial output: %s', (text) => {
    const projection = roomProjection();
    const message = projection.messagesById['message-agent']!;
    message.status = 'failed';
    message.text = text;
    message.message = { blocks: [{ type: 'error', data: {
      message: 'Encountered invalidated oauth token for user, failing request',
    } }] } as unknown as typeof message.message;
    const card = roomTranscript(projection, options).messages.at(-1) as AssistantMessage;
    expect(card.error).toBe(MODEL_AUTH_FAILURE_TEXT);
    expect(card.blocks.at(-1)).toMatchObject({
      kind: 'text', text: text.startsWith('模型服务') ? MODEL_AUTH_FAILURE_TEXT : text,
    });
  });
  it('folds one Runtime loop into one assistant card with its public blocks', () => {
    const projection = roomProjection();

    const { messages } = roomTranscript(projection, options);

    expect(messages.map((message) => message.role)).toEqual(['user', 'assistant']);
    const user = messages[0]!;
    expect(user.role === 'user' && user.text).toBe('请完成主线迁移');
    const card = messages[1] as AssistantMessage;
    expect(card.actor).toBe('Mars');
    expect(card.actorRole).toBe('实现');
    expect(card.turnId).toBe('root-a');
    expect(card.blocks.map((block) => block.kind)).toEqual(['tool', 'tool', 'text']);
  });

  it('names tools by their reader-facing label instead of the Runtime id', () => {
    const { messages } = roomTranscript(roomProjection(), options);

    const card = messages[1] as AssistantMessage;
    const tool = card.blocks.find((block) => block.id === 'tool:tool-a');
    expect(tool).toMatchObject({ kind: 'tool', name: '读取文件', status: 'running' });
    /* The raw Runtime blob is not a reader line, and the derived fallback would
       only repeat the card's own name and state, so the summary stays empty
       and the blob stays reachable as the recorded call. */
    expect(tool?.kind === 'tool' && tool.summary).toBe('');
    expect(tool?.kind === 'tool' && tool.input).toContain('PawWindowLayer.tsx');
  });

  it('keeps a dispatch planet-only when Runtime persona names have no public alias', () => {
    const projection = roomProjection();
    projection.activityOrder = ['dispatch-persona-name'];
    projection.activitiesById = {
      'dispatch-persona-name': {
        id: 'dispatch-persona-name', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
        kind: 'route_decision', status: 'completed', summary: '已确定本轮分工',
        payload: {
          sourceEventType: 'route_decision', dispatchId: 'dispatch-persona-name',
          targetParticipantId: 'participant-target', targetDisplayName: '不应出现的目标人名',
          candidates: [{ participantId: 'participant-candidate', displayName: '不应出现的候选人名', score: 0.8, signals: [] }],
        },
        sequence: 2, createdAtMs: 110, updatedAtMs: 110,
      },
    };

    const transcript = roomTranscript(projection, {
      ...options,
      actorName: (participantId) => participantId === 'participant-a' ? 'Mars' : participantId ? '' : 'Sol',
    });
    const dispatch = transcript.messages
      .flatMap((message) => message.role === 'assistant' ? message.blocks : [])
      .find((block) => block.id === 'dispatch:dispatch-persona-name');

    expect(JSON.stringify(dispatch)).not.toContain('不应出现的目标人名');
    expect(JSON.stringify(dispatch)).not.toContain('不应出现的候选人名');
    expect(dispatch?.kind === 'tool' && dispatch.name).toContain('协作行星');
  });

  it('keeps a pending approval on the card and links it back to its activity', () => {
    const projection = roomProjection();

    const { messages, activityByBlockId } = roomTranscript(projection, options);

    const card = messages[1] as AssistantMessage;
    const approval = card.blocks.find((block) => block.id === 'approval:approval-a');
    expect(approval).toMatchObject({ kind: 'tool', name: '受控操作审批', status: 'pending' });
    expect(activityByBlockId['approval:approval-a']).toBe(projection.activitiesById['approval-a']);
    expect(roomApprovalDecision(projection.activitiesById['approval-a']!)).toEqual({
      approvalId: 'approval-a',
      payloadSha256: 'a'.repeat(64),
    });
  });

  it('keeps an approved execution failure as a tool with its real cause', () => {
    const projection = roomProjection();
    const activity = projection.activitiesById['tool-a']!;
    activity.status = 'failed';
    activity.summary = 'bash';
    activity.payload = { sourceEventType: 'tool_finished', toolName: 'bash', approvalId: 'already-approved', error: 'Tool gateway request timed out after 30000ms', result: { outputPreview: 'Tool gateway request timed out after 30000ms' } };
    const transcript = roomTranscript(projection, options);
    const block = transcript.messages.flatMap((message) => message.role === 'assistant' ? message.blocks : []).find((item) => item.id === 'tool:tool-a');
    expect(block).toMatchObject({ kind: 'tool', name: '终端命令', status: 'error', summary: 'Tool gateway request timed out after 30000ms' });
    expect(block?.kind === 'tool' && block.output).toContain('失败原因');
  });

  it('projects returned and reassigned WorkItem events as compact public receipts', () => {
    const projection = roomProjection();
    projection.activityOrder.push('work-returned', 'work-reassigned');
    projection.activitiesById['work-returned'] = {
      id: 'work-returned', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
      kind: 'participant_activity', status: 'completed', summary: '',
      payload: {
        activityKind: 'work', phase: 'returned', workItemId: 'work-17',
        previousWorkItemRevision: 4, currentWorkItemRevision: 5,
        ownerParticipantId: 'participant-a', reason: 'requirements_changed',
        documentRef: 'workdoc:brief-17@5',
      },
      sequence: 5, createdAtMs: 140, updatedAtMs: 140,
    };
    projection.activitiesById['work-reassigned'] = {
      id: 'work-reassigned', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
      kind: 'participant_activity', status: 'completed', summary: '',
      payload: {
        activityKind: 'work', phase: 'reassigned', workItemId: 'work-17',
        previousWorkItemRevision: 5, currentWorkItemRevision: 5,
        ownerParticipantId: 'participant-b', reason: 'facilitator_reassigned',
        documentRef: 'workdoc:brief-17@5',
      },
      sequence: 6, createdAtMs: 150, updatedAtMs: 150,
    };

    const blocks = roomTranscript(projection, options).messages
      .flatMap((message) => message.role === 'assistant' ? message.blocks : []);

    expect(blocks.find((block) => block.id === 'note:work-returned')).toMatchObject({
      kind: 'thinking',
      summary: '需求已更新 · r4→r5 · 负责人 Mars',
      detail: '任务 work-17\n原因 requirements_changed\n文档 workdoc:brief-17@5',
    });
    expect(blocks.find((block) => block.id === 'note:work-reassigned')).toMatchObject({
      kind: 'thinking',
      summary: '负责人变更 · r5 · 负责人 Venus',
      detail: '任务 work-17\n原因 facilitator_reassigned\n文档 workdoc:brief-17@5',
    });
  });

  it('starts a new card when the speaking partner changes inside one turn', () => {
    const projection = roomProjection();
    projection.messageOrder.push('message-agent-b');
    projection.messagesById['message-agent-b'] = {
      id: 'message-agent-b', roomId: projection.roomId, turnId: 'root-a', participantId: 'participant-b',
      sourceSessionId: 'session-b', role: 'assistant', status: 'completed', text: '我来复核。',
      projectionKind: 'post', sequence: 5, createdAtMs: 140,
    };

    const { messages } = roomTranscript(projection, options);

    expect(messages.map((message) => message.role === 'assistant' ? message.actor : 'user'))
      .toEqual(['user', 'Mars', 'Venus']);
  });

  it('marks a streaming reply so the progressive renderer keeps a mutable tail', () => {
    const projection = roomProjection();
    projection.messagesById['message-agent']!.status = 'streaming';

    const card = roomTranscript(projection, options).messages[1] as AssistantMessage;

    expect(card.blocks.at(-1)).toMatchObject({ kind: 'text', streaming: true });
  });

  it('reports a failed turn as a card error with a retry source', () => {
    const projection = roomProjection();
    projection.turnOrder.push('root-a');
    projection.turnsById['root-a'] = {
      id: 'root-a', rootId: 'root-a', status: 'failed', messageIds: ['message-user'],
      activityIds: [], participantIds: ['participant-a'], createdAtMs: 100, updatedAtMs: 145,
      failure: '503 upstream request failed',
    };

    const { messages } = roomTranscript(projection, options);

    expect((messages.at(-1) as AssistantMessage).error).toBe('503 upstream request failed');
    expect(roomTranscriptRetrySource(projection, 'root-a')).toEqual({
      rootId: 'root-a',
      text: '请完成主线迁移',
    });
  });

  it('withdraws retry once newer user input has superseded the failure', () => {
    const projection = roomProjection();
    projection.turnOrder.push('root-a');
    projection.turnsById['root-a'] = {
      id: 'root-a', rootId: 'root-a', status: 'failed', messageIds: ['message-user'],
      activityIds: [], participantIds: ['participant-a'], createdAtMs: 100, updatedAtMs: 145,
      failure: '503 upstream request failed',
    };
    projection.messageOrder.push('message-user-new');
    projection.messagesById['message-user-new'] = {
      id: 'message-user-new', roomId: projection.roomId, turnId: 'root-b', participantId: null,
      sourceSessionId: '', role: 'user', status: 'completed', text: '改用新的方案继续',
      projectionKind: 'post', sequence: 6, createdAtMs: 160,
    };

    expect(roomTranscriptRetrySource(projection, 'root-a')).toBeUndefined();
  });

  it('restricts a partner satellite to that partner\u2019s own public lane', () => {
    const projection = roomProjection();
    projection.activityOrder.push('tool-b');
    projection.activitiesById['tool-b'] = {
      id: 'tool-b', turnId: 'root-a', participantId: 'participant-b', sourceSessionId: 'session-b',
      kind: 'tool', status: 'completed', summary: '写入完成',
      payload: { sourceEventType: 'tool_finished', toolName: 'write' },
      sequence: 6, createdAtMs: 150, updatedAtMs: 150,
    };

    const { messages } = roomTranscript(projection, { ...options, participantId: 'participant-a' });

    const blockIds = messages.flatMap((message) => message.role === 'assistant' ? message.blocks.map((block) => block.id) : []);
    expect(blockIds).toContain('tool:tool-a');
    expect(blockIds).not.toContain('tool:tool-b');
  });

  it('derives the run phase from real turn status', () => {
    const projection = roomProjection();
    expect(roomPhase(projection)).toBe('idle');
    projection.turnOrder.push('root-a');
    projection.turnsById['root-a'] = {
      id: 'root-a', rootId: 'root-a', status: 'running', messageIds: [], activityIds: [],
      participantIds: [], createdAtMs: 100, updatedAtMs: 100,
    };
    expect(roomPhase(projection)).toBe('responding');
  });

  it('drops execution-only projections that were never public', () => {
    const projection = roomProjection();
    projection.messageOrder.push('message-internal');
    projection.messagesById['message-internal'] = {
      id: 'message-internal', roomId: projection.roomId, turnId: 'root-a', participantId: 'participant-a',
      sourceSessionId: 'session-a', role: 'assistant', status: 'completed', text: '内部执行记录',
      projectionKind: 'execution', sequence: 7, createdAtMs: 170,
    };

    const blockIds = roomTranscript(projection, options).messages
      .flatMap((message) => message.role === 'assistant' ? message.blocks.map((block) => block.id) : []);

    expect(blockIds).not.toContain('text:message-internal');
  });

  describe('steer receipt', () => {
    it('reports an unsent steer as undelivered while a Root is still in flight', () => {
      const projection = appendOptimisticRoomMessage(runningProjection(), {
        clientMessageId: 'client-steer',
        text: '改成先做迁移脚本',
        nowMs: 200,
      });

      const steer = lastUserMessage(roomTranscript(projection, options).messages);
      expect(steer.steerReceipt).toBe('unread');
      expect(steer.deliveryStatus).toBe('sending');
    });

    it('leaves an ordinary prompt on a plain timestamp when no Root is running', () => {
      const projection = appendOptimisticRoomMessage(roomProjection(), {
        clientMessageId: 'client-prompt',
        text: '再开一个新任务',
        nowMs: 200,
      });

      expect(lastUserMessage(roomTranscript(projection, options).messages).steerReceipt).toBeUndefined();
    });

    it('turns the receipt to delivered once Runtime publishes it into the running Root', () => {
      const projection = runningProjection();
      publishSteer(projection, 5);

      expect(lastUserMessage(roomTranscript(projection, options).messages).steerReceipt).toBe('read');
    });

    it('settles the receipt once the Root publishes work after the steer', () => {
      const projection = runningProjection();
      publishSteer(projection, 5);
      projection.messageOrder.push('message-after');
      projection.turnsById['root-a']!.messageIds.push('message-after');
      projection.messagesById['message-after'] = {
        id: 'message-after', roomId: projection.roomId, turnId: 'root-a', participantId: 'participant-a',
        sourceSessionId: 'session-a', role: 'assistant', status: 'completed', text: '好，改做迁移脚本。',
        projectionKind: 'post', sequence: 6, createdAtMs: 210,
      };

      expect(lastUserMessage(roomTranscript(projection, options).messages).steerReceipt).toBe('settling');
    });

    it('closes the receipt when the steered Root reaches a terminal status', () => {
      const projection = runningProjection();
      publishSteer(projection, 5);
      projection.turnsById['root-a']!.status = 'completed';

      expect(lastUserMessage(roomTranscript(projection, options).messages).steerReceipt).toBe('done');
    });
  });
});

function lastUserMessage(messages: TranscriptMessage[]): UserMessage {
  const user = messages.filter((message): message is UserMessage => message.role === 'user').at(-1);
  if (!user) throw new Error('expected a user message in the transcript');
  return user;
}

/** The base fixture with its Root still open, which is what makes a later
 *  message a steer rather than a fresh request. */
function runningProjection() {
  const projection = roomProjection();
  projection.turnOrder.push('root-a');
  projection.turnsById['root-a'] = {
    id: 'root-a', rootId: 'root-a', status: 'running',
    messageIds: ['message-user', 'message-agent'], activityIds: ['tool-a', 'approval-a'],
    participantIds: ['participant-a'], createdAtMs: 100, updatedAtMs: 130,
  };
  return projection;
}

function publishSteer(projection: ReturnType<typeof roomProjection>, sequence: number) {
  projection.messageOrder.push('message-steer');
  projection.turnsById['root-a']!.messageIds.push('message-steer');
  projection.messagesById['message-steer'] = {
    id: 'message-steer', roomId: projection.roomId, turnId: 'root-a', participantId: null,
    sourceSessionId: '', role: 'user', status: 'completed', text: '改成先做迁移脚本',
    projectionKind: 'post', sequence, createdAtMs: 200,
  };
}

function roomProjection() {
  const projection = createRoomProjection('room-live');
  projection.messageOrder.push('message-user', 'message-agent');
  projection.messagesById['message-user'] = {
    id: 'message-user', roomId: 'room-live', turnId: 'root-a', participantId: null,
    sourceSessionId: '', role: 'user', status: 'completed', text: '请完成主线迁移',
    projectionKind: 'post', sequence: 1, createdAtMs: 100,
  };
  projection.messagesById['message-agent'] = {
    id: 'message-agent', roomId: 'room-live', turnId: 'root-a', participantId: 'participant-a',
    sourceSessionId: 'session-a', role: 'assistant', status: 'completed', text: '已接入生产 reducer。',
    projectionKind: 'post', sequence: 4, createdAtMs: 130,
  };
  projection.activityOrder.push('tool-a', 'approval-a');
  projection.activitiesById['tool-a'] = {
    id: 'tool-a', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
    kind: 'tool', status: 'running',
    summary: '```json\n{"path":"/Volumes/private/workspace/PawWindowLayer.tsx"}\n```',
    payload: { sourceEventType: 'tool_started', toolName: 'read' },
    sequence: 2, createdAtMs: 110, updatedAtMs: 110,
  };
  projection.activitiesById['approval-a'] = {
    id: 'approval-a', turnId: 'root-a', participantId: 'participant-a', sourceSessionId: 'session-a',
    kind: 'approval_required', status: 'waiting', summary: '批准受控迁移操作',
    payload: { sourceEventType: 'approval_required', approvalId: 'approval-a', payloadSha256: 'a'.repeat(64) },
    sequence: 3, createdAtMs: 120, updatedAtMs: 120,
  };
  return projection;
}
