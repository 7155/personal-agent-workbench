import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
  ArrowLeft,
  ArrowRight,
  BookOpen,
  Database,
  ExternalLink,
  FileSearch,
  Files,
  FolderPlus,
  MoreHorizontal,
  Network,
  RefreshCw,
  Search,
  ServerCog,
  Settings2,
  Trash2,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode, type RefObject } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Virtuoso } from 'react-virtuoso';
import { useControlTransport } from '@/app/control-transport';
import { AppSidebarToggle, useAppSidebar } from '@/paw-os/apps/app-sidebar';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Disclosure,
  EmptyState,
  Field,
  IconButton,
  Input,
  Menu,
  MenuContent,
  MenuItem,
  MenuLabel,
  MenuSeparator,
  MenuTrigger,
  Select,
  Switch,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  TextArea,
} from '@/components/primitives';
import {
  InlineNotice,
  QueryState,
  StatusBadge,
  asRecord,
  numberValue,
  publicErrorText,
} from '@/features/overview/management-ui';
import {
  ManagementMutationWorkflow,
  parseManagementWorkPreview,
  parseManagementWorkReceipt,
} from '@/features/overview/management-mutation';
import {
  configurationMutationPathIds,
  useConfigurationMutationBoundary,
} from '@/features/configuration/api';
import {
  chooseKnowledgeFiles,
  cancelKnowledgeJob,
  createKnowledgeBase,
  deleteKnowledgeBase,
  deleteKnowledgeDocument,
  importKnowledgeDocuments,
  knowledgeLibraryKeys,
  openKnowledgeHit,
  previewKnowledgeEmbeddingImpact,
  previewKnowledgeReindex,
  previewKnowledgeChunking,
  probeKnowledgeEmbedding,
  rebuildKnowledgeBase,
  retryKnowledgeDocument,
  searchKnowledgeBase,
  updateKnowledgeBase,
  useKnowledgeDocumentDetail,
  useKnowledgeLibraryQueries,
  type KnowledgeChunkingConfig,
  type KnowledgeChunkPreview,
  type DocumentKnowledgeBase,
  type KnowledgeDocument,
  type KnowledgeIndexRuntimeStatus,
  type KnowledgeEmbeddingCandidate,
  type KnowledgeEmbeddingImpact,
  type KnowledgeEmbeddingProfileState,
  type KnowledgeEmbeddingProvider,
  type KnowledgeParserMode,
  type KnowledgeRetrievalConfig,
  type KnowledgeSearchHit,
  type KnowledgeSearchResult,
  type KnowledgeSearchRetrieval,
  knowledgeIndexRuntimeStatus,
} from './api';
import { KnowledgeDocumentViewer, KnowledgeJobsPanel, KnowledgeMaterialsPanel, type KnowledgeUploadItem } from './document-workspace';
import { KnowledgeGraphPanel } from './knowledge-graph';
import { publicKnowledgeText } from './public-copy';
import { useKnowledgeReadingContext, type KnowledgeReadingController } from './reading-context';
import { usePawOsAppActive, usePawOsAppCompact, usePawOsAppIdentity } from '@/features/paw-os/surface-context';
import { usePageVisibility } from '@/platform/use-page-visibility';
import './knowledge.css';

type DetailTab = 'materials' | 'viewer' | 'search' | 'graph' | 'jobs' | 'settings';

const KNOWLEDGE_SEARCH_TIMEOUT_MS = 20_000;

