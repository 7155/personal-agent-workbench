import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { webcrypto } from 'node:crypto';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { LabKnowledge } from './LabKnowledge';
import { parseKnowledgeState, type KnowledgeState } from './knowledge-types';
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
  render(<QueryClientProvider client={client}><ControlTransportProvider transport={transport}><LabKnowledge project={project} busy={false} onCommand={onCommand} onBind={onBind} onOpenBinding={onOpenBinding} /></ControlTransportProvider></QueryClientProvider>);
  return { onCommand, onBind, client, transport };
}

describe('Knowledge resource frontend', () => {
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

  it('rejects incomplete evaluation responses instead of rendering false metrics', () => {
    expect(() => parseKnowledgeState({ ...ready(), evaluations: [{}] })).toThrow('未完整返回');
  });
});
