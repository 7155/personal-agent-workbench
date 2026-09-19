import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { EarthDataDock } from './EarthDataDock';

const layer = { id: 'layer:roads', name: '道路候选', path: '.earth/layers/roads.geojson', format: 'geojson' as const, featureCount: 1, geometryTypes: ['LineString'], crs: 'EPSG:4326', updatedAt: '2026-09-19T00:00:00Z', visible: true, features: [] };

it('keeps the Agent object, layer, file and database views in one dock', () => {
  const onOpenFile = vi.fn();
  render(<EarthDataDock run={null} workspaceRoot="/work/project" activeObjectLabel=".earth/layers/roads.geojson" projectLayers={[layer]} spatialSources={[]} workspaceFiles={[{ path: '/work/project/report.html', name: 'report.html', kind: 'file', byteSize: 128 }]} selectedFeatures={[]} onOpenFile={onOpenFile} />);
  expect(screen.getByText('Agent 当前操作：.earth/layers/roads.geojson')).toBeVisible();
  expect(screen.getByText('道路候选')).toBeVisible();
  fireEvent.click(screen.getByRole('tab', { name: /文件/ }));
  expect(screen.getByText('report.html')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '打开' }));
  expect(onOpenFile).toHaveBeenCalledWith(expect.objectContaining({ path: '/work/project/report.html' }));
  fireEvent.click(screen.getByRole('tab', { name: /数据库/ }));
  expect(screen.getByRole('tabpanel', { name: '空间数据库' })).toBeVisible();
  expect(screen.getByText(/还没有登记空间数据库/)).toBeVisible();
});

it('edits a selected feature property through the versioned layer callback', () => {
  const feature: GeoJSON.Feature = { type: 'Feature', id: 'parcel-1', properties: { name: '旧名称', area: 12 }, geometry: { type: 'Point', coordinates: [120, 30] } };
  const onUpdateFeature = vi.fn().mockResolvedValue(undefined);
  render(<EarthDataDock run={null} workspaceRoot="/work/project" projectLayers={[]} spatialSources={[]} workspaceFiles={[]} selectedFeatures={[feature]} onUpdateFeature={onUpdateFeature} />);
  fireEvent.change(screen.getByRole('textbox', { name: '属性 name' }), { target: { value: '新名称' } });
  fireEvent.click(screen.getByRole('button', { name: '保存属性版本' }));
  expect(onUpdateFeature).toHaveBeenCalledWith(expect.objectContaining({ id: 'parcel-1', properties: { name: '新名称', area: 12 } }));
});