export function KnowledgeFeature() {
  const appSurface = usePawOsAppIdentity();
  const sidebar = useAppSidebar('knowledge');
  const surfaceActive = usePawOsAppActive();
  const compact = usePawOsAppCompact();
  const pageVisible = usePageVisibility();
  const queriesEnabled = (surfaceActive ?? true) && pageVisible;
  const [searchParams, setSearchParams] = useSearchParams();
  const [selectedBaseId, setSelectedBaseId] = useState(searchParams.get('base') ?? '');
  const tab = asDetailTab(searchParams.get('tab') ?? 'search');
  // 从别处深链进来的一条资料：只在还没有有效选择时决定落点，之后由人自己开。
  const routeBaseId = searchParams.get('base') ?? '';
  const routeDocumentId = searchParams.get('document') ?? '';
  const [createOpen, setCreateOpen] = useState(false);
  const [deleteBaseOpen, setDeleteBaseOpen] = useState(false);
  const [documentToDelete, setDocumentToDelete] = useState<KnowledgeDocument | null>(null);
  const reading = useKnowledgeReadingContext(selectedBaseId, appSurface?.windowId ?? 'standalone');
  const { documentId: selectedDocumentId, focusHit: focusedHit } = reading.context;
  const setSelectedDocumentId = (documentId: string) => reading.update((current) => current.documentId === documentId ? current : { ...current, documentId });
  const setFocusedHit = (focusHit: KnowledgeSearchHit | null) => reading.update((current) => current.focusHit === focusHit ? current : { ...current, focusHit });
  const [reparseDocument, setReparseDocument] = useState<KnowledgeDocument | null>(null);
  const [uploadItems, setUploadItems] = useState<KnowledgeUploadItem[]>([]);
  const dialogTriggerRef = useRef<HTMLElement | null>(null);
  const libraryToolsTriggerRef = useRef<HTMLButtonElement | null>(null);
  const queries = useKnowledgeLibraryQueries(selectedBaseId, queriesEnabled);
  const queryClient = useQueryClient();
  const bases = queries.bases.data ?? [];
  const selectedBase = queries.base.data ?? bases.find((item) => item.id === selectedBaseId) ?? null;
  const documents = queries.documents.data ?? [];
  const detailQuery = useKnowledgeDocumentDetail(selectedBaseId, selectedDocumentId, queriesEnabled);
  const selectTab = (nextTab: DetailTab, replace = true) => {
    const next = new URLSearchParams(searchParams);
    next.set('tab', nextTab);
    if (selectedBaseId) next.set('base', selectedBaseId);
    if (nextTab === 'viewer' && selectedDocumentId) next.set('document', selectedDocumentId);
    setSearchParams(next, { replace });
  };
  const openSource = (documentId: string, origin: 'materials' | 'search' | 'graph', focusHit: KnowledgeSearchHit | null = null) => {
    reading.update((current) => ({ ...current, documentId, readerOrigin: origin, focusHit }));
    const next = new URLSearchParams(searchParams);
    next.set('base', selectedBaseId);
    next.set('document', documentId);
    next.set('tab', 'viewer');
    setSearchParams(next, { replace: true });
  };
  const selectBase = (baseId: string) => {
    setSelectedBaseId(baseId);
    setSearchParams({ base: baseId, tab: 'search' }, { replace: true });
  };

  useEffect(() => {
    if (!selectedBaseId && bases.length) {
      setSelectedBaseId(bases.find((item) => item.id === routeBaseId)?.id ?? bases[0]?.id ?? '');
    }
    if (selectedBaseId && bases.length && !bases.some((item) => item.id === selectedBaseId)) {
      setSelectedBaseId(bases[0]?.id ?? '');
    }
  }, [bases, routeBaseId, selectedBaseId]);

  useEffect(() => {
    // An explicit source link owns the read target even before the catalog
    // arrives, or when its document is not in the current catalog response.
    if (tab === 'viewer' && routeDocumentId && (!routeBaseId || routeBaseId === selectedBaseId)) {
      if (selectedDocumentId !== routeDocumentId) setSelectedDocumentId(routeDocumentId);
      return;
    }
    // A search/graph citation can be newer than the materials list. Its exact
    // document remains the read target while detail is fetched independently.
    if (selectedDocumentId && (focusedHit?.documentId === selectedDocumentId
      || (tab === 'viewer' && routeDocumentId === selectedDocumentId))) return;
    if (!queries.documents.isSuccess) return;
    if (!documents.length) {
      setSelectedDocumentId('');
      return;
    }
    if (!documents.some((item) => item.id === selectedDocumentId)) {
      setSelectedDocumentId(documents.find((item) => item.id === routeDocumentId)?.id ?? documents[0]?.id ?? '');
    }
  }, [documents, focusedHit?.documentId, queries.documents.isSuccess, routeBaseId, routeDocumentId, selectedBaseId, selectedDocumentId, tab]);

  useEffect(() => {
    setUploadItems([]);
  }, [selectedBaseId]);

  const refresh = () => void Promise.all([
    queries.bases.refetch(),
    queries.worker.refetch(),
    queries.parsers.refetch(),
    ...(selectedBaseId ? [queries.base.refetch(), queries.documents.refetch(), queries.jobs.refetch()] : []),
    ...(selectedBaseId && selectedDocumentId ? [detailQuery.refetch()] : []),
  ]);

  const invalidateBase = async (baseId = selectedBaseId) => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: knowledgeLibraryKeys.bases() }),
      ...(baseId ? [
        queryClient.invalidateQueries({ queryKey: knowledgeLibraryKeys.base(baseId) }),
        queryClient.invalidateQueries({ queryKey: knowledgeLibraryKeys.documents(baseId) }),
        queryClient.invalidateQueries({ queryKey: knowledgeLibraryKeys.jobs(baseId) }),
        queryClient.invalidateQueries({ queryKey: [...knowledgeLibraryKeys.root, 'document-detail', baseId] }),
        queryClient.invalidateQueries({ queryKey: [...knowledgeLibraryKeys.root, 'document-content', baseId] }),
      ] : []),
    ]);
  };

  const createMutation = useMutation({
    mutationFn: async (input: { name: string; description: string }) => {
      const created = await createKnowledgeBase(queries.transport, input);
      if (!created.id || created.name !== input.name) {
        throw new Error('知识服务没有确认新知识库，未关闭创建窗口。');
      }
      return created;
    },
    onSuccess: async (base) => {
      queryClient.setQueryData<DocumentKnowledgeBase[]>(knowledgeLibraryKeys.bases(), (current = []) => (
        current.some((item) => item.id === base.id)
          ? current.map((item) => item.id === base.id ? base : item)
          : [...current, base]
      ));
      queryClient.setQueryData(knowledgeLibraryKeys.base(base.id), base);
      setSelectedBaseId(base.id);
      setCreateOpen(false);
      await invalidateBase(base.id);
    },
  });
  const deleteBaseMutation = useMutation({
    mutationFn: () => selectedBase
      ? deleteKnowledgeBase(queries.transport, selectedBase)
      : Promise.reject(new Error('没有选中的知识库。')),
    onSuccess: async () => {
      setDeleteBaseOpen(false);
      setSelectedBaseId('');
      await invalidateBase('');
    },
  });
  const importMutation = useMutation({
    mutationFn: async ({ retryItem, droppedFiles }: { retryItem?: KnowledgeUploadItem; droppedFiles?: File[] }) => {
      if (!selectedBase) return [];
      const parser = retryItem?.parser ?? selectedBase.parser;
      if (queries.transport.kind !== 'http') {
        try {
          const receipts = await importKnowledgeDocuments(queries.transport, {
            kbId: selectedBase.id,
            parserProvider: parser === 'mineru' ? 'mineru_local_http' : parser,
            maxFiles: 20,
          });
          setUploadItems(receipts.map((receipt) => ({ id: receipt.documentId, fileName: receipt.fileName, byteSize: receipt.byteSize, parser, status: 'accepted', documentId: receipt.documentId, error: '' })));
          return receipts;
        } catch (error) {
          setUploadItems([{ id: 'native-import', fileName: '本机文件导入', byteSize: 0, parser, status: 'failed', documentId: '', error: publicErrorText(error, '本机文件导入失败。') }]);
          throw error;
        }
      }
      const files = retryItem?.file
        ? [retryItem.file]
        : droppedFiles?.length
          ? droppedFiles.slice(0, 20)
          : await chooseKnowledgeFiles(20);
      if (!files.length) return [];
      const queue = retryItem ? [retryItem] : files.map((file, index) => ({
        id: uploadItemId(file, index), fileName: file.name, byteSize: file.size, file, parser, status: 'queued' as const, documentId: '', error: '',
      }));
      if (!retryItem) setUploadItems(queue);
      const receipts = [];
      const errors: string[] = [];
      for (const item of queue) {
        setUploadItems((current) => replaceUploadItem(current, item.id, { status: 'uploading', error: '' }));
        try {
          const [receipt] = await importKnowledgeDocuments(queries.transport, {
            kbId: selectedBase.id,
            files: item.file ? [item.file] : undefined,
            parserProvider: parser === 'mineru' ? 'mineru_local_http' : parser,
            maxFiles: 1,
          });
          if (!receipt) throw new Error('导入服务没有返回文件回执。');
          receipts.push(receipt);
          setUploadItems((current) => replaceUploadItem(current, item.id, { status: 'accepted', documentId: receipt.documentId, error: '' }));
        } catch (error) {
          const message = publicErrorText(error, '上传失败。');
          errors.push(`${item.fileName}: ${message}`);
          setUploadItems((current) => replaceUploadItem(current, item.id, { status: 'failed', error: message }));
        }
      }
      if (errors.length) throw new Error(errors.join('\n'));
      return receipts;
    },
    onSettled: () => invalidateBase(),
  });
  const retryMutation = useMutation({
    mutationFn: ({ document, parser }: { document: KnowledgeDocument; parser: KnowledgeParserMode }) => selectedBase
      ? retryKnowledgeDocument(queries.transport, selectedBase, document, { parser })
      : Promise.reject(new Error('没有选中的知识库。')),
    onSuccess: async () => {
      setReparseDocument(null);
      selectTab('jobs');
      await invalidateBase();
    },
  });
  const deleteDocumentMutation = useMutation({
    mutationFn: (documentId: string) => deleteKnowledgeDocument(queries.transport, selectedBaseId, documentId),
    onSuccess: async () => {
      setDocumentToDelete(null);
      await invalidateBase();
    },
  });
  const updateMutation = useMutation({
    mutationFn: async (patch: {
      name?: string;
      description?: string;
      agentEnabled?: boolean;
      parser?: KnowledgeParserMode;
      chunkingConfig?: KnowledgeChunkingConfig;
      retrievalConfig?: KnowledgeRetrievalConfig;
    }) => {
      if (!selectedBase) throw new Error('没有选中的知识库。');
      const updated = await updateKnowledgeBase(queries.transport, selectedBase, patch);
      if (
        (patch.name !== undefined && updated.name !== patch.name)
        || (patch.description !== undefined && updated.description !== patch.description)
      ) {
        throw new Error('知识服务没有确认基本信息更新，页面仍保留你的输入。');
      }
      return updated;
    },
    onSuccess: async (base) => {
      queryClient.setQueryData(knowledgeLibraryKeys.base(base.id), base);
      queryClient.setQueryData<DocumentKnowledgeBase[]>(knowledgeLibraryKeys.bases(), (current = []) => (
        current.map((item) => item.id === base.id ? base : item)
      ));
      await invalidateBase(base.id);
    },
  });
  const rebuildMutation = useMutation({
    mutationFn: async () => {
      if (!selectedBase) throw new Error('没有选中的知识库。');
      const preview = await previewKnowledgeReindex(queries.transport, selectedBase);
      await rebuildKnowledgeBase(queries.transport, selectedBase, preview);
    },
    onSuccess: async () => {
      selectTab('jobs');
      await invalidateBase();
    },
  });
  const cancelJobMutation = useMutation({
    mutationFn: (jobId: string) => cancelKnowledgeJob(queries.transport, selectedBaseId, jobId),
    onSettled: () => invalidateBase(),
  });
  const chunkPreviewMutation = useMutation({
    mutationFn: ({ documentId, config }: { documentId: string; config: KnowledgeChunkingConfig }) => (
      selectedBase
        ? previewKnowledgeChunking(queries.transport, selectedBase.id, documentId, config)
        : Promise.reject(new Error('没有选中的知识库。'))
    ),
  });

  const pageError = queries.bases.error as Error | null;
  const worker = workerState(queries.worker.data, queries.worker.error);
  const rememberDialogTrigger = (trigger?: HTMLElement) => {
    const candidate = trigger ?? document.activeElement;
    dialogTriggerRef.current = candidate instanceof HTMLElement
      ? candidate
      : null;
  };
  const Surface = appSurface ? 'section' : 'main';

  return (
    <Surface
      aria-label={appSurface ? '知识库' : undefined}
      className="knowledge-feature knowledge-feature--migrated-v1"
      data-knowledge-view={tab}
      data-paw-os-app={appSurface?.appId}
      data-paw-os-compact={compact || undefined}
      data-route-id="knowledge"
      role={appSurface ? 'region' : undefined}
    >
      <h1 className="knowledge-feature__title">知识库</h1>
      <QueryState error={pageError} isPending={queries.bases.isPending} onRetry={refresh}>
        <div className="knowledge-library" data-empty={!bases.length || undefined} data-native-layout={appSurface ? 'app' : undefined} data-sidebar-collapsed={appSurface ? sidebar.collapsed : undefined}>
          {/* Inside a PAWOS window the library index and the workspace share one
              window-bound grid. The command band spans both columns and always
              carries service health and library actions; the rail below it is
              the wide-window selector. A narrow window drops the rail and the
              band's labelled 当前知识库 selector takes over — both states live
              in the same DOM so the swap is a container query, not a resize
              re-render. */}
          {appSurface ? (
            <KnowledgeBaseSwitcher
              base={selectedBase}
              bases={bases}
              onCreate={(trigger) => { rememberDialogTrigger(trigger); setCreateOpen(true); }}
              onRefresh={refresh}
              onSelect={selectBase}
              refreshing={queries.bases.isFetching || queries.worker.isFetching}
              selectedBaseId={selectedBaseId}
              sidebarToggle={<AppSidebarToggle collapsed={sidebar.collapsed} controlsId={sidebar.controlsId} label="知识库目录" onToggle={() => sidebar.setCollapsed(!sidebar.collapsed)} toggleRef={sidebar.toggleRef} />}
              worker={worker}
            />
          ) : null}
          <KnowledgeBaseRail
            bases={bases}
            onCreate={(trigger) => { rememberDialogTrigger(trigger); setCreateOpen(true); }}
            onRefresh={refresh}
            onSelect={selectBase}
            refreshing={queries.bases.isFetching || queries.worker.isFetching}
            selectedBaseId={selectedBaseId}
            sidebar={appSurface ? sidebar : undefined}
            variant={appSurface ? 'app' : 'web'}
            worker={worker}
          />
          <section className="knowledge-library__detail" aria-label="知识库详情">
            {selectedBase ? (
              <>
                {/* Inside a PAWOS window the command band above already carries
                    the library identity and counts, so the tab
                    workspace is the first object on screen. The web route keeps
                    the full header sheet. */}
                {appSurface ? null : <KnowledgeBaseHeader base={selectedBase} worker={worker} />}
                <Tabs className="knowledge-library__tabs" onValueChange={(value) => selectTab(asDetailTab(value))} value={tab}>
                  <TabsList aria-label="知识库管理视图">
                    <TabsTrigger value="search"><Search aria-hidden="true" size={14} />搜索</TabsTrigger>
                    <TabsTrigger value="materials"><Files aria-hidden="true" size={14} />资料</TabsTrigger>
                    <TabsTrigger aria-label="查看材料" value="viewer"><BookOpen aria-hidden="true" size={14} />阅读</TabsTrigger>
                  </TabsList>
                  <Menu>
                    <MenuTrigger asChild>
                      <Button aria-label="更多知识库工具" className="knowledge-library__tools" data-current-tool={['graph', 'jobs', 'settings'].includes(tab) ? tab : undefined} leadingIcon={<MoreHorizontal size={15} />} ref={libraryToolsTriggerRef} size="small" variant="quiet">
                        {tab === 'graph' ? '图谱' : tab === 'jobs' ? '处理记录' : tab === 'settings' ? '设置' : '更多'}
                      </Button>
                    </MenuTrigger>
                    <MenuContent align="end" onCloseAutoFocus={(event) => { if (deleteBaseOpen) event.preventDefault(); }}>
                      <MenuItem onSelect={() => selectTab('graph')}><Network size={14} />知识图谱</MenuItem>
                      <MenuItem onSelect={() => selectTab('jobs')}><RefreshCw size={14} />处理记录</MenuItem>
                      <MenuItem onSelect={() => selectTab('settings')}><Settings2 size={14} />设置</MenuItem>
                      <MenuLabel>知识服务：{worker.label}</MenuLabel>
                      <MenuSeparator />
                      <MenuItem onSelect={() => { rememberDialogTrigger(libraryToolsTriggerRef.current ?? undefined); setDeleteBaseOpen(true); }}><Trash2 size={14} />删除知识库</MenuItem>
                    </MenuContent>
                  </Menu>
                  <TabsContent value="materials">
                    <KnowledgeMaterialsPanel
                      key={selectedBase.id}
                      detail={detailQuery.data ?? null}
                      detailError={detailQuery.error as Error | null}
                      detailLoading={detailQuery.isPending && Boolean(selectedDocumentId)}
                      documents={documents}
                      dropSupported={queries.transport.kind === 'http'}
                      error={queries.documents.error as Error | null}
                      loading={queries.documents.isPending}
                      filter={reading.context.materialsFilter}
                      onFilterChange={(materialsFilter) => reading.update((current) => ({ ...current, materialsFilter }))}
                      importError={importMutation.error as Error | null}
                      importing={importMutation.isPending}
                      onDelete={(document, trigger) => { rememberDialogTrigger(trigger); setDocumentToDelete(document); }}
                      onClearUploads={() => { importMutation.reset(); setUploadItems([]); }}
                      onImport={() => importMutation.mutate({})}
                      onImportFiles={(files) => importMutation.mutate({ droppedFiles: files })}
                      onOpen={(documentId) => openSource(documentId, 'materials')}
                      onReparse={(document, trigger) => { rememberDialogTrigger(trigger); setReparseDocument(document); }}
                      onRetryList={() => void queries.documents.refetch()}
                      onRetryDetail={() => void detailQuery.refetch()}
                      onRetryUpload={(item) => importMutation.mutate({ retryItem: item })}
                      onSelect={(documentId) => { setFocusedHit(null); setSelectedDocumentId(documentId); }}
                      pendingDocumentId={retryMutation.isPending ? retryMutation.variables?.document.id ?? '' : ''}
                      selectedDocumentId={selectedDocumentId}
                      uploadItems={uploadItems}
                    />
                  </TabsContent>
                  <TabsContent value="viewer">
                    <KnowledgeDocumentViewer
                      detail={detailQuery.data ?? null}
                      documents={documents}
                      error={(selectedDocumentId ? detailQuery.error : queries.documents.error) as Error | null}
                      loading={selectedDocumentId ? detailQuery.isPending : queries.documents.isPending}
                      onRetry={() => { if (selectedDocumentId) void detailQuery.refetch(); else void queries.documents.refetch(); }}
                      focusHit={focusedHit}
                      onBack={() => selectTab(reading.context.readerOrigin)}
                      backLabel={reading.context.readerOrigin === 'search' ? '返回搜索结果' : reading.context.readerOrigin === 'graph' ? '返回图谱' : '返回资料'}
                      onImportMaterials={() => selectTab('materials')}
                      hasMoreChunks={Boolean(detailQuery.hasNextPage)}
                      hasMoreContent={Boolean(detailQuery.hasNextContentPage)}
                      loadMoreChunksFailed={detailQuery.isFetchNextPageError}
                      loadingMoreChunks={detailQuery.isFetchingNextPage}
                      loadingMoreContent={detailQuery.isFetchingNextContentPage}
                      onLoadMoreChunks={() => void detailQuery.fetchNextPage()}
                      onLoadMoreContent={() => void detailQuery.fetchNextContentPage()}
                      onSelectDocument={(documentId) => openSource(documentId, reading.context.readerOrigin)}
                      selectedDocumentId={selectedDocumentId}
                      transport={queries.transport}
                    />
                  </TabsContent>
                  <TabsContent value="search">
                    <KnowledgeSearchPanel key={selectedBase.id} base={selectedBase} reading={reading} onOpenHit={(hit) => openSource(hit.documentId, 'search', hit)} transport={queries.transport} />
                  </TabsContent>
                  <TabsContent aria-label="知识图谱" aria-labelledby={undefined} value="graph">
                    <KnowledgeGraphPanel
                      active={queriesEnabled}
                      base={selectedBase}
                      documents={documents}
                      onOpenSource={(node) => {
                        if (!node.documentId) return;
                        const document = documents.find((item) => item.id === node.documentId);
                        openSource(node.documentId, 'graph', node.chunkId ? {
                          id: node.chunkId,
                          documentId: node.documentId,
                          documentName: node.documentName || document?.name || '',
                          title: node.heading || node.label,
                          excerpt: node.excerpt,
                          score: node.weight,
                          page: node.page,
                          heading: node.heading,
                          lineStart: null,
                          lineEnd: null,
                          diagnostics: { effectiveMode: 'unknown', lexicalRank: null, denseRank: null, graphRank: null, lexicalScore: null, denseScore: null, graphScore: null, retrievalScore: null, retrievalRank: null, rerankScore: null, rerankRank: null, rerankProvider: '', rerankFingerprint: '', independentRerankStage: false, subagentSubstitute: false, graphMatches: [], graphPaths: [] },
                        } : null);
                      }}
                      transport={queries.transport}
                    />
                  </TabsContent>
                  <TabsContent aria-label="处理记录" aria-labelledby={undefined} value="jobs">
                    <KnowledgeJobsPanel
                      cancelError={cancelJobMutation.error}
                      cancellingJobId={cancelJobMutation.isPending ? cancelJobMutation.variables ?? '' : ''}
                      error={queries.jobs.error as Error | null}
                      jobs={queries.jobs.data ?? []}
                      loading={queries.jobs.isFetching}
                      onCancel={(jobId) => cancelJobMutation.mutate(jobId)}
                      onRefresh={() => void queries.jobs.refetch()}
                    />
                  </TabsContent>
                  <TabsContent aria-label="知识库设置" aria-labelledby={undefined} value="settings">
                    <KnowledgeSettingsPanel
                      key={selectedBase.id}
                      base={selectedBase}
                      documents={documents}
                      embeddingState={queries.embeddingProfile.data}
                      embeddingStateError={queries.embeddingProfile.error}
                      indexRuntime={knowledgeIndexRuntimeStatus(queries.worker.data)}
                      onAgentEnabled={(agentEnabled) => updateMutation.mutate({ agentEnabled })}
                      onParser={(parser) => updateMutation.mutate({ parser })}
                      onSaveInfo={(name, description) => updateMutation.mutate({ name, description })}
                      onSaveChunking={(chunkingConfig) => updateMutation.mutate({ chunkingConfig })}
                      onSaveRetrieval={(retrievalConfig) => updateMutation.mutate({ retrievalConfig })}
                      onPreviewChunking={(documentId, config) => chunkPreviewMutation.mutate({ documentId, config })}
                      onRebuild={() => rebuildMutation.mutate()}
                      parserData={queries.parsers.data}
                      pending={updateMutation.isPending}
                      updateError={updateMutation.error}
                      rebuildError={rebuildMutation.error}
                      rebuilding={rebuildMutation.isPending}
                      chunkPreview={chunkPreviewMutation.data ?? null}
                      chunkPreviewError={chunkPreviewMutation.error}
                      chunkPreviewing={chunkPreviewMutation.isPending}
                      refreshParser={() => void Promise.all([queries.parsers.refetch(), queries.worker.refetch()])}
                      settingsEnvelope={queries.settings.data}
                      worker={worker}
                    />
                  </TabsContent>
                </Tabs>
              </>
            ) : (
              <EmptyState
                action={<Button leadingIcon={<FolderPlus size={15} />} onClick={(event) => { rememberDialogTrigger(event.currentTarget); setCreateOpen(true); }} variant="primary">新建知识库</Button>}
                description="为项目资料、论文或产品文档建立独立知识库。"
                icon={Database}
                title="还没有文档知识库"
              />
            )}
          </section>
        </div>
      </QueryState>
      <CreateKnowledgeBaseDialog
        error={createMutation.error}
        loading={createMutation.isPending}
        onCreate={(input) => createMutation.mutate(input)}
        onOpenChange={(open) => { setCreateOpen(open); if (!open) createMutation.reset(); }}
        open={createOpen}
        returnFocusRef={dialogTriggerRef}
      />
      <ConfirmDialog
        description={selectedBase ? `将删除“${selectedBase.name}”及其文档索引。个人记忆不会受到影响。` : ''}
        error={deleteBaseMutation.error}
        loading={deleteBaseMutation.isPending}
        onConfirm={() => deleteBaseMutation.mutate()}
        onOpenChange={setDeleteBaseOpen}
        open={deleteBaseOpen}
        returnFocusRef={dialogTriggerRef}
        title="删除文档知识库"
      />
      <ConfirmDialog
        description={documentToDelete ? `将移除“${documentToDelete.name}”及其索引段落。` : ''}
        error={deleteDocumentMutation.error}
        loading={deleteDocumentMutation.isPending}
        onConfirm={() => { if (documentToDelete) deleteDocumentMutation.mutate(documentToDelete.id); }}
        onOpenChange={(open) => { if (!open) setDocumentToDelete(null); }}
        open={Boolean(documentToDelete)}
        returnFocusRef={dialogTriggerRef}
        title="删除文档"
      />
      <ReparseDocumentDialog
        document={reparseDocument}
        error={retryMutation.error}
        loading={retryMutation.isPending}
        onConfirm={(parser) => { if (reparseDocument) retryMutation.mutate({ document: reparseDocument, parser }); }}
        onOpenChange={(open) => { if (!open) { setReparseDocument(null); retryMutation.reset(); } }}
        returnFocusRef={dialogTriggerRef}
      />
    </Surface>
  );
}

