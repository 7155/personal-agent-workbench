import { afterEach, describe, expect, it } from 'vitest';
import { MockControlTransport } from '@/test/mock-transport';
import { commandJevPlan, uncertainJevPlan, type JevPlanCommand } from './jev-execution';
import { commandJevAssignment, pendingJevAssignment, type JevAssignmentInput } from './jev-task-assignment';
import { commandJevRevision, pendingJevRevision, type JevRevisionInput } from './jev-task-revision';

afterEach(() => sessionStorage.clear());

const room = 'cold-room';
const taskKey = JSON.stringify([room, 'graph', 'task']);
const plan: JevPlanCommand = { action: 'approve_plan', graphId: 'graph', rootId: 'root', planHash: 'plan-hash' };
const assignment: JevAssignmentInput = { action: 'request_reclaim', graphId: 'graph', taskId: 'task', taskHash: 'task-hash', targetParticipantId: 'mars', reason: 'handoff' };
const revision: JevRevisionInput = { action: 'revise_task', graphId: 'graph', rootId: 'root', taskId: 'task', taskHash: 'task-hash', expectedTopologyRevision: 2, expectedRequirementsRevision: 1, objective: 'revised task', expectedOutput: 'result', acceptanceCriteria: ['verified'], reason: 'new requirement' };

const cases = [
  { name: 'plan', prefix: 'paw.jev.plan.v1', key: room, input: plan,
    invalid: { ...plan, action: 'unknown_action' }, receipt: { ok: true, graphId: 'graph' },
    read: (transport: MockControlTransport) => uncertainJevPlan(transport, room),
    submit: (transport: MockControlTransport) => commandJevPlan(transport, room, plan) },
  { name: 'assignment', prefix: 'paw.jev.assignment.v1', key: taskKey, input: assignment,
    invalid: { ...assignment, taskId: 'another-task' }, receipt: { status: 'requested' },
    read: (transport: MockControlTransport) => pendingJevAssignment(transport, room, 'graph', 'task'),
    submit: (transport: MockControlTransport) => commandJevAssignment(transport, room, assignment) },
  { name: 'revision', prefix: 'paw.jev.revision.v1', key: taskKey, input: revision,
    invalid: { ...revision, graphId: 'another-graph' }, receipt: { ok: true, graphId: 'graph', changedTaskId: 'task', revisionId: 'revision', status: 'awaiting_drain' },
    read: (transport: MockControlTransport) => pendingJevRevision(transport, room, 'graph', 'task'),
    submit: (transport: MockControlTransport) => commandJevRevision(transport, room, revision) },
];

describe.each(cases)('$name persisted command recovery', fixture => {
  function restore(input: unknown) {
    // A never-used connection has no live owner. Seed the historical public
    // browser format directly so this cannot pass through an in-memory entry.
    const identity = `cold:${crypto.randomUUID()}`;
    const transport = new MockControlTransport({ routes: { 'agent.jev.command': fixture.receipt } });
    Object.defineProperty(transport, 'connectionIdentity', { value: identity });
    const key = `${fixture.prefix}:${encodeURIComponent(identity)}:${encodeURIComponent(fixture.key)}`;
    sessionStorage.setItem(key, JSON.stringify({ input, clientMessageId: 'saved-request', uncertain: false }));
    return { transport, key };
  }

  it('restores the original intent without sending and retries only on an explicit command', async () => {
    const { transport, key } = restore(fixture.input);
    expect(fixture.read(transport)).toEqual(fixture.input);
    expect(transport.requests).toHaveLength(0);
    await fixture.submit(transport);
    expect(transport.requests).toHaveLength(1);
    expect(transport.requests[0].request).toMatchObject({ params: { roomId: room }, body: { ...fixture.input, clientMessageId: 'saved-request' } });
    expect(fixture.read(transport)).toBeUndefined();
    expect(sessionStorage.getItem(key)).toBeNull();
  });

  it('rejects a malformed or differently bound saved intent without executing it', () => {
    const { transport } = restore(fixture.invalid);
    expect(fixture.read(transport)).toBeUndefined();
    expect(transport.requests).toHaveLength(0);
  });
});
