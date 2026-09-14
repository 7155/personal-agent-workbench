import { StrictMode, type ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { MockControlTransport } from '@/test/mock-transport';
import type { ControlRequest, KnowledgeDocumentImportReceipt } from '@/platform/transport';
import { TeamProvider, useTeam } from '@/features/team/team-context';
import { TeamApi } from '@/features/team/team-api';
import type { TeamSession, TeamSpace } from '@/features/team/types';
import { KnowledgeFeature } from './index';
import { TEAM_KNOWLEDGE_FILE_ACCEPT, TEAM_KNOWLEDGE_MAX_FILE_BYTES, chooseKnowledgeFiles, knowledgeUploadSizeError } from './api';

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso-list">{data.map((item, index) => <div key={index}>{itemContent(index, item)}</div>)}</div>
  ),
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('Team Knowledge access boundaries', () => {
  it('keeps project viewers read-only while allowing keyword search without advanced requests', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderTeamKnowledge(transport, sessionFor(viewerSpace));

    expect(await screen.findByText(/官网 · 项目成员可见/u)).toBeVisible();
    expect(screen.queryByRole('button', { name: '新建知识库' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '导入文件' })).not.toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: '设置' })).not.toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: '知识图谱' })).not.toBeInTheDocument();

    const forbidden = new Set([
      'knowledgeEmbedding.profile',
      'configuration.settings',
      'knowledgeBases.graph.get',
      'knowledgeBases.create',
      'knowledgeBases.update',
      'knowledgeBases.delete.preview',
      'knowledgeBases.document.delete',
      'knowledgeBases.document.retry',
      'knowledgeBases.reindexPreview',
      'knowledgeBases.rebuild',
      'knowledgeBases.job.cancel',
      'knowledgeBases.chunkPreview',
    ]);
    expect(transport.requests.some(({ request }) => forbidden.has(request.pathId))).toBe(false);

    await user.type(screen.getByRole('textbox', { name: '搜索知识库' }), '团队关键词');
    await user.click(screen.getByRole('button', { name: '搜索' }));
    await waitFor(() => expect(transport.requests.some(({ request }) => request.pathId === 'knowledgeBases.search')).toBe(true));
    const search = latestRequest(transport, 'knowledgeBases.search');
    expect(search?.body).toMatchObject({ query: '团队关键词', mode: 'lexical' });
  });

  it('lets project contributors import existing material but rejects an oversized file locally', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderTeamKnowledge(transport, sessionFor(contributorSpace));

    await user.click(await screen.findByRole('tab', { name: '资料' }));
    const dropzone = await screen.findByRole('button', { name: '导入文件' });
    const oversized = { name: 'too-large.md', size: TEAM_KNOWLEDGE_MAX_FILE_BYTES + 1 } as File;
    fireEvent.drop(dropzone, { dataTransfer: { files: [oversized], types: ['Files'] } });

    expect((await screen.findAllByText(/too-large\.md 超过团队资料单文件 8 MiB 上限/u))[0]).toBeVisible();
    expect(transport.knowledgeImportCalls).toHaveLength(0);
    expect(screen.queryByRole('button', { name: '新建知识库' })).not.toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: '设置' })).not.toBeInTheDocument();
    expect(knowledgeUploadSizeError({ name: 'small.md', size: TEAM_KNOWLEDGE_MAX_FILE_BYTES })).toBeNull();
  });

  it('keeps project maintainer management controls while withholding vector and graph configuration', async () => {
    const transport = createTransport();
    const user = userEvent.setup();
    renderTeamKnowledge(transport, sessionFor(maintainerSpace));

    expect(await screen.findByRole('button', { name: '新建知识库' })).toBeVisible();
    await user.click(screen.getByRole('button', { name: '更多知识库工具' }));
    expect(screen.queryByRole('menuitem', { name: '知识图谱' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('menuitem', { name: '设置' }));
    expect(await screen.findByRole('button', { name: '保存基本信息' })).toBeVisible();
    expect(screen.queryByRole('region', { name: '向量模型与索引' })).not.toBeInTheDocument();
    expect(screen.queryByText('高级：检索调优', { selector: 'summary' })).not.toBeInTheDocument();
    expect(transport.requests.some(({ request }) => request.pathId === 'knowledgeEmbedding.profile' || request.pathId === 'configuration.settings')).toBe(false);
  });

  it('uses the Team-safe file chooser allowlist', async () => {
    let chooser: HTMLInputElement | null = null;
    vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(function (this: HTMLInputElement) {
      chooser = this;
    });
    const pending = chooseKnowledgeFiles(20, TEAM_KNOWLEDGE_FILE_ACCEPT);
    expect(chooser).not.toBeNull();
    const input = chooser as unknown as HTMLInputElement;
    expect(input.accept).toBe(TEAM_KNOWLEDGE_FILE_ACCEPT);
    input.dispatchEvent(new Event('cancel'));
    await expect(pending).resolves.toEqual([]);
  });

  it('stops the next queued file when the Team space changes during an import', async () => {
    const transport = createTransport();
    const firstStarted = deferred<void>();
    const firstResult = deferred<KnowledgeDocumentImportReceipt[]>();
    const importer = vi.spyOn(transport, 'importKnowledgeDocuments')
      .mockImplementationOnce(async () => {
        firstStarted.resolve();
        return firstResult.promise;
      })
      .mockResolvedValue([]);
    const user = userEvent.setup();
    renderTeamKnowledge(transport, sessionFor(spaceA, spaceB), <SpaceSwitcher />, true);

    await user.click(await screen.findByRole('tab', { name: '资料' }));
    const dropzone = await screen.findByRole('button', { name: '导入文件' });
    const first = new File(['one'], 'one.md', { type: 'text/markdown' });
    const second = new File(['two'], 'two.md', { type: 'text/markdown' });
    fireEvent.drop(dropzone, { dataTransfer: { files: [first, second], types: ['Files'] } });
    await firstStarted.promise;

    await user.click(screen.getByRole('button', { name: '切换到项目 B' }));
    expect(await screen.findByText(/项目 B.*项目成员可见/u)).toBeVisible();
    await act(async () => {
      firstResult.resolve([receiptFor('one.md')]);
    });

    await waitFor(() => expect(importer).toHaveBeenCalledTimes(1));
    expect(importer.mock.calls[0]?.[0]).toMatchObject({ kbId: 'kb-team', parserProvider: 'builtin', maxFiles: 1 });
  });
});

