import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { LiveCloudRun } from './LiveCloudRun';
import type { EarthRun } from './workspace';
afterEach(cleanup);
const receipt = (status: EarthRun['status']): EarthRun => ({schemaVersion:'earth.run.v1',runId:'run-agent',status,code:'',scriptPath:'/work/analysis.js',project:'demo',sourceHash:'hash',startedAt:'2025-01-01T00:00:00Z',updatedAt:'2025-01-01T00:01:00Z',layers:[],console:[]});
it('observes Agent execution and opens only a newly completed saved report once', async () => {
  const open = vi.fn().mockResolvedValue(undefined);
  const old = {...receipt('completed'), artifacts:[{kind:'html',path:'/work/old.html'}]};
  const {rerender} = render(<LiveCloudRun run={old} onOpenReport={open} />);
  expect(open).not.toHaveBeenCalled();
  expect(screen.queryByLabelText('云端计算进度')).not.toBeInTheDocument();
  rerender(<LiveCloudRun run={receipt('running')} onOpenReport={open} />);
  expect(screen.getByText('GEE 正在计算')).toBeVisible();
  expect(open).not.toHaveBeenCalled();
  const completed = {...receipt('completed'), artifacts:[{kind:'html',path:'/work/.earth/runs/run-agent/artifacts/report.html'}]};
  rerender(<LiveCloudRun run={completed} onOpenReport={open} />);
  await waitFor(() => expect(open).toHaveBeenCalledExactlyOnceWith(completed.artifacts[0].path));
  rerender(<LiveCloudRun run={{...completed}} onOpenReport={open} />);
  expect(open).toHaveBeenCalledTimes(1);
});
it('does not open a report from an incomplete or failed run', () => {
  const open = vi.fn();
  const {rerender} = render(<LiveCloudRun run={receipt('running')} onOpenReport={open} />);
  rerender(<LiveCloudRun run={{...receipt('failed'),artifacts:[{kind:'html',path:'/work/partial.html'}]}} onOpenReport={open} />);
  expect(open).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', {name:'打开地理分析报告'})).not.toBeInTheDocument();
});