function KnowledgeBaseRail({
  bases,
  onCreate,
  onRefresh,
  onSelect,
  refreshing,
  selectedBaseId,
  sidebar,
  variant = 'web',
  worker,
}: {
  bases: readonly DocumentKnowledgeBase[];
  onCreate: (trigger: HTMLElement) => void;
  onRefresh: () => void;
  onSelect: (baseId: string) => void;
  refreshing: boolean;
  selectedBaseId: string;
  sidebar?: ReturnType<typeof useAppSidebar>;
  variant?: 'web' | 'app';
  worker: WorkerState;
}) {
  // In a PAWOS window the command band above the rail already owns refresh,
  // create, service health and the narrow-window selector, so the app
  // rail carries only the library index. Duplicating those controls would put
  // two identically named buttons in the same window.
  const app = variant === 'app';
  return (
    <aside className="knowledge-base-rail" aria-label="文档知识库" data-variant={variant} {...sidebar?.contentProps}>
      <header>
        <div><strong>知识库</strong><span>{bases.length} 个独立库</span></div>
        {app ? null : (
          <div className="knowledge-base-rail__actions">
            <IconButton disabled={refreshing} icon={<RefreshCw size={14} />} label="刷新知识库" onClick={onRefresh} size="small" tooltip />
            <IconButton icon={<FolderPlus size={15} />} label="新建知识库" onClick={(event) => onCreate(event.currentTarget)} size="small" tooltip />
          </div>
        )}
      </header>
      {app ? null : (
        <div className="knowledge-base-rail__mobile">
          <Field htmlFor="knowledge-mobile-base" label="当前知识库">
            <Select
              disabled={!bases.length}
              id="knowledge-mobile-base"
              onValueChange={onSelect}
              options={bases.map((base) => ({
                value: base.id,
                label: `${base.name} · ${base.documentCount} 个文件`,
              }))}
              value={selectedBaseId || bases[0]?.id || ''}
            />
          </Field>
          <span className="knowledge-base-rail__mobile-worker" data-state={worker.tone}>
            <i aria-hidden="true" />
            {worker.label}
          </span>
          <div className="knowledge-base-rail__actions">
            <IconButton disabled={refreshing} icon={<RefreshCw size={15} />} label="刷新知识库" onClick={onRefresh} size="large" tooltip />
            <IconButton icon={<FolderPlus size={16} />} label="新建知识库" onClick={(event) => onCreate(event.currentTarget)} size="large" tooltip />
          </div>
        </div>
      )}
      {app ? null : (
        <div className="knowledge-base-rail__worker" data-state={worker.tone}>
          <i aria-hidden="true" />
          <span>知识服务</span>
          <b>{worker.label}</b>
        </div>
      )}
      {bases.length ? (
        <Virtuoso
          className="knowledge-base-rail__list"
          data={bases}
          itemContent={(_index, base) => (
            <button
              aria-label={`${base.name}，${base.documentCount} 个文件，${base.chunkCount} 个段落`}
              aria-current={base.id === selectedBaseId ? 'page' : undefined}
              className="knowledge-base-row"
              data-selected={base.id === selectedBaseId || undefined}
              onClick={() => onSelect(base.id)}
              type="button"
            >
              <span className="knowledge-base-row__icon"><BookOpen size={15} /></span>
              <span><strong>{base.name}</strong><small>{base.documentCount} 个文件 · {base.chunkCount} 个段落</small></span>
              {base.agentEnabled ? <span className="knowledge-base-row__agent">伙伴可用</span> : null}
            </button>
          )}
        />
      ) : <p className="knowledge-base-rail__empty">新建一个库后再导入资料。</p>}
    </aside>
  );
}

/** PAWOS window command band: library identity, live counts, service health,
    and library-level actions in one row, so the workspace below starts at the
    top of the window instead of under a stacked header sheet. A wide window
    reads its selection from the rail and shows the library name here; a narrow
    window hides the rail and promotes the labelled 当前知识库 selector. */
function KnowledgeBaseSwitcher({
  base,
  bases,
  onCreate,
  onRefresh,
  onSelect,
  refreshing,
  selectedBaseId,
  sidebarToggle,
  worker,
}: {
  base: DocumentKnowledgeBase | null;
  bases: readonly DocumentKnowledgeBase[];
  onCreate: (trigger: HTMLElement) => void;
  onRefresh: () => void;
  onSelect: (baseId: string) => void;
  refreshing: boolean;
  selectedBaseId: string;
  sidebarToggle?: ReactNode;
  worker: WorkerState;
}) {
  return (
    <section aria-label="切换文档知识库" className="knowledge-base-switcher">
      {sidebarToggle}
      <p className="knowledge-base-switcher__current">{base ? base.name : '还没有知识库'}</p>
      <Field htmlFor="knowledge-native-base" label="当前知识库">
        <Select
          disabled={!bases.length}
          id="knowledge-native-base"
          onValueChange={onSelect}
          options={bases.map((item) => ({
            value: item.id,
            label: `${item.name} · ${item.documentCount} 个文件`,
          }))}
          value={selectedBaseId || bases[0]?.id || ''}
        />
      </Field>
      {base ? (
        <span aria-label={`${base.documentCount} 个文件，${base.chunkCount} 个段落`} className="knowledge-base-switcher__meta">
          <b>{base.documentCount}</b> 文件
          <i aria-hidden="true" />
          <b>{base.chunkCount}</b> 段落
        </span>
      ) : null}
      {worker.tone !== 'success' ? <span className="knowledge-base-switcher__worker" data-state={worker.tone}>
        <i aria-hidden="true" />
        知识服务：{worker.label}
      </span> : null}
      <div className="knowledge-base-switcher__actions">
        <IconButton disabled={refreshing} icon={<RefreshCw size={15} />} label="刷新知识库" onClick={onRefresh} size="small" tooltip />
        <Button leadingIcon={<FolderPlus size={15} />} onClick={(event) => onCreate(event.currentTarget)} size="small">新建知识库</Button>
      </div>
    </section>
  );
}

function KnowledgeBaseHeader({ base, worker }: { base: DocumentKnowledgeBase; worker: WorkerState }) {
  return (
    <header className="knowledge-base-header">
      <div>
        <h2>{base.name}</h2>
        <p>{base.description || '这个库还没有说明。'}</p>
      </div>
      <dl>
        <div><dt>文件</dt><dd>{base.documentCount}</dd></div>
        <div><dt>段落</dt><dd>{base.chunkCount}</dd></div>
        <div><dt>解析</dt><dd>{parserLabel(base.parser)}</dd></div>
        <div><dt>服务</dt><dd><StatusBadge label={worker.label} tone={worker.tone} /></dd></div>
      </dl>
    </header>
  );
}

