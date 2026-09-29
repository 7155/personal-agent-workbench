import { useState, type ReactNode } from 'react';
import { ArrowRight, Check, ChevronDown, GitBranch } from 'lucide-react';
import { JsonTree, MetricComparison } from './ExperimentArtifactVisual';
import { writeClipboardText } from '@/platform/clipboard';
import { object, type JsonValue, type LabArtifact } from './types';
import './experiment-report.css';

type Row = Record<string, unknown>;
export type ExperimentReportMode = 'overview' | 'changes' | 'metrics' | 'records';
const array = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
const label = (value: unknown, fallback = '未记录') => typeof value === 'string' && value.trim() ? value : fallback;
const numeric = (value: unknown): number | undefined => typeof value === 'number' && Number.isFinite(value) ? value : undefined;
const decisionLabels: Record<string, string> = { baseline: '基线', keep: '保留候选', reject: '保留基线', improved: '候选有提升', no_improvement: '未观察到提升', diagnostic_only: '仅供诊断', not_run: '尚未运行', inconclusive: '待验证', unknown: '待验证' };
const decisionLabel = (value: unknown) => decisionLabels[label(value)] ?? label(value, '尚无判定');
const stageLabels: Record<string, string> = { sol_baseline: 'Sol · 起始基线', luna_model_only: 'Luna · 仅更换模型', luna_prompt_v4: 'Luna · 优化提示词', luna_prompt_adapted: 'Luna · 优化提示词' };
const factorLabels: Record<string, string> = { model: '模型', prompt: '提示词', retrieval: '检索', workflow: '工作流', tool: '工具', data: '数据' };
const money = (value: unknown) => numeric(value) === undefined ? '未记录' : `$${Number(value).toLocaleString('en-US', { maximumFractionDigits: 6 })}`;
const percent = (value: unknown) => numeric(value) === undefined ? '未记录' : `${Number((Number(value) * 100).toFixed(2))}%`;

export function isExperimentSnapshot(value: unknown): boolean {
  const data = object(value);
  return data.schemaVersion === 'paw.lab-imported-experiments.v1' && Array.isArray(data.experiments);
}

/** Kind + view/columns select a presentation; titles and prose never supply metrics. */
export function experimentReportMode(artifact: LabArtifact): ExperimentReportMode | undefined {
  if (artifact.kind === 'experiment_snapshot' && isExperimentSnapshot(artifact.content)) return 'records';
  if (artifact.kind !== 'experiment_history') return;
  if (artifact.view === 'markdown') return 'changes';
  if (artifact.view !== 'table') return;
  const columns = new Set(array(object(artifact.content).columns).map((item) => object(item).key));
  if (['experiment', 'metric', 'baseline', 'candidate'].every((key) => columns.has(key))) return 'metrics';
  if (['experiment', 'state', 'dataset', 'split', 'cases', 'decision'].every((key) => columns.has(key))) return 'overview';
}

