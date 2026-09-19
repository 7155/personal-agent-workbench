import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { EarthDataDock, type EarthDataDockProps } from './EarthDataDock';
import type { ProjectLayer, SpatialSourceSummary, WorkspaceFileSummary } from './layer-catalog';
import type { EarthRun } from './workspace';

afterEach(cleanup);

const layer = { id: 'layer:roads', name: '道路候选', path: '.earth/layers/roads.geojson', format: 'geojson' as const, featureCount: 1, geometryTypes: ['LineString'], crs: 'EPSG:4326', updatedAt: '2026-09-19T00:00:00Z', visible: true, features: [] };

it('keeps the Agent object, layer, file and database views in one dock', async () => {
  const onOpenFile = vi.fn();
  render(<EarthDataDock run={null} workspaceRoot="/work/project" activeObjectLabel=".earth/layers/roads.geojson" projectLayers={[layer]} spatialSources={[]} workspaceFiles={[{ path: '/work/project/report.html', name: 'report.html', kind: 'file', byteSize: 128 }]} selectedFeatures={[]} onOpenFile={onOpenFile} />);
  expect(screen.getByText('Agent · roads.geojson')).toBeVisible();
  expect(screen.getByText('Agent 当前操作：.earth/layers/roads.geojson')).not.toBeVisible();
  fireEvent.click(screen.getByText('project'));
  expect(screen.getByText('Agent 当前操作：.earth/layers/roads.geojson')).toBeVisible();
  expect(screen.getByText('道路候选')).toBeVisible();
  fireEvent.click(screen.getByRole('tab', { name: /文件/ }));
  expect(screen.getByText('report.html')).toBeVisible();
  expect(screen.queryByText('/work/project/report.html')).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('treeitem', { name: 'report.html' }));
  expect(screen.getByText('/work/project/report.html')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '打开 report.html' }));
  expect(onOpenFile).toHaveBeenCalledWith(expect.objectContaining({ path: '/work/project/report.html' }));
  await waitFor(() => expect(screen.getByRole('complementary')).toHaveAttribute('aria-busy', 'false'));
  fireEvent.click(screen.getByRole('tab', { name: /数据库/ }));
  expect(screen.getByRole('tabpanel', { name: '空间数据库' })).toBeVisible();
  expect(screen.getByText(/还没有登记空间数据库/)).toBeVisible();
});

it('edits a selected feature property through the versioned layer callback', async () => {
  const feature: GeoJSON.Feature = { type: 'Feature', id: 'parcel-1', properties: { name: '旧名称', area: 12 }, geometry: { type: 'Point', coordinates: [120, 30] } };
  const onUpdateFeature = vi.fn().mockResolvedValue(undefined);
  render(<EarthDataDock run={null} workspaceRoot="/work/project" projectLayers={[]} spatialSources={[]} workspaceFiles={[]} selectedFeatures={[feature]} onUpdateFeature={onUpdateFeature} />);
  fireEvent.change(screen.getByRole('textbox', { name: '属性 name' }), { target: { value: '新名称' } });
  fireEvent.click(screen.getByRole('button', { name: '保存属性版本' }));
  expect(onUpdateFeature).toHaveBeenCalledWith(expect.objectContaining({ id: 'parcel-1', properties: { name: '新名称', area: 12 } }));
  await waitFor(() => expect(screen.getByRole('complementary')).toHaveAttribute('aria-busy', 'false'));
});

const defaults: EarthDataDockProps = { run: null, workspaceRoot: '/work/project', projectLayers: [], spatialSources: [], workspaceFiles: [], selectedFeatures: [] };
const point = (id: string | number, properties: Record<string, unknown> = {}, layerId = 'layer:a'): GeoJSON.Feature & { pawLayerId: string; pawRevision: number } => ({ type: 'Feature', id, properties, geometry: { type: 'Point', coordinates: [120, 30] }, pawLayerId: layerId, pawRevision: 7 });
const withFeatures = (features: GeoJSON.Feature[], changes: Partial<ProjectLayer> = {}): ProjectLayer => ({ ...layer, id: 'layer:a', name: '地块', revision: 7, features, featureCount: features.length, ...changes });

