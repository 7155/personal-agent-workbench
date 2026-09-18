import { useQuery } from '@tanstack/react-query';
import { useControlTransport } from '@/app/control-transport';
import { Button } from '@/components/primitives';
import { labConnectionKey, requestLabControl } from '../control-request';
import { projectError } from './api';
import { object } from './types';
import { LabRetrievalRun, LabRetrievalHitMetrics } from './LabRetrievalEvidence';

/** Exact public owner read, including jobs no longer in the recent project list. */
export function LabKnowledgeRecord({ projectId, jobId, onClose, onOpenKnowledge }: { projectId: string; jobId: string; onClose?: () => void; onOpenKnowledge?: () => void }) {
  const transport = useControlTransport();
  const query = useQuery({ queryKey: ['lab-knowledge-record', labConnectionKey(transport), projectId, jobId],
    queryFn: async ({ signal }) => {
      const raw = object(await requestLabControl(transport, { pathId: 'agent.eval-lab.trials.get', query: { jobId }, signal }));
      const job = object(raw.job); const spec = object(job.publicSpec);
      if (raw.schemaVersion !== 'rag-ime.agent-lab-trial.v1' || job.jobId !== jobId || job.sceneId !== 'knowledge-resource'
        || spec.projectId !== projectId || typeof job.state !== 'string' || typeof spec.operation !== 'string') throw new Error('原任务身份与当前项目不匹配，未替换为其他记录。');
      return job;
    }, retry: false, refetchInterval: (query) => ['queued', 'running', 'cancelling'].includes(String(query.state.data?.state)) ? 1500 : false });
  const job = query.data; const result = object(job?.result); const spec = object(job?.publicSpec);
  const metrics = object(object(object(result.report).metrics).metrics);
  const profile = object(result.profile ?? spec.profile);
  const hits = Array.isArray(result.hits) ? result.hits.map(object) : [];
  const state = ({ queued: '排队中', running: '运行中', cancelling: '正在停止', completed: '已完成', failed: '失败', interrupted: '已中断', cancelled: '已取消' } as Record<string, string>)[String(job?.state)] ?? '尚未返回';
  const metric = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 4 }) : '未提供';
  return <aside className="lab-space-source lab-knowledge-record" aria-label="原知识库任务记录"><header><h3>原知识库任务 · {state}</h3><div>{onOpenKnowledge ? <Button size="small" onClick={onOpenKnowledge}>在知识库中查看</Button> : null}{onClose ? <Button size="small" onClick={onClose}>收起原任务</Button> : null}</div></header><p>原任务：{jobId}</p>
    {query.isPending ? <p role="status">正在读取原任务…</p> : query.isError ? <><p role="alert">{projectError(query.error)}</p><Button onClick={() => void query.refetch()}>重新读取原任务</Button></> : <>
      <p>{String(job?.progress || job?.error || result.message || '已读取原执行记录；查看不会重新运行。')}</p>
      {typeof result.query === 'string' ? <p>实际检索问题：{result.query}</p> : null}
      {spec.operation === 'search' || spec.operation === 'evaluate' ? <LabRetrievalRun result={result} recordedProfile={profile} /> : null}
      {Object.keys(metrics).length ? <section aria-label="本次检索指标"><h4>这次找资料的效果</h4><p>这是单次检索结果，只说明找资料的表现；还没有和另一套配置做对照。</p><dl><dt>平均首位命中（MRR）</dt><dd>{metric(metrics.mrr)}</dd>{['recallAtK', 'ndcgAtK'].map((name) => Object.entries(object(metrics[name])).map(([k, value]) => <div key={`${name}:${k}`}><dt>{name === 'recallAtK' ? `找到相关资料的比例（Recall@${k}）` : `相关资料排序质量（nDCG@${k}）`}</dt><dd>{metric(value)}</dd></div>))}</dl></section> : null}
      {hits.length ? <section aria-label="本次实际检索来源"><h4>实际命中的来源</h4>{hits.map((hit, index) => <article key={String(hit.chunkId ?? index)}><h5>{String(hit.title || hit.sourceId || '来源')}</h5><LabRetrievalHitMetrics hit={hit} index={index} /><p className="golden-preserve-text">{typeof hit.content === 'string' ? hit.content : '此记录未返回正文。'}</p><small>{String(hit.sourceId || '')} · {String(hit.chunkId || '')}</small>{typeof hit.uri === 'string' ? <p>{hit.uri}</p> : null}</article>)}</section> : null}
      <details><summary>查看完整公开回执与配置</summary><pre className="golden-preserve-text">{JSON.stringify(job, null, 2)}</pre></details>
    </>}
  </aside>;
}
