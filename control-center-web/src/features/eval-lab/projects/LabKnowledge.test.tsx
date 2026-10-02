import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { webcrypto } from 'node:crypto';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { LabKnowledge, retrievalProfileError } from './LabKnowledge';
import { defaultRetrieval, parseKnowledgeState, type KnowledgeState } from './knowledge-types';
import type { JsonValue, LabProject, ProjectReceipt } from './types';

const clients: QueryClient[] = [];
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); sessionStorage.clear(); vi.unstubAllGlobals(); });
const project: LabProject = { schemaVersion: 'rag-ime.agent-lab-project.v1', projectId: 'project-1', revision: 1, title: 'Product support', description: 'Answer actual support questions', briefVersion: 1,
  materialCount: 0, artifactCount: 0, guideSessionId: '', createdAtMs: 1, updatedAtMs: 1, materialSetId: '', materialSet: { materialSetId: '', version: 0, materials: [], createdAtMs: null }, materialVersions: [],
  intake: { state: 'needs_materials', requestedPath: '', resolvedPath: '', readCount: 0, readBytes: 0, skippedCount: 0, partial: false, issues: [], checkedAtMs: null }, artifacts: [], bindings: [], workspace: { artifactOrder: [], primaryArtifactId: '', layout: 'split' }, workspaceBinding: null };
function state(): KnowledgeState { return { schemaVersion: 'paw.lab-knowledge-resource.v1', corpora: [], datasets: [], indexes: [], evaluations: [], jobs: [], embedding: { provider: 'none', model: '' } }; }
function ready(): KnowledgeState {
  return { ...state(), corpora: [{ jobId: 'corpus-1', title: 'Real support corpus', corpusHash: 'corpus-hash', documentCount: 6221, byteSize: 53000000, intake: {}, preview: [] }],
    datasets: [{ datasetId: 'dataset-1', corpusId: 'corpus-1', corpusHash: 'corpus-hash', title: 'Expert questions', sha256: 'dataset-hash', caseCount: 200, splits: { development: 140, holdout: 60 }, referenceAnswerCount: 200, retrievalEvaluableCount: 200, officialSplit: false, preview: [{ caseId: 'q1', question: 'How do refunds work?' }] }],
    indexes: [{ jobId: 'index-1', corpusId: 'corpus-1', corpusHash: 'corpus-hash', title: 'Index', documentCount: 6221, chunkCount: 39880, configHash: 'profile-hash', chunking: { strategy: 'markdown', size: 1200, overlap: 160 }, dense: { available: false, provider: { semantic: false, provider: 'none' }, vectorCount: 0 }, reranker: { configured: false } }] };
}
function mount(read: () => unknown, onCommand = vi.fn(async (_input: Record<string, JsonValue>) => undefined as ProjectReceipt | undefined)) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } }); clients.push(client);
  const transport = new MockControlTransport({ routes: { 'agent.eval-lab.projects.get': () => ({ ok: true, items: [project], project, supportedViews: [], knowledge: read() }) } });
  const onBind = vi.fn(async (_input: Record<string, JsonValue>): Promise<ProjectReceipt | undefined> => undefined); const onOpenBinding = vi.fn();
  const view = render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><LabKnowledge project={project} busy={false} onCommand={onCommand} onBind={onBind} onOpenBinding={onOpenBinding} /></ControlTransportProvider></QueryClientProvider>);
  return { onCommand, onBind, client, transport, ...view };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
const uploadReceipt = (ready = false): ProjectReceipt => ({ ok: true, project, clientRequestId: 'test', replayed: false,
  upload: { uploadId: 'upload-1', chunkBytes: 4, ready } });
async function selectCorpusFile() {
  await waitFor(() => expect(screen.getByRole('button', { name: '连接已有知识库' })).not.toBeDisabled());
  const body = new TextEncoder().encode('test-file');
  const file = new File([body], 'corpus.jsonl');
  Object.defineProperty(file, 'arrayBuffer', { value: async () => body.buffer });
  fireEvent.change(screen.getByLabelText('选择语料文件'), { target: { files: [file] } });
  fireEvent.click(screen.getByRole('button', { name: '整理资料' }));
  return file;
}

