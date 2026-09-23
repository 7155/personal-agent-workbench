import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button } from '@/components/primitives';
import { useRichHtmlUrl } from '@/features/agent/file-preview/use-rich-html-url';
import { commandLabApp, externalWorkspaceUrl, pendingLabAppCommands, type LabApp, type LabAppCall, type LabAppCommand, type LabAppVersion } from './apps';
import { object, type JsonValue } from './types';
import { projectCommandRejected, projectError } from './api';

const bridge = `<script>(()=>{
  const pending=new Map();
  const request=(kind,fields={},onProgress)=>new Promise((resolve,reject)=>{
    const requestId=crypto.randomUUID();pending.set(requestId,{resolve,reject,onProgress});
    parent.postMessage({kind,requestId,...fields},'*');
  });
  window.pawApp=Object.freeze({mode:'paw',capabilities:Object.freeze({progress:true,cancel:true}),
    ready:()=>parent.postMessage({kind:'paw.lab-app.ready',requestId:crypto.randomUUID()},'*'),
    invoke:(actionId,input={},options={})=>{
      const requestId=crypto.randomUUID();
      const stop=()=>parent.postMessage({kind:'paw.lab-app.cancel',requestId},'*');
      const result=new Promise((resolve,reject)=>{
        pending.set(requestId,{resolve,reject,onProgress:options.onProgress});
        parent.postMessage({kind:'paw.lab-app.invoke',requestId,actionId,input,model:options.model},'*');
        options.signal?.addEventListener('abort',stop,{once:true});if(options.signal?.aborted)stop();
      });
      return result.finally(()=>options.signal?.removeEventListener('abort',stop));
    },
    models:()=>request('paw.lab-app.models'),
    storage:Object.freeze({get:key=>request('paw.lab-app.state.get',{key}),set:(key,value)=>request('paw.lab-app.state.set',{key,value})}),
    reconcile:(callId)=>request('paw.lab-app.reconcile',{callId}),
    history:()=>request('paw.lab-app.history')});
  addEventListener('message',e=>{
    if(e.source!==parent||!['paw.lab-app.result','paw.lab-app.progress'].includes(e.data?.kind))return;
    const p=pending.get(e.data.requestId);if(!p)return;
    if(e.data.kind==='paw.lab-app.progress'){try{p.onProgress?.(e.data.progress)}catch{}return}
    pending.delete(e.data.requestId);
    if(e.data.ok)p.resolve(e.data.result);
    else p.reject(Object.assign(new Error(e.data.message||'应用调用未完成'),{state:e.data.state,requestId:e.data.requestId}));
  });
})();</script>`;
const policy = `<meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'">`;

