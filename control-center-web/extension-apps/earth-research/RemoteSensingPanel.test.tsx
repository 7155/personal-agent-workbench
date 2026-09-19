import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { RemoteSensingPanel, type RemoteSensingPreparation } from './RemoteSensingPanel';
import type { ProjectLayer } from './layer-catalog';

afterEach(cleanup);

const region: GeoJSON.Feature<GeoJSON.Polygon> = { type: 'Feature', id: 'region', properties: { name: '研究区域' }, geometry: { type: 'Polygon', coordinates: [[[120, 30], [121, 30], [121, 31], [120, 31], [120, 30]]] } };
const sample: GeoJSON.Feature<GeoJSON.Point> = { type: 'Feature', id: 9, properties: { name: '采样点' }, geometry: { type: 'Point', coordinates: [120.5, 30.5] } };
const samples: ProjectLayer = { id: 'samples', name: '裂缝样本', path: '.earth/layers/samples.geojson', format: 'geojson', featureCount: 4, geometryTypes: ['Point'], crs: 'EPSG:4326', updatedAt: '', revision: 4, visible: true, features: ['裂缝', '裂缝', '非裂缝', '非裂缝'].map((value, index) => ({ ...sample, id: index, properties: { class: value, site: `site-${index}` } })) };
const prepared: RemoteSensingPreparation = { status: 'prepared', planId: 'plan-1', runnable: true, execution: 'local', path: '.earth/remote-sensing/plans/plan-1/plan.json', requirements: [{ key: 'image', label: '本地影像', status: 'provided' }] };

it('freezes an explicit ROI and captures labeled samples separately with their original identities', async () => {
  const onSaveSamples = vi.fn().mockResolvedValue(undefined);
  const onPrepareWorkflow = vi.fn().mockResolvedValue(prepared);
  const view = render(<RemoteSensingPanel projectLayers={[samples]} selectedFeatures={[region]} onSaveSamples={onSaveSamples} onPrepareWorkflow={onPrepareWorkflow} />);
  expect(screen.getByRole('button', { name: '准备分析' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: '使用当前范围' }));
  fireEvent.change(screen.getByRole('combobox', { name: '训练样本图层' }), { target: { value: samples.id } });
  view.rerender(<RemoteSensingPanel projectLayers={[samples]} selectedFeatures={[region, sample]} onSaveSamples={onSaveSamples} onPrepareWorkflow={onPrepareWorkflow} />);
  fireEvent.click(screen.getByText('标注当前地图选择'));
  fireEvent.change(screen.getByRole('textbox', { name: '当前样本类别' }), { target: { value: '非裂缝' } });
  fireEvent.click(screen.getByRole('button', { name: '保存 1 个标注样本' }));
  await waitFor(() => expect(onSaveSamples).toHaveBeenCalledWith({ sampleLayerId: 'samples', layerName: '裂缝样本', classField: 'class', classValue: '非裂缝', features: [sample] }));
  await waitFor(() => expect(screen.getByRole('button', { name: '准备分析' })).toBeEnabled());
  fireEvent.change(screen.getByRole('textbox', { name: '遥感影像路径' }), { target: { value: 'data/ice.tif' } });
  fireEvent.click(screen.getByText('进阶设置'));
  fireEvent.change(screen.getByRole('textbox', { name: '分类波段' }), { target: { value: '1, 2, near_infrared' } });
  fireEvent.change(screen.getByRole('textbox', { name: '空间分组字段' }), { target: { value: 'site' } });
  fireEvent.click(screen.getByRole('button', { name: '准备分析' }));
  await waitFor(() => expect(onPrepareWorkflow).toHaveBeenCalledWith({ kind: 'classification', provider: 'local', region: region.geometry, sampleLayerId: 'samples', samplePath: samples.path, sampleRevision: 4, classField: 'class', groupField: 'site', imagePath: 'data/ice.tif', bands: [1, 2, 'near_infrared'] }));
});

