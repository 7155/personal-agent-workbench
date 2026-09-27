// The isolated review harness executes the same scenarios. This host version
// uses the real Zustand store and all real dependency owners (no test linker).
import { test } from 'vitest';
import assert from 'node:assert/strict';
import * as reducer from '@/contracts/room-reducer';
import { useRoomLiveStore as store } from '@/features/rooms/state/live-store';
import * as focusModule from './room-focus-projection';
import { buildRoomWorkStatus, roomWorkStatusLabel, roomPartnerStatusLabel } from './room-work-status';
import * as window from '@/features/rooms/state/room-event-window';
const event = (sequence, eventType='room_config_changed', overrides={}) => ({
  schemaVersion:'rag-ime.agent-room-event.v1',eventId:`r:${sequence}`,roomId:'r',sequence,
  turnId:'',eventType,participantId:null,sourceSessionId:'',createdAtMs:sequence,
  payload:{},resumeToken:`r:${sequence}`, ...overrides,
});
const control = (cursor) => event(cursor+1,'snapshot_required',{eventId:`r:snapshot-required:${cursor}`,resumeToken:`r:${cursor}`});
const state = (cursor=0) => ({...reducer.createRoomProjection('r'),lastSequence:cursor,lastEventId:`r:${cursor}`,resumeToken:`r:${cursor}`});
const snapshot = (cursor, first=1) => ({schemaVersion:'rag-ime.agent-room-snapshot.v1',ok:true,
  room:{id:'r',lastEventSequence:cursor}, events:Array.from({length:cursor-first+1},(_,i)=>event(i+first)),
  firstSequence:cursor?first:0,lastSequence:cursor,resumeToken:cursor?`r:${cursor}`:'',truncated:first>1});
const turn = (id='new',status='running') => ({id,status,messageIds:[],activityIds:[],participantIds:['earth','mars'],createdAtMs:10,updatedAtMs:20});
const participant = (id,ordinal=0) => ({id,sessionId:`s-${id}`,ordinal,displayName:id,status:'active',collaborationRole:ordinal?'specialist':'coordinator'});
const work = (id,overrides={}) => ({id,roomId:'r',rootTurnId:'new',rootWorkId:id,parentWorkId:'',topicId:'',
 objective:id,expectedOutput:'result',acceptanceCriteria:['proof'],accountableParticipantId:'earth',currentOwnerParticipantId:'mars',offeredToParticipantId:'',createdByParticipantId:'earth',clientMessageId:'',state:'active',depth:0,revision:1,resultSummary:'',artifactRefs:[],evidenceRefs:[],review:{operabilityVerdict:'',requirementVerdict:'',evidenceRefs:[],reason:'',reviewerParticipantId:'',reviewedAtMs:0},blocker:{},acceptedTurnId:'',createdAtMs:10,updatedAtMs:20,completedAtMs:0,...overrides});
const room = (workItems=[]) => ({id:'r',title:'Review',participants:[participant('earth'),participant('mars',1)],workItems});

