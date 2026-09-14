import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  createRoomProjection,
  parseRoomEventSnapshot,
  reduceRoomEvents,
  replayRoomEventSnapshot,
  type RoomProjectionState,
} from '@/contracts/room-reducer';
import { parseRoomEvent } from '@/contracts/validators';
import type { TeamApi } from '@/features/team/team-api';
import { TeamProvider } from '@/features/team/team-context';
import { RoomTurn } from './RoomTurn';

describe('RoomTurn canonical conversation chronology', () => {
  afterEach(cleanup);

  it('renders question, answer, question, answer, alignment, and typed start in server order', () => {
    const projection = liveProjection(alignmentEvents());
    const view = render(roomTurn(projection));

    expectTextOrder(view.container, [
      '@澄·今 我准备写 TUI',
      '目标界面是什么？',
      '终端原生 TUI',
      '首版交付边界是什么？',
      '可运行闭环',
      '已经对齐：先完成终端原生 TUI 的可运行闭环。',
      '开始行动',
    ]);
    expect(messageOrder(view.container)).toEqual([
      'opening',
      'question-1',
      'answer-1',
      'question-2',
      'answer-2',
      'alignment',
      'start',
    ]);
  });

  it('preserves A/B/A public posts across participant lanes', () => {
    const projection = liveProjection([
      userEvent(1, 'opening', '请一起完成跨角色任务'),
      postEvent(2, 'a-first', 'work_result', '澄·今先确认边界', 'participant-a', 'dispatch-a'),
      postEvent(3, 'b-middle', 'work_result', '澄·初补充独立检查', 'participant-b', 'dispatch-b'),
      postEvent(4, 'a-last', 'work_result', '澄·今完成最终整合', 'participant-a', 'dispatch-a'),
    ]);
    const view = render(roomTurn(projection));

    expectTextOrder(view.container, [
      '请一起完成跨角色任务',
      '澄·今先确认边界',
      '澄·初补充独立检查',
      '澄·今完成最终整合',
    ]);
    expect(messageOrder(view.container)).toEqual([
      'opening',
      'a-first',
      'b-middle',
      'a-last',
    ]);
    const committedPosts = [...view.container.querySelectorAll<HTMLElement>('.room-agent-lane__post')];
    expect(committedPosts.every((post) => post.closest('.room-agent-lane'))).toBe(true);
    const lanes = [...view.container.querySelectorAll<HTMLDetailsElement>('.room-agent-lane')];
    // Public replies are the conversational result, so they stay visible by
    // default while their lower-level work/tool details remain progressive.
    expect(lanes.every((lane) => lane.open)).toBe(true);
    expect(lanes.map((lane) => lane.querySelector('summary')?.textContent)).toEqual([
      expect.stringContaining('澄·今先确认边界'),
      expect.stringContaining('澄·初补充独立检查'),
      expect.stringContaining('澄·今完成最终整合'),
    ]);
    expect(lanes.map((lane) => (
      lane.querySelector('[data-room-message-id]')?.getAttribute('data-room-message-id')
    ))).toEqual(['a-first', 'b-middle', 'a-last']);
  });

  it('renders the persisted human actor on a Room post and marks the current account', async () => {
    const projection = liveProjection([event(1, 'user_message', {
      messageId: 'opening',
      text: '请一起完成跨角色任务',
      rootId: 'root-a',
      actorUserId: 'user-1',
      actorDisplayName: 'Alice',
    }, null, '')]);
    expect(projection.messagesById.opening).toMatchObject({ actorUserId: 'user-1', actorDisplayName: 'Alice' });

    const api = {
      status: vi.fn().mockResolvedValue({ enabled: true, name: 'PAW Team' }),
      me: vi.fn().mockResolvedValue({
        user: { id: 'user-1', username: 'alice', displayName: 'Alice', role: 'member', active: true },
        csrfToken: 'csrf-memory',
        spaces: [{ id: 'project-1', kind: 'project', name: 'PAW Team', role: 'contributor', revision: 1 }],
      }),
    } as unknown as TeamApi;
    const view = render(<TeamProvider api={api}>{roomTurn(projection)}</TeamProvider>);
    const header = view.container.querySelector<HTMLElement>('.room-user-message__header');
    expect(header).toHaveTextContent('Alice');
    await waitFor(() => expect(header).toHaveTextContent('你'));
    expect(header?.querySelector('small')).toHaveTextContent('你');
  });

  it('opens a participant lane when authoritative assistant text starts streaming', async () => {
    const projection = liveProjection([
      userEvent(1, 'opening', '请开始输出实时正文'),
      event(2, 'route_decision', {
        rootId: 'root-a',
        dispatchId: 'dispatch-live',
        targetParticipantId: 'participant-a',
      }),
      event(3, 'participant_delta', {
        rootId: 'root-a',
        dispatchId: 'dispatch-live',
        messageId: 'assistant-live',
        blockId: 'assistant-live:text',
        delta: '第一段实时正文',
      }),
    ]);
    const view = render(roomTurn(projection));
    const lane = view.container.querySelector<HTMLDetailsElement>('.room-agent-lane')!;

    await waitFor(() => expect(lane).toHaveAttribute('open'));
    expect(screen.getByText('第一段实时正文')).toBeVisible();
  });

  it('renders live reduction and reconnect replay with the same canonical order', () => {
    const events = alignmentEvents();
    const live = liveProjection(events);
    const replayed = replayRoomEventSnapshot(
      createRoomProjection('room-a'),
      parseRoomEventSnapshot(snapshot(events)),
    );
    const liveView = render(roomTurn(live));
    expectTextOrder(liveView.container, [
      '@澄·今 我准备写 TUI',
      '目标界面是什么？',
      '终端原生 TUI',
      '首版交付边界是什么？',
      '可运行闭环',
      '已经对齐：先完成终端原生 TUI 的可运行闭环。',
      '开始行动',
    ]);
    const liveOrder = messageOrder(liveView.container);
    liveView.unmount();
    const replayedView = render(roomTurn(replayed));

    expectTextOrder(replayedView.container, [
      '@澄·今 我准备写 TUI',
      '目标界面是什么？',
      '终端原生 TUI',
      '首版交付边界是什么？',
      '可运行闭环',
      '已经对齐：先完成终端原生 TUI 的可运行闭环。',
      '开始行动',
    ]);
    expect(messageOrder(replayedView.container)).toEqual(liveOrder);
    expect(liveOrder).toEqual([
      'opening',
      'question-1',
      'answer-1',
      'question-2',
      'answer-2',
      'alignment',
      'start',
    ]);
  });






});

