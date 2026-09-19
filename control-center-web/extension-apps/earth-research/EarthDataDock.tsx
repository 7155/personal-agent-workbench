import { useEffect, useMemo, useState } from 'react';
import type { EarthRun } from './workspace';
import type { ProjectLayer, SpatialSourceDraft, SpatialSourceSummary, WorkspaceFileSummary } from './layer-catalog';

type DockTab = 'layers' | 'files' | 'databases';

type Props = {
  run: EarthRun | null;
  workspaceRoot: string;
  projectLayers: ProjectLayer[];
  spatialSources: SpatialSourceSummary[];
  workspaceFiles: WorkspaceFileSummary[];
  activeObjectLabel?: string;
  selectedFeatures: GeoJSON.Feature[];
  onSaveLayer?: (name: string, features: GeoJSON.Feature[]) => Promise<void> | void;
  onUpdateFeature?: (feature: GeoJSON.Feature) => Promise<void> | void;
  onCreateBundle?: () => Promise<void> | void;
  onExportLayer?: (format: 'shp' | 'gpkg', layer: ProjectLayer) => Promise<void> | void;
  onToggleLayer?: (layerId: string, visible: boolean) => Promise<void> | void;
  onRemoveLayer?: (layerId: string) => Promise<void> | void;
  onConnectSource?: (source: SpatialSourceDraft) => Promise<void> | void;
  onRefreshCatalog?: () => Promise<void> | void;
  onRefreshFiles?: () => Promise<void> | void;
  onOpenFile?: (file: WorkspaceFileSummary) => Promise<void> | void;
};

const GIS_EXTENSIONS = /\.(geojson|json|shp|gpkg|sqlite|kml|kmz|tif|tiff|csv|html?|md|pdf|png|jpg|jpeg)$/iu;