for(const cursor of [3,9,10,30]) test(`recovery control cursor=${cursor} preserves durable cursor=10`,()=>{
 const initial=state(10), result=reducer.reduceRoomEvent(initial,control(cursor));
 assert.equal(result.disposition,'snapshot-required');assert.equal(result.state.needsSnapshot,true);
 assert.equal(result.state.lastSequence,10);assert.equal(result.state.resumeToken,'r:10');
 assert.equal(result.state.recoveryCursor,cursor);
});
test('foreign recovery control cannot rewind this room',()=>{
 const initial=state(10);assert.equal(reducer.reduceRoomEvent(initial,{...control(3),roomId:'other'}).state,initial);
});
test('historical snapshot marker remains an inert diagnostic',()=>{
 const result=reducer.reduceRoomEvent(state(2),event(3,'snapshot_required'),{snapshotReplay:true});
 assert.equal(result.disposition,'applied');assert.equal(result.state.needsSnapshot,false);
});
test('rejected gap tail is not cached as authoritative history',()=>{
 store.getState().reset();store.getState().replaySnapshot('r',snapshot(1));
 assert.equal(store.getState().applyEvents('r',[event(2),event(4),event(5)]),true);
 assert.equal(store.getState().projections.r.lastSequence,2);
 assert.deepEqual(store.getState().historyByRoomId.r.events.map(e=>e.sequence),[1,2]);
 assert.equal(store.getState().snapshotsByRoomId.r.lastSequence,2);
});
test('transient recovery event is never stored in snapshot history',()=>{
 store.getState().reset();store.getState().replaySnapshot('r',snapshot(2));store.getState().applyEvents('r',[control(9)]);
 assert.equal(store.getState().snapshotsByRoomId.r.lastSequence,2);
 assert.deepEqual(store.getState().historyByRoomId.r.events.map(e=>e.eventType),['room_config_changed','room_config_changed']);
});
test('fresh control authorizes rewind without mixing old cached history',()=>{
 store.getState().reset();store.getState().replaySnapshot('r',snapshot(20));
 store.getState().applyEvents('r',[control(5)]);
 assert.equal(store.getState().replaySnapshot('r',snapshot(5,3)),true);
 assert.equal(store.getState().projections.r.needsSnapshot,false);
 assert.deepEqual(store.getState().historyByRoomId.r.events.map(e=>e.sequence),[3,4,5]);
 assert.equal(store.getState().projections.r.lastSequence,5);
});
test('ordinary delayed snapshot cannot rewind a healthy stream',()=>{
 store.getState().reset();store.getState().replaySnapshot('r',snapshot(20));
 assert.equal(store.getState().replaySnapshot('r',snapshot(5)),false);assert.equal(store.getState().projections.r.lastSequence,20);
});
test('new empty root cannot borrow another root failure',()=>{
 const p=state();p.turnOrder=['old','new'];p.turnsById={old:turn('old','failed'),new:turn()};
 const f=focusModule.buildRoomFocusProjection(room([work('old-work',{rootTurnId:'old',state:'failed'})]),p);
 assert.equal(f.workItems.length,0);assert.equal(f.goal.rootId,'new');assert.equal(f.counts.blocked,0);
});
test('genuinely unbound tasks remain visible during metadata skew',()=>{
 const p=state();p.turnOrder=['new'];p.turnsById={new:turn()};
 assert.equal(focusModule.buildRoomFocusProjection(room([work('draft',{rootTurnId:''})]),p).workItems[0].id,'draft');
});
test('offered recipient is not an accepted executor',()=>{
 const f=focusModule.buildRoomFocusProjection(room([work('offer',{currentOwnerParticipantId:'',offeredToParticipantId:'mars',state:'queued'})]));
 assert.equal(f.workItems[0].ownerParticipantId,undefined);assert.equal(f.workItems[0].offeredToParticipantId,'mars');
});
test('accountability alone is not execution ownership',()=>{
 const f=focusModule.buildRoomFocusProjection(room([work('not-assigned',{currentOwnerParticipantId:'',state:'queued'})]));
 assert.equal(f.workItems[0].ownerParticipantId,undefined);assert.equal(f.workItems[0].accountableParticipantId,'earth');
});
{
 const view=(options={})=>{
  const p=state(20);p.turnOrder=['new'];p.turnsById={new:turn()};
  const r=room([work('build')]); const f=focusModule.buildRoomFocusProjection(r,p);
  return {focus:f,projection:p,recoveryState:'synced',visible:true,...options};
 };
 test('parallel execution uses current root evidence',()=>{
  const r=buildRoomWorkStatus(view());assert.equal(r.state,'running');assert.equal(r.animate,true);assert.deepEqual(r.executingParticipantIds,['earth','mars']);
 });
 for(const recoveryState of ['failed','recovering'])test(`${recoveryState} cannot animate cached execution`,()=>{
  const r=buildRoomWorkStatus(view({recoveryState}));assert.equal(r.animate,false);assert.equal(r.live,false);assert.equal(r.updatedAtMs,20);assert.deepEqual(r.executingParticipantIds,[]);
 });
 test('pending snapshot gap suppresses animation even on connected socket',()=>{
  const i=view();i.projection.needsSnapshot=true;assert.equal(buildRoomWorkStatus(i).state,'syncing');assert.equal(buildRoomWorkStatus(i).animate,false);
 });
 test('hidden document pauses display without declaring execution stopped',()=>{
  const r=buildRoomWorkStatus(view({visible:false}));assert.equal(r.state,'paused-view');assert.equal(r.animate,false);
 });
 test('explicit human question wins over execution animation',()=>{
  const i=view();i.projection.pendingUserQuestion={postId:'q',rootId:'new',prompt:'Which branch?',roomId:'r',sequence:10,options:[]};
  const r=buildRoomWorkStatus(i);assert.equal(r.state,'needs-input');assert.equal(r.action,'answer');assert.equal(r.animate,false);
 });
 test('old root question does not interrupt new root',()=>{
  const i=view();i.projection.pendingUserQuestion={postId:'q',rootId:'old',prompt:'old'};assert.equal(buildRoomWorkStatus(i).state,'running');
 });
 test('internal activity approval id does not invent a user gate',()=>{
  const i=view();i.projection.turnsById.new.activityIds=['a'];i.projection.activitiesById.a={id:'a',turnId:'new',participantId:'earth',status:'running',kind:'participant_activity',summary:'tool',createdAtMs:10,payload:{approvalId:'automatic',causalMetadata:{roomBound:true}}};
  assert.notEqual(buildRoomWorkStatus(i).state,'needs-input');
 });
 test('coordinator terminal and active partners is waiting for partners, not failure',()=>{
  const i=view();i.projection.turnsById.new.terminalParticipantIds=['earth'];i.focus.partners[0].state='completed';
  const r=buildRoomWorkStatus(i);assert.equal(r.state,'waiting-partners');assert.deepEqual(r.executingParticipantIds,['mars']);
 });
 test('all participant terminals do not invent a root success',()=>{
  const i=view();i.projection.turnsById.new.terminalParticipantIds=['earth','mars'];
  const r=buildRoomWorkStatus(i);assert.equal(r.state,'awaiting-root');assert.equal(r.animate,false);
 });
 test('returned execution and task acceptance remain separate',()=>{
  const i=view();i.projection.turnsById.new.terminalParticipantIds=['earth','mars'];i.focus.workItems[0].state='review';
  const r=buildRoomWorkStatus(i);assert.equal(r.state,'review');assert.equal(r.completed,0);assert.equal(r.review,1);assert.equal(r.animate,false);
 });
 test('terminal root with stale active work item is not restarted',()=>{
  const i=view();i.projection.turnsById.new.status='completed';const r=buildRoomWorkStatus(i);assert.equal(r.state,'completed');assert.equal(r.animate,false);assert.equal(r.completed,0);assert.match(r.detail,/未收束/);
 });
 test('metadata-only active work never animates',()=>{
  const i=view();i.projection=state();const r=buildRoomWorkStatus(i);assert.equal(r.animate,false);assert.equal(r.state,'idle');
 });
 test('dispatch returns are not counted as accepted work items',()=>{
  const i=view();i.focus.workItems.push({...i.focus.workItems[0],id:'runtime',source:'runtime',state:'completed'});const r=buildRoomWorkStatus(i);assert.equal(r.total,1);assert.equal(r.completed,0);
 });
 test('pending stop is not a terminal receipt',()=>{
  const r=buildRoomWorkStatus(view({stopping:true}));assert.equal(r.state,'stopping');assert.equal(r.animate,false);
 });
 for(const [terminal,expected]of [['failed','failed'],['aborted','stopped']])test(`${terminal} root overrides stale running tools`,()=>{
  const i=view();i.projection.turnsById.new.status=terminal;const r=buildRoomWorkStatus(i);assert.equal(r.state,expected);assert.equal(r.animate,false);
 });
 test('work, runtime and participant completion use different text',()=>{
  assert.equal(roomWorkStatusLabel({state:'completed',source:'runtime'}),'执行已返回');assert.equal(roomWorkStatusLabel({state:'completed',source:'work-item'}),'工作项已完成');assert.equal(roomPartnerStatusLabel('completed'),'本轮执行结束');
 });

 test('tail merge deduplicates and sorts only this room domain events',()=>{
  assert.deepEqual(window.appendRoomEventWindow('r',[event(1)],[event(3),event(2),event(2),event(4,'room_config_changed',{roomId:'other'}),control(9)]).map(e=>e.sequence),[1,2,3]);
 });
 test('conflicting identities cannot be silently collapsed',()=>{
  assert.throws(()=>window.appendRoomEventWindow('r',[],[event(1),event(1,'room_config_changed',{eventId:'different'})]),/Conflicting/);
 });
 test('snapshot before recovery highwater is rejected',()=>{
  assert.equal(window.canApplyRoomSnapshot({...state(20),needsSnapshot:true,recoveryCursor:8},7),false);
 });
 test('snapshot must reach a forward recovery control before sync resumes',()=>{
  assert.equal(window.canApplyRoomSnapshot({...state(10),needsSnapshot:true,recoveryCursor:30},20),false);
  assert.equal(window.canApplyRoomSnapshot({...state(10),needsSnapshot:true,recoveryCursor:30},30),true);
 });
 test('invalid snapshot cursors cannot enter the cache',()=>{
  for(const seq of [-1,NaN,Infinity,1.5]) assert.equal(window.canApplyRoomSnapshot(state(),seq),false);
 });
 test('acceptance requires completed work and both explicit review verdicts',()=>{
  const review={operability:'passed',requirement:'satisfied'};
  assert.equal(roomWorkStatusLabel({source:'work-item',state:'completed',review}),'已验收');
  assert.notEqual(roomWorkStatusLabel({source:'work-item',state:'review',review}),'已验收');
  assert.notEqual(roomWorkStatusLabel({source:'work-item',state:'completed',review:{operability:'passed',requirement:'failed'}}),'已验收');
 });

 test('explicit retry keeps its work without borrowing an unrelated root',()=>{
  const p=state();p.turnOrder=['other','old','new'];p.turnsById={other:turn('other','failed'),old:turn('old','failed'),new:{...turn(),retryOfRootId:'old',activityIds:['retry-progress']}};
  p.activityOrder=['retry-progress'];p.activitiesById['retry-progress']={id:'retry-progress',turnId:'new',participantId:'mars',status:'running',kind:'participant_activity',summary:'retry is checking evidence',sequence:10,createdAtMs:10,updatedAtMs:10,payload:{workItemId:'retry-work'}};
  const f=focusModule.buildRoomFocusProjection(room([work('unrelated',{rootTurnId:'other',state:'failed'}),work('retry-work',{rootTurnId:'old'})]),p);
  assert.deepEqual(f.workItems.map(w=>w.id),['retry-work']);assert.equal(f.goal.rootId,'new');assert.equal(f.workItems[0].currentAction,'retry is checking evidence');
 });
 test('bounded retry history retains a recorded missing parent identity',()=>{
  const p=state();p.turnOrder=['new'];p.turnsById={new:{...turn(),retryOfRootId:'pruned-parent'}};
  const f=focusModule.buildRoomFocusProjection(room([work('retained',{rootTurnId:'pruned-parent'})]),p);
  assert.equal(f.workItems[0].id,'retained');assert.equal(f.goal.rootId,'new');
 });

}
