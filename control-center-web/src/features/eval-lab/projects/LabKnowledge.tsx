import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { ArrowRight, RefreshCw, Search, Upload } from 'lucide-react';
import { useControlTransport } from '@/app/control-transport';
import { Button } from '@/components/primitives';
import { labConnectionKey, requestLabControl } from '../control-request';
import { projectError, readLabProject } from './api';
import { object, type JsonValue, type LabBinding, type LabProject, type ProjectReceipt } from './types';
import { activeKnowledgeJob, defaultRetrieval, parseKnowledgeState, type KnowledgeEvaluation, type KnowledgeJob, type RetrievalProfile } from './knowledge-types';
import './lab-knowledge.css';
import { LabKnowledgeRecord } from './LabKnowledgeRecord';
import { LabRetrievalRun, LabRetrievalHitMetrics } from './LabRetrievalEvidence';

type Page = 'sources' | 'index' | 'evaluation';
type Draft = { page: Page; corpusId: string; datasetId: string; indexId: string; sourcePath: string; datasetPath: string;
  query: string; strategy: string; size: number; overlap: number; embedding: string; count: number; profile: RetrievalProfile;
  corpusFields: Record<string, string>; datasetFields: Record<string, string>; noDataset: boolean; datasetMode: 'import' | 'agent' };
const initial: Draft = { page: 'sources', corpusId: '', datasetId: '', indexId: '', sourcePath: '', datasetPath: '', query: '',
  strategy: 'markdown', size: 1200, overlap: 160, embedding: 'none', count: 4, profile: defaultRetrieval,
  corpusFields: {}, datasetFields: {}, noDataset: false, datasetMode: 'import' };
const labels: Record<string, string> = { download_wix: '下载 WixQA', import_corpus: '整理资料', connect_base: '连接知识库',
  import_dataset: '导入评测集', restore_index: '复用已有索引', index: '建立索引', search: '试检索', evaluate: '检索评测' };