function KnowledgeSearchPanel({ base, reading, onOpenHit, transport }: { base: DocumentKnowledgeBase; reading: KnowledgeReadingController; onOpenHit: (hit: KnowledgeSearchHit) => void; transport: ReturnType<typeof useKnowledgeLibraryQueries>['transport'] }) {
  const snapshot = reading.context.search;
  const [draft, setLocalDraft] = useState(snapshot.draft);
  const [detailOpen, setDetailOpen] = useState(false);
  const [pendingSince, setPendingSince] = useState<number | null>(null);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [searchNotice, setSearchNotice] = useState('');
  const searchAbortRef = useRef<AbortController | null>(null);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const resultRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const backRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef(false);
  const setDraft = (value: string) => {
    setLocalDraft(value);
    reading.update((current) => ({ ...current, search: { ...current.search, draft: value } }));
  };
  const searchMutation = useMutation({
    mutationFn: ({ query, config, signal }: { query: string; config: KnowledgeRetrievalConfig; request: number; signal: AbortSignal }) => searchKnowledgeBase(transport, base.id, query, config, signal),
    onSuccess: (result: KnowledgeSearchResult, input) => {
      clearSearchObservation();
      setSearchNotice('');
      reading.update((current) => current.search.request !== input.request ? current : ({
        ...current, search: { ...current.search, hits: result.hits, retrieval: result.retrieval, selectedId: result.hits[0]?.id ?? '', status: 'success', error: '' },
      }));
    },
    onError: (error, input) => {
      if (isAbortError(error)) return;
      clearSearchObservation();
      reading.update((current) => current.search.request !== input.request ? current : ({
        ...current, search: { ...current.search, status: 'error', error: publicErrorText(error, '知识服务暂时无法完成检索。') },
      }));
    },
  });
  const hits = snapshot.hits;
  const selected = hits.find((item) => item.id === snapshot.selectedId) ?? hits[0] ?? null;
  const config = snapshot.config ?? base.retrievalConfig;
  const retrieval = snapshot.retrieval;
  const displayedConfig = retrieval?.config ?? config;
  function clearSearchObservation(): void {
    if (searchTimerRef.current) {
      clearTimeout(searchTimerRef.current);
      searchTimerRef.current = null;
    }
    searchAbortRef.current = null;
    setPendingSince(null);
  }
  function cancelSearch(
    reason: 'manual' | 'timeout' = 'manual',
    request = snapshot.request,
    controller = searchAbortRef.current,
  ): void {
    if (!controller || searchAbortRef.current !== controller) return;
    clearSearchObservation();
    controller.abort();
    if (reason === 'timeout') {
      reading.update((current) => current.search.request !== request ? current : ({
        ...current,
        search: { ...current.search, status: 'error', error: `检索超过 ${KNOWLEDGE_SEARCH_TIMEOUT_MS / 1000} 秒，已自动取消。可以缩小关键词范围后重试。` },
      }));
      return;
    }
    setSearchNotice('已取消检索，可以重新搜索。');
    reading.update((current) => current.search.request !== request ? current : ({
      ...current,
      search: { ...current.search, status: 'idle', error: '', hits: [], retrieval: null, selectedId: '' },
    }));
  }
  useEffect(() => {
    if (snapshot.status !== 'pending' || pendingSince === null) return;
    const updateElapsed = () => setElapsedMs(Math.max(0, Date.now() - pendingSince));
    updateElapsed();
    const interval = globalThis.setInterval(updateElapsed, 250);
    return () => globalThis.clearInterval(interval);
  }, [pendingSince, snapshot.status]);
  useEffect(() => () => {
    if (searchTimerRef.current) globalThis.clearTimeout(searchTimerRef.current);
    searchAbortRef.current?.abort();
  }, []);
  useEffect(() => {
    if (detailOpen && backRef.current && getComputedStyle(backRef.current).display !== 'none') {
      backRef.current.focus();
    } else if (!detailOpen && returnFocusRef.current) {
      returnFocusRef.current = false;
      resultRefs.current[hits.findIndex((hit) => hit.id === selected?.id)]?.focus();
    }
  }, [detailOpen, hits, selected?.id]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!draft.trim() || snapshot.status === 'pending') return;
    setDetailOpen(false);
    setSearchNotice('');
    const config = { ...base.retrievalConfig };
    const next = reading.update((current) => ({ ...current, search: {
      ...current.search, query: draft.trim(), config, hits: [], retrieval: null, selectedId: '', status: 'pending', error: '', request: current.search.request + 1,
    } }));
    const controller = new AbortController();
    searchAbortRef.current?.abort();
    searchAbortRef.current = controller;
    setPendingSince(Date.now());
    setElapsedMs(0);
    searchTimerRef.current = globalThis.setTimeout(() => cancelSearch('timeout', next.search.request, controller), KNOWLEDGE_SEARCH_TIMEOUT_MS);
    searchMutation.mutate({ query: next.search.query, config, request: next.search.request, signal: controller.signal });
  };
  return (
    <div className="knowledge-panel knowledge-search">
      <form className="knowledge-search__form" onSubmit={submit}>
        <Field htmlFor="knowledge-library-search" label="搜索知识库" description={`在「${publicKnowledgeText(base.name)}」的资料中查找。`}>
          <Input id="knowledge-library-search" onChange={(event) => setDraft(event.target.value)} placeholder="输入一个问题或关键词" value={draft} />
        </Field>
        <Button disabled={!draft.trim() || snapshot.status === 'pending'} leadingIcon={<Search size={15} />} loading={snapshot.status === 'pending'} type="submit" variant="primary">搜索</Button>
      </form>
      <Disclosure className="knowledge-search__config" summary="搜索范围与方式">
        <p>只查询当前知识库，不读取个人记忆。{snapshot.config ? '下列配置对应当前结果。' : ''}</p>
        <p>{retrievalModeLabel(displayedConfig.mode)} · 最多显示 {displayedConfig.topK} 条 · 最低相关度 {displayedConfig.threshold.toFixed(2)} · {displayedConfig.rerankEnabled ? `Reranker：${retrieval?.reranker.provider || '已启用'}` : 'Reranker：关闭'}</p>
      </Disclosure>
      {snapshot.error ? <InlineNotice title="检索失败" tone="warning">{snapshot.error}</InlineNotice> : null}
      {searchNotice ? <p className="knowledge-search__notice" role="status">{searchNotice}</p> : null}
      {snapshot.status === 'pending' ? (
        <div className="knowledge-search__pending" role="status">
          <span><strong>正在查找相关来源…</strong><small>已等待 {Math.max(1, Math.ceil(elapsedMs / 1000))} 秒 · 超过 {KNOWLEDGE_SEARCH_TIMEOUT_MS / 1000} 秒会自动取消</small></span>
          <Button onClick={() => cancelSearch()} size="small" variant="quiet">取消</Button>
        </div>
      ) : null}
      {snapshot.status === 'success' ? <p className="knowledge-search__result-summary" role="status">“{snapshot.query}” · {hits.length} 条结果</p> : null}
      {retrieval ? <KnowledgeRetrievalSummary retrieval={retrieval} /> : null}
      {hits.length ? (
        <div className="knowledge-search__results" data-detail-open={detailOpen || undefined}>
          <div className="knowledge-search__list" role="listbox" aria-label="检索结果" onKeyDown={(event) => {
            if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
            event.preventDefault();
            const index = hits.findIndex((hit) => hit.id === selected?.id);
            const nextIndex = event.key === 'Home' ? 0 : event.key === 'End' ? hits.length - 1 : Math.max(0, Math.min(hits.length - 1, index + (event.key === 'ArrowDown' ? 1 : -1)));
            const hit = hits[nextIndex];
            if (!hit) return;
            reading.update((current) => ({ ...current, search: { ...current.search, selectedId: hit.id } }));
            resultRefs.current[nextIndex]?.focus();
          }}>
            {hits.map((hit, index) => (
              <button aria-selected={selected?.id === hit.id} data-selected={selected?.id === hit.id || undefined} key={hit.id} onClick={() => { reading.update((current) => ({ ...current, search: { ...current.search, selectedId: hit.id } })); setDetailOpen(true); }} ref={(node) => { resultRefs.current[index] = node; }} role="option" tabIndex={selected?.id === hit.id ? 0 : -1} type="button">
                <span><strong>{publicKnowledgeText(hit.documentName)}</strong><small>{hitRankLabel(hit, index)} · {publicKnowledgeText(hit.title)} · {citationLabel(hit)}{hit.diagnostics.graphRank === null ? '' : ' · 图谱关联'}</small><small>{publicKnowledgeText(hit.excerpt) || '没有可显示的摘录'}</small></span>
                <b data-level={relevanceLevel(hit.score)}>{relevanceLabel(hit.score)}</b>
              </button>
            ))}
          </div>
          {selected ? <div className="knowledge-search__reader">
            <Button className="knowledge-search__back" leadingIcon={<ArrowLeft size={14} />} onClick={() => { returnFocusRef.current = true; setDetailOpen(false); }} ref={backRef} size="small" variant="quiet">返回检索结果</Button>
            <KnowledgeHitDetail baseId={base.id} hit={selected} rank={Math.max(1, hits.findIndex((item) => item.id === selected.id) + 1)} onOpen={onOpenHit} transport={transport} />
          </div> : null}
        </div>
      ) : snapshot.status === 'success' ? (
        <EmptyState description="换一个关键词，或检查文件是否已经完成索引。" icon={FileSearch} title="没有匹配段落" />
      ) : snapshot.status === 'idle' ? (
        <p className="knowledge-search__hint">输入问题或关键词。找到相关段落后，可以打开原文核对。</p>
      ) : null}
    </div>
  );
}

function KnowledgeRetrievalSummary({ retrieval }: { retrieval: KnowledgeSearchRetrieval }) {
  const library = retrieval.libraries[0];
  const config = retrieval.config ?? library?.config;
  return (
    <section aria-label="本次召回诊断" className="knowledge-search__retrieval-summary">
      <header><strong>本次召回诊断</strong><span>{retrievalModeLabelValue(retrieval.effectiveMode)} · {library ? `${library.returned} 条返回` : '服务未返回分阶段统计'}</span></header>
      <dl>
        <div><dt>候选规模</dt><dd>{library ? `${library.candidateLimit}（关键词 ${library.lexicalCandidates} · 向量 ${library.denseCandidates} · 图谱 ${library.graphCandidates}）` : '未报告'}</dd></div>
        <div><dt>图谱状态</dt><dd>{library ? `${library.graphStatus}${library.graphMatchedNodes ? ` · 关联 ${library.graphMatchedNodes} 个节点` : ''}` : '未报告'}</dd></div>
        <div><dt>RAG 参数</dt><dd>{config ? `topK ${config.topK} · RRF ${config.rrfK} · 候选 ×${config.candidateMultiplier}` : '未报告'}</dd></div>
        <div><dt>Reranker</dt><dd>{retrieval.reranker.configured ? `${retrieval.reranker.provider} · ${library?.rerankCandidates ?? 0} 个候选` : retrieval.reranker.provider === 'none' ? '未启用' : `未配置（${retrieval.reranker.provider}）`}</dd></div>
      </dl>
    </section>
  );
}

function KnowledgeHitDetail({ baseId, hit, onOpen, rank, transport }: { baseId: string; hit: KnowledgeSearchHit; onOpen: (hit: KnowledgeSearchHit) => void; rank: number; transport: ReturnType<typeof useKnowledgeLibraryQueries>['transport'] }) {
  const openMutation = useMutation({ mutationFn: () => openKnowledgeHit(transport, baseId, hit), onSuccess: () => onOpen(hit) });
  const graphPaths = hit.diagnostics.graphPaths;
  const visibleGraphPaths = graphPaths.slice(0, 2);
  return (
    <article className="knowledge-search__detail">
      <span>{publicKnowledgeText(hit.documentName)}</span>
      <h3>{publicKnowledgeText(hit.title)}</h3>
      <p>{publicKnowledgeText(hit.excerpt) || '这个段落没有可显示的摘录。'}</p>
      <dl>
        <div><dt>位置</dt><dd>{citationLabel(hit)}</dd></div>
        <div><dt>最终排名</dt><dd>第 {hit.diagnostics.rerankRank ?? rank} 条{hit.diagnostics.rerankRank !== null ? '（Reranker 后）' : ''}</dd></div>
        <div><dt>相关程度</dt><dd>{relevanceLabel(hit.score, false)}</dd></div>
        <div><dt>标题路径</dt><dd>{publicKnowledgeText(hit.heading) || '未提供'}</dd></div>
      </dl>
      <Disclosure className="knowledge-search__advanced" summary="高级：检索详情">
        <dl>
          <div><dt>相关度分数</dt><dd>{scorePoints(hit.score)} / 100</dd></div>
          <div><dt>命中方式</dt><dd>{retrievalEvidenceLabel(hit)}</dd></div>
          {hit.diagnostics.retrievalRank !== null ? <div><dt>初始召回排名</dt><dd>第 {hit.diagnostics.retrievalRank} 条 · {scorePoints(hit.diagnostics.retrievalScore)} / 100</dd></div> : null}
          {hit.diagnostics.rerankRank !== null ? <div><dt>Reranker 排名</dt><dd>第 {hit.diagnostics.rerankRank} 条 · {scorePoints(hit.diagnostics.rerankScore)} / 100 · {hit.diagnostics.rerankProvider || '已启用'}</dd></div> : null}
          {graphPaths.length ? (
            <div>
              <dt>关联路径</dt>
              <dd>
                <span>{publicKnowledgeText(visibleGraphPaths.join('；'))}</span>
                {graphPaths.length > visibleGraphPaths.length ? (
                  <Disclosure
                    className="knowledge-search__path-disclosure"
                    summary={`显示 ${visibleGraphPaths.length} / 共 ${graphPaths.length} 条 · 查看全部`}
                  >
                    <ol>
                      {graphPaths.map((path, index) => (
                        <li key={`${index}:${path}`}>{publicKnowledgeText(path)}</li>
                      ))}
                    </ol>
                  </Disclosure>
                ) : null}
              </dd>
            </div>
          ) : null}
        </dl>
      </Disclosure>
      <Button leadingIcon={<ExternalLink size={14} />} loading={openMutation.isPending} onClick={() => openMutation.mutate()} size="small" variant="primary">打开来源</Button>
      {openMutation.error ? <p className="knowledge-inline-error">当前无法打开来源。</p> : null}
    </article>
  );
}

