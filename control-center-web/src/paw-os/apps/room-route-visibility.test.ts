import { describe, it } from 'vitest';
import assert from 'node:assert/strict';
import { projectRoomRoutes } from './room-route-visibility';
import { visualActivity, visualRoster, visualTurn } from '@/test/fixtures/room-visual';
import type { RoomActivityProjection } from '@/contracts/room-reducer';

const route = (jev: Record<string, unknown> = { status: 'selected', choice: 'mars', confidence: .82, sourceRevision: 'rev-1' }): RoomActivityProjection => visualActivity('route', {
  payload: { dispatchId: 'd', targetParticipantId: 'mars', reason: 'jev', jev },
});
const tool = (overrides: Partial<RoomActivityProjection> = {}): RoomActivityProjection => visualActivity('tool', {
  kind: 'participant_activity', payload: { dispatchId: 'd', sourceEventType: 'tool_started' }, sequence: 2, ...overrides,
});

describe('Existing Jev choices and execution are different receipts', () => {
  it('does not create a Jev record or decision when there is no route event', () => {
    assert.deepEqual(projectRoomRoutes([], visualRoster, visualTurn()), []);
    assert.deepEqual(projectRoomRoutes([route()], visualRoster), []);
  });
  it('a selected choice with confidence is still not a started execution', () => {
    const v = projectRoomRoutes([route()], visualRoster, visualTurn())[0];
    assert.equal(v.decision, 'selected'); assert.equal(v.execution, 'planned'); assert.equal(v.confidence, .82);
  });
  it('exact dispatch, participant, session and root activity proves execution', () => {
    const v = projectRoomRoutes([route(), tool()], visualRoster, visualTurn())[0];
    assert.equal(v.execution, 'started'); assert.equal(v.startedReceiptId, 'tool');
  });
  for (const mismatch of [{ turnId: 'other-root' }, { participantId: 'earth' }, { sourceSessionId: 'ordinary-session' }, { payload: { dispatchId: 'another-dispatch', sourceEventType: 'tool_started' } }]) {
    it(`does not borrow a tool receipt from ${JSON.stringify(mismatch)}`, () => {
      assert.equal(projectRoomRoutes([route(), tool(mismatch)], visualRoster, visualTurn())[0].execution, 'planned');
    });
  }
  it('old root routes and duplicate route event identities do not leak into the view', () => {
    const rows = projectRoomRoutes([visualActivity('old', { turnId: 'old' }), route(), route()], visualRoster, visualTurn());
    assert.equal(rows.length, 1); assert.equal(rows[0].id, 'route');
  });
  it('participant completion alone cannot settle a newer dispatch of that participant', () => {
    const v = projectRoomRoutes([route()], visualRoster, visualTurn({ terminalParticipantIds: ['mars'] }))[0];
    assert.equal(v.execution, 'planned');
  });
  it('exact dispatch terminal is execution returned, never work acceptance', () => {
    const v = projectRoomRoutes([route()], visualRoster, visualTurn({ terminalDispatchIds: ['d'] }))[0];
    assert.equal(v.execution, 'returned'); assert.equal(v.startedReceiptId, undefined); assert.match(v.executionLabel, /验收另计/);
  });
  it('failure and cancellation do not render a successful return', () => {
    assert.equal(projectRoomRoutes([route()], visualRoster, visualTurn({ failedDispatchIds: ['d'] }))[0].execution, 'failed');
    assert.equal(projectRoomRoutes([route()], visualRoster, visualTurn({ abortedDispatchIds: ['d'] }))[0].execution, 'stopped');
  });
  it('a conflicting dispatch owner cannot be treated as execution of the selected target', () => {
    const v = projectRoomRoutes([route(), tool()], visualRoster, visualTurn({ dispatchParticipantIds: { d: 'earth' }, terminalDispatchIds: ['d'] }))[0];
    assert.equal(v.execution, 'unknown');
  });
  it('without dispatch identity, a choice is not correlated using only the name', () => {
    const r = route(); delete r.payload.dispatchId;
    assert.equal(projectRoomRoutes([r, tool()], visualRoster, visualTurn())[0].execution, 'unknown');
  });
  for (const reason of ['explicit_invite', 'mention', 'explicit_mention', 'work_item_owner']) it(`${reason} keeps explicit precedence despite an attached Jev object`, () => {
    const r = route(); r.payload.reason = reason;
    const v = projectRoomRoutes([r], visualRoster, visualTurn())[0]; assert.equal(v.source, 'explicit'); assert.equal(v.decision, 'explicit');
  });
  for (const status of ['abstained', 'unavailable'] as const) it(`${status} preserves the fallback target without calling it a Jev assignment`, () => {
    const r = route({ status, choice: 'unknown', confidence: .2 }); r.payload.reason = 'moderator';
    const v = projectRoomRoutes([r], visualRoster, visualTurn())[0]; assert.equal(v.targetId, 'mars'); assert.equal(v.decision, status); assert.match(v.decisionLabel, /沿用原路由/);
  });
  for (const confidence of [-1, 1.2, NaN, Infinity, '0.9', .69]) it(`invalid/low selected confidence ${confidence} is never elevated to a valid choice`, () => {
    const v = projectRoomRoutes([route({ status: 'selected', choice: 'mars', confidence })], visualRoster, visualTurn())[0];
    assert.equal(v.decision, 'invalid'); assert.equal(v.execution, 'planned');
  });
  it('unknown choice and disagreement with the recorded target are flagged', () => {
    for (const choice of ['unknown', 'invented-person', 'earth']) assert.equal(projectRoomRoutes([route({ status: 'selected', choice, confidence: .99 })], visualRoster, visualTurn())[0].decision, 'invalid');
  });
  it('stale decision receipt stays stale without hiding an independent execution fact', () => {
    const v = projectRoomRoutes([route({ status: 'stale_decision' }), tool()], visualRoster, visualTurn())[0];
    assert.equal(v.decision, 'stale'); assert.equal(v.execution, 'started');
  });
  it('provider/unknown status does not fabricate a decision explanation', () => {
    const v = projectRoomRoutes([route({ status: 'future' })], visualRoster, visualTurn())[0]; assert.equal(v.decision, 'unknown');
  });
  it('removed participant retains historical receipt but cannot open/create a missing session', () => {
    const v = projectRoomRoutes([route()], { participants: [] }, visualTurn())[0]; assert.equal(v.canOpen, false); assert.equal(v.decision, 'selected');
  });
  it('a decision revision is recorded as historical provenance, not revalidated on render', () => {
    const original = route(); const before = JSON.stringify(original);
    const v = projectRoomRoutes([original], visualRoster, visualTurn())[0];
    assert.equal(v.sourceRevision, 'rev-1'); assert.equal(v.raw, original.payload); assert.equal(JSON.stringify(original), before);
  });
  it('ordinary Room routing needs no Jev receipt and no synthetic confidence', () => {
    const v = projectRoomRoutes([visualActivity('rule')], visualRoster, visualTurn())[0];
    assert.equal(v.source, 'room'); assert.equal(v.decision, 'rule'); assert.equal(v.confidence, undefined);
  });
});
