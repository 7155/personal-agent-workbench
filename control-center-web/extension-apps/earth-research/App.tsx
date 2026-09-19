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
import { parseMapState, parseViewCommand, type EarthMapState, type EarthViewCommand } from './pi-package/view-contract';
import { featureCollection, layerSlug, parseGeoJsonFeatures, parseProjectLayerCatalog, parseSpatialCatalog, type ProjectLayer, type SpatialSourceDraft, type SpatialSourceSummary, type WorkspaceFileSummary } from './layer-catalog';
import './app.css';
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
  const scopeHintId = useId();
  const [session, setSession] = useState<SessionSummary>();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [readError, setReadError] = useState('');
  const [root, setRoot] = useState('');
  const [project, setProject] = useState('');
  const [draft, setDraft] = useState('');
  const [analysisMode, setAnalysisMode] = useState<AnalysisMode>('site');
  const [planOpen, setPlanOpen] = useState(false);
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
  const [drawer, setDrawer] = useState<'console' | 'sources' | 'gis' | 'knowledge' | null>(null);
  const [knowledgeQuery, setKnowledgeQuery] = useState('');
  const [selection, setSelection] = useState<MapSelection | null>(null);
  const [projectLayers, setProjectLayers] = useState<ProjectLayer[]>([]);
  const [spatialSources, setSpatialSources] = useState<SpatialSourceSummary[]>([]);
  const [workspaceFiles, setWorkspaceFiles] = useState<WorkspaceFileSummary[]>([]);
  const [layerMessage, setLayerMessage] = useState('');
  const [viewCommand, setViewCommand] = useState<EarthViewCommand>();
  const viewSeen = useRef('');
  const mapStateTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const mapStatePending = useRef<EarthMapState | undefined>(undefined);
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
        if (alive && source.path === next.scriptPath && typeof source.content === 'string') {
          setScriptFile(source);
          // Keep the script visible only while the user has not opened another
          // workspace artifact.  Reports, maps and HTML remain authoritative
          // until the user explicitly opens a different file.
          setActiveFile(current => current === null || current.path === next.scriptPath ? source : current);
        }
      } catch (reason) { if (alive) setReadError(message(reason)); }
      finally { if (alive) timer = setTimeout(() => void read(), 2500); }
    }
    void read();
    return () => { alive = false; clearTimeout(timer); };
  }, [session?.id, workspaceRoot, transport, refresh]);

  const editor = useWorkspaceTextEditor(session && activeFile ? { sessionId: session.id, path: activeFile.path, name: activeFile.path.split('/').pop() || 'analysis.js' } : null, activeFile, saved => setActiveFile(saved));
  const newSession = useCallback((created: SessionSummary) => { setViewCommand(undefined); viewSeen.current = ""; setSession(created); setSessions(current => [created, ...current.filter(x => x.id !== created.id)]); setRun(null); setLastCompleted(null); setScriptFile(null); setActiveFile(null); }, []);
  function startAnother() { setViewCommand(undefined); viewSeen.current = ""; setRoot(workspaceRoot); setSession(undefined); setRun(null); setLastCompleted(null); setScriptFile(null); setActiveFile(null); setError(''); setReadError(''); setSelection(null); }
  const mapRun = run?.status === 'completed' ? run : lastCompleted;

  const saveWorkspaceFile = useCallback(async (path: string, content: string) => {
    if (!session) throw new Error('请先开始一个分析会话，再保存项目图层。');
    let resourceRevision: string | undefined;
    try { resourceRevision = (await readCompleteFile(transport, { sessionId: session.id, path, name: path.split('/').pop() || 'workspace-file' })).resourceRevision; } catch { /* First write. */ }
    const body: Record<string, string> = resourceRevision ? { path, resourceRevision, content } : { path, content };
    await transport.request({ pathId: 'agent.session.workspace.save', params: { sessionId: session.id }, body });
  }, [session?.id, transport]);

  const persistProjectLayerCatalog = useCallback(async (layers: ProjectLayer[]) => {
    const catalogPath = `${workspaceRoot}/.earth/layers/catalog.json`;
    const catalogLayers = layers.map(({ features: _features, ...item }) => item);
    await saveWorkspaceFile(catalogPath, JSON.stringify({ schemaVersion: 'earth.spatial-layer-catalog.v1', updatedAt: new Date().toISOString(), layers: catalogLayers }, null, 2));
  }, [saveWorkspaceFile, workspaceRoot]);

  const refreshProjectLayers = useCallback(async () => {
    if (!session || !workspaceRoot) { setProjectLayers([]); setSpatialSources([]); return; }
    const catalogPath = `${workspaceRoot}/.earth/layers/catalog.json`;
    try {
      const catalogFile = await readCompleteFile(transport, { sessionId: session.id, path: catalogPath, name: 'catalog.json' });
      const records = parseProjectLayerCatalog(JSON.parse(catalogFile.content));
      const loaded = await Promise.all(records.filter(record => record.path).map(async record => {
        try {
          const file = await readCompleteFile(transport, { sessionId: session.id, path: `${workspaceRoot}/${record.path}`, name: record.path.split('/').pop() || record.name });
          return { ...record, features: parseGeoJsonFeatures(JSON.parse(file.content)) } as ProjectLayer;
        } catch { return { ...record, features: [] } as ProjectLayer; }
      }));
      setProjectLayers(loaded);
      setLayerMessage(loaded.length ? `已加载 ${loaded.length} 个项目图层` : '');
    } catch { setProjectLayers([]); }
    try {
      const databaseFile = await readCompleteFile(transport, { sessionId: session.id, path: `${workspaceRoot}/.earth/gis/databases.json`, name: 'databases.json' });
      setSpatialSources(parseSpatialCatalog(JSON.parse(databaseFile.content)));
    } catch { setSpatialSources([]); }
  }, [session?.id, transport, workspaceRoot]);

  useEffect(() => { void refreshProjectLayers(); }, [refreshProjectLayers]);

  const refreshWorkspaceFiles = useCallback(async () => {
    if (!session || !workspaceRoot) { setWorkspaceFiles([]); return; }
    try {
      const files: WorkspaceFileSummary[] = [];
      const queue: Array<{ path: string; depth: number }> = [{ path: workspaceRoot, depth: 0 }];
      const visited = new Set<string>();
      while (queue.length && files.length < 240) {
        const current = queue.shift()!;
        if (visited.has(current.path) || current.depth > 4) continue;
        visited.add(current.path);
        const value = await transport.request({ pathId: 'agent.session.workspace.list', params: { sessionId: session.id }, query: { path: current.path, depth: 1, limit: 240 } });
        const items = value && typeof value === 'object' && !Array.isArray(value) && Array.isArray((value as { items?: unknown }).items) ? (value as { items: unknown[] }).items : [];
        for (const item of items) {
          if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
          const record = item as Record<string, unknown>;
          if (typeof record.path !== 'string' || typeof record.name !== 'string' || !record.path.startsWith(`${workspaceRoot}/`)) continue;
          const kind = record.kind === 'directory' || record.kind === 'symlink' ? record.kind : 'file';
          const entry = { path: record.path, name: record.name, kind, byteSize: typeof record.byteSize === 'number' ? record.byteSize : undefined } as WorkspaceFileSummary;
          if (kind === 'directory' && current.depth < 4) queue.push({ path: record.path, depth: current.depth + 1 });
          else if (kind === 'file') files.push(entry);
        }
      }
      setWorkspaceFiles(files);
    } catch { setWorkspaceFiles([]); }
  }, [session?.id, transport, workspaceRoot]);

  useEffect(() => { void refreshWorkspaceFiles(); }, [refreshWorkspaceFiles, refresh]);

  const invokeGISCommand = useCallback(async (sessionId: string, command: string): Promise<Record<string, any>> => {
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
    return result;
  }, [transport]);

  const openWorkspaceFile = useCallback(async (entry: WorkspaceFileSummary) => {
    if (!session) return;
    const kind = workspaceFileKind(entry.path);
    if (kind === 'binary') {
      setReadError(`“${entry.name}”是二进制 GIS 文件，已保留在图层/文件目录中；请使用对应的图层或导出操作查看。`);
      return;
    }
    try {
      const snapshot = await readCompleteFile(transport, { sessionId: session.id, path: entry.path, name: entry.name });
      setActiveFile(snapshot);
      setView('code');
      setLayerMessage(`已打开文件：${entry.path}`);
      setReadError('');
    } catch (reason) { setReadError(`无法打开 ${entry.name}：${message(reason)}`); }
  }, [session?.id, transport]);

  const saveProjectLayer = useCallback(async (name: string, features: GeoJSON.Feature[]) => {
    const cleanName = name.trim();
    if (!cleanName || !features.length) throw new Error('请先选择至少一个点、线或面，并填写图层名称。');
    const slug = layerSlug(cleanName);
    const existing = projectLayers.find(item => item.path === `.earth/layers/${slug}.geojson` || item.name === cleanName);
    const id = existing?.id ?? `layer:${crypto.randomUUID()}`;
    const relativePath = existing?.path ?? `.earth/layers/${slug}-${crypto.randomUUID().slice(0, 8)}.geojson`;
    const revision = (existing?.revision ?? 0) + 1;
    const historyPath = `.earth/layers/history/${id.replace(/[^A-Za-z0-9_-]+/g, '_')}/v${revision}.geojson`;
    const now = new Date().toISOString();
    const stableFeatures = features.map((feature, index) => ({ ...structuredClone(feature), id: feature.id ?? `${id}:feature:${index + 1}` }));
    const geometryTypes = [...new Set(stableFeatures.map(feature => feature.geometry?.type).filter(Boolean))] as string[];
    const layer: ProjectLayer = { id, name: cleanName, path: relativePath, format: 'geojson', featureCount: stableFeatures.length, geometryTypes, crs: 'EPSG:4326', updatedAt: now, revision, history: [...(existing?.history ?? []), historyPath], visible: existing?.visible !== false, features: stableFeatures };
    const serialized = JSON.stringify(featureCollection(stableFeatures), null, 2);
    await saveWorkspaceFile(`${workspaceRoot}/${historyPath}`, serialized);
    await saveWorkspaceFile(`${workspaceRoot}/${relativePath}`, serialized);
    const current = projectLayers.filter(item => item.id !== layer.id);
    const next = current.concat(layer);
    await persistProjectLayerCatalog(next);
    setProjectLayers(next);
    setLayerMessage(`已保存图层“${cleanName}” · ${features.length} 个要素`);
  }, [persistProjectLayerCatalog, projectLayers, saveWorkspaceFile, workspaceRoot]);

  const updateProjectFeature = useCallback(async (feature: GeoJSON.Feature) => {
    const key = selectionKey(feature);
    const owner = projectLayers.find(layer => layer.features.some(item => selectionKey(item) === key));
    if (!owner) throw new Error('该对象尚未登记到项目图层，请先保存图层后再编辑属性。');
    const nextFeatures = owner.features.map(item => selectionKey(item) === key ? structuredClone(feature) : item);
    await saveProjectLayer(owner.name, nextFeatures);
    setSelection(current => current ? { ...current, features: current.features.map(item => selectionKey(item) === key ? structuredClone(feature) : item) } : current);
    setLayerMessage(`已保存“${owner.name}”的属性版本`);
  }, [projectLayers, saveProjectLayer]);

  const toggleProjectLayer = useCallback(async (layerId: string, visible: boolean) => {
    const next = projectLayers.map(layer => layer.id === layerId ? { ...layer, visible } : layer);
    await persistProjectLayerCatalog(next);
    setProjectLayers(next);
    setLayerMessage(visible ? '图层已显示' : '图层已隐藏');
  }, [persistProjectLayerCatalog, projectLayers]);

  const removeProjectLayer = useCallback(async (layerId: string) => {
    const layer = projectLayers.find(item => item.id === layerId);
    if (!layer) return;
    const next = projectLayers.filter(item => item.id !== layerId);
    await persistProjectLayerCatalog(next);
    setProjectLayers(next);
    setLayerMessage(`已从项目目录移除“${layer.name}”；原始文件仍保留在工作区`);
  }, [persistProjectLayerCatalog, projectLayers]);

  const exportProjectLayer = useCallback(async (format: 'shp' | 'gpkg', layer: ProjectLayer) => {
    if (!session) throw new Error('请先开始一个分析会话。');
    await saveProjectLayer(layer.name, layer.features);
    const result = await invokeGISCommand(session.id, `/earth-gis-export ${JSON.stringify({ input: layer.path, format, name: layerSlug(layer.name) })}`);
    const outputs = Array.isArray(result.outputs) ? result.outputs.map((item: any) => item?.path || item?.name).filter(Boolean).join('、') : '';
    setLayerMessage(`已导出 ${format.toUpperCase()}：${outputs || '已写入工作区'} · ${result.featureCount ?? layer.featureCount} 个要素`);
    await refreshWorkspaceFiles();
  }, [invokeGISCommand, refreshWorkspaceFiles, saveProjectLayer, session?.id]);

  const connectSpatialSource = useCallback(async (source: SpatialSourceDraft) => {
    if (!session) throw new Error('请先开始一个分析会话。');
    const result = await invokeGISCommand(session.id, `/earth-spatial-connect ${JSON.stringify(source)}`);
    setLayerMessage(`已登记“${source.name}” · ${result.status || 'completed'}${result.layers?.length ? ` · ${result.layers.length} 个图层` : ''}`);
    await refreshProjectLayers();
  }, [invokeGISCommand, refreshProjectLayers, session?.id]);
  const createGISBundle = useCallback(async () => {
    if (!session || !mapRun || mapRun.status !== 'completed') throw new Error('请先等待本次 GIS 运行完成。');
    const result = await invokeGISCommand(session.id, `/earth-gis-bundle ${JSON.stringify({ runId: mapRun.runId, name: 'earth-analysis', version: 1 })}`);
    setLayerMessage(`成果包已写入 ${result.path || '.earth/deliverables'}，请在文件目录读回 run-manifest.json。`);
    await refreshWorkspaceFiles();
  }, [invokeGISCommand, mapRun, refreshWorkspaceFiles, session?.id]);
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
      await send(createPendingAppMessage({ sessionId: created.id, ownerAppId: manifest.id, surfaceKey: SURFACE, message: firstTurn(manifest.skillRef, project.trim(), analysisMode, messageWithWorkspaceContext(draft.trim(),mapContext)) }));
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

  return <main className="earth-app">
    {planOpen ? <div className="earth-plan-overlay" role="presentation"><section className="earth-plan-dialog" role="dialog" aria-modal="true" aria-labelledby="earth-plan-title"><p className="earth-app__eyebrow">GRILL · 执行前核对</p><h2 id="earth-plan-title">先把任务问清楚</h2><p>我会按「{ANALYSIS_MODES.find(([key]) => key === analysisMode)?.[1]}」组织一次真实 Earth Engine 分析。</p><dl><div><dt>分析范围</dt><dd>{root || '尚未填写'}</dd></div><div><dt>执行项目</dt><dd>{project || '尚未填写'}</dd></div><div><dt>用户目标</dt><dd>{draft || '尚未填写'}</dd></div></dl><p className="earth-plan-dialog__plan"><strong>建议方案</strong><br />读取官方资料 → 准备工作区 → 编写并保存 JavaScript → 执行真实脚本 → 在地图上展示图层与结果。缺少关键数据时先报告，不猜测结论。</p><div className="earth-plan-dialog__actions"><button type="button" onClick={() => setPlanOpen(false)}>返回修改</button><button type="button" onClick={() => { setPlanOpen(false); void start({ preventDefault: () => {} } as FormEvent); }}>确认方案并执行</button></div></section></div> : null}
        <header className="earth-app__header"><div><strong>Earth Agent</strong><span>地理分析与选址选线</span></div>{sessions.length ? <select aria-label="分析会话" value={session?.id || ''} onChange={event => { const next = sessions.find(x => x.id === event.target.value); if (next) { setSession(next); setRun(null); setLastCompleted(null); setScriptFile(null); setActiveFile(null); setSelection(null); setViewCommand(undefined); viewSeen.current = ""; } }}><option value="" disabled>新分析</option>{sessions.map(item => <option key={item.id} value={item.id}>{item.title} · {new Date(item.updatedAtMs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</option>)}</select> : null}<span role="status">{run ? runStatus[run.status] : '准备开始分析'}</span><button onClick={startAnother} disabled={sending || Boolean(pending)}>新分析</button><button onClick={() => setRefresh(x => x + 1)} disabled={!session}>刷新结果</button></header>
    <div className="earth-app__body">
      <section className="earth-agent" data-earth-surface="agent" aria-label="Agent 工作栏">

        {error ? <div className="earth-error" role="alert"><p>{error}</p>{!session ? <button onClick={() => setHistoryRevision(x => x + 1)}>重新读取</button> : null}</div> : null}
        {pending ? <div className="earth-error" role="status"><p>这条请求尚未确认接纳，已保留原消息标识。</p><button disabled={sending} onClick={() => void retry()}>核对并重试原请求</button></div> : null}
        {loading ? <p className="earth-empty" role="status">正在恢复 App 对话…</p> : session ? <PawWindowChromeProvider><PawSessionWorkspace
          key={session.id} active record={session} recordId={session.id} composerContext={mapContext}
          composerPlaceholder="描述分析目标，或继续调整当前方案…"
          onNewWork={startAnother}
          onSessionCreated={newSession} onSessionUpdated={setSession} onSessionActivity={() => setRefresh(x => x + 1)}
        /></PawWindowChromeProvider> : <form className="earth-start earth-start--home" onSubmit={event => { event.preventDefault(); if (root.startsWith('/') && project.trim() && draft.trim()) setPlanOpen(true); }}>
          <div className="earth-start__hero"><p className="earth-app__eyebrow">GOOGLE EARTH AGENT · WORKSPACE</p><h1>把地理问题<br /><em>变成可验证的方案</em></h1><p>云端遥感、本地 GIS 和地图交互在同一个工作流里运行。每一步都有代码、数据来源和真实回执。</p></div>
          <div className="earth-start__fields"><label>项目文件夹<input aria-describedby={scopeHintId} value={root} onChange={event => setRoot(event.target.value)} placeholder="选择已有分析项目的绝对路径" required /></label><small id={scopeHintId}>Agent 只在这个绑定工作区内读取、编辑和运行。</small><label>Google Cloud 项目<input value={project} onChange={event => setProject(event.target.value)} placeholder="已开通 Earth Engine 的项目 ID" required /></label></div>
          {mapContext ? <p className="earth-start-context">{mapContext.label} · {mapContext.detail}<button type="button" onClick={mapContext.onClear}>移除</button></p> : null}
          <fieldset className="earth-mode-picker"><legend>从一个工作流开始</legend><div role="radiogroup" aria-label="分析功能">{ANALYSIS_MODES.map(([key,label,hint], index) => <button type="button" key={key} aria-pressed={analysisMode === key} className={`earth-mode-card earth-mode-card--${index + 1}`} onClick={() => setAnalysisMode(key)}><span className="earth-mode-card__index">0{index + 1}</span><strong>{label}</strong><small>{hint}</small></button>)}</div></fieldset>
          <div className="earth-start__quick-task"><span>想先试运行？</span><button type="button" onClick={() => { setAnalysisMode('site'); setDraft(GIS_ACCEPTANCE_TASK); }}>填入验收任务</button><small>{GIS_ACCEPTANCE_TASK}</small></div>
          <label className="earth-start__task">分析目标<textarea aria-label="分析任务" value={draft} onChange={event => setDraft(event.target.value)} placeholder={ANALYSIS_MODES.find(([key]) => key === analysisMode)?.[2]} rows={3} required /></label>
          <div className="earth-start__submit"><span>当前会话会保留脚本、来源、运行记录和结果文件</span><button aria-label="开始分析" disabled={sending || Boolean(error)} type="submit">生成分析方案 <span aria-hidden="true">↗</span></button></div>
        </form>}
      </section>
      <section className="earth-workspace" aria-label="地图与代码工作区">
        <nav className="earth-toolbar" aria-label="工作区视图">{([['split', '地图＋代码'], ['map', '地图'], ['code', '代码']] as const).map(([key, label]) => <button key={key} aria-pressed={view === key} onClick={() => setView(key)}>{label}</button>)}<div className="earth-toolbar__spacer" /><button aria-pressed={drawer === 'knowledge'} onClick={() => setDrawer(drawer === 'knowledge' ? null : 'knowledge')}>GIS 知识</button><button aria-pressed={drawer === 'sources'} onClick={() => setDrawer(drawer === 'sources' ? null : 'sources')}>官方资料</button><button aria-pressed={drawer === 'gis'} onClick={() => setDrawer(drawer === 'gis' ? null : 'gis')}>GIS 工具箱</button><button aria-pressed={drawer === 'console'} onClick={() => setDrawer(drawer === 'console' ? null : 'console')}>控制台</button></nav>
        <div className="earth-panels" data-view={view}>
          <div className="earth-map-panel" hidden={view === 'code'}><EarthMap key={workspaceRoot} workspaceKey={workspaceRoot} onActivity={mapActivity} onMapState={persistMapState} run={mapRun} command={viewCommand} selection={selection?.features ?? []} onSelect={selectMapFeature} projectLayers={projectLayers} spatialSources={spatialSources} workspaceFiles={workspaceFiles} activeObjectLabel={activeObjectLabel} onSaveLayer={saveProjectLayer} onUpdateFeature={updateProjectFeature} onCreateBundle={createGISBundle} onExportLayer={exportProjectLayer} onToggleLayer={toggleProjectLayer} onRemoveLayer={removeProjectLayer} onConnectSource={connectSpatialSource} onRefreshCatalog={refreshProjectLayers} onRefreshFiles={refreshWorkspaceFiles} onOpenFile={openWorkspaceFile} />{mapRun && mapRun.runId !== run?.runId ? <span className="earth-map-retained" role="status">显示上次完成的结果 · {mapRun.runId.slice(0,8)}</span> : null}{layerMessage ? <span className="earth-layer-toast" role="status">{layerMessage}</span> : null}</div>
          <section className="earth-code" hidden={view === 'map'} aria-label="Earth Engine JavaScript">
            <header><strong>{activeKind === 'html' ? 'HTML 报告' : activeKind === 'markdown' ? 'Markdown 文档' : activeKind === 'binary' ? 'GIS 文件' : 'Earth Engine · JavaScript'}</strong><button disabled={activeKind !== 'script' || !session || !scriptFile || busy || sending || Boolean(pending) || scriptDirty} onClick={() => void runSaved()}>运行已保存代码</button></header>
            {run ? <small className="earth-version">运行 {run.runId.slice(0, 8)} · {scriptFile && scriptFile.content !== run.code ? '脚本已修改，结果属于上次代码' : '代码与该次运行对应'}</small> : null}
            {editor.panel}
            {!editor.editing && activeKind === 'html' && activeFile ? <iframe className="earth-html-preview" title={activeFile.path.split('/').pop() || 'HTML 报告'} sandbox="" srcDoc={activeFile.content} /> : null}
            {!editor.editing && activeKind !== 'html' && (activeFile || run) ? <CodePreview content={editor.copyContent ?? activeFile?.content ?? run?.code ?? ''} fileName={activeFile?.path.split('/').pop() || 'analysis.js'} language={activeKind === 'json' ? 'json' : 'javascript'} /> : !editor.editing && !activeFile ? <p className="earth-empty">Agent 编写的实际脚本会显示在这里。</p> : null}
          </section>
        </div>
        {readError ? <details className="earth-read-error"><summary>尚未读到最新结果 · 已保留当前内容</summary><p>{readError}</p><button onClick={() => setRefresh(x => x + 1)}>重新读取</button></details> : null}
        {drawer ? <div className="earth-drawer">{drawer === 'console' ? <EarthResults run={run} /> : drawer === 'gis' ? <section className="earth-sources earth-gis-catalog"><h2>本地 GIS 工具箱</h2><p>Agent 可在当前 Session 工作区读取真实矢量/栅格数据，先检查 CRS 和字段，再运行确定性算子。结果保存在 <code>.earth/gis/runs/</code>，不会伪装成 Earth Engine 结果。</p>{gisCatalog.map(group => <details key={group.category} open><summary>{group.category} · {group.ops.length} 个算子</summary>{group.ops.map(operation => <div className="earth-gis-op" key={operation.op}><strong>{operation.op}</strong><span>{operation.desc}</span><small>{operation.inputs.map(input => `${input.role}:${input.kind}`).join(' · ') || '无输入'}{operation.args.length ? ` · 参数：${operation.args.map(arg => arg.name).join(', ')}` : ''}</small></div>)}</details>)}</section> : drawer === 'knowledge' ? <section className="earth-sources earth-gis-knowledge"><h2>GIS 方法库</h2><p>版本化的 CRS、scale、云端/本地边界、路线和机器学习规则。Agent 可通过 <code>earth_gis_search</code> 检索，再决定工具和脚本。</p><input aria-label="搜索 GIS 知识" value={knowledgeQuery} onChange={event => setKnowledgeQuery(event.target.value)} placeholder="搜索 buffer、scale、随机森林…" />{gisKnowledge.filter(item => !knowledgeQuery.trim() || `${item.title} ${item.text} ${item.tags.join(' ')}`.toLowerCase().includes(knowledgeQuery.toLowerCase())).map(item => <article className="earth-knowledge-card" key={item.id}><strong>{item.title}</strong><p>{item.text}</p><small>{item.tags.join(' · ')}</small></article>)}</section> : <section className="earth-sources"><h2>本次运行的资料</h2>{run?.sourceRefs?.length ? run.sourceRefs.filter(item => item.url.startsWith('https://developers.google.com/earth-engine/')).map(item => <p key={item.url}><a href={item.url} target="_blank" rel="noreferrer">{item.title} ↗</a><small>读取于 {item.retrievedAt}</small></p>) : <p>尚未记录官方资料读取回执。实际工具过程保留在左侧。</p>}<h2>Google 官方参考入口</h2>{SOURCES.map(([label, url]) => <a key={url} href={url} target="_blank" rel="noreferrer">{label} ↗</a>)}</section>}</div> : null}
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
