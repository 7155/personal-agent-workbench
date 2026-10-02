import { createRoomProjection, reduceRoomEvents, type RoomProjectionState } from '@/contracts/room-reducer';
import type { AgentSubagentRunV1 } from '@/contracts/generated/agent-subagent-run.v1';
import type { UiRoomEvent } from '@/contracts/ui-events';
import type { RoomSummary } from '@/features/rooms/room-types';

/**
 * A deterministic Room round shaped like the real workflow. Used by tests and
 * the visual fixture only; never by product code paths.
 *
 * Earth (coordinator) receives the request, recruits Venus, delegates a
 * parallel batch to Mars and Venus, Venus launches a Tool Agent satellite,
 * Mars' first tool fails then succeeds, both submit, Earth hands the
 * integration to Jupiter (reviewer, model chosen by Jev), accepts, replies.
 */
export function collabDemoRoom(options: { cut?: number; start?: number } = {}): { room: RoomSummary; projection: RoomProjectionState; satellites: Record<string, AgentSubagentRunV1[]>; nowMs: number } {
  const roomId = 'room-demo';
  const rootId = `${roomId}:root-1`;
  const t0 = options.start ?? Date.UTC(2026, 8, 29, 8, 25, 0);
  const participants = [
    { id: 'p-earth', sessionId: 's-earth', ordinal: 0, collaborationRole: 'coordinator' as const },
    { id: 'p-mars', sessionId: 's-mars', ordinal: 1, collaborationRole: 'implementer' as const },
    { id: 'p-venus', sessionId: 's-venus', ordinal: 2, collaborationRole: 'researcher' as const },
    { id: 'p-jupiter', sessionId: 's-jupiter', ordinal: 3, collaborationRole: 'reviewer' as const },
  ];
  const room: RoomSummary = {
    id: roomId, title: '整理发布说明', status: 'active', routingPolicy: 'moderator', moderatorParticipantId: 'p-earth', updatedAtMs: t0,
    participants: participants.map((participant) => ({ ...participant, roleId: 'r', roleVersion: '1', displayName: participant.id, status: 'active' })),
    workItems: [],
  };
  let sequence = 0;
  const event = (atS: number, eventType: string, participantId: string | null, payload: Record<string, unknown>): UiRoomEvent => {
    sequence += 1;
    const participant = participants.find((item) => item.id === participantId);
    return {
      schemaVersion: 'rag-ime.agent-room-event.v1', eventId: `${roomId}:${sequence}`, roomId, sequence, turnId: rootId, topicId: '',
      participantId, sourceSessionId: participant?.sessionId ?? '', createdAtMs: t0 + atS * 1000, payload: { rootId, ...payload },
      resumeToken: `${roomId}:${sequence}`, streamKind: 'room', eventType: eventType as UiRoomEvent['eventType'],
    };
  };
  const tool = (atS: number, endS: number, participantId: string, dispatchId: string, id: string, toolName: string, op: string, failed = false) => [
    event(atS, 'participant_activity', participantId, { dispatchId, sourceEventType: 'tool_started', toolCallId: id, toolName, arguments: { op }, summary: `${toolName} ${op}` }),
    event(endS, 'participant_activity', participantId, { dispatchId, sourceEventType: 'tool_finished', toolCallId: id, toolName, arguments: { op }, isError: failed, summary: failed ? '名册读取失败' : `${toolName} ${op} 完成` }),
  ];
  const child = (atS: number, target: string, dispatchId: string, phase: string, summary: string) => event(atS, 'participant_activity', 'p-earth', {
    activityKind: 'child', phase, status: phase, childDispatchId: dispatchId, dispatchId, targetParticipantId: target, summary,
  });
  const work = (atS: number, actor: string, phase: string, id: string, objective: string) => event(atS, 'participant_activity', actor, {
    activityKind: 'work', phase, workItemId: id, work: { id, objective, createdByParticipantId: 'p-earth', accountableParticipantId: 'p-earth', currentOwnerParticipantId: actor },
  });
  const events: UiRoomEvent[] = [
    event(0, 'user_message', null, { messageId: 'u1', text: '整理 v0.2 发布说明，并交给独立伙伴复核' }),
    event(1, 'route_decision', 'p-earth', { dispatchId: 'd-earth', targetParticipantId: 'p-earth', reason: 'facilitator', routingPolicy: 'moderator' }),
    ...tool(3, 5, 'p-earth', 'd-earth', 't-list', 'room_partner', 'list'),
    ...tool(6, 7, 'p-earth', 'd-earth', 't-add', 'room_partner', 'add_participant'),
    event(7.5, 'participant_status', 'p-venus', { status: 'participant_joined', participantId: 'p-venus' }),
    event(10, 'route_decision', 'p-mars', { dispatchId: 'd-mars', parentDispatchId: 'd-earth', targetParticipantId: 'p-mars', reason: 'partner_delegate', child: true, waveId: 'w1', parallelIndex: 0, parallelSize: 2 }),
    event(10.2, 'route_decision', 'p-venus', { dispatchId: 'd-venus', parentDispatchId: 'd-earth', targetParticipantId: 'p-venus', reason: 'partner_delegate', child: true, waveId: 'w1', parallelIndex: 1, parallelSize: 2 }),
    ...tool(13, 17, 'p-mars', 'd-mars', 't-mars-1', 'read', '', true),
    ...tool(19, 26, 'p-mars', 'd-mars', 't-mars-2', 'workspace_edit', ''),
    ...tool(14, 21, 'p-venus', 'd-venus', 't-venus-1', 'grep', ''),
    event(30, 'participant_activity', 'p-venus', { activityKind: 'intercom', phase: 'delivered', message: { id: 'ic-1', kind: 'ask', sourceParticipantId: 'p-venus', targetParticipantId: 'p-mars', content: '变更列表里要包含迁移步骤吗？', status: 'delivered' } }),
    event(33, 'participant_activity', 'p-mars', { activityKind: 'intercom', phase: 'delivered', message: { id: 'ic-2', kind: 'reply', sourceParticipantId: 'p-mars', targetParticipantId: 'p-venus', content: '要，放在最后一节。', status: 'delivered' } }),
    work(40, 'p-mars', 'submitted', 'w-mars', '写发布说明正文'),
    child(41, 'p-mars', 'd-mars', 'completed', '正文已完成，含迁移步骤'),
    work(48, 'p-venus', 'submitted', 'w-venus', '汇总变更与引用'),
    child(49, 'p-venus', 'd-venus', 'completed', '变更列表与引用已整理'),
    work(52, 'p-earth', 'completed', 'w-mars', '写发布说明正文'),
    work(53, 'p-earth', 'completed', 'w-venus', '汇总变更与引用'),
    event(55, 'route_decision', 'p-jupiter', { dispatchId: 'd-jupiter', parentDispatchId: 'd-earth', targetParticipantId: 'p-jupiter', reason: 'partner_delegate', child: true, purpose: 'verify' }),
    ...tool(58, 66, 'p-jupiter', 'd-jupiter', 't-jup', 'room_partner', 'verification_submit'),
    work(67, 'p-jupiter', 'submitted', 'w-review', '独立复核整合版本'),
    child(68, 'p-jupiter', 'd-jupiter', 'completed', '复核通过：可运行、满足需求'),
    work(70, 'p-earth', 'completed', 'w-review', '独立复核整合版本'),
    ...tool(72, 74, 'p-earth', 'd-earth', 't-post', 'room_partner', 'post'),
    event(76, 'room_post', 'p-earth', { dispatchId: 'd-earth', post: {
      schemaVersion: 'wisdom-weasel.room-post.v2', postId: 'post-final', roomId, rootId, generation: 0, dispatchId: 'd-earth', authorActorRef: 'p-earth', kind: 'result', visibility: 'room',
      content: 'v0.2 发布说明已完成并通过独立复核。', idempotencyKey: 'post-final', publicationSource: { kind: 'room_commit', ref: 'commit:final' }, createdAtMs: t0 + 76_000,
    } }),
    event(77, 'turn_completed', 'p-earth', { dispatchId: 'd-earth' }),
    event(77.5, 'turn_completed', null, {}),
  ];
  const cut = options.cut ?? Number.POSITIVE_INFINITY;
  const visible = events.filter((item) => item.createdAtMs - t0 <= cut * 1000);
  const projection = reduceRoomEvents(createRoomProjection(roomId), visible);
  const satellites: Record<string, AgentSubagentRunV1[]> = {
    's-venus': [subagentRun({ id: 'sat-1', parent: 's-venus', task: '检索历史发布说明格式', createdAtMs: t0 + 15_000, completedAtMs: cut >= 28 ? t0 + 28_000 : null, template: 'researcher', tools: 4 })],
  };
  return { room, projection, satellites, nowMs: t0 + Math.min(cut, 78) * 1000 };
}

