import { useEffect, useState } from 'react';
import { ArrowUpRight, MessageCircle, Network } from 'lucide-react';
import { useOptionalControlTransport } from '@/app/control-transport';
import { usePawOsAppActive, usePawOsDesktop } from '@/features/paw-os/surface-context';
import { usePageVisibility } from '@/platform/use-page-visibility';
import type { RoomSummary } from '@/features/rooms/room-types';
import type { PublicToolResultView } from '../timeline/public-tool-result';
import { CollaborationCopy, ControlledRoomParticipants, controlledRoomParticipants } from './ControlledRoomParticipants';

const object=(v:unknown):Record<string,unknown>=>v&&typeof v==='object'&&!Array.isArray(v)?v as Record<string,unknown>:{};
const text=(v:unknown)=>typeof v==='string'?v:'';
export type CollaborationReceiptTarget={id:string;kind?:'room'|'session';label:string;receipt?:Record<string,unknown>};
/** Only native structured receipts/recorded codemode call arguments. Never parse assistant text or stdout. */
export function collaborationReceiptTargets(view:Pick<PublicToolResultView,'toolId'|'operation'|'rawResult'|'codeMode'>,source:string):CollaborationReceiptTarget[] {
  if (!source || !(view.toolId==='agents'&&view.operation==='coordinator'||view.toolId==='codemode')) return [];
  const targets=new Map<string,CollaborationReceiptTarget>();
  const visit=(value:unknown,depth:number)=>{
    if(depth>4)return;const row=object(value),schema=row.schemaVersion;
    if(schema==='rag-ime.agent-coordinator-create.v1'&&row.ok===true&&row.sourceSessionId===source){const target=object(row.target),kind=row.kind;if(text(target.id)&&(kind==='room'||kind==='session'))targets.set(text(target.id),{id:text(target.id),kind,label:row.created===true?'对象已创建 · 创建本身不会派发':'已有对象已恢复',receipt:row})}
    if(schema==='rag-ime.agent-room-message.v1'&&row.ok===true&&text(row.roomId))targets.set(text(row.roomId),{id:text(row.roomId),kind:'room',label:row.accepted===true?'派发已受理 · 尚非完成结果':row.cancelled===true?'派发已取消':'派发未受理',receipt:row});
    if(schema==='rag-ime.agent-prompt-accepted.v1'&&row.ok===true&&text(row.sessionId))targets.set(text(row.sessionId),{id:text(row.sessionId),kind:'session',label:'请求已受理 · 尚非完成结果',receipt:row});
    for(const key of ['result','details'])if(row[key])visit(row[key],depth+1);
  };
  if(view.toolId==='agents'&&view.rawResult?.format==='json')visit(view.rawResult.value,0);
  for(const call of view.codeMode?.calls??[]){
    if(call.name!=='agents'||call.status!=='ok')continue;
    try{const args=object(JSON.parse(call.args));if(args.op==='coordinator'&&['prompt','stop','resume'].includes(text(args.action))&&text(args.targetId)&&!targets.has(text(args.targetId)))targets.set(text(args.targetId),{id:text(args.targetId),label:'原生调用已返回 · 当前状态见原对象'})}catch{/* Missing/truncated arguments remain in the raw receipt, without invented links. */}
  }
  return [...targets.values()].slice(0,8);
}
export type OwnedCollaborationReference={id:string;kind:'room'|'session';title:string;room?:RoomSummary};
/** Exact source + coordinator identity and persisted target identity, never recent-history similarity. */
export function ownedCollaborationReferences(value:unknown,source:string,identity:string):OwnedCollaborationReference[]{
  if(!identity||!Array.isArray(value))return[];
  return value.flatMap<OwnedCollaborationReference>(raw=>{const row=object(raw),target=object(row.target);if(row.sourceSessionId!==source||row.coordinatorId!==identity||!text(row.id)||target.id!==row.id||!text(target.title)||target.status==='archived'||!(row.kind==='room'||row.kind==='session'))return[];
    if(row.kind==='room'&&!Array.isArray(target.participants))return[];
    return[{id:text(row.id),kind:row.kind,title:text(target.title),...(row.kind==='room'?{room:target as unknown as RoomSummary}:{})}];
  }).slice(0,100);
}
export function CollaborationReceiptLinks({view,sourceSessionId}:{view:PublicToolResultView;sourceSessionId?:string}){
  const candidates=collaborationReceiptTargets(view,sourceSessionId??'');
  return candidates.length&&sourceSessionId?<VerifiedCollaborationLinks candidates={candidates} source={sourceSessionId}/>:null;
}
/** One object row per target even when creation and prompt receipts share a group. */
export function CollaborationReceiptGroup({views,sourceSessionId}:{views:PublicToolResultView[];sourceSessionId?:string}){
  const targets=new Map<string,CollaborationReceiptTarget>();
  for(const view of views)for(const target of collaborationReceiptTargets(view,sourceSessionId??''))targets.set(target.id,target);
  return targets.size&&sourceSessionId?<VerifiedCollaborationLinks candidates={[...targets.values()]} source={sourceSessionId}/>:null;
}
export function collaborationDispatchFacts(receipt:Record<string,unknown>|undefined,room:RoomSummary|undefined){
  if(!room||!Array.isArray(receipt?.dispatches))return[];
  return receipt.dispatches.slice(0,8).flatMap(value=>{const row=object(value),participant=controlledRoomParticipants(room).find(p=>p.id===row.participantId&&p.sessionId===row.sessionId);if(!participant)return[];
    return[{participantId:participant.id,sessionId:participant.sessionId,displayName:participant.displayName,dispatchId:text(row.dispatchId),sessionTurnId:text(row.sessionTurnId)}];
  });
}
function VerifiedCollaborationLinks({candidates,source}:{candidates:CollaborationReceiptTarget[];source:string}) {
  const transport=useOptionalControlTransport(),desktop=usePawOsDesktop(),visible=usePageVisibility(),surface=usePawOsAppActive()??true;
  const candidateKey=candidates.map(c=>`${c.kind??''}:${c.id}`).join('|');
  const [directory,setDirectory]=useState<{source:string;transport:typeof transport;items:OwnedCollaborationReference[]}>();
  const [failed,setFailed]=useState(false);
  useEffect(()=>{
    if(!candidateKey||!visible||!surface||!transport)return;let current=true;setFailed(false);setDirectory(undefined);
    void transport.request<Record<string,unknown>>({pathId:'agent.coordinator.command',body:{sourceSessionId:source,action:'read'}}).then(response=>{
      if(!current)return;if(response.ok!==true||response.sourceSessionId!==source||typeof response.coordinatorId!=='string'){setFailed(true);return}
      setDirectory({source,transport,items:ownedCollaborationReferences(response.objects,source,response.coordinatorId)});
    },()=>{if(current)setFailed(true)});return()=>{current=false};
  },[candidateKey,source,transport,visible,surface]);
  if(!transport)return null;
  const current=directory?.source===source&&directory.transport===transport?directory.items:[];
  const references=candidates.flatMap(receipt=>{const owned=current.find(item=>item.id===receipt.id&&(!receipt.kind||item.kind===receipt.kind));return owned?[{...owned,receipt}]:[]});
  if(!references.length)return <small className="paw-collaboration-receipt">{failed||directory?'协作对象未核对，原始回执保留在详情。':'正在核对原协作对象…'}</small>;
  return <>{references.map(reference=><section className="paw-collaboration-receipt" key={`${reference.kind}:${reference.id}`} aria-label="原协作对象">
    <button type="button" className="paw-collaboration-receipt__open" disabled={!desktop} aria-label={`打开原 ${reference.kind==='room'?'Room':'Session'} ${reference.title}`} onClick={()=>desktop?.openWindow({appId:'agent',target:{kind:reference.kind,id:reference.id,title:reference.title}})}>{reference.kind==='room'?<Network size={16}/>:<MessageCircle size={16}/>}<strong>{reference.title}</strong><ArrowUpRight size={13}/></button><small>{reference.receipt.label}</small>
    {reference.room?<ControlledRoomParticipants room={reference.room} active={surface&&visible}/>:null}
    <details className="paw-collaboration-identity"><summary>派发与标识详情</summary><code>{reference.id}</code><CollaborationCopy value={reference.id} label="协作对象 ID"/>{['roomTurnId','turnId','clientMessageId','clientRequestId'].flatMap(key=>{const value=text(reference.receipt.receipt?.[key]);return value?<div key={key}><span>{key} </span><code>{value}</code><CollaborationCopy value={value} label={key}/></div>:[]})}{collaborationDispatchFacts(reference.receipt.receipt,reference.room).map(dispatch=><div key={`${dispatch.participantId}:${dispatch.dispatchId}`}><strong>{dispatch.displayName} · 原派发</strong>{(['sessionId','dispatchId','sessionTurnId'] as const).map(key=>dispatch[key]?<div key={key}><span>{key} </span><code>{dispatch[key]}</code><CollaborationCopy value={dispatch[key]} label={`${dispatch.displayName} ${key}`}/></div>:null)}</div>)}</details>
  </section>)}</>;
}