function roomTurn(
  projection: RoomProjectionState,
  props: Partial<Parameters<typeof RoomTurn>[0]> = {},
) {
  return <RoomTurn
    personas={[]}
    projection={projection}
    room={{
      participants: [
        {
          id: 'participant-a',
          sessionId: 'session-a',
          roleId: 'companion-present-v1',
          roleVersion: '1',
          displayName: '澄·今',
        },
        {
          id: 'participant-b',
          sessionId: 'session-b',
          roleId: 'companion-firstlight-v1',
          roleVersion: '1',
          displayName: '澄·初',
        },
      ],
    }}
    turnId="root-a"
    {...props}
  />;
}

function messageOrder(container: HTMLElement): string[] {
  return [...container.querySelectorAll<HTMLElement>('[data-room-message-id]')]
    .map((element) => element.dataset.roomMessageId ?? '');
}

function expectTextOrder(container: HTMLElement, texts: string[]): void {
  const elements = texts.map((text) => textElement(container, text));
  for (let index = 1; index < elements.length; index += 1) {
    expect(
      elements[index - 1]!.compareDocumentPosition(elements[index]!)
      & Node.DOCUMENT_POSITION_FOLLOWING,
      `${texts[index - 1]} should precede ${texts[index]}`,
    ).toBeTruthy();
  }
}

function textElement(container: HTMLElement, text: string): HTMLElement {
  const element = [...container.querySelectorAll<HTMLElement>('p, strong')]
    .find((candidate) => candidate.textContent === text);
  if (!element) throw new TypeError(`Missing public message text: ${text}`);
  return element;
}

function liveProjection(events: ReturnType<typeof event>[]): RoomProjectionState {
  return reduceRoomEvents(
    createRoomProjection('room-a'),
    events.map((item) => parseRoomEvent(item)),
  );
}

function alignmentEvents() {
  return [
    userEvent(1, 'opening', '@澄·今 我准备写 TUI'),
    questionEvent(2, 'question-1', '目标界面是什么？', ['终端原生 TUI', '网页界面']),
    userEvent(3, 'answer-1', '终端原生 TUI', 'question-1'),
    questionEvent(4, 'question-2', '首版交付边界是什么？', ['可运行闭环', '完整插件生态']),
    userEvent(5, 'answer-2', '可运行闭环', 'question-2'),
    postEvent(6, 'alignment', 'alignment', '已经对齐：先完成终端原生 TUI 的可运行闭环。'),
    userEvent(7, 'start', '开始行动'),
  ];
}

