/// <reference types="node" />

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { createRoomProjection, parseRoomEventSnapshot, reduceRoomEvents, type RoomProjectionState } from '@/contracts/room-reducer';
import { parseRoomEvent } from '@/contracts/validators';
import { PawOsSatelliteHost } from '@/features/paw-os/PawOsSatelliteHost';
import type { RoomSummary } from '@/features/rooms/room-types';
import { useRoomLiveStore } from '@/features/rooms/state/live-store';
import { MockControlTransport } from '@/test/mock-transport';
import { PawRoomFocusOverview } from './PawRoomFocusOverview';
import { PawRoomConversation } from './PawRoomWorkspace';
import { buildRoomFocusProjection, type RoomFocusProjection } from './room-focus-projection';
import { buildRoomFocusMesh } from './room-focus-mesh';
import {
  roomDispatchPlans,
  roomDispatchWaves,
  roomToolActivityLine,
  roomToolEvidence,
  roomToolSummaryIsMachine,
} from './room-gravity-projection';

/**
 * Sol gravity against the real minecraft-harness fixture: 3261 authoritative
 * Room events, one coordinator, one reviewer and two parallel partners. The
 * projection and both surfaces must answer goal, owners, parallel lanes,
 * blockers and the verification planet from this data alone.
 */

const root = resolve(process.cwd(), 'e2e/fixtures/minecraft-harness-20260825');

const fullTrustPermissionPolicy = {
  schemaVersion: 'rag-ime.room-permission-policy.v1',
  room: { executionMode: 'full_trust' },
  partner: { executionMode: 'inherit' },
  toolAgent: { executionMode: 'inherit' },
} as const;

function readRoomFixture(path: string): Record<string, unknown> & { room: RoomSummary } {
  const response = JSON.parse(readFileSync(resolve(root, path), 'utf8')) as Record<string, unknown> & { room: RoomSummary };
  return {
    ...response,
    room: {
      ...response.room,
      permissionPolicy: fullTrustPermissionPolicy,
    },
  };
}

const firstRootId = 'room-turn:10000000-0000-4000-8000-000000000001';
const latestRootId = 'room-turn:10000000-0000-4000-8000-000000000002';

/**
 * The harness is a history fixture containing several independent public
 * inputs. Current Focus intentionally follows only the latest logical Root,
 * so the rich first-input gravity assertions need an explicit root-scoped
 * projection instead of accidentally depending on cross-round aggregation.
 */
function projectionForPublicRoot(projection: RoomProjectionState, rootId: string): RoomProjectionState {
  const turn = projection.turnsById[rootId];
  if (!turn) throw new Error(`missing harness root ${rootId}`);
  const activityIds = new Set(turn.activityIds);
  const messageIds = new Set(turn.messageIds);
  const activityOrder = projection.activityOrder.filter((id) => activityIds.has(id));
  const messageOrder = projection.messageOrder.filter((id) => messageIds.has(id));
  return {
    ...projection,
    activityOrder,
    activitiesById: Object.fromEntries(activityOrder.map((id) => [id, projection.activitiesById[id]])),
    messageOrder,
    messagesById: Object.fromEntries(messageOrder.map((id) => [id, projection.messagesById[id]])),
    turnOrder: [rootId],
    turnsById: { [rootId]: turn },
  };
}

const harness = (() => {
  const events = readFileSync(resolve(root, 'room/history.jsonl'), 'utf8')
    .trim().split('\n').map((line) => parseRoomEvent(JSON.parse(line)));
  const snapshot = readRoomFixture('room/snapshot.json');
  const projection = reduceRoomEvents(createRoomProjection(snapshot.room.id), events);
  const focus = buildRoomFocusProjection(snapshot.room, projection);
  const firstRootProjection = projectionForPublicRoot(projection, firstRootId);
  const firstRootFocus = buildRoomFocusProjection(snapshot.room, firstRootProjection);
  return {
    events,
    room: snapshot.room,
    projection,
    focus,
    firstRoot: { projection: firstRootProjection, focus: firstRootFocus },
  };
})() satisfies { events: unknown[]; room: RoomSummary; projection: RoomProjectionState; focus: RoomFocusProjection };