const workspaceFile = (path: string, byteSize?: number): WorkspaceFileSummary => ({ path: `/work/project/${path}`, name: path.split('/').at(-1)!, kind: 'file', ...(byteSize === undefined ? {} : { byteSize }) });

it('expands folders and Shapefile companions, and opens the real SHP entry from the dataset or its sidecar', async () => {
  const files: WorkspaceFileSummary[] = [
    ...['shp', 'shx', 'dbf', 'prj', 'cpg'].map(extension => workspaceFile(`data/source/parcels.${extension}`, 1024)),
    { path: '/work/project/data/empty', name: 'empty', kind: 'directory' },
    workspaceFile('roads.gpkg'), workspaceFile('height.tif'), workspaceFile('report.html'),
  ];
  const onOpenFile = vi.fn().mockResolvedValue(undefined);
  render(<EarthDataDock {...defaults} workspaceFiles={files} onOpenFile={onOpenFile} />);
  fireEvent.click(screen.getByRole('tab', { name: '文件' }));
  const tree = screen.getByRole('tree', { name: '项目文件目录' });
  expect(within(tree).getByText('GeoPackage')).toBeVisible();
  expect(within(tree).getByText('栅格')).toBeVisible();
  expect(within(tree).getByText('报告')).toBeVisible();
  expect(screen.queryByRole('treeitem', { name: 'parcels.shp' })).not.toBeInTheDocument();
  fireEvent.doubleClick(screen.getByRole('treeitem', { name: 'data' }));
  expect(onOpenFile).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '展开文件夹 source' }));
  const dataset = screen.getByRole('treeitem', { name: 'parcels.shp' });
  fireEvent.click(dataset);
  const details = screen.getByRole('region', { name: '所选文件详情' });
  expect(within(details).getByText('/work/project/data/source/parcels.shp')).toBeVisible();
  expect(details).toHaveTextContent('5.0 KB');
  expect(details).toHaveTextContent('含 .prj，坐标系需读取后确认');
  expect(details).not.toHaveTextContent('EPSG:4326');
  fireEvent.doubleClick(dataset);
  await waitFor(() => expect(onOpenFile).toHaveBeenCalledWith(files[0]));
  await waitFor(() => expect(screen.getByRole('complementary')).toHaveAttribute('aria-busy', 'false'));
  fireEvent.click(screen.getByRole('button', { name: '展开配套 parcels.shp' }));
  expect(screen.getByRole('treeitem', { name: 'parcels.cpg' })).toBeVisible();
  expect(screen.getByRole('treeitem', { name: 'parcels.shx' })).toBeVisible();
  fireEvent.click(screen.getByRole('treeitem', { name: 'parcels.dbf' }));
  expect(within(details).getByText('/work/project/data/source/parcels.dbf')).toBeVisible();
  expect(screen.getByRole('button', { name: '打开 parcels.shp' })).toHaveTextContent('打开所属 SHP');
  fireEvent.click(screen.getByRole('button', { name: '打开 parcels.shp' }));
  await waitFor(() => expect(onOpenFile).toHaveBeenCalledTimes(2));
  expect(onOpenFile).toHaveBeenLastCalledWith(files[0]);
  await waitFor(() => expect(screen.getByRole('complementary')).toHaveAttribute('aria-busy', 'false'));
  fireEvent.doubleClick(screen.getByRole('treeitem', { name: 'empty' }));
  fireEvent.click(screen.getByRole('treeitem', { name: 'empty' }));
  expect(details).toHaveTextContent('当前列表未列出子项');
  expect(onOpenFile).toHaveBeenCalledTimes(2);
});

