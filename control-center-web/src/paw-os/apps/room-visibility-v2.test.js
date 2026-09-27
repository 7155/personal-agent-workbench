// Host regression: actual dependencies. Isolated equivalents live in ZIP2 validation/.
import { test } from 'vitest';
import assert from 'node:assert/strict';
import * as reducer from '@/contracts/room-reducer';
import { useRoomLiveStore as store } from '@/features/rooms/state/live-store';
import { useRoomProjectionBridge as bridge } from '@/features/rooms/state/projection-bridge';
import * as statusModule from './room-work-status';
import { buildRoomTaskView } from './room-task-view';
const event = (sequence, eventType='room_config_changed', overrides={}) => ({
  schemaVersion:'rag-ime.agent-room-event.v1', eventId:`r:${sequence}`, roomId:'r', sequence,
  turnId:'', participantId:null, sourceSessionId:'', createdAtMs:sequence,
  payload:{}, eventType, resumeToken:`r:${sequence}`, ...overrides,
});
const control = (cursor) => event(cursor+1,'snapshot_required',{eventId:`r:reset:${cursor}`,resumeToken:`r:${cursor}`});
const snapshot = (cursor, first=1) => ({schemaVersion:'rag-ime.agent-room-snapshot.v1',ok:true,
  room:{id:'r',lastEventSequence:cursor},events:Array.from({length:Math.max(0,cursor-first+1)},(_,i)=>event(i+first)),
  firstSequence:cursor?first:0,lastSequence:cursor,resumeToken:cursor?`r:${cursor}`:'',truncated:first>1});
const page = (from,to) => ({roomId:'r',items:Array.from({length:to-from+1},(_,i)=>event(from+i)),firstSequence:from,lastSequence:to,hasMore:false,retainedPrefixTruncated:false});
const task = (id,state='running',overrides={}) => ({id,source:'work-item',objective:`任务 ${id}`,state,
  ownerParticipantId:'mars',expectedOutput:'报告',acceptanceCriteria:['能核对'],reviewRequired:false,evidence:[],updatedAtMs:20,...overrides});
const focus = (tasks=[task('a')]) => ({goal:{title:'测试协作',description:'',rootId:'root',state:'running'},workItems:tasks,
  partners:[{participantId:'mars',sessionId:'s-mars',displayName:'资料研究',celestialName:'Mars',state:'running',currentAction:'核对来源',ownedWorkItemIds:[],unread:false}],
  flow:[],handoffs:[],rootEvidence:[],counts:{active:1,review:0,blocked:0,completed:0}});
const input = (state='running', tasks) => ({focus:focus(tasks),projection:{...reducer.createRoomProjection('r'),turnOrder:['root'],turnsById:{root:{id:'root',status:state,messageIds:[],activityIds:[],participantIds:['mars'],createdAtMs:10,updatedAtMs:30}}},recoveryState:'synced',visible:true});