export function subagentRun(input: { id: string; parent: string; task: string; createdAtMs: number; completedAtMs: number | null; template?: AgentSubagentRunV1['templateId']; parentRunId?: string; depth?: number; attempt?: number; failed?: boolean; tools?: number; nodeId?: string }): AgentSubagentRunV1 {
  const running = input.completedAtMs === null;
  return {
    schemaVersion: 'rag-ime.agent-subagent-run.v1', id: input.id, nodeId: input.nodeId ?? input.id, attemptId: `${input.id}:a`, attemptNumber: input.attempt ?? 1, predecessorAttemptId: '',
    ownerRunId: '', parentRunId: input.parentRunId ?? '', depth: input.depth ?? 1, batchId: 'b', childSessionId: `${input.id}-session`, todoTask: '', todoPhase: '',
    templateId: input.template ?? 'worker', templateVersion: '1', ordinal: 0, task: input.task, expectedOutput: '', acceptanceCriteria: [],
    launchDigest: { schemaVersion: 'rag-ime.agent-subagent-launch-digest.v1', contextMode: 'fresh', templateId: input.template ?? 'worker', templateVersion: '1', modelProfile: 'luna', thinkingLevel: 'max', toolProfileVersion: '1', toolAllowlistMode: 'profile', tools: [], piSkillsEnabled: false, codexSkillsEnabled: false, workspaceAccess: 'read_only', workspaceRootCount: 1, outputContract: { required: false, schemaSha256: '' }, extensionRuntime: 'pi_host_managed' },
    contract: { status: running ? 'pending' : 'valid', error: '', toolCallId: '', validatedAtMs: input.completedAtMs },
    state: running ? 'running' : input.failed ? 'failed' : 'completed',
    budget: { maxTurns: 8, maxToolCalls: 20, maxTotalTokens: 100_000, maxDurationMs: 600_000, maxOutputChars: 20_000 },
    usage: { turnCount: 2, toolCount: input.tools ?? 3, totalTokens: 12_400 }, result: {}, error: input.failed ? '超时' : '',
    resultContextScheduledAtMs: null, createdAtMs: input.createdAtMs, startedAtMs: input.createdAtMs + 400, updatedAtMs: input.completedAtMs ?? input.createdAtMs + 1000, completedAtMs: input.completedAtMs,
  } as unknown as AgentSubagentRunV1;
}