function hitRankLabel(hit: KnowledgeSearchHit, index: number): string {
  if (hit.diagnostics.rerankRank !== null) {
    return `最终排名 #${hit.diagnostics.rerankRank}${hit.diagnostics.retrievalRank === null ? '' : ` · 初始 #${hit.diagnostics.retrievalRank}`}`;
  }
  return `排名 #${index + 1}`;
}

function relevanceLabel(score: number | null, detailed = true): string {
  if (score === null) return detailed ? '相关度未知' : '未知';
  if (score >= 0.75) return detailed ? '高相关' : '高';
  if (score >= 0.45) return detailed ? '相关' : '中';
  return '较低';
}

function relevanceLevel(score: number | null): 'high' | 'mid' | 'low' | 'unknown' {
  if (score === null) return 'unknown';
  if (score >= 0.75) return 'high';
  if (score >= 0.45) return 'mid';
  return 'low';
}

interface SettingsDraftChange {
  label: string;
  current: string;
  proposed: string;
}

function SettingsDraftDiff({ changes, effect }: { changes: readonly SettingsDraftChange[]; effect?: string }) {
  if (!changes.length) return null;
  return (
    <div className="knowledge-settings__draft" role="status">
      <header>
        <strong>未保存的更改 · {changes.length} 项</strong>
        <span className="knowledge-settings__draft-direction">当前 → 保存后</span>
      </header>
      <ul>
        {changes.map((change) => (
          <li key={change.label}>
            <span>{change.label}</span>
            <span className="knowledge-settings__draft-values">
              <s>{change.current}</s>
              <ArrowRight aria-hidden size={12} />
              <b>{change.proposed}</b>
            </span>
          </li>
        ))}
      </ul>
      {effect ? <p>{effect}</p> : null}
    </div>
  );
}

// Filter on the raw values, then fall back to a readable placeholder for
// display: an empty description must never look identical to the literal
// text a user typed, or the draft panel and the save button would disagree.
function settingsDraftChanges(entries: readonly [string, string, string][]): SettingsDraftChange[] {
  return entries
    .filter(([, current, proposed]) => current !== proposed)
    .map(([label, current, proposed]) => ({ label, current: current || '（未填写）', proposed: proposed || '（未填写）' }));
}

function yesNoLabel(value: boolean): string {
  return value ? '开启' : '关闭';
}