function userEvent(sequence: number, messageId: string, text: string, answerToPostId = '') {
  return event(sequence, 'user_message', {
    messageId,
    text,
    rootId: 'root-a',
    ...(answerToPostId ? { answerToPostId } : {}),
  }, null, '');
}

function questionEvent(sequence: number, postId: string, prompt: string, labels: string[]) {
  return postEvent(sequence, postId, 'wait', prompt, 'participant-a', 'dispatch-align', {
    prompt,
    options: labels.map((label, index) => ({ value: `choice-${index}`, label })),
  });
}

function postEvent(
  sequence: number,
  postId: string,
  kind: string,
  content: string,
  participantId = 'participant-a',
  dispatchId = 'dispatch-align',
  question?: { prompt: string; options: { value: string; label: string }[] },
) {
  return event(sequence, 'room_post', {
    post: {
      schemaVersion: 'wisdom-weasel.room-post.v2',
      postId,
      roomId: 'room-a',
      rootId: 'root-a',
      generation: 1,
      dispatchId,
      authorActorRef: participantId,
      kind,
      visibility: 'room',
      content,
      chronology: {
        schemaVersion: 'wisdom-weasel.room-post-chronology.v1',
        roomEventId: `room-a:${sequence}`,
        roomEventSequence: sequence,
        createdAtMs: sequence * 1_000,
        afterPostId: null,
        orderKey: `room-event:${String(sequence).padStart(20, '0')}`,
      },
      ...(question ? { question } : {}),
      idempotencyKey: `post:${postId}`,
      publicationSource: { kind: 'room_commit', ref: `commit:${postId}` },
      createdAtMs: sequence * 1_000,
    },
  }, participantId, participantId === 'participant-b' ? 'session-b' : 'session-a');
}

function event(
  sequence: number,
  eventType: string,
  payload: Record<string, unknown>,
  participantId: string | null = 'participant-a',
  sourceSessionId = 'session-a',
) {
  return {
    schemaVersion: 'rag-ime.agent-room-event.v1',
    eventId: `room-a:${sequence}`,
    roomId: 'room-a',
    sequence,
    turnId: 'root-a',
    eventType,
    participantId,
    sourceSessionId,
    createdAtMs: sequence * 1_000,
    payload,
    resumeToken: `room-a:${sequence}`,
  };
}

function snapshot(events: ReturnType<typeof event>[]) {
  const lastSequence = events.at(-1)?.sequence ?? 0;
  return {
    schemaVersion: 'rag-ime.agent-room-snapshot.v1',
    ok: true,
    room: {
      schemaVersion: 'rag-ime.agent-room.v1',
      id: 'room-a',
      title: 'Chronology Room',
      status: 'active',
      executionMode: 'full_trust',
      permissionPolicy: {
        schemaVersion: 'rag-ime.room-permission-policy.v1',
        room: { executionMode: 'full_trust' },
        partner: { executionMode: 'inherit' },
        toolAgent: { executionMode: 'inherit' },
      },
      routingPolicy: 'moderator',
      moderatorParticipantId: 'participant-a',
      workspaceRoots: ['/Volumes/work/learnA'],
      createdAtMs: 1,
      updatedAtMs: lastSequence * 1_000,
      lastEventSequence: lastSequence,
      participants: [
        participant('participant-a', 'session-a', 0),
        participant('participant-b', 'session-b', 1),
      ],
    },
    events,
    firstSequence: events[0]?.sequence ?? 0,
    lastSequence,
    resumeToken: lastSequence ? `room-a:${lastSequence}` : '',
    truncated: false,
  };
}

function participant(id: string, sessionId: string, ordinal: number) {
  return {
    schemaVersion: 'rag-ime.agent-participant.v1',
    id,
    roomId: 'room-a',
    sessionId,
    roleId: ordinal === 0 ? 'companion-present-v1' : 'companion-firstlight-v1',
    roleVersion: '1',
    displayName: ordinal === 0 ? '澄·今' : '澄·初',
    collaborationRole: ordinal === 0 ? 'coordinator' : 'researcher',
    status: 'active',
    ordinal,
    createdAtMs: 1,
    lastSpokeAtMs: null,
  };
}
