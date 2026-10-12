import { afterEach, expect, it, vi } from 'vitest';
import { createPreviewTransport } from './preview-control-transport';

type RecordReply = {session:{id:string};coordinatorId:string;created:boolean;objects:{id:string;sourceSessionId:string;coordinatorId:string;target:{status:string}}[]};
afterEach(()=>vi.useRealTimers());

it('keeps one StarMate owner and admits an idempotent target without sending work',async()=>{
  const transport=createPreviewTransport();
  const owner=await transport.request<RecordReply>({pathId:'agent.coordinator.ensure',body:{}});
  const repeated=await transport.request<RecordReply>({pathId:'agent.coordinator.ensure',body:{}});
  expect(repeated).toMatchObject({created:false,coordinatorId:owner.coordinatorId,session:owner.session});
  const body={sourceSessionId:owner.session.id,action:'create_session',clientRequestId:'exact-create',input:{task:'检查公开资料'}};
  const first=await transport.request<{created:boolean;target:{id:string}}>({pathId:'agent.coordinator.command',body});
  const replay=await transport.request<{created:boolean;target:{id:string}}>({pathId:'agent.coordinator.command',body});
  expect(replay).toMatchObject({created:false,target:first.target});
  expect(first.created).toBe(true);
  const snapshot=await transport.request<{messages:unknown[];status:string}>({pathId:'agent.session.snapshot',params:{sessionId:first.target.id}});
  expect(snapshot.messages).toEqual([]);expect(snapshot.status).toBe('idle');
  expect((await transport.request<RecordReply>({pathId:'agent.coordinator.ensure',body:{}})).objects)
    .toMatchObject([{id:first.target.id,sourceSessionId:owner.session.id,coordinatorId:owner.coordinatorId}]);
  await expect(transport.request({pathId:'agent.coordinator.command',body:{...body,input:{task:'另一项任务'}}})).rejects.toThrow('绑定');
});

it('rotates an archived owner and cannot transplant its objects into the new source',async()=>{
  const transport=createPreviewTransport();
  const old=await transport.request<RecordReply>({pathId:'agent.coordinator.ensure',body:{}});
  await transport.request({pathId:'agent.coordinator.command',body:{sourceSessionId:old.session.id,action:'create_session',clientRequestId:'old-target',input:{task:'旧任务'}}});
  await transport.request({pathId:'agent.session.archive',params:{sessionId:old.session.id},body:{archived:true}});
  const replacement=await transport.request<RecordReply>({pathId:'agent.coordinator.ensure',body:{}});
  expect(replacement.session.id).not.toBe(old.session.id);expect(replacement.coordinatorId).not.toBe(old.coordinatorId);
  expect(replacement.objects).toEqual([]);
  await expect(transport.request({pathId:'agent.coordinator.command',body:{sourceSessionId:old.session.id,action:'create_session',clientRequestId:'retired-owner',input:{task:'不能创建'}}})).rejects.toThrow('已变更');
});

it('stops only its exact admitted turn and does not publish the delayed demo result',async()=>{
  vi.useFakeTimers();const transport=createPreviewTransport();
  const owner=await transport.request<RecordReply>({pathId:'agent.coordinator.ensure',body:{}});
  const created=await transport.request<{target:{id:string}}>({pathId:'agent.coordinator.command',body:{sourceSessionId:owner.session.id,action:'create_session',clientRequestId:'stop-target',input:{task:'停止验收'}}});
  const targetId=created.target.id;
  await transport.request({pathId:'agent.session.prompt',params:{sessionId:targetId},body:{message:'仅本轮',clientMessageId:'stop-turn'}});
  const body={sourceSessionId:owner.session.id,action:'stop',targetId,input:{turnId:'wrong'}};
  await expect(transport.request({pathId:'agent.coordinator.command',body})).rejects.toThrow('指定轮次');
  await expect(transport.request({pathId:'agent.coordinator.command',body:{...body,targetId:'session-preview'}})).rejects.toThrow('不属于');
  await transport.request({pathId:'agent.coordinator.command',body:{...body,input:{turnId:`${targetId}:turn:stop-turn`}}});
  await vi.advanceTimersByTimeAsync(5000);
  const snapshot=await transport.request<{messages:{role:string}[];runtimeQuiescent:boolean;liveEvents:{payload:{status?:string}}[]}>({pathId:'agent.session.snapshot',params:{sessionId:targetId}});
  expect(snapshot.messages.map(item=>item.role)).toEqual(['user']);expect(snapshot.runtimeQuiescent).toBe(true);
  expect(snapshot.liveEvents.at(-1)?.payload.status).toBe('aborted');
});