export function ExperimentReport({ content, mode, selectedId, onSelect, toolbar }: {
  content: JsonValue; mode: ExperimentReportMode; selectedId?: string; onSelect?: (id: string) => void; toolbar?: ReactNode;
}) {
  const data = object(content);
  const records = array(data.experiments).map(object).filter((row) => typeof row.experimentId === 'string');
  const [localSelection, setLocalSelection] = useState('');
  const record = records.find((row) => row.experimentId === (selectedId ?? localSelection))
    ?? records.find((row) => row.projectionState === 'current') ?? records[0];
  const select = (id: string) => { setLocalSelection(id); onSelect?.(id); };
  if (!record) return <p className="lab-visual-empty">还没有实验记录。运行完成后，这里会展示改动、指标和结果。</p>;
  const comparison = object(record.comparison); const decision = label(comparison.decision, 'unknown');
  const baseline = object(record.baseline); const candidate = object(record.candidate); const dataset = object(record.dataset);
  const stages = array(comparison.stageChain).map(object);
  const factors = array(record.factors).map(object);
  const titles = { overview: '方案、过程与结果', changes: '从改动到结论', metrics: '质量与成本对照', records: '评测记录与来源' };
  return <div className="lab-report">
    <div className="lab-report__selection">
      <label>实验记录<select value={label(record.experimentId)} onChange={(event) => select(event.target.value)}>{records.map((row, index) => <option key={label(row.experimentId)} value={label(row.experimentId)}>{index + 1}. {row.projectionState === 'current' ? '当前 · ' : ''}{label(row.title, label(row.experimentId))}</option>)}</select></label>
      <span>{records.findIndex((row) => row === record) + 1} / {records.length}</span>
    </div>
    <header className="lab-report__heading"><div><h3>{titles[mode]}</h3><p className="lab-report__context">{record.projectionState === 'current' ? '当前保留的对照' : '历史对照'} · {numeric(dataset.caseCount) === undefined ? '案例数未记录' : `${dataset.caseCount} 个案例`}</p></div><div className="lab-report__heading-actions"><Decision value={decision} />{toolbar}</div></header>
    <Scope comparison={comparison} dataset={dataset} imported={data.executionPerformed === false} />
    {mode === 'overview' || mode === 'changes' ? <>
      {stages.length ? mode === 'overview' ? <StageTable stages={stages} caseCount={numeric(dataset.caseCount)} /> : <StageFlow stages={stages} /> : <div className="lab-report__simple-flow" aria-label="方案对照"><span>基线方案</span><ArrowRight size={20} /><span>候选方案</span><ArrowRight size={20} /><Decision value={decision} /></div>}
      {mode === 'changes' ? <section aria-label="改动前后"><SectionTitle title="具体改了什么" detail={`${factors.length} 项已记录的改动`} />{factors.length ? <div className="lab-report__changes">{factors.map((factor, index) => <div key={index} className="lab-report__change"><header><span>{String(index + 1).padStart(2, '0')}</span><h4>{factorLabels[label(factor.name)] ?? label(factor.name, '方案调整')}</h4></header><div className="lab-report__change-pair"><div><small>改动前</small><Value value={factor.before} /></div><ArrowRight size={18} aria-hidden="true" /><div><small>改动后</small><Value value={factor.after} /></div></div><p>{label(factor.reason, '尚未记录改动原因。')}</p></div>)}</div> : <p className="lab-visual-empty">这份回执没有记录逐项改动。可以在原始方案中查看已有配置。</p>}</section> : null}
      <section aria-label="结果对比">{mode === 'overview' && stages.length ? <details><summary>展开质量与成本图表</summary><StageCharts stages={stages} caseCount={numeric(dataset.caseCount)} /></details> : <><SectionTitle title="结果发生了什么变化" detail="质量与成本一起看" />{stages.length ? <StageCharts stages={stages} caseCount={numeric(dataset.caseCount)} /> : <MetricComparison baseline={object(baseline.metrics)} candidate={object(candidate.metrics)} />}</>}</section>
      <Conclusion record={record} />
      {mode === 'overview' ? <section><SectionTitle title="全部实验" detail="选择一条，查看它的方案与结果" /><div className="lab-report__history">{records.map((row, index) => <button key={label(row.experimentId)} aria-current={row === record ? 'true' : undefined} onClick={() => select(label(row.experimentId))}><span>{String(index + 1).padStart(2, '0')}</span><span><strong>{label(row.title)}</strong><small>{row.projectionState === 'current' ? '当前对照' : '历史记录'} · {label(object(row.dataset).split, '划分未记录')}</small></span><Decision value={label(object(row.comparison).decision)} /></button>)}</div></section> : null}
    </> : mode === 'metrics' ? <>
      {stages.length ? <section><SectionTitle title="各阶段实测对照" detail="按回执中的阶段排列" /><StageCharts stages={stages} caseCount={numeric(dataset.caseCount)} /></section> : null}
      <section><SectionTitle title="基线 → 候选" detail="展开可查看全部数值指标" /><MetricComparison key={label(record.experimentId)} baseline={object(baseline.metrics)} candidate={object(candidate.metrics)} /></section>
      <Conclusion record={record} />
    </> : <>
      <section><SectionTitle title="一次对照的结构" detail="评测条件 → 两个方案 → 判定" /><div className="lab-report__receipt-flow"><div><small>评测条件</small><strong>{numeric(dataset.caseCount) === undefined ? '案例数未记录' : `${dataset.caseCount} 个案例`}</strong><span>{label(dataset.split)}</span></div><ArrowRight size={18} aria-hidden="true" /><div><small>运行记录</small><strong>基线 / 候选</strong><span>{[baseline, candidate].filter((row) => label(row.runId, '')).length} 个已关联运行</span></div><ArrowRight size={18} aria-hidden="true" /><div><small>判定</small><Decision value={decision} /><span>按原始回执保留</span></div></div></section>
      <section><SectionTitle title="原始运行索引" detail="展开查看配置与输出示例" /><div className="lab-report__receipts">{([['基线', baseline], ['候选', candidate]] as const).map(([name, row]) => <div key={name}><header><h4>{name}</h4><small>{array(row.evidenceRefs).length} 份来源</small></header><code>{label(row.runId, '未关联运行')}</code><details><summary>配置与输出示例</summary><JsonTree value={Object.fromEntries(Object.entries(row).filter(([key]) => !['metrics', 'evidenceRefs', 'runId'].includes(key)))} /></details><details><summary>来源文件</summary><SourceList values={row.evidenceRefs} /></details></div>)}</div></section>
      <details className="lab-report__disclosure"><summary>评测集与固定条件</summary><JsonTree value={dataset} /><JsonTree value={record.frozenControls ?? []} /></details>
      <details className="lab-report__disclosure"><summary>这条实验的全部字段</summary><JsonTree value={record} /></details>
    </>}
    {mode !== 'records' ? <details className="lab-report__disclosure"><summary><GitBranch size={15} /> 评测条件、结论范围与来源 <ChevronDown size={15} /></summary><JsonTree value={dataset} /><JsonTree value={record.frozenControls ?? []} /><JsonTree value={record.claim ?? {}} /><SourceList values={[...array(baseline.evidenceRefs), ...array(candidate.evidenceRefs)]} /></details> : null}
  </div>;
}