test('snapshot and cursor are published in one store notification',()=>{
  store.getState().reset();const observations=[];
  const off=store.subscribe(s=>observations.push([s.projections.r?.lastSequence,s.snapshotsByRoomId.r?.lastSequence]));
  store.getState().replaySnapshot('r',snapshot(2));off();
  assert.deepEqual(observations,[[2,2]]);
});
test('accepted live events publish cache and execution atomically',()=>{
  store.getState().reset();store.getState().replaySnapshot('r',snapshot(2));const observations=[];
  const off=store.subscribe(s=>observations.push([s.projections.r.lastSequence,s.historyByRoomId.r.events.at(-1).sequence]));
  store.getState().applyEvents('r',[event(3)]);off();assert.deepEqual(observations,[[3,3]]);
});
test('shell bridge receives exactly the authoritative projection object',()=>{
  store.getState().reset();store.getState().replaySnapshot('r',snapshot(2));
  assert.equal(bridge.getState().projections,store.getState().projections);
  store.getState().applyEvents('r',[event(3)]);assert.equal(bridge.getState().projections.r,store.getState().projections.r);
  store.getState().remove('r');assert.equal(bridge.getState().projections.r,undefined);
});
test('late history cannot clear a live gap or reset its authority',()=>{
  store.getState().reset();store.getState().replaySnapshot('r',snapshot(6,4));store.getState().applyEvents('r',[control(9)]);
  const old=store.getState().projections.r;
  assert.equal(store.getState().prependHistory('r',page(1,3)),false);
  assert.equal(store.getState().projections.r,old);assert.equal(old.needsSnapshot,true);
});
test('empty late history cannot change pagination during recovery',()=>{
  store.getState().reset();store.getState().replaySnapshot('r',snapshot(6,4));store.getState().applyEvents('r',[control(9)]);
  assert.equal(store.getState().prependHistory('r',{...page(1,1),items:[]}),false);
  assert.equal(store.getState().historyByRoomId.r.hasMore,true);
});
test('cold conversation replay is not allowed to clear a reset request',()=>{
  store.getState().reset();store.getState().ensure('r');store.getState().applyEvents('r',[control(9)]);
  assert.equal(store.getState().replayConversationSnapshot('r',{schemaVersion:'rag-ime.agent-room-conversation-snapshot.v1',ok:true,
    room:{id:'r',lastEventSequence:0},events:[],firstEventSequence:0,cursorSequence:0,resumeToken:'',deferredEventCount:0,truncated:false}),false);
  assert.equal(store.getState().projections.r.needsSnapshot,true);
});
test('reset drops the old prefix even when replacement overtakes the old cursor',()=>{
  store.getState().reset();store.getState().replaySnapshot('r',snapshot(20));store.getState().applyEvents('r',[control(5)]);
  store.getState().replaySnapshot('r',snapshot(25,18));
  assert.deepEqual(store.getState().historyByRoomId.r.events.map(e=>e.sequence),[18,19,20,21,22,23,24,25]);
  assert.equal(store.getState().projections.r.needsSnapshot,false);
});
test('invalid full snapshot leaves both cache and projection untouched',()=>{
  store.getState().reset();store.getState().replaySnapshot('r',snapshot(2));const old=store.getState();
  assert.throws(()=>store.getState().replaySnapshot('r',{...snapshot(5),events:[event(3),event(5)],firstSequence:3}));
  assert.equal(store.getState().projections,old.projections);assert.equal(store.getState().historyByRoomId,old.historyByRoomId);
});
test('invalid deferred enrichment does not poison the accepted cache',()=>{
  store.getState().reset();store.getState().replaySnapshot('r',snapshot(2));const old=store.getState();
  assert.throws(()=>store.getState().replaySnapshotWithTail('r',snapshot(2),[event(4)]));
  assert.equal(store.getState().snapshotsByRoomId,old.snapshotsByRoomId);
});
for(const [terminal,expected] of [['completed','completed'],['failed','failed'],['aborted','stopped']])test(`${terminal} Root receipt outranks a stale stopping flag`,()=>{
  const i=input(terminal);i.stopping=true;const s=statusModule.buildRoomWorkStatus(i);assert.equal(s.state,expected);assert.equal(s.animate,false);
});
test('completed Root does not revive a stale pending question',()=>{
  const i=input('completed');i.pendingInput=true;i.projection.pendingUserQuestion={rootId:'root',prompt:'old question'};
  assert.equal(statusModule.buildRoomWorkStatus(i).state,'completed');assert.notEqual(statusModule.buildRoomWorkStatus(i).action,'answer');
});
test('completed Root reports unfinished review separately',()=>{
  const s=statusModule.buildRoomWorkStatus(input('completed',[task('review','review')]));
  assert.equal(s.state,'completed');assert.equal(s.review,1);assert.match(s.detail,/未收束/);assert.equal(s.completed,0);
});
test('a pending grouped input never exposes another Root question text',()=>{
  const i=input();i.pendingInput=true;i.projection.pendingUserQuestion={rootId:'old',prompt:'另一个项目的旧问题'};
  const s=statusModule.buildRoomWorkStatus(i);assert.equal(s.state,'needs-input');assert.doesNotMatch(s.detail,/另一个项目/);
});
for(const invalid of [NaN,Infinity,1e20])test(`invalid optional timestamp ${invalid} cannot poison valid receipt time`,()=>{
  const i=input('running',[task('bad','running',{updatedAtMs:invalid})]);assert.equal(statusModule.buildRoomWorkStatus(i).updatedAtMs,30);
});
for(const state of ['completed','failed','stopped'])test(`terminal ${state} task is not relabelled as an unaccepted offer`,()=>{
  assert.notEqual(statusModule.roomWorkStatusLabel(task('offered',state,{ownerParticipantId:undefined,offeredToParticipantId:'mars'})),'待接收');
});
{
 const view=buildRoomTaskView;
 const f=focus([task('done','completed'),task('busy'),task('review','review'),task('failed','failed'),task('waiting','waiting'),task('stopped','stopped')]);
 test('task filters preserve source order and explicit counts',()=>{const v=view(f,'attention','');assert.deepEqual(v.tasks.map(t=>t.id),['review','failed']);assert.deepEqual(v.counts,{all:6,attention:2,active:1,settled:2});assert.equal(v.selectedTask.id,'failed');});
 test('search accepts both task and real partner names',()=>{assert.equal(view(f,'all','MARS 资料研究 failed').tasks[0].id,'failed');});
 test('search is literal text, not regular expression or HTML',()=>{assert.equal(view(f,'all','.*').tasks.length,0);assert.equal(view(f,'all','<img>').tasks.length,0);});
 test('selection stays pinned when completion removes it from active filter',()=>{const v=view(f,'active','','done');assert.equal(v.selectedTask.id,'done');assert.equal(v.selectedOutsideFilter,true);});
 test('removed selected task is disclosed, not replaced with another',()=>{const v=view(f,'all','','gone');assert.equal(v.selectedTask,undefined);assert.equal(v.selectionUnavailable,true);});
 test('search with no matches keeps manually selected details available',()=>{const v=view(f,'all','no matches','review');assert.equal(v.tasks.length,0);assert.equal(v.selectedTask.id,'review');assert.equal(v.selectedOutsideFilter,true);});
 test('view construction does not mutate task order or task objects',()=>{const before=JSON.stringify(f);view(f,'attention','');assert.equal(JSON.stringify(f),before);});
 test('empty view has no fake selected task or counts',()=>{const v=view(focus([]),'all','');assert.equal(v.selectedTask,undefined);assert.equal(v.counts.all,0);});
}