it('requires a separate run action and invalidates preparation when input parameters change', async () => {
  const onPrepareWorkflow = vi.fn().mockResolvedValue(prepared), onRunWorkflow = vi.fn();
  render(<RemoteSensingPanel projectLayers={[samples]} selectedFeatures={[region]} onPrepareWorkflow={onPrepareWorkflow} onRunWorkflow={onRunWorkflow} />);
  fireEvent.click(screen.getByRole('button', { name: '使用当前范围' }));
  fireEvent.change(screen.getByRole('combobox', { name: '训练样本图层' }), { target: { value: 'samples' } });
  fireEvent.change(screen.getByRole('textbox', { name: '遥感影像路径' }), { target: { value: 'data/image.tif' } });
  fireEvent.click(screen.getByRole('button', { name: '准备分析' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '运行已准备的分析' })).toBeEnabled());
  expect(onRunWorkflow).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole('textbox', { name: '遥感影像路径' }), { target: { value: 'data/another.tif' } });
  expect(screen.getByText(/参数或输入版本已改变/)).toBeVisible();
  expect(screen.getByRole('button', { name: '运行已准备的分析' })).toBeDisabled();
  expect(screen.getByRole('button', { name: '重新准备分析' })).toBeEnabled();
});

it('only displays completed analysis after the real run callback reports completion', async () => {
  const onPrepareWorkflow = vi.fn().mockResolvedValue(prepared);
  const onRunWorkflow = vi.fn().mockResolvedValue({ status: 'completed', runId: 'local-result', outputs: [{ path: '.earth/remote-sensing/runs/local-result/classification.tif' }] });
  render(<RemoteSensingPanel projectLayers={[]} selectedFeatures={[region]} onPrepareWorkflow={onPrepareWorkflow} onRunWorkflow={onRunWorkflow} />);
  fireEvent.click(screen.getByRole('button', { name: '使用当前范围' }));
  fireEvent.click(screen.getByRole('button', { name: '准备分析' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '运行已准备的分析' })).toBeEnabled());
  expect(screen.queryByRole('heading', { name: '分析已完成' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '运行已准备的分析' }));
  await waitFor(() => expect(onRunWorkflow).toHaveBeenCalledWith('plan-1'));
  await waitFor(() => expect(screen.getByRole('heading', { name: '分析已完成' })).toBeVisible());
  expect(screen.getByText('local-result')).toBeVisible();
  expect(screen.getByRole('button', { name: '本次分析已完成' })).toBeDisabled();
  expect(within(screen.getByRole('list', { name: '分析步骤' })).getAllByRole('listitem')[3]).toHaveAttribute('aria-current', 'step');
});

it('shows missing input requirements without offering execution', async () => {
  const onPrepareWorkflow = vi.fn().mockResolvedValue({ status: 'needs_input', planId: 'missing', runnable: false, requirements: [{ key: 'samples', label: '训练样本', status: 'missing', detail: '需要两类独立样本' }] });
  render(<RemoteSensingPanel projectLayers={[]} selectedFeatures={[region]} onPrepareWorkflow={onPrepareWorkflow} onRunWorkflow={vi.fn()} />);
  fireEvent.click(screen.getByRole('button', { name: '使用当前范围' }));
  fireEvent.click(screen.getByRole('button', { name: '准备分析' }));
  await waitFor(() => expect(screen.getByRole('heading', { name: '还需要补充输入' })).toBeVisible());
  expect(screen.getByText('需要两类独立样本')).toBeVisible();
  expect(screen.queryByRole('button', { name: '运行已准备的分析' })).not.toBeInTheDocument();
  expect(within(screen.getByRole('list', { name: '分析步骤' })).getAllByRole('listitem')[1]).toHaveAttribute('aria-current', 'step');
});

