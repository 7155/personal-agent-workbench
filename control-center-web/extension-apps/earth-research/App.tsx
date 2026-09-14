import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { PawSessionWorkspace, sessionWorkspaceProjectionSlice } from '@/paw-os/apps/PawSessionWorkspace';
import { useAgentLiveStore } from '@/features/agent/state/live-store';
import { PawWindowChromeProvider } from '@/paw-os/shell/PawWindowChrome';
import { sessionItems, type SessionSummary } from '@/features/agent/types';
import { agentCommandReceiptFailure, isAmbiguousAgentPromptFailure } from '@/features/agent/public-error';
import { CodePreview } from '@/features/agent/file-preview/CodePreview';
import { readCompleteFile, useWorkspaceTextEditor, type EditableWorkspacePreview } from '@/features/files/WorkspaceTextEditor';
import type { PawExtensionAppProps } from '@/paw-os/extensions/types';
import { createPendingAppMessage, promptRequestFor, successorForDurablyFailedCommand, type PendingAppMessage } from './message-identity';
import { GoogleEarthMap as EarthMap } from './GoogleEarthMap';
import { EarthResults } from './EarthResults';
import { selectionDetail, selectionKey, updateSelection, type MapSelection, type SelectionMode } from './map-selection';
import { messageWithWorkspaceContext } from '@/paw-os/apps/workspace-draft';
import { geoJsonOutputs, parseRun, runStatus, type EarthRun } from './workspace';
import { parseViewCommand, type EarthViewCommand } from './pi-package/view-contract';
import './app.css';

const SOURCES = [
  ['Earth Engine API', 'https://developers.google.com/earth-engine/apidocs'],
  ['Copernicus DSM', 'https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_DEM_GLO30_2024_1'],
  ['WorldCover', 'https://developers.google.com/earth-engine/datasets/catalog/ESA_WorldCover_v200'],
  ['累计代价计算', 'https://developers.google.com/earth-engine/guides/image_cumulative_cost'],
];
const SURFACE = 'analysis';
type View = 'split' | 'map' | 'code';