function Decision({ value, stage = false }: { value: string; stage?: boolean }) { return <span className="lab-report__decision" data-decision={value}>{value === 'keep' ? <Check size={14} aria-hidden="true" /> : null}{stage && value === 'reject' ? '未通过标准' : stage && value === 'keep' ? '通过标准' : decisionLabel(value)}</span>; }
function SectionTitle({ title, detail }: { title: string; detail?: string }) { return <header className="lab-report__section-title"><h4>{title}</h4>{detail ? <small>{detail}</small> : null}</header>; }
function Value({ value }: { value: unknown }) { return typeof value === 'string' || typeof value === 'number' ? <span>{value}</span> : value == null ? <span>未记录</span> : <JsonTree value={value} />; }
function SourceList({ values }: { values: unknown }) {
  const refs = array(values).filter((value): value is string => typeof value === 'string');
  const [notice, setNotice] = useState('');
  const copy = async (ref: string) => { try { await writeClipboardText(ref); setNotice('已复制来源路径'); } catch { setNotice('复制失败，可选中来源路径后手动复制。'); } };
  return refs.length ? <><ul className="lab-report__sources">{[...new Set(refs)].map((ref) => <li key={ref}><code>{ref}</code><button type="button" onClick={() => void copy(ref)} aria-label={`复制来源 ${ref}`}>复制路径</button></li>)}</ul>{notice ? <p role="status" className="lab-visual-note">{notice}</p> : null}</> : <p className="lab-visual-note">未记录来源文件。</p>;
}
function Scope({ comparison, dataset, imported }: { comparison: Row; dataset: Row; imported: boolean }) {
  const boundary = object(comparison.validationBoundary);
  const parts = [label(dataset.split, '划分未记录'), ...(boundary.candidateAware === true ? ['看过候选后校准'] : []), ...(boundary.heldOutOpened === false || dataset.heldOutConsumed === false ? ['未做留出验证'] : [])];
  return <div className="lab-report__scope"><span>{parts.join(' · ')}</span><span>{boundary.providerBillAvailable === false ? '成本为 Runtime 估算，非实际账单。' : '成本沿用回执口径。'}{imported ? ' 导入未重新运行。' : ''}</span></div>;
}
function StageTable({ stages, caseCount }: { stages: Row[]; caseCount?: number }) {
  return <section aria-label="方案演进对照表"><SectionTitle title="方案演进" detail="质量达标后，再比较成本；历史判定保留" /><div className="lab-report__stage-table"><table><thead><tr><th>方案</th><th>任务通过</th><th>业务检查</th><th>估算成本</th><th>结论</th></tr></thead><tbody>{stages.map((stage, index) => {
    const total = numeric(stage.taskCount) ?? caseCount; const passed = numeric(stage.taskSuccessCount);
    const rate = numeric(stage.agentSuccessRate) ?? numeric(stage.taskSuccessRate);
    return <tr key={index}><th scope="row">{stageLabels[label(stage.stage)] ?? label(stage.stage, `阶段 ${index + 1}`)}</th><td>{passed !== undefined && total !== undefined && total > 0 && Number.isInteger(total) && Number.isInteger(passed) && passed >= 0 && passed <= total ? `${passed}/${total}` : rate !== undefined && rate >= 0 && rate <= 1 ? percent(rate) : '未记录'}</td><td>{numeric(stage.verifierPassCount) !== undefined && numeric(stage.verifierCount) !== undefined ? `${stage.verifierPassCount}/${stage.verifierCount}` : '未记录'}</td><td>{money(stage.costUsd)}</td><td><Decision value={label(stage.decision)} stage /></td></tr>;
  })}</tbody></table></div></section>;
}
function StageFlow({ stages }: { stages: Row[] }) {
  return <section aria-label="已记录的优化过程"><SectionTitle title="方案演进" detail={`${stages.length} 个已记录阶段`} /><ol className="lab-report__stages">{stages.map((stage, index) => <li key={index}><span className="lab-report__step">{String(index + 1).padStart(2, '0')}</span><div><h4>{stageLabels[label(stage.stage)] ?? label(stage.stage, `阶段 ${index + 1}`)}</h4><Decision value={label(stage.decision)} stage /></div>{index < stages.length - 1 ? <ArrowRight className="lab-report__stage-arrow" size={19} aria-hidden="true" /> : null}</li>)}</ol></section>;
}
function StageCharts({ stages, caseCount }: { stages: Row[]; caseCount?: number }) {
  const maxCost = Math.max(...stages.map((stage) => Math.abs(numeric(stage.costUsd) ?? 0)), Number.EPSILON);
  return <div className="lab-report__charts">
    <div className="lab-report__chart" role="group" aria-label="各阶段质量"><h5>完整任务通过率</h5><span className="lab-report__chart-unit">0 — 100%</span>{stages.map((stage, index) => {
      const passed = numeric(stage.taskSuccessCount); const count = numeric(stage.taskCount) ?? caseCount;
      const recordedRate = numeric(stage.agentSuccessRate) ?? numeric(stage.taskSuccessRate);
      const rate = recordedRate !== undefined ? (recordedRate >= 0 && recordedRate <= 1 ? recordedRate : undefined)
        : passed !== undefined && count !== undefined && Number.isInteger(count) && count > 0 && Number.isInteger(passed) && passed >= 0 && passed <= count ? passed / count : undefined;
      const covered = numeric(stage.exactCitationFactsCovered); const total = numeric(stage.citationFactCount);
      return <div className="lab-report__bar-row" key={index}><span>{stageLabels[label(stage.stage)] ?? label(stage.stage)}</span><strong>{percent(rate)}</strong><div className="lab-report__track" aria-hidden="true"><span style={{ width: `${rate === undefined ? 0 : Math.max(0, Math.min(1, rate)) * 100}%` }} /></div>{covered !== undefined && total !== undefined ? <small>证据覆盖 {covered} / {total}</small> : null}</div>;
    })}</div>
    <div className="lab-report__chart" role="group" aria-label="各阶段成本"><h5>运行估算成本</h5><span className="lab-report__chart-unit">USD · 相同标尺</span>{stages.map((stage, index) => <div className="lab-report__bar-row" key={index}><span>{stageLabels[label(stage.stage)] ?? label(stage.stage)}</span><strong>{money(stage.costUsd)}</strong><div className="lab-report__track" data-series="cost" aria-hidden="true"><span style={{ width: `${Math.abs(numeric(stage.costUsd) ?? 0) / maxCost * 100}%` }} /></div><small>{stage.decision === 'reject' ? '未通过标准' : stage.decision === 'keep' ? '通过标准' : decisionLabel(stage.decision)}</small></div>)}</div>
  </div>;
}
function Conclusion({ record }: { record: Row }) {
  const comparison = object(record.comparison);
  return <section className="lab-report__conclusion"><div><h4>这次实验的结论</h4><Decision value={label(comparison.decision)} /></div><details><summary>查看判定理由</summary><p>{label(comparison.decisionReason, '原记录没有提供判定理由。')}</p>{record.businessProblem ? <p>{label(record.businessProblem)}</p> : null}</details></section>;
}
