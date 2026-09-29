import { describe, expect, it } from 'vitest';
import { createRoomProjection } from '@/contracts/room-reducer';
import { createCollaborationDemo, DEMO_LABELS } from '../../../e2e/fixtures/jev-collaboration-v4-data';
import { buildJevCollaboration, collaborationNeighborhood, layoutJevCollaboration } from './jev-collaboration-model';

const names = ['Earth', 'Mars', 'Venus', 'Mercury', 'Jupiter', 'Saturn', 'Uranus', 'Neptune'];
const build = (fixture: ReturnType<typeof createCollaborationDemo>, projection?: ReturnType<typeof createRoomProjection>) => buildJevCollaboration(
  fixture.graph, fixture.room, fixture.mission, DEMO_LABELS, i => names[i]!, projection,
);

describe('V4 collaboration projection on the production contracts', () => {
  it('separates ownership, current verifier and accepted task count', () => {
    const model = build(createCollaborationDemo('mixed'));
    expect(model.nodes).toHaveLength(5);
    expect(model.counts.accepted).toBe(1);
    const reader = model.nodes.find(n => n.id === 'reader')!;
    expect(reader.owner?.name).toBe('Venus');
    expect(reader.runs.find(r => r.purpose === 'verify')?.person?.name).toBe('Earth');
  });
  it('keeps topology stable across status updates and ignores reference edges for waiting', () => {
    const first = layoutJevCollaboration(build(createCollaborationDemo('mixed')));
    expect(first.positions.find(p => p.id === 'reader')?.depth).toBe(0);
    expect(first.positions.find(p => p.id === 'integration')?.depth).toBe(1);
    expect(layoutJevCollaboration(build(createCollaborationDemo('mixed', 2))).positions).toEqual(first.positions);
  });
  it('excludes old task revision dispatches', () => {
    expect(build(createCollaborationDemo('returned')).nodes.find(n => n.id === 'reader')?.runs).toEqual([]);
  });
  it.each(['roomId', 'rootId', 'graphId'])('rejects contradictory %s on a dispatch', field => {
    const f = createCollaborationDemo('mixed');
    const effect = f.graph.effects[0]!;
    effect.request[field] = 'another-scope';
    expect(build(f).runs.some(r => r.id === effect.effectId)).toBe(false);
  });
  it('does not invent missing participants or turn selected models into actual usage', () => {
    const f = createCollaborationDemo('mixed');
    const effect = f.graph.effects[0]!;
    effect.request.ownerId = 'missing'; effect.state = 'pending'; effect.receipt = {};
    effect.request.contextManifest = { executionScope: { modelSelection: { modelId: 'candidate' } } };
    const run = build(f).runs.find(r => r.id === effect.effectId)!;
    expect(run.person).toBeUndefined(); expect(run.model).toBe('');
  });
  it('does not fabricate execution or artifacts for a pending plan', () => {
    const model = build(createCollaborationDemo('plan'));
    expect(model.planned).toBe(true); expect(model.runs).toEqual([]);
    expect(model.nodes.every(n => n.refs.length === 0)).toBe(true);
    expect(model.counts.accepted).toBe(0);
  });
  it('keeps missing dependencies and cycles explicit without fake completion', () => {
    const f = createCollaborationDemo('mixed');
    f.graph.edges.push({ prerequisite: 'missing', dependent: 'api', kind: 'requires' });
    expect(build(f).nodes).toHaveLength(5); expect(build(f).notices.length).toBeGreaterThan(0);
    const layout = layoutJevCollaboration(build(createCollaborationDemo('cycle')));
    expect(layout.unresolved.length).toBeGreaterThan(0);
    expect(layout.positions).toHaveLength(5);
    expect(layout.positions.every(p => Number.isFinite(p.x) && Number.isFinite(p.y))).toBe(true);
  });
  it('highlights ancestors and descendants, not an unrelated shared prerequisite', () => {
    const neighborhood = collaborationNeighborhood(build(createCollaborationDemo('mixed')), 'integration');
    expect([...neighborhood]).toEqual(expect.arrayContaining(['api', 'reader', 'delivery']));
    expect(neighborhood.has('formats')).toBe(false);
    expect(build(createCollaborationDemo('empty')).nodes).toEqual([]);
  });
  it('binds public actions to Root, participant, Session and dispatch; accepts missing sequence', () => {
    const f = createCollaborationDemo('mixed');
    const projection = createRoomProjection(f.room.id);
    const rows = [
      ['legacy', undefined, 'dispatch-api-exec', 'demo-root-turn', 'demo-session-1'],
      ['latest', 2, 'dispatch-api-exec', 'demo-root-turn', 'demo-session-1'],
      ['other-dispatch', 3, 'other', 'demo-root-turn', 'demo-session-1'],
      ['other-root', 4, 'dispatch-api-exec', 'other', 'demo-session-1'],
      ['other-session', 5, 'dispatch-api-exec', 'demo-root-turn', 'other'],
    ] as const;
    for (const [id, sequence, dispatchId, rootId, session] of rows) {
      projection.activityOrder.push(id);
      projection.activitiesById[id] = {
        id, sequence, participantId: 'p1', sourceSessionId: session, turnId: rootId,
        summary: id, payload: { rootId, dispatchId }, kind: 'tool', status: 'running', createdAtMs: 1,
      };
    }
    expect(build(f, projection).runs.find(r => r.id === 'api-exec')?.latest).toBe('latest');
    delete projection.activitiesById.latest;
    projection.activityOrder = projection.activityOrder.filter(id => id !== 'latest');
    expect(build(f, projection).runs.find(r => r.id === 'api-exec')?.latest).toBe('legacy');
  });
  it('does not mutate snapshot inputs', () => {
    const f = createCollaborationDemo('mixed'); const before = JSON.stringify(f);
    build(f); expect(JSON.stringify(f)).toBe(before);
  });
});