const stateLabels: Record<string, string> = { queued: '排队中', running: '进行中', cancelling: '正在停止', completed: '已完成', failed: '未完成', interrupted: '已中断', cancelled: '已停止' };
const modeLabels = { lexical: '关键词', dense: '语义', hybrid: '混合' };
const amount = (value: number) => Number.isFinite(value) ? value.toLocaleString() : '—';
const rate = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(2)}%` : '—';

export function LabKnowledge({ project, busy, onCommand, onBind, onOpenBinding, initialJobId = '' }: {
  project: LabProject; busy: boolean; initialJobId?: string;
  onCommand: (input: Record<string, JsonValue>) => Promise<ProjectReceipt | undefined>;
  onBind: (input: Record<string, JsonValue>) => Promise<ProjectReceipt | undefined>;
  onOpenBinding: (binding: LabBinding) => void;
}) {
  const transport = useControlTransport(); const connection = labConnectionKey(transport);
  const key = `paw.lab.knowledge-draft.v1:${connection}:${project.projectId}`;
  const [draft, setDraft] = useState<Draft>(() => { try { const saved = object(JSON.parse(sessionStorage.getItem(key) ?? '{}')); return { ...initial, ...saved, profile: { ...defaultRetrieval, ...object(saved.profile) } } as Draft; } catch { return initial; } });
  const [sourceFile, setSourceFile] = useState<File>(); const [datasetFile, setDatasetFile] = useState<File>();
  const [recordId, setRecordId] = useState(initialJobId);
  useEffect(() => { setRecordId(initialJobId); }, [initialJobId]);
  const [error, setError] = useState(''); const [uploadProgress, setUploadProgress] = useState('');
  const [showBases, setShowBases] = useState(false); const [baseId, setBaseId] = useState('');
  const lastJob = useRef(''); const uploadStop = useRef(false);
  const patch = (value: Partial<Draft>) => setDraft((current) => ({ ...current, ...value }));
  useEffect(() => { try { sessionStorage.setItem(key, JSON.stringify(draft)); } catch { /* Current inputs remain in this component. */ } }, [key, draft]);
  useEffect(() => () => { uploadStop.current = true; }, []);
  const query = useQuery({ queryKey: ['lab-knowledge', connection, project.projectId],
    queryFn: async ({ signal }) => parseKnowledgeState((await readLabProject(transport, project.projectId, '', undefined, signal)).knowledge),
    retry: false, refetchOnWindowFocus: true,
    refetchInterval: (query) => query.state.status === 'error' || query.state.data?.jobs.some(activeKnowledgeJob) ? 1500 : false });
  const bases = useQuery({ queryKey: ['lab-knowledge-bases', connection], enabled: showBases, retry: false,
    queryFn: async ({ signal }) => {
      const result = object(await requestLabControl(transport, { pathId: 'knowledgeBases.list', signal }));
      if (!Array.isArray(result.items)) throw new Error('已有知识库暂未完整返回。');
      return result.items.map(object).filter((row) => typeof row.id === 'string' && typeof row.name === 'string');
    } });
  const state = query.data; const active = state?.jobs.filter(activeKnowledgeJob) ?? [];
  const disabled = busy || active.length > 0 || Boolean(uploadProgress) || query.isError || !state;
  const corpus = state?.corpora.find((row) => row.jobId === draft.corpusId) ?? state?.corpora[0];
  const indexes = state?.indexes.filter((row) => row.corpusId === corpus?.jobId) ?? [];
  const index = indexes.find((row) => row.jobId === draft.indexId) ?? indexes[0];
  const datasets = state?.datasets.filter((row) => row.corpusHash === corpus?.corpusHash) ?? [];
  const dataset = datasets.find((row) => row.datasetId === draft.datasetId) ?? datasets[0];
  const searchJob = state?.jobs.find((job) => job.state === 'completed' && job.result?.kind === 'search' && job.result.indexId === index?.jobId);
  const selectedJob = state?.jobs.find((job) => job.jobId === lastJob.current);
  useEffect(() => {
    if (!selectedJob || selectedJob.state !== 'completed') return;
    lastJob.current = '';
    const result = selectedJob.result;
    if (result?.kind === 'corpus') patch({ corpusId: selectedJob.jobId, indexId: '', datasetId: '', page: 'index' });
    if (result?.kind === 'index') patch({ indexId: selectedJob.jobId });
    if (result?.kind === 'dataset') patch({ datasetId: selectedJob.jobId, noDataset: false });
  }, [selectedJob]);
  const command = async (value: Record<string, JsonValue>) => {
    setError('');
    const receipt = await onCommand(value);
    if (receipt?.job) lastJob.current = receipt.job.jobId;
    if (receipt) await query.refetch();
    return receipt;
  };
  const upload = async (file: File) => {
    if (file.size <= 0 || file.size > 300 * 1024 * 1024) throw new Error('请选择非空文件，最多 300 MB。');
    setUploadProgress(`正在读取 ${file.name}…`);
    const bytes = await file.arrayBuffer();
    const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map((value) => value.toString(16).padStart(2, '0')).join('');
    if (uploadStop.current) throw new Error('上传已停止，可以重新选择原文件继续。');
    const started = await onCommand({ operation: 'upload_begin', name: file.name, bytes: file.size, sha256: hash });
    const uploadId = started?.upload?.uploadId;
    if (!uploadId) throw new Error('上传尚未确认，请先核对原操作。');
    const chunkBytes = started.upload?.chunkBytes ?? 512 * 1024;
    for (let offset = 0; offset < bytes.byteLength; offset += chunkBytes) {
      if (uploadStop.current) throw new Error('上传已停止，可以重新选择原文件继续。');
      const part = new Uint8Array(bytes.slice(offset, offset + chunkBytes)); let binary = '';
      for (let start = 0; start < part.length; start += 8192) binary += String.fromCharCode(...part.subarray(start, start + 8192));
      setUploadProgress(`正在上传 ${file.name} · ${Math.round(offset / file.size * 100)}%`);
      if (!await onCommand({ operation: 'upload_chunk', uploadId, index: offset / chunkBytes, data: btoa(binary) })) throw new Error('上传分片尚未确认，请先核对原操作。');
    }
    if (!await onCommand({ operation: 'upload_seal', uploadId })) throw new Error('文件接收结果尚未确认，请核对原操作。');
    return uploadId;
  };
  const importFile = async (kind: 'corpus' | 'dataset') => {
    setError(''); uploadStop.current = false;
    try {
      const file = kind === 'corpus' ? sourceFile : datasetFile;
      const source: Record<string, JsonValue> = file ? { uploadId: await upload(file) } : { path: kind === 'corpus' ? draft.sourcePath : draft.datasetPath };
      await command({ operation: kind === 'corpus' ? 'import_corpus' : 'import_dataset', ...source,
        ...(kind === 'dataset' ? { corpusId: corpus!.jobId } : {}), fields: kind === 'corpus' ? draft.corpusFields : draft.datasetFields });
    } catch (reason) { setError(projectError(reason)); }
    finally { setUploadProgress(''); }
  };
  const evaluate = (split: 'development' | 'holdout') => command({ operation: 'evaluate', indexId: index!.jobId,
    datasetId: dataset!.datasetId, profile: { ...draft.profile }, split });
  const bind = async () => {
    setError('');
    const receipt = await onBind({ adapterId: 'golden.knowledge_qa', input: { indexId: index!.jobId, targetCount: draft.count,
      profile: { ...draft.profile }, ...(!draft.noDataset && dataset ? { datasetId: dataset.datasetId } : {}) } });
    if (receipt?.binding) onOpenBinding(receipt.binding);
  };
  const profile = draft.profile;
  const updateProfile = (value: Partial<RetrievalProfile>) => patch({ profile: { ...profile, ...value } });
  const evaluations = state?.evaluations.filter((row) => row.datasetHash === dataset?.sha256) ?? [];
  const validCount = Number.isInteger(draft.count) && draft.count >= 2 && draft.count <= 100;
  return <section className="lab-knowledge" aria-label="知识库实验">
    <header><div><h2>知识库实验</h2><p>整理真实资料，检查检索，再比较回答。</p></div><Button size="small" disabled={query.isFetching} onClick={() => void query.refetch()}><RefreshCw size={14} />重新读取</Button></header>
    {recordId ? <LabKnowledgeRecord projectId={project.projectId} jobId={recordId} onClose={() => setRecordId('')} /> : null}
    <nav aria-label="知识库实验步骤">{([['sources', '资料'], ['index', '索引与检索'], ['evaluation', '评测']] as const).map(([page, title]) => <button key={page} aria-current={draft.page === page ? 'step' : undefined} onClick={() => patch({ page })}>{title}</button>)}</nav>
    {query.isPending ? <p role="status">正在读取知识库实验…</p> : null}
    {query.isError ? <p role="alert" className="lab-project-error">{projectError(query.error)}</p> : null}
    {error ? <p role="alert" className="lab-project-error">{error}</p> : null}
    {uploadProgress ? <div className="lab-knowledge-status" role="status"><span>{uploadProgress}</span><Button size="small" onClick={() => { uploadStop.current = true; }}>停止上传</Button></div> : null}
    {active.map((job) => <div key={job.jobId} className="lab-knowledge-status" role="status"><div><strong>{labels[job.publicSpec.operation]} · {stateLabels[job.state]}</strong><p>{query.isError ? '当前进度尚未确认；原任务仍保留。' : job.progress || '等待执行'}</p></div><Button size="small" disabled={busy || job.state === 'cancelling'} onClick={() => void command({ operation: 'cancel', jobId: job.jobId })}>停止任务</Button></div>)}
    {!active.length && state?.jobs[0] && ['failed', 'interrupted', 'cancelled'].includes(state.jobs[0].state) ? <p role="status" className="lab-knowledge-status">{labels[state.jobs[0].publicSpec.operation]} · {stateLabels[state.jobs[0].state]}。{state.jobs[0].result?.message || '原记录已保留。检查来源或配置后，可以重新发起；不会自动重跑。'}</p> : null}
    {draft.page === 'sources' ? <div className="lab-knowledge-section">
      <h3>把资料接进来</h3><p>上传 JSONL 语料，或填写执行器上的文件夹路径。资料会保存为此项目的独立快照。</p>
      <div className="lab-knowledge-inline"><label className="lab-project-file-picker"><Upload size={15} />{sourceFile?.name ?? '选择语料文件'}<input type="file" accept=".jsonl,.ndjson" disabled={disabled} onChange={(event) => setSourceFile(event.target.files?.[0])} /></label>{sourceFile ? <Button size="small" disabled={disabled} onClick={() => setSourceFile(undefined)}>移除文件</Button> : null}</div>
      {!sourceFile ? <label>执行器上的文件夹或 JSONL 路径<input value={draft.sourcePath} placeholder="例如：/Users/you/Downloads/company-kb" disabled={disabled} onChange={(event) => patch({ sourcePath: event.target.value })} /></label> : null}
      <p className="lab-knowledge-note">最多 20,000 篇文档、300 MB。文件夹支持 TXT、Markdown 和 HTML；JSONL 每行是一篇文档，至少包含来源 ID 和正文。</p>
      <FieldMapping kind="corpus" value={draft.corpusFields} onChange={(corpusFields) => patch({ corpusFields })} disabled={disabled} />
      <div className="lab-knowledge-inline"><Button variant="primary" disabled={disabled || (!sourceFile && !draft.sourcePath.trim())} onClick={() => void importFile('corpus')}>整理资料</Button><Button disabled={disabled} onClick={() => setShowBases((value) => !value)}>连接已有知识库</Button></div>
      <p className="lab-knowledge-note">先在资料来源处下载文件，再上传语料；问题与参考答案在「评测」中单独导入。</p>
      {showBases ? <div className="lab-knowledge-existing">{bases.isPending ? <p role="status">正在读取已有知识库…</p> : bases.isError ? <p role="alert">{projectError(bases.error)}</p> : <><label>选择已有知识库<select value={baseId} onChange={(event) => setBaseId(event.target.value)}><option value="">请选择</option>{bases.data?.map((row) => <option key={String(row.id)} value={String(row.id)}>{String(row.name)} · {String(row.documentCount ?? '—')} 篇</option>)}</select></label><Button disabled={disabled || !baseId} onClick={() => void command({ operation: 'connect_base', kbId: baseId })}>复制来源到实验</Button><p className="lab-knowledge-note">通过知识库服务读取来源；之后的切片与检索实验使用独立副本。</p></>}</div> : null}
      {state?.corpora.length ? <><h3>已整理的资料</h3><label>资料版本<select value={corpus?.jobId ?? ''} onChange={(event) => patch({ corpusId: event.target.value, indexId: '', datasetId: '' })}>{state.corpora.map((row) => <option key={row.jobId} value={row.jobId}>{row.title} · {amount(row.documentCount)} 篇</option>)}</select></label>
        {corpus ? <><p>{amount(corpus.documentCount)} 篇文档 · {(corpus.byteSize / 1024 / 1024).toFixed(1)} MB{corpus.intake.skippedCount ? ` · ${corpus.intake.skippedCount} 项非支持格式未导入` : ''}</p><div className="lab-knowledge-previews">{corpus.preview.map((row) => <details key={row.sourceId}><summary>{row.title}<span>{amount(row.byteSize)} bytes</span></summary><p>{row.excerpt}</p><small>{row.sourceId}</small></details>)}</div><Button variant="primary" onClick={() => patch({ page: 'index' })}>配置索引<ArrowRight size={15} /></Button></> : null}</> : null}
    </div> : draft.page === 'index' ? <div className="lab-knowledge-section">
      {!corpus ? <Empty onSources={() => patch({ page: 'sources' })} /> : <>
        <h3>{corpus.title}</h3><p>{amount(corpus.documentCount)} 篇文档。更改切片或 Embedding 会建立新索引，已有版本仍可选择。</p>
        <div className="lab-knowledge-fields"><label>切片方式<select value={draft.strategy} disabled={disabled} onChange={(event) => patch({ strategy: event.target.value })}><option value="markdown">按 Markdown 结构</option><option value="general">按段落</option><option value="fixed">固定长度</option><option value="qa">问答结构</option></select></label><label>切片长度（字符）<input type="number" min={200} max={8000} value={draft.size} disabled={disabled} onChange={(event) => patch({ size: Number(event.target.value) })} /></label><label>重叠字符<input type="number" min={0} max={2000} value={draft.overlap} disabled={disabled} onChange={(event) => patch({ overlap: Number(event.target.value) })} /></label></div>
        <label>Embedding<select value={draft.embedding} disabled={disabled} onChange={(event) => patch({ embedding: event.target.value })}><option value="none">不使用向量 · 先建立关键词基线</option><option value="configured">沿用知识库的语义 Embedding 配置</option></select></label>
        <p className="lab-knowledge-note">{state?.embedding.provider === 'none' ? '当前未配置语义模型。可先测试关键词检索，也可在知识库 App 的设置中配置 Embedding。' : `当前配置：${state?.embedding.provider} / ${state?.embedding.model}。使用远程 Embedding 时会提交文档并可能产生费用。`}</p>
        <Button variant="primary" disabled={disabled || !Number.isInteger(draft.size) || draft.size < 200 || draft.size > 8000 || draft.overlap < 0 || draft.overlap >= draft.size} onClick={() => void command({ operation: 'index', corpusId: corpus.jobId, chunking: { strategy: draft.strategy, size: draft.size, overlap: draft.overlap }, embedding: draft.embedding })}>建立新索引</Button>
        {index ? <><h3>试一次真实检索</h3><label>索引版本<select value={index.jobId} onChange={(event) => patch({ indexId: event.target.value })}>{indexes.map((row, number) => <option key={row.jobId} value={row.jobId}>{indexes.length - number} · {row.chunking.strategy} {row.chunking.size}/{row.chunking.overlap} · {amount(row.chunkCount)} 片</option>)}</select></label><p>{amount(index.documentCount)} 篇独立正文 / {amount(index.chunkCount)} 个切片 · {index.dense.available ? `${amount(index.dense.vectorCount)} 条向量` : '关键词索引已就绪'}</p>{index.duplicateSourceCount ? <p className="lab-knowledge-note">保留 {amount(index.sourceCount ?? index.documentCount)} 个原始来源；其中 {amount(index.duplicateSourceCount)} 个来源与其他来源正文完全相同，复用索引并保留引用关系。</p> : null}
          <RetrievalFields value={profile} onChange={updateProfile} disabled={disabled} semantic={index.dense.provider.semantic} reranker={index.reranker.configured === true} />
          <form className="lab-knowledge-search" onSubmit={(event) => { event.preventDefault(); if (draft.query.trim() && !disabled) void command({ operation: 'search', indexId: index.jobId, query: draft.query, profile: { ...profile } }); }}><label>检索问题<input value={draft.query} placeholder="输入实际业务问题" onChange={(event) => patch({ query: event.target.value })} disabled={disabled} /></label><Button type="submit" disabled={disabled || !draft.query.trim()}><Search size={15} />试检索</Button></form>
          {dataset?.preview?.length && !draft.query ? <div className="lab-knowledge-examples"><span>试试原始开发题：</span>{dataset.preview.map((row) => <button key={row.caseId} onClick={() => patch({ query: row.question })}>{row.question}</button>)}</div> : null}
          {searchJob?.result?.hits ? <div className="lab-knowledge-hits"><p>“{String(searchJob.result.query)}” · {searchJob.result.hits.length} 个实际命中</p><LabRetrievalRun result={searchJob.result} />{searchJob.result.hits.length ? searchJob.result.hits.map((hit, index) => <article key={hit.chunkId}><h4>{hit.title}</h4><LabRetrievalHitMetrics hit={hit} index={index} /><p>{hit.content}</p><small>来源 {hit.sourceId} · 切片 {hit.chunkId}</small></article>) : <p>未命中资料。可以换一种问法，或调整检索方式后重试。</p>}</div> : null}
          <Button variant="primary" onClick={() => patch({ page: 'evaluation' })}>继续评测<ArrowRight size={15} /></Button></> : null}
      </>}
    </div> : <div className="lab-knowledge-section">
      {!corpus ? <Empty onSources={() => patch({ page: 'sources' })} /> : <>
        <h3>先确定评测数据从哪里来</h3><p>没有评测就没有可比较的指标。请明确使用自己的题集，或让 Agent 根据当前资料起草待审核题集。</p>
        <div className="lab-knowledge-mode-choice" role="radiogroup" aria-label="评测集来源"><label className={draft.datasetMode === 'import' ? 'is-selected' : ''}><input type="radio" name="dataset-mode" checked={draft.datasetMode === 'import'} disabled={disabled} onChange={() => patch({ datasetMode: 'import', noDataset: false })} /><strong>我提供评测集</strong><span>上传或读取 JSONL/CSV，题目、参考答案和来源由我控制。</span></label><label className={draft.datasetMode === 'agent' ? 'is-selected' : ''}><input type="radio" name="dataset-mode" checked={draft.datasetMode === 'agent'} disabled={disabled} onChange={() => patch({ datasetMode: 'agent', noDataset: true })} /><strong>Agent 起草评测集</strong><span>根据已接入资料生成候选题，先审核标准，再允许运行指标。</span></label></div>
        {draft.datasetMode === 'import' ? <><div className="lab-knowledge-inline"><label className="lab-project-file-picker"><Upload size={15} />{datasetFile?.name ?? '选择评测集文件'}<input type="file" accept=".jsonl,.ndjson,.csv" disabled={disabled} onChange={(event) => setDatasetFile(event.target.files?.[0])} /></label>{datasetFile ? <Button disabled={disabled} size="small" onClick={() => setDatasetFile(undefined)}>移除文件</Button> : null}</div>
        {!datasetFile ? <label>执行器上的评测集路径<input value={draft.datasetPath} placeholder="JSONL 或 CSV 文件的绝对路径" disabled={disabled} onChange={(event) => patch({ datasetPath: event.target.value })} /></label> : null}<FieldMapping kind="dataset" value={draft.datasetFields} onChange={(datasetFields) => patch({ datasetFields })} disabled={disabled} /><Button disabled={disabled || (!datasetFile && !draft.datasetPath.trim())} onClick={() => void importFile('dataset')}>导入评测集</Button></> : <div className="lab-knowledge-agent-dataset"><p>Agent 只会生成候选题和参考标准，不会把它们直接当作真实指标。</p><Button variant="primary" disabled={disabled || !index} onClick={() => void bind()}>生成待审核评测集</Button></div>}
        {dataset ? <><label>评测集版本<select value={dataset.datasetId} onChange={(event) => patch({ datasetId: event.target.value, noDataset: false })}>{datasets.map((row) => <option key={row.datasetId} value={row.datasetId}>{row.title} · {row.caseCount} 题</option>)}</select></label><p>{dataset.caseCount} 道原题 · {dataset.splits.development} 道开发题 / {dataset.splits.holdout} 道保留题 · {dataset.retrievalEvaluableCount} 道有来源标注</p><p className="lab-knowledge-note">{dataset.providedSplit ? '保留原分组：开发题与保留题沿用导入文件的划分；不据此声明官方金标。' : '按共享来源和重复问题分组，避免分组间重复使用；这是本项目派生分组，不是数据集官方划分。'}</p></> : <p className="lab-knowledge-note">尚无评测集。下方可以建立待审核标准；合成题会明确标记，不计作真实客户问题。</p>}
        {index && dataset ? <><h3>先检查资料是否找对</h3><label>本次评测的索引<select value={index.jobId} disabled={disabled} onChange={(event) => patch({ indexId: event.target.value })}>{indexes.map((row, number) => <option key={row.jobId} value={row.jobId}>{indexes.length - number} · {row.chunking.strategy} {row.chunking.size}/{row.chunking.overlap} · {amount(row.chunkCount)} 片</option>)}</select></label><RetrievalFields value={profile} onChange={updateProfile} disabled={disabled} semantic={index.dense.provider.semantic} reranker={index.reranker.configured === true} />
          <div className="lab-knowledge-inline"><Button variant="primary" disabled={disabled || !dataset.splits.development || !dataset.retrievalEvaluableCount} onClick={() => void evaluate('development')}>运行开发集检索评测</Button><Button disabled={disabled || !dataset.splits.holdout || !dataset.retrievalEvaluableCount} onClick={() => void evaluate('holdout')}>固定配置，检查保留集</Button></div>
          <p className="lab-knowledge-note">仅评测检索，不调用回答模型。语义检索和重排仍会使用所配置的服务。保留集用于配置选定后的检查；重复使用会记录次数。</p>
          {evaluations.length ? <EvaluationTable evaluations={evaluations} disabled={disabled} onUse={(row) => { const selected = state?.indexes.find((index) => index.jobId === row.indexId); if (selected) patch({ indexId: selected.jobId, corpusId: selected.corpusId, profile: row.profile }); }} /> : null}</> : !index ? <p>资料尚未建立索引。<Button size="small" onClick={() => patch({ page: 'index' })}>前往建索引</Button></p> : null}
        {index ? <><h3>进入回答评测</h3>
          <label>本轮回答评测题数<input type="number" min={2} max={100} value={draft.count} onChange={(event) => patch({ count: Number(event.target.value) })} /></label><p className="lab-knowledge-note">首次建议 4 题。{dataset && !draft.noDataset ? `从 ${dataset.caseCount} 道原题中稳定选取，并保留开发与验证分组。` : '从知识库的文档样本起草，所有标准先进入待审核状态。'}完整知识库仍参与每次检索。后续起草、评审和回答会调用所选模型。</p>
          <Button variant="primary" disabled={disabled || !validCount || (draft.datasetMode === 'import' && !dataset)} onClick={() => void bind()}>{draft.datasetMode === 'import' ? '使用已导入题集，进入回答评测' : '建立待审核标准'}<ArrowRight size={15} /></Button>
          <p className="lab-knowledge-note">此步只绑定索引、检索配置和题目预算。下一页可以核对标准、校准评审，再运行基线与候选提示词。</p></> : null}
      </>}
    </div>}
    {state?.jobs.length ? <details className="lab-knowledge-history"><summary>任务记录 · {state.jobs.length}</summary><ol>{state.jobs.map((job: KnowledgeJob) => <li key={job.jobId}><strong>{labels[job.publicSpec.operation]} · {stateLabels[job.state] ?? job.state}</strong><span>{job.result?.message || job.progress || job.error}</span><small>{job.jobId}</small><Button size="small" onClick={() => setRecordId(job.jobId)}>查看原任务</Button></li>)}</ol></details> : null}
  </section>;
}

function Empty({ onSources }: { onSources: () => void }) { return <div className="lab-knowledge-empty"><p>先接入一份真实知识库，再配置检索和评测。</p><Button onClick={onSources}>接入资料</Button></div>; }
function FieldMapping({ kind, value, onChange, disabled }: { kind: 'corpus' | 'dataset'; value: Record<string, string>; onChange: (value: Record<string, string>) => void; disabled: boolean }) {
  const fields = kind === 'corpus' ? [['id', '来源 ID', 'id'], ['text', '正文', 'contents / text / content'], ['title', '标题', 'title'], ['uri', '来源地址', 'url']]
    : [['id', '题目 ID', 'id（可缺省）'], ['question', '问题', 'question'], ['answer', '参考答案', 'answer'], ['sources', '参考来源 ID 列表', 'article_ids / sourceIds'], ['split', '原分组（可选）', 'split: development / holdout']];
  return <details className="lab-knowledge-mapping"><summary>文件字段不同？设置字段映射</summary><div className="lab-knowledge-fields">{fields.map(([key, title, placeholder]) => <label key={key}>{title}<input value={value[key] ?? ''} placeholder={placeholder} disabled={disabled} onChange={(event) => { const next = { ...value }; if (event.target.value.trim()) next[key] = event.target.value.trim(); else delete next[key]; onChange(next); }} /></label>)}</div><p className="lab-knowledge-note">留空使用自动识别。CSV 中的来源列表可以写成 JSON 数组，或用分号分隔。</p></details>;
}
function RetrievalFields({ value, onChange, disabled, semantic, reranker }: { value: RetrievalProfile; onChange: (value: Partial<RetrievalProfile>) => void; disabled: boolean; semantic: boolean; reranker: boolean }) {
  return <div className="lab-knowledge-profile"><div className="lab-knowledge-fields"><label>检索方式<select value={value.mode} disabled={disabled} onChange={(event) => onChange({ mode: event.target.value as RetrievalProfile['mode'] })}><option value="lexical">关键词</option><option value="dense" disabled={!semantic}>语义</option><option value="hybrid" disabled={!semantic}>混合</option></select></label><label>Top K<input type="number" min={1} max={20} value={value.topK} disabled={disabled} onChange={(event) => onChange({ topK: Number(event.target.value) })} /></label></div><details><summary>更多检索设置</summary><div className="lab-knowledge-fields"><label>分数阈值<input type="number" min={0} max={1} step={0.05} value={value.threshold} disabled={disabled} onChange={(event) => onChange({ threshold: Number(event.target.value) })} /></label><label>回答证据预算（字符）<input type="number" min={1000} max={60000} step={1000} value={value.contextChars} disabled={disabled} onChange={(event) => onChange({ contextChars: Number(event.target.value) })} /></label><label>重排候选数<input type="number" min={value.topK} max={100} value={value.candidateDepth} disabled={disabled} onChange={(event) => onChange({ candidateDepth: Number(event.target.value) })} /></label></div><label className="lab-knowledge-check"><input type="checkbox" checked={value.rerank} disabled={disabled || !reranker} onChange={(event) => onChange({ rerank: event.target.checked })} />使用已配置的重排模型{!reranker ? '（当前未配置）' : ''}</label></details></div>;
}
function EvaluationTable({ evaluations, disabled, onUse }: { evaluations: KnowledgeEvaluation[]; disabled: boolean; onUse: (row: KnowledgeEvaluation) => void }) {
  return <div className="lab-knowledge-table"><table><caption>同一评测集上的实际检索结果。检索命中不等于回答正确。</caption><thead><tr><th>配置</th><th>分组</th><th>计分 / 计划</th><th>Recall@K</th><th>MRR</th><th>nDCG@K</th><th>平均耗时</th><th>下一步</th></tr></thead><tbody>{evaluations.map((row) => {
    const metrics = row.report.metrics.metrics; const k = String(row.profile.topK);
    return <tr key={row.jobId}><th>{modeLabels[row.profile.mode]} · K={k}{row.profile.rerank ? ' · 重排' : ''}<small>{row.indexConfig ? `切片 ${row.indexConfig.chunking.size}/${row.indexConfig.chunking.overlap}` : `索引 ${row.indexId.slice(-6)}`}</small></th><td>{row.split === 'development' ? '开发' : `保留 · 第 ${row.holdoutUseNumber} 次`}</td><td>{row.evaluatedCount} / {row.plannedCount}{row.unlabeledCount ? <small>{row.unlabeledCount} 题缺来源标注</small> : null}</td><td>{rate(metrics.recallAtK[k])}</td><td>{typeof metrics.mrr === 'number' ? metrics.mrr.toFixed(4) : '—'}</td><td>{rate(metrics.ndcgAtK[k])}</td><td>{row.report.costs.meanRetrievalLatencyMs.toFixed(1)} ms</td><td><Button size="small" disabled={disabled} onClick={() => onUse(row)}>用于回答评测</Button></td></tr>;
  })}</tbody></table></div>;
}
