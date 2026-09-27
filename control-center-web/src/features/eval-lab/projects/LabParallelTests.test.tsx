import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { LabParallelTests } from './LabParallelTests';
import type { LabWorkflowNode } from './project-workflow-types';
afterEach(cleanup);
const node: LabWorkflowNode = { id: 'a', kind: 'experiment', title: '候选 A', status: 'running', summary: '开发集 · 第 4 / 32 题', dependencies: [], source: 'runtime', ref: { kind: 'golden_job', id: 'a' }, testProgress: { stage: '正在评审', costUsd: .02, costBasis: 'estimate', costScope: 'all_job_calls', elapsedMs: 61000, phases: [{ split: 'development', variant: 'baseline', candidateIndex: 0, completed: 3, total: 32, passed: 2, failed: 0, uncertain: 1, errors: 0 }] } };
it('shows independent concurrent lanes, saved denominators and unknown progress without inventing completion', () => {
  const open = vi.fn();
  render(<LabParallelTests nodes={[node, { ...node, id: 'b', title: '候选 B', testProgress: undefined }, { ...node, id: 'c', title: '候选 C', status: 'queued', testProgress: undefined }]} onOpen={open} />);
  expect(screen.getByText('2 运行中 · 1 排队')).toBeVisible();
  expect(screen.getByRole('progressbar', { name: '开发集基线题目进度' })).toHaveAttribute('value', '3');
  expect(screen.getByText('已返回 3 / 32 题')).toBeVisible();
  expect(screen.getByText('通过 2 · 未通过 0 · 待判定 1 · 执行异常 0')).toBeVisible();
  expect(screen.getByText('逐题计数尚未返回')).toBeVisible();
  expect(screen.queryByText('66.7%')).not.toBeInTheDocument();
  expect(screen.getByText('估算费用 $0.02')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '查看候选 B的运行记录' })); expect(open.mock.calls[0]?.[0].id).toBe('b');
});
it('shows completed paired results as history and exposes failures even without completed runs', () => {
  const { rerender } = render(<LabParallelTests nodes={[{ ...node, status: 'failed', testProgress: undefined }]} onOpen={() => {}} />);
  fireEvent.click(screen.getByRole('button', { name: '最近记录 · 1' })); expect(screen.getByText('失败')).toBeVisible();
  rerender(<LabParallelTests nodes={[{ ...node, status: 'completed', testProgress: undefined, metrics: [{ label: '留出集通过率', baseline: .5, candidate: .75, unit: 'ratio' }] }]} onOpen={() => {}} />);
  expect(screen.getByText('50%')).toBeVisible(); expect(screen.getByText('75%')).toBeVisible();
  expect(screen.getByText(/历史记录不代表同时运行/)).toBeVisible();
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
});
