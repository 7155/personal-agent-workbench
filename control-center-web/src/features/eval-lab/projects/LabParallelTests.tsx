import { useState } from 'react';
import { ArrowUpRight, Check, CircleAlert, LoaderCircle } from 'lucide-react';
import type { LabWorkflowMetric, LabWorkflowNode } from './project-workflow-types';
import { workflowNodeStatus } from './LabWorkflowGraph';

function value(number: number | null | undefined, metric: LabWorkflowMetric) {
  if (number == null || !Number.isFinite(number)) return '待返回';
  if (metric.unit === 'ratio') return `${Number((number * 100).toFixed(1))}%`;
  if (metric.unit === 'USD') return `$${Number(number.toFixed(5))}`;
  return `${Number(number.toFixed(2))}${metric.unit ? ` ${metric.unit}` : ''}`;
}
function TestLane({ node, onOpen }: { node: LabWorkflowNode; onOpen: (node: LabWorkflowNode) => void }) {
  const progress = node.testProgress;
  const running = node.status === 'running';
  const elapsed = progress?.elapsedMs == null ? null : progress.elapsedMs + (['running', 'queued'].includes(node.status) && node.updatedAtMs ? Math.max(0, Date.now() - node.updatedAtMs) : 0);
  const hasFailure = ['failed', 'interrupted', 'cancelled'].includes(node.status);
  const metrics = node.status === 'completed' ? (node.metrics ?? []).filter((metric) => metric.value == null && metric.baseline != null && metric.candidate != null).slice(0, 3) : [];
  return <article className="lab-test-lane" data-state={node.status} aria-label={`${node.title}的测试进度`}>
    <header><span className="lab-test-lane__state">{running ? <LoaderCircle size={14} /> : hasFailure ? <CircleAlert size={14} /> : node.status === 'completed' ? <Check size={14} /> : null}{workflowNodeStatus(node)}</span><button onClick={() => onOpen(node)} aria-label={`查看${node.title}的运行记录`}>{node.title}<ArrowUpRight size={13} /></button></header>
    <p>{progress?.stage || node.summary || '等待执行器返回进度'}</p>
    {progress?.counter ? <div className="lab-test-phases"><div><div><strong>已完成</strong><span>{progress.counter.completed} / {progress.counter.total} {progress.counter.unit}</span></div><progress max={progress.counter.total || 1} value={progress.counter.completed} aria-label={`${node.title}核对进度`} /></div></div> : null}
    {progress?.phases.length ? <div className="lab-test-phases">{progress.phases.map((phase) => <div key={`${phase.split}:${phase.variant}:${phase.candidateIndex}`}>
      <div><strong>{phase.split === 'development' ? '开发集' : '留出集'} · {phase.variant === 'baseline' ? '基线' : `候选 ${phase.candidateIndex}`}</strong><span>已返回 {phase.completed}{phase.total == null ? ' 题' : ` / ${phase.total} 题`}</span></div>
      {phase.total != null && phase.total > 0 ? <progress max={phase.total} value={phase.completed} aria-label={`${phase.split === 'development' ? '开发集' : '留出集'}${phase.variant === 'baseline' ? '基线' : `候选 ${phase.candidateIndex}`}题目进度`} /> : null}
      <small>通过 {phase.passed} · 未通过 {phase.failed} · 待判定 {phase.uncertain} · 执行异常 {phase.errors}</small>
    </div>)}</div> : running && !progress?.counter ? <small>逐题计数尚未返回</small> : null}
    {progress ? <div className="lab-test-lane__resources"><span>{progress.costUsd == null ? '费用待返回' : `${progress.costBasis === 'estimate' ? '估算费用' : '费用'} $${Number(progress.costUsd.toFixed(5))}`}<small>本任务全部调用</small></span><span>{elapsed == null ? '耗时待返回' : `${Math.floor(elapsed / 60000)}分 ${Math.floor(elapsed / 1000) % 60}秒`}<small>自提交起，含排队</small></span></div> : null}
    {metrics.length ? <div className="lab-test-lane__effects">{metrics.map((metric) => <div key={metric.label}><small>{metric.label}</small><span>{value(metric.baseline, metric)} <span aria-label="到">→</span> <strong>{value(metric.candidate, metric)}</strong></span></div>)}</div> : null}
  </article>;
}
export function LabParallelTests({ nodes, onOpen }: { nodes: LabWorkflowNode[]; onOpen: (node: LabWorkflowNode) => void }) {
  const [showResults, setShowResults] = useState(false);
  const tests = nodes.filter((node) => node.source === 'runtime' && (node.kind === 'experiment' || node.kind === 'calibration' || (node.kind === 'dataset' && node.ref.kind === 'golden_job')) && !['pending', 'unavailable'].includes(node.status));
  const active = tests.filter((node) => node.status === 'running' || node.status === 'queued');
  const completed = tests.filter((node) => node.status === 'completed').sort((a, b) => (b.updatedAtMs ?? 0) - (a.updatedAtMs ?? 0));
  const failed = tests.filter((node) => ['failed', 'interrupted'].includes(node.status)).sort((a, b) => (b.updatedAtMs ?? 0) - (a.updatedAtMs ?? 0));
  return <section className="lab-parallel-tests" aria-label="并行测试进度与效果">
    <header><h3>并行测试</h3><span>{active.filter((node) => node.status === 'running').length} 运行中 · {active.filter((node) => node.status === 'queued').length} 排队</span>{completed.length || failed.length ? <button aria-expanded={showResults} onClick={() => setShowResults(!showResults)}>{showResults ? '收起最近结果' : `最近记录 · ${completed.length + failed.length}`}</button> : null}</header>
    {!active.length ? <p className="lab-parallel-tests__empty">当前没有运行中的测试。启动后，各实验会在这里分别更新进度。</p> : <div className="lab-parallel-tests__lanes">{active.map((node) => <TestLane key={node.id} node={node} onOpen={onOpen} />)}</div>}
    {active.some((node) => node.testProgress?.phases.length) ? <p className="lab-parallel-tests__note">统计来自已保存的逐题回执，可能晚于当前执行。部分结果不代表最终提升；不同题集不合并通过率。</p> : null}
    {showResults ? <><p className="lab-parallel-tests__note">最近完成的 3 项 · 基线 → 候选；历史记录不代表同时运行。</p>{completed.slice(0, 3).map((node) => <TestLane key={node.id} node={node} onOpen={onOpen} />)}{failed.slice(0, 2).map((node) => <TestLane key={node.id} node={node} onOpen={onOpen} />)}</> : null}
  </section>;
}
