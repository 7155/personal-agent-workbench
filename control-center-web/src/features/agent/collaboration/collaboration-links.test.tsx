import { useEffect } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { PawOsDesktopProvider } from '@/features/paw-os/surface-context';
import { MockControlTransport } from '@/test/mock-transport';
import type { RoomSummary } from '@/features/rooms/room-types';
import { ActivitySummary } from '../timeline/ActivitySummary';
import type { PublicToolResultView } from '../timeline/public-tool-result';
import { createAgentProjection, reduceAgentEvent } from '@/contracts/agent-reducer';
import type { UiAgentEvent } from '@/contracts/ui-events';
import { agentProjectionKey, agentSessionAddress, useAgentLiveStore } from '../state/live-store';
import type { AgentLiveSessionOptions } from '../runtime/use-agent-live-session';
import { CollaborationReceiptLinks, collaborationReceiptTargets, collaborationDispatchFacts, ownedCollaborationReferences } from './CollaborationReceiptLinks';
import { ControlledRoomParticipants, collaborationSessionState, controlledRoomParticipants } from './ControlledRoomParticipants';
vi.mock('../runtime/use-agent-live-session',()=>({useAgentLiveSession:({active,sessionId,transport,onSnapshot,onRecoveryState}:AgentLiveSessionOptions)=>{
 useEffect(()=>{if(active){onRecoveryState?.('synced');onSnapshot?.({sessionId,value:{},view:'recent',presentable:true,hydrated:true,sequence:1,resumeToken:'1'})}},[active,sessionId,transport]);return vi.fn();
}}));
afterEach(()=>{cleanup();useAgentLiveStore.setState({projections:{}})});
const room:RoomSummary={id:'owned-room',title:'证据协作',status:'active',routingPolicy:'parallel',moderatorParticipantId:'one',updatedAtMs:100,participants:[
{id:'one',sessionId:'worker-one',roleId:'research',roleVersion:'1',displayName:'证据伙伴',collaborationRole:'researcher',ordinal:1,status:'active'},
{id:'two',sessionId:'worker-two',roleId:'review',roleVersion:'1',displayName:'复核伙伴',collaborationRole:'reviewer',ordinal:2,status:'active'}]};
const owned={id:room.id,kind:'room',sourceSessionId:'source',coordinatorId:'owner',task:'核对证据',target:room};
const create={schemaVersion:'rag-ime.agent-coordinator-create.v1',sourceSessionId:'source',coordinatorId:'owner',ok:true,created:true,kind:'room',target:room};
const view=(value:unknown,format:'json'|'text'='json'):PublicToolResultView=>({toolId:'agents',operation:'coordinator',toolLabel:'协作',summary:'结果',resultKind:'structured',fields:[],request:[],resultItems:[],sources:[],rawResult:{format,value}});
function event(id:string,sequence:number,eventType:UiAgentEvent['eventType'],payload:Record<string,unknown>):UiAgentEvent{return {schemaVersion:'rag-ime.agent-event.v1',sessionId:id,eventId:`${id}:${sequence}`,turnId:'original-turn',sequence,createdAtMs:sequence*100,eventType,payload,resumeToken:`${id}:${sequence}`,streamKind:'agent'}}
function projection(id:string,completed=false){let state=reduceAgentEvent(createAgentProjection(id),event(id,1,'status_changed',{status:'busy'})).state;if(completed){state=reduceAgentEvent(state,event(id,2,'text_delta',{delta:'已保存答复'})).state;state=reduceAgentEvent(state,event(id,3,'turn_completed',{status:'completed'})).state}return state}
function setup(v=view(create),objects:unknown[]=[owned]){
 const transport=new MockControlTransport({routes:{'agent.coordinator.command':{ok:true,sourceSessionId:'source',coordinatorId:'owner',objects}}}),openWindow=vi.fn();
 useAgentLiveStore.setState({projections:{[agentProjectionKey(agentSessionAddress(transport,'worker-one'))]:projection('worker-one'),[agentProjectionKey(agentSessionAddress(transport,'worker-two'))]:projection('worker-two',true)}});
 render(<ControlTransportProvider transport={transport}><PawOsDesktopProvider openWindow={openWindow}><textarea aria-label="保留草稿" defaultValue="继续核对的草稿"/><CollaborationReceiptLinks view={v} sourceSessionId="source"/></PawOsDesktopProvider></ControlTransportProvider>);
 return {transport,openWindow};
}
describe('source-bound collaboration links',()=>{
 it('links an exact original result without turning unknown disposition into completion',async()=>{
  const result={schemaVersion:'rag-ime.agent-coordinator-result.v1',ok:true,evidenceOnly:true,sourceSessionId:'source',kind:'room',targetId:room.id,state:'unknown',execution:{roomId:room.id,roomTurnId:'original-room-turn'},artifacts:[]};
  const {openWindow}=setup(view(result));
  const button=await screen.findByRole('button',{name:'打开原 Room 证据协作'});expect(screen.getByText('已读取原轮证据 · 终态待核对')).toBeInTheDocument();
  fireEvent.click(button);expect(openWindow).toHaveBeenCalledWith({appId:'agent',target:{kind:'room',id:room.id,title:room.title}});
  expect(screen.getByText('original-room-turn')).toBeInTheDocument();
  expect(collaborationReceiptTargets(view({...result,sourceSessionId:'foreign'}),'source')).toEqual([]);
  expect(collaborationReceiptTargets(view({...result,execution:{...result.execution,roomId:'foreign'}}),'source')).toEqual([]);
  expect(collaborationReceiptTargets(view({...result,evidenceOnly:false}),'source')).toEqual([]);
 });
 it('accepts typed coordinator receipts, rejects text/cross-source and keeps ownership exact',()=>{
  expect(collaborationReceiptTargets(view({result:create}),'source')).toMatchObject([{id:room.id,kind:'room'}]);
  expect(collaborationReceiptTargets(view({...create,sourceSessionId:'other'}),'source')).toEqual([]);
  expect(collaborationReceiptTargets(view(JSON.stringify(create),'text'),'source')).toEqual([]);
  expect(collaborationReceiptTargets({...view(create),toolId:'read'},'source')).toEqual([]);
  expect(ownedCollaborationReferences([owned,{...owned,id:'other-room',sourceSessionId:'other',target:{...room,id:'other-room'}}],'source','owner')).toHaveLength(1);
 });
 it('does not trust arbitrary codemode return objects and rejects dispatch identities outside the persisted roster',()=>{
  expect(collaborationReceiptTargets({...view(create),toolId:'codemode'},'source')).toEqual([]);
  const dispatch={participantId:'one',sessionId:'worker-one',dispatchId:'original-dispatch',sessionTurnId:'original-worker-turn'};
  expect(collaborationDispatchFacts({dispatches:[dispatch,{...dispatch,sessionId:'foreign-worker'}]},room)).toEqual([{...dispatch,displayName:'证据伙伴'}]);
 });
 it('lists only the active persisted roster, without reviving removed or muted partners',()=>{
  expect(controlledRoomParticipants({...room,participants:[room.participants[0],{...room.participants[1],status:'removed'},{...room.participants[1],id:'muted',sessionId:'muted-worker',status:'muted'}]}).map(p=>p.sessionId)).toEqual(['worker-one']);
 });
 it('uses only successful recorded native codemode coordinator targets',()=>{
  const v={...view(undefined),toolId:'codemode',codeMode:{calls:[{id:'actual-call',name:'agents',status:'ok' as const,args:JSON.stringify({op:'coordinator',action:'prompt',targetId:room.id})},{id:'cancelled',name:'agents',status:'cancelled' as const,args:JSON.stringify({op:'coordinator',action:'prompt',targetId:'unowned'})}]}};
  expect(collaborationReceiptTargets(v,'source')).toMatchObject([{id:room.id}]);
 });
 it('keeps accepted separate from completed and opens original Room/workers while preserving draft',async()=>{
  const {openWindow,transport}=setup(view({schemaVersion:'rag-ime.agent-room-message.v1',ok:true,accepted:true,roomId:room.id,roomTurnId:'original-room-turn'}));
  await screen.findByRole('button',{name:'打开原 Room 证据协作'});expect(screen.getByText('派发已受理 · 尚非完成结果')).toBeInTheDocument();
  await waitFor(()=>expect(screen.getByText('1 运行中 · 1 本轮完成')).toBeInTheDocument());
  expect(document.querySelector('.paw-collaboration-partner__name strong')).toHaveTextContent('证据伙伴');
  fireEvent.click(screen.getByText('子智能体',{exact:false,selector:'summary'}));
  expect(screen.getByRole('heading',{name:'运行中 1'})).toBeInTheDocument();expect(screen.getByRole('heading',{name:'本轮完成 1'})).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'打开 Mars 的原 Session'}));
  expect(openWindow).toHaveBeenLastCalledWith({appId:'agent',target:{kind:'session',id:'worker-one',title:'Mars · 证据伙伴'}});
  fireEvent.click(screen.getByRole('button',{name:'打开原 Room 证据协作'}));expect(openWindow).toHaveBeenLastCalledWith({appId:'agent',target:{kind:'room',id:room.id,title:room.title}});
  expect(screen.getByRole('textbox',{name:'保留草稿'})).toHaveValue('继续核对的草稿');
  const key=agentProjectionKey(agentSessionAddress(transport,'worker-one'));
  act(()=>useAgentLiveStore.setState(state=>({projections:{...state.projections,[key]:reduceAgentEvent(state.projections[key],event('worker-one',2,'turn_completed',{status:'aborted'})).state}})));
  await waitFor(()=>expect(screen.getByText('1 本轮完成 · 1 已停止')).toBeInTheDocument());
 });
 it('shows original target navigation beside a collapsed native Tool record, retaining raw details',async()=>{
  const transport=new MockControlTransport({routes:{'agent.coordinator.command':{ok:true,sourceSessionId:'source',coordinatorId:'owner',objects:[owned]}}});
  render(<ControlTransportProvider transport={transport}><PawOsDesktopProvider openWindow={vi.fn()}><ActivitySummary inline sessionId="source" activities={[{id:'native-tool',turnId:'source-turn',kind:'tool_finished',status:'completed',summary:'协作已返回',payload:{toolId:'agents',operation:'coordinator',result:{result:create}},createdAtMs:1,updatedAtMs:2}]}/></PawOsDesktopProvider></ControlTransportProvider>);
  await screen.findByRole('button',{name:'打开原 Room 证据协作'});
  const toggle=document.querySelector('.agent-activity--inline > summary');expect(toggle).toHaveAttribute('aria-expanded','false');
  fireEvent.click(toggle!);await waitFor(()=>expect(document.querySelector('.agent-activity-row > summary')).not.toBeNull());fireEvent.click(document.querySelector('.agent-activity-row > summary')!);expect(await screen.findByText('完整返回')).toBeInTheDocument();
  expect(transport.requests.filter(call=>call.request.pathId==='agent.coordinator.command').every(call=>call.request.body&&typeof call.request.body==='object'&&!Array.isArray(call.request.body)&&call.request.body.action==='read')).toBe(true);
 });
 it('never navigates unknown or cross-source targets',async()=>{
  setup(view({...create,target:{...room,id:'unowned'}}));await screen.findByText('协作对象未核对，原始回执保留在详情。');expect(screen.queryByRole('button',{name:/打开原 Room/u})).not.toBeInTheDocument();
 });
 it('keeps exact stopped/failed outcomes and ignores harmless empty bootstrap',()=>{
  const empty=createAgentProjection('empty');empty.turnOrder=['bootstrap'];empty.turnsById.bootstrap={id:'bootstrap',status:'completed',messageIds:[],activityIds:[],createdAtMs:1,updatedAtMs:1};expect(collaborationSessionState(empty).state).toBe('idle');
  const stopped=reduceAgentEvent(projection('worker'),event('worker',2,'turn_completed',{status:'aborted'})).state;expect(collaborationSessionState(stopped).state).toBe('stopped');
  stopped.turnsById['original-turn'].status='failed';expect(collaborationSessionState(stopped).state).toBe('failed');expect(collaborationSessionState({...stopped,needsSnapshot:true}).state).toBe('unknown');
 });
 it('makes inactive counts unknown instead of reviving old live state',()=>{
  render(<ControlTransportProvider transport={new MockControlTransport()}><ControlledRoomParticipants room={room} active={false}/></ControlTransportProvider>);expect(screen.getByText('2 待核对')).toBeInTheDocument();expect(screen.queryByText(/运行中/u)).not.toBeInTheDocument();
 });
});
