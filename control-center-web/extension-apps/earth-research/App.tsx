import type {MapOptions} from './GISDeliveryPanel';
import { ChevronDown, Rows2, Map as MapIcon, Code2, Maximize2, Minimize2, Plus, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { PawSessionWorkspace, sessionWorkspaceProjectionSlice } from '@/paw-os/apps/PawSessionWorkspace';
import { useAgentLiveStore } from '@/features/agent/state/live-store';
import { PawWindowChromeProvider } from '@/paw-os/shell/PawWindowChrome';
import { pawBrowserHost } from '@/paw-os/apps/paw-browser-host';
import { sessionItems, type SessionSummary } from '@/features/agent/types';
import { agentCommandReceiptFailure, isAmbiguousAgentPromptFailure } from '@/features/agent/public-error';
import { CodePreview } from '@/features/agent/file-preview/CodePreview';
import { readCompleteFile, useWorkspaceTextEditor, type EditableWorkspacePreview } from '@/features/files/WorkspaceTextEditor';
import type { PawExtensionAppProps } from '@/paw-os/extensions/types';
import { createPendingAppMessage, promptRequestFor, successorForDurablyFailedCommand, type PendingAppMessage } from './message-identity';
import { GoogleEarthMap as EarthMap } from './GoogleEarthMap';
import { RemoteSensingPanel, type RemoteSensingWorkflowPlan, type RemoteSensingPreparation } from './RemoteSensingPanel';
import { RasterQueryPanel } from './RasterQueryPanel';
import { GISOperationPanel } from './GISOperationPanel';
import { GISDeliveryPanel } from './GISDeliveryPanel';
import { MapTaskLauncher, type MapTaskAction } from './MapTaskLauncher';
import { ProjectWorkspacePicker, normalizeWorkspaceRoot } from './ProjectWorkspacePicker';
import { CloudTasksPanel, type CloudTaskSnapshot } from './CloudTasksPanel';
import { EarthResults } from './EarthResults';
import { selectionDetail, selectionKey, updateSelection, type MapSelection, type SelectionMode } from './map-selection';
import { messageWithWorkspaceContext } from '@/paw-os/apps/workspace-draft';
import { geoJsonOutputs, parseRun, runStatus, type EarthRun } from './workspace';
import { parseMapState, parseViewCommand, type EarthMapState, type EarthViewCommand } from './pi-package/view-contract';
import { bindLayerFeatures, selectedLayerFeatures, parseGeoJsonFeatures, parseProjectLayerCatalog, parseSpatialCatalog, type ProjectLayer, type SpatialSourceDraft, type SpatialSourceSummary, type WorkspaceFileSummary } from './layer-catalog';
import type { LocalGISRunSummary } from './EarthDataDock';
import './app.css';
import { createGISCommandQueue } from './gis-command-queue';
import gisCatalog from './pi-package/gis-catalog.json';
import gisKnowledge from './pi-package/gis-knowledge.json';

const SOURCES = [
  ['Earth Engine API', 'https://developers.google.com/earth-engine/apidocs'],
  ['Copernicus DSM', 'https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_DEM_GLO30_2024_1'],
  ['WorldCover', 'https://developers.google.com/earth-engine/datasets/catalog/ESA_WorldCover_v200'],
  ['累计代价计算', 'https://developers.google.com/earth-engine/guides/image_cumulative_cost'],
];
const SURFACE = 'analysis';
type WorkspacePreviewKind = 'script' | 'html' | 'markdown' | 'json' | 'text' | 'binary';
type View = 'split' | 'map' | 'code';
type AnalysisMode = 'site' | 'route' | 'change' | 'classification' | 'batch' | 'custom';
const ANALYSIS_MODES: Array<[AnalysisMode, string, string]> = [
  ['site', '候选地块', '筛选适合建设的候选区域'],
  ['route', '接入路线', '比较道路、电网或管线接入路线'],
  ['change', '时序变化', '分析遥感影像与土地覆盖变化'],
  ['classification', '遥感分类', '用训练样本提取地物和专题图层'],
  ['batch', '批量处理', '对多幅影像或多个地块重复执行工作流'],
  ['custom', '自定义分析', '提出你的地理问题'],
];

export const GIS_ACCEPTANCE_TASK = '找适合建变电站的地块，避开河流 200 米，并导出 SHP。';

export default function EarthResearchApp({ manifest }: PawExtensionAppProps) {
  const transport = useControlTransport();
  const directoryHost = pawBrowserHost();
  const [session, setSession] = useState<SessionSummary>();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [readError, setReadError] = useState('');
  const [root, setRoot] = useState('');
  const [projectPickerOpen, setProjectPickerOpen] = useState(false);
  const [project, setProject] = useState('');
  const [draft, setDraft] = useState('');
  const [analysisMode, setAnalysisMode] = useState<AnalysisMode>('site');
  const [planOpen, setPlanOpen] = useState(false);
  const [agentVisible,setAgentVisible]=useState(false);
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
  const [pending, setPending] = useState<PendingAppMessage>();
  const [run, setRun] = useState<EarthRun | null>(null);
  const [lastCompleted, setLastCompleted] = useState<EarthRun | null>(null);
  // The script used for a run and the file currently opened in the workbench
  // are different things.  A completed report or HTML artifact must not be
  // replaced when the run receipt is polled again.
  const [scriptFile, setScriptFile] = useState<EditableWorkspacePreview | null>(null);
  const [activeFile, setActiveFile] = useState<EditableWorkspacePreview | null>(null);
  const [view, setView] = useState<View>('map');
  const [drawer, setDrawer] = useState<'tasks' | 'delivery' | 'cloud' | 'console' | 'sources' | 'gis' | 'knowledge' | 'remote' | 'raster' | null>(null);
  const [knowledgeQuery, setKnowledgeQuery] = useState('');
  const [remoteKind,setRemoteKind]=useState<RemoteSensingWorkflowPlan['kind']>('ndvi');
  const [selection, setSelection] = useState<MapSelection | null>(null);
  const [projectLayers, setProjectLayers] = useState<ProjectLayer[]>([]);
  const [spatialSources, setSpatialSources] = useState<SpatialSourceSummary[]>([]);
  const [workspaceFiles, setWorkspaceFiles] = useState<WorkspaceFileSummary[]>([]);
  const [workspaceFilesIncomplete, setWorkspaceFilesIncomplete] = useState(false);
  const [workspaceFilesNotice, setWorkspaceFilesNotice] = useState('');
  const [layerMessage, setLayerMessage] = useState('');
  const [localRuns,setLocalRuns]=useState<LocalGISRunSummary[]>([]);
  const [rasterPath,setRasterPath]=useState('');
  const [localResult,setLocalResult]=useState<Record<string,any>|null>(null);
  const layerSaveQueue=useRef<Promise<unknown>>(Promise.resolve());
  const commandQueue=useRef(createGISCommandQueue());
  const [viewCommand, setViewCommand] = useState<EarthViewCommand>();
  const viewSeen = useRef('');
  const mapStateTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const mapStatePending = useRef<EarthMapState | undefined>(undefined);
  const [refresh, setRefresh] = useState(0);
  const [historyRevision, setHistoryRevision] = useState(0);
  const workspaceRoot = session?.workspaceRoots?.[0] ?? '';
  const workspaceBinding = useRef('');
  workspaceBinding.current = `${session?.id ?? ''}\0${workspaceRoot}`;
  const activeSession = useRef(session);
  activeSession.current = session;
  const busy = useAgentLiveStore(state => Boolean(sessionWorkspaceProjectionSlice(state, session?.id ?? '').activeTurnId));

  const newSession = useCallback((created: SessionSummary) => {
    const nextRoot = created.workspaceRoots?.[0] ?? '';
    workspaceBinding.current = `${created.id}\0${nextRoot}`;
    activeSession.current = created;
    clearTimeout(mapStateTimer.current); mapStatePending.current = undefined;
    setViewCommand(undefined); viewSeen.current = '';
    setSession(created); setRoot(nextRoot);
    setSessions(current => [created, ...current.filter(item => item.id !== created.id)]);
    setRun(null); setLastCompleted(null); setScriptFile(null); setActiveFile(null);
    setSelection(null); setLocalResult(null); setLocalRuns([]); setProjectLayers([]); setSpatialSources([]); setWorkspaceFiles([]);
    setWorkspaceFilesIncomplete(false); setWorkspaceFilesNotice('');
    setRasterPath(''); setLayerMessage(''); setProject(''); setReadError(''); setError(''); setView('map'); setDrawer(null);
    setProjectPickerOpen(false);
  }, []);

  useEffect(() => {
    let alive = true; setLoading(true);
    transport.request({ pathId: 'agent.sessions.list', query: { limit: 100, includeArchived: false, surfaceKind: 'extension_app', ownerAppId: manifest.id } })
      .then(value => {
        if (!alive) return;
        const restored = sessionItems(value, { includeAppOwned: true }).filter(x => x.surfaceKind === 'extension_app' && x.ownerAppId === manifest.id && x.surfaceKey === SURFACE)
          .sort((a, b) => b.updatedAtMs - a.updatedAtMs);
        setSessions(restored);
        const next = restored.find(item => item.id === activeSession.current?.id)
          ?? restored.find(item => item.workspaceRoots?.[0]?.startsWith('/'));
        if (next && next.id !== activeSession.current?.id) newSession(next);
        else if (!next) setProjectPickerOpen(true);
        setError('');
      }).catch(reason => { if (alive) setError(message(reason)); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [transport, manifest.id, historyRevision, newSession]);

  useEffect(() => {
    if (!session || !workspaceRoot) return;
    let alive = true; let timer: ReturnType<typeof setTimeout>;
    const sessionId = session.id;
    async function read() {
      const path = `${workspaceRoot}/.earth/workspace.json`;
      let hasRunRecord = false;
      try {
        const raw = await readCompleteFile(transport, { sessionId, path, name: 'workspace.json' });
        hasRunRecord = true;
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
        if (!alive) return;
        setProject(next.project); setReadError('');
        const source = await readCompleteFile(transport, { sessionId, path: next.scriptPath, name: next.scriptPath.split('/').pop() || 'analysis.js' });
        if (alive && source.path === next.scriptPath && typeof source.content === 'string') {
          setScriptFile(source);
          // Keep the script visible only while the user has not opened another
          // workspace artifact.  Reports, maps and HTML remain authoritative
          // until the user explicitly opens a different file.
          setActiveFile(current => current === null || current.path === next.scriptPath ? source : current);
        }
      } catch (reason) {
        // A local project need not have run Earth Engine yet. Missing optional
        // cloud state is normal; a missing script for an existing run is not.
        if (alive) setReadError(!hasRunRecord && /path does not exist in the authorized workspace/.test(message(reason)) ? '' : message(reason));
      }
      finally { if (alive) timer = setTimeout(() => void read(), 2500); }
    }
    void read();
    return () => { alive = false; clearTimeout(timer); };
  }, [session?.id, workspaceRoot, transport, refresh]);

  const editor = useWorkspaceTextEditor(session && activeFile ? { sessionId: session.id, path: activeFile.path, name: activeFile.path.split('/').pop() || 'analysis.js' } : null, activeFile, saved => {setActiveFile(saved);if(saved.path === scriptFile?.path)setScriptFile(saved);});
  function startAnother() {
    if (sendingRef.current || pending) return;
    setRoot(workspaceRoot); setProjectPickerOpen(true);
  }
  const mapRun = run?.status === 'completed' ? run : lastCompleted;

  const refreshProjectLayers = useCallback(async () => {
    if (!session || !workspaceRoot) { setProjectLayers([]); setSpatialSources([]); return; }
    const binding = `${session.id}\0${workspaceRoot}`;
    const catalogPath = `${workspaceRoot}/.earth/layers/catalog.json`;
    try {
      const catalogFile = await readCompleteFile(transport, { sessionId: session.id, path: catalogPath, name: 'catalog.json' });
      const records = parseProjectLayerCatalog(JSON.parse(catalogFile.content));
      const loaded = await Promise.all(records.filter(record => record.path).map(async record => {
        try {
          const file = await readCompleteFile(transport, { sessionId: session.id, path: `${workspaceRoot}/${record.path}`, name: record.path.split('/').pop() || record.name });
          return bindLayerFeatures({ ...record, features: parseGeoJsonFeatures(JSON.parse(file.content)) } as ProjectLayer);
        } catch { return { ...record, features: [] } as ProjectLayer; }
      }));
      if (workspaceBinding.current !== binding) return;
      setProjectLayers(loaded);
      setLayerMessage(loaded.length ? `已加载 ${loaded.length} 个项目图层` : '');
    } catch { if (workspaceBinding.current === binding) setProjectLayers([]); }
    if (workspaceBinding.current !== binding) return;
    try {
      const databaseFile = await readCompleteFile(transport, { sessionId: session.id, path: `${workspaceRoot}/.earth/gis/databases.json`, name: 'databases.json' });
      if (workspaceBinding.current === binding) setSpatialSources(parseSpatialCatalog(JSON.parse(databaseFile.content)));
    } catch { if (workspaceBinding.current === binding) setSpatialSources([]); }
  }, [session?.id, transport, workspaceRoot]);

  useEffect(() => { void refreshProjectLayers(); }, [refreshProjectLayers]);

  const refreshWorkspaceFiles = useCallback(async () => {
    if (!session || !workspaceRoot) { setWorkspaceFiles([]); setWorkspaceFilesIncomplete(false); setWorkspaceFilesNotice(''); return; }
    const binding = `${session.id}\0${workspaceRoot}`;
    const files: WorkspaceFileSummary[] = [];
    const queue: Array<{ path: string; depth: number }> = [{ path: workspaceRoot, depth: 0 }];
    const visited = new Set<string>();
    const paths = new Set<string>();
    let incomplete = false;
    const failures: string[] = [];
    while (queue.length && files.length < 240) {
      const current = queue.shift()!;
      if (visited.has(current.path)) continue;
      visited.add(current.path);
      try {
        const value = await transport.request({ pathId: 'agent.session.workspace.list', params: { sessionId: session.id }, query: { path: current.path, depth: 1, limit: 240 } });
        if (workspaceBinding.current !== binding) return;
        if (!value || typeof value !== 'object' || !Array.isArray((value as { items?: unknown }).items)) throw new Error('目录列表未返回完整结构');
        const { items, truncated } = value as { items: unknown[]; truncated?: boolean };
        if (truncated) incomplete = true;
        for (const item of items) {
          if (files.length >= 240) { incomplete = true; break; }
          if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
          const record = item as Record<string, unknown>;
          if (typeof record.path !== 'string' || typeof record.name !== 'string' || !record.path.startsWith(`${workspaceRoot}/`)) continue;
          if (paths.has(record.path)) continue;
          paths.add(record.path);
          const kind = record.kind === 'directory' || record.kind === 'symlink' ? record.kind : 'file';
          const entry = { path: record.path, name: record.name, kind, byteSize: typeof record.byteSize === 'number' ? record.byteSize : undefined } as WorkspaceFileSummary;
          files.push(entry);
          if (kind === 'directory') {
            if (current.depth < 4) queue.push({ path: record.path, depth: current.depth + 1 });
            else incomplete = true;
          }
        }
      } catch (reason) {
        if (workspaceBinding.current !== binding) return;
        incomplete = true;
        failures.push(`${current.path}：${message(reason)}`);
      }
    }
    if (queue.length) incomplete = true;
    if (workspaceBinding.current !== binding) return;
    setWorkspaceFiles(files); setWorkspaceFilesIncomplete(incomplete);
    setWorkspaceFilesNotice(failures.length ? `部分目录未能读取，已保留可读取的条目。${failures[0]}` : incomplete ? '目录尚未完整读取：本次最多展示 240 个条目并向下读取 4 层，部分目录也可能被服务端截断。' : '');
  }, [session?.id, transport, workspaceRoot]);

  useEffect(() => { void refreshWorkspaceFiles(); }, [refreshWorkspaceFiles, refresh]);

  const invokeGISCommand = useCallback((sessionId: string, command: string): Promise<Record<string, any>> => commandQueue.current(sessionId, async () => {
    const receipt = await transport.request<Record<string, unknown>>({ pathId: 'agent.session.command.invoke', params: { sessionId }, body: { command } });
    const envelope = receipt && typeof receipt.result === 'object' && receipt.result !== null && !Array.isArray(receipt.result)
      ? receipt.result as Record<string, any>
      : undefined;
    const expectedCommand = command.trim().slice(1).split(/\s+/, 1)[0];
    if (!envelope || envelope.schemaVersion !== 'rag-ime.pi-package-command-result.v1' || envelope.command !== expectedCommand) throw new Error('GIS 命令没有返回可核验的 Package 回执。');
    const result = typeof envelope.result === 'object' && envelope.result !== null && !Array.isArray(envelope.result)
      ? envelope.result as Record<string, any>
      : undefined;
    if (!result || (result.status === 'failed' || result.status === 'error')) throw new Error(String(result?.error || 'GIS 命令未返回成功回执。'));
    if (activeSession.current?.id !== sessionId) throw new Error('已切换 Agent；这次操作的回执保留在原项目，请回到原 Agent 查看。');
    return result;
  }), [transport]);

  const saveImportedLayer=useRef<(name:string,features:GeoJSON.Feature[],source?:Record<string,unknown>)=>Promise<void>>(async()=>{});
  const openWorkspaceFile = useCallback(async (entry: WorkspaceFileSummary) => {
    if (!session || entry.kind !== 'file') return;
    const binding = `${session.id}\0${workspaceRoot}`;
    const kind = workspaceFileKind(entry.path);
    if (kind === 'binary') {
      const relative=entry.path.replace(`${workspaceRoot}/`,'');
      if(/\.tiff?$/i.test(entry.path)){setRasterPath(relative);setDrawer('raster');return;}
      if(/\.(gpkg|sqlite|db)$/i.test(entry.path)) {
        try {await invokeGISCommand(session.id,`/earth-spatial-connect ${JSON.stringify({name:entry.name,kind:entry.path.endsWith('.gpkg')?'geopackage':'spatialite',path:relative})}`);await refreshProjectLayers();setLayerMessage('数据库已读取；请在数据库视角选择要加载的图层。');}catch(reason){setReadError(message(reason));}
        return;
      }
      if(/\.shp$/i.test(entry.path)) {
        try {
          const result=await invokeGISCommand(session.id,`/earth-gis-export ${JSON.stringify({input:relative,format:'geojson',name:entry.name,scope:'all'})}`);
          if (workspaceBinding.current !== binding) return;
          const output=result.outputs?.find((item:any)=>item.kind==='vector');
          if(!output?.geojson)throw new Error('图层过大，请按范围导入。');
          await saveImportedLayer.current(entry.name,parseGeoJsonFeatures(output.geojson),{kind:'file',path:relative,format:'shp',runId:result.runId});
        }catch(reason){if (workspaceBinding.current === binding) setReadError(message(reason));}return;
      }
      setReadError(`“${entry.name}”保存在 ${entry.path}，请使用对应的桌面查看器打开。`);return;
    }
    try {
      const snapshot = await readCompleteFile(transport, { sessionId: session.id, path: entry.path, name: entry.name });
      if (workspaceBinding.current !== binding) return;
      setActiveFile(snapshot);
      setView(current => current === 'code' ? 'code' : 'split');
      setLayerMessage(`已打开文件：${entry.path}`);
      setReadError('');
    } catch (reason) { if (workspaceBinding.current === binding) setReadError(`无法打开 ${entry.name}：${message(reason)}`); }
  }, [session?.id, transport,workspaceRoot,invokeGISCommand,refreshProjectLayers]);

  const commitLayer = useCallback(async (name:string, features:GeoJSON.Feature[], owner?:ProjectLayer, expectedRevision?:number, source?:Record<string,unknown>) => {
    if(!session) throw new Error('请先打开项目会话。');
    const commit = async () => {
      const result=await invokeGISCommand(session.id, `/earth-layer-save ${JSON.stringify({name,features,layerId:owner?.id,expectedRevision,source,commandId:crypto.randomUUID()})}`);
      const saved=bindLayerFeatures(result.layer as ProjectLayer);
      setProjectLayers(current=>[...current.filter(item=>item.id!==saved.id),saved]);
      setLayerMessage(`已保存“${saved.name}” · v${saved.revision} · ${saved.featureCount} 个要素`);
      return saved;
    };
    const pending=layerSaveQueue.current.then(commit,commit);
    layerSaveQueue.current=pending.catch(()=>{});
    return pending;
  },[invokeGISCommand,session?.id]);
  const saveProjectLayer = useCallback(async (name:string,features:GeoJSON.Feature[]) => {
    await commitLayer(name,features);
  },[commitLayer]);
  saveImportedLayer.current=async(name,features,source)=>{await commitLayer(name,features,undefined,undefined,source);};
  const updateProjectFeatures=useCallback(async(features:GeoJSON.Feature[])=>{
    const groups=new Map<string,GeoJSON.Feature[]>();
    for(const feature of features) {
      const identity=feature as GeoJSON.Feature & {pawLayerId?:string;pawRevision?:number};
      const owners=projectLayers.filter(layer=>identity.pawLayerId ? layer.id===identity.pawLayerId : layer.features.some(item=>selectionKey(item)===selectionKey(feature)));
      if(owners.length!==1)throw new Error('请选择已保存项目图层中的对象。');
      const owner=owners[0];
      if(identity.pawRevision!==undefined && identity.pawRevision!==owner.revision)throw new Error('图层已更新，草稿没有覆盖它，请重新载入。');
      groups.set(owner.id,[...(groups.get(owner.id) ?? []),feature]);
    }
    for(const [id,changes] of groups) {
      const owner=projectLayers.find(layer=>layer.id===id)!;
      const next=owner.features.map(item=>changes.find(feature=>feature.id===item.id) ?? item);
      const saved=await commitLayer(owner.name,next,owner,(changes[0] as any).pawRevision ?? owner.revision);
      setSelection(current=>current ? {...current,features:current.features.map(item=>(item as any).pawLayerId===id ? saved.features.find(feature=>feature.id===item.id) ?? item : item)}:current);
    }
  },[projectLayers,commitLayer]);
  const updateProjectFeature=useCallback(async(feature:GeoJSON.Feature)=>{await updateProjectFeatures([feature]);},[updateProjectFeatures]);

  const toggleProjectLayer=useCallback(async(layerId:string,visible:boolean)=>{
    const layer=projectLayers.find(item=>item.id===layerId);if(!session||!layer)return;
    await invokeGISCommand(session.id,`/earth-layer-metadata ${JSON.stringify({layerId,expectedRevision:layer.revision ?? 1,visible})}`);
    await refreshProjectLayers();setLayerMessage(visible?'图层已显示':'图层已隐藏');
  },[projectLayers,session?.id,invokeGISCommand,refreshProjectLayers]);
  const removeProjectLayer=useCallback(async(layerId:string)=>{
    const layer=projectLayers.find(item=>item.id===layerId);if(!session||!layer)return;
    await invokeGISCommand(session.id,`/earth-layer-metadata ${JSON.stringify({layerId,expectedRevision:layer.revision ?? 1,remove:true})}`);
    await refreshProjectLayers();setSelection(current=>{if(!current)return current;const features=current.features.filter(feature=>(feature as any).pawLayerId!==layerId);return features.length?{...current,features}:null;});
    setLayerMessage(`已移除“${layer.name}”的目录引用，历史文件仍保留。`);
  },[projectLayers,session?.id,invokeGISCommand,refreshProjectLayers]);

  const refreshLocalRuns = useCallback(async () => {
    if(!session) {setLocalRuns([]);return;}
    const result=await invokeGISCommand(session.id,'/earth-gis-runs {}');
    setLocalRuns(Array.isArray(result.runs) ? result.runs : []);
  },[invokeGISCommand,session?.id]);
  useEffect(()=> {
    let alive=true;let timer:ReturnType<typeof setTimeout>;
    const poll=async()=>{try {if(alive)await refreshLocalRuns();}catch{/* No local runs/old package; keep existing visible records. */}finally{if(alive)timer=setTimeout(()=>void poll(),5000);}};
    void poll();return()=>{alive=false;clearTimeout(timer);};
  },[refreshLocalRuns]);
  const exportProjectLayer = useCallback(async (format:'shp'|'gpkg',layer:ProjectLayer,scope:'all'|'selected') => {
    if(!session)throw new Error('请先打开项目会话。');
    const selected=selectedLayerFeatures(layer,selection?.features ?? []);
    if(scope==='selected' && !selected.length)throw new Error('当前图层没有选中要素。');
    const result=await invokeGISCommand(session.id,`/earth-gis-export ${JSON.stringify({input:layer.path,format,name:layer.name,scope,featureIds:scope==='selected' ? selected.map(feature=>feature.id) : undefined,layerId:layer.id,revision:layer.revision})}`);
    await refreshWorkspaceFiles();await refreshLocalRuns();
    setLayerMessage(`已导出 ${result.featureCount} 个要素 · ${scope==='selected'?'仅选中':'整个图层'} · ${result.outputs?.map((item:any)=>item.path).join('、')}`);
  },[invokeGISCommand,selection,refreshWorkspaceFiles,refreshLocalRuns,session?.id]);
  const connectSpatialSource = useCallback(async (source:SpatialSourceDraft) => {
    if(!session)throw new Error('请先打开项目会话。');
    const result=await invokeGISCommand(session.id,`/earth-spatial-connect ${JSON.stringify(source)}`);
    await refreshProjectLayers();
    if(result.status!=='ready'){setLayerMessage(`“${source.name}”连接尚未就绪，请查看数据库提示。`);throw new Error(result.error || '数据库连接尚未就绪。');}
    setLayerMessage(`已连接“${source.name}” · ${result.layers?.length ?? 0} 个图层`);
  },[invokeGISCommand,refreshProjectLayers,session?.id]);
  const loadSourceLayer=useCallback(async(source:SpatialSourceSummary,layer:string)=>{
    if(!session)throw new Error('请先打开项目会话。');
    const result=await invokeGISCommand(session.id,`/earth-spatial-load ${JSON.stringify({sourceId:source.id,layer})}`);
    const data=result.geojson ?? JSON.parse((await readCompleteFile(transport,{sessionId:session.id,path:`${workspaceRoot}/${result.path}`,name:layer})).content);
    await commitLayer(`${source.name} · ${layer}`,parseGeoJsonFeatures(data),undefined,undefined,result.sourceLineage);
  },[commitLayer,invokeGISCommand,session?.id,transport,workspaceRoot]);
  const createGISBundle=useCallback(async(runId:string,mapOptions?:MapOptions)=>{
    if(!session)throw new Error('请先打开项目会话。');
    const result=await invokeGISCommand(session.id,`/earth-gis-bundle ${JSON.stringify({runId,name:'earth-analysis',mapOptions})}`);
    await refreshWorkspaceFiles();
    setLayerMessage(`已生成第 ${result.manifest.version} 版成果 · ${result.verification.verifiedFiles} 个文件读回通过 · ${result.path}`);
    return {path:result.path as string,version:result.manifest.version as number,runId,verifiedFiles:result.verification.verifiedFiles as number};
  },[invokeGISCommand,refreshWorkspaceFiles,session?.id]);
  const showLocalRun=useCallback(async(runId:string)=>{
    if(!session)return;
    const result=await invokeGISCommand(session.id,`/earth-gis-run-read ${JSON.stringify({runId})}`);
    setLocalResult(result);setView('map');setLayerMessage(`显示本地运行 ${runId} 的固定结果`);
  },[invokeGISCommand,session?.id]);
  const compareLocalRuns=useCallback(async(firstRunId:string,secondRunId:string)=>{
    if(!session)return;
    const result=await invokeGISCommand(session.id,`/earth-gis-compare ${JSON.stringify({firstRunId,secondRunId})}`);
    await openWorkspaceFile({path:`${workspaceRoot}/${result.report}`,name:'report.html',kind:'file'});
  },[invokeGISCommand,session?.id,workspaceRoot,openWorkspaceFile]);
  const openLayerRevision=useCallback(async(_layer:ProjectLayer,path:string)=> {
    await openWorkspaceFile({path:`${workspaceRoot}/${path}`,name:path.split('/').pop() || '图层版本',kind:'file'});
  },[openWorkspaceFile,workspaceRoot]);
  const runSitingPlan=useCallback(async(plan:{parcels:string;avoidance:string;distance:number;commandId:string})=>{
    if(!session)throw new Error('请先打开项目。');
    setLayerMessage(`正在计算 ${plan.distance} 米避让方案…`);
    const result=await invokeGISCommand(session.id,`/earth-gis-siting ${JSON.stringify(plan)}`);
    await refreshLocalRuns();setLocalResult(result);setLayerMessage(`${plan.distance} 米方案已完成 · ${result.runId}`);
  },[invokeGISCommand,session?.id,refreshLocalRuns]);
  const queryRaster=useCallback(async(request:{path:string;band:number;geometry:GeoJSON.Geometry})=>{
    if(!session)throw new Error('请先打开项目。');
    const geometry=request.geometry;
    const command=geometry.type==='Point'?'earth-gis-pixel':'earth-gis-region';
    const input=geometry.type==='Point'?{path:request.path,band:request.band,longitude:geometry.coordinates[0],latitude:geometry.coordinates[1]}:request;
    return await invokeGISCommand(session.id,`/${command} ${JSON.stringify(input)}`);
  },[invokeGISCommand,session?.id]);
  const remotePlans=useRef(new Map<string,RemoteSensingPreparation>());
  const prepareRemote=useCallback(async(plan:RemoteSensingWorkflowPlan):Promise<RemoteSensingPreparation>=>{
    if(!session)throw new Error('请先打开项目。');
    const result=await invokeGISCommand(session.id,`/earth-remote-prepare ${JSON.stringify({plan})}`) as RemoteSensingPreparation;
    if(result.planId)remotePlans.current.set(result.planId,result);
    await refreshWorkspaceFiles();return result;
  },[invokeGISCommand,session?.id,refreshWorkspaceFiles]);
  const runRemote=useCallback(async(planId:string)=>{
    if(!session)throw new Error('请先打开项目。');
    const prepared=remotePlans.current.get(planId) as RemoteSensingPreparation & {execution?:string;backend?:string};
    const command=prepared?.execution==='gee'||prepared?.backend==='gee'?'earth-cloud-run':'earth-remote-run';
    const result=await invokeGISCommand(session.id,`/${command} ${JSON.stringify({planId})}`);
    if(result.status!=='completed')throw new Error(result.error || '运行尚未完成。');
    if(command==='earth-remote-run')setLocalResult(result);
    await refreshLocalRuns();await refreshWorkspaceFiles();setRefresh(value=>value+1);
    setLayerMessage(`遥感计算已完成 · ${result.runId}`);return result as {status:string;runId:string};
  },[invokeGISCommand,session?.id,refreshLocalRuns,refreshWorkspaceFiles]);
  const researchWithAgent=useCallback(async(planId:string)=>{
    if(!session)throw new Error('请先打开项目。');
    const text=`请读取项目 .earth/cloud-workflows/${planId}/plan.json 和 evidence-request.json，按其中固定区域、时段和问题开展调研。使用原 Session 工具查阅真实来源，必要时运行已准备的 GIS/GEE 分析。输出有来源的 HTML 报告、实际图表和缺失资料；不得把计划准备当作已完成研究。`;
    await send(createPendingAppMessage({sessionId:session.id,ownerAppId:manifest.id,surfaceKey:SURFACE,message:text}));setAgentVisible(true);
  },[session?.id,manifest.id]);
  const saveSamples=useCallback(async(input:{sampleLayerId?:string;layerName:string;classField:string;classValue:string;features:GeoJSON.Feature[]})=>{
    if(!input.features.length)throw new Error('先在地图选择样本。');
    const owner=input.sampleLayerId?projectLayers.find(layer=>layer.id===input.sampleLayerId):undefined;
    if(input.sampleLayerId&&!owner)throw new Error('样本图层不存在。');
    const samples=input.features.map(feature=>({type:'Feature' as const,id:crypto.randomUUID(),geometry:structuredClone(feature.geometry),properties:{...feature.properties,[input.classField]:input.classValue,sourceFeatureId:feature.id ?? null}}));
    await commitLayer(input.layerName,[...(owner?.features ?? []),...samples],owner,owner?.revision);
  },[commitLayer,projectLayers]);
  const persistMapState = useCallback((state: EarthMapState) => {
    mapStatePending.current = state;
    clearTimeout(mapStateTimer.current);
    mapStateTimer.current = setTimeout(async () => {
      const next = mapStatePending.current;
      if (!next || !session || !workspaceRoot) return;
      const path = `${workspaceRoot}/.earth/map-state.json`;
      try {
        let resourceRevision: string | undefined;
        try { resourceRevision = (await readCompleteFile(transport, { sessionId: session.id, path, name: 'map-state.json' })).resourceRevision; } catch { /* First state write. */ }
        const body: Record<string, string> = resourceRevision ? { path, resourceRevision, content: JSON.stringify(next, null, 2) } : { path, content: JSON.stringify(next, null, 2) };
        await transport.request({ pathId: 'agent.session.workspace.save', params: { sessionId: session.id }, body });
      } catch { /* A transient state receipt must not interrupt map use. */ }
    }, 180);
  }, [session?.id, workspaceRoot, transport]);
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
      else if(viewCommand.panel==='knowledge') setDrawer('knowledge');
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
  async function pickProjectDirectory(): Promise<string | null> {
    const path = transport.pickFiles
      ? (await transport.pickFiles({ purpose: 'workspace-root', selection: 'directory', multiple: false, maxFiles: 1 }))[0]?.path
      : (await directoryHost?.pickWorkspaceDirectory?.())?.path;
    return path?.trim() || null;
  }
  async function createProjectAgent(path: string) {
    const selectedRoot = normalizeWorkspaceRoot(path);
    if (!selectedRoot.startsWith('/')) throw new Error('请输入项目文件夹的绝对路径。');
    if (sendingRef.current || pending) throw new Error('请先等待当前请求完成。');
    sendingRef.current=true;setSending(true);setError('');
    try {
      const sameProject = sessions.filter(item => normalizeWorkspaceRoot(item.workspaceRoots?.[0] ?? '') === selectedRoot);
      const title = `${selectedRoot.split('/').filter(Boolean).at(-1) || 'GIS 项目'} · Agent ${sameProject.length + 1}`;
      const value=await transport.request<{session:SessionSummary}>({pathId:'agent.sessions.create',body:{title,mode:'coordinator',toolProfileVersion:'control-center-v1',executionMode:'workspace_managed',workspaceRoots:[selectedRoot],workspaceScopeConfirmation:'APPROVE_WORKSPACE_SCOPE',projectContextEnabled:true,piSkillsEnabled:true,codexSkillsEnabled:false,surfaceKind:'extension_app',ownerAppId:manifest.id,surfaceKey:SURFACE}});
      if(!value.session?.id)throw new Error('没有返回项目会话。');
      newSession({...value.session,workspaceRoots:value.session.workspaceRoots?.length ? value.session.workspaceRoots : [selectedRoot]});setAgentVisible(true);
    }finally{sendingRef.current=false;setSending(false);}
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
      const created = { ...value.session, workspaceRoots: value.session.workspaceRoots?.length ? value.session.workspaceRoots : [root.trim()] };
      newSession(created); setAgentVisible(true);
      await send(createPendingAppMessage({ sessionId: created.id, ownerAppId: manifest.id, surfaceKey: SURFACE, message: firstTurn(manifest.skillRef, project.trim(), analysisMode, messageWithWorkspaceContext(draft.trim(),mapContext)) }));
    } catch (reason) { setError(message(reason)); }
    finally { sendingRef.current = false; setSending(false); }
  }
  async function retry() {
    if (!pending || sendingRef.current) return; sendingRef.current = true; setSending(true);
    try { await send(pending); } catch (reason) { setError(message(reason)); } finally { sendingRef.current = false; setSending(false); }
  }
  async function requestSpatialPlan(question:string) {
    if(!session)throw new Error('请先打开本地 GIS 项目。');
    setAgentVisible(true);
    await send(createPendingAppMessage({sessionId:session.id,ownerAppId:manifest.id,surfaceKey:SURFACE,message:messageWithWorkspaceContext(`请基于本项目已有图层与实际数据，整理并执行可验证的空间分析方案。先区分必须满足的条件、比较偏好和缺失资料；没有路网、容量或高程数据时明确标记未判断，不把简单缓冲当通行时间。不要覆盖已有版本。用户目标：${question}`,mapContext)}));
    setDrawer(null);
  }
  function chooseMapTask(action:MapTaskAction) {
    if(action==='spatial'){setDrawer('gis');return;}
    if(action==='delivery'){setDrawer('delivery');return;}
    setRemoteKind(action==='change'?'ndvi':action);setDrawer('remote');
  }
  function mapActivity() { if (!editor.editing) setView(current => current === 'code' ? 'map' : current); setDrawer(null); }
  function selectMapFeature(feature: GeoJSON.Feature | null,mode:SelectionMode='replace') {
    mapActivity();
    setSelection(current=>updateSelection(current,feature,mode,mapRun?.runId ?? null));

  }
  const mapContext = selection ? {label:selection.features.length>1 ? `已选择 ${selection.features.length} 个对象` : String(selection.features[0].properties?.name ?? (selection.features[0].geometry.type==='Point' ? '地图选点' : '选中几何')),detail:selectionDetail(selection),items:selection.features.length>1 ? selection.features.map(feature=>({id:selectionKey(feature),label:String(feature.properties?.name ?? feature.properties?.id ?? feature.id ?? feature.geometry.type),onRemove:()=>selectMapFeature(feature,'remove')})) : undefined,text:JSON.stringify({runId:selection.runId,selectedObjects:selection.features.filter(feature=>(feature as any).pawLayerId).map(feature=>({layerId:(feature as any).pawLayerId,revision:(feature as any).pawRevision,featureId:feature.id,path:projectLayers.find(layer=>layer.id===(feature as any).pawLayerId)?.path})),type:'FeatureCollection',features:selection.features.filter(feature=>!(feature as any).pawLayerId)},null,2),onClear:()=>selectMapFeature(null)} : undefined;
  const activeObjectLabel = activeFile?.path ?? (selection?.features.length ? `${selection.features.length} 个地图对象` : undefined);
  const activeKind = workspaceFileKind(activeFile?.path);
  const scriptDirty = Boolean(activeFile?.path === scriptFile?.path && editor.copyContent !== null && editor.copyContent !== scriptFile?.content);
  async function runSaved() {
    if (!session || !scriptFile || busy || sendingRef.current || pending) return;
    sendingRef.current = true; setSending(true);
    try {
      const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(scriptFile.content));
      const expectedSourceHash = [...new Uint8Array(bytes)].map(x => x.toString(16).padStart(2, '0')).join('');
      await send(createPendingAppMessage({ sessionId: session.id, ownerAppId: manifest.id, surfaceKey: SURFACE,
        message: `运行我已保存的代码，不修改业务参数。请调用 earth_run_script(${JSON.stringify({ script: scriptFile.path.slice(workspaceRoot.length + 1), expectedSourceHash })})，并根据真实回执解释结果。代码版本不一致时保留当前文件并报告。` }));
    } catch (reason) { setError(message(reason)); } finally { sendingRef.current = false; setSending(false); }
  }

  return <main className={`earth-app${session && !agentVisible ? ' earth-app--agent-hidden' : ''}`}>
    {planOpen ? <div className="earth-plan-overlay" role="presentation"><section className="earth-plan-dialog" role="dialog" aria-modal="true" aria-labelledby="earth-plan-title"><h2 id="earth-plan-title">先把任务问清楚</h2><p>我会按「{ANALYSIS_MODES.find(([key]) => key === analysisMode)?.[1]}」组织一次真实 Earth Engine 分析。</p><dl><div><dt>分析范围</dt><dd>{root || '尚未填写'}</dd></div><div><dt>执行项目</dt><dd>{project || '尚未填写'}</dd></div><div><dt>用户目标</dt><dd>{draft || '尚未填写'}</dd></div></dl><p className="earth-plan-dialog__plan"><strong>建议方案</strong><br />读取官方资料 → 准备工作区 → 编写并保存 JavaScript → 执行真实脚本 → 在地图上展示图层与结果。缺少关键数据时先报告，不猜测结论。</p><div className="earth-plan-dialog__actions"><button type="button" onClick={() => setPlanOpen(false)}>返回修改</button><button type="button" onClick={() => { setPlanOpen(false); void start({ preventDefault: () => {} } as FormEvent); }}>确认方案并执行</button></div></section></div> : null}
    <header className="earth-app__header">
      <ProjectWorkspacePicker sessions={sessions} session={session} draftRoot={root} open={projectPickerOpen} loading={loading} disabled={sending || Boolean(pending)} onOpenChange={setProjectPickerOpen} onDraftRootChange={setRoot} onSelectSession={next => { newSession(next); setAgentVisible(true); }} onCreateSession={createProjectAgent} onPickDirectory={transport.pickFiles || directoryHost?.pickWorkspaceDirectory ? pickProjectDirectory : undefined} />
      <span role="status">{run ? runStatus[run.status] : session ? '项目就绪' : '打开项目'}</span>
      <button aria-expanded={Boolean(session) && agentVisible} aria-controls="earth-agent-panel" onClick={()=>setAgentVisible(value=>!value)} disabled={!session}>{agentVisible?'收起 Agent':'展开 Agent'}</button>
      <button onClick={() => setRefresh(x => x + 1)} disabled={!session}>刷新结果</button>
    </header>
    <div className="earth-app__body">
      <section className="earth-agent" id="earth-agent-panel" data-earth-surface="agent" aria-label="Agent 工作栏" hidden={Boolean(session) && !agentVisible}>

        {error ? <div className="earth-error" role="alert"><p>{error}</p>{!session ? <button onClick={() => setHistoryRevision(x => x + 1)}>重新读取</button> : null}</div> : null}
        {pending ? <div className="earth-error" role="status"><p>这条请求尚未确认接纳，已保留原消息标识。</p><button disabled={sending} onClick={() => void retry()}>核对并重试原请求</button></div> : null}
        {loading ? <p className="earth-empty" role="status">正在恢复 App 对话…</p> : session ? <PawWindowChromeProvider><PawSessionWorkspace
          key={session.id} active record={session} recordId={session.id} composerContext={mapContext}
          composerPlaceholder="描述分析目标，或继续调整当前方案…"
          onNewWork={startAnother}
          onSessionCreated={newSession} onSessionUpdated={updated => { setSession(updated); setSessions(current => current.map(item => item.id === updated.id ? updated : item)); }} onSessionActivity={() => setRefresh(x => x + 1)}
        /></PawWindowChromeProvider> : <form className="earth-start earth-start--home" onSubmit={event => { event.preventDefault(); if (root.startsWith('/') && project.trim() && draft.trim()) setPlanOpen(true); }}>
          <div className="earth-start__hero"><h1>把地理问题<br /><em>变成可验证的方案</em></h1><p>导入图层、编辑地块，或圈选范围开始分析。</p></div>
          <div className="earth-start__fields"><p>从顶部选择项目文件夹，可直接打开本地 GIS 工作区。需要云端分析时，再填写下方内容。</p><label>Google Cloud 项目<input value={project} onChange={event => setProject(event.target.value)} placeholder="已开通 Earth Engine 的项目 ID" required /></label></div>
          {mapContext ? <p className="earth-start-context">{mapContext.label} · {mapContext.detail}<button type="button" onClick={mapContext.onClear}>移除</button></p> : null}
          <label>分析类型<select aria-label="分析功能" value={analysisMode} onChange={event=>setAnalysisMode(event.target.value as AnalysisMode)}>{ANALYSIS_MODES.map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label>
          <div className="earth-start__quick-task"><span>想先试运行？</span><button type="button" onClick={() => { setAnalysisMode('site'); setDraft(GIS_ACCEPTANCE_TASK); }}>填入验收任务</button><small>{GIS_ACCEPTANCE_TASK}</small></div>
          <label className="earth-start__task">分析目标<textarea aria-label="分析任务" value={draft} onChange={event => setDraft(event.target.value)} placeholder={ANALYSIS_MODES.find(([key]) => key === analysisMode)?.[2]} rows={3} required /></label>
          <div className="earth-start__submit"><span>当前会话会保留脚本、来源、运行记录和结果文件</span><button aria-label="开始分析" disabled={sending || Boolean(error)} type="submit">生成分析方案 <span aria-hidden="true">↗</span></button></div>
        </form>}
      </section>
      <section className="earth-workspace" aria-label="地图与代码工作区">
        <nav className="earth-toolbar" aria-label="工作区视图">
          <div className="earth-toolbar__views" role="group" aria-label="视图布局">
            {([['map', '地图', MapIcon], ['split', '地图＋代码', Rows2], ['code', '代码', Code2]] as const).map(([key,label,Icon])=><button key={key} aria-pressed={view===key} title={key === 'split' ? '在地图下方查看代码或文件' : key === 'code' ? '专注查看代码或文件' : '展开地图工作区'} onClick={()=>setView(key)}><Icon size={14} aria-hidden="true"/>{label}</button>)}
          </div>
          <button className="earth-toolbar__primary" aria-pressed={drawer==='tasks'} onClick={()=>setDrawer(drawer==='tasks'?null:'tasks')}><Plus size={14} aria-hidden="true"/>开始任务</button>
          <div className="earth-toolbar__spacer"/>
          <details className="earth-toolbar__menu" onKeyDown={event=>{if(event.key==='Escape')event.currentTarget.open=false;}}><summary>分析<ChevronDown size={13} aria-hidden="true"/></summary><div>
            {([['raster','栅格查询'],['remote','遥感分析'],['gis','空间分析'],['delivery','制图交付']] as const).map(([key,label])=><button key={key} onClick={event=>{setDrawer(key);event.currentTarget.closest('details')?.removeAttribute('open');}}>{label}</button>)}
          </div></details>
          <details className="earth-toolbar__menu" onKeyDown={event=>{if(event.key==='Escape')event.currentTarget.open=false;}}><summary>资料<ChevronDown size={13} aria-hidden="true"/></summary><div>
            <button onClick={event=>{setDrawer('knowledge');event.currentTarget.closest('details')?.removeAttribute('open');}}>GIS 方法库</button>
            <button onClick={event=>{setDrawer('sources');event.currentTarget.closest('details')?.removeAttribute('open');}}>数据与官方资料</button>
          </div></details>
          <button aria-pressed={drawer==='console'} onClick={()=>setDrawer(drawer==='console'?null:'console')}>运行结果</button>
        </nav>
        {selection?.features.length?<div className="earth-task-context"><span>已选 {selection.features.length} 个对象</span><button onClick={()=>setDrawer('tasks')}>对所选开始任务</button><small>任务中再指定研究范围或训练样本</small></div>:null}
        <div className="earth-panels" data-view={view}>
          <div className="earth-map-panel" hidden={view === 'code'}><EarthMap key={workspaceRoot} workspaceKey={workspaceRoot} onActivity={mapActivity} onMapState={persistMapState} run={mapRun} command={viewCommand} selection={selection?.features ?? []} onSelect={selectMapFeature} projectLayers={projectLayers} spatialSources={spatialSources} workspaceFiles={workspaceFiles} workspaceFilesIncomplete={workspaceFilesIncomplete} workspaceFilesNotice={workspaceFilesNotice} activeObjectLabel={activeObjectLabel} onSaveLayer={saveProjectLayer} onUpdateFeature={updateProjectFeature} onUpdateFeatures={updateProjectFeatures} onCreateBundle={async(id)=>{await createGISBundle(id);}} localRuns={localRuns} localResult={localResult} onShowRun={showLocalRun} onCompareRuns={compareLocalRuns} onLoadSourceLayer={loadSourceLayer} onOpenLayerRevision={openLayerRevision} onExportLayer={exportProjectLayer} onToggleLayer={toggleProjectLayer} onRemoveLayer={removeProjectLayer} onConnectSource={connectSpatialSource} onRefreshCatalog={refreshProjectLayers} onRefreshFiles={refreshWorkspaceFiles} onOpenFile={openWorkspaceFile} />{mapRun && mapRun.runId !== run?.runId ? <span className="earth-map-retained" role="status">显示上次完成的结果 · {mapRun.runId.slice(0,8)}</span> : null}{layerMessage ? <span className="earth-layer-toast" role="status">{layerMessage}</span> : null}</div>
          <section className="earth-code" hidden={view === 'map'} aria-label="Earth Engine JavaScript">
            <header><div className="earth-code__identity"><strong>{activeKind === 'html' ? 'HTML 报告' : activeKind === 'markdown' ? 'Markdown 文档' : activeKind === 'binary' ? 'GIS 文件' : 'Earth Engine · JavaScript'}</strong>{activeFile ? <span title={activeFile.path}>{activeFile.path.split('/').pop()}</span> : null}</div><button disabled={activeKind !== 'script' || !session || !scriptFile || busy || sending || Boolean(pending) || scriptDirty} onClick={() => void runSaved()}>运行已保存代码</button><button className="earth-code__view-action" aria-label={view === 'code' ? '恢复地图与文件分屏' : '最大化文件预览'} title={view === 'code' ? '恢复地图与文件分屏' : '最大化文件预览'} onClick={() => setView(view === 'code' ? 'split' : 'code')}>{view === 'code' ? <Minimize2 size={15} aria-hidden="true" /> : <Maximize2 size={15} aria-hidden="true" />}</button><button className="earth-code__view-action" aria-label="关闭文件预览" title="关闭文件预览" onClick={() => setView('map')}><X size={15} aria-hidden="true" /></button></header>
            {run ? <small className="earth-version">运行 {run.runId.slice(0, 8)} · {scriptFile && scriptFile.content !== run.code ? '脚本已修改，结果属于上次代码' : '代码与该次运行对应'}</small> : null}
            {editor.panel}
            {!editor.editing && activeKind === 'html' && activeFile ? <iframe className="earth-html-preview" title={activeFile.path.split('/').pop() || 'HTML 报告'} sandbox="" srcDoc={activeFile.content} /> : null}
            {!editor.editing && activeKind !== 'html' && (activeFile || run) ? <CodePreview content={editor.copyContent ?? activeFile?.content ?? run?.code ?? ''} fileName={activeFile?.path.split('/').pop() || 'analysis.js'} language={activeKind === 'json' ? 'json' : 'javascript'} /> : !editor.editing && !activeFile ? <p className="earth-empty">Agent 编写的实际脚本会显示在这里。</p> : null}
          </section>
        </div>
        {readError ? <details className="earth-read-error"><summary>尚未读到最新结果 · 已保留当前内容</summary><p>{readError}</p><button onClick={() => setRefresh(x => x + 1)}>重新读取</button></details> : null}
        {drawer ? <div className="earth-drawer"><header className="earth-drawer__header"><strong>{({tasks:'开始一项地理工作',delivery:'制图与交付',remote:'遥感与区域研究',raster:'查询栅格数据',gis:'空间分析',knowledge:'方法资料',sources:'数据来源',console:'运行结果',cloud:'云端任务'} as const)[drawer]}</strong><button aria-label="关闭任务面板" onClick={()=>setDrawer(null)}><X size={16} aria-hidden="true"/></button></header>{drawer==='cloud'?<CloudTasksPanel key={session?.id} onRefresh={async()=>{if(!session)throw new Error('请先打开项目会话。');return await invokeGISCommand(session.id,'/earth-cloud-status {}') as CloudTaskSnapshot;}} onCancel={async(id)=>{if(!session)throw new Error('请先打开项目会话。');return invokeGISCommand(session.id,`/earth-cloud-cancel ${JSON.stringify({taskIds:[id]})}`);}}/>:drawer==='tasks'?<MapTaskLauncher selectedFeatures={selection?.features ?? []} workspaceReady={Boolean(session)} onChoose={chooseMapTask}/>:drawer==='delivery'?<GISDeliveryPanel runs={localRuns} onGenerate={createGISBundle} onOpenReport={async(path)=>{await openWorkspaceFile({path:`${workspaceRoot}/${path}`,name:'report.html',kind:'file'});setDrawer(null);}}/>:drawer === 'raster' ? <RasterQueryPanel key={rasterPath} initialPath={rasterPath} workspaceRoot={workspaceRoot} files={workspaceFiles.filter(file=>file.kind==='file')} selection={selection?.features ?? []} onQuery={queryRaster}/> : drawer === 'remote' ? <RemoteSensingPanel initialKind={remoteKind} imageFiles={workspaceFiles.filter(file=>file.kind==='file' && /\.tiff?$/i.test(file.path)).map(file=>({name:file.name,path:file.path.replace(`${workspaceRoot}/`,'')}))} onOpenResult={async(path)=>{await openWorkspaceFile({path:`${workspaceRoot}/${path}`,name:path.split('/').pop() || path,kind:'file'});if(/\.(html?|md|csv|json)$/i.test(path))setDrawer(null);}} projectLayers={projectLayers} selectedFeatures={selection?.features ?? []} onPrepareWorkflow={prepareRemote} onRunWorkflow={runRemote} onSaveSamples={saveSamples} onResearch={researchWithAgent}/> : drawer === 'console' ? <><button onClick={()=>setDrawer('cloud')}>查看实时云端任务</button><EarthResults run={run} /></> : drawer === 'gis' ? <section className="earth-sources earth-gis-catalog"><GISOperationPanel layers={projectLayers} onRun={runSitingPlan} onPlan={requestSpatialPlan}/><details><summary>高级：查看 GIS 算子</summary><p>Agent 可在当前 Session 工作区读取真实矢量/栅格数据，先检查 CRS 和字段，再运行确定性算子。结果保存在 <code>.earth/gis/runs/</code>，不会伪装成 Earth Engine 结果。</p>{gisCatalog.map(group => <details key={group.category} open><summary>{group.category} · {group.ops.length} 个算子</summary>{group.ops.map(operation => <div className="earth-gis-op" key={operation.op}><strong>{operation.op}</strong><span>{operation.desc}</span><small>{operation.inputs.map(input => `${input.role}:${input.kind}`).join(' · ') || '无输入'}{operation.args.length ? ` · 参数：${operation.args.map(arg => arg.name).join(', ')}` : ''}</small></div>)}</details>)}</details></section> : drawer === 'knowledge' ? <section className="earth-sources earth-gis-knowledge"><h2>GIS 方法库</h2><p>版本化的 CRS、scale、云端/本地边界、路线和机器学习规则。Agent 可通过 <code>earth_gis_search</code> 检索，再决定工具和脚本。</p><input aria-label="搜索 GIS 知识" value={knowledgeQuery} onChange={event => setKnowledgeQuery(event.target.value)} placeholder="搜索 buffer、scale、随机森林…" />{gisKnowledge.filter(item => !knowledgeQuery.trim() || `${item.title} ${item.text} ${item.tags.join(' ')}`.toLowerCase().includes(knowledgeQuery.toLowerCase())).map(item => <article className="earth-knowledge-card" key={item.id}><strong>{item.title}</strong><p>{item.text}</p><small>{item.tags.join(' · ')}</small></article>)}</section> : <section className="earth-sources"><h2>本次运行的资料</h2>{run?.sourceRefs?.length ? run.sourceRefs.filter(item => item.url.startsWith('https://developers.google.com/earth-engine/')).map(item => <p key={item.url}><a href={item.url} target="_blank" rel="noreferrer">{item.title} ↗</a><small>读取于 {item.retrievedAt}</small></p>) : <p>尚未记录官方资料读取回执。实际工具过程保留在左侧。</p>}<h2>Google 官方参考入口</h2>{SOURCES.map(([label, url]) => <a key={url} href={url} target="_blank" rel="noreferrer">{label} ↗</a>)}</section>}</div> : null}
      </section>
    </div>
  </main>;
}
export function firstTurn(skill: string, project: string, mode: AnalysisMode, task: string) {
  return `使用已安装的 App Skill：${skill}。这是 Earth Agent 的地理分析会话。Google Cloud 项目：${project}。\n本次功能：${ANALYSIS_MODES.find(([key]) => key === mode)?.[1] ?? '自定义分析'}。\n先调用 earth_workspace({project: "${project}"}) 准备执行器；写完实际 JavaScript 文件后调用 earth_run_script({script: "analysis.js"})。不要在磁盘中搜索或猜测执行器路径。使用项目的 .earth/runtime.json 配置。查阅 Google 官方资料、实际编码与运行，真实结果由执行器写入 .earth/workspace.json。禁止伪造运行记录；无数据和无可行解如实报告。若任务要求报告、图表或 HTML，必须基于已校验的结构化运行结果写入工作区，并登记 report.html、statistics.csv、method-and-quality.md 或其他实际成果文件；报告中的数字必须能回溯到 runId，导出未完成就明确写“待处理”。\n用户任务：${task}`;
}
function workspaceFileKind(filePath?: string): WorkspacePreviewKind {
  const extension = filePath?.split('.').pop()?.toLowerCase() || '';
  if (extension === 'html' || extension === 'htm') return 'html';
  if (extension === 'md' || extension === 'markdown') return 'markdown';
  if (extension === 'json' || extension === 'geojson') return 'json';
  if (['js', 'mjs', 'ts', 'py', 'txt', 'csv', 'xml', 'kml'].includes(extension)) return extension === 'js' || extension === 'mjs' || extension === 'ts' || extension === 'py' ? 'script' : 'text';
  if (['shp', 'shx', 'dbf', 'prj', 'gpkg', 'sqlite', 'db', 'tif', 'tiff', 'img', 'kmz', 'pdf', 'png', 'jpg', 'jpeg'].includes(extension)) return 'binary';
  return 'text';
}
function message(error: unknown) { return error instanceof Error ? error.message : String(error); }