it('sends exact typed NDVI band references and an explicit local provider', async () => {
  const onPrepareWorkflow = vi.fn().mockResolvedValue(prepared);
  render(<RemoteSensingPanel projectLayers={[]} selectedFeatures={[region]} onPrepareWorkflow={onPrepareWorkflow} />);
  fireEvent.click(screen.getByRole('button', { name: '使用当前范围' }));
  fireEvent.change(screen.getByRole('combobox', { name: '遥感任务' }), { target: { value: 'ndvi' } });
  fireEvent.change(screen.getByRole('textbox', { name: '遥感影像路径' }), { target: { value: 'data/vegetation.tif' } });
  fireEvent.change(screen.getByRole('textbox', { name: 'NDVI 红光波段' }), { target: { value: '3' } });
  fireEvent.change(screen.getByRole('textbox', { name: 'NDVI 近红外波段' }), { target: { value: 'near_infrared' } });
  fireEvent.click(screen.getByRole('button', { name: '准备分析' }));
  await waitFor(() => expect(onPrepareWorkflow).toHaveBeenCalledWith({ kind: 'ndvi', provider: 'local', region: region.geometry, imagePath: 'data/vegetation.tif', bands: { red: 3, nir: 'near_infrared' } }));
});

it('validates cloud date ranges and sends only relevant workflow inputs', async () => {
  const onPrepareWorkflow = vi.fn().mockResolvedValue({ status: 'prepared', planId: 'cloud-plan', runnable: false });
  render(<RemoteSensingPanel projectLayers={[]} selectedFeatures={[region]} onPrepareWorkflow={onPrepareWorkflow} />);
  fireEvent.click(screen.getByRole('button', { name: '使用当前范围' }));
  fireEvent.change(screen.getByRole('textbox', { name: '遥感影像路径' }), { target: { value: '/irrelevant-old-path.tif' } });
  fireEvent.change(screen.getByRole('combobox', { name: '遥感任务' }), { target: { value: 'change' } });
  fireEvent.change(screen.getByLabelText('遥感开始日期'), { target: { value: '2025-01-01' } });
  fireEvent.change(screen.getByLabelText('遥感结束日期'), { target: { value: '2024-01-01' } });
  fireEvent.click(screen.getByRole('button', { name: '准备分析' }));
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('结束日期不能早于开始日期'));
  expect(onPrepareWorkflow).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('遥感结束日期'), { target: { value: '2026-01-01' } });
  fireEvent.click(screen.getByRole('button', { name: '准备分析' }));
  await waitFor(() => expect(onPrepareWorkflow).toHaveBeenCalledWith({ kind: 'change', provider: 'gee', region: region.geometry, collection: 'COPERNICUS/S2_SR_HARMONIZED', dateFrom: '2025-01-01', dateTo: '2026-01-01', scale: 10 }));
  await waitFor(() => expect(screen.getByText('准备回执已保存；尚未生成分析结果。')).toBeVisible());
});

it('continues a prepared research plan through the current Agent without pretending to run analysis', async () => {
  const onPrepareWorkflow = vi.fn().mockResolvedValue({ status: 'prepared', planId: 'research-plan', runnable: false, execution: 'research' });
  const onResearch = vi.fn().mockResolvedValue(undefined), onRunWorkflow = vi.fn();
  render(<RemoteSensingPanel projectLayers={[]} selectedFeatures={[region]} onPrepareWorkflow={onPrepareWorkflow} onRunWorkflow={onRunWorkflow} onResearch={onResearch} />);
  fireEvent.click(screen.getByRole('button', { name: '使用当前范围' }));
  fireEvent.change(screen.getByRole('combobox', { name: '遥感任务' }), { target: { value: 'research' } });
  fireEvent.change(screen.getByRole('textbox', { name: '遥感研究问题' }), { target: { value: '调查区域内的裂缝变化及可用证据。' } });
  fireEvent.click(screen.getByRole('button', { name: '准备分析' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '交给 Agent 继续调研' })).toBeEnabled());
  expect(onResearch).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: '运行已准备的分析' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '交给 Agent 继续调研' }));
  await waitFor(() => expect(onResearch).toHaveBeenCalledWith('research-plan'));
  await waitFor(() => expect(screen.getByRole('button', { name: '已交给 Agent 继续调研' })).toBeDisabled());
  expect(onRunWorkflow).not.toHaveBeenCalled();
  expect(screen.queryByRole('heading', { name: '分析已完成' })).not.toBeInTheDocument();
});

