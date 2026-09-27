import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { GISOperationPanel } from './GISOperationPanel';
import type { ProjectLayer } from './layer-catalog';
import type { SitingPreflight } from './pi-package/gis-preflight.mjs';
afterEach(cleanup);
const layers: ProjectLayer[] = [
  { id: 'sites', name: '候选地块', path: 'sites.geojson', format: 'geojson', featureCount: 1, geometryTypes: ['Polygon'], crs: 'EPSG:4326', updatedAt: '', revision: 1, visible: true, features: [] },
  { id: 'river', name: '河流', path: 'river.geojson', format: 'geojson', featureCount: 1, geometryTypes: ['LineString'], crs: 'EPSG:4326', updatedAt: '', revision: 1, visible: true, features: [] },
];
const checked: SitingPreflight = { schemaVersion: 'earth.gis-preflight.v1', status: 'completed', ready: true, checkedAt: '2026-09-20T00:00:00Z', inputs: [{ role: 'parcels', path: 'sites.geojson', sha256: 'a'.repeat(64), files: [{ name: 'sites.geojson', sha256: 'a'.repeat(64) }], crs: 'EPSG:4326', rows: 1 }, { role: 'avoidance', path: 'river.geojson', sha256: 'b'.repeat(64), files: [{ name: 'river.geojson', sha256: 'b'.repeat(64) }], crs: 'EPSG:4326', rows: 1 }], issues: [], steps: [], distance: 200, units: 'm' };
async function choose() { await userEvent.selectOptions(screen.getByLabelText('分析避让图层'), 'river.geojson'); }
it('requires checked inputs and passes their actual fingerprints to execution', async () => {
  const onRun = vi.fn().mockResolvedValue(undefined), onCheck = vi.fn().mockResolvedValue(checked);
  render(<GISOperationPanel layers={layers} onRun={onRun} onCheck={onCheck} />); await choose();
  expect(screen.queryByRole('button', { name: '生成方案' })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '检查输入' }));
  await userEvent.click(await screen.findByRole('button', { name: '生成方案' }));
  expect(onRun).toHaveBeenCalledWith(expect.objectContaining({ expectedInputs: checked.inputs.map(({ role, path, sha256, files }) => ({ role, path, sha256, files })), distance: 200 }));
});
it('reuses the logical command ID after a lost execution receipt', async () => {
  const onRun = vi.fn().mockRejectedValueOnce(new Error('网络断开')).mockResolvedValue(undefined);
  render(<GISOperationPanel layers={layers} onRun={onRun} onCheck={vi.fn().mockResolvedValue(checked)} />); await choose();
  await userEvent.click(screen.getByRole('button', { name: '检查输入' })); await userEvent.click(screen.getByRole('button', { name: '生成方案' }));
  await userEvent.click(await screen.findByRole('button', { name: '核对并重试' }));
  expect(onRun.mock.calls[0][0].commandId).toBe(onRun.mock.calls[1][0].commandId);
});
it('changes to distance invalidate preflight, and an empty input is not zero', async () => {
  render(<GISOperationPanel layers={layers} onRun={vi.fn()} onCheck={vi.fn().mockResolvedValue(checked)} />); await choose();
  await userEvent.click(screen.getByRole('button', { name: '检查输入' }));
  await userEvent.clear(screen.getByLabelText('避让距离'));
  expect(screen.queryByRole('button', { name: '生成方案' })).not.toBeInTheDocument();
  expect(screen.queryByLabelText('输入检查结果')).not.toBeInTheDocument();
});
it('keeps the task draft when its host hides and restores the drawer', async () => {
  const props = { layers, onRun: vi.fn(), onCheck: vi.fn().mockResolvedValue(checked) };
  const view = render(<div><GISOperationPanel {...props} /></div>); await choose();
  await userEvent.clear(screen.getByLabelText('避让距离')); await userEvent.type(screen.getByLabelText('避让距离'), '350');
  view.rerender(<div hidden><GISOperationPanel {...props} /></div>);
  view.rerender(<div><GISOperationPanel {...props} /></div>);
  expect(screen.getByLabelText('避让距离')).toHaveValue(350); expect(screen.getByLabelText('分析避让图层')).toHaveValue('river.geojson');
});
it('does not allow double submission while the first calculation is unresolved', async () => {
  let done!: () => void; const onRun = vi.fn(() => new Promise<void>(resolve => { done = resolve; }));
  render(<GISOperationPanel layers={layers} onRun={onRun} onCheck={vi.fn().mockResolvedValue(checked)} />); await choose();
  await userEvent.click(screen.getByRole('button', { name: '检查输入' }));
  await userEvent.dblClick(screen.getByRole('button', { name: '生成方案' })); expect(onRun).toHaveBeenCalledTimes(1);
  await act(async () => { done(); }); await waitFor(() => expect(screen.getByText('分析已完成，结果已保存。')).toBeVisible());
});
