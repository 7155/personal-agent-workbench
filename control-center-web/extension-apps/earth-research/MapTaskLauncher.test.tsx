import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MapTaskLauncher } from './MapTaskLauncher';

afterEach(cleanup);

const polygon: GeoJSON.Feature = { type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]] } };
const point: GeoJSON.Feature = { type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [0, 0] } };

it('offers six outcome-based entries and only dispatches the chosen action', () => {
  const onChoose = vi.fn();
  render(<MapTaskLauncher selectedFeatures={[polygon, point]} workspaceReady onChoose={onChoose} />);
  expect(screen.getAllByRole('button')).toHaveLength(6);
  expect(screen.getByText(/已选 2 个地图要素，其中 1 个面可用作范围/)).toBeVisible();
  expect(onChoose).not.toHaveBeenCalled();
  const choices = [
    ['寻找类似地物', 'classification'], ['研究变化', 'change'], ['区域调研', 'research'],
    ['选址与空间分析', 'spatial'], ['制作动画', 'animation'], ['制图交付', 'delivery'],
  ];
  for (const [name, action] of choices) {
    fireEvent.click(screen.getByRole('button', { name }));
    expect(onChoose).toHaveBeenLastCalledWith(action);
  }
  expect(onChoose).toHaveBeenCalledTimes(6);
});

it('explains missing workspace and selection without inventing an analysis result', () => {
  render(<MapTaskLauncher selectedFeatures={[]} workspaceReady={false} onChoose={vi.fn()} />);
  expect(screen.getByText(/还没有地图选择/)).toHaveTextContent('请先关联工作区');
  expect(screen.getByRole('button', { name: '区域调研' })).toBeEnabled();
  expect(screen.getByText(/路线、设施容量与服务覆盖需先确认/)).toHaveTextContent('不代表这些计算已经完成');
});

it('does not describe a point selection as an analysis region', () => {
  render(<MapTaskLauncher selectedFeatures={[point]} workspaceReady onChoose={vi.fn()} />);
  expect(screen.getByText(/区域任务还需要绘制或选择一个面/)).toBeVisible();
});