function KnowledgeSettingsPanel({
  base,
  chunkPreview,
  chunkPreviewError,
  chunkPreviewing,
  documents,
  embeddingState,
  embeddingStateError,
  indexRuntime,
  onAgentEnabled,
  onParser,
  onSaveInfo,
  onSaveChunking,
  onSaveRetrieval,
  onPreviewChunking,
  onRebuild,
  parserData,
  pending,
  rebuildError,
  rebuilding,
  refreshParser,
  settingsEnvelope,
  updateError,
  worker,
}: {
  base: DocumentKnowledgeBase;
  chunkPreview: KnowledgeChunkPreview | null;
  chunkPreviewError: unknown;
  chunkPreviewing: boolean;
  documents: readonly KnowledgeDocument[];
  embeddingState: KnowledgeEmbeddingProfileState | undefined;
  embeddingStateError: unknown;
  indexRuntime: KnowledgeIndexRuntimeStatus;
  onAgentEnabled: (enabled: boolean) => void;
  onParser: (parser: KnowledgeParserMode) => void;
  onSaveInfo: (name: string, description: string) => void;
  onSaveChunking: (config: KnowledgeChunkingConfig) => void;
  onSaveRetrieval: (config: KnowledgeRetrievalConfig) => void;
  onPreviewChunking: (documentId: string, config: KnowledgeChunkingConfig) => void;
  onRebuild: () => void;
  parserData: unknown;
  pending: boolean;
  rebuildError: unknown;
  rebuilding: boolean;
  refreshParser: () => void;
  settingsEnvelope: unknown;
  updateError: unknown;
  worker: WorkerState;
}) {
  const mineru = mineruState(parserData);
  const [chunking, setChunking] = useState(base.chunkingConfig);
  const [retrieval, setRetrieval] = useState(base.retrievalConfig);
  const [name, setName] = useState(base.name);
  const [description, setDescription] = useState(base.description);
  const [previewDocumentId, setPreviewDocumentId] = useState(documents[0]?.id ?? '');
  const chunkingError = chunking.size < 200 || chunking.size > 8_000
    ? '切分大小必须在 200–8000 之间。'
    : chunking.overlap < 0 || chunking.overlap > 2_000 || chunking.overlap >= chunking.size
      ? '重叠必须大于等于 0，且小于切分大小。'
      : chunking.strategy === 'separator' && !chunking.separator
        ? '自定义分隔符不能为空。'
      : '';
  const retrievalError = retrieval.topK < 1 || retrieval.topK > 100
    ? '返回数量必须在 1–100 之间。'
    : retrieval.threshold < 0 || retrieval.threshold > 1
      ? '最低相关度必须在 0–1 之间。'
      : retrieval.lexicalWeight < 0 || retrieval.lexicalWeight > 10 || retrieval.denseWeight < 0 || retrieval.denseWeight > 10
        ? '检索权重必须在 0–10 之间。'
        : retrieval.graphWeight < 0 || retrieval.graphWeight > 10
          ? '关系权重必须在 0–10 之间。'
        : retrieval.lexicalWeight + retrieval.denseWeight <= 0
          ? '关键词权重和向量权重不能同时为 0。'
            : retrieval.rrfK < 1 || retrieval.rrfK > 1_000
              ? '融合系数必须在 1–1000 之间。'
              : retrieval.candidateMultiplier < 1 || retrieval.candidateMultiplier > 20
                ? '候选范围必须在 1–20 之间。'
                : retrieval.rerankCandidateDepth < 1 || retrieval.rerankCandidateDepth > 100 || (retrieval.rerankEnabled && retrieval.rerankCandidateDepth < retrieval.topK)
                  ? 'Reranker 候选数必须在 1–100 之间，且不能小于返回数量。'
                  : '';
  useEffect(() => {
    if (!documents.some((document) => document.id === previewDocumentId)) {
      setPreviewDocumentId(documents[0]?.id ?? '');
    }
  }, [documents, previewDocumentId]);
  const visibleChunkPreview = chunkPreview?.documentId === previewDocumentId ? chunkPreview : null;
  const infoChanges = settingsDraftChanges([
    ['名称', base.name, name.trim()],
    ['说明', base.description, description.trim()],
  ]);
  const chunkingChanges = settingsDraftChanges([
    ['策略', chunkingStrategyLabel(base.chunkingConfig.strategy), chunkingStrategyLabel(chunking.strategy)],
    ['大小', String(base.chunkingConfig.size), String(chunking.size)],
    ['重叠', String(base.chunkingConfig.overlap), String(chunking.overlap)],
    ...(chunking.strategy === 'separator' || base.chunkingConfig.strategy === 'separator'
      ? [['分隔符', base.chunkingConfig.separator || '未设置', chunking.separator || '未设置'] as [string, string, string]]
      : []),
    ['保留标题边界', yesNoLabel(base.chunkingConfig.respectHeadings), yesNoLabel(chunking.respectHeadings)],
    ['保留页面边界', yesNoLabel(base.chunkingConfig.respectPageBoundaries), yesNoLabel(chunking.respectPageBoundaries)],
  ]);
  const retrievalChanges = settingsDraftChanges([
    ['模式', retrievalModeLabel(base.retrievalConfig.mode), retrievalModeLabel(retrieval.mode)],
    ['返回数量', String(base.retrievalConfig.topK), String(retrieval.topK)],
    ['最低相关度', base.retrievalConfig.threshold.toFixed(2), retrieval.threshold.toFixed(2)],
    ['关键词权重', String(base.retrievalConfig.lexicalWeight), String(retrieval.lexicalWeight)],
    ['向量权重', String(base.retrievalConfig.denseWeight), String(retrieval.denseWeight)],
    ['图谱增强', yesNoLabel(base.retrievalConfig.graphEnabled), yesNoLabel(retrieval.graphEnabled)],
    ['关系权重', String(base.retrievalConfig.graphWeight), String(retrieval.graphWeight)],
    ['融合系数', String(base.retrievalConfig.rrfK), String(retrieval.rrfK)],
    ['候选范围', String(base.retrievalConfig.candidateMultiplier), String(retrieval.candidateMultiplier)],
    ['Reranker', yesNoLabel(base.retrievalConfig.rerankEnabled), yesNoLabel(retrieval.rerankEnabled)],
    ['Reranker 候选数', String(base.retrievalConfig.rerankCandidateDepth), String(retrieval.rerankCandidateDepth)],
  ]);
  return (
    <div className="knowledge-panel knowledge-settings">
      {updateError ? <InlineNotice title="设置没有保存" tone="warning">{publicErrorText(updateError, '知识库仍使用保存前的配置。')}</InlineNotice> : null}
      <section>
        <div className="knowledge-settings__heading"><BookOpen size={16} /><div><strong>基本信息</strong><span>知识库识别信息</span></div></div>
        <div className="knowledge-settings-fields knowledge-settings-fields--info">
          <Field htmlFor="knowledge-base-settings-name" label="名称"><Input id="knowledge-base-settings-name" maxLength={120} onChange={(event) => setName(event.target.value)} value={name} /></Field>
          <Field htmlFor="knowledge-base-settings-description" label="说明"><Input id="knowledge-base-settings-description" maxLength={1_000} onChange={(event) => setDescription(event.target.value)} value={description} /></Field>
        </div>
        <SettingsDraftDiff changes={infoChanges} effect="保存后立即生效，不影响已导入的材料与索引。" />
        <div className="knowledge-settings__actions">
          {name !== base.name || description !== base.description ? <Button aria-label="放弃基本信息更改" disabled={pending} onClick={() => { setName(base.name); setDescription(base.description); }} size="small" variant="quiet">放弃更改</Button> : null}
          <Button disabled={pending || !name.trim() || !infoChanges.length} loading={pending} onClick={() => onSaveInfo(name.trim(), description.trim())} size="small" variant="primary">保存基本信息</Button>
        </div>
      </section>
      <div className="knowledge-settings-grid">
        <section>
          <div className="knowledge-settings__heading"><ServerCog size={16} /><div><strong>伙伴</strong><span>检索权限</span></div></div>
          <Switch checked={base.agentEnabled} disabled={pending} label="允许伙伴检索此知识库" onCheckedChange={onAgentEnabled} />
        </section>
        <section>
          <div className="knowledge-settings__heading"><Settings2 size={16} /><div><strong>解析器</strong><span>新导入与重试</span></div></div>
          <Field htmlFor="knowledge-parser" label="解析方式">
            <Select disabled={pending} id="knowledge-parser" onValueChange={(value) => onParser(asParserMode(value))} options={[{ value: 'auto', label: '自动选择' }, { value: 'builtin', label: '内置解析' }, { value: 'mineru', label: 'MinerU' }]} value={base.parser} />
          </Field>
          <div className="knowledge-parser-health"><div><span>解析服务</span><StatusBadge label={worker.label} tone={worker.tone} /></div><div><span>MinerU</span><StatusBadge label={mineru.label} tone={mineru.tone} /></div><IconButton icon={<RefreshCw size={14} />} label="检查解析服务" onClick={refreshParser} size="small" tooltip /></div>
          {base.parser === 'mineru' && !mineru.ready ? <InlineNotice title="MinerU 未连接" tone="warning">本机服务不可用。</InlineNotice> : null}
        </section>
      </div>
      <section>
        <div className="knowledge-settings__heading"><Settings2 size={16} /><div><strong>切分</strong><span>修改后材料进入待重建状态</span></div></div>
        <div className="knowledge-settings-fields knowledge-settings-fields--chunking">
          <Field htmlFor="knowledge-chunk-strategy" label="策略"><Select id="knowledge-chunk-strategy" onValueChange={(value) => setChunking({ ...chunking, strategy: asChunkingStrategy(value) })} options={[...chunkingStrategyOptions]} value={chunking.strategy} /></Field>
          <Field htmlFor="knowledge-chunk-size" label="大小"><Input id="knowledge-chunk-size" max={8_000} min={200} onChange={(event) => setChunking({ ...chunking, size: Number(event.target.value) })} step={100} type="number" value={chunking.size} /></Field>
          <Field htmlFor="knowledge-chunk-overlap" label="重叠"><Input id="knowledge-chunk-overlap" max={2_000} min={0} onChange={(event) => setChunking({ ...chunking, overlap: Number(event.target.value) })} step={20} type="number" value={chunking.overlap} /></Field>
          {chunking.strategy === 'separator' ? <Field htmlFor="knowledge-chunk-separator" label="分隔符"><Input id="knowledge-chunk-separator" maxLength={100} onChange={(event) => setChunking({ ...chunking, separator: event.target.value })} value={chunking.separator} /></Field> : null}
          <Switch checked={chunking.respectHeadings} label="保留标题边界" onCheckedChange={(respectHeadings) => setChunking({ ...chunking, respectHeadings })} />
          <Switch checked={chunking.respectPageBoundaries} label="保留页面边界" onCheckedChange={(respectPageBoundaries) => setChunking({ ...chunking, respectPageBoundaries })} />
        </div>
        {chunkingError ? <p className="knowledge-inline-error" role="alert">{chunkingError}</p> : null}
        <div className="knowledge-chunk-preview-controls">
          <Field htmlFor="knowledge-preview-document" label="预览材料"><Select id="knowledge-preview-document" onValueChange={setPreviewDocumentId} options={documents.map((document) => ({ value: document.id, label: document.name }))} value={previewDocumentId} /></Field>
          <Button disabled={!previewDocumentId || Boolean(chunkingError)} loading={chunkPreviewing} onClick={() => onPreviewChunking(previewDocumentId, chunking)} size="small">预览切分</Button>
        </div>
        {chunkPreviewError ? <InlineNotice title="切分预览失败" tone="warning">{publicErrorText(chunkPreviewError, '请确认材料已经完成解析。')}</InlineNotice> : null}
        {visibleChunkPreview ? <div className="knowledge-chunk-preview"><header><strong>{visibleChunkPreview.total} 个段落</strong><span>显示前 {visibleChunkPreview.chunks.length} 个</span></header><div>{visibleChunkPreview.chunks.map((chunk) => <article key={chunk.id}><b>#{chunk.ordinal + 1}{chunk.page ? ` · 第 ${chunk.page} 页` : ''}</b><p>{publicKnowledgeText(chunk.content)}</p></article>)}</div></div> : null}
        <SettingsDraftDiff changes={chunkingChanges} effect="保存后，新导入的材料按新切分处理；已有材料进入待重建，重建完成前检索仍使用现有段落。" />
        <div className="knowledge-settings__actions">
          {chunkingChanges.length ? <Button aria-label="放弃切分更改" disabled={pending} onClick={() => setChunking(base.chunkingConfig)} size="small" variant="quiet">放弃更改</Button> : null}
          <Button disabled={pending || Boolean(chunkingError) || !chunkingChanges.length} loading={pending} onClick={() => onSaveChunking(chunking)} size="small" variant="primary">保存切分设置</Button>
        </div>
      </section>
      <section>
        <div className="knowledge-settings__heading"><Search size={16} /><div><strong>检索</strong><span>页面测试与伙伴检索</span></div></div>
        <div className="knowledge-settings-fields">
          <Field htmlFor="knowledge-retrieval-mode" label="模式"><Select id="knowledge-retrieval-mode" onValueChange={(value) => setRetrieval({ ...retrieval, mode: asRetrievalMode(value) })} options={[{ value: 'hybrid', label: '混合' }, { value: 'dense', label: '向量' }, { value: 'lexical', label: '关键词' }]} value={retrieval.mode} /></Field>
          <Field htmlFor="knowledge-retrieval-topk" label="返回数量"><Input id="knowledge-retrieval-topk" max={100} min={1} onChange={(event) => setRetrieval({ ...retrieval, topK: Number(event.target.value) })} type="number" value={retrieval.topK} /></Field>
          <Field htmlFor="knowledge-retrieval-threshold" label="最低相关度"><Input id="knowledge-retrieval-threshold" max={1} min={0} onChange={(event) => setRetrieval({ ...retrieval, threshold: Number(event.target.value) })} step={0.05} type="number" value={retrieval.threshold} /></Field>
        </div>
        <Disclosure
          className="knowledge-settings__advanced-details"
          contentClassName="knowledge-settings__advanced-content"
          summary="高级：检索调优"
        >
          <div className="knowledge-settings-fields knowledge-settings-fields--advanced">
            <Field htmlFor="knowledge-lexical-weight" label="关键词权重"><Input id="knowledge-lexical-weight" max={10} min={0} onChange={(event) => setRetrieval({ ...retrieval, lexicalWeight: Number(event.target.value) })} step={0.1} type="number" value={retrieval.lexicalWeight} /></Field>
            <Field htmlFor="knowledge-dense-weight" label="向量权重"><Input id="knowledge-dense-weight" max={10} min={0} onChange={(event) => setRetrieval({ ...retrieval, denseWeight: Number(event.target.value) })} step={0.1} type="number" value={retrieval.denseWeight} /></Field>
            <Switch checked={retrieval.graphEnabled} disabled={retrieval.mode !== 'hybrid'} label="启用图谱增强" onCheckedChange={(graphEnabled) => setRetrieval({ ...retrieval, graphEnabled })} />
            <Field htmlFor="knowledge-graph-weight" label="关系权重"><Input disabled={retrieval.mode !== 'hybrid' || !retrieval.graphEnabled} id="knowledge-graph-weight" max={10} min={0} onChange={(event) => setRetrieval({ ...retrieval, graphWeight: Number(event.target.value) })} step={0.05} type="number" value={retrieval.graphWeight} /></Field>
            <Field htmlFor="knowledge-rrf-k" label="融合系数"><Input id="knowledge-rrf-k" max={1_000} min={1} onChange={(event) => setRetrieval({ ...retrieval, rrfK: Number(event.target.value) })} type="number" value={retrieval.rrfK} /></Field>
            <Field htmlFor="knowledge-candidate-multiplier" label="候选范围"><Input id="knowledge-candidate-multiplier" max={20} min={1} onChange={(event) => setRetrieval({ ...retrieval, candidateMultiplier: Number(event.target.value) })} type="number" value={retrieval.candidateMultiplier} /></Field>
            <Switch checked={retrieval.rerankEnabled} label="启用 Reranker" onCheckedChange={(rerankEnabled) => setRetrieval({ ...retrieval, rerankEnabled })} />
            <Field htmlFor="knowledge-rerank-depth" label="Reranker 候选数"><Input disabled={!retrieval.rerankEnabled} id="knowledge-rerank-depth" max={100} min={1} onChange={(event) => setRetrieval({ ...retrieval, rerankCandidateDepth: Number(event.target.value) })} type="number" value={retrieval.rerankCandidateDepth} /></Field>
          </div>
        </Disclosure>
        {retrievalError ? <p className="knowledge-inline-error" role="alert">{retrievalError}</p> : null}
        <SettingsDraftDiff changes={retrievalChanges} effect="保存后从下一次检索开始生效，不需要重建索引。" />
        <div className="knowledge-settings__actions">
          {retrievalChanges.length ? <Button aria-label="放弃检索更改" disabled={pending} onClick={() => setRetrieval(base.retrievalConfig)} size="small" variant="quiet">放弃更改</Button> : null}
          <Button disabled={pending || Boolean(retrievalError) || !retrievalChanges.length} loading={pending} onClick={() => onSaveRetrieval(retrieval)} size="small" variant="primary">保存检索设置</Button>
        </div>
      </section>
      <KnowledgeEmbeddingSettings
        baseRevision={String(base.revision)}
        documents={documents}
        error={embeddingStateError}
        fallbackRuntime={indexRuntime}
        settingsEnvelope={settingsEnvelope}
        state={embeddingState}
      />
      <section>
        <div className="knowledge-settings__heading"><RefreshCw size={16} /><div><strong>索引重建</strong><span>{base.documentCount} 个材料 · {base.chunkCount} 个现有段落</span></div></div>
        <div className="knowledge-settings__actions"><Button loading={rebuilding} onClick={onRebuild} size="small" variant="primary">重建索引</Button></div>
        {rebuildError ? <InlineNotice title="索引操作失败" tone="warning">{publicErrorText(rebuildError, '当前索引保持不变。')}</InlineNotice> : null}
      </section>
    </div>
  );
}

type EmbeddingMutationContext = {
  candidate: KnowledgeEmbeddingCandidate;
  changes: KnowledgeEmbeddingImpact['configurationChanges'];
  impact: KnowledgeEmbeddingImpact;
};

function KnowledgeEmbeddingSettings({
  baseRevision,
  documents,
  error,
  fallbackRuntime,
  settingsEnvelope,
  state,
}: {
  baseRevision: string;
  documents: readonly KnowledgeDocument[];
  error: unknown;
  fallbackRuntime: KnowledgeIndexRuntimeStatus;
  settingsEnvelope: unknown;
  state: KnowledgeEmbeddingProfileState | undefined;
}) {
  const transport = useControlTransport();
  const queryClient = useQueryClient();
  const mutationBoundary = useConfigurationMutationBoundary();
  const [candidate, setCandidate] = useState<KnowledgeEmbeddingCandidate>(emptyEmbeddingCandidate());
  const [draftDirty, setDraftDirty] = useState(false);
  const [verifiedDraftKey, setVerifiedDraftKey] = useState('');
  const profileRevision = state?.profile.profileSha256 ?? '';
  const draftKey = JSON.stringify(candidate);
  const settingsRoot = asRecord(settingsEnvelope);
  const runtimeConfig = asRecord(settingsRoot.runtimeConfig);
  const rawRuntimeRevision = settingsRoot.runtimeRevision ?? runtimeConfig.runtimeRevision;
  const runtimeRevision = Number.isInteger(rawRuntimeRevision) && numberValue(rawRuntimeRevision) >= 0
    ? numberValue(rawRuntimeRevision)
    : null;
  const validationError = embeddingCandidateError(candidate);

  useEffect(() => {
    if (!state || draftDirty) return;
    setCandidate(candidateFromEmbeddingState(state));
  }, [draftDirty, profileRevision, state]);

  const probeMutation = useMutation({
    mutationKey: ['knowledge-library', 'embedding', 'probe'],
    mutationFn: (profile: KnowledgeEmbeddingCandidate) => probeKnowledgeEmbedding(transport, profile),
    onSuccess: (_receipt, profile) => setVerifiedDraftKey(JSON.stringify(profile)),
  });
  const verified = Boolean(probeMutation.data && verifiedDraftKey === draftKey);
  const updateCandidate = (patch: Partial<KnowledgeEmbeddingCandidate>) => {
    setCandidate((current) => ({ ...current, ...patch }));
    setDraftDirty(true);
    setVerifiedDraftKey('');
    probeMutation.reset();
  };
  const selectProvider = (provider: KnowledgeEmbeddingProvider) => {
    if (provider === 'environment' || provider === 'none') {
      updateCandidate({ provider, model: '', baseUrl: '', dimensions: 0, secretReference: '', queryPrefix: '', documentPrefix: '', denseBackend: 'sqlite-exact' });
      return;
    }
    if (provider === 'local-hash') {
      updateCandidate({ provider, model: 'deterministic-term-vector-v1', baseUrl: '', dimensions: candidate.dimensions >= 8 ? candidate.dimensions : 96, secretReference: '', queryPrefix: '', documentPrefix: '' });
      return;
    }
    updateCandidate({ provider, model: candidate.model === 'deterministic-term-vector-v1' ? '' : candidate.model, baseUrl: provider === 'openai-compatible' ? candidate.baseUrl : '', secretReference: provider === 'openai-compatible' ? candidate.secretReference : '' });
  };
  const refreshAuthoritativeState = async () => {
    await queryClient.invalidateQueries({ queryKey: knowledgeLibraryKeys.root });
    setDraftDirty(false);
    setVerifiedDraftKey('');
  };
  const blockedReason = runtimeRevision === null
    ? '当前设置尚未就绪，请刷新后重试。'
    : validationError
      ? validationError
      : !verified
        ? '请先测试连接；只有连接通过的这份配置才能查看影响。'
        : '';
  const phase = state?.phase ?? 'applied_pending_restart';
  const runtime = state?.runtime;
  const runtimeProvider = String(runtime?.provider.provider ?? fallbackRuntime.provider);
  const runtimeModel = String(runtime?.provider.model ?? fallbackRuntime.model);
  const fingerprint = runtime?.fingerprint || fallbackRuntime.fingerprint;
  const dimensions = runtime?.dimensions ?? fallbackRuntime.dimensions;
  const vectorCount = runtime?.vectorCount ?? fallbackRuntime.vectorCount;
  const phaseLabel = phase === 'active' ? '已生效' : phase === 'applied_pending_rebuild' ? '待重建' : '等待索引服务重启';
  const phaseTone = phase === 'active' ? 'success' : 'warning';
  const connectionLabel = verified
    ? '当前设置已通过连接测试'
    : probeMutation.isPending
      ? '正在测试连接'
      : probeMutation.error
        ? '连接测试未通过'
        : '尚未测试当前设置';
  const indexCoverageLabel = typeof vectorCount === 'number'
    ? `${vectorCount} 个段落已建立向量`
    : '等待索引状态';

  return (
    <section aria-label="向量模型与索引">
      <div className="knowledge-settings__heading"><Database size={16} /><div><strong>向量模型与索引</strong><span>配置向量模型，测试连接后查看影响</span></div></div>
      {error ? <InlineNotice title="无法读取当前向量模型配置" tone="warning">{publicErrorText(error, '当前草稿不会自动应用。')}</InlineNotice> : null}
      <div className="knowledge-index-status">
        <StatusBadge label={phaseLabel} tone={phaseTone} />
        <span>{phase === 'active' ? '当前设置已经可以用于知识检索。' : phase === 'applied_pending_rebuild' ? '设置已保存，完成重建后即可用于检索。' : '设置正在应用，稍后刷新查看结果。'}</span>
      </div>
      <dl className="mgmt-kv">
        <dt>连接状态</dt><dd>{connectionLabel}</dd>
        <dt>索引状态</dt><dd>{indexCoverageLabel}</dd>
      </dl>
      <InlineNotice title="下一步" tone={phase === 'active' ? 'success' : 'info'}>{phase === 'active' ? '可以直接使用知识检索。需要更换向量模型时，再打开高级设置。' : '打开高级设置检查连接并更新配置；完成后按提示重建索引。'}</InlineNotice>
      <Disclosure
        className="knowledge-embedding-advanced"
        contentClassName="knowledge-embedding-advanced__content"
        summary="高级：连接与索引设置"
      >
        <div className="knowledge-settings-fields knowledge-settings-fields--index">
        <Field htmlFor="knowledge-embedding-provider" label="向量模型服务">
          <Select
            id="knowledge-embedding-provider"
            onValueChange={(value) => selectProvider(value as KnowledgeEmbeddingProvider)}
            options={embeddingProviderOptions}
            value={candidate.provider}
          />
        </Field>
        <Field htmlFor="knowledge-embedding-model" label="向量模型">
          <Input disabled={['environment', 'none', 'local-hash'].includes(candidate.provider)} id="knowledge-embedding-model" maxLength={1_000} onChange={(event) => updateCandidate({ model: event.target.value })} value={candidate.model} />
        </Field>
        <Field htmlFor="knowledge-embedding-base-url" label="兼容 API 地址">
          <Input disabled={candidate.provider !== 'openai-compatible'} id="knowledge-embedding-base-url" maxLength={2_000} onChange={(event) => updateCandidate({ baseUrl: event.target.value })} placeholder="https://host.example/v1" value={candidate.baseUrl} />
        </Field>
        <Field htmlFor="knowledge-embedding-dimensions" label="向量维度">
          <Input disabled={['environment', 'none'].includes(candidate.provider)} id="knowledge-embedding-dimensions" max={65_536} min={0} onChange={(event) => updateCandidate({ dimensions: Number(event.target.value) })} type="number" value={candidate.dimensions} />
        </Field>
        <Field htmlFor="knowledge-embedding-secret-reference" label="密钥环境变量名">
          <Input disabled={candidate.provider !== 'openai-compatible'} id="knowledge-embedding-secret-reference" maxLength={128} onChange={(event) => updateCandidate({ secretReference: event.target.value })} placeholder="PAW_EMBEDDING_API_KEY" value={candidate.secretReference} />
        </Field>
        </div>
        <Disclosure
          className="knowledge-embedding-advanced"
          contentClassName="knowledge-embedding-advanced__content"
          summary="高级：索引参数"
        >
        <div className="knowledge-settings-fields knowledge-settings-fields--advanced">
          <Field htmlFor="knowledge-embedding-backend" label="向量索引方式">
            <Select disabled={['environment', 'none'].includes(candidate.provider)} id="knowledge-embedding-backend" onValueChange={(value) => updateCandidate({ denseBackend: value === 'usearch' ? 'usearch' : 'sqlite-exact' })} options={[{ value: 'sqlite-exact', label: '精确匹配（SQLite）' }, { value: 'usearch', label: '近似加速（USearch）' }]} value={candidate.denseBackend} />
          </Field>
          <Field htmlFor="knowledge-embedding-query-prefix" label="查询前缀">
            <Input disabled={['environment', 'none', 'local-hash'].includes(candidate.provider)} id="knowledge-embedding-query-prefix" maxLength={500} onChange={(event) => updateCandidate({ queryPrefix: event.target.value })} value={candidate.queryPrefix} />
          </Field>
          <Field htmlFor="knowledge-embedding-document-prefix" label="文档前缀">
            <Input disabled={['environment', 'none', 'local-hash'].includes(candidate.provider)} id="knowledge-embedding-document-prefix" maxLength={500} onChange={(event) => updateCandidate({ documentPrefix: event.target.value })} value={candidate.documentPrefix} />
          </Field>
        </div>
        </Disclosure>
      {validationError ? <p className="knowledge-inline-error" role="alert">{validationError}</p> : null}
      <div className="knowledge-settings__actions">
        <Button disabled={Boolean(validationError) || !state} loading={probeMutation.isPending} onClick={() => probeMutation.mutate(candidate)} size="small">测试连接</Button>
      </div>
      {probeMutation.error ? <InlineNotice title="连接测试失败" tone="warning">{publicErrorText(probeMutation.error, '不会保存或重启当前服务。')}</InlineNotice> : null}
      {verified && probeMutation.data ? (
        <InlineNotice title="连接测试通过" tone="success">
          当前设置可以连接；保存前会列出受影响的知识库。密钥不会显示或发送到页面。
        </InlineNotice>
      ) : null}
      <ManagementMutationWorkflow<EmbeddingMutationContext>
        availability={mutationBoundary.availability(blockedReason)}
        description="先确认连接仍可用并列出受影响的知识库；保存后会更新向量模型配置、重启索引服务并逐库重建。旧索引会保留，以便必要时撤回。"
        draftKey={JSON.stringify({ draftKey, profileRevision, runtimeRevision })}
        mutationKey={['knowledge-library', 'embedding', 'apply']}
        onApply={async (preview) => parseManagementWorkReceipt(
          await mutationBoundary.request({
            pathId: configurationMutationPathIds.apply,
            body: {
              changes: preview.context.changes,
              expectedRuntimeRevision: preview.expectedRuntimeRevision,
              previewToken: preview.previewToken,
              payloadSha256: preview.payloadSha256,
              confirmText: preview.requiredConfirm,
            },
          }),
          configurationMutationPathIds.apply,
          preview.payloadSha256,
        )}
        onApplied={() => void refreshAuthoritativeState()}
        onPreview={async () => {
          if (!verified || runtimeRevision === null || !state) throw new Error('候选配置已变化，请重新测试连接。');
          const impact = await previewKnowledgeEmbeddingImpact(transport, candidate);
          if (impact.currentProfileSha256 !== state.profile.profileSha256 || impact.probe.profileSha256 !== probeMutation.data?.profileSha256) {
            throw new Error('当前配置或连接测试结果已经变化，请刷新后重试。');
          }
          const context: EmbeddingMutationContext = { candidate: { ...candidate }, changes: impact.configurationChanges, impact };
          const preview = parseManagementWorkPreview(
            await mutationBoundary.request({
              pathId: configurationMutationPathIds.preview,
              body: { changes: impact.configurationChanges, expectedRuntimeRevision: runtimeRevision },
            }),
            configurationMutationPathIds.apply,
            context,
          );
          return {
            ...preview,
            summary: {
              ...preview.summary,
              title: '更新向量模型配置？',
              items: [
                `候选：${impact.probe.provider} / ${impact.probe.model || '无向量模型'} / ${impact.probe.dimensions} 维`,
                `影响 ${impact.affectedBaseCount} 个知识库、${impact.affectedDocumentCount} 个文档、${impact.affectedChunkCount} 个段落`,
                impact.requiresWorkerRestart ? '保存后需要重启索引服务' : '当前服务配置无需重启',
                impact.requiresRebuild ? '新配置生效前需要逐库重建向量索引' : '当前没有需要重建的文档',
              ],
              risk: 'R2',
            },
          };
        }}
        onRollback={async (receipt, preview) => parseManagementWorkReceipt(
          await mutationBoundary.request({
            pathId: configurationMutationPathIds.rollback,
            body: { receiptId: receipt.receiptId, rollbackToken: receipt.rollbackToken, payloadSha256: receipt.payloadSha256, confirmText: 'rollback' },
          }),
          configurationMutationPathIds.rollback,
          preview.payloadSha256,
        )}
        onRolledBack={() => void refreshAuthoritativeState()}
        risk="R2"
        title="更新向量模型配置"
      />
      <div className="knowledge-settings-fields knowledge-settings-fields--index">
        <Field htmlFor="knowledge-dense-provider" label="当前服务"><Input disabled id="knowledge-dense-provider" readOnly value={runtimeProvider || '未报告'} /></Field>
        <Field htmlFor="knowledge-dense-model" label="当前模型标识"><Input disabled id="knowledge-dense-model" readOnly value={fingerprint || runtimeModel || '未报告'} /></Field>
        <Field htmlFor="knowledge-dense-dimension" label="实际维度"><Input disabled id="knowledge-dense-dimension" readOnly value={dimensions ?? '未报告'} /></Field>
        <Field htmlFor="knowledge-vector-count" label="当前向量数量"><Input disabled id="knowledge-vector-count" readOnly value={vectorCount ?? '未报告'} /></Field>
        <Field htmlFor="knowledge-index-revision" label="索引版本"><Input disabled id="knowledge-index-revision" readOnly value={indexRevisionLabel(documents)} /></Field>
        <Field htmlFor="knowledge-config-revision" label="配置版本"><Input disabled id="knowledge-config-revision" readOnly value={baseRevision} /></Field>
      </div>
      <InlineNotice title="何时生效" tone="info">当前索引服务与保存的向量模型配置一致，并且向量覆盖全部段落后，才会显示“已生效”；保存配置不代表索引已经重建完成。</InlineNotice>
      </Disclosure>
    </section>
  );
}

const embeddingProviderOptions = [
  { value: 'environment', label: '沿用环境配置' },
  { value: 'none', label: '关闭向量检索（仅关键词）' },
  { value: 'local-hash', label: 'Local Hash（基线）' },
  { value: 'sentence-transformers', label: 'Sentence Transformers' },
  { value: 'mlx-bert', label: 'MLX BERT' },
  { value: 'openai-compatible', label: 'OpenAI 兼容向量模型' },
];

function emptyEmbeddingCandidate(): KnowledgeEmbeddingCandidate {
  return { provider: 'environment', model: '', baseUrl: '', dimensions: 0, secretReference: '', queryPrefix: '', documentPrefix: '', denseBackend: 'sqlite-exact' };
}

function candidateFromEmbeddingState(state: KnowledgeEmbeddingProfileState): KnowledgeEmbeddingCandidate {
  if (state.profile.source === 'environment') return emptyEmbeddingCandidate();
  return {
    provider: state.profile.provider,
    model: state.profile.model,
    baseUrl: state.profile.baseUrl,
    dimensions: state.profile.dimensions,
    secretReference: state.profile.secretReference,
    queryPrefix: state.profile.queryPrefix,
    documentPrefix: state.profile.documentPrefix,
    denseBackend: state.profile.denseBackend,
  };
}

function embeddingCandidateError(candidate: KnowledgeEmbeddingCandidate): string {
  if (!Number.isInteger(candidate.dimensions) || candidate.dimensions < 0 || candidate.dimensions > 65_536) return '向量维度必须是 0–65536 的整数。';
  if (candidate.provider === 'local-hash' && candidate.dimensions < 8) return 'Local Hash 至少需要 8 维。';
  if (['sentence-transformers', 'mlx-bert', 'openai-compatible'].includes(candidate.provider) && !candidate.model.trim()) return '当前服务需要填写模型名称或本机模型目录。';
  if (candidate.provider === 'openai-compatible') {
    try {
      const parsed = new URL(candidate.baseUrl);
      if (!['http:', 'https:'].includes(parsed.protocol)) return '兼容 API 地址必须使用 HTTP(S)。';
    } catch {
      return 'OpenAI 兼容服务需要填写有效的 HTTP(S) API 地址。';
    }
    if (candidate.secretReference && !/^[A-Z][A-Z0-9_]{2,127}$/u.test(candidate.secretReference)) return '密钥引用必须是大写环境变量名，页面不会保存明文密钥。';
  }
  return '';
}

function CreateKnowledgeBaseDialog({
  error,
  loading,
  onCreate,
  onOpenChange,
  open,
  returnFocusRef,
}: {
  error: unknown;
  loading: boolean;
  onCreate: (input: { name: string; description: string }) => void;
  onOpenChange: (open: boolean) => void;
  open: boolean;
  returnFocusRef: RefObject<HTMLElement | null>;
}) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  useEffect(() => {
    if (!open) { setName(''); setDescription(''); }
  }, [open]);
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!loading) onOpenChange(next); }}>
      <DialogContent onCloseAutoFocus={(event) => restoreDialogFocus(event, returnFocusRef)}>
        <DialogHeader><DialogTitle>新建文档知识库</DialogTitle><DialogDescription>资料和个人记忆会使用不同存储，不会混在一起。</DialogDescription></DialogHeader>
        <form className="knowledge-create-form" id="knowledge-create-form" onSubmit={(event) => { event.preventDefault(); if (name.trim()) onCreate({ name: name.trim(), description: description.trim() }); }}>
          <Field htmlFor="knowledge-base-name" label="名称" required><Input autoFocus id="knowledge-base-name" maxLength={120} onChange={(event) => setName(event.target.value)} placeholder="例如：项目技术文档" value={name} /></Field>
          <Field htmlFor="knowledge-base-description" label="说明"><TextArea id="knowledge-base-description" maxLength={1_000} onChange={(event) => setDescription(event.target.value)} placeholder="这个库包含什么、给谁使用" rows={4} value={description} /></Field>
          {error ? <p className="knowledge-inline-error" role="alert">{publicErrorText(error, '暂时无法新建知识库。')}</p> : null}
        </form>
        <DialogFooter><Button disabled={loading} onClick={() => onOpenChange(false)} variant="quiet">取消</Button><Button disabled={!name.trim()} form="knowledge-create-form" loading={loading} type="submit" variant="primary">创建</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ReparseDocumentDialog({ document, error, loading, onConfirm, onOpenChange, returnFocusRef }: { document: KnowledgeDocument | null; error: unknown; loading: boolean; onConfirm: (parser: KnowledgeParserMode) => void; onOpenChange: (open: boolean) => void; returnFocusRef: RefObject<HTMLElement | null> }) {
  const [parser, setParser] = useState<KnowledgeParserMode>('auto');
  useEffect(() => { if (document) setParser(asParserMode(document.parser)); }, [document]);
  return (
    <Dialog open={Boolean(document)} onOpenChange={(next) => { if (!loading) onOpenChange(next); }}>
      <DialogContent onCloseAutoFocus={(event) => restoreDialogFocus(event, returnFocusRef)}>
        <DialogHeader><DialogTitle>重新解析文档</DialogTitle><DialogDescription>{document ? `“${document.name}”将重新生成文档内容、段落和索引。` : ''}</DialogDescription></DialogHeader>
        <div className="knowledge-create-form">
          <Field htmlFor="knowledge-document-parser" label="解析方式">
            <Select id="knowledge-document-parser" onValueChange={(value) => setParser(asParserMode(value))} options={[{ value: 'auto', label: '自动选择' }, { value: 'builtin', label: '内置文本解析' }, { value: 'mineru', label: 'MinerU OCR / 版面解析' }]} value={parser} />
          </Field>
          {parser === 'mineru' ? <InlineNotice title="MinerU OCR" tone="info">适用于扫描 PDF、图片和需要保留版面的文档；任务会走本机 MinerU 服务。</InlineNotice> : null}
          {error ? <p className="knowledge-inline-error" role="alert">{publicErrorText(error, '重新解析失败，现有索引仍然保留。')}</p> : null}
        </div>
        <DialogFooter><Button disabled={loading} onClick={() => onOpenChange(false)} variant="quiet">取消</Button><Button leadingIcon={<RefreshCw size={14} />} loading={loading} onClick={() => onConfirm(parser)} variant="primary">开始重新解析</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ConfirmDialog({ description, error, loading, onConfirm, onOpenChange, open, returnFocusRef, title }: { description: string; error: unknown; loading: boolean; onConfirm: () => void; onOpenChange: (open: boolean) => void; open: boolean; returnFocusRef: RefObject<HTMLElement | null>; title: string }) {
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!loading) onOpenChange(next); }}>
      <DialogContent onCloseAutoFocus={(event) => restoreDialogFocus(event, returnFocusRef)}>
        <DialogHeader><DialogTitle>{title}</DialogTitle><DialogDescription>{description}</DialogDescription></DialogHeader>
        {error ? <p className="knowledge-inline-error" role="alert">{publicErrorText(error, '删除失败，原内容仍然保留。')}</p> : null}
        <DialogFooter><Button disabled={loading} onClick={() => onOpenChange(false)} variant="quiet">取消</Button><Button leadingIcon={<Trash2 size={14} />} loading={loading} onClick={onConfirm} variant="danger">确认删除</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function restoreDialogFocus(
  event: Event,
  returnFocusRef: RefObject<HTMLElement | null>,
): void {
  event.preventDefault();
  returnFocusRef.current?.focus();
}

