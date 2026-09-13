import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Button } from '@/components/primitives';
import { useControlTransport } from '@/app/control-transport';
import { LabSessionRecord } from '../projects/LabSessionRecord';
import { labConnectionKey, requestLabControl } from '../control-request';
import { object } from './types';

/** Observe the owning Session; reads never restart a model call. */
export function GoldenRunRecord({ sessionId, turnId, active = false }: { sessionId: string; turnId?: string; active?: boolean }) {
  const [open, setOpen] = useState(false);
  const transport = useControlTransport();
  const [readTranscript, setReadTranscript] = useState(false);
  const query = useQuery({
    queryKey: ['golden-run-record', labConnectionKey(transport), sessionId],
    queryFn: ({ signal }) => requestLabControl(transport, { pathId: 'agent.session.snapshot', params: { sessionId }, signal }),
    enabled: open || active, retry: false, staleTime: 0, refetchInterval: active ? 5_000 : false,
  });
  const snapshot = object(query.data);
  const errors = (Array.isArray(snapshot.items) ? snapshot.items : []).flatMap((item) => {
    const message = object(item);
    return (Array.isArray(message.blocks) ? message.blocks : []).filter((block) => object(block).type === 'error')
      .map((block) => object(object(block).data).message).filter((text): text is string => typeof text === 'string');
  });
  const missingRuntime = errors.some((error) => /Cannot find module|ERR_MODULE_NOT_FOUND/.test(error));
  const latestRuntimeEvent = (Array.isArray(snapshot.liveEvents) ? snapshot.liveEvents : []).map(object).reverse()
    .find((event) => ['status_changed', 'provider_request_completed', 'reasoning_summary', 'text_delta', 'message_completed'].includes(String(event.eventType)));
  const progress = object(latestRuntimeEvent?.payload);
  const retrying = active && latestRuntimeEvent?.eventType === 'status_changed' && progress.phase === 'provider_retry';
  return <div className="golden-run-record">
    {retrying && !query.isError ? <p role="status" aria-label="当前模型运行状态">{typeof progress.summary === 'string' ? progress.summary : 'Runtime 正在恢复模型连接。'}{typeof progress.delayMs === 'number' && progress.delayMs > 0 ? ` · 本次等待 ${Math.ceil(progress.delayMs / 1000)} 秒` : ''}</p> : null}
    {active && query.isError ? <p role="status">当前模型运行状态暂未确认，已有请求不会重新发送。</p> : null}
    <Button size="small" variant="quiet" aria-expanded={open} onClick={() => setOpen((value) => !value)}>查看原运行记录</Button>
    {open ? <div className="golden-run-record__body">
      {query.isPending ? <p role="status">正在读取原对话…</p> : query.isError ? <><p role="alert">暂时无法读取原记录。已有请求没有重新发送。</p><Button size="small" onClick={() => void query.refetch()}>重新读取运行记录</Button></> : <>
        {missingRuntime ? <p className="golden-field-error">本机模型运行组件缺失。请先更新或修复 Pi Runtime，再返回起草步骤重新运行。</p> : null}
        {errors.length ? <details open={!missingRuntime}><summary>模型返回的错误</summary>{errors.map((error, index) => <p className="golden-preserve-text" key={index}>{error}</p>)}</details> : <p>原对话已保留，可以打开查看消息和执行状态。</p>}
        <Button size="small" onClick={() => setReadTranscript(true)}>在此阅读原{turnId ? '回合' : '对话'}</Button>
        {readTranscript ? <LabSessionRecord sessionId={sessionId} turnId={turnId} onClose={() => setReadTranscript(false)} /> : null}
      </>}
    </div> : null}
  </div>;
}
