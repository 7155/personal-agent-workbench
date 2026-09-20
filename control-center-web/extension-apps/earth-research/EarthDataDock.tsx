import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { ArrowDown, ArrowUp, ChevronDown, ChevronRight, Database, File, FileArchive, FileImage, FileText, Folder, FolderOpen, Layers, Link, PanelRightClose, PanelRightOpen, RefreshCw, Search, Table2, TriangleAlert } from 'lucide-react';
import type { EarthRun } from './workspace';
import { selectedLayerFeatures, type ProjectLayer, type SpatialSourceDraft, type SpatialSourceSummary, type WorkspaceFileSummary } from './layer-catalog';
import { createPropertyDraft, discardPropertyDraft, hasEditedProperties, materializePropertyDraft, updatePropertyDraft, type PropertyDraft } from './property-draft';
import { buildWorkspaceFileTree, filterWorkspaceFileTree, workspaceNodeByteSize, WORKSPACE_FILE_LABELS, type WorkspaceFileNode } from './workspace-file-tree';
import './earth-files.css';

type DockTab = 'layers' | 'attributes' | 'runs' | 'files' | 'databases';
type BoundFeature = GeoJSON.Feature & { pawLayerId?: string; pawRevision?: number };
type EditSession = { feature: BoundFeature; draft: PropertyDraft };

export type LocalGISRunSummary = {
  runId: string;
  status: string;
  op: string;
  params?: Record<string, unknown>;
  updatedAt: string;
};

export type EarthDataDockProps = {
  run: EarthRun | null;
  workspaceRoot: string;
  projectLayers: ProjectLayer[];
  spatialSources: SpatialSourceSummary[];
  workspaceFiles: WorkspaceFileSummary[];
  workspaceFilesIncomplete?: boolean;
  workspaceFilesNotice?: string;
  localRuns?: LocalGISRunSummary[];
  activeObjectLabel?: string;
  selectedFeatures: GeoJSON.Feature[];
  onSaveLayer?: (name: string, features: GeoJSON.Feature[]) => Promise<void> | void;
  onUpdateFeature?: (feature: GeoJSON.Feature) => Promise<void> | void;
  onSelectFeature?: (feature: GeoJSON.Feature, layerId: string) => Promise<void> | void;
  onCreateBundle?: (runId: string) => Promise<void> | void;
  onShowRun?: (runId: string) => Promise<void> | void;
  onCompareRuns?: (firstRunId: string, secondRunId: string) => Promise<void> | void;
  onExportLayer?: (format: 'shp' | 'gpkg', layer: ProjectLayer, scope: 'all' | 'selected') => Promise<void> | void;
  onToggleLayer?: (layerId: string, visible: boolean) => Promise<void> | void;
  onRemoveLayer?: (layerId: string) => Promise<void> | void;
  onOpenLayerRevision?: (layer: ProjectLayer, path: string) => Promise<void> | void;
  onConnectSource?: (source: SpatialSourceDraft) => Promise<void> | void;
  onLoadSourceLayer?: (source: SpatialSourceSummary, layerName: string) => Promise<void> | void;
  onRefreshCatalog?: () => Promise<void> | void;
  onRefreshFiles?: () => Promise<void> | void;
  onOpenFile?: (file: WorkspaceFileSummary) => Promise<void> | void;
};

const EMPTY_RUNS: LocalGISRunSummary[] = [];
const PAGE_SIZE = 50;
const valueSort = new Intl.Collator('zh-CN', { numeric: true, sensitivity: 'base' });
const STATUS_LABELS: Record<string, string> = { completed: '已完成', failed: '失败', running: '运行中', starting: '正在启动', cancelled: '已取消', ready: '可读取', configured_pending: '待连接', missing_secret:'未配置连接凭据', dependency_missing:'缺少数据库驱动', connection_failed:'连接失败', authentication_failed:'认证失败', permission_denied:'没有读取权限', postgis_missing:'尚未启用 PostGIS', database_missing:'数据库不存在', query_timeout:'查询超时', runtime_unavailable:'运行环境不可用', query_failed:'查询失败', unknown: '尚未检查' };

function displayValue(value: unknown): string {
  if (value === undefined) return '—';
  if (value === null) return 'null';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

function featureId(feature: GeoJSON.Feature): string | number | undefined {
  const id = feature.id ?? feature.properties?.id;
  return typeof id === 'string' || typeof id === 'number' ? id : undefined;
}

function featureLabel(feature: GeoJSON.Feature): string {
  return String(featureId(feature) ?? '未编号');
}

function propertyType(value: unknown): string {
  if (value === null) return '空值';
  if (Array.isArray(value)) return '数组';
  return ({ string: '文本', number: '数字', boolean: '布尔', object: '对象', undefined: '未定义' } as Record<string, string>)[typeof value] ?? '文本';
}

function compareValues(a: unknown, b: unknown): number {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return valueSort.compare(displayValue(a), displayValue(b));
}

function timeLabel(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value || '时间未记录' : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
}

function shortPath(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).at(-1) || path || '未绑定项目';
}