interface WorkerState { label: string; tone: 'success' | 'warning' | 'danger' | 'info' | 'neutral' }

function workerState(value: unknown, error: unknown): WorkerState {
  if (error) return { label: '不可用', tone: 'danger' };
  const payload = object(value);
  const status = String(payload.status ?? payload.state ?? '').toLowerCase();
  if (payload.ok === true || ['ready', 'healthy', 'idle', 'running'].includes(status)) return { label: status === 'idle' ? '待命' : '可用', tone: 'success' };
  if (!value) return { label: '检查中', tone: 'info' };
  return { label: '未就绪', tone: 'warning' };
}

function mineruState(value: unknown): WorkerState & { ready: boolean } {
  const payload = object(value);
  const list = Array.isArray(payload.items) ? payload.items : Array.isArray(payload.parsers) ? payload.parsers : [];
  const mineru = list.map(object).find((item) => String(item.id ?? item.name ?? item.provider).toLowerCase().includes('mineru')) ?? object(payload.mineru);
  const status = String(mineru.status ?? mineru.state ?? '').toLowerCase();
  const ready = mineru.ready === true || mineru.available === true || ['ready', 'healthy', 'running'].includes(status);
  const enabled = mineru.enabled === true;
  if (ready) return { ready, label: '已连接', tone: 'success' };
  if (enabled) return { ready, label: '连接失败', tone: 'danger' };
  return { ready, label: '未启用', tone: 'neutral' };
}

