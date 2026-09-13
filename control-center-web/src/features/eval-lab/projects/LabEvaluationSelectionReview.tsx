import { useQuery } from '@tanstack/react-query';
import { useEffect } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button } from '@/components/primitives';
import { labConnectionKey } from '../control-request';
import { getGoldenSuites } from '../golden/api';
import { isExperimentResult } from '../golden/types';
import { isApplicationMethod } from '../golden/application-method';
import { ExperimentReport } from '../golden/Experiment';
import type { LabEvaluationSelection } from './apps';
import { projectError } from './api';

export const evaluationSelectionKey = (selection?: LabEvaluationSelection) => selection ? `${selection.suiteId}:${selection.jobId}:${selection.variant}` : '';

export function LabEvaluationSelectionReview({ selection, onVerified, onClear }: { selection: LabEvaluationSelection; onVerified: (key: string) => void; onClear?: () => void }) {
  const transport = useControlTransport();
  const query = useQuery({ queryKey: ['lab-delivery-selection', labConnectionKey(transport), selection.suiteId, selection.jobId],
    queryFn: ({ signal }) => getGoldenSuites(transport, selection.suiteId, signal), retry: false });
  const suite = query.data?.suite;
  const job = suite?.suiteId === selection.suiteId ? suite.jobs.find((item) => item.jobId === selection.jobId) : undefined;
  const result = job?.kind === 'experiment' && job.state === 'completed' && isExperimentResult(job.result) && job.result.suiteId === selection.suiteId ? job.result : undefined;
  const config = result?.[selection.variant];
  const method = isApplicationMethod(config?.applicationMethod) ? config.applicationMethod : undefined;
  const key = evaluationSelectionKey(selection);
  useEffect(() => { onVerified(!query.isError && result && method ? key : ''); }, [key, result, method, query.isError, onVerified]);
  return <section className="lab-app-configuration" aria-label="待交付的评测方案"><header><h3>已选择{selection.variant === 'baseline' ? '基线' : '候选'}用于交付</h3>{onClear ? <Button type="button" size="small" onClick={onClear}>取消本次选择</Button> : null}</header><p>原实验：{selection.jobId}</p>
    {query.isPending ? <p role="status">正在核对原完成回执…</p> : query.isError ? <><p role="alert">{projectError(query.error)}</p><Button type="button" onClick={() => void query.refetch()}>重新核对评测来源</Button></> : !result || !method ? <p role="alert">所选完成回执或冻结应用方法尚未完整返回，不能据此准备新版本；未替换为其他实验。</p> : <>
      <dl><div><dt>冻结模型</dt><dd>{config!.provider} / {config!.model}</dd></div><div><dt>应用方法</dt><dd>{method.title}<small>SHA {method.sha256}</small></dd></div><div><dt>评测快照</dt><dd>{result.snapshotId}</dd></div><div><dt>原判定</dt><dd>{result.comparison.decision}</dd></div></dl>
      {result.knowledge ? <p>本轮冻结知识库：{result.knowledge.documentCount.toLocaleString()} 篇 · {String(result.knowledge.profile.mode)} · Top-K {String(result.knowledge.profile.topK ?? '未返回')} · 索引 {result.knowledge.indexId}</p> : result.executionMode === 'knowledge_qa' ? <p>原回执未完整返回冻结检索配置，请在原评测中核对；这里不使用当前配置代替。</p> : null}
      <details><summary>审查原评测、双方 Prompt、指标与逐题来源</summary><ExperimentReport result={result} sources={suite!.sources} /></details>
    </>}
  </section>;
}