afterEach(() => {
  cleanup();
  useRoomLiveStore.setState({ projections: {} });
});

const machineTokenPattern = /^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/u;

function harnessToolActivities() {
  return harness.projection.activityOrder
    .map((id) => harness.projection.activitiesById[id])
    .filter((activity): activity is NonNullable<typeof activity> => Boolean(activity))
    .filter((activity) => String(activity.payload.sourceEventType ?? '').startsWith('tool'));
}

describe('room gravity projection over the minecraft harness', () => {
  it('recovers the real task counters and tree from 3261 events', () => {
    expect(harness.events).toHaveLength(3261);
    // The first public input is the rich Minecraft collaboration scene. Its
    // own root has one blocked core task and two completed first-round tracks.
    // Every child dispatch is bound to an explicit WorkItem, so the sheet
    // shows each first-round task exactly once — no runtime duplicate rows.
    expect(harness.firstRoot.focus.counts).toEqual({ active: 0, review: 0, blocked: 1, completed: 2 });
    expect(harness.firstRoot.focus.workItems).toHaveLength(3);
    expect(harness.firstRoot.focus.workItems.every((item) => item.source === 'work-item')).toBe(true);

    const blocked = harness.firstRoot.focus.workItems.find((item) => item.state === 'blocked');
    expect(blocked?.id).toBe('room-work:10000000-0000-4000-8000-000000000001');
    expect(blocked?.blocker?.reason).toContain('WorkDocument');

    // The same full projection still drives current Focus, but it is anchored
    // to the latest logical public Root and cannot inherit this old blocker.
    expect(harness.focus.goal.rootId).toBe(latestRootId);
    expect(harness.focus.workItems.map((item) => item.id)).toEqual([
      'room-work:10000000-0000-4000-8000-000000000002',
    ]);
  });

  it('reconstructs the parallel wave with both partner planets on their own tracks', () => {
    const activities = harness.firstRoot.projection.activityOrder
      .map((id) => harness.firstRoot.projection.activitiesById[id])
      .filter((activity): activity is NonNullable<typeof activity> => Boolean(activity));
    const waves = roomDispatchWaves(roomDispatchPlans(activities));

    expect(waves).toHaveLength(1);
    expect(waves[0]?.phaseName).toBe('并行实现纯逻辑与界面轨道');
    expect(waves[0]?.parallelSize).toBe(2);
    expect(waves[0]?.dispatches.map((dispatch) => dispatch.targetDisplayName)).toEqual(['Agent 2', 'Agent 4']);

    // The wave lands on the explicit WorkItems, so the tree can draw lanes.
    const core = harness.firstRoot.focus.workItems.find((item) => item.id.endsWith('000000000001') && item.source === 'work-item');
    const ui = harness.firstRoot.focus.workItems.find((item) => item.id.endsWith('000000000005') && item.source === 'work-item');
    expect(core?.wave?.parallelIndex).toBe(0);
    expect(ui?.wave?.parallelIndex).toBe(1);
    expect(core?.wave?.waveId).toBe(ui?.wave?.waveId);
  });

  it('keeps the dual-axis review verdicts and the verifying planet on completed work', () => {
    const reviewed = harness.firstRoot.focus.workItems.filter((item) => item.review);
    expect(reviewed.length).toBeGreaterThanOrEqual(2);
    for (const item of reviewed) {
      expect(item.review?.operability).toBe('passed');
      expect(item.review?.requirement).toBe('satisfied');
      expect(item.review?.reviewerParticipantId).toBeTruthy();
      expect(item.verifierParticipantId).toBe(item.review?.reviewerParticipantId);
    }
  });

  it('turns every route decision into a readable dispatch packet with a resolved source planet', () => {
    const dispatches = harness.firstRoot.focus.flow.filter((packet) => packet.kind === 'dispatch');
    // The first Root reaches its terminal fence after the opening facilitator
    // route and the two parallel partner routes. Later retry routes are
    // separate historical execution and must not be smuggled into this Root.
    expect(dispatches).toHaveLength(3);
    expect(dispatches.every((packet) => packet.dispatchPlan)).toBe(true);

    // The delegate_batch wave: the coordinator planet pulls both partners.
    const waveDispatches = dispatches.filter((packet) => packet.dispatchPlan?.waveId);
    expect(waveDispatches.map((packet) => packet.summary)).toEqual([
      '伙伴委派 · 交给 Venus · 并行轨道 1/2 · 并行实现纯逻辑与界面轨道',
      '伙伴委派 · 交给 Jupiter · 并行轨道 2/2 · 并行实现纯逻辑与界面轨道',
    ]);
    for (const packet of waveDispatches) {
      expect(packet.sourceParticipantId).toBe('participant:10000000-0000-4000-8000-000000000001');
    }
    // Root routing is Sol's own gravity, never a fabricated planet.
    const opening = dispatches.find((packet) => packet.dispatchPlan?.reason === 'facilitator');
    expect(opening?.sourceParticipantId).toBe('root');
  });

  it('renders the collaboration console as one mesh with owners, blockers and verifiers', () => {
    const { container } = render(<PawRoomFocusOverview focus={harness.firstRoot.focus} onOpenParticipant={vi.fn()} />);

    // Pulse counters mirror the real numbers.
    const pulse = screen.getByLabelText('协作摘要');
    expect(pulse).toHaveTextContent('受阻');
    expect(pulse.querySelectorAll('.paw-room-focus-overview__pulse-bar > i').length).toBeGreaterThanOrEqual(2);

    // Gravity is a planet-to-planet relation graph. WorkItems remain in the
    // round task sheet and detail inspector, so they cannot duplicate the
    // same task as graph nodes here.
    const mesh = screen.getByRole('group', { name: '协作网状图' });
    const projectedMesh = buildRoomFocusMesh(harness.firstRoot.focus);
    // Partner nodes and relation labels are both intentionally keyboard
    // controls: selecting an edge opens its authoritative relation detail.
    fireEvent.click(within(mesh).getByText(/协作关系 ·/));
    expect(within(mesh).getAllByRole('button')).toHaveLength(harness.focus.partners.length + projectedMesh.edges.length);
    expect(mesh.querySelector('.paw-room-focus-overview__mesh-node--work')).toBeNull();
    // The two wave planets stay clickable owners with their live states.
    expect(within(mesh).getByRole('button', { name: /^Venus，/ })).toBeInTheDocument();
    expect(within(mesh).getByRole('button', { name: /^Jupiter，/ })).toBeInTheDocument();

    // Edges exist only between real planets. Work ownership whose other end is
    // a task stays in the task sheet; only recorded partner-to-partner gravity
    // is rendered in this graph.
    expect(projectedMesh.edges.length).toBeGreaterThan(0);
    expect(container.querySelectorAll('.paw-room-focus-overview__mesh-edge-label')).toHaveLength(projectedMesh.edges.length);

    // The strongest WorkItem remains the default detail selection even though
    // tasks are no longer duplicated as gravity nodes.
    expect(screen.getByRole('region', { name: '焦点详情' })).toHaveTextContent('WorkDocument 尚未完成开工与交付同步');

    // The machine enum never leaks into the reader-facing console.
    expect(screen.getByLabelText('Sol 协作态势').textContent).not.toContain('route_decision');
  });

  it('keeps one row per task in the production snapshot window — the paper UI double-count is gone', () => {
    // The first root keeps the rich task-sheet fixture for the one-task/one-row
    // invariant; the production snapshot window is independently asserted as
    // the latest public Root and must not reintroduce old rounds.
    const firstRootObjectives = harness.firstRoot.focus.workItems.map((item) => item.objective);
    expect(harness.firstRoot.focus.workItems).toHaveLength(3);
    expect(new Set(firstRootObjectives).size).toBe(firstRootObjectives.length);

    const page = parseRoomEventSnapshot(readRoomFixture('room/snapshot.json'));
    const projection = reduceRoomEvents(createRoomProjection(harness.room.id), page.events);
    const focus = buildRoomFocusProjection(harness.room, projection);

    expect(focus.goal.rootId).toBe(latestRootId);
    expect(focus.counts).toEqual({ active: 0, review: 0, blocked: 0, completed: 1 });
    expect(focus.workItems).toHaveLength(1);
    expect(focus.workItems[0]?.id).toBe('room-work:10000000-0000-4000-8000-000000000002');
    const objectives = focus.workItems.map((item) => item.objective);
    expect(new Set(objectives).size).toBe(objectives.length);
    // The latest round remains visible with its real long text; historical
    // first-round objectives belong to their own sheet, never this one.
    expect(objectives.some((objective) => objective.includes('Core acceptance migration'))).toBe(true);
    expect(objectives.some((objective) => objective.includes('smoke test'))).toBe(false);
    // The dispatch itself is flow, not a duplicate task row.
    expect(focus.flow.some((packet) => packet.kind === 'dispatch')).toBe(true);
  });

  it('renders the fixture route decisions as dispatch cards in the public chronology', () => {
    // The opening scene up to the parallel delegate_batch wave (seq 68-73).
    const slice = readFileSync(resolve(root, 'room/history.jsonl'), 'utf8')
      .trim().split('\n').map((line) => parseRoomEvent(JSON.parse(line)))
      .filter((event) => event.sequence <= 120);
    const projection = reduceRoomEvents(createRoomProjection(harness.room.id), slice);

    const { container } = render(<PawRoomConversation
      onApprovalDecision={async () => undefined}
      onRetryTurn={() => undefined}
      projection={projection}
      retryingTurn={false}
      room={harness.room}
    />);

    const cards = [...container.querySelectorAll<HTMLElement>('.ccui-tool-card')]
      .filter((card) => card.textContent?.includes('任务分派'));
    expect(cards.length).toBeGreaterThanOrEqual(2);
    const texts = cards.map((card) => card.textContent ?? '');
    expect(texts.some((text) => text.includes('Earth → Venus · 任务分派'))).toBe(true);
    expect(texts.some((text) => text.includes('Earth → Jupiter · 任务分派'))).toBe(true);
    const waveCard = cards.find((card) => card.textContent?.includes('Earth → Venus')) as HTMLElement | undefined;
    expect(waveCard).toHaveTextContent('伙伴委派');
    expect(waveCard).toHaveTextContent('并行轨道 1/2');
    expect(waveCard).toHaveTextContent('并行实现纯逻辑与界面轨道');
    // The real core objective rides with the dispatch, not just an id — a
    // WorkItem paragraph belongs in the receipt body, not in its head line.
    expect(waveCard).not.toHaveTextContent('实现原创 3D 方块生存游戏的纯逻辑核心');
    fireEvent.click(within(waveCard!).getByRole('button'));
    expect(waveCard).toHaveTextContent('实现原创 3D 方块生存游戏的纯逻辑核心');
    // The dead label pattern (planet · 分派 with no plan) is gone.
    expect(container.textContent).not.toContain('route_decision');

    // Every tool receipt names the concrete tool, never the bare category.
    const toolNames = [...container.querySelectorAll('.ccui-tool-main strong')]
      .map((element) => element.textContent?.trim() ?? '');
    expect(toolNames.length).toBeGreaterThanOrEqual(1);
    expect(toolNames).not.toContain('工具');
  });

  it('resolves all 116 machine-id tool summaries in the 3261-event history into evidence lines', () => {
    // The Runtime echoes only the tool id as the summary for 116 of the 290
    // real tool activities (`room_partner`, `agents`, `tool_search`…). Every
    // reader row must resolve to the evidence headline with the honest state.
    const toolActivities = harnessToolActivities();
    expect(toolActivities).toHaveLength(290);
    const machine = toolActivities.filter((activity) => roomToolSummaryIsMachine(activity.summary, activity.payload));
    expect(machine).toHaveLength(116);
    for (const activity of toolActivities) {
      const line = roomToolActivityLine(activity.summary, activity.payload, activity.status);
      expect(line).not.toBe('');
      expect(machineTokenPattern.test(line)).toBe(false);
      expect(line).not.toBe(String(activity.payload.toolName ?? ''));
    }

    // The real parallel wave keeps its true op through the fallback.
    const batch = toolActivities.find((activity) => (
      (activity.payload.arguments as Record<string, unknown> | undefined)?.op === 'delegate_batch'
    ));
    expect(batch && roomToolActivityLine(batch.summary, batch.payload, batch.status))
      .toBe('行星协调 · 批量并行委派 已完成');

    // Op labels stay scoped to their owning tool: the workspace_job/browser
    // status polls in this run are never presented as partner checks.
    const jobStatus = toolActivities.find((activity) => (
      activity.payload.toolName === 'workspace_job'
      && (activity.payload.arguments as Record<string, unknown> | undefined)?.op === 'status'
    ));
    expect(jobStatus).toBeDefined();
    expect(roomToolEvidence(jobStatus!.payload)?.facts.find((fact) => fact.label === '操作')?.value).toBe('status');
  });

  it('keeps every chronology tool row a readable evidence line in the opening scene', () => {
    const slice = harness.events.filter((event) => event.sequence <= 120);
    const projection = reduceRoomEvents(createRoomProjection(harness.room.id), slice);

    const { container } = render(<PawRoomConversation
      onApprovalDecision={async () => undefined}
      onRetryTurn={() => undefined}
      projection={projection}
      retryingTurn={false}
      room={harness.room}
    />);

    const summaries = [...container.querySelectorAll('.ccui-tool-main')]
      .map((main) => main.textContent?.trim() ?? '');
    expect(summaries.length).toBeGreaterThanOrEqual(5);
    // No row text is ever the bare Runtime id (`room_partner`, `tool_search`…).
    expect(summaries.filter((summary) => machineTokenPattern.test(summary))).toEqual([]);
    // The delegate_batch tool call reads as the real gravity it exerted.
    expect(summaries.some((summary) => summary.includes('行星协调 · 批量并行委派 已完成'))).toBe(true);
  });

  it('expands the coordinator satellite to the real sent and modified evidence — no empty tool rows', async () => {
    // The opening scene: the coordinator writes ROOT.md/AGENTS.md and pulls
    // both partners with one delegate_batch (seq 30-110 of the real run).
    const slice = harness.events.filter((event) => event.sequence >= 30 && event.sequence <= 110);
    const projection = reduceRoomEvents(createRoomProjection(harness.room.id), slice);
    useRoomLiveStore.setState({ projections: { [harness.room.id]: projection } });
    const roomGet = readRoomFixture('room/get.json');
    const transport = new MockControlTransport({ routes: { 'agent.room.get': roomGet } });
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    render(
      <ControlTransportProvider transport={transport}>
        <QueryClientProvider client={queryClient}>
          <PawOsSatelliteHost target={{
            kind: 'participant', id: 'participant:10000000-0000-4000-8000-000000000001',
            roomId: harness.room.id, title: 'Agent 3', subtitle: 'coordinator',
          }} />
        </QueryClientProvider>
      </ControlTransportProvider>,
    );

    const timeline = await screen.findByRole('log', { name: '行星公开对话时间线' });
    fireEvent.click(within(timeline).getByRole('button', { name: /工具记录 · 8 项/ }));
    const records = await screen.findByRole('dialog', { name: /工具记录 · 8 项/ });
    const receipts = [...records.querySelectorAll('.ccui-tool-card')];
    const messages = receipts.map((card) => card.querySelector('.ccui-tool-main')?.textContent?.trim() ?? '');
    expect(messages.length).toBeGreaterThanOrEqual(5);
    // Real prose summaries survive; machine ids never surface as row text.
    expect(messages.some((message) => message.includes('已创建 ROOT.md'))).toBe(true);
    expect(messages.some((message) => message.includes('行星协调 · 批量并行委派 已完成'))).toBe(true);
    expect(messages.filter((message) => machineTokenPattern.test(message))).toEqual([]);

    // The write receipt expands to the file it really modified.
    const writeRow = receipts.find((card) => card.textContent?.includes('已创建 ROOT.md')) as HTMLElement;
    expect(writeRow).toBeDefined();
    fireEvent.click(within(writeRow).getByRole('button'));
    expect(writeRow).toHaveTextContent('docs/agent/ROOT.md');
  });
});
