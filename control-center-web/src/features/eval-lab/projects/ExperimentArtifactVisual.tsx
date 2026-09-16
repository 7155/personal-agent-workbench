import { useId, useState } from 'react';
import { ArrowRight, Check, ChevronRight, FileText } from 'lucide-react';
import { object, type ArtifactTable, type JsonValue } from './types';
import './experiment-artifact-visual.css';

type RecordValue = Record<string, unknown>;
const text = (value: unknown, fallback = '未记录') => typeof value === 'string' && value.trim() ? value : fallback;
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
const number = (value: unknown): number | undefined => typeof value === 'number' && Number.isFinite(value) ? value : undefined;
const decisionLabels: Record<string, string> = { keep: '保留候选', reject: '保留基线', improved: '采用候选', no_improvement: '保留基线', inconclusive: '待验证', unknown: '待验证', diagnostic_only: '诊断记录', not_run: '尚未运行' };
const decisionText = (value: unknown) => decisionLabels[text(value)] ?? text(value, '尚无判定');
const metricLabels: Record<string, string> = {
  agentSuccessRate: '完整任务通过率', answerSuccessRate: '回答通过率', passRate: '通过率',
  citationFactCoverage: '引用事实覆盖', exactCitationFactCoverage: '精确引用覆盖',
  recallAt10: 'Recall@10', mrr: 'MRR', ndcgAt10: 'nDCG@10',
  apiCostUsd: 'API 估算成本（USD）', estimatedApiCostUsd: 'API 估算成本（USD）', latencyMs: '耗时（ms）',
  taskSuccessCount: '完整任务通过数', taskCount: '任务数', agentCaseCount: 'Agent 案例数',
  tokens: 'Token 数', toolCalls: '工具调用数', failedToolCalls: '工具调用失败数',
};
const rates = new Set(['agentSuccessRate', 'answerSuccessRate', 'passRate', 'citationFactCoverage', 'exactCitationFactCoverage', 'recallAt10']);
const normalizedMetrics = new Set([...rates, 'mrr', 'ndcgAt10']);
function formatMetric(key: string, value: number | undefined) {
  if (value === undefined) return '未记录';
  if (rates.has(key) && value >= 0 && value <= 1) return `${Number((value * 100).toFixed(2))}%`;
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 6 });
}

/** Numeric fields only; missing, null and numeric strings never become zero. */
export function MetricComparison({ baseline, candidate }: { baseline: RecordValue; candidate: RecordValue }) {
  const [expanded, setExpanded] = useState(false);
  const keys = [...new Set([...Object.keys(baseline), ...Object.keys(candidate)])]
    .filter((key) => number(baseline[key]) !== undefined || number(candidate[key]) !== undefined)
    .sort((a, b) => Number(Boolean(metricLabels[b])) - Number(Boolean(metricLabels[a])));
  if (!keys.length) return <p className="lab-visual-empty">这份记录没有可绘制的数值指标。可在原始数据中查看其他字段。</p>;
  return <div className="lab-metric-comparison">
    <div className="lab-visual-legend"><span data-series="baseline">基线</span><span data-series="candidate">候选</span><small>每个指标单独标尺</small></div>
    <div className="lab-metric-comparison__rows">{(expanded ? keys : keys.slice(0, 6)).map((key) => {
      const before = number(baseline[key]); const after = number(candidate[key]);
      const max = normalizedMetrics.has(key) && Math.max(before ?? 0, after ?? 0) <= 1 ? 1 : Math.max(Math.abs(before ?? 0), Math.abs(after ?? 0), Number.EPSILON);
      return <div className="lab-metric-row" key={key} role="group" aria-label={metricLabels[key] ?? key}>
        <div className="lab-metric-row__label"><strong>{metricLabels[key] ?? key}</strong>{metricLabels[key] ? <small>{key}</small> : null}</div>
        <div className="lab-metric-row__bars">{([['baseline', '基线', before], ['candidate', '候选', after]] as const).map(([series, label, value]) => <div className="lab-metric-row__series" key={series} data-series={series}>
          <span className="lab-metric-row__track" aria-hidden="true"><span style={{ width: `${value === undefined ? 0 : Math.min(Math.abs(value) / max, 1) * 100}%` }} /></span>
          <span aria-label={`${label}：${formatMetric(key, value)}`}>{formatMetric(key, value)}</span>
        </div>)}</div>
      </div>;
    })}</div>
    {keys.length > 6 ? <button className="lab-visual-link" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? '收起其他指标' : `查看全部 ${keys.length} 项指标`}</button> : null}
    <p className="lab-visual-note">缺失值显示为“未记录”。成本按原报告口径展示，耗时不单独决定采用。</p>
  </div>;
}

