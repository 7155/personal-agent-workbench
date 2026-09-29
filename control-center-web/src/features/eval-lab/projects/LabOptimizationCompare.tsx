import { LabParallelTests } from './LabParallelTests';
import { OptimizationObjectivePicker } from './OptimizationObjectivePicker';
import { preferenceWeights } from './optimization-weights';
import { useEffect, useState } from 'react';
import { ArrowLeft, ArrowRight, ArrowUpRight, Check, CircleHelp, SlidersHorizontal } from 'lucide-react';
import { Button } from '@/components/primitives';
import { LabWorkflowNodeDetail, workflowDecisionName, workflowNodeStatus } from './LabWorkflowGraph';
import { optimizationDraft, optimizationGroups, optimizationObjectives, testedDimensions, type OptimizationDimension, type OptimizationPreference, type OptimizationObjective } from './optimization-parameters';
import type { LabWorkflowMetric, LabWorkflowNode } from './project-workflow-types';
import { decisionGroup, metricIsCost, metricIsLatency, pairedMetricVisual, type DecisionGroup } from './comparison-metrics';
import './lab-optimization-compare.css';

const metricLabels: Record<string, string> = { agentSuccessRate: '任务通过率', taskSuccessRate: '任务通过率', taskSuccessCount: '通过任务数', businessCheckPassCount: '通过业务检查', verifierPassCount: '通过业务检查', verifierPassRate: '业务检查通过率', apiCostUsd: '估算成本', costUsd: '估算成本', totalTokens: 'Token 用量', mrr: 'MRR', passRate: '通过率', recallAtK: 'Recall@K' };
function metricName(metric: LabWorkflowMetric) { return metricLabels[metric.label] ?? metric.label; }
function valueText(value: number | null | undefined, metric: LabWorkflowMetric) {
  if (value == null || !Number.isFinite(value)) return '未测量';
  if (metric.unit === 'ratio' || /Rate$/.test(metric.label)) return `${Number((value * 100).toFixed(2))}%`;
  if (metric.unit === 'USD' || /costUsd/i.test(metric.label)) return `$${Number(value.toFixed(6))}`;
  return `${Number(value.toFixed(4)).toLocaleString()}${metric.unit ? ` ${metric.unit}` : ''}`;
}
function MetricPair({ metric, compact = false }: { metric: LabWorkflowMetric; compact?: boolean }) {
  const visual = pairedMetricVisual(metric);
  return <span className="lab-compare-metric" data-compact={compact}>
    <small>{metricName(metric)}{metric.sampleCount !== undefined ? ` · n=${metric.sampleCount}` : ''}</small>
    {metric.value != null ? <strong>{valueText(metric.value, metric)}{!compact ? <small>单次结果 · 未配对</small> : null}</strong>
      : <><span className="lab-compare-metric__values"><span>{valueText(metric.baseline, metric)}</span><ArrowRight size={12} aria-label="对比" /><strong>{valueText(metric.candidate, metric)}</strong></span>
        {visual?.beforeWidth !== undefined && visual.afterWidth !== undefined ? <span className="lab-metric-bars" aria-hidden="true"><i style={{ transform: `scaleX(${visual.beforeWidth / 100})` }} /><i style={{ transform: `scaleX(${visual.afterWidth / 100})` }} /></span> : null}
        {visual ? <span className="lab-metric-delta">{visual.delta}</span> : null}</>}
  </span>;
}
function Decision({ node }: { node: LabWorkflowNode }) {
  const rawDecision = node.status === 'completed' ? node.decision?.toLowerCase() : undefined;
  const decision = rawDecision === 'improved' ? 'keep' : rawDecision;
  return <span className="lab-compare-decision" data-decision={decision}>{decision === 'keep' ? <Check size={13} /> : null}{node.status === 'completed' ? decision === 'baseline' ? '基线' : workflowDecisionName(node.decision) : workflowNodeStatus(node)}</span>;
}