describe('Knowledge resource frontend', () => {
  it('blocks invalid or unsupported retrieval settings before issuing a job', () => {
    expect(retrievalProfileError({ ...defaultRetrieval, topK: 0 }, false, false)).toContain('Top K');
    expect(retrievalProfileError({ ...defaultRetrieval, mode: 'dense' }, false, false)).toContain('语义向量');
    expect(retrievalProfileError({ ...defaultRetrieval, rerank: true }, false, false)).toContain('重排模型');
    expect(retrievalProfileError({ ...defaultRetrieval, candidateDepth: 2 }, false, false)).toContain('候选数');
    expect(retrievalProfileError({ ...defaultRetrieval, contextChars: 0 }, false, false)).toContain('预算');
    expect(retrievalProfileError(defaultRetrieval, false, false)).toBe('');
  });
  it('shows unavailable embedding and rejects fractional overlap without building an index', async () => {
    const { onCommand } = mount(ready);
    fireEvent.click(await screen.findByRole('button', { name: '索引与检索' }));
    expect(screen.getByRole('option', { name: '沿用知识库的语义 Embedding 配置' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('重叠字符'), { target: { value: '1.5' } });
    expect(screen.getByRole('button', { name: '建立新索引' })).toBeDisabled();
    expect(screen.getByLabelText('重排候选数')).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Top K'), { target: { value: '0' } });
    fireEvent.change(screen.getByLabelText('检索问题'), { target: { value: '测试问题' } });
    expect(screen.getByRole('button', { name: '试检索' })).toBeDisabled();
    expect(onCommand).not.toHaveBeenCalled();
  });
  it('shows the completed recall run parameters and ranks independently of the editable draft', async () => {
    const source = ready();
    source.jobs.push({ jobId: 'search-1', state: 'completed', progress: '', error: '', createdAtMs: 1, updatedAtMs: 2,
      publicSpec: { projectId: 'project-1', operation: 'search' }, result: { kind: 'search', indexId: 'index-1', query: 'original question',
        profile: { mode: 'lexical', topK: 3, threshold: 0.25, candidateDepth: 40, contextChars: 16000, rerank: true },
        retrieval: { rerank: { enabled: true, provider: 'test-reranker', duplicateDocumentHitsDropped: 2 } },
        hits: [{ sourceId: 'source-1', chunkId: 'chunk-1', title: 'Ranked source', uri: '', content: 'Full source text', score: 0.4,
          rerankOriginalRank: 8, rerankRank: 3, rerankScore: 0.93 }],
      } });
    mount(() => source);
    fireEvent.click(await screen.findByRole('button', { name: '索引与检索' }));
    expect(await screen.findByText('Ranked source')).toBeVisible();
    fireEvent.change(screen.getByLabelText('Top K'), { target: { value: '12' } });
    expect(screen.getByRole('region', { name: '本次召回参数与执行' })).toHaveTextContent('3 / 0.25');
    expect(screen.getByRole('region', { name: '本次召回参数与执行' })).toHaveTextContent('本次已重排 · test-reranker');
    expect(screen.getByLabelText('结果 1 排名与分数')).toHaveTextContent('最终排名#1');
    expect(screen.getByLabelText('结果 1 排名与分数')).toHaveTextContent('8 / 0.4');
    expect(screen.getByLabelText('结果 1 排名与分数')).toHaveTextContent('3 / 0.93');
  });
  it('preserves supplied split attribution and offers its optional import field mapping', async () => {
    const source = ready(); source.datasets[0]!.providedSplit = true;
    mount(() => source);
    await waitFor(() => expect(screen.getByRole('button', { name: '连接已有知识库' })).not.toBeDisabled());
    fireEvent.click(screen.getByRole('button', { name: '评测' }));
    expect(await screen.findByText(/保留原分组：开发题与保留题沿用导入文件的划分/)).toBeVisible();
    expect(screen.queryByText(/这是本项目派生分组/)).not.toBeInTheDocument();
    expect(screen.getByLabelText('原分组（可选）')).toBeInTheDocument();
    expect(parseKnowledgeState(source).datasets[0]?.providedSplit).toBe(true);
  });
  it('imports an executor path through the resource operation without adding it to model context', async () => {
    const { onCommand } = mount(state);
    const path = await screen.findByRole('textbox', { name: '执行器上的文件夹或 JSONL 路径' });
    await waitFor(() => expect(path).not.toBeDisabled());
    fireEvent.change(path, { target: { value: '/data/company-kb' } });
    fireEvent.click(screen.getByRole('button', { name: '整理资料' }));
    await waitFor(() => expect(onCommand).toHaveBeenCalledWith({ operation: 'import_corpus', path: '/data/company-kb', fields: {} }));
    expect(onCommand.mock.calls[0]?.[0]).not.toHaveProperty('sources');
  });

  it('keeps the current path during a failed read and never resubmits a resource job', async () => {
    let offline = false;
    const { client, onCommand } = mount(() => { if (offline) throw new Error('Failed to fetch'); return state(); });
    const input = await screen.findByRole('textbox', { name: '执行器上的文件夹或 JSONL 路径' });
    fireEvent.change(input, { target: { value: '/data/unfinished-input' } });
    offline = true;
    await act(async () => { await client.invalidateQueries({ queryKey: ['lab-knowledge'] }); });
    expect(await screen.findByRole('alert')).toHaveTextContent('项目服务暂时不可用');
    expect(input).toHaveValue('/data/unfinished-input');
    expect(onCommand).not.toHaveBeenCalled();
    offline = false;
    await act(async () => { await client.invalidateQueries({ queryKey: ['lab-knowledge'] }); });
    expect(input).toHaveValue('/data/unfinished-input');
  });

  it('binds the full index plus an explicit small original-question budget, and offers a separate no-dataset path', async () => {
    const { onBind } = mount(ready);
    await waitFor(() => expect(screen.getByRole('button', { name: '连接已有知识库' })).not.toBeDisabled());
    fireEvent.click(screen.getByRole('button', { name: '评测' }));
    expect(await screen.findByText(/^200 道原题 ·/)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '使用已导入题集，进入回答评测' }));
    expect(onBind).toHaveBeenCalledWith(expect.objectContaining({ adapterId: 'golden.knowledge_qa', input: expect.objectContaining({ indexId: 'index-1', datasetId: 'dataset-1', targetCount: 4 }) }));
    fireEvent.click(screen.getByRole('radio', { name: /Agent 起草评测集/ }));
    fireEvent.click(screen.getByRole('button', { name: '建立待审核标准' }));
    expect(onBind.mock.calls[1]?.[0]).toEqual(expect.objectContaining({ input: expect.not.objectContaining({ datasetId: expect.anything() }) }));
  });

  it('uploads a corpus larger than the old 2 MB text limit in bounded idempotent chunks', async () => {
    vi.stubGlobal('crypto', webcrypto);
    const calls: Record<string, JsonValue>[] = [];
    const onCommand = vi.fn(async (input: Record<string, JsonValue>): Promise<ProjectReceipt> => {
      calls.push(input);
      return { ok: true, project, clientRequestId: 'test', replayed: false,
        ...(String(input.operation).startsWith('upload') ? { upload: { uploadId: 'upload-1', chunkBytes: 512 * 1024, ready: input.operation === 'upload_seal' } } : {}) };
    });
    mount(state, onCommand);
    await waitFor(() => expect(screen.getByRole('button', { name: '连接已有知识库' })).not.toBeDisabled());
    const body = new TextEncoder().encode(JSON.stringify({ id: 'doc-1', text: 'x'.repeat(2_100_000) }) + '\n');
    const file = new File([body], 'large-corpus.jsonl');
    Object.defineProperty(file, 'arrayBuffer', { value: async () => body.buffer });
    fireEvent.change(screen.getByLabelText('选择语料文件'), { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: '整理资料' }));
    await waitFor(() => expect(calls.at(-1)?.operation).toBe('import_corpus'), { timeout: 5000 });
    const chunks = calls.filter((input) => input.operation === 'upload_chunk');
    expect(chunks.length).toBeGreaterThan(1);
    expect(chunks.every((input) => String(input.data).length < 710000)).toBe(true);
    expect(calls.at(-1)).toMatchObject({ uploadId: 'upload-1', operation: 'import_corpus' });
    expect(calls.at(-1)).not.toHaveProperty('path');
  });

  it.each([
    { operation: 'upload_chunk', waiting: '当前分片回执', sealCalls: 0 },
    { operation: 'upload_seal', waiting: '文件接收回执', sealCalls: 1 },
  ])('stops after the pending $operation receipt without continuing to import', async ({ operation, waiting, sealCalls }) => {
    vi.stubGlobal('crypto', webcrypto);
    const pending = deferred<ProjectReceipt>();
    const onCommand = vi.fn(async (input: Record<string, JsonValue>) => {
      if (input.operation === operation && (operation !== 'upload_chunk' || input.index === 2)) return pending.promise;
      return uploadReceipt();
    });
    mount(state, onCommand);
    fireEvent.click(screen.getByText('文件字段不同？设置字段映射'));
    fireEvent.change(screen.getByLabelText('来源 ID'), { target: { value: 'external_id' } });
    const file = await selectCorpusFile();
    await waitFor(() => expect(onCommand).toHaveBeenCalledWith(expect.objectContaining({ operation,
      ...(operation === 'upload_chunk' ? { index: 2 } : {}) })));
    fireEvent.click(screen.getByRole('button', { name: '停止上传' }));
    expect.soft(screen.getByRole('status')).toHaveTextContent(`正在停止，等待${waiting}`);
    expect.soft(screen.queryByRole('button', { name: '正在停止' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '整理资料' })).toBeDisabled();
    await act(async () => { pending.resolve(uploadReceipt(operation === 'upload_seal')); });
    await waitFor(() => expect(screen.getByRole('button', { name: '整理资料' })).not.toBeDisabled());
    expect(onCommand.mock.calls.filter(([input]) => input.operation === 'upload_seal')).toHaveLength(sealCalls);
    expect(onCommand).not.toHaveBeenCalledWith(expect.objectContaining({ operation: 'import_corpus' }));
    expect(screen.getByRole('alert')).toHaveTextContent('未发起资料导入');
    expect(screen.getByRole('alert')).toHaveTextContent('upload-1');
    expect((screen.getByLabelText('corpus.jsonl') as HTMLInputElement).files?.[0]).toBe(file);
    expect(screen.getByLabelText('来源 ID')).toHaveValue('external_id');
    fireEvent.click(screen.getByRole('button', { name: '整理资料' }));
    await waitFor(() => expect(onCommand).toHaveBeenCalledWith(expect.objectContaining({ operation: 'import_corpus', fields: { id: 'external_id' } })));
    expect(onCommand.mock.calls.filter(([input]) => input.operation === 'upload_begin')).toHaveLength(2);
    expect(onCommand.mock.calls.filter(([input]) => input.operation === 'import_corpus')).toHaveLength(1);
  });

  it('stops before hashing when file reading is still pending', async () => {
    const digest = vi.fn();
    vi.stubGlobal('crypto', { randomUUID: () => webcrypto.randomUUID(), subtle: { digest } });
    const pending = deferred<ArrayBuffer>();
    const { onCommand } = mount(state);
    await waitFor(() => expect(screen.getByRole('button', { name: '连接已有知识库' })).not.toBeDisabled());
    const file = new File(['test-file'], 'corpus.jsonl');
    Object.defineProperty(file, 'arrayBuffer', { value: () => pending.promise });
    fireEvent.change(screen.getByLabelText('选择语料文件'), { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: '整理资料' }));
    fireEvent.click(screen.getByRole('button', { name: '停止上传' }));
    await act(async () => { pending.resolve(new ArrayBuffer(9)); });
    await waitFor(() => expect(screen.getByRole('button', { name: '整理资料' })).not.toBeDisabled());
    expect(digest).not.toHaveBeenCalled();
    expect(onCommand).not.toHaveBeenCalled();
  });

  it.each(['upload_begin', 'upload_chunk', 'upload_seal'])('ignores a late %s receipt after leaving the component', async (operation) => {
    vi.stubGlobal('crypto', webcrypto);
    const pending = deferred<ProjectReceipt>();
    const onCommand = vi.fn(async (input: Record<string, JsonValue>) => {
      if (input.operation === operation && (operation !== 'upload_chunk' || input.index === 2)) return pending.promise;
      return uploadReceipt();
    });
    const { unmount } = mount(state, onCommand);
    await selectCorpusFile();
    await waitFor(() => expect(onCommand).toHaveBeenCalledWith(expect.objectContaining({ operation,
      ...(operation === 'upload_chunk' ? { index: 2 } : {}) })));
    const callsBeforeLeaving = onCommand.mock.calls.length;
    unmount();
    await act(async () => { pending.resolve(uploadReceipt(operation === 'upload_seal')); });
    expect(onCommand).toHaveBeenCalledTimes(callsBeforeLeaving);
  });

  it('keeps an uncertain receipt visible after stopping and never retries it automatically', async () => {
    vi.stubGlobal('crypto', webcrypto);
    const pending = deferred<ProjectReceipt | undefined>();
    const onCommand = vi.fn(async (input: Record<string, JsonValue>) => input.operation === 'upload_seal' ? pending.promise : uploadReceipt());
    mount(state, onCommand);
    await selectCorpusFile();
    await waitFor(() => expect(onCommand).toHaveBeenCalledWith({ operation: 'upload_seal', uploadId: 'upload-1' }));
    fireEvent.click(screen.getByRole('button', { name: '停止上传' }));
    await act(async () => { pending.resolve(undefined); });
    expect(await screen.findByRole('alert')).toHaveTextContent('文件接收结果尚未确认，请核对原操作');
    expect(screen.getByRole('alert')).toHaveTextContent('upload-1');
    expect(onCommand.mock.calls.filter(([input]) => input.operation === 'upload_seal')).toHaveLength(1);
    expect(onCommand).not.toHaveBeenCalledWith(expect.objectContaining({ operation: 'import_corpus' }));
  });

  it('does not offer upload cancellation once the import request has been submitted', async () => {
    vi.stubGlobal('crypto', webcrypto);
    const pending = deferred<ProjectReceipt>();
    const onCommand = vi.fn(async (input: Record<string, JsonValue>) => input.operation === 'import_corpus' ? pending.promise : uploadReceipt());
    mount(state, onCommand);
    await selectCorpusFile();
    await waitFor(() => expect(onCommand).toHaveBeenCalledWith(expect.objectContaining({ operation: 'import_corpus' })));
    expect(screen.getByRole('status')).toHaveTextContent('文件已接收，正在提交导入');
    expect(screen.queryByRole('button', { name: '停止上传' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '整理资料' })).toBeDisabled();
    await act(async () => { pending.resolve(uploadReceipt(true)); });
    await waitFor(() => expect(screen.getByRole('button', { name: '整理资料' })).not.toBeDisabled());
  });

  it('also stops dataset import after the last chunk while retaining its selected file', async () => {
    vi.stubGlobal('crypto', webcrypto);
    const pending = deferred<ProjectReceipt>();
    const onCommand = vi.fn(async (input: Record<string, JsonValue>) => input.operation === 'upload_chunk' ? pending.promise : uploadReceipt());
    mount(ready, onCommand);
    fireEvent.click(await screen.findByRole('button', { name: '评测' }));
    const picker = await screen.findByLabelText('选择评测集文件');
    await waitFor(() => expect(picker).not.toBeDisabled());
    const bytes = new TextEncoder().encode('data');
    const file = new File([bytes], 'dataset.csv');
    Object.defineProperty(file, 'arrayBuffer', { value: async () => bytes.buffer });
    fireEvent.change(picker, { target: { files: [file] } });
    fireEvent.click(screen.getByRole('button', { name: '导入评测集' }));
    await waitFor(() => expect(onCommand).toHaveBeenCalledWith(expect.objectContaining({ operation: 'upload_chunk' })));
    fireEvent.click(screen.getByRole('button', { name: '停止上传' }));
    expect(screen.getByRole('status')).toHaveTextContent('正在停止，等待当前分片回执');
    await act(async () => { pending.resolve(uploadReceipt()); });
    await waitFor(() => expect(screen.getByRole('button', { name: '导入评测集' })).not.toBeDisabled());
    expect(onCommand).not.toHaveBeenCalledWith(expect.objectContaining({ operation: 'upload_seal' }));
    expect(onCommand).not.toHaveBeenCalledWith(expect.objectContaining({ operation: 'import_dataset' }));
    expect((screen.getByLabelText('dataset.csv') as HTMLInputElement).files?.[0]).toBe(file);
  });

  it('rejects incomplete evaluation responses instead of rendering false metrics', () => {
    expect(() => parseKnowledgeState({ ...ready(), evaluations: [{}] })).toThrow('未完整返回');
  });
});