function object(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function asDetailTab(value: string): DetailTab { return ['viewer', 'search', 'graph', 'jobs', 'settings'].includes(value) ? value as DetailTab : 'materials'; }
function asParserMode(value: string): KnowledgeParserMode { return value === 'builtin' ? value : value === 'mineru' || value === 'mineru_local_http' ? 'mineru' : 'auto'; }
function asChunkingStrategy(value: string): KnowledgeChunkingConfig['strategy'] { return ['general', 'markdown', 'book', 'qa', 'laws', 'separator', 'fixed'].includes(value) ? value as KnowledgeChunkingConfig['strategy'] : 'markdown'; }
const chunkingStrategyOptions: readonly { value: KnowledgeChunkingConfig['strategy']; label: string }[] = [
  { value: 'general', label: '通用段落' },
  { value: 'markdown', label: 'Markdown 标题' },
  { value: 'book', label: '书籍章节' },
  { value: 'qa', label: '问答' },
  { value: 'laws', label: '法律条款' },
  { value: 'separator', label: '自定义分隔符' },
  { value: 'fixed', label: '固定长度' },
];
function chunkingStrategyLabel(value: KnowledgeChunkingConfig['strategy']): string {
  return chunkingStrategyOptions.find((option) => option.value === value)?.label ?? value;
}
function asRetrievalMode(value: string): KnowledgeRetrievalConfig['mode'] { return value === 'dense' || value === 'lexical' ? value : 'hybrid'; }
function retrievalModeLabel(value: KnowledgeRetrievalConfig['mode']): string { return value === 'dense' ? '向量检索' : value === 'lexical' ? '关键词检索' : '混合检索'; }
function retrievalModeLabelValue(value: string): string {
  return value === 'dense' ? '向量检索' : value === 'lexical' ? '关键词检索' : value === 'hybrid' ? '混合检索' : '服务未报告';
}
function parserLabel(value: KnowledgeParserMode): string { return value === 'builtin' ? '内置' : value === 'mineru' ? 'MinerU' : '自动'; }
function isAbortError(value: unknown): boolean {
  return (typeof DOMException !== 'undefined' && value instanceof DOMException && value.name === 'AbortError')
    || (value instanceof Error && value.name === 'AbortError');
}
function scorePoints(value: number | null): string { return value === null ? '未提供' : String(Math.round(value <= 1 ? value * 100 : value)); }
function retrievalEvidenceLabel(hit: KnowledgeSearchHit): string {
  const mode = hit.diagnostics.effectiveMode === 'hybrid' ? '混合检索' : hit.diagnostics.effectiveMode === 'lexical' ? '关键词检索' : hit.diagnostics.effectiveMode === 'dense' ? '向量检索' : '检索服务未报告';
  const ranks = [
    hit.diagnostics.lexicalRank === null ? '' : `关键词候选第 ${hit.diagnostics.lexicalRank}`,
    hit.diagnostics.denseRank === null ? '' : `向量候选第 ${hit.diagnostics.denseRank}`,
    hit.diagnostics.graphRank === null ? '' : `图谱候选第 ${hit.diagnostics.graphRank}`,
  ].filter(Boolean);
  const matches = hit.diagnostics.graphMatches.length ? ` · 关联 ${publicKnowledgeText(hit.diagnostics.graphMatches.slice(0, 3).join('、'))}` : '';
  return ranks.length ? `${mode} · ${ranks.join(' · ')}${matches}` : mode;
}
function citationLabel(hit: KnowledgeSearchHit): string { if (hit.page !== null) return `第 ${hit.page} 页`; if (hit.lineStart !== null) return hit.lineEnd && hit.lineEnd !== hit.lineStart ? `第 ${hit.lineStart}-${hit.lineEnd} 行` : `第 ${hit.lineStart} 行`; return '文档段落'; }
function uploadItemId(file: File, index: number): string { return `upload-${Date.now()}-${index}-${file.name}-${file.size}`; }
function replaceUploadItem(items: KnowledgeUploadItem[], id: string, patch: Partial<KnowledgeUploadItem>): KnowledgeUploadItem[] { return items.map((item) => item.id === id ? { ...item, ...patch } : item); }
function indexRevisionLabel(documents: readonly KnowledgeDocument[]): string {
  const revisions = [...new Set(documents.map((document) => document.indexedConfigRevision).filter((value) => value > 0))].sort((left, right) => left - right);
  if (!revisions.length) return '未建立';
  return revisions.length === 1 ? String(revisions[0]) : `${revisions[0]}–${revisions.at(-1)}`;
}
