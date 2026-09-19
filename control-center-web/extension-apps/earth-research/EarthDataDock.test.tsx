import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { EarthDataDock, type EarthDataDockProps } from './EarthDataDock';
import type { ProjectLayer, SpatialSourceSummary } from './layer-catalog';
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
  expect(screen.getByText('/work/project/report.html')).not.toBeVisible();
  fireEvent.click(screen.getByText('文件详情'));
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
