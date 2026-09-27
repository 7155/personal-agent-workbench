import { useEffect, useRef, useState } from 'react';
import { CloudRunProgress, cloudProgressFromRun } from './CloudRunProgress';
import type { EarthRun } from './workspace';

/** Observe the same saved receipts for Agent scripts and form-driven runs. */
export function LiveCloudRun({ run, onOpenReport }: { run: EarthRun | null; onOpenReport: (path: string) => Promise<void> }) {
  const mountedAt = useRef(Date.now());
  const observed = useRef(new Set<string>());
  const opened = useRef(new Set<string>());
  const [error, setError] = useState('');
  const running = run?.status === 'starting' || run?.status === 'running';
  const eligible = Boolean(run && (running || observed.current.has(run.runId) || Date.parse(run.startedAt) >= mountedAt.current));
  const report = run?.artifacts?.find(item => /\.html?$/i.test(item.path));
  useEffect(() => {
    if (!run || !eligible) return;
    observed.current.add(run.runId);
    if (run.status !== 'completed' || !report || opened.current.has(run.runId)) return;
    opened.current.add(run.runId);
    let active = true;
    void onOpenReport(report.path).catch(() => { if (active) setError('报告已保存，暂未打开。请点击下方按钮重试。'); });
    return () => { active = false; };
  }, [run, eligible, report, onOpenReport]);
  if (!run || !eligible) return null;
  const progress = cloudProgressFromRun(run, { planId: run.runId, submittedAt: Date.parse(run.startedAt), status: 'running' }, run.sourceHash);
  if (!progress) return null;
  return <details className="earth-live-run" open={running}>
    <summary>{running ? '云端计算进行中' : run.status === 'completed' ? '云端成果已就绪' : '云端运行状态'} · 查看进度</summary>
    <CloudRunProgress progress={progress} />
    {error ? <p role="alert">{error}</p> : null}
    {run.status === 'completed' && report ? <button onClick={() => void onOpenReport(report.path).then(() => setError('')).catch(() => setError('报告暂未打开，请稍后重试。'))}>打开地理分析报告</button> : null}
  </details>;
}