it.each([false, true])('reports missing Shapefile components without treating unknown CRS as complete (partial listing: %s)', incomplete => {
  const files = [workspaceFile('unprojected.shp'), workspaceFile('unprojected.shx'), workspaceFile('unprojected.dbf'), workspaceFile('broken.shp')];
  render(<EarthDataDock {...defaults} workspaceFiles={files} workspaceFilesIncomplete={incomplete} workspaceFilesNotice={incomplete ? '当前仅列出前 240 项。' : undefined} onOpenFile={vi.fn()} />);
  fireEvent.click(screen.getByRole('tab', { name: '文件' }));
  fireEvent.click(screen.getByRole('treeitem', { name: 'unprojected.shp' }));
  const details = screen.getByRole('region', { name: '所选文件详情' });
  expect(details).toHaveTextContent('已列出 .shp、.shx、.dbf');
  expect(details).toHaveTextContent(incomplete ? 'CRS 未知；未找到 .prj（目录尚未完整读取）' : 'CRS 未知（缺少 .prj）');
  expect(details).not.toHaveTextContent('配套齐全');
  fireEvent.click(screen.getByRole('treeitem', { name: 'broken.shp' }));
  expect(details).toHaveTextContent(incomplete ? '未找到 .shx、.dbf（目录尚未完整读取）' : '缺少必要配套 .shx、.dbf');
  if (incomplete) {
    expect(screen.getByRole('status')).toHaveTextContent('当前仅列出前 240 项');
    expect(details).not.toHaveTextContent('缺少必要配套');
  }
});

it('reveals search matches in their directories, refreshes the list, and does not follow symlinks', async () => {
  const files: WorkspaceFileSummary[] = [...['shp', 'shx', 'dbf'].map(extension => workspaceFile(`deep/source/roads.${extension}`)), { ...workspaceFile('linked.shp'), kind: 'symlink' }];
  const onOpenFile = vi.fn(), onRefreshFiles = vi.fn().mockResolvedValue(undefined);
  render(<EarthDataDock {...defaults} workspaceFiles={files} onOpenFile={onOpenFile} onRefreshFiles={onRefreshFiles} />);
  fireEvent.click(screen.getByRole('tab', { name: '文件' }));
  fireEvent.change(screen.getByRole('textbox', { name: '筛选 GIS 文件' }), { target: { value: 'roads.dbf' } });
  await waitFor(() => expect(screen.getByRole('treeitem', { name: 'roads.dbf' })).toBeVisible());
  expect(screen.getByRole('treeitem', { name: 'deep' })).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByRole('treeitem', { name: 'source' })).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByRole('treeitem', { name: 'roads.shx' })).toBeVisible();
  fireEvent.click(screen.getByRole('treeitem', { name: 'roads.dbf' }));
  expect(screen.getByRole('region', { name: '所选文件详情' })).not.toHaveTextContent('bytes');
  fireEvent.change(screen.getByRole('textbox', { name: '筛选 GIS 文件' }), { target: { value: '' } });
  fireEvent.click(screen.getByRole('treeitem', { name: 'linked.shp' }));
  fireEvent.doubleClick(screen.getByRole('treeitem', { name: 'linked.shp' }));
  expect(onOpenFile).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: '打开 linked.shp' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '刷新文件列表' }));
  await waitFor(() => expect(onRefreshFiles).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.getByRole('complementary')).toHaveAttribute('aria-busy', 'false'));
});

it('supports keyboard tree navigation without opening folders as files', async () => {
  const onOpenFile = vi.fn().mockResolvedValue(undefined);
  const file = workspaceFile('data/roads.gpkg');
  render(<EarthDataDock {...defaults} workspaceFiles={[file]} onOpenFile={onOpenFile} />);
  fireEvent.click(screen.getByRole('tab', { name: '文件' }));
  const directory = screen.getByRole('treeitem', { name: 'data' });
  fireEvent.keyDown(directory, { key: 'ArrowRight' });
  fireEvent.keyDown(directory, { key: 'ArrowRight' });
  const child = screen.getByRole('treeitem', { name: 'roads.gpkg' });
  expect(child).toHaveFocus();
  fireEvent.keyDown(child, { key: ' ' });
  expect(child).toHaveAttribute('aria-selected', 'true');
  expect(onOpenFile).not.toHaveBeenCalled();
  fireEvent.keyDown(child, { key: 'Enter' });
  await waitFor(() => expect(onOpenFile).toHaveBeenCalledWith(file));
  await waitFor(() => expect(screen.getByRole('complementary')).toHaveAttribute('aria-busy', 'false'));
  fireEvent.keyDown(child, { key: 'ArrowLeft' });
  expect(directory).toHaveFocus();
  fireEvent.keyDown(directory, { key: 'Enter' });
  expect(directory).toHaveAttribute('aria-expanded', 'false');
  expect(onOpenFile).toHaveBeenCalledTimes(1);
});