export function EarthDataDock({ run, workspaceRoot, projectLayers, spatialSources, workspaceFiles, activeObjectLabel, selectedFeatures, onSaveLayer, onUpdateFeature, onCreateBundle, onExportLayer, onToggleLayer, onRemoveLayer, onConnectSource, onRefreshCatalog, onRefreshFiles, onOpenFile }: Props) {
  const [tab, setTab] = useState<DockTab>('layers');
  const [layerName, setLayerName] = useState('候选区域');
  const [selectedLayerId, setSelectedLayerId] = useState('');
  const [sourceName, setSourceName] = useState('项目数据库');
  const [sourceKind, setSourceKind] = useState<SpatialSourceDraft['kind']>('geopackage');
  const [sourcePath, setSourcePath] = useState('data/roads.gpkg');
  const [sourceSecret, setSourceSecret] = useState('PAW_POSTGIS_URL');
  const [fileFilter, setFileFilter] = useState('');
  const [error, setError] = useState('');
  const [draftProperties, setDraftProperties] = useState<Record<string, string>>({});
  const selectedLayer = projectLayers.find(layer => layer.id === selectedLayerId) ?? projectLayers[0];
  const allFiles = useMemo(() => {
    const artifactFiles = (run?.artifacts ?? []).map(artifact => ({ path: artifact.path.startsWith('/') ? artifact.path : `${workspaceRoot}/${artifact.path}`, name: artifact.path.split('/').pop() || artifact.path, kind: 'file' as const, byteSize: artifact.bytes }));
    return [...new Map([...workspaceFiles, ...artifactFiles].map(file => [file.path, file])).values()];
  }, [run?.artifacts, workspaceFiles, workspaceRoot]);
  const filteredFiles = useMemo(() => allFiles.filter(file => file.kind === 'file' && GIS_EXTENSIONS.test(file.name) && (!fileFilter.trim() || `${file.name} ${file.path}`.toLowerCase().includes(fileFilter.toLowerCase()))), [allFiles, fileFilter]);
  const runLayers = run?.layers.filter(layer => layer.tileUrl) ?? [];
  const selectedFeature = selectedFeatures[0];
  useEffect(() => {
    setDraftProperties(Object.fromEntries(Object.entries(selectedFeature?.properties ?? {}).map(([key, value]) => [key, typeof value === 'string' ? value : JSON.stringify(value ?? '')])));
  }, [selectedFeature?.id, selectedFeature?.geometry, selectedFeature?.properties]);

  async function saveLayer() {
    if (!onSaveLayer) return;
    setError('');
    try { await onSaveLayer(layerName, selectedFeatures); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  }
  async function exportLayer(format: 'shp' | 'gpkg') {
    if (!selectedLayer || !onExportLayer) return;
    setError('');
    try { await onExportLayer(format, selectedLayer); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  }
  async function connectSource() {
    if (!onConnectSource) return;
    const name = sourceName.trim();
    const source: SpatialSourceDraft = sourceKind === 'postgis' ? { name, kind: sourceKind, secretReference: sourceSecret.trim() } : { name, kind: sourceKind, path: sourcePath.trim() };
    if (!name) { setError('请填写数据源名称。'); return; }
    if (sourceKind === 'postgis' && !source.secretReference) { setError('PostGIS 只接受环境变量名，不填写连接串或密码。'); return; }
    if (sourceKind !== 'postgis' && !source.path) { setError('本地数据库需要工作区相对路径，例如 data/roads.gpkg。'); return; }
    setError('');
    try { await onConnectSource(source); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  }
  async function saveProperties() {
    if (!selectedFeature || !onUpdateFeature) return;
    setError('');
    const properties = Object.fromEntries(Object.entries(draftProperties).map(([key, value]) => {
      try { return [key, value.trim() === '' ? '' : JSON.parse(value)]; } catch { return [key, value]; }
    }));
    try { await onUpdateFeature({ ...selectedFeature, properties }); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  }

  return <aside className="earth-data-dock" aria-label="GIS 数据工作区">
    <header className="earth-data-dock__header">
      <div><strong>GIS 数据工作区</strong><small>{activeObjectLabel ? `Agent 当前操作：${activeObjectLabel}` : 'Agent 当前没有锁定对象'}</small></div>
      <button type="button" className="earth-data-dock__refresh" aria-label="刷新 GIS 数据" title="刷新图层、文件和数据库目录" onClick={() => { void onRefreshCatalog?.(); void onRefreshFiles?.(); }}>↻</button>
    </header>
    <div className="earth-data-dock__binding"><span>当前 Session</span><code title={workspaceRoot}>{workspaceRoot || '尚未绑定工作区'}</code>{run ? <small>运行 {run.runId.slice(0, 8)} · {run.status}</small> : <small>尚未有运行结果</small>}</div>
    <nav className="earth-data-dock__tabs" role="tablist" aria-label="GIS 数据视角">
      <button type="button" role="tab" aria-selected={tab === 'layers'} onClick={() => setTab('layers')}>图层 <small>{projectLayers.length + runLayers.length}</small></button>
      <button type="button" role="tab" aria-selected={tab === 'files'} onClick={() => setTab('files')}>文件 <small>{filteredFiles.length}</small></button>
      <button type="button" role="tab" aria-selected={tab === 'databases'} onClick={() => setTab('databases')}>数据库 <small>{spatialSources.length}</small></button>
    </nav>
    {tab === 'layers' ? <section className="earth-data-dock__content" role="tabpanel" aria-label="图层管理">
      <div className="earth-data-dock__section-head"><strong>地图图层</strong><span>云端结果与本地项目图层</span></div>
      {runLayers.length ? <div className="earth-data-dock__run-layers">{runLayers.map(layer => <div className="earth-data-dock__run-layer" key={layer.id}><span className="earth-data-dock__dot earth-data-dock__dot--cloud" /><strong>{layer.name}</strong><small>Earth Engine · {layer.id}</small></div>)}</div> : <p className="earth-data-dock__empty">运行完成后，Earth Engine 图层会出现在这里。</p>}
      <div className="earth-data-dock__section-head"><strong>项目图层</strong><span>{projectLayers.length} 个已登记图层</span></div>
      <div className="earth-data-dock__layer-actions"><input aria-label="新图层名称" value={layerName} onChange={event => setLayerName(event.target.value)} placeholder="图层名称" /><button type="button" disabled={!onSaveLayer || !selectedFeatures.length} onClick={() => void saveLayer()}>保存图层</button></div>
      {projectLayers.map(layer => <div className={`earth-data-dock__layer ${selectedLayer?.id === layer.id ? 'is-active' : ''}`} key={layer.id}>
        <div className="earth-data-dock__layer-head"><label><input type="checkbox" aria-label={layer.name} checked={layer.visible !== false} disabled={!onToggleLayer} onChange={event => void onToggleLayer?.(layer.id, event.target.checked)} /><strong>{layer.name}</strong></label><button type="button" aria-label={`从目录移除 ${layer.name}`} onClick={() => void onRemoveLayer?.(layer.id)}>移除</button></div>
        <button type="button" className="earth-data-dock__layer-select" onClick={() => setSelectedLayerId(layer.id)}><span>{layer.featureCount} 要素 · {layer.geometryTypes.join(' / ') || '未知'} · {layer.crs} · v{layer.revision ?? 1}</span><code>{layer.path}</code></button>
      </div>)}
      {!projectLayers.length ? <p className="earth-data-dock__empty">先在地图上画点、线或面，再保存为项目图层。它会写入当前 Session 的 .earth/layers/。</p> : null}
      {selectedFeatures.length ? <section className="earth-data-dock__selection" aria-label="当前选中对象"><div className="earth-data-dock__section-head"><strong>当前选中对象</strong><span>{selectedFeatures.length} 个</span></div><table><tbody>{Object.entries(selectedFeature?.properties ?? {}).slice(0, 8).map(([key, value]) => <tr key={key}><th>{key}</th><td>{typeof value === 'object' ? JSON.stringify(value) : String(value ?? '')}</td></tr>)}</tbody></table>{selectedFeature && onUpdateFeature ? <div className="earth-data-dock__attribute-editor"><strong>属性编辑</strong>{Object.keys(draftProperties).map(key => <label key={key}>{key}<input aria-label={`属性 ${key}`} value={draftProperties[key] ?? ''} onChange={event => setDraftProperties(current => ({ ...current, [key]: event.target.value }))} /></label>)}<button type="button" onClick={() => void saveProperties()}>保存属性版本</button></div> : null}<small>这是地图选择上下文；属性保存会生成新的图层版本，几何与字段可在项目重开后恢复。</small></section> : null}
      <div className="earth-data-dock__export"><select aria-label="导出图层" value={selectedLayer?.id ?? ''} onChange={event => setSelectedLayerId(event.target.value)}><option value="">选择项目图层</option>{projectLayers.map(layer => <option key={layer.id} value={layer.id}>{layer.name} · {layer.featureCount}</option>)}</select><button type="button" disabled={!selectedLayer || !onExportLayer} onClick={() => void exportLayer('shp')}>SHP</button><button type="button" disabled={!selectedLayer || !onExportLayer} onClick={() => void exportLayer('gpkg')}>GPKG</button></div>
      <div className="earth-data-dock__layer-actions"><button type="button" className="earth-data-dock__database-link" onClick={() => setTab('databases')}>连接空间数据库</button>{run?.status === 'completed' && onCreateBundle ? <button type="button" onClick={() => void onCreateBundle()}>打包成果</button> : null}</div>
    </section> : null}
    {tab === 'files' ? <section className="earth-data-dock__content" role="tabpanel" aria-label="工作区文件管理">
      <div className="earth-data-dock__section-head"><strong>工作区文件</strong><span>Agent 实际可读写的文件</span></div>
      <div className="earth-data-dock__file-toolbar"><input aria-label="筛选 GIS 文件" value={fileFilter} onChange={event => setFileFilter(event.target.value)} placeholder="筛选 geojson、gpkg、html…" /><button type="button" onClick={() => void onRefreshFiles?.()}>刷新</button></div>
      {filteredFiles.map(file => <div className="earth-data-dock__file" key={file.path}><div><strong>{file.name}</strong><small>{file.byteSize === undefined ? '文件' : `${file.byteSize.toLocaleString()} bytes`}</small></div><code title={file.path}>{file.path}</code><button type="button" onClick={() => void onOpenFile?.(file)}>打开</button></div>)}
      {!filteredFiles.length ? <p className="earth-data-dock__empty">当前工作区没有已读取的 GIS、报告或 HTML 文件。</p> : null}
    </section> : null}
    {tab === 'databases' ? <section className="earth-data-dock__content" role="tabpanel" aria-label="空间数据库">
      <div className="earth-data-dock__section-head"><strong>空间数据库目录</strong><span>GeoPackage · SpatiaLite · PostGIS</span></div>
      {spatialSources.map(source => <div className="earth-data-dock__database" key={source.id}><div><strong>{source.name}</strong><span className={`earth-source-status earth-source-status--${source.status}`}>{source.status}</span></div><small>{source.kind} · {source.layers.length ? source.layers.join(' · ') : '尚未读取图层'}</small><code>{source.path || source.schema || 'secret reference'}</code></div>)}
      {!spatialSources.length ? <p className="earth-data-dock__empty">还没有登记空间数据库。登记后，Agent 会读取真实目录并保留状态。</p> : null}
      <div className="earth-data-dock__db-form"><label>名称<input aria-label="空间数据源名称" value={sourceName} onChange={event => setSourceName(event.target.value)} /></label><label>类型<select aria-label="空间数据源类型" value={sourceKind} onChange={event => setSourceKind(event.target.value as SpatialSourceDraft['kind'])}><option value="geopackage">GeoPackage</option><option value="spatialite">SpatiaLite</option><option value="postgis">PostGIS</option></select></label>{sourceKind === 'postgis' ? <label>密钥引用<input aria-label="PostGIS 密钥引用" value={sourceSecret} onChange={event => setSourceSecret(event.target.value)} /><small>只填环境变量名，不填密码。</small></label> : <label>工作区路径<input aria-label="空间数据库路径" value={sourcePath} onChange={event => setSourcePath(event.target.value)} /></label>}<button type="button" onClick={() => void connectSource()}>连接并登记</button></div>
    </section> : null}
    {error ? <p className="earth-data-dock__error" role="alert">{error}</p> : null}
  </aside>;
}