export default function EarthResearchApp({ manifest }: PawExtensionAppProps) {
  const transport = useControlTransport();
  const scopeHintId = useId();
  const [session, setSession] = useState<SessionSummary>();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [readError, setReadError] = useState('');
  const [root, setRoot] = useState('');
  const [project, setProject] = useState('');
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
  const [pending, setPending] = useState<PendingAppMessage>();
  const [run, setRun] = useState<EarthRun | null>(null);
  const [lastCompleted, setLastCompleted] = useState<EarthRun | null>(null);
  const [file, setFile] = useState<EditableWorkspacePreview | null>(null);
  const [view, setView] = useState<View>('map');
  const [drawer, setDrawer] = useState<'console' | 'sources' | null>(null);
  const [selection, setSelection] = useState<MapSelection | null>(null);
  const [viewCommand, setViewCommand] = useState<EarthViewCommand>();
  const viewSeen = useRef('');
  const [refresh, setRefresh] = useState(0);
  const [historyRevision, setHistoryRevision] = useState(0);
  const workspaceRoot = session?.workspaceRoots?.[0] ?? root;
  const busy = useAgentLiveStore(state => Boolean(sessionWorkspaceProjectionSlice(state, session?.id ?? '').activeTurnId));

  useEffect(() => {
    let alive = true; setLoading(true);
    transport.request({ pathId: 'agent.sessions.list', query: { limit: 100, includeArchived: false, surfaceKind: 'extension_app', ownerAppId: manifest.id } })
      .then(value => {
        if (!alive) return;
        const restored = sessionItems(value, { includeAppOwned: true }).filter(x => x.surfaceKind === 'extension_app' && x.ownerAppId === manifest.id && x.surfaceKey === SURFACE)
          .sort((a, b) => b.updatedAtMs - a.updatedAtMs);
        setSessions(restored); setSession(restored[0]); setError('');
      }).catch(reason => { if (alive) setError(message(reason)); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [transport, manifest.id, historyRevision]);

  useEffect(() => {
    if (!session || !workspaceRoot) return;
    let alive = true; let timer: ReturnType<typeof setTimeout>;
    const sessionId = session.id;
    async function read() {
      const path = `${workspaceRoot}/.earth/workspace.json`;
      try {
        const raw = await readCompleteFile(transport, { sessionId, path, name: 'workspace.json' });
        if (!alive) return;
        if (raw.path !== path || raw.truncated || typeof raw.content !== 'string') throw new Error('运行记录未完整读取。');
        const next = parseRun(JSON.parse(raw.content));
        // Only read code from the Session's authorized workspace, not a result-supplied external path.
        if (!next.scriptPath.startsWith(workspaceRoot + '/') || next.scriptPath.split('/').includes('..')) throw new Error('脚本不属于当前工作区。');
        setRun(current => current?.runId === next.runId && current.updatedAt === next.updatedAt ? current : next);
        if (next.status === 'completed') setLastCompleted(next);
        else {
          try {
            const previous = await readCompleteFile(transport, { sessionId, path: `${workspaceRoot}/.earth/last-completed.json`, name: 'last-completed.json' });
            const completed = parseRun(JSON.parse(previous.content));
            if (alive && completed.status === 'completed') setLastCompleted(current => current?.runId === completed.runId ? current : completed);
          } catch { /* Optional previous run; keep the last result already visible. */ }
        }
        setProject(next.project); setReadError('');
        const source = await readCompleteFile(transport, { sessionId, path: next.scriptPath, name: next.scriptPath.split('/').pop() || 'analysis.js' });
        if (alive && source.path === next.scriptPath && typeof source.content === 'string') setFile(source);
      } catch (reason) { if (alive) setReadError(message(reason)); }
      finally { if (alive) timer = setTimeout(() => void read(), 2500); }
    }
    void read();
    return () => { alive = false; clearTimeout(timer); };
  }, [session?.id, workspaceRoot, transport, refresh]);

  const editor = useWorkspaceTextEditor(session && file ? { sessionId: session.id, path: file.path, name: file.path.split('/').pop() || 'analysis.js' } : null, file, saved => setFile(saved));
  const newSession = useCallback((created: SessionSummary) => { setViewCommand(undefined); viewSeen.current = ""; setSession(created); setSessions(current => [created, ...current.filter(x => x.id !== created.id)]); setRun(null); setLastCompleted(null); setFile(null); }, []);
  function startAnother() { setViewCommand(undefined); viewSeen.current = ""; setRoot(workspaceRoot); setSession(undefined); setRun(null); setLastCompleted(null); setFile(null); setError(''); setReadError(''); setSelection(null); }
  const mapRun = run?.status === 'completed' ? run : lastCompleted;
  useEffect(() => {
    if (!session || !workspaceRoot) return;
    let alive = true; let timer: ReturnType<typeof setTimeout>;
    const sessionId = session.id;
    async function receive() {
      try {
        const raw = await readCompleteFile(transport, {sessionId,path:`${workspaceRoot}/.earth/view.json`,name:'view.json'});
        const command = parseViewCommand(JSON.parse(raw.content));
        if (!alive || command.requestId === viewSeen.current) return;
        try {
          const saved = await readCompleteFile(transport,{sessionId,path:`${workspaceRoot}/.earth/view-receipt.json`,name:'view-receipt.json'});
          const receipt = JSON.parse(saved.content);
          if (!alive) return;
          if (receipt.requestId === command.requestId && ['applied','rejected'].includes(receipt.status)) {viewSeen.current=command.requestId;return;}
        } catch { /* A new command without a receipt is still actionable. */ }
        let reason = '';
        if (command.runId && command.runId !== mapRun?.runId) reason = '请求属于另一份运行结果，未改变当前地图。';
        if (command.action === 'layer' && !mapRun?.layers.some(layer => layer.id === command.layerId && layer.tileUrl)) reason = '当前地图不存在该图层。';
        if (command.action === 'feature' && !geoJsonOutputs(mapRun).some(output => {
          const data = output.geojson as GeoJSON.FeatureCollection | GeoJSON.Feature;
          return (data.type === 'FeatureCollection' ? data.features : data.type === 'Feature' ? [data] : []).some(item => String(item.id ?? item.properties?.id ?? '') === command.featureId);
        })) reason = '当前结果不存在该要素。';
        viewSeen.current = command.requestId;
        if (!reason) setViewCommand(command);
        else await acknowledge(command, 'rejected', reason);
      } catch { /* No view request yet, or a transient read; leave the user's view intact. */ }
      finally { if(alive) timer=setTimeout(()=>void receive(),1500); }
    }
    async function acknowledge(command: EarthViewCommand, status: string, reason?: string) {
      const path = `${workspaceRoot}/.earth/view-receipt.json`;
      const previous = await readCompleteFile(transport,{sessionId,path,name:'view-receipt.json'});
      if(alive) await transport.request({pathId:'agent.session.workspace.save',params:{sessionId},body:{path,resourceRevision:previous.resourceRevision,content:JSON.stringify({requestId:command.requestId,status,reason,displayedRunId:mapRun?.runId??null})}});
    }
    void receive(); return()=>{alive=false;clearTimeout(timer);};
  }, [session?.id,workspaceRoot,mapRun?.runId,transport]);
  useEffect(() => {
    if (!viewCommand || !session) return;
    if(viewCommand.action==='panel') {
      if(viewCommand.panel==='results') setDrawer('console');
      else if(viewCommand.panel==='sources') setDrawer('sources');
      else setView(viewCommand.panel as View);
    }
    let alive=true;
    const path=`${workspaceRoot}/.earth/view-receipt.json`, sessionId=session.id;
    void readCompleteFile(transport,{sessionId,path,name:'view-receipt.json'}).then(previous=> {
      if(alive) return transport.request({pathId:'agent.session.workspace.save',params:{sessionId},body:{path,resourceRevision:previous.resourceRevision,content:JSON.stringify({requestId:viewCommand.requestId,status:'applied',action:viewCommand.action,displayedRunId:mapRun?.runId??null})}});
    }).catch(()=>{viewSeen.current='';});
    return()=>{alive=false;};
  },[viewCommand,session?.id,workspaceRoot,transport]);

  async function send(logical: PendingAppMessage) {
    setPending(logical);
    try {
      const response = await transport.request(promptRequestFor(logical));
      const failure = agentCommandReceiptFailure(response);
      if (failure) throw failure;
      setPending(undefined); setError(''); setDraft('');
    } catch (reason) {
      if (!isAmbiguousAgentPromptFailure(reason) && agentCommandReceiptFailure(reason)?.state === 'failed') setPending(successorForDurablyFailedCommand(logical));
      throw reason;
    }
  }
  async function start(event: FormEvent) {
    event.preventDefault(); if (sendingRef.current || !draft.trim() || !root.startsWith('/') || !project.trim()) return;
    sendingRef.current = true; setSending(true); setError('');
    try {
      const value = await transport.request<{ session: SessionSummary }>({ pathId: 'agent.sessions.create', body: {
        title: manifest.label, mode: 'coordinator', toolProfileVersion: 'control-center-v1', executionMode: 'workspace_managed', workspaceRoots: [root.trim()],
        workspaceScopeConfirmation: 'APPROVE_WORKSPACE_SCOPE', projectContextEnabled: true, piSkillsEnabled: true, codexSkillsEnabled: false,
        surfaceKind: 'extension_app', ownerAppId: manifest.id, surfaceKey: SURFACE,
      } });
      if (!value.session?.id) throw new Error('Runtime 未返回会话，原输入已保留。');
      const created = { ...value.session, workspaceRoots: [root.trim()] };
      setSession(created);
      setSessions(current => [created, ...current.filter(x => x.id !== created.id)]);
      await send(createPendingAppMessage({ sessionId: created.id, ownerAppId: manifest.id, surfaceKey: SURFACE, message: firstTurn(manifest.skillRef, project.trim(), messageWithWorkspaceContext(draft.trim(),mapContext)) }));
    } catch (reason) { setError(message(reason)); }
    finally { sendingRef.current = false; setSending(false); }
  }
  async function retry() {
    if (!pending || sendingRef.current) return; sendingRef.current = true; setSending(true);
    try { await send(pending); } catch (reason) { setError(message(reason)); } finally { sendingRef.current = false; setSending(false); }
  }
  function mapActivity() { if (!editor.editing) setView('map'); setDrawer(null); }
  function selectMapFeature(feature: GeoJSON.Feature | null,mode:SelectionMode='replace') {
    mapActivity();
    setSelection(current=>updateSelection(current,feature,mode,mapRun?.runId ?? null));

  }
  const mapContext = selection ? {label:selection.features.length>1 ? `已选择 ${selection.features.length} 个对象` : String(selection.features[0].properties?.name ?? (selection.features[0].geometry.type==='Point' ? '地图选点' : '选中几何')),detail:selectionDetail(selection),items:selection.features.length>1 ? selection.features.map(feature=>({id:selectionKey(feature),label:String(feature.properties?.name ?? feature.properties?.id ?? feature.id ?? feature.geometry.type),onRemove:()=>selectMapFeature(feature,'remove')})) : undefined,text:JSON.stringify({runId:selection.runId,type:'FeatureCollection',features:selection.features},null,2),onClear:()=>selectMapFeature(null)} : undefined;
  async function runSaved() {
    if (!session || !file || busy || sendingRef.current || pending) return;
    sendingRef.current = true; setSending(true);
    try {
      const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(file.content));
      const expectedSourceHash = [...new Uint8Array(bytes)].map(x => x.toString(16).padStart(2, '0')).join('');
      await send(createPendingAppMessage({ sessionId: session.id, ownerAppId: manifest.id, surfaceKey: SURFACE,
        message: `运行我已保存的代码，不修改业务参数。请调用 earth_run_script(${JSON.stringify({ script: file.path.slice(workspaceRoot.length + 1), expectedSourceHash })})，并根据真实回执解释结果。代码版本不一致时保留当前文件并报告。` }));
    } catch (reason) { setError(message(reason)); } finally { sendingRef.current = false; setSending(false); }
  }

  return <main className="earth-app">
    <header className="earth-app__header"><div><strong>Earth Agent</strong><span>地理分析与选址选线</span></div>{sessions.length ? <select aria-label="分析会话" value={session?.id || ''} onChange={event => { const next = sessions.find(x => x.id === event.target.value); if (next) { setSession(next); setRun(null); setLastCompleted(null); setFile(null); setSelection(null); setViewCommand(undefined); viewSeen.current = ""; } }}><option value="" disabled>新分析</option>{sessions.map(item => <option key={item.id} value={item.id}>{item.title} · {new Date(item.updatedAtMs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</option>)}</select> : null}<span role="status">{run ? runStatus[run.status] : '准备开始分析'}</span><button onClick={startAnother} disabled={sending || Boolean(pending)}>新分析</button><button onClick={() => setRefresh(x => x + 1)} disabled={!session}>刷新结果</button></header>
    <div className="earth-app__body">
      <section className="earth-agent" data-earth-surface="agent" aria-label="Agent 工作栏">

        {error ? <div className="earth-error" role="alert"><p>{error}</p>{!session ? <button onClick={() => setHistoryRevision(x => x + 1)}>重新读取</button> : null}</div> : null}
        {pending ? <div className="earth-error" role="status"><p>这条请求尚未确认接纳，已保留原消息标识。</p><button disabled={sending} onClick={() => void retry()}>核对并重试原请求</button></div> : null}
        {loading ? <p className="earth-empty" role="status">正在恢复 App 对话…</p> : session ? <PawWindowChromeProvider><PawSessionWorkspace
          key={session.id} active record={session} recordId={session.id} composerContext={mapContext}
          composerPlaceholder="描述分析目标，或继续调整当前方案…"
          onNewWork={startAnother}
          onSessionCreated={newSession} onSessionUpdated={setSession} onSessionActivity={() => setRefresh(x => x + 1)}
        /></PawWindowChromeProvider> : <form className="earth-start" onSubmit={event => void start(event)}>
          <h1>把地理数据<br />变成可比较的方案</h1><p>Agent 查阅资料、编写 Earth Engine 代码并执行。代码、工具和结果都留在这个工作区。</p>
          <label>项目文件夹<input aria-describedby={scopeHintId} value={root} onChange={event => setRoot(event.target.value)} placeholder="选择已有分析项目的绝对路径" required /></label><small id={scopeHintId}>开始后，Agent 可在这个文件夹内读取、编辑与运行分析。</small>
          <label>Google Cloud 项目<input value={project} onChange={event => setProject(event.target.value)} placeholder="已开通 Earth Engine 的项目 ID" required /></label>
          {mapContext ? <p className="earth-start-context">{mapContext.label} · {mapContext.detail}<button type="button" onClick={mapContext.onClear}>移除</button></p> : null}
          <label>分析任务<textarea value={draft} onChange={event => setDraft(event.target.value)} placeholder="读取候选地块，排除禁区，并比较接入线路…" rows={4} required /></label>
          <button disabled={sending || Boolean(error)} type="submit">{sending ? '正在建立会话…' : '开始分析'}</button>
        </form>}
      </section>
      <section className="earth-workspace" aria-label="地图与代码工作区">
        <nav className="earth-toolbar" aria-label="工作区视图">{([['split', '地图＋代码'], ['map', '地图'], ['code', '代码']] as const).map(([key, label]) => <button key={key} aria-pressed={view === key} onClick={() => setView(key)}>{label}</button>)}<div className="earth-toolbar__spacer" /><button aria-pressed={drawer === 'sources'} onClick={() => setDrawer(drawer === 'sources' ? null : 'sources')}>官方资料</button><button aria-pressed={drawer === 'console'} onClick={() => setDrawer(drawer === 'console' ? null : 'console')}>控制台</button></nav>
        <div className="earth-panels" data-view={view}>
          <div className="earth-map-panel" hidden={view === 'code'}><EarthMap key={workspaceRoot} workspaceKey={workspaceRoot} onActivity={mapActivity} run={mapRun} command={viewCommand} selection={selection?.features ?? []} onSelect={selectMapFeature} />{mapRun && mapRun.runId !== run?.runId ? <span className="earth-map-retained" role="status">显示上次完成的结果 · {mapRun.runId.slice(0,8)}</span> : null}</div>
          <section className="earth-code" hidden={view === 'map'} aria-label="Earth Engine JavaScript">
            <header><strong>Earth Engine · JavaScript</strong><button disabled={!session || !file || busy || sending || Boolean(pending) || (editor.copyContent !== null && editor.copyContent !== file?.content)} onClick={() => void runSaved()}>运行已保存代码</button></header>
            {run ? <small className="earth-version">运行 {run.runId.slice(0, 8)} · {file && file.content !== run.code ? '文件已修改，结果属于上次代码' : '代码与该次运行对应'}</small> : null}
            {editor.panel}
            {!editor.editing && (file || run) ? <CodePreview content={editor.copyContent ?? file?.content ?? run?.code ?? ''} fileName={file?.path.split('/').pop() || 'analysis.js'} language="javascript" /> : !editor.editing ? <p className="earth-empty">Agent 编写的实际脚本会显示在这里。</p> : null}
          </section>
        </div>
        {readError ? <details className="earth-read-error"><summary>尚未读到最新结果 · 已保留当前内容</summary><p>{readError}</p><button onClick={() => setRefresh(x => x + 1)}>重新读取</button></details> : null}
        {drawer ? <div className="earth-drawer">{drawer === 'console' ? <EarthResults run={run} /> : <section className="earth-sources"><h2>本次运行的资料</h2>{run?.sourceRefs?.length ? run.sourceRefs.filter(item => item.url.startsWith('https://developers.google.com/earth-engine/')).map(item => <p key={item.url}><a href={item.url} target="_blank" rel="noreferrer">{item.title} ↗</a><small>读取于 {item.retrievedAt}</small></p>) : <p>尚未记录官方资料读取回执。实际工具过程保留在左侧。</p>}<h2>Google 官方参考入口</h2>{SOURCES.map(([label, url]) => <a key={url} href={url} target="_blank" rel="noreferrer">{label} ↗</a>)}</section>}</div> : null}
      </section>
    </div>
  </main>;
}
export function firstTurn(skill: string, project: string, task: string) {
  return `使用已安装的 App Skill：${skill}。这是 Earth Agent 的地理分析会话。Google Cloud 项目：${project}。\n先调用 earth_workspace({project: "${project}"}) 准备执行器；写完实际 JavaScript 文件后调用 earth_run_script({script: "analysis.js"})。不要在磁盘中搜索或猜测执行器路径。使用项目的 .earth/runtime.json 配置。查阅 Google 官方资料、实际编码与运行，真实结果由执行器写入 .earth/workspace.json。禁止伪造运行记录；无数据和无可行解如实报告。\n用户任务：${task}`;
}
function message(error: unknown) { return error instanceof Error ? error.message : String(error); }