it('distinguishes the recorded source file from the editable project copy in folded layer details', () => {
  const active = withFeatures([], { source: { kind: 'file', path: 'data/source/parcels.shp', format: 'shp' } });
  const view = render(<EarthDataDock {...defaults} projectLayers={[active]} />);
  expect(screen.getByText('data/source/parcels.shp')).not.toBeVisible();
  fireEvent.click(screen.getByText('版本与图层管理'));
  expect(screen.getByText('源文件')).toBeVisible();
  expect(screen.getByText('data/source/parcels.shp')).toBeVisible();
  expect(screen.getByText('项目副本')).toBeVisible();
  expect(screen.getByText(active.path)).toBeVisible();
  view.rerender(<EarthDataDock {...defaults} projectLayers={[{ ...active, source: { path: 42 } }]} />);
  expect(screen.queryByText('源文件')).not.toBeInTheDocument();
});

it('preserves untouched property types and the exact source feature revision', async () => {
  const feature = point('001', { name: '原名称', code: '001', truth: 'false', textNull: 'null', nested: { flag: true }, missing: null, count: 12 });
  const onUpdateFeature = vi.fn().mockResolvedValue(undefined);
  render(<EarthDataDock {...defaults} projectLayers={[withFeatures([feature])]} selectedFeatures={[feature]} onUpdateFeature={onUpdateFeature} />);
  fireEvent.click(screen.getByRole('tab', { name: '属性表' }));
  expect(screen.getByRole('button', { name: '保存属性版本' })).toBeDisabled();
  fireEvent.change(screen.getByRole('textbox', { name: '属性 name' }), { target: { value: '已修改' } });
  fireEvent.click(screen.getByRole('button', { name: '保存属性版本' }));
  await waitFor(() => expect(onUpdateFeature).toHaveBeenCalledWith({ ...feature, properties: { ...feature.properties, name: '已修改' } }));
});

it('validates numeric edits and cancels the draft without writing', () => {
  const feature = point(4, { count: 12, name: '原名称' });
  const onUpdateFeature = vi.fn();
  render(<EarthDataDock {...defaults} selectedFeatures={[feature]} onUpdateFeature={onUpdateFeature} />);
  fireEvent.change(screen.getByRole('textbox', { name: '属性 count' }), { target: { value: 'unknown' } });
  expect(screen.getByRole('alert')).toHaveTextContent('不是有效数字');
  expect(screen.getByRole('button', { name: '保存属性版本' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '取消修改' }));
  expect(screen.getByRole('textbox', { name: '属性 count' })).toHaveValue('12');
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(onUpdateFeature).not.toHaveBeenCalled();
});

it('keeps an unsaved edit bound to its original feature when the map selection changes', async () => {
  const first = point('first', { name: 'before' });
  const second = point('second', { name: 'other' }, 'layer:b');
  const onUpdateFeature = vi.fn().mockResolvedValue(undefined);
  const view = render(<EarthDataDock {...defaults} selectedFeatures={[first]} onUpdateFeature={onUpdateFeature} />);
  fireEvent.change(screen.getByRole('textbox', { name: '属性 name' }), { target: { value: 'draft' } });
  view.rerender(<EarthDataDock {...defaults} selectedFeatures={[second]} onUpdateFeature={onUpdateFeature} />);
  expect(screen.getByText(/保留了先前对象的草稿/)).toBeVisible();
  expect(screen.getByRole('textbox', { name: '属性 name' })).toHaveValue('draft');
  fireEvent.click(screen.getByRole('button', { name: '保存属性版本' }));
  await waitFor(() => expect(onUpdateFeature).toHaveBeenCalledWith({ ...first, properties: { name: 'draft' } }));
  await waitFor(() => expect(screen.getByRole('textbox', { name: '属性 name' })).toHaveValue('other'));
});

