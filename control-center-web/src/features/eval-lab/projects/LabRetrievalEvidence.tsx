import { object } from './types';
import './lab-retrieval-evidence.css';

const number = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? String(Number(value.toFixed(6))) : '未报告';
const mode = (value: unknown) => ({ lexical: '关键词', dense: '语义', hybrid: '混合' }[String(value)] ?? '未报告');

/** Only display the recorded run, never the current editable profile. */
export function LabRetrievalRun({ result, recordedProfile }: { result: Record<string, unknown>; recordedProfile?: unknown }) {
  const profile = object(result.profile ?? recordedProfile);
  const retrieval = object(result.retrieval);
  const nestedRerank = object(retrieval.rerank);
  const reranker = typeof nestedRerank.enabled === 'boolean' ? nestedRerank : object(result.reranker);
  const config = object(retrieval.config);
  const rerankState = reranker.enabled === true ? '本次已重排' : reranker.enabled === false ? '本次未重排' : '本次是否重排未报告';
  return <section className="lab-retrieval-evidence" aria-label="本次召回参数与执行">
    <h4>本次召回参数与执行</h4>
    <dl>
      <div><dt>检索方式</dt><dd>{mode(profile.mode)} · 实际 {mode(retrieval.effectiveMode)}</dd></div>
      <div><dt>Top K / 分数阈值</dt><dd>{number(profile.topK)} / {number(profile.threshold)}</dd></div>
      <div><dt>重排候选数 / 上下文字符上限</dt><dd>{number(profile.candidateDepth)} / {number(profile.contextChars)}</dd></div>
      <div><dt>Reranker 请求</dt><dd>{profile.rerank === true ? '开启' : profile.rerank === false ? '关闭' : '未报告'}</dd></div>
      <div><dt>Reranker 执行</dt><dd>{rerankState}{typeof reranker.provider === 'string' ? ` · ${reranker.provider}` : ''}{typeof reranker.model === 'string' ? ` / ${reranker.model}` : ''}</dd></div>
      <div><dt>各路候选数（可重叠）</dt><dd>关键词 {number(retrieval.lexicalCandidates)} · 向量 {number(retrieval.denseCandidates)} · 图谱 {number(retrieval.graphCandidates)}</dd></div>
      <div><dt>每路候选上限</dt><dd>{number(retrieval.candidateLimit)}</dd></div>
      <div><dt>RRF / 候选倍数</dt><dd>{number(config.rrfK)} / {number(config.candidateMultiplier)}</dd></div>
      {Object.keys(config).length ? <div><dt>关键词 / 向量 / 图谱权重</dt><dd>{number(config.lexicalWeight)} / {number(config.denseWeight)} / {config.graphEnabled === false ? '关闭' : number(config.graphWeight)}</dd></div> : null}
      {reranker.enabled === true ? <div><dt>文档去重</dt><dd>丢弃 {number(reranker.duplicateDocumentHitsDropped)} 条同文档命中</dd></div> : null}
    </dl>
    <p>参数来自该次执行记录。分数用于各阶段排序，不代表正确率；文档去重后，最终排名可能与重排排名不同。</p>
  </section>;
}

export function LabRetrievalHitMetrics({ hit, index }: { hit: Record<string, unknown>; index: number }) {
  const diagnostics = object(hit.diagnostics);
  return <dl className="lab-retrieval-hit-metrics" aria-label={`结果 ${index + 1} 排名与分数`}>
    <div><dt>最终排名</dt><dd>#{index + 1}</dd></div>
    <div><dt>召回排名 / 分数</dt><dd>{number(hit.rerankOriginalRank ?? diagnostics.retrievalRank)} / {number(hit.score)}</dd></div>
    <div><dt>重排排名 / 分数</dt><dd>{number(hit.rerankRank)} / {number(hit.rerankScore)}</dd></div>
    {(['lexical', 'dense', 'graph'] as const).map((channel) => diagnostics[`${channel}Rank`] != null ? <div key={channel}><dt>{{ lexical: '关键词', dense: '向量', graph: '图谱' }[channel]}排名 / 分数</dt><dd>{number(diagnostics[`${channel}Rank`])} / {number(diagnostics[`${channel}Score`])}</dd></div> : null)}
  </dl>;
}