it('does not dispatch a research plan after its saved evidence question becomes stale', async () => {
  const onPrepareWorkflow = vi.fn().mockResolvedValue({ status: 'prepared', planId: 'research-plan', runnable: false });
  const onResearch = vi.fn();
  render(<RemoteSensingPanel projectLayers={[]} selectedFeatures={[region]} onPrepareWorkflow={onPrepareWorkflow} onResearch={onResearch} />);
  fireEvent.click(screen.getByRole('button', { name: '使用当前范围' }));
  fireEvent.change(screen.getByRole('combobox', { name: '遥感任务' }), { target: { value: 'research' } });
  fireEvent.change(screen.getByRole('textbox', { name: '遥感研究问题' }), { target: { value: '原研究问题' } });
  fireEvent.click(screen.getByRole('button', { name: '准备分析' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '交给 Agent 继续调研' })).toBeEnabled());
  fireEvent.change(screen.getByRole('textbox', { name: '遥感研究问题' }), { target: { value: '新的研究问题' } });
  expect(screen.getByRole('button', { name: '交给 Agent 继续调研' })).toBeDisabled();
  expect(onResearch).not.toHaveBeenCalled();
});

it('starts at the requested task and keeps four truthful progress stages', async () => {
  const onPrepareWorkflow = vi.fn().mockResolvedValue(prepared);
  render(<RemoteSensingPanel initialKind="ndvi" projectLayers={[]} selectedFeatures={[region]} onPrepareWorkflow={onPrepareWorkflow} />);
  expect(screen.getByRole('heading', { name: '查看植被状况' })).toBeVisible();
  expect(screen.getByRole('combobox', { name: '遥感任务' })).toHaveValue('ndvi');
  const steps = within(screen.getByRole('list', { name: '分析步骤' })).getAllByRole('listitem');
  expect(steps.map(step => step.textContent)).toEqual(['1确定范围', '2样本 / 影像', '3试算', '4查看成果']);
  expect(steps[0]).toHaveAttribute('aria-current', 'step');
  expect(screen.getByText(/尚无成果。真实执行回执返回后/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '使用当前范围' }));
  expect(steps[1]).toHaveAttribute('aria-current', 'step');
  fireEvent.change(screen.getByRole('textbox', { name: '遥感影像路径' }), { target: { value: 'data/vegetation.tif' } });
  fireEvent.change(screen.getByRole('textbox', { name: 'NDVI 红光波段' }), { target: { value: '3' } });
  fireEvent.change(screen.getByRole('textbox', { name: 'NDVI 近红外波段' }), { target: { value: '4' } });
  fireEvent.click(screen.getByRole('button', { name: '准备分析' }));
  await waitFor(() => expect(steps[2]).toHaveAttribute('aria-current', 'step'));
  expect(onPrepareWorkflow).toHaveBeenCalledWith({ kind: 'ndvi', provider: 'local', region: region.geometry, imagePath: 'data/vegetation.tif', bands: { red: 3, nir: 4 } });
  expect(steps[3]).not.toHaveAttribute('aria-current');
  expect(screen.queryByRole('heading', { name: '分析已完成' })).not.toBeInTheDocument();
});