it('keeps a rejected property draft available for recovery', async () => {
  const onUpdateFeature = vi.fn().mockRejectedValue(new Error('图层已有新版本，请重新读取。'));
  render(<EarthDataDock {...defaults} selectedFeatures={[point(1, { name: 'before' })]} onUpdateFeature={onUpdateFeature} />);
  fireEvent.change(screen.getByRole('textbox', { name: '属性 name' }), { target: { value: 'draft' } });
  fireEvent.click(screen.getByRole('button', { name: '保存属性版本' }));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('已有新版本'));
  expect(screen.getByRole('textbox', { name: '属性 name' })).toHaveValue('draft');
  expect(screen.getByRole('button', { name: '取消修改' })).toBeEnabled();
});

it('sorts and filters the active layer while preserving typed feature identity for map selection', async () => {
  const numericId = point(1, { name: '北地块', area: 9 });
  const stringId = point('1', { name: '南地块', area: 12 });
  const onSelectFeature = vi.fn();
  render(<EarthDataDock {...defaults} projectLayers={[withFeatures([stringId, numericId])]} selectedFeatures={[numericId]} onSelectFeature={onSelectFeature} />);
  fireEvent.click(screen.getByRole('tab', { name: '属性表' }));
  fireEvent.change(screen.getByRole('combobox', { name: '属性表排序字段' }), { target: { value: 'area' } });
  expect(screen.getAllByRole('row')[1]).toHaveTextContent('北地块');
  expect(within(screen.getAllByRole('row')[1]).getByRole('checkbox')).toBeChecked();
  expect(within(screen.getAllByRole('row')[2]).getByRole('checkbox')).not.toBeChecked();
  fireEvent.change(screen.getByRole('textbox', { name: '筛选属性表' }), { target: { value: '南地块' } });
  expect(screen.getByText('筛选显示 1')).toBeVisible();
  expect(screen.getByText('已选 1')).toBeVisible();
  fireEvent.click(screen.getByRole('checkbox', { name: '选择要素 1' }));
  await waitFor(() => expect(onSelectFeature).toHaveBeenCalledWith(stringId, 'layer:a'));
  expect(onSelectFeature.mock.calls[0][0].id).toBe('1');
});

it('keeps export selection scoped to the active layer, independent of the table filter', async () => {
  const feature = point(1, { name: '北地块' });
  const foreign = point(1, { name: '其他图层' }, 'layer:b');
  const active = withFeatures([feature]);
  const onExportLayer = vi.fn();
  const view = render(<EarthDataDock {...defaults} projectLayers={[active]} selectedFeatures={[foreign]} onExportLayer={onExportLayer} />);
  expect(screen.getByRole('button', { name: '导出所选 · 0' })).toBeDisabled();
  view.rerender(<EarthDataDock {...defaults} projectLayers={[active]} selectedFeatures={[feature]} onExportLayer={onExportLayer} />);
  fireEvent.click(screen.getByRole('tab', { name: '属性表' }));
  fireEvent.change(screen.getByRole('textbox', { name: '筛选属性表' }), { target: { value: '不匹配' } });
  expect(screen.getByText('筛选显示 0')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '导出所选 · 1' }));
  await waitFor(() => expect(onExportLayer).toHaveBeenCalledWith('gpkg', active, 'selected'));
  await waitFor(() => expect(screen.getByRole('button', { name: '导出全部 · 1' })).toBeEnabled());
  fireEvent.change(screen.getByRole('combobox', { name: '导出格式' }), { target: { value: 'shp' } });
  fireEvent.click(screen.getByRole('button', { name: '导出全部 · 1' }));
  await waitFor(() => expect(onExportLayer).toHaveBeenLastCalledWith('shp', active, 'all'));
});