const viewerSpace: TeamSpace = { id: 'project-viewer', kind: 'project', name: '官网', role: 'viewer', revision: 1 };
const contributorSpace: TeamSpace = { id: 'project-contributor', kind: 'project', name: '官网', role: 'contributor', revision: 1 };
const maintainerSpace: TeamSpace = { id: 'project-maintainer', kind: 'project', name: '官网', role: 'maintainer', revision: 1 };
const spaceA: TeamSpace = { id: 'project-a', kind: 'project', name: '项目 A', role: 'contributor', revision: 1 };
const spaceB: TeamSpace = { id: 'project-b', kind: 'project', name: '项目 B', role: 'contributor', revision: 1 };

function sessionFor(...spaces: TeamSpace[]): TeamSession {
  return {
    user: { id: 'user-member', username: 'member', displayName: '成员', role: 'member', active: true },
    csrfToken: 'csrf-team-test',
    spaces,
  };
}

function renderTeamKnowledge(transport: MockControlTransport, session: TeamSession, extra?: ReactNode, strict = false) {
  const api = fakeApi(session);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const tree = (
    <MemoryRouter initialEntries={['/knowledge?base=kb-team&tab=search']}>
      <TooltipProvider>
        <TeamProvider api={api}>
          <ControlTransportProvider transport={transport}>
            <QueryClientProvider client={client}>
              {extra}
              <KnowledgeFeature />
            </QueryClientProvider>
          </ControlTransportProvider>
        </TeamProvider>
      </TooltipProvider>
    </MemoryRouter>
  );
  return render(strict ? <StrictMode>{tree}</StrictMode> : tree);
}

function SpaceSwitcher() {
  const team = useTeam();
  return <button onClick={() => team.selectSpace(spaceB.id)} type="button">切换到项目 B</button>;
}