it('switches launcher tasks without reusing the prior prepared plan or losing the confirmed region', async () => {
  const onPrepareWorkflow = vi.fn().mockResolvedValue(prepared), onRunWorkflow = vi.fn();
  const props = { projectLayers: [samples], selectedFeatures: [region], onPrepareWorkflow, onRunWorkflow };
  const view = render(<RemoteSensingPanel {...props} initialKind="classification" />);
  fireEvent.click(screen.getByRole('button', { name: '使用当前范围' }));
  fireEvent.change(screen.getByRole('textbox', { name: '遥感影像路径' }), { target: { value: 'data/source.tif' } });
  fireEvent.click(screen.getByRole('button', { name: '准备分析' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '运行已准备的分析' })).toBeEnabled());
  view.rerender(<RemoteSensingPanel {...props} initialKind="animation" />);
  expect(screen.getByRole('heading', { name: '制作动画' })).toBeVisible();
  expect(screen.getByRole('button', { name: '更新为当前范围' })).toBeEnabled();
  expect(screen.queryByRole('region', { name: '分析准备回执' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '运行已准备的分析' })).not.toBeInTheDocument();
  expect(onRunWorkflow).not.toHaveBeenCalled();
  view.rerender(<RemoteSensingPanel {...props} initialKind="classification" />);
  expect(screen.getByRole('textbox', { name: '遥感影像路径' })).toHaveValue('data/source.tif');
});

it('ignores a late preparation response after the launcher selects another task', async () => {
  let complete: (value: RemoteSensingPreparation) => void = () => undefined;
  const onPrepareWorkflow = vi.fn(() => new Promise<RemoteSensingPreparation>(resolve => { complete = resolve; }));
  const props = { projectLayers: [], selectedFeatures: [region], onPrepareWorkflow, onRunWorkflow: vi.fn() };
  const view = render(<RemoteSensingPanel {...props} initialKind="classification" />);
  fireEvent.click(screen.getByRole('button', { name: '使用当前范围' }));
  fireEvent.click(screen.getByRole('button', { name: '准备分析' }));
  view.rerender(<RemoteSensingPanel {...props} initialKind="research" />);
  await act(async () => { complete(prepared); });
  expect(screen.getByRole('heading', { name: '区域调研' })).toBeVisible();
  expect(screen.queryByRole('region', { name: '分析准备回执' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '运行已准备的分析' })).not.toBeInTheDocument();
});

it('keeps class and spatial group checks visible while advanced inputs remain editable', () => {
  const grouped: ProjectLayer = { ...samples, features: samples.features.map(feature => ({ ...feature, properties: { ...feature.properties, site: 'one-site' } })) };
  render(<RemoteSensingPanel projectLayers={[grouped]} selectedFeatures={[region]} />);
  fireEvent.change(screen.getByRole('combobox', { name: '训练样本图层' }), { target: { value: grouped.id } });
  expect(screen.getByText(/每类需来自至少两个可分开的要素或空间组/)).toBeVisible();
  expect(screen.getByText('进阶设置').closest('details')).not.toHaveAttribute('open');
  expect(screen.getByLabelText('空间分组字段')).not.toBeVisible();
  fireEvent.click(screen.getByText('进阶设置'));
  expect(screen.getByLabelText('空间分组字段')).toBeVisible();
  fireEvent.change(screen.getByRole('textbox', { name: '空间分组字段' }), { target: { value: 'site' } });
  expect(screen.getByText(/部分类型不足两个有效分组/)).toBeVisible();
  expect(screen.getByText(/当前本地方法为随机森林像元分类/)).toHaveTextContent('不能代替独立地区或日期的检验');
  fireEvent.change(screen.getByRole('combobox', { name: '分类字段' }), { target: { value: 'missing' } });
  expect(screen.getByText(/4 个要素缺少可用的 missing 类别/)).toHaveTextContent('至少需要两类已标注样本');
  fireEvent.click(screen.getByText('进阶设置'));
  expect(screen.getByLabelText('空间分组字段')).not.toBeVisible();
  expect(screen.getByText(/4 个要素缺少可用的 missing 类别/)).toBeVisible();
});