it('loads the chosen named database layer and surfaces a load failure', async () => {
  const source: SpatialSourceSummary = { id: 'db:1', name: '区域数据库', kind: 'geopackage', path: 'data/region.gpkg', status: 'ready', layers: ['roads', 'parcels'], schema: '', table: '', updatedAt: '' };
  const onLoadSourceLayer = vi.fn().mockRejectedValue(new Error('parcels 图层读取失败'));
  render(<EarthDataDock {...defaults} spatialSources={[source]} onLoadSourceLayer={onLoadSourceLayer} />);
  fireEvent.click(screen.getByRole('tab', { name: '数据库' }));
  fireEvent.change(screen.getByRole('combobox', { name: '区域数据库 子图层' }), { target: { value: 'parcels' } });
  fireEvent.click(screen.getByRole('button', { name: '加载到地图' }));
  await waitFor(() => expect(onLoadSourceLayer).toHaveBeenCalledWith(source, 'parcels'));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('parcels 图层读取失败'));
});

it('opens the exact recorded layer revision without using the current path', async () => {
  const path = '.earth/layers/history/parcels/v6.geojson';
  const active = withFeatures([], { history: [path] });
  const onOpenLayerRevision = vi.fn();
  render(<EarthDataDock {...defaults} projectLayers={[active]} onOpenLayerRevision={onOpenLayerRevision} />);
  fireEvent.click(screen.getByText('版本与图层管理'));
  fireEvent.click(screen.getByRole('button', { name: /v6.geojson/ }));
  await waitFor(() => expect(onOpenLayerRevision).toHaveBeenCalledWith(active, path));
});

it('selects completed local runs for comparison and delivery without using the GEE run', async () => {
  const run: EarthRun = { schemaVersion: 'earth.run.v1', runId: 'cloud-run', status: 'completed', code: '', scriptPath: '', project: '', sourceHash: '', startedAt: '', updatedAt: '', layers: [], console: [] };
  const localRuns = [{ runId: 'local-new', status: 'completed', op: 'buffer', params: { distance: 300 }, updatedAt: '2026-09-19T02:00:00Z' }, { runId: 'local-old', status: 'completed', op: 'buffer', params: { distance: 200 }, updatedAt: '2026-09-19T01:00:00Z' }, { runId: 'local-failed', status: 'failed', op: 'clip', updatedAt: '2026-09-19T00:00:00Z' }];
  const onCreateBundle = vi.fn(), onCompareRuns = vi.fn(), onShowRun = vi.fn();
  render(<EarthDataDock {...defaults} run={run} localRuns={localRuns} onCreateBundle={onCreateBundle} onCompareRuns={onCompareRuns} onShowRun={onShowRun} />);
  fireEvent.click(screen.getByRole('tab', { name: /运行/ }));
  fireEvent.change(screen.getByRole('combobox', { name: '对照本地运行' }), { target: { value: 'local-old' } });
  expect(screen.queryByRole('option', { name: /local-failed/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '比较两个方案' }));
  await waitFor(() => expect(onCompareRuns).toHaveBeenCalledWith('local-new', 'local-old'));
  await waitFor(() => expect(screen.getByRole('button', { name: '生成成果包' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '生成成果包' }));
  await waitFor(() => expect(onCreateBundle).toHaveBeenCalledWith('local-new'));
  await waitFor(() => expect(screen.getByRole('button', { name: '查看运行结果' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: '查看运行结果' }));
  await waitFor(() => expect(onShowRun).toHaveBeenCalledWith('local-new'));
  fireEvent.click(screen.getByRole('button', { name: /clip.*local-failed/ }));
  await waitFor(() => expect(screen.getByRole('button', { name: '生成成果包' })).toBeDisabled());
  expect(onCreateBundle).not.toHaveBeenCalledWith('cloud-run');
});

it('does not offer local delivery just because a cloud run completed', () => {
  const run = { schemaVersion: 'earth.run.v1', runId: 'cloud', status: 'completed', code: '', scriptPath: '', project: '', sourceHash: '', startedAt: '', updatedAt: '', layers: [], console: [] } as EarthRun;
  render(<EarthDataDock {...defaults} run={run} onCreateBundle={vi.fn()} />);
  fireEvent.click(screen.getByRole('tab', { name: '运行' }));
  expect(screen.queryByRole('button', { name: '生成成果包' })).not.toBeInTheDocument();
  expect(screen.getByText('cloud')).toBeVisible();
});
