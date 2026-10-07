import { useCallback, useEffect, useState } from 'react';
import { Check, Circle, CircleDashed, Copy, Square, TriangleAlert } from 'lucide-react';
import type { AgentProjectionState } from '@/contracts/agent-reducer';
import { useControlTransport } from '@/app/control-transport';
import { usePawOsAppActive, usePawOsDesktop } from '@/features/paw-os/surface-context';
import { usePageVisibility } from '@/platform/use-page-visibility';
import { useAgentLiveSession, type AgentRecoveryState } from '../runtime/use-agent-live-session';
import { agentSessionAddress, selectAgentProjection, useAgentLiveStore } from '../state/live-store';
import { RoomPlanetAvatar } from '@/features/rooms/RoomPlanetAvatar';
import { roomCollaborationRoleLabel, roomPlanetName } from '@/features/rooms/room-copy';
import type { RoomParticipant, RoomSummary } from '@/features/rooms/room-types';
import './collaboration-links.css';

export type CollaborationSessionState = 'running' | 'waiting' | 'completed' | 'failed' | 'stopped' | 'idle' | 'unknown';
/** A ready bootstrap is not a completed turn. Newer failed/stopped/tool-only turns win. */
export function collaborationSessionState(current?: AgentProjectionState): { state: CollaborationSessionState; label: string; time?: number; turnId?: string } {
  if (!current || current.needsSnapshot) return { state:'unknown',label:'状态待核对' };
  const turn = [...current.turnOrder].reverse().map(id=>current.turnsById[id]).find(turn=>turn && (turn.status !== 'completed' || turn.activityIds.length || turn.messageIds.some(id=>['user','assistant'].includes(current.messagesById[id]?.role??''))));
  const facts={time:turn?.updatedAtMs,turnId:turn?.id};
  if (current.durableRecovery?.paused) return {state:'waiting',label:'已暂停',...facts};
  if (['aborting','stopping'].includes(current.status)) return {state:'waiting',label:'正在停止',...facts};
  if (['busy','analyzing','working','retrying'].includes(current.status) || turn?.status==='running') return {state:'running',label:'运行中',...facts};
  if (current.status==='waiting' || turn?.status==='waiting') return {state:'waiting',label:'等待输入',...facts};
  if (['failed','faulted'].includes(current.status) || turn?.status==='failed') return {state:'failed',label:'本轮失败',...facts};
  if (turn?.status==='aborted') return {state:'stopped',label:'已停止',...facts};
  return turn?.status==='completed' ? {state:'completed',label:'本轮完成',...facts} : {state:'idle',label:'就绪'};
}
export function controlledRoomParticipants(room: Pick<RoomSummary,'participants'>): RoomParticipant[] {
  const seen=new Set<string>();
  return (Array.isArray(room.participants)?room.participants:[]).filter(p=>{
    if (!p || p.status!=='active' || typeof p.id!=='string' || !p.id || typeof p.sessionId!=='string' || !p.sessionId || typeof p.displayName!=='string' || !Number.isInteger(p.ordinal) || p.ordinal<0 || seen.has(p.sessionId)) return false;
    seen.add(p.sessionId);return true;
  }).slice(0,8);
}
export function CollaborationCopy({ value, label }: {value:string;label:string}) {
  const [result,setResult]=useState('');
  return <><button type="button" className="paw-collaboration-copy" aria-label={`复制 ${label}`} onClick={()=>{void navigator.clipboard.writeText(value).then(()=>setResult('已复制'),()=>setResult('无法复制，请选择标识'))}}><Copy size={12}/></button>{result?<small role="status">{result}</small>:null}</>;
}
export function ControlledRoomParticipants({ room, active=true, onOpenSession }: {room:RoomSummary;active?:boolean;onOpenSession?:(id:string,title:string)=>void}) {
  const participants=controlledRoomParticipants(room);
  const [states,setStates]=useState<Record<string,CollaborationSessionState>>({});
  const report=useCallback((id:string,state:CollaborationSessionState)=>setStates(previous=>previous[id]===state?previous:{...previous,[id]:state}),[]);
  const counts=participants.reduce<Record<string,number>>((all,p)=>{const state=active?states[p.sessionId]??'unknown':'unknown';all[state]=(all[state]??0)+1;return all},{});
  const summary=[counts.running&&`${counts.running} 运行中`,counts.completed&&`${counts.completed} 本轮完成`,counts.waiting&&`${counts.waiting} 等待`,counts.failed&&`${counts.failed} 失败`,counts.stopped&&`${counts.stopped} 已停止`,counts.idle&&`${counts.idle} 就绪`,counts.unknown&&`${counts.unknown} 待核对`].filter(Boolean).join(' · ');
  if (!participants.length) return null;
  const groups: {id:string;label:string;states:CollaborationSessionState[]}[]=[
    {id:'running',label:'运行中',states:['running']},{id:'waiting',label:'等待',states:['waiting']},
    {id:'completed',label:'本轮完成',states:['completed']},{id:'failed',label:'需查看',states:['failed']},
    {id:'stopped',label:'本轮已停止',states:['stopped']},{id:'idle',label:'就绪 / 待核对',states:['idle','unknown']},
  ];
  // Keep controllers as stable siblings when a status changes group. Nesting
  // them under group containers would remount the lease and lose synced state.
  const rows=groups.flatMap(group=>{
    const members=participants.filter(p=>group.states.includes(active?states[p.sessionId]??'unknown':'unknown'));
    return members.length?[<li key={`group:${group.id}`} className="paw-collaboration-group-heading" role="presentation"><h3>{group.label} <span>{members.length}</span></h3></li>,...members.map(participant=><ControlledParticipant key={`${room.id}:${participant.sessionId}`} participant={participant} active={active} report={report} onOpenSession={onOpenSession}/>)]:[];
  });
  return <details className="paw-collaboration-partners"><summary>子智能体 <span title="原 Session 的当前本轮状态；不表示成果验收或后台进程退出">{summary}</span></summary><ul>{rows}</ul></details>;
}
function ControlledParticipant({ participant,active,report,onOpenSession }: {participant:RoomParticipant;active:boolean;report:(id:string,state:CollaborationSessionState)=>void;onOpenSession?:(id:string,title:string)=>void}) {
  const transport=useControlTransport(),desktop=usePawOsDesktop();
  const visible=usePageVisibility(),surface=usePawOsAppActive()??true;
  const [synced,setSynced]=useState(false),[recovery,setRecovery]=useState<AgentRecoveryState>('recovering');
  const projection=useAgentLiveStore(store=>selectAgentProjection(store,agentSessionAddress(transport,participant.sessionId)));
  const present=active&&visible&&surface;
  useAgentLiveSession({sessionId:participant.sessionId,transport,active:present,snapshotView:'recent',onSnapshot:()=>setSynced(true),onRecoveryState:setRecovery});
  const facts=collaborationSessionState(present&&synced&&recovery==='synced'?projection:undefined);
  useEffect(()=>report(participant.sessionId,facts.state),[participant.sessionId,facts.state,report]);
  const name=roomPlanetName(participant.ordinal),title=`${name} · ${participant.displayName}`;
  const mark=facts.state==='completed'?<Check size={12}/>:facts.state==='failed'?<TriangleAlert size={12}/>:facts.state==='stopped'?<Square size={11}/>:facts.state==='unknown'?<CircleDashed size={12}/>:<Circle size={11}/>;
  return <li data-state={facts.state}><button type="button" className="paw-collaboration-partner" disabled={!desktop&&!onOpenSession} aria-label={`打开 ${name} 的原 Session`} onClick={()=>onOpenSession?onOpenSession(participant.sessionId,title):desktop?.openWindow({appId:'agent',target:{kind:'session',id:participant.sessionId,title}})}>
    <RoomPlanetAvatar ordinal={participant.ordinal} size={28} decorative activity={facts.state==='running'?'working':'static'}/><span className="paw-collaboration-partner__name"><strong>{participant.displayName}</strong><small>{roomCollaborationRoleLabel(participant.collaborationRole)} · {name}</small></span><span className="paw-collaboration-partner__status">{mark}{recovery==='failed'&&facts.state==='unknown'?'同步失败':facts.label}{facts.time?<time dateTime={new Date(facts.time).toISOString()}>{new Date(facts.time).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</time>:null}</span>
  </button><details className="paw-collaboration-identity"><summary>标识详情</summary><code>{participant.sessionId}</code><CollaborationCopy value={participant.sessionId} label={`${name} Session ID`}/>{facts.turnId?<><code>{facts.turnId}</code><CollaborationCopy value={facts.turnId} label={`${name} turn ID`}/></>:null}</details></li>;
}