export function LabOptimizationCompare({ nodes, selected, onSelect, onOpenNode, onOpenSource, onDraft, onOpenKnowledge, preferenceKey }: {
  nodes: LabWorkflowNode[]; selected?: LabWorkflowNode; onSelect: (node?: LabWorkflowNode) => void;
  onOpenNode: (node: LabWorkflowNode) => void; onOpenSource?: (node: LabWorkflowNode) => void;
  onDraft?: (text: string) => void; onOpenKnowledge?: () => void; preferenceKey?: string;
}) {
  const [dimension, setDimension] = useState<OptimizationDimension | 'all'>('all');
  const [parametersOpen, setParametersOpen] = useState(false);
  const [parametersMounted, setParametersMounted] = useState(false);
  const showParameters = () => { setParametersMounted(true); setParametersOpen(true); };
  const [search, setSearch] = useState('');
  const [decision, setDecision] = useState<DecisionGroup | 'all'>('all');
  const [preference, setPreference] = useState<OptimizationPreference>(() => {
    try { const value = JSON.parse(sessionStorage.getItem(`paw.lab.objective.v1:${preferenceKey}`) ?? 'null'); if (preferenceKey && value && optimizationObjectives.some((item) => item.id === value.objective) && typeof value.target === 'string' && typeof value.guardrail === 'string') return { ...value, weights: preferenceWeights(value.objective, value.weights) }; } catch { /* Start with an editable quality goal. */ }
    return { objective: 'quality', target: '', guardrail: '' };
  });
  useEffect(() => { if (preferenceKey) { try { sessionStorage.setItem(`paw.lab.objective.v1:${preferenceKey}`, JSON.stringify(preference)); } catch { /* The in-memory draft remains available. */ } } }, [preference, preferenceKey]);
  const experiments = nodes.filter((node) => node.kind === 'experiment');
  const visible = experiments.filter((node) => (dimension === 'all' || testedDimensions(node).includes(dimension)) && (decision === 'all' || decisionGroup(node) === decision) && `${node.title} ${node.summary} ${node.factors?.map((factor) => factor.name).join(' ') ?? ''}`.toLowerCase().includes(search.toLowerCase()));
  return <section className="lab-compare" aria-label="优化对比工作面">
    <header className="lab-compare-heading"><div><h2>{parametersOpen ? '准备下一轮优化' : selected ? '单轮对比' : '优化对比'}</h2><p>{parametersOpen ? '目标与约束已沿用，选择一个主要改动。' : selected ? '查看本轮收益、代价与具体改动。' : '先看结果，再决定下一轮改哪里。'}</p></div><Button onClick={() => parametersOpen ? setParametersOpen(false) : showParameters()} aria-expanded={parametersOpen}><SlidersHorizontal size={15} />{parametersOpen ? '返回结果对比' : '选择优化参数'}</Button></header>
    <LabParallelTests nodes={nodes} onOpen={onOpenNode} />
    <OptimizationObjectivePicker preference={preference} onChange={setPreference} />
    {parametersMounted ? <div hidden={!parametersOpen}><LabOptimizationParameters preference={preference} initialDimension={dimension === 'all' ? 'Embedding' : dimension} onDraft={onDraft} onOpenKnowledge={onOpenKnowledge} selected={selected} /></div> : null}
    {!parametersOpen ? <>
    {selected?.kind === 'experiment' ? <>
      <div className="lab-compare-detail-heading"><Button size="small" onClick={() => onSelect()}><ArrowLeft size={14} />全部优化</Button><span>{selected.source === 'runtime' ? '执行器记录' : '历史记录 · 未重新运行'}</span></div>
      <section className="lab-compare-detail" aria-label="单轮优化对比"><header><h3>{selected.title}</h3><Decision node={selected} /></header>
        <p>{selected.summary || '此轮尚未记录说明。'}</p>
        <section aria-label="本轮结果"><h4>结果如何变化</h4><p className="lab-compare-note">仅展示本轮回执中的基线与候选；不自动与上一轮或其他评测集相减。</p><OptimizationTradeoff node={selected} objective={preference.objective} /><div className="lab-compare-metrics">{selected.metrics?.length ? selected.metrics.map((metric, index) => <MetricPair key={index} metric={metric} />) : <p>尚无可比较指标，完成状态不代表效果提升。</p>}</div></section>
        <section aria-label="改动前后"><h4>改了什么</h4>{selected.factors?.length ? selected.factors.map((factor, index) => <article className="lab-compare-factor" key={index}><h5>{factor.name}</h5><div><section><small>修改前</small><pre>{factor.before || '未记录基线配置'}</pre></section><section><small>修改后 / 本次配置</small><pre>{factor.after || '未记录候选配置'}</pre></section></div>{factor.reason ? <p>{factor.reason}</p> : null}</article>) : <p>这条记录没有保存逐项参数差异。请从原始运行核对，不根据标题推断改动。</p>}</section>
        <section className="lab-compare-outcome"><h4>结果与判定依据</h4>{selected.reasons?.length ? <ul>{selected.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul> : <p>尚未记录判定理由。</p>}<Button onClick={() => onOpenNode(selected)}>查看逐题结果与原始运行<ArrowUpRight size={14} /></Button></section>
        <details className="lab-compare-original"><summary>来源、关联任务与完整记录</summary><LabWorkflowNodeDetail selected={selected} nodes={nodes} onSelect={onSelect} onOpenNode={onOpenNode} onOpenSource={onOpenSource} /></details>
        {onDraft ? <CandidateDraft key={selected.id} preference={preference} dimension={testedDimensions(selected)[0] ?? 'Prompt'} selected={selected} onDraft={onDraft} /> : null}
      </section>
    </> : <>
      <OptimizationDecisionSummary nodes={experiments} selected={decision} onSelect={setDecision} />
      <div className="lab-compare-tools"><label>筛选手段<select value={dimension} onChange={(event) => setDimension(event.target.value as OptimizationDimension | 'all')}><option value="all">全部手段 · {experiments.length} 轮</option>{optimizationGroups.map((group) => <option key={group.id} value={group.id}>{group.title} · {experiments.filter((node) => testedDimensions(node).includes(group.id)).length} 轮</option>)}</select></label><label>查找实验<input type="search" placeholder="实验名称或参数" value={search} onChange={(event) => setSearch(event.target.value)} /></label></div>
      <div className="lab-compare-legend"><span><i />基线</span><span><i />候选</span><span>同一指标、同一尺度；条长不用于跨实验排名</span></div>
      <div className="lab-compare-table" data-objective={preference.objective} role="region" aria-label="优化轮次对照表" tabIndex={0}><table><caption>每行对应一轮实际记录。基线 → 候选；未测量不记为 0，不同评测集不合并排名。</caption><thead><tr><th>方案 / 手段</th><th>本轮改动</th><th>质量与效果</th><th>成本与耗时</th><th>结论</th></tr></thead><tbody>{visible.map((node) => {
        const cost = (metric: LabWorkflowMetric) => metricIsCost(metric) || metricIsLatency(metric) || /token|用量/i.test(metric.label);
        const priority = (metric: LabWorkflowMetric) => /taskSuccessRate|agentSuccessRate|通过率|成功率|passRate/i.test(metric.label) ? 0 : /recall|mrr|ndcg|verifierPass|通过业务|taskSuccessCount/i.test(metric.label) ? 1 : 2;
        const quality = node.metrics?.filter((metric) => !cost(metric)).sort((a, b) => priority(a) - priority(b)) ?? [];
        const resources = node.metrics?.filter(cost).sort((a, b) => Number(preference.objective === 'latency' ? metricIsLatency(b) : metricIsCost(b)) - Number(preference.objective === 'latency' ? metricIsLatency(a) : metricIsCost(a))) ?? [];
        return <tr key={node.id}><th scope="row"><button onClick={() => onSelect(node)}>{node.title}<ArrowUpRight size={13} /></button><small>{testedDimensions(node).join(' · ') || '手段未记录'}</small><small>{node.decision === 'baseline' ? '单次基线记录' : workflowNodeStatus(node)}</small></th><td>{node.factors?.length ? <>{node.factors.slice(0, 2).map((factor, index) => <div className="lab-compare-change" key={index}><strong>{factor.name}</strong><span>{factor.before || '未记录'} → {factor.after || '未记录'}</span></div>)}{node.factors.length > 2 ? <small>另有 {node.factors.length - 2} 项，打开查看</small> : null}</> : <span className="lab-compare-note">未记录参数差异</span>}</td><td>{quality.length ? quality.slice(0, 1).map((metric, index) => <MetricPair key={index} metric={metric} compact />) : '未测量'}{quality.length > 1 ? <small>详情中还有 {quality.length - 1} 项</small> : null}</td><td>{resources.length ? resources.slice(0, 1).map((metric, index) => <MetricPair key={index} metric={metric} compact />) : '未测量'}</td><td><Decision node={node} /></td></tr>;
      })}</tbody></table></div>
      <details className="lab-compare-methods"><summary>实现手段与已测覆盖 · 模型、Embedding、检索、Prompt 等</summary><OptimizationDirectionMap experiments={experiments} dimension={dimension} onSelect={(next) => { setDimension(next); setDecision('all'); onSelect(); }} /></details>
      {!visible.length ? <div className="lab-compare-empty"><CircleHelp size={22} /><h3>{experiments.length ? '没有匹配的实验' : '还没有优化对照'}</h3><p>{dimension === 'all' ? '选择一个参数方向，固定基线与评测标准，再准备第一轮候选。' : `${dimension} 尚无匹配记录。可以先查看参数和配置入口。`}</p><Button onClick={() => { setDimension('all'); setDecision('all'); setSearch(''); }}>清除筛选</Button><Button onClick={showParameters}>查看可优化的参数</Button></div> : null}
    </>}</> : null}
  </section>;
}

function OptimizationDecisionSummary({ nodes, selected, onSelect }: { nodes: LabWorkflowNode[]; selected: DecisionGroup | 'all'; onSelect: (value: DecisionGroup | 'all') => void }) {
  const groups: { id: DecisionGroup; label: string }[] = [{ id: 'baseline', label: '基线' }, { id: 'keep', label: '保留' }, { id: 'reject', label: '未保留' }, { id: 'pending', label: '待判定' }];
  return <section className="lab-decision-summary" aria-label="实验结论分布"><div><button aria-pressed={selected === 'all'} onClick={() => onSelect('all')}>全部 {nodes.length} 轮</button>{groups.map((group) => { const count = nodes.filter((node) => decisionGroup(node) === group.id).length; return <button key={group.id} aria-pressed={selected === group.id} onClick={() => onSelect(group.id)} data-group={group.id}><i aria-hidden="true" />{group.label} <strong>{count}</strong></button>; })}</div><div className="lab-decision-summary__bar" aria-hidden="true">{groups.map((group) => { const count = nodes.filter((node) => decisionGroup(node) === group.id).length; return count ? <span key={group.id} data-group={group.id} style={{ flex: count }} /> : null; })}</div></section>;
}

function OptimizationTradeoff({ node, objective }: { node: LabWorkflowNode; objective: OptimizationObjective }) {
  const paired = (node.metrics ?? []).filter((metric) => metric.value == null && metric.baseline != null && metric.candidate != null && Number.isFinite(metric.baseline) && Number.isFinite(metric.candidate));
  const quality = paired.find((metric) => /通过率|成功率|passRate|SuccessRate|recall|mrr|ndcg/i.test(metric.label));
  const resource = paired.find((metric) => objective === 'latency' ? /latency|耗时|延迟/i.test(metric.label) : /cost|费用|成本/i.test(metric.label) || metric.unit === 'USD');
  if (!quality || !resource) return <p className="lab-compare-note">取舍图需要本轮配对的质量与{objective === 'latency' ? '耗时' : '成本'}指标；当前记录不全，暂不绘制。</p>;
  const left = resource.baseline!; const right = resource.candidate!; const before = quality.baseline!; const after = quality.candidate!;
  if (left < 0 || right < 0 || before < 0 || after < 0) return null;
  const ratio = quality.unit === 'ratio' || /Rate$|率/.test(quality.label);
  if (ratio && (before > 1 || after > 1)) return <p className="lab-compare-note">本轮比例超出有效范围，暂不绘制取舍图。</p>;
  const maxX = Math.max(left, right, Number.EPSILON) * 1.2;
  const maxY = ratio ? Math.max(1, before, after) : Math.max(before, after, Number.EPSILON) * 1.2;
  const x = (v: number) => 62 + v / maxX * 320;
  const y = (v: number) => 160 - v / maxY * 115;
  return <figure className="lab-tradeoff"><svg viewBox="0 0 460 205" role="img" aria-label={`${metricName(quality)}与${metricName(resource)}的取舍：基线 ${valueText(before, quality)} / ${valueText(left, resource)}，候选 ${valueText(after, quality)} / ${valueText(right, resource)}`}><path d="M62 28V160H424" fill="none" stroke="currentColor" opacity=".25" />{[.5, 1].map((fraction) => <g key={fraction}><line x1="62" x2="424" y1={y(maxY * fraction)} y2={y(maxY * fraction)} stroke="currentColor" opacity=".1" /><text x="54" y={y(maxY * fraction) + 3} textAnchor="end">{valueText(maxY * fraction, quality)}</text></g>)}{[.5, 1].map((fraction) => <text key={fraction} x={x(maxX * fraction)} y="176" textAnchor="middle">{valueText(maxX * fraction, resource)}</text>)}<text x="64" y="18">{metricName(quality)} ↑</text><text x="258" y="193">← 更低的{metricName(resource)}</text><text x="43" y="174">0</text><line x1={x(left)} y1={y(before)} x2={x(right)} y2={y(after)} stroke="var(--lab-accent)" strokeDasharray="4 4" /><circle cx={x(left)} cy={y(before)} r="6" fill="var(--lab-muted)" /><circle cx={x(right)} cy={y(after)} r="6" fill="var(--lab-accent)" /><text x={x(left)} y={y(before) - 12} textAnchor="middle">基线</text><text x={x(right)} y={y(after) + 24} textAnchor="middle">候选</text></svg><figcaption><strong>本轮效果与资源取舍</strong><span>越靠左上，质量越高、资源越少。</span><span>基线 {valueText(before, quality)} / {valueText(left, resource)}</span><span>候选 {valueText(after, quality)} / {valueText(right, resource)}</span><small>展示实际配对数值；保留结论仍以原评测标准和判定为准。</small></figcaption></figure>;
}

function OptimizationDirectionMap({ experiments, dimension, onSelect }: { experiments: LabWorkflowNode[]; dimension: OptimizationDimension | 'all'; onSelect: (dimension: OptimizationDimension | 'all') => void }) {
  const rows: OptimizationDimension[][] = [['切片', 'Embedding', 'RAG', '重排', '模型'], ['Prompt', 'Skill', 'Tool', 'MCP / Workflow']];
  return <section className="lab-direction-map" aria-label="优化手段覆盖图"><header><div><h3>通过哪些手段实现目标</h3><p>从资料到回答，查看每一层测过什么。</p></div><button aria-pressed={dimension === 'all'} onClick={() => onSelect('all')}>全部 {experiments.length} 轮</button></header>
    {rows.map((row, rowIndex) => <div className="lab-direction-map__lane" key={rowIndex}><span>{rowIndex === 0 ? '资料 → 回答' : '方法与执行'}</span><ol>{row.map((id, index) => {
      const records = experiments.filter((node) => testedDimensions(node).includes(id));
      const completed = records.filter((node) => node.status === 'completed');
      const kept = completed.filter((node) => ['keep', 'improved'].includes(node.decision?.toLowerCase() ?? '')).length;
      const rejected = completed.filter((node) => ['reject', 'no_improvement'].includes(node.decision?.toLowerCase() ?? '')).length;
      const pending = completed.length - kept - rejected;
      const running = records.filter((node) => node.source === 'runtime' && ['running', 'queued'].includes(node.status)).length;
      return <li key={id}><button aria-pressed={dimension === id} onClick={() => onSelect(id)}><strong>{id === 'MCP / Workflow' ? 'Workflow' : id === '模型' ? '回答模型' : id}</strong><span>{records.length ? `${records.length} 轮记录${running ? ` · ${running} 进行中` : ''}` : '未记录测试'}</span><small>{completed.length ? `${kept} 保留 · ${rejected} 未保留${pending ? ` · ${pending} 待判定` : ''}` : records.length ? '等待完成对照' : '查看参数，准备候选'}</small><span className="lab-direction-map__segments" aria-hidden="true">{kept > 0 ? <i data-state="keep" style={{ flex: kept }} /> : null}{rejected > 0 ? <i data-state="reject" style={{ flex: rejected }} /> : null}{pending > 0 ? <i style={{ flex: pending }} /> : null}</span></button>{rowIndex === 0 && index < row.length - 1 ? <ArrowRight size={13} aria-hidden="true" /> : null}</li>;
    })}</ol></div>)}<p className="lab-direction-map__legend">线条表示处理顺序；色段表示本方向的历史判定数量，不是提升比例。一轮涉及多个方向时会分别计入。</p>
  </section>;
}

function CandidateDraft({ dimension, selected, onDraft, preference }: { dimension: OptimizationDimension; selected?: LabWorkflowNode; onDraft: (text: string) => void; preference?: OptimizationPreference }) {
  const [request, setRequest] = useState(''); const [sent, setSent] = useState(false);
  return <form className="lab-compare-draft" onSubmit={(event) => { event.preventDefault(); if (!request.trim()) return; onDraft(optimizationDraft(dimension, request, selected, preference)); setSent(true); }}><label>下一轮想改什么<textarea rows={2} value={request} onChange={(event) => { setRequest(event.target.value); setSent(false); }} placeholder="例如：固定切片和题集，比较另一个 Embedding 模型的召回效果" /></label><div><span>准备草稿 → 核对差异 → 运行对比 → 保留或放弃</span><Button variant="primary" type="submit" disabled={!request.trim()}>带入 Agent 草稿</Button></div>{sent ? <p role="status">已带入项目 Agent 输入框，尚未发送或执行。</p> : null}</form>;
}

export function LabOptimizationParameters({ initialDimension = 'Embedding', selected, onDraft, onOpenKnowledge, preference }: { initialDimension?: OptimizationDimension; selected?: LabWorkflowNode; onDraft?: (text: string) => void; onOpenKnowledge?: () => void; preference?: OptimizationPreference }) {
  const [dimension, setDimension] = useState(initialDimension);
  const [advanced, setAdvanced] = useState(false);
  const group = optimizationGroups.find((item) => item.id === dimension)!;
  return <section className="lab-parameters" aria-label="优化参数指南"><header><h3>从问题选择参数</h3><p>先固定评测集和通过标准，每轮只改一个主要因素。</p></header><div className="lab-parameters-layout"><nav aria-label="参数分组">{optimizationGroups.map((item) => <button key={item.id} aria-pressed={item.id === dimension} onClick={() => setDimension(item.id)}>{item.title}</button>)}</nav><div className="lab-parameters-content"><h4>{group.question}</h4><p>观察：{group.measure}</p><label className="lab-parameters-advanced"><input type="checkbox" checked={advanced} onChange={(event) => setAdvanced(event.target.checked)} />显示按需高级项</label><dl>{group.parameters.filter((parameter) => advanced || !parameter.advanced).map((parameter) => <div key={parameter.name}><dt>{parameter.name}<small>{parameter.advanced ? '按需测试' : '优先检查'}</small></dt><dd><p>{parameter.purpose}</p><span>{parameter.access}</span></dd></div>)}</dl>{group.route === 'knowledge' && onOpenKnowledge ? <Button size="small" onClick={onOpenKnowledge}>打开知识库实验<ArrowUpRight size={14} /></Button> : null}{onDraft ? <CandidateDraft key={dimension} preference={preference} dimension={dimension} selected={selected} onDraft={onDraft} /> : null}</div></div><details className="lab-parameters-sources"><summary>参数选择依据</summary><p>候选范围取决于当前资料与任务，这里不预设最优参数。</p><a href="https://www.sbert.net/examples/sentence_transformer/applications/semantic-search/README.html" target="_blank" rel="noreferrer">Sentence Transformers：模型与查询编码</a><a href="https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion" target="_blank" rel="noreferrer">Elastic：混合检索与候选窗口</a><a href="https://docs.cohere.com/reference/rerank" target="_blank" rel="noreferrer">Cohere：重排与截断参数</a></details></section>;
}