function Value({ value }: { value: unknown }) {
  return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' ? <span className="lab-visual-value">{String(value)}</span>
    : value == null ? <span className="lab-visual-note">未记录</span> : <JsonTree value={value} />;
}

/** A readable fallback for arbitrary structured artifacts; no schema is invented. */
export function JsonTree({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (value === null || typeof value !== 'object') return <Value value={value} />;
  const entries = Array.isArray(value) ? value.map((item, index) => [String(index + 1), item] as const) : Object.entries(value);
  return <div className="lab-json-tree">{entries.length ? entries.map(([key, item]) => <div key={key}>
    {item !== null && typeof item === 'object' ? <details open={depth === 0}><summary>{key}<small>{Array.isArray(item) ? `${item.length} 项` : `${Object.keys(item).length} 个字段`}</small></summary><JsonTree value={item} depth={depth + 1} /></details>
      : <div className="lab-json-tree__leaf"><strong>{key}</strong><Value value={item} /></div>}
  </div>) : <span className="lab-visual-note">暂无条目</span>}</div>;
}

export function RawArtifactData({ content }: { content: JsonValue }) {
  return <details className="lab-visual-raw"><summary>原始数据</summary><pre>{JSON.stringify(content, null, 2)}</pre></details>;
}

export function ExperimentArtifactVisual({ content }: { content: JsonValue }) {
  const data = object(content);
  if (data.schemaVersion === 'paw.lab-project-progress.v1' && Array.isArray(data.steps)) return <ProjectProgress data={data} />;
  if (!['paw.lab-imported-experiments.v1', 'paw.lab-experiment-steps.v1'].includes(text(data.schemaVersion)) || !Array.isArray(data.experiments)) return <JsonTree value={content} />;
  return <ExperimentExplorer key={text(data.schemaVersion)} data={data} />;
}

function ExperimentExplorer({ data }: { data: RecordValue }) {
  const records = list(data.experiments).map((value, index) => ({ ...object(value), experimentId: text(object(value).experimentId, `record-${index}`) } as RecordValue));
  const [selected, setSelected] = useState(''); const [section, setSection] = useState('changes');
  const record = records.find((item) => item.experimentId === selected) ?? records.find((item) => item.projectionState === 'current') ?? records[0];
  const panelId = useId();
  if (!record) return <p className="lab-visual-empty">还没有实验记录。完成一次运行后，这里会展示方案、过程和指标。</p>;
  // Only a persisted parent edge establishes continuation. Display order is not lineage.
  const parentOf = (item: RecordValue) => text(item.comparedTo, '') || text(records.find((candidate) => candidate.supersededBy === item.experimentId)?.experimentId, '');
  const parent = parentOf(record);
  const comparison = object(record.comparison ?? record.effect);
  const factors = list(record.steps ?? record.factors).map(object);
  const baseline = object(record.baseline); const candidate = object(record.candidate);
  const dataset = object(record.dataset);
  const evidence = [...new Set([...list(record.evidenceRefs), ...list(baseline.evidenceRefs), ...list(candidate.evidenceRefs)].filter((item): item is string => typeof item === 'string'))];
  const navigate = (id: string) => { setSelected(id); setSection('changes'); };
  return <div className="lab-experiment-visual">
    <div className="lab-visual-context"><span>{data.schemaVersion === 'paw.lab-imported-experiments.v1' ? '历史实验' : '实验结果'} · {records.length} 条记录</span><p>{text(data.caption, data.executionPerformed === false ? '这是保存的实验快照，本次导入没有重新执行。当前运行请到“运行”查看。' : '按原始回执整理；当前执行状态请到“运行”查看。')}</p></div>
    <label className="lab-experiment-picker">实验记录<select value={text(record.experimentId)} onChange={(event) => navigate(event.target.value)}>{records.map((item) => <option key={text(item.experimentId)} value={text(item.experimentId)}>{text(item.title, text(item.experimentId))}</option>)}</select></label>
    <section className="lab-visual-summary" aria-label="先看结论"><strong>先看结论</strong><span className="lab-experiment-decision">{decisionText(record.decision ?? comparison.decision)}</span><p>{text(comparison.decisionReason ?? record.whyContinue, '原始记录没有提供判定理由，请打开“指标与判定”查看实际数值。')}</p></section>
    <div className="lab-experiment-explorer">
      <nav className="lab-experiment-timeline" aria-label="实验演进"><h3>实验演进</h3><ol>{records.map((item, index) => {
        const id = text(item.experimentId, `record-${index}`); const previous = parentOf(item);
        return <li key={id}><button aria-current={item === record ? 'step' : undefined} aria-controls={panelId} onClick={() => navigate(id)}><span className="lab-experiment-timeline__number" aria-hidden="true">{index + 1}</span><span><strong>{text(item.title, id)}</strong><small>{decisionText(item.decision ?? object(item.comparison ?? item.effect).decision)}</small>{previous ? <small>承接：{text(records.find((value) => value.experimentId === previous)?.title, previous)}</small> : <small>未关联前序实验</small>}</span><ChevronRight size={15} /></button></li>;
      })}</ol></nav>
      <div className="lab-experiment-detail" id={panelId}>
        <header><h3>{text(record.title, text(record.experimentId))}</h3><span className="lab-experiment-decision">{decisionText(record.decision ?? comparison.decision)}</span></header>
        {parent ? <p className="lab-visual-note">直接对照：{records.some((item) => item.experimentId === parent) ? <button className="lab-visual-link" onClick={() => navigate(parent)}>{text(records.find((item) => item.experimentId === parent)?.title, parent)}</button> : parent}</p> : null}
        <div className="lab-experiment-structure" aria-label="实验结构">
          <button aria-pressed={section === 'dataset'} onClick={() => setSection('dataset')}><FileText size={18} /><strong>评测条件</strong><small>{number(dataset.caseCount) === undefined ? '用什么题、什么资料来测' : `${dataset.caseCount} 个案例 · ${text(dataset.split, '划分未记录')}`}</small></button>
          <ArrowRight className="lab-experiment-structure__arrow" size={17} aria-hidden="true" />
          <button aria-pressed={section === 'changes'} onClick={() => setSection('changes')}><span className="lab-visual-pair">基线 <ArrowRight size={15} /> 候选</span><strong>方案与改动</strong><small>{factors.length ? `${factors.length} 项改动` : '改了什么、为什么改'}</small></button>
          <ArrowRight className="lab-experiment-structure__arrow" size={17} aria-hidden="true" />
          <button aria-pressed={section === 'metrics'} onClick={() => setSection('metrics')}><Check size={18} /><strong>指标与判定</strong><small>结果好不好、是否采用</small></button>
        </div>
        <section className="lab-experiment-inspect" aria-label={section === 'metrics' ? '指标与判定详情' : section === 'dataset' ? '评测条件详情' : '方案与改动详情'}>
          {section === 'metrics' ? <><h4>基线与候选的实际指标</h4><MetricComparison baseline={object(baseline.metrics)} candidate={object(candidate.metrics)} /><p>{text(comparison.decisionReason ?? record.whyContinue, '这份记录未提供判定理由。')}</p></>
            : section === 'dataset' ? <><h4>评测集与固定条件</h4><JsonTree value={dataset} />{list(record.frozenControls).length ? <JsonTree value={record.frozenControls} /> : <p className="lab-visual-note">未记录冻结条件，不据此推断实验可比。</p>}</>
              : <><h4>从基线到候选，改了什么</h4>{factors.length ? <ol className="lab-change-list">{factors.map((factor, index) => <li key={index}><div><span className="lab-change-list__index">{index + 1}</span><h5>{text(factor.layer ?? factor.name, '方案调整')}</h5></div><div className="lab-change-pair"><div><small>改动前</small><Value value={factor.before} /></div><ArrowRight size={16} aria-hidden="true" /><div><small>改动后</small><Value value={factor.after} /></div></div><p>{text(factor.reason, '原记录未提供修改原因。')}</p></li>)}</ol> : <p className="lab-visual-empty">未记录逐步改动，可展开配置查看两个方案。</p>}
                <details><summary>两个方案的配置</summary><div className="lab-change-pair"><div><h5>基线</h5><JsonTree value={Object.fromEntries(Object.entries(baseline).filter(([key]) => !['metrics', 'evidenceRefs'].includes(key)))} /></div><ArrowRight size={16} aria-hidden="true" /><div><h5>候选</h5><JsonTree value={Object.fromEntries(Object.entries(candidate).filter(([key]) => !['metrics', 'evidenceRefs'].includes(key)))} /></div></div></details>
              </>}
        </section>
        {record.businessProblem ? <p>{text(record.businessProblem)}</p> : null}
        {object(record.continuation).failureEvidence ? <details><summary>继续优化的失败依据</summary><Value value={object(record.continuation).failureEvidence} /></details> : null}
        {Object.keys(object(record.claim)).length ? <details><summary>结论适用范围</summary><JsonTree value={record.claim} /></details> : null}
        <details className="lab-experiment-evidence"><summary>来源与原始运行引用 · {evidence.length}</summary>{evidence.length ? <ul>{evidence.map((ref) => <li key={ref}><code>{ref}</code></li>)}</ul> : <p className="lab-visual-note">未记录来源引用。</p>}</details>
      </div>
    </div>
  </div>;
}

/** Table columns are the published contract. Other tables keep their ordinary renderer. */
export function ExperimentTableVisual({ content }: { content: ArtifactTable }) {
  const [selected, setSelected] = useState('');
  const columns = new Set(content.columns.map((column) => column.key));
  if (['experiment', 'metric', 'baseline', 'candidate'].every((key) => columns.has(key))) {
    const names = [...new Set(content.rows.map((row) => text(row.experiment)))];
    const name = names.includes(selected) ? selected : names[0];
    const rows = content.rows.filter((row) => text(row.experiment) === name);
    return <section className="lab-experiment-table-visual" aria-label="实验指标图"><label>选择实验<select value={name ?? ''} onChange={(event) => setSelected(event.target.value)}>{names.map((item) => <option key={item}>{item}</option>)}</select></label><MetricComparison key={name} baseline={Object.fromEntries(rows.map((row) => [text(row.metric), row.baseline]))} candidate={Object.fromEntries(rows.map((row) => [text(row.metric), row.candidate]))} /></section>;
  }
  if (['experimentId', 'comparedTo', 'decision', 'whyContinue'].every((key) => columns.has(key))) return <section className="lab-experiment-chain" aria-label="实验过程"><ol>{content.rows.map((row, index) => <li key={text(row.experimentId, String(index))}><span aria-hidden="true">{index + 1}</span><div><h3>{text(row.experiment, text(row.experimentId))}</h3><p>{decisionText(row.decision)} · 直接对照：{text(row.comparedTo)}</p><p>{text(row.whyContinue)}</p>{row.failureEvidence ? <details><summary>失败依据与指标变化</summary><Value value={row.failureEvidence} /><Value value={row.metricDeltas} /></details> : null}</div></li>)}</ol></section>;
  return null;
}

function ProjectProgress({ data }: { data: RecordValue }) {
  const states: Record<string, string> = { pending: '待进行', active: '进行中', completed: '已有结果', blocked: '需要补充' };
  return <section className="lab-project-progress" aria-label="项目步骤"><p className="lab-visual-note">Agent 保存的步骤快照{typeof data.observedAt === 'string' ? ` · ${data.observedAt}` : ''}。实时执行状态以“运行”中的回执为准。</p><ol>{list(data.steps).map(object).map((step, index) => <li key={text(step.id, String(index))} data-state={text(step.state)}><span aria-hidden="true">{step.state === 'completed' ? <Check size={16} /> : index + 1}</span><div><h3>{text(step.title, `步骤 ${index + 1}`)}<small>{states[text(step.state)] ?? '状态未记录'}</small></h3><p>{text(step.summary, '尚无步骤说明')}</p>{list(step.dependsOn).length ? <p className="lab-visual-note">依赖：{list(step.dependsOn).join('、')}</p> : null}{list(step.evidenceRefs).length ? <details><summary>结果与来源</summary><Value value={step.evidenceRefs} /></details> : null}{step.nextAction ? <p>下一步：{text(step.nextAction)}</p> : null}</div></li>)}</ol></section>;
}