function fileSize(bytes: number | undefined): string {
  if (bytes === undefined || !Number.isFinite(bytes)) return '大小未读取';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

type FileTreeRow = { node: WorkspaceFileNode; depth: number; parentId?: string; position: number; siblings: number };

function fileTreeRows(nodes: WorkspaceFileNode[], expanded?: Set<string>, depth = 0, parentId?: string): FileTreeRow[] {
  return nodes.flatMap((node, index) => [{ node, depth, parentId, position: index + 1, siblings: nodes.length }, ...(!expanded || expanded.has(node.id) ? fileTreeRows(node.children, expanded, depth + 1, node.id) : [])]);
}

function fileIcon(node: WorkspaceFileNode, expanded: boolean) {
  const Icon = node.kind === 'directory' ? expanded ? FolderOpen : Folder
    : node.category === 'symlink' ? Link
      : ['geopackage', 'database'].includes(node.category) ? Database
        : ['vector', 'shapefile'].includes(node.category) ? Layers
          : ['raster', 'image'].includes(node.category) ? FileImage
            : node.category === 'report' ? FileText
              : node.category === 'table' ? Table2
                : node.category === 'archive' ? FileArchive : File;
  return <Icon size={15} aria-hidden="true" className={`earth-files__icon earth-files__icon--${node.category}`} />;
}

export function EarthDataDock({ run, workspaceRoot, projectLayers, spatialSources, workspaceFiles, workspaceFilesIncomplete = false, workspaceFilesNotice, localRuns = EMPTY_RUNS, activeObjectLabel, selectedFeatures, onSaveLayer, onUpdateFeature, onSelectFeature, onCreateBundle, onShowRun, onCompareRuns, onExportLayer, onToggleLayer, onRemoveLayer, onOpenLayerRevision, onConnectSource, onLoadSourceLayer, onRefreshCatalog, onRefreshFiles, onOpenFile }: EarthDataDockProps) {
  const dockId = useId();
  const [tab, setTab] = useState<DockTab>('layers');
  const [collapsed, setCollapsed] = useState(false);
  const [layerName, setLayerName] = useState('候选区域');
  const [selectedLayerId, setSelectedLayerId] = useState('');
  const [exportFormat, setExportFormat] = useState<'shp' | 'gpkg'>('gpkg');
  const [sourceName, setSourceName] = useState('项目数据库');
  const [sourceKind, setSourceKind] = useState<SpatialSourceDraft['kind']>('geopackage');
  const [sourcePath, setSourcePath] = useState('');
  const [sourceSecret, setSourceSecret] = useState('PAW_POSTGIS_URL');
  const [sourceLayers, setSourceLayers] = useState<Record<string, string>>({});
  const [fileFilter, setFileFilter] = useState('');
  const [expandedFiles, setExpandedFiles] = useState<Set<string>>(() => new Set());
  const [selectedFileId, setSelectedFileId] = useState('');
  const [focusedFileId, setFocusedFileId] = useState('');
  const fileRowsRef = useRef(new Map<string, HTMLDivElement>());
  const [attributeFilter, setAttributeFilter] = useState('');
  const [onlySelected, setOnlySelected] = useState(false);
  const [sortColumn, setSortColumn] = useState('__feature_id__');
  const [sortDescending, setSortDescending] = useState(false);
  const [page, setPage] = useState(0);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [comparisonRunId, setComparisonRunId] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [pending, setPending] = useState('');
  const actionLock = useRef(false);
  const [editSession, setEditSession] = useState<EditSession | null>(null);
  const selectedLayer = projectLayers.find(layer => layer.id === selectedLayerId) ?? projectLayers[0];
  const candidateFeature = selectedFeatures.at(-1) as BoundFeature | undefined;
  const candidateSnapshot = candidateFeature ? JSON.stringify(candidateFeature) : '';
  const draftDirty = editSession ? hasEditedProperties(editSession.draft) : false;
  const draftResult = editSession ? materializePropertyDraft(editSession.draft) : null;
  const editLayer = projectLayers.find(layer => layer.id === editSession?.feature.pawLayerId);
  const editingOtherSelection = Boolean(editSession && candidateFeature && (editSession.feature.pawLayerId !== candidateFeature.pawLayerId || featureId(editSession.feature) !== featureId(candidateFeature)));

  useEffect(() => {
    const feature = candidateFeature ? structuredClone(candidateFeature) : undefined;
    setEditSession(current => current && hasEditedProperties(current.draft) ? current : feature ? { feature, draft: createPropertyDraft(feature.properties) } : null);
    if (feature?.pawLayerId) setSelectedLayerId(feature.pawLayerId);
  }, [candidateSnapshot, draftDirty]);

  const selectedInLayer = useMemo(() => selectedLayer ? selectedLayerFeatures(selectedLayer, selectedFeatures) : [], [selectedLayer, selectedFeatures]);
  const selectedSet = useMemo(() => new Set(selectedInLayer), [selectedInLayer]);
  const columns = useMemo(() => [...new Set((selectedLayer?.features ?? []).flatMap(feature => Object.keys(feature.properties ?? {})))], [selectedLayer]);
  const effectiveSortColumn = columns.includes(sortColumn) ? sortColumn : '__feature_id__';
  const filteredFeatures = useMemo(() => {
    const needle = attributeFilter.trim().toLocaleLowerCase();
    return (selectedLayer?.features ?? [])
      .filter(feature => (!onlySelected || selectedSet.has(feature)) && (!needle || `${featureLabel(feature)} ${JSON.stringify(feature.properties ?? {})}`.toLocaleLowerCase().includes(needle)))
      .map((feature, index) => ({ feature, index }))
      .sort((a, b) => {
        const av = effectiveSortColumn === '__feature_id__' ? featureId(a.feature) : a.feature.properties?.[effectiveSortColumn];
        const bv = effectiveSortColumn === '__feature_id__' ? featureId(b.feature) : b.feature.properties?.[effectiveSortColumn];
        return compareValues(av, bv) * (sortDescending ? -1 : 1) || a.index - b.index;
      }).map(row => row.feature);
  }, [selectedLayer, selectedSet, attributeFilter, onlySelected, effectiveSortColumn, sortDescending]);
  useEffect(() => { setPage(0); }, [selectedLayer?.id, attributeFilter, onlySelected, effectiveSortColumn, sortDescending]);
  const currentPage = Math.min(page, Math.max(0, Math.ceil(filteredFeatures.length / PAGE_SIZE) - 1));
  const visibleFeatures = filteredFeatures.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE);
  const allFiles = useMemo(() => {
    const artifactFiles = (run?.artifacts ?? []).map(artifact => ({ path: artifact.path.startsWith('/') ? artifact.path : `${workspaceRoot}/${artifact.path}`, name: artifact.path.split('/').pop() || artifact.path, kind: 'file' as const, byteSize: artifact.bytes }));
    const files = new Map<string, WorkspaceFileSummary>();
    for (const file of [...artifactFiles, ...workspaceFiles]) files.set(file.path, { ...files.get(file.path), ...file, byteSize: file.byteSize ?? files.get(file.path)?.byteSize });
    return [...files.values()];
  }, [run?.artifacts, workspaceFiles, workspaceRoot]);
  const fileTree = useMemo(() => buildWorkspaceFileTree(allFiles, workspaceRoot), [allFiles, workspaceRoot]);
  const allFileNodes = useMemo(() => fileTreeRows(fileTree).map(row => row.node), [fileTree]);
  const filteredFileTree = useMemo(() => filterWorkspaceFileTree(fileTree, fileFilter), [fileTree, fileFilter]);
  const visibleFileRows = useMemo(() => fileTreeRows(filteredFileTree, expandedFiles), [filteredFileTree, expandedFiles]);
  const selectedFile = allFileNodes.find(node => node.id === selectedFileId);
  const focusableFileId = visibleFileRows.some(row => row.node.id === focusedFileId) ? focusedFileId : visibleFileRows[0]?.node.id;
  useEffect(() => { setExpandedFiles(new Set()); setSelectedFileId(''); setFocusedFileId(''); setFileFilter(''); }, [workspaceRoot]);
  useEffect(() => {
    if (fileFilter.trim()) setExpandedFiles(current => new Set([...current, ...fileTreeRows(filteredFileTree).filter(row => row.node.kind === 'directory' || row.node.kind === 'shapefile').map(row => row.node.id)]));
  }, [fileFilter, filteredFileTree]);
  const runLayers = run?.layers.filter(layer => layer.tileUrl) ?? [];
  const orderedRuns = useMemo(() => [...localRuns].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)), [localRuns]);
  const selectedRun = orderedRuns.find(item => item.runId === selectedRunId) ?? orderedRuns[0];
  const completedRuns = orderedRuns.filter(item => item.status === 'completed');
  const comparisonRun = completedRuns.find(item => item.runId === comparisonRunId && item.runId !== selectedRun?.runId);

  async function perform(label: string, action: () => Promise<void> | void, success?: string) {
    if (actionLock.current) return false;
    actionLock.current = true;
    setPending(label); setError(''); setNotice('');
    try { await action(); if (success) setNotice(success); return true; }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); return false; }
    finally { actionLock.current = false; setPending(''); }
  }

  function toggleFileBranch(node: WorkspaceFileNode) {
    setExpandedFiles(current => { const next = new Set(current); if (next.has(node.id)) next.delete(node.id); else next.add(node.id); return next; });
  }

  function fileOpenTarget(node: WorkspaceFileNode): WorkspaceFileSummary | undefined {
    if (node.kind === 'directory' || node.kind === 'symlink') return undefined;
    if (node.partOfShapefile) return allFileNodes.find(candidate => candidate.kind === 'shapefile' && candidate.path === node.partOfShapefile)?.entry;
    if (node.category === 'sidecar') return undefined;
    return node.entry;
  }

  function openFileNode(node: WorkspaceFileNode) {
    const target = fileOpenTarget(node);
    if (target && onOpenFile) void perform('打开文件', () => onOpenFile(target));
  }

  function focusFileRow(id?: string) {
    if (!id) return;
    setFocusedFileId(id); fileRowsRef.current.get(id)?.focus();
  }

  function fileTreeKeyDown(event: KeyboardEvent<HTMLDivElement>, row: FileTreeRow, index: number) {
    if (event.target !== event.currentTarget) return;
    const { node, parentId } = row;
    const branch = node.kind === 'directory' || node.kind === 'shapefile';
    if (!['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'Enter', ' '].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'ArrowDown') focusFileRow(visibleFileRows[index + 1]?.node.id);
    else if (event.key === 'ArrowUp') focusFileRow(visibleFileRows[index - 1]?.node.id);
    else if (event.key === 'Home') focusFileRow(visibleFileRows[0]?.node.id);
    else if (event.key === 'End') focusFileRow(visibleFileRows.at(-1)?.node.id);
    else if (event.key === 'ArrowRight' && branch) { if (!expandedFiles.has(node.id)) toggleFileBranch(node); else focusFileRow(node.children[0]?.id); }
    else if (event.key === 'ArrowLeft') { if (branch && expandedFiles.has(node.id)) toggleFileBranch(node); else focusFileRow(parentId); }
    else if (event.key === 'Enter') { setSelectedFileId(node.id); if (node.kind === 'directory') toggleFileBranch(node); else openFileNode(node); }
    else if (event.key === ' ') setSelectedFileId(node.id);
  }

  function shapefileMissingLabel(node: WorkspaceFileNode): string {
    const missing = node.shapefile?.missingRequired.map(extension => `.${extension}`).join('、');
    return missing ? workspaceFilesIncomplete ? `未找到 ${missing}（目录尚未完整读取）` : `缺少必要配套 ${missing}` : '';
  }

  const selectedFileTarget = selectedFile ? fileOpenTarget(selectedFile) : undefined;
  const selectedFileBytes = selectedFile ? workspaceNodeByteSize(selectedFile) : undefined;

  async function saveProperties() {
    if (!editSession || !onUpdateFeature || !draftDirty) return;
    const result = materializePropertyDraft(editSession.draft);
    if (!result.ok) { setError(result.error); return; }
    const feature = { ...editSession.feature, properties: result.properties };
    if (await perform('保存属性', () => onUpdateFeature(feature), '属性已保存。')) setEditSession({ feature, draft: createPropertyDraft(result.properties) });
  }

  function cancelProperties() {
    setEditSession(current => candidateFeature ? { feature: structuredClone(candidateFeature), draft: createPropertyDraft(candidateFeature.properties) } : current ? { ...current, draft: discardPropertyDraft(current.draft) } : null);
    setError(''); setNotice('已取消属性修改。');
  }

  function connectSource() {
    if (!onConnectSource) return;
    const name = sourceName.trim();
    const source: SpatialSourceDraft = sourceKind === 'postgis' ? { name, kind: sourceKind, secretReference: sourceSecret.trim() } : { name, kind: sourceKind, path: sourcePath.trim() };
    if (!name) { setError('请填写数据源名称。'); return; }
    if (sourceKind === 'postgis' && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(source.secretReference ?? '')) { setError('请填写 PostGIS 连接所用的环境变量名，例如 PAW_POSTGIS_URL。'); return; }
    if (sourceKind !== 'postgis' && !source.path) { setError('请填写工作区中的数据库路径，例如 data/roads.gpkg。'); return; }
    void perform('连接数据库', () => onConnectSource(source));
  }

  const tabs: Array<{ id: DockTab; label: string; count?: number }> = [
    { id: 'layers', label: '图层', count: projectLayers.length + runLayers.length },
    { id: 'attributes', label: '属性表' }, { id: 'runs', label: '运行', count: localRuns.length },
    { id: 'files', label: '文件' }, { id: 'databases', label: '数据库' },
  ];

  const exportControls = selectedLayer ? <section className="earth-data-dock__export-panel" aria-label="导出当前图层">
    <div className="earth-data-dock__section-head"><strong>导出 {selectedLayer.name}</strong><span>v{selectedLayer.revision ?? 1}</span></div>
    <label className="earth-data-dock__inline-field">格式<select aria-label="导出格式" value={exportFormat} onChange={event => setExportFormat(event.target.value as 'shp' | 'gpkg')}><option value="gpkg">GeoPackage · GPKG</option><option value="shp">Shapefile · ZIP</option></select></label>
    <div className="earth-data-dock__split-actions">
      <button type="button" disabled={Boolean(pending) || !onExportLayer || !selectedLayer.featureCount} onClick={() => void perform('导出全部', () => onExportLayer!(exportFormat, selectedLayer, 'all'))}>导出全部 · {selectedLayer.featureCount}</button>
      <button type="button" disabled={Boolean(pending) || !onExportLayer || !selectedInLayer.length} onClick={() => void perform('导出所选', () => onExportLayer!(exportFormat, selectedLayer, 'selected'))}>导出所选 · {selectedInLayer.length}</button>
    </div>
    <small>所选范围仅包含这个图层中已选中的要素。</small>
  </section> : null;

  const attributeEditor = editSession && onUpdateFeature ? <section className="earth-data-dock__attribute-editor" aria-label="属性编辑">
    <div className="earth-data-dock__section-head"><strong title={`要素 ID：${featureLabel(editSession.feature)}`}>{typeof editSession.feature.properties?.name === 'string' && editSession.feature.properties.name.trim() ? editSession.feature.properties.name : `要素 ${featureLabel(editSession.feature)}`}</strong><span className={draftDirty ? 'earth-data-dock__unsaved' : undefined}>{draftDirty ? '未保存' : '未修改'}</span></div>
    <p className="earth-data-dock__context-line">{editLayer?.name ?? '地图对象'}{editSession.feature.pawRevision !== undefined ? ` · 基于 v${editSession.feature.pawRevision}` : ''} · {editSession.feature.geometry?.type}</p>
    {editingOtherSelection && draftDirty ? <p className="earth-data-dock__draft-notice">保留了先前对象的草稿。保存或取消后，再编辑当前选择。</p> : null}
    <div className="earth-data-dock__property-fields">{Object.entries(editSession.draft).map(([key, entry]) => <label key={key}>
      <span title={key}>{key}<small>{propertyType(entry.original)}{entry.edited ? ' · 已修改' : ''}</small></span>
      {typeof entry.original === 'boolean' ? <select aria-label={`属性 ${key}`} value={entry.value} disabled={Boolean(pending)} onChange={event => setEditSession(current => current ? { ...current, draft: updatePropertyDraft(current.draft, key, event.target.value) } : null)}><option value="true">true</option><option value="false">false</option></select>
        : typeof entry.original === 'object' && entry.original !== null ? <textarea aria-label={`属性 ${key}`} rows={2} value={entry.value} disabled={Boolean(pending)} onChange={event => setEditSession(current => current ? { ...current, draft: updatePropertyDraft(current.draft, key, event.target.value) } : null)} />
          : <input aria-label={`属性 ${key}`} inputMode={typeof entry.original === 'number' ? 'decimal' : undefined} value={entry.value} disabled={Boolean(pending)} onChange={event => setEditSession(current => current ? { ...current, draft: updatePropertyDraft(current.draft, key, event.target.value) } : null)} />}
    </label>)}</div>
    {!Object.keys(editSession.draft).length ? <p className="earth-data-dock__empty">这个要素没有可编辑的属性字段。</p> : null}
    {draftResult && !draftResult.ok ? <p className="earth-data-dock__validation" role="alert">{draftResult.error}</p> : null}
    <div className="earth-data-dock__edit-actions"><button type="button" className="earth-data-dock__primary" disabled={Boolean(pending) || !draftDirty || !draftResult?.ok} onClick={() => void saveProperties()}>{pending === '保存属性' ? '正在保存…' : '保存属性版本'}</button><button type="button" disabled={Boolean(pending) || !draftDirty} onClick={cancelProperties}>取消修改</button></div>
  </section> : null;

  return <aside className={`earth-data-dock${tab === 'attributes' ? ' earth-data-dock--table' : ''}${collapsed ? ' earth-data-dock--collapsed' : ''}`} aria-label="GIS 数据工作区" aria-busy={Boolean(pending)}>
    <header className="earth-data-dock__header">
      <div><strong>GIS 数据工作区</strong><small title={activeObjectLabel || selectedLayer?.name}>{draftDirty ? '有未保存的属性修改' : activeObjectLabel ? `Agent · ${shortPath(activeObjectLabel)}` : selectedLayer ? `${selectedLayer.name} · v${selectedLayer.revision ?? 1}` : '图层、属性与运行记录'}</small></div>
      {!collapsed ? <button type="button" className="earth-data-dock__icon-button" aria-label="刷新 GIS 数据" title="刷新图层、文件和数据库目录" disabled={Boolean(pending) || (!onRefreshCatalog && !onRefreshFiles)} onClick={() => void perform('刷新数据', async () => { await Promise.all([onRefreshCatalog?.(), onRefreshFiles?.()]); })}><RefreshCw size={15} className={pending === '刷新数据' ? 'is-refreshing' : undefined} /></button> : null}
      <button type="button" className="earth-data-dock__icon-button" aria-label={collapsed ? '展开 GIS 数据工作区' : '收起 GIS 数据工作区'} aria-expanded={!collapsed} onClick={() => setCollapsed(value => !value)}>{collapsed ? <PanelRightOpen size={16} /> : <PanelRightClose size={16} />}</button>
    </header>
    <div className="earth-data-dock__body" hidden={collapsed}>
      <details className="earth-data-dock__binding"><summary><FolderOpen size={14} aria-hidden="true" /><strong title={workspaceRoot}>{shortPath(workspaceRoot)}</strong><span>{selectedFeatures.length} 已选</span><ChevronDown size={13} aria-hidden="true" /></summary><div className="earth-data-dock__binding-details"><span>项目工作区</span><code>{workspaceRoot || '尚未绑定工作区'}</code><small>{projectLayers.length} 个项目图层 · 地图已选 {selectedFeatures.length} 个要素</small>{activeObjectLabel ? <p>Agent 当前操作：{activeObjectLabel}</p> : null}</div></details>
      <nav className="earth-data-dock__tabs" role="tablist" aria-label="GIS 数据视角">{tabs.map(item => <button type="button" key={item.id} id={`${dockId}-${item.id}`} role="tab" aria-controls={`${dockId}-panel-${item.id}`} aria-selected={tab === item.id} tabIndex={tab === item.id ? 0 : -1} onClick={() => setTab(item.id)} onKeyDown={event => {
        if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const index = tabs.findIndex(candidate => candidate.id === item.id);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
        setTab(tabs[next].id); document.getElementById(`${dockId}-${tabs[next].id}`)?.focus();
      }}>{item.label}{item.count ? <small>{item.count}</small> : null}</button>)}</nav>

      {tab === 'layers' ? <section className="earth-data-dock__content" id={`${dockId}-panel-layers`} role="tabpanel" aria-label="图层管理">
        <div className="earth-data-dock__section-head"><strong>项目图层</strong><span>{projectLayers.length} 个</span></div>
        <div className="earth-data-dock__layer-list">{projectLayers.map(layer => <div className={`earth-data-dock__layer ${selectedLayer?.id === layer.id ? 'is-active' : ''}`} key={layer.id}>
          <input type="checkbox" aria-label={`${layer.name} 可见`} checked={layer.visible !== false} disabled={Boolean(pending) || !onToggleLayer} onChange={event => void perform('更新图层可见性', () => onToggleLayer?.(layer.id, event.target.checked))} />
          <Layers size={14} aria-hidden="true" className="earth-data-dock__row-icon" /><button type="button" className="earth-data-dock__layer-select" title={layer.name} aria-pressed={selectedLayer?.id === layer.id} onClick={() => setSelectedLayerId(layer.id)}><strong>{layer.name}</strong><span title={layer.geometryTypes.join(' / ')}>{layer.featureCount} 要素 · {layer.geometryTypes.join(' / ') || '未知几何'} · v{layer.revision ?? 1}</span></button>{selectedLayer?.id === layer.id ? <span className="earth-data-dock__current-label">当前</span> : null}
        </div>)}</div>
        {!projectLayers.length ? <p className="earth-data-dock__empty">在地图上绘制或导入要素，再保存为项目图层。也可以从数据库加载子图层。</p> : null}
        {selectedLayer ? <div className="earth-data-dock__layer-detail"><div><span>{selectedLayer.crs} · v{selectedLayer.revision ?? 1}</span><button type="button" onClick={() => setTab('attributes')}>打开属性表</button></div>
          <details className="earth-data-dock__history"><summary>版本与图层管理<ChevronDown size={13} /></summary><dl className="earth-data-dock__metadata"><div><dt>图层 ID</dt><dd><code>{selectedLayer.id}</code></dd></div>{typeof selectedLayer.source?.path === 'string' ? <div><dt>源文件</dt><dd><code>{selectedLayer.source.path}</code></dd></div> : null}<div><dt>项目副本</dt><dd><code>{selectedLayer.path}</code></dd></div><div><dt>更新时间</dt><dd>{timeLabel(selectedLayer.updatedAt)}</dd></div></dl><div className="earth-data-dock__history-list">{[...(selectedLayer.history ?? [])].reverse().map(path => <button type="button" key={path} disabled={Boolean(pending) || !onOpenLayerRevision} onClick={() => void perform('打开图层版本', () => onOpenLayerRevision?.(selectedLayer, path))}><span>{path.split('/').at(-1) ?? path}</span><small>查看快照</small></button>)}{!selectedLayer.history?.length ? <p className="earth-data-dock__empty">尚无保存的历史快照。</p> : null}</div><button type="button" className="earth-data-dock__remove" disabled={Boolean(pending) || !onRemoveLayer} onClick={() => void perform('移除图层', () => onRemoveLayer?.(selectedLayer.id))}>从目录移除 {selectedLayer.name}</button></details>
        </div> : null}
        <details className="earth-data-dock__save-layer" open={!projectLayers.length}><summary>将地图所选保存为新图层 <span>{selectedFeatures.length} 要素</span></summary><div className="earth-data-dock__layer-actions"><input aria-label="新图层名称" value={layerName} onChange={event => setLayerName(event.target.value)} placeholder="图层名称" /><button type="button" disabled={Boolean(pending) || !onSaveLayer || !selectedFeatures.length || !layerName.trim()} onClick={() => void perform('保存图层', () => onSaveLayer?.(layerName.trim(), selectedFeatures))}>保存图层</button></div></details>
        {selectedFeatures.length ? <button type="button" className="earth-data-dock__selection-link" onClick={() => setTab('attributes')}><span>地图已选 {selectedFeatures.length} 个要素</span><strong>{draftDirty ? '继续编辑草稿' : '查看属性与编辑'}</strong></button> : null}
        {attributeEditor && !selectedLayer ? attributeEditor : null}
        {exportControls}
        {runLayers.length ? <section className="earth-data-dock__cloud-layers"><div className="earth-data-dock__section-head"><strong>Earth Engine 结果</strong><span>云端图层</span></div>{runLayers.map(layer => <div className="earth-data-dock__run-layer" key={layer.id}><strong>{layer.name}</strong><small>{layer.status || '图层已返回'}</small></div>)}</section> : null}
      </section> : null}

      {tab === 'attributes' ? <section className="earth-data-dock__content" id={`${dockId}-panel-attributes`} role="tabpanel" aria-label="图层属性表">
        <label className="earth-data-dock__inline-field">当前图层<select aria-label="属性表图层" value={selectedLayer?.id ?? ''} onChange={event => setSelectedLayerId(event.target.value)}>{!projectLayers.length ? <option value="">尚无项目图层</option> : null}{projectLayers.map(layer => <option key={layer.id} value={layer.id}>{layer.name} · v{layer.revision ?? 1}</option>)}</select></label>
        {selectedLayer ? <><div className="earth-data-dock__table-summary"><strong>{selectedLayer.featureCount} 个要素</strong><span>已载入 {selectedLayer.features.length}</span><span>已选 {selectedInLayer.length}</span><span>筛选显示 {filteredFeatures.length}</span></div>
          <div className="earth-data-dock__table-tools"><label className="earth-data-dock__search"><Search size={14} /><input aria-label="筛选属性表" value={attributeFilter} onChange={event => setAttributeFilter(event.target.value)} placeholder="搜索 ID 或字段内容" /></label><label className="earth-data-dock__checkbox"><input type="checkbox" checked={onlySelected} onChange={event => setOnlySelected(event.target.checked)} />仅看所选</label></div>
          <div className="earth-data-dock__sort"><span>排序</span><select aria-label="属性表排序字段" value={effectiveSortColumn} onChange={event => setSortColumn(event.target.value)}><option value="__feature_id__">要素 ID</option>{columns.map(key => <option value={key} key={key}>{key}</option>)}</select><button type="button" aria-label={sortDescending ? '改为升序' : '改为降序'} onClick={() => setSortDescending(value => !value)}>{sortDescending ? <ArrowDown size={13} /> : <ArrowUp size={13} />}{sortDescending ? '降序' : '升序'}</button></div>
          <div className="earth-data-dock__table-scroll" tabIndex={0} aria-label="属性表，可横向滚动"><table><thead><tr><th scope="col">选择</th><th scope="col">要素 ID</th>{columns.map(key => <th scope="col" key={key}>{key}</th>)}</tr></thead><tbody>{visibleFeatures.map((feature, index) => <tr key={`${typeof featureId(feature)}:${featureLabel(feature)}:${index}`} aria-selected={selectedSet.has(feature)}><td><input type="checkbox" aria-label={`选择要素 ${featureLabel(feature)}`} title={`${typeof featureId(feature) === 'number' ? '数字' : '文本'} ID`} checked={selectedSet.has(feature)} disabled={Boolean(pending) || !onSelectFeature} onChange={() => void perform('选择要素', () => onSelectFeature?.(feature, selectedLayer.id))} /></td><th scope="row"><button type="button" title={`${typeof featureId(feature) === 'number' ? '数字' : '文本'} ID：${featureLabel(feature)}`} disabled={Boolean(pending) || !onSelectFeature} onClick={() => void perform('选择要素', () => onSelectFeature?.(feature, selectedLayer.id))}>{featureLabel(feature)}</button></th>{columns.map(key => <td key={key} title={displayValue(feature.properties?.[key])} className={typeof feature.properties?.[key] === 'number' ? 'is-number' : undefined}>{displayValue(feature.properties?.[key])}</td>)}</tr>)}</tbody></table></div>
          {!filteredFeatures.length ? <p className="earth-data-dock__empty">{selectedLayer.features.length ? '没有匹配的要素。调整筛选或关闭“仅看所选”。' : '这个图层尚未载入要素，刷新数据后再查看。'}</p> : null}
          {filteredFeatures.length > PAGE_SIZE ? <div className="earth-data-dock__pagination"><button type="button" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>上一页</button><span>{currentPage + 1} / {Math.ceil(filteredFeatures.length / PAGE_SIZE)}</span><button type="button" disabled={(currentPage + 1) * PAGE_SIZE >= filteredFeatures.length} onClick={() => setPage(currentPage + 1)}>下一页</button></div> : null}
        </> : <p className="earth-data-dock__empty">保存一个项目图层后，可以在这里筛选、排序，并联动地图选择。</p>}
        {attributeEditor ?? <p className="earth-data-dock__empty">在地图或属性表中选择一个要素，查看和编辑它的字段。</p>}
        {exportControls}
      </section> : null}

      {tab === 'runs' ? <section className="earth-data-dock__content" id={`${dockId}-panel-runs`} role="tabpanel" aria-label="运行与交付">
        <div className="earth-data-dock__section-head"><strong>本地 GIS 运行</strong><span>{localRuns.length} 次</span></div>
        <p className="earth-data-dock__context-line">选择运行查看结果，比较两个已完成的方案，再生成成果包。</p>
        <div className="earth-data-dock__run-list">{orderedRuns.map(item => <button type="button" key={item.runId} aria-pressed={selectedRun?.runId === item.runId} onClick={() => setSelectedRunId(item.runId)}><strong>{item.op || 'GIS 分析'}</strong><span className={`earth-data-dock__run-status earth-data-dock__run-status--${item.status}`}>{STATUS_LABELS[item.status] ?? item.status}</span><code>{item.runId}</code><small>{timeLabel(item.updatedAt)}</small></button>)}</div>
        {!localRuns.length ? <p className="earth-data-dock__empty">还没有本地运行。完成一次 GIS 分析后，这里会保留输入、参数与结果。</p> : null}
        {selectedRun ? <section className="earth-data-dock__run-detail" aria-label="所选本地运行"><div className="earth-data-dock__section-head"><strong>{selectedRun.op || 'GIS 分析'}</strong><span>{STATUS_LABELS[selectedRun.status] ?? selectedRun.status}</span></div><code>{selectedRun.runId}</code>{Object.keys(selectedRun.params ?? {}).length ? <dl>{Object.entries(selectedRun.params ?? {}).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{displayValue(value)}</dd></div>)}</dl> : <p className="earth-data-dock__empty">这次运行没有额外参数。</p>}
          <div className="earth-data-dock__split-actions"><button type="button" disabled={Boolean(pending) || !onShowRun} onClick={() => void perform('查看运行', () => onShowRun?.(selectedRun.runId))}>查看运行结果</button><button type="button" className="earth-data-dock__primary" disabled={Boolean(pending) || selectedRun.status !== 'completed' || !onCreateBundle} onClick={() => void perform('生成成果包', () => onCreateBundle?.(selectedRun.runId))}>{pending === '生成成果包' ? '正在打包…' : '生成成果包'}</button></div>
          {selectedRun.status === 'completed' ? <div className="earth-data-dock__comparison"><label>对照方案<select aria-label="对照本地运行" value={comparisonRun?.runId ?? ''} onChange={event => setComparisonRunId(event.target.value)}><option value="">选择另一个已完成的运行</option>{completedRuns.filter(item => item.runId !== selectedRun.runId).map(item => <option value={item.runId} key={item.runId}>{item.op} · {item.runId}</option>)}</select></label><button type="button" disabled={Boolean(pending) || !comparisonRun || !onCompareRuns} onClick={() => comparisonRun && void perform('比较方案', () => onCompareRuns?.(selectedRun.runId, comparisonRun.runId))}>比较两个方案</button></div> : <p className="earth-data-dock__context-line">运行完成后可比较方案和生成成果包。</p>}
        </section> : null}
        <section className="earth-data-dock__cloud-summary" aria-label="Earth Engine 云端运行"><div className="earth-data-dock__section-head"><strong>Earth Engine</strong><span>云端运行</span></div>{run ? <><p><strong>{STATUS_LABELS[run.status] ?? run.status}</strong> · {run.layers.length} 图层 · {run.tasks?.length ?? 0} 导出任务</p><code>{run.runId}</code><small>云端导出任务与本地 GIS 成果包分别记录。</small></> : <p className="earth-data-dock__empty">尚无 Earth Engine 运行。</p>}</section>
      </section> : null}

      {tab === 'files' ? <section className="earth-data-dock__content earth-files" id={`${dockId}-panel-files`} role="tabpanel" aria-label="工作区文件管理">
        <div className="earth-files__toolbar">
          <label className="earth-data-dock__search"><Search size={14} aria-hidden="true" /><input aria-label="筛选 GIS 文件" value={fileFilter} onChange={event => setFileFilter(event.target.value)} placeholder="查找文件、目录或类型" /></label>
          <button type="button" className="earth-files__refresh" aria-label="刷新文件列表" title="重新读取当前项目目录" disabled={Boolean(pending) || !onRefreshFiles} onClick={() => void perform('刷新文件', () => onRefreshFiles?.())}><RefreshCw size={14} aria-hidden="true" /></button>
        </div>
        {workspaceFilesNotice || workspaceFilesIncomplete ? <details className="earth-files__listing-notice"><summary><span role="status">目录未全部载入 · 查看说明</span></summary><p>{workspaceFilesNotice || '目录尚未完整读取；当前仅显示已读取的文件，可刷新重试。'}</p></details> : null}
        <div className="earth-files__columns" aria-hidden="true"><span>名称</span><span>类型</span><span>大小</span></div>
        <div className="earth-files__tree" role="tree" aria-label="项目文件目录">
          {visibleFileRows.map((row, index) => {
            const { node, depth } = row;
            const branch = node.kind === 'directory' || node.kind === 'shapefile';
            const expanded = expandedFiles.has(node.id);
            const missingLabel = shapefileMissingLabel(node);
            const unknownCrs = Boolean(node.shapefile && !node.shapefile.hasProjection);
            const bytes = workspaceNodeByteSize(node);
            return <div key={node.id} ref={element => { if (element) fileRowsRef.current.set(node.id, element); else fileRowsRef.current.delete(node.id); }} className="earth-files__row" role="treeitem" aria-label={node.name} aria-level={depth + 1} aria-posinset={row.position} aria-setsize={row.siblings} aria-selected={selectedFileId === node.id} aria-expanded={branch ? expanded : undefined} tabIndex={focusableFileId === node.id ? 0 : -1} title={node.path} style={{ paddingInlineStart: 5 + depth * 14 }} onFocus={() => setFocusedFileId(node.id)} onClick={() => { setSelectedFileId(node.id); setFocusedFileId(node.id); }} onDoubleClick={() => { if (node.kind === 'directory') toggleFileBranch(node); else openFileNode(node); }} onKeyDown={event => fileTreeKeyDown(event, row, index)}>
              {branch ? <button type="button" className="earth-files__disclosure" tabIndex={-1} aria-label={`${expanded ? '收起' : '展开'}${node.kind === 'directory' ? '文件夹' : '配套'} ${node.name}`} onClick={event => { event.stopPropagation(); setSelectedFileId(node.id); toggleFileBranch(node); focusFileRow(node.id); }} onDoubleClick={event => event.stopPropagation()}>{expanded ? <ChevronDown size={13} aria-hidden="true" /> : <ChevronRight size={13} aria-hidden="true" />}</button> : <span className="earth-files__disclosure-space" aria-hidden="true" />}
              {fileIcon(node, expanded)}
              <span className="earth-files__name"><span>{node.name}</span>{missingLabel || unknownCrs ? <small className="earth-files__warning"><TriangleAlert size={11} aria-hidden="true" />{missingLabel ? <span title={missingLabel}>{workspaceFilesIncomplete ? '未找到 ' : '缺 '}{node.shapefile!.missingRequired.map(extension => `.${extension}`).join('、')}</span> : null}{unknownCrs ? <span title={workspaceFilesIncomplete ? '未找到 .prj（目录尚未完整读取）' : '缺少 .prj，CRS 未知'}>CRS 未知</span> : null}</small> : null}</span>
              <span className="earth-files__type">{node.kind === 'shapefile' ? `SHP · ${node.children.length}` : WORKSPACE_FILE_LABELS[node.category]}</span>
              <span className="earth-files__size" title={bytes === undefined ? '大小未读取' : `${bytes.toLocaleString()} bytes`}>{node.kind === 'directory' ? '' : bytes === undefined ? '—' : fileSize(bytes)}</span>
            </div>;
          })}
        </div>
        {!visibleFileRows.length ? <p className="earth-data-dock__empty earth-files__empty">{fileFilter ? '没有匹配的文件或目录。试试更短的名称或清空搜索。' : workspaceFilesIncomplete ? '暂未读到文件，请刷新重试。' : '当前目录列表没有文件。导入数据或生成成果后可刷新。'}</p> : null}
        {selectedFile ? <section className="earth-files__details" aria-label="所选文件详情">
          <div className="earth-files__detail-heading"><strong title={selectedFile.name}>{selectedFile.name}</strong>{selectedFileTarget ? <button type="button" aria-label={`打开 ${selectedFileTarget.name}`} disabled={Boolean(pending) || !onOpenFile} onClick={() => openFileNode(selectedFile)}>{selectedFile.partOfShapefile && selectedFile.path !== selectedFile.partOfShapefile ? '打开所属 SHP' : '打开'}</button> : selectedFile.kind === 'directory' ? <button type="button" onClick={() => toggleFileBranch(selectedFile)}>{expandedFiles.has(selectedFile.id) ? '收起文件夹' : '展开文件夹'}</button> : null}</div>
          <dl><div><dt>完整路径</dt><dd><code>{selectedFile.path}</code></dd></div><div><dt>类型</dt><dd>{WORKSPACE_FILE_LABELS[selectedFile.category]}</dd></div>{selectedFileBytes !== undefined ? <div><dt>{selectedFile.kind === 'shapefile' ? '配套合计' : '大小'}</dt><dd>{fileSize(selectedFileBytes)} · {selectedFileBytes.toLocaleString()} bytes</dd></div> : null}
            {selectedFile.kind === 'directory' ? <div><dt>目录内容</dt><dd>{selectedFile.children.length ? `当前列表有 ${selectedFile.children.length} 个直接子项` : '当前列表未列出子项'}</dd></div> : null}
            {selectedFile.shapefile ? <><div><dt>必要配套</dt><dd className={shapefileMissingLabel(selectedFile) ? 'earth-files__warning' : undefined}>{shapefileMissingLabel(selectedFile) || '已列出 .shp、.shx、.dbf'}</dd></div><div><dt>坐标系</dt><dd>{selectedFile.shapefile.hasProjection ? '含 .prj，坐标系需读取后确认' : workspaceFilesIncomplete ? 'CRS 未知；未找到 .prj（目录尚未完整读取）' : 'CRS 未知（缺少 .prj）'}</dd></div><div><dt>配套文件</dt><dd>{selectedFile.children.map(node => node.name).join('、')}</dd></div></> : null}
          </dl>
          {selectedFile.kind === 'shapefile' && selectedFileBytes === undefined ? <p>配套文件大小尚未全部读取。</p> : null}
          {selectedFile.kind === 'symlink' ? <p>符号链接保留在列表中，不跟随链接打开。</p> : selectedFile.category === 'sidecar' && !selectedFile.partOfShapefile ? <p>当前列表未找到同名 .shp；配套文件需随主文件读取。</p> : selectedFile.partOfShapefile && selectedFile.path !== selectedFile.partOfShapefile ? <p>这是 Shapefile 配套文件，打开时使用对应的 .shp 主文件。</p> : null}
        </section> : <p className="earth-files__hint">选择查看路径；双击打开文件。SHP 配套随主文件展开。</p>}
      </section> : null}

      {tab === 'databases' ? <section className="earth-data-dock__content" id={`${dockId}-panel-databases`} role="tabpanel" aria-label="空间数据库">
        <div className="earth-data-dock__section-head"><strong>空间数据库</strong><span>{spatialSources.length} 个数据源</span></div>
        {spatialSources.map(source => {
          const sourceLayer = source.layers.includes(sourceLayers[source.id]) ? sourceLayers[source.id] : source.layers[0] ?? '';
          return <div className="earth-data-dock__database" key={source.id}><div><Database size={15} aria-hidden="true" className="earth-data-dock__row-icon" /><strong title={source.name}>{source.name}</strong><span className={`earth-source-status earth-source-status--${source.status}`}>{STATUS_LABELS[source.status] ?? source.status}</span></div><small>{source.kind} · {source.layers.length} 个子图层</small>{source.error?<p role="alert">{source.error}</p>:null}{source.kind==='postgis'?<button disabled={Boolean(pending)||!onConnectSource} onClick={()=>void perform('重新连接',()=>onConnectSource?.({name:source.name,kind:source.kind,secretReference:source.secretReference,schema:source.schema,table:source.table,readOnly:true}))}>重新连接</button>:null}{source.layers.length ? <div className="earth-data-dock__source-load"><label>子图层<select aria-label={`${source.name} 子图层`} value={sourceLayer} onChange={event => setSourceLayers(current => ({ ...current, [source.id]: event.target.value }))}>{source.layers.map(name => <option value={name} key={name}>{name}</option>)}</select></label><button type="button" disabled={Boolean(pending) || !onLoadSourceLayer || !sourceLayer || source.status !== 'ready'} onClick={() => void perform('载入子图层', () => onLoadSourceLayer?.(source, sourceLayer))}>加载到地图</button></div> : <p className="earth-data-dock__empty">{source.status === 'ready' ? '这个数据源没有返回可用子图层。' : '连接可用后，刷新数据以读取子图层。'}</p>}<details className="earth-data-dock__resource-details"><summary>连接详情<ChevronDown size={12} aria-hidden="true" /></summary><dl className="earth-data-dock__metadata"><div><dt>位置</dt><dd><code>{source.path || source.schema || '通过环境变量连接'}</code></dd></div>{source.table ? <div><dt>数据表</dt><dd>{source.table}</dd></div> : null}<div><dt>更新时间</dt><dd>{timeLabel(source.updatedAt)}</dd></div></dl></details></div>;
        })}
        {!spatialSources.length ? <p className="earth-data-dock__empty">还没有登记空间数据库。连接后先选择具体子图层，再加载到地图。</p> : null}
        <details className="earth-data-dock__connect" open={!spatialSources.length}><summary>连接数据源<ChevronDown size={13} /></summary><div className="earth-data-dock__db-form"><label>名称<input aria-label="空间数据源名称" value={sourceName} onChange={event => setSourceName(event.target.value)} /></label><label>类型<select aria-label="空间数据源类型" value={sourceKind} onChange={event => setSourceKind(event.target.value as SpatialSourceDraft['kind'])}><option value="geopackage">GeoPackage</option><option value="spatialite">SpatiaLite</option><option value="postgis">PostGIS</option></select></label>{sourceKind === 'postgis' ? <label>连接环境变量<input aria-label="PostGIS 密钥引用" value={sourceSecret} onChange={event => setSourceSecret(event.target.value)} /><small>填写已配置的环境变量名。</small></label> : <label>工作区路径<input aria-label="空间数据库路径" value={sourcePath} placeholder="data/roads.gpkg" onChange={event => setSourcePath(event.target.value)} /></label>}<button type="button" disabled={Boolean(pending) || !onConnectSource} onClick={connectSource}>连接并读取目录</button></div></details>
      </section> : null}
      {pending || notice ? <p className="earth-data-dock__notice" role="status">{pending ? `${pending}…` : notice}</p> : null}
      {error ? <p className="earth-data-dock__error" role="alert">{error}</p> : null}
    </div>
  </aside>;
}
