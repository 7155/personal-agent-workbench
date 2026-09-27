import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { LabProjectFlow } from './LabProjectFlow';
import type { LabProject } from './types';

afterEach(cleanup);
it('explains stages and opens actual experiment records without claiming a pass', () => {
  const node = { id: 'trial', kind: 'experiment', title: '固定问题比较', status: 'completed', summary: '', dependencies: [], ref: { kind: 'golden_job', id: 'trial' }, source: 'runtime', decision: 'no_improvement' };
  const select = vi.fn(); const graph = vi.fn(); const apps = vi.fn();
  render(<LabProjectFlow project={{ workflow: { nodes: [node] } } as unknown as LabProject} onSelectNode={select} onOpenApps={apps} onOpenGraph={graph} />);
  fireEvent.click(screen.getByRole('button', { name: /比较方案/ }));
  expect(screen.getByText(/运行完成不等于质量通过/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: /固定问题比较/ })); expect(select).toHaveBeenCalledWith(node);
  fireEvent.click(screen.getByRole('button', { name: '查看任务依赖图' })); expect(graph).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: /交付应用/ }));
  expect(screen.getByText('尚未返回这一环节的记录。')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '打开应用版本与试用' })); expect(apps).toHaveBeenCalledOnce();
});