it('allows cloud dataset and resolution changes through advanced settings', async () => {
  const onPrepareWorkflow = vi.fn().mockResolvedValue({ status: 'prepared', planId: 'cloud', runnable: false });
  render(<RemoteSensingPanel initialKind="change" projectLayers={[]} selectedFeatures={[region]} onPrepareWorkflow={onPrepareWorkflow} />);
  fireEvent.click(screen.getByRole('button', { name: '使用当前范围' }));
  fireEvent.change(screen.getByLabelText('遥感开始日期'), { target: { value: '2024-06-01' } });
  fireEvent.change(screen.getByLabelText('遥感结束日期'), { target: { value: '2025-06-01' } });
  fireEvent.click(screen.getByText('进阶设置'));
  fireEvent.change(screen.getByRole('textbox', { name: '遥感影像集合' }), { target: { value: 'COPERNICUS/S2_SR_HARMONIZED' } });
  fireEvent.change(screen.getByRole('spinbutton', { name: '遥感分析分辨率' }), { target: { value: '20' } });
  fireEvent.click(screen.getByRole('button', { name: '准备分析' }));
  await waitFor(() => expect(onPrepareWorkflow).toHaveBeenCalledWith({ kind: 'change', provider: 'gee', region: region.geometry, collection: 'COPERNICUS/S2_SR_HARMONIZED', dateFrom: '2024-06-01', dateTo: '2025-06-01', scale: 20 }));
});

it('offers actual workspace image paths without preventing a manually entered path', async () => {
  const onPrepareWorkflow = vi.fn().mockResolvedValue(prepared);
  render(<RemoteSensingPanel projectLayers={[]} selectedFeatures={[region]} imageFiles={[{ path: 'data/裂缝影像.tif', name: '裂缝影像.tif' }, { path: 'data/vegetation.tiff', name: 'vegetation.tiff' }]} onPrepareWorkflow={onPrepareWorkflow} />);
  const input = screen.getByRole('combobox', { name: '遥感影像路径' });
  const choices = document.getElementById(input.getAttribute('list')!);
  expect([...choices!.querySelectorAll('option')].map(option => [option.value, option.label])).toEqual([['data/裂缝影像.tif', '裂缝影像.tif'], ['data/vegetation.tiff', 'vegetation.tiff']]);
  expect(screen.getByText(/可选择工作区内的 2 个影像，也可手填相对路径/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '使用当前范围' }));
  fireEvent.change(input, { target: { value: 'data/裂缝影像.tif' } });
  fireEvent.click(screen.getByRole('button', { name: '准备分析' }));
  await waitFor(() => expect(onPrepareWorkflow).toHaveBeenLastCalledWith(expect.objectContaining({ imagePath: 'data/裂缝影像.tif' })));
  fireEvent.change(input, { target: { value: 'other/manual.tif' } });
  fireEvent.click(screen.getByRole('button', { name: '重新准备分析' }));
  await waitFor(() => expect(onPrepareWorkflow).toHaveBeenLastCalledWith(expect.objectContaining({ imagePath: 'other/manual.tif' })));
});

it('opens only an output path returned by the real run and reports open failures', async () => {
  const outputPath = '.earth/gis/runs/actual-result/classification.tif';
  const onPrepareWorkflow = vi.fn().mockResolvedValue(prepared);
  const onRunWorkflow = vi.fn().mockResolvedValue({ status: 'completed', runId: 'actual-result', outputs: [{ path: outputPath }] });
  const onOpenResult = vi.fn().mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error('成果文件暂时无法打开'));
  render(<RemoteSensingPanel projectLayers={[]} selectedFeatures={[region]} onPrepareWorkflow={onPrepareWorkflow} onRunWorkflow={onRunWorkflow} onOpenResult={onOpenResult} />);
  expect(screen.queryByRole('button', { name: /打开成果/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '使用当前范围' }));
  fireEvent.click(screen.getByRole('button', { name: '准备分析' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '运行已准备的分析' })).toBeEnabled());
  expect(screen.queryByRole('button', { name: /打开成果/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '运行已准备的分析' }));
  const open = await screen.findByRole('button', { name: `打开成果 ${outputPath}` });
  fireEvent.click(open);
  await waitFor(() => expect(onOpenResult).toHaveBeenCalledWith(outputPath));
  await waitFor(() => expect(open).toBeEnabled());
  fireEvent.click(open);
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('成果文件暂时无法打开'));
  expect(onRunWorkflow).toHaveBeenCalledTimes(1);
});