export function LabAppPreview({ app, version, calls, selectedCallId = '', onActivity }: { app: LabApp; version: LabAppVersion; calls: LabAppCall[]; selectedCallId?: string; onActivity: () => void }) {
  const transport = useControlTransport(); const frame = useRef<HTMLIFrameElement>(null);
  const inFlight = useRef(new Set<string>()); const delivered = useRef(new Set<string>());
  const requestCalls = useRef(new Map<string, string>());
  const progressSent = useRef(new Map<string, string>());
  const cancelWanted = useRef(new Set<string>()); const cancelSent = useRef(new Set<string>());
  const [integratedProgress, setIntegratedProgress] = useState(false);
  const [workspaceVisible, setWorkspaceVisible] = useState(false);
  const [workspaceOpened, setWorkspaceOpened] = useState(false);
  const external = version.spec.externalWorkspace;
  const split = external?.presentation === 'split';
  const destination = externalWorkspaceUrl(external?.url);
  // A cross-origin workspace keeps its own service identity and API cookies.
  // Never allow an App-authored URL to mount this control surface as a sibling.
  const workspaceUrl = destination && new URL(destination).origin !== window.location.origin ? destination : null;
  const localWorkspace = workspaceUrl ? ['127.0.0.1', 'localhost', '[::1]'].includes(new URL(workspaceUrl).hostname) : false;
  const [workspaceConnection, setWorkspaceConnection] = useState<'checking' | 'ready' | 'unavailable'>('checking');
  const [workspaceRetry, setWorkspaceRetry] = useState(0);
  useEffect(() => {
    if (!workspaceUrl || (!workspaceOpened && !split) || !localWorkspace) return;
    const controller = new AbortController();
    let active = true;
    const timeout = window.setTimeout(() => controller.abort(), 3000);
    setWorkspaceConnection('checking');
    void fetch(workspaceUrl, { method: 'HEAD', mode: 'no-cors', credentials: 'omit', cache: 'no-store', signal: controller.signal })
      .then(() => { if (active) setWorkspaceConnection('ready'); })
      .catch(() => { if (active) setWorkspaceConnection('unavailable'); })
      .finally(() => window.clearTimeout(timeout));
    return () => { active = false; controller.abort(); window.clearTimeout(timeout); };
  }, [workspaceUrl, workspaceOpened, split, localWorkspace, workspaceRetry]);
  const workspaceReady = !localWorkspace || workspaceConnection === 'ready';
  const [error, setError] = useState('');
  const [pending, setPending] = useState<LabAppCommand[]>(() => pendingLabAppCommands(transport, app.appId)
    .filter((command) => command.action === 'invoke' ? command.input.version === version.version : ['cancel', 'resume'].includes(command.action)));
  const html = useMemo(() => `<!doctype html>${policy}${bridge}${version.html}`, [version.html]);
  const previewUrl = useRichHtmlUrl(html);
  useEffect(() => { setIntegratedProgress(false); }, [version.html]);
  const send = useCallback(async (command: LabAppCommand, requestId?: string) => {
    setError('');
    try {
      const receipt = await commandLabApp(transport, command);
      setPending((items) => items.filter((item) => item.clientRequestId !== command.clientRequestId));
      if (receipt.call && requestId) requestCalls.current.set(receipt.call.callId, requestId);
      onActivity();
    } catch (reason) {
      const definite = projectCommandRejected(reason);
      setPending((items) => [...items.filter((item) => item.clientRequestId !== command.clientRequestId), ...(definite ? [] : [command])]);
      const message = projectError(reason, '应用请求暂未确认，已保留原输入。'); setError(message);
      if (definite && requestId) frame.current?.contentWindow?.postMessage({ kind: 'paw.lab-app.result', requestId, ok: false, state: 'rejected', message }, '*');
      if (definite) onActivity();
    }
  }, [transport, onActivity]);
  useEffect(() => {
    if (selectedCallId) return;
    const receive = (event: MessageEvent) => {
      if (!frame.current || event.source !== frame.current.contentWindow) return;
      const data = object(event.data);
      if (typeof data.requestId !== 'string' || !/^[a-f0-9-]{36}$/u.test(data.requestId)) return;
      if (data.kind === 'paw.lab-app.ready') { setIntegratedProgress(true); return; }
      if (data.kind === 'paw.lab-app.state.get' || data.kind === 'paw.lab-app.state.set') {
        try {
          if (typeof data.key !== 'string' || !/^[a-zA-Z0-9_.:-]{1,80}$/u.test(data.key)) throw new Error('应用状态名称无效。');
          const key = `paw.lab.app-state.v1:${transport.connectionIdentity}:${app.appId}:${data.key}`;
          let result: JsonValue = null;
          if (data.kind === 'paw.lab-app.state.set') {
            const serialized = JSON.stringify(data.value);
            if (!serialized || serialized.length > 128_000) throw new Error('应用状态超过 128,000 字符，请导出文件保存。');
            localStorage.setItem(key, serialized);
          } else {
            result = JSON.parse(localStorage.getItem(key) ?? 'null') as JsonValue;
          }
          frame.current?.contentWindow?.postMessage({ kind: 'paw.lab-app.result', requestId: data.requestId, ok: true, result }, '*');
        } catch {
          frame.current?.contentWindow?.postMessage({ kind: 'paw.lab-app.result', requestId: data.requestId, ok: false, state: 'rejected', message: '浏览器未能读取或保存应用状态。当前输入仍可使用，请导出文件留存。' }, '*');
        }
        return;
      }
      if (data.kind === 'paw.lab-app.cancel') {
        if (inFlight.current.has(data.requestId)) {
          cancelWanted.current.add(data.requestId);
          const call = calls.find((item) => requestCalls.current.get(item.callId) === data.requestId
            && item.appId === app.appId && item.version === version.version);
          if (call && ['queued', 'running', 'interrupted'].includes(call.state) && !cancelSent.current.has(call.callId)) {
            cancelSent.current.add(call.callId);
            void send({ action: 'cancel', appId: app.appId, expectedRevision: app.revision,
              clientRequestId: `app-frame-cancel:${data.requestId}`, input: { callId: call.callId } });
          }
          onActivity();
        }
        return;
      }
      if (data.kind === 'paw.lab-app.models') {
        void transport.request({ pathId: 'agent.role.models' }).then(catalog => {
          frame.current?.contentWindow?.postMessage({ kind: 'paw.lab-app.result', requestId: data.requestId, ok: true,
            result: { catalog, selected: version.spec.model } }, '*');
        }).catch(reason => frame.current?.contentWindow?.postMessage({ kind: 'paw.lab-app.result', requestId: data.requestId,
          ok: false, message: projectError(reason, '模型列表暂时不可读。') }, '*'));
        return;
      }
      if (data.kind === 'paw.lab-app.reconcile') {
        const call = calls.find(item => item.callId === data.callId || requestCalls.current.get(item.callId) === data.callId);
        frame.current?.contentWindow?.postMessage({ kind: 'paw.lab-app.result', requestId: data.requestId, ok: Boolean(call),
          result: call ? { requestId: call.callId, version: call.version, actionId: call.actionId, input: call.input,
            model: call.model, state: call.state, result: call.result, message: call.error, progress: call.progress } : undefined,
          message: '原请求尚未确认，请通过上方的核对原操作检查接收状态。' }, '*');
        onActivity();
        return;
      }
      if (data.kind === 'paw.lab-app.history') {
        const result = calls.filter((call) => call.appId === app.appId)
          .sort((left, right) => right.createdAtMs - left.createdAtMs).slice(0, 20)
          .map((call) => ({ requestId: call.callId, version: call.version, actionId: call.actionId,
            input: call.input, model: call.model, state: call.state, result: call.result, message: call.error, progress: call.progress,
            createdAtMs: call.createdAtMs, updatedAtMs: call.updatedAtMs }));
        frame.current?.contentWindow?.postMessage({ kind: 'paw.lab-app.result', requestId: data.requestId, ok: true, result }, '*');
        return;
      }
      let inputSize: number;
      try { inputSize = JSON.stringify(data.input ?? {}).length; } catch { return; }
      if (data.kind !== 'paw.lab-app.invoke'
          || typeof data.actionId !== 'string' || !version.spec.actions.some((action) => action.id === data.actionId)
          || inputSize > 32_000 || inFlight.current.has(data.requestId)) return;
      inFlight.current.add(data.requestId);
      void send({ action: 'invoke', appId: app.appId, expectedRevision: app.revision,
        clientRequestId: `app-ui:${app.appId}:${data.requestId}`,
        input: { version: version.version, actionId: data.actionId, values: object(data.input) as Record<string, JsonValue>, ...(data.model ? { model: object(data.model) as Record<string, JsonValue> } : {}) } }, data.requestId);
    };
    window.addEventListener('message', receive); return () => window.removeEventListener('message', receive);
  }, [app.appId, app.revision, version.spec.actions, version.version, calls, send, onActivity, transport, version.spec.model, selectedCallId]);
  useEffect(() => {
    if (selectedCallId) return;
    for (const call of calls) {
      const requestId = requestCalls.current.get(call.callId);
      if (!requestId || call.appId !== app.appId || call.version !== version.version || delivered.current.has(call.callId)) continue;
      if (cancelWanted.current.has(requestId) && !cancelSent.current.has(call.callId)
          && ['queued', 'running', 'interrupted'].includes(call.state)) {
        cancelSent.current.add(call.callId);
        void send({ action: 'cancel', appId: app.appId, expectedRevision: app.revision,
          clientRequestId: `app-frame-cancel:${requestId}`, input: { callId: call.callId } });
      }
      const progress = JSON.stringify(call.progress ?? {});
      if (progress !== progressSent.current.get(call.callId)) {
        frame.current?.contentWindow?.postMessage({ kind: 'paw.lab-app.progress', requestId, progress: call.progress ?? {} }, '*');
        progressSent.current.set(call.callId, progress);
      }
      if (!['completed', 'failed', 'cancelled'].includes(call.state)) continue;
      frame.current?.contentWindow?.postMessage({ kind: 'paw.lab-app.result', requestId, ok: call.state === 'completed', state: call.state, result: call.result, message: call.error }, '*');
      delivered.current.add(call.callId);
    }
  }, [calls, app.appId, app.revision, version.version, send, selectedCallId]);
  const displayCalls = selectedCallId ? calls.filter((call) => call.callId === selectedCallId) : calls;
  const receipts = displayCalls.length ? <details className="lab-app-preview__receipts" open={Boolean(selectedCallId) || undefined}><summary>实际调用记录 · {displayCalls.length}</summary>{displayCalls.map((call) => <article key={call.callId} aria-label={call.callId === selectedCallId ? '选中的原应用调用' : undefined} data-selected={call.callId === selectedCallId || undefined}>
      <strong>{version.spec.actions.find((action) => action.id === call.actionId)?.title ?? call.actionId} · {call.state === 'completed' ? '已完成' : call.state === 'failed' ? '失败' : call.state === 'cancelled' ? '已停止' : call.state === 'interrupted' ? '中断' : '处理中'}</strong>
      {call.callId === selectedCallId ? <p>原调用 {call.callId} · 查看不会重新执行。</p> : null}
      <small>应用 v{call.version} · {call.sessionId || '等待 Runtime 接纳'}</small>
      {['queued', 'running'].includes(call.state) ? <small>{appProgressLabel(call.progress?.stage)}</small> : null}
      {call.result.text ? <pre>{call.result.text}</pre> : call.error ? <p>{call.error}</p> : null}
      {call.callId === selectedCallId ? <details><summary>查看原输入、来源与完整公开回执</summary><pre>{JSON.stringify(call, null, 2)}</pre></details> : null}
      <SourceReadCount call={call} />
      {call.result.usage && Object.keys(call.result.usage).length ? <small>实际用量：{JSON.stringify(call.result.usage)}</small> : null}
    </article>)}</details> : null;
  const active = calls.filter((call) => call.appId === app.appId && ['queued', 'running', 'interrupted'].includes(call.state)
    && (!integratedProgress || !requestCalls.current.has(call.callId) || call.state === 'interrupted' || pending.length > 0 || error));
  if (selectedCallId) return <div className="lab-app-preview">{receipts}</div>;
  return <div className={`lab-app-preview${split ? ' lab-app-preview--split' : ''}`} data-workspace-view={workspaceVisible ? 'workspace' : 'chat'}>
    {external ? <nav className="lab-app-preview__workspaces" aria-label="应用工作区">
      <button type="button" aria-pressed={!workspaceVisible} onClick={() => setWorkspaceVisible(false)}>资料问答</button>
      <button type="button" aria-pressed={workspaceVisible} onClick={() => { if (!split) setWorkspaceOpened(true); setWorkspaceVisible(true); }}>{external.title}</button>
      {workspaceUrl && workspaceReady ? <a href={workspaceUrl} target="_blank" rel="noopener noreferrer">在浏览器打开 ↗</a> : null}
    </nav> : null}
    {error || pending.length ? <div className="lab-project-error" role="alert"><p>{error || '有尚未确认的应用操作，已保留原请求。'}</p>{pending.map((command) => <Button key={command.clientRequestId} onClick={() => void send(command, command.action === 'invoke' ? command.clientRequestId.split(':').at(-1) : undefined)}>核对原操作</Button>)}</div> : null}
    {active.length ? <div className="lab-app-preview__activity" role="status">{active.map((call) => <span key={call.callId}>
      <small>应用 v{call.version}</small>
      {call.state === 'interrupted' ? '调用中断，保留原请求' : call.cancelRequested ? '正在停止…' : appProgressLabel(call.progress?.stage)}
      <SourceReadCount call={call} />
      {call.state === 'interrupted' && !call.cancelRequested ? <Button size="small" onClick={() => void send({ action: 'resume', appId: app.appId, expectedRevision: app.revision, clientRequestId: `app-resume:${crypto.randomUUID()}`, input: { callId: call.callId } })}>恢复原调用</Button> : null}
      <Button size="small" disabled={call.cancelRequested && call.state !== 'interrupted'} onClick={() => void send({ action: 'cancel', appId: app.appId, expectedRevision: app.revision, clientRequestId: `app-cancel:${crypto.randomUUID()}`, input: { callId: call.callId } })}>{call.cancelRequested && call.state === 'interrupted' ? '重试停止' : '停止'}</Button>
    </span>)}</div> : null}
    <div className="lab-app-preview__panes">
    <iframe ref={frame} data-pane="chat" title={`${version.spec.title} · 应用预览`} hidden={!split && workspaceVisible} sandbox="allow-scripts allow-downloads allow-popups allow-popups-to-escape-sandbox" referrerPolicy="no-referrer" src={previewUrl} />
    {external && (workspaceOpened || split) ? workspaceUrl
      ? workspaceReady
        ? <iframe data-pane="workspace" title={external.title} hidden={!split && !workspaceVisible} sandbox="allow-scripts allow-same-origin allow-forms allow-downloads allow-popups allow-popups-to-escape-sandbox" referrerPolicy="no-referrer" src={workspaceUrl} />
        : <div data-pane="workspace" hidden={!split && !workspaceVisible} className="lab-app-preview__workspace-state" role={workspaceConnection === 'unavailable' ? 'alert' : 'status'}>
          <strong>{workspaceConnection === 'unavailable' ? '工作台服务未连接' : '正在检查工作台连接…'}</strong>
          <p>{workspaceConnection === 'unavailable' ? '请先启动本机工作台服务，再重新连接。资料问答仍可使用。' : '正在确认本机工作台是否可用。'}</p>
          {workspaceConnection === 'unavailable' ? <Button size="small" onClick={() => setWorkspaceRetry((value) => value + 1)}>重新连接</Button> : null}
        </div>
      : <p hidden={!split && !workspaceVisible} className="lab-project-error" role="alert">工作台地址不可用。请检查应用声明的 HTTPS 或本机服务地址；不能嵌入当前控制服务。</p> : null}
    </div>
    {receipts}
  </div>;
}

function appProgressLabel(stage?: string): string {
  const labels: Record<string, string> = { researching: '读取研究原文', context_ready: '已读取应用资料', retrieving: '正在检索资料…', sources_ready: '来源已找到', model_starting: '正在连接模型…', model_wait: '正在等待模型响应…', thinking: '模型正在思考…', answering: '正在输出回答…' };
  return labels[stage ?? ''] ?? '应用正在处理…';
}

function SourceReadCount({ call }: { call: LabAppCall }) {
  const knowledge = object(call.progress?.knowledge ?? object(call.result).knowledge);
  const count = object(knowledge.workflow).executedSourceReadCallCount;
  return typeof count === 'number' && Number.isSafeInteger(count) && count >= 0 ? <small>已完成 {count} 次原文查找／打开</small> : null;
}