function fakeApi(session: TeamSession): TeamApi {
  return {
    status: vi.fn().mockResolvedValue({ enabled: true, name: 'PAW Team' }),
    login: vi.fn(),
    me: vi.fn().mockResolvedValue(session),
    logout: vi.fn().mockResolvedValue({ ok: true }),
    createProject: vi.fn(),
    listMembers: vi.fn().mockResolvedValue([]),
    listDirectory: vi.fn().mockResolvedValue([]),
    createMember: vi.fn(),
    setMemberStatus: vi.fn(),
    listProjectMembers: vi.fn().mockResolvedValue([]),
    addProjectMember: vi.fn(),
    removeProjectMember: vi.fn(),
    getProjectOverview: vi.fn(),
    getProjectPreview: vi.fn(),
    startProjectPreview: vi.fn(),
    stopProjectPreview: vi.fn(),
    listConnections: vi.fn(),
    createConnectionWithToken: vi.fn(),
    startConnectionOAuth: vi.fn(),
    revokeConnection: vi.fn(),
    createConnectionGrant: vi.fn(),
    revokeConnectionGrant: vi.fn(),
    updateProjectBrief: vi.fn(),
    adoptSessionRequirements: vi.fn(),
    listProjectDrafts: vi.fn(),
    listProjectSessions: vi.fn(),
    getProjectDraftDiff: vi.fn(),
    publishProjectDraft: vi.fn(),
    integrateProjectDraft: vi.fn(),
    adoptProjectDraft: vi.fn(),
  } as unknown as TeamApi;
}

function createTransport(): MockControlTransport {
  const transport = new MockControlTransport({
    routes: {
      'knowledgeBases.list': { items: [knowledgeBase()] },
      'knowledgeBases.get': { base: knowledgeBase() },
      'knowledgeBases.documents.list': { items: [knowledgeDocument()] },
      'knowledgeBases.document.get': knowledgeDetail,
      'knowledgeBases.jobs.list': { items: [] },
      'knowledgeWorker.health': { ok: true, status: 'ready' },
      'knowledgeParsers.list': { items: [{ id: 'builtin', enabled: true, ready: true, status: 'ready' }] },
      'knowledgeBases.search': { hits: [{ id: 'chunk-1', documentId: 'file-team', documentName: '团队资料.md', title: '团队关键词', excerpt: '共享项目资料中的关键词。', score: .8, page: null }] },
      'knowledgeBases.open': { ok: true },
    },
    knowledgeImportReceipts: [receiptFor('accepted.md')],
  });
  // TeamKnowledge deliberately rejects mock/native transports; model the
  // same HTTP transport kind used by TeamScopedProviders in these UI tests.
  Object.defineProperty(transport, 'kind', { configurable: true, value: 'http' });
  return transport;
}

function knowledgeBase() {
  return {
    id: 'kb-team', name: '项目资料库', description: '项目资料', documentCount: 1, chunkCount: 1,
    status: 'ready', agentEnabled: false, parserProvider: 'builtin', updatedAtMs: 1_700_000_000_000, revision: 1,
    chunkingConfig: { strategy: 'markdown', size: 1_200, overlap: 160, separator: '\n\n', respectHeadings: true, respectPageBoundaries: true },
    retrievalConfig: { mode: 'hybrid', topK: 10, threshold: .2, lexicalWeight: 1, denseWeight: 1, graphEnabled: true, graphWeight: .7, rrfK: 60, candidateMultiplier: 4 },
  };
}

function knowledgeDocument() {
  return {
    id: 'file-team', baseId: 'kb-team', name: '团队资料.md', mimeType: 'text/markdown', byteSize: 128,
    status: 'ready', stage: 'ready', progress: 1, error: '', chunkCount: 1, parserProvider: 'builtin',
    updatedAtMs: 1_700_000_000_000, revision: 1, sha256: 'a'.repeat(64), pageCount: 0, tokenCount: 20,
    parserVersion: 'builtin-1', indexedConfigRevision: 1, sourceReadPath: '',
  };
}

function knowledgeDetail(request: ControlRequest) {
  return {
    document: { ...knowledgeDocument(), id: String(request.params?.fileId ?? 'file-team') },
    chunks: { items: [{ chunkId: 'chunk-1', ordinal: 0, content: '共享项目资料中的关键词。' }], total: 1, hasMore: false },
    pages: [],
    artifact: { available: true, format: 'markdown', mimeType: 'text/markdown', byteSize: 128, lineCount: 1, sha256: 'a'.repeat(64) },
    contentWindow: { items: [{ lineNumber: 1, content: '共享项目资料中的关键词。' }], total: 1, hasMore: false },
    assets: [],
    tables: [],
  };
}

function receiptFor(fileName: string): KnowledgeDocumentImportReceipt {
  return { kbId: 'kb-team', documentId: `file-${fileName}`, fileName, mimeType: 'text/markdown', byteSize: 3, sha256: 'b'.repeat(64), status: 'queued' };
}

function latestRequest(transport: MockControlTransport, pathId: ControlRequest['pathId']): ControlRequest | undefined {
  return [...transport.requests].reverse().find(({ request }) => request.pathId === pathId)?.request;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => { resolve = nextResolve; });
  return { promise, resolve };
}
