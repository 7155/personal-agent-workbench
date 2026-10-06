import {
  Archive,
  ArrowLeft,
  BookOpen,
  ChevronRight,
  EyeOff,
  Fingerprint,
  GitBranch,
  RefreshCw,
  Search,
  Tags,
  X,
} from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import { lazy, Suspense, useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
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
  Select,
  Switch,
  Tabs as ViewTabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  TextArea,
} from '@/components/primitives';
import { useControlTransport } from '@/app/control-transport';
import { EvidenceEchoUsage } from '@/features/evidence-echo/EvidenceEchoUsage';
import { useProductIdentity } from '@/features/identity/product-identity';
import {
  openPawOsRoute,
  usePawOsAppIdentity,
  usePawOsDesktop,
} from '@/features/paw-os/surface-context';
import {
  memoryBookArchivePathIds,
  memoryQueryKeys,
  useMemoryBookArchiveBoundary,
  useMemoryQueries,
  type MemoryKind,
} from './api';
import { MemoryTopicPage } from './MemoryTopicPage';
import { MemoryCurationWorkbench } from './MemoryCurationWorkbench';
import { ActivityTimeline } from './ActivityTimeline';
import { MemoryLibraryNavigation, memoryLibraryCounts } from './MemoryLibraryNavigation';
import { MemoryPreferences } from './MemoryPreferences';
import { MemoryProfile } from './MemoryProfile';
import { RoleBookLayer } from './RoleBookLayer';
import {
  MemoryReferenceDialog,
  type MemoryReferenceSelection,
} from './MemoryReferenceDialog';
import {
  publicMemoryOwnerLabel,
  publicMemorySourceLabel,
  publicMemoryText,
} from './public-copy';
import {
  InlineNotice,
  ManagementPage,
  ManagementSection,
  OperationalList,
  PaginationBar,
  QueryState,
  StatusBadge,
  arrayRecords,
  asRecord,
  numberValue,
  stringValue,
} from '@/features/overview/management-ui';
import {
  ManagementMutationWorkflow,
  parseManagementWorkPreview,
  parseManagementWorkReceipt,
} from '@/features/overview/management-mutation';
import type { JsonValue } from '@/platform/transport';
import './memory.css';

const MemoryRelations = lazy(() => import('./MemoryRelations').then((module) => ({ default: module.MemoryRelations })));

type MemoryLayer = 'evidence' | 'atoms' | 'books';
type MemoryRouteLayer = MemoryLayer | 'timelines' | 'role-books';
type MemoryView = 'profile' | 'catalog' | 'roleBooks' | 'timeline' | 'relations' | 'organize' | 'preferences';

function defaultMemoryStatus(kind: MemoryKind): string {
  if (kind === 'phrases') return 'approved';
  if (kind === 'evidence') return '';
  if (kind === 'atoms') return 'current';
  return 'active';
}

export function MemoryFeature() {
  const queryClient = useQueryClient();
  const identity = useProductIdentity();
  const appSurface = usePawOsAppIdentity();
  const desktop = usePawOsDesktop();
  const location = useLocation();
  const navigate = useNavigate();
  const routeSelection = useMemo(() => memoryRouteSelection(location.search), [location.search]);
  const [view, setView] = useState<MemoryView>(
    routeSelection.view,
  );
  const [profileVisited, setProfileVisited] = useState(view === 'profile');
  useEffect(() => { if (view === 'profile') setProfileVisited(true); }, [view]);
  const [preferencesVisited, setPreferencesVisited] = useState(view === 'preferences');
  useEffect(() => { if (view === 'preferences') setPreferencesVisited(true); }, [view]);
  const [layer, setLayer] = useState<MemoryLayer>(routeSelection.layer);
  const kind: MemoryKind = layer;
  const [draftQuery, setDraftQuery] = useState('');
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState(defaultMemoryStatus(kind));
  const [ownerKey, setOwnerKey] = useState('');
  const [selectedId, setSelectedId] = useState(routeSelection.id);
  const [catalogDetailCollapsed, setCatalogDetailCollapsed] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const detailRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const routeLayerRef = useRef(routeSelection.layer);
  const [reference, setReference] = useState<MemoryReferenceSelection | null>(
    routeSelection.reference,
  );
  const selectedOwner = parseOwnerKey(ownerKey);
  const { pages, summary } = useMemoryQueries(
    kind,
    query,
    status,
    selectedOwner.ownerKind,
    selectedOwner.ownerId,
    view === 'catalog',
  );
  const archiveBoundary = useMemoryBookArchiveBoundary();
  const summaryPayload = asRecord(summary.data);
  const ownerOptions = useMemo(() => [
    { value: '', label: '全部归属' },
    ...arrayRecords(summaryPayload.owners).map((owner) => {
      const ownerKind = stringValue(owner.ownerKind);
      const ownerId = stringValue(owner.ownerId);
      const ownerName = memoryOwnerName(owner);
      return {
        value: encodeOwnerKey(ownerKind, ownerId),
        label: `${ownerLabel(ownerKind, ownerId, ownerName)} · ${numberValue(owner.itemCount)} 项`,
      };
    }),
  ], [summaryPayload.owners]);
  const rows = useMemo(() =>
    (pages.data?.pages ?? []).flatMap((page) => arrayRecords(asRecord(page).items)).map(normalizeMemoryRow),
  [pages.data]);
  const selected = rows.find((row) => stringValue(row.id) === selectedId)
    ?? (kind === 'books' && selectedId ? { id: selectedId } as Record<string, unknown> : undefined);
  const selectedStatus = stringValue(selected?.status);
  const selectedType = stringValue(selected?.type);
  const archiveDraft = {
    bookId: selectedId,
    archived: selectedStatus !== 'archived',
    reason: selectedStatus === 'archived' ? 'control_center_restore' : 'control_center_archive',
  };
  const archiveAvailability = archiveBoundary.availability(archiveBlockedReason());
  const runtimeRevision = numberValue(summaryPayload.runtimeRevision);
  // 空目录有两种事实：筛选没命中，或这一层还没有沉淀过内容。前者提示调整
  // 筛选；后者不是错误，要讲清内容沿哪条链路沉淀进来。
  const catalogFiltered = Boolean(query) || Boolean(ownerKey) || status !== defaultMemoryStatus(kind);
  // Summary counts and catalog content have independent read boundaries.
  // A slow search must leave its input mounted; a failed summary must not
  // hide records or a topic that was read through its own entity request.
  const error = pages.error as Error | null;
  const pending = view === 'catalog' && pages.isPending;
  const summaryState = summary.error ? 'error' : summary.isPending ? 'pending' : 'ready';
  const refresh = () => queryClient.refetchQueries({
    queryKey: memoryQueryKeys.root,
    type: 'active',
  });

  useEffect(() => {
    const next = memoryRouteSelection(location.search);
    setView(next.view);
    if (next.view === 'catalog') {
      setLayer(next.layer);
      if (next.layer !== routeLayerRef.current) setStatus(defaultMemoryStatus(next.layer));
      routeLayerRef.current = next.layer;
      setSelectedId(next.id);
      setCatalogDetailCollapsed(false);
      setEditOpen(false);
    }
    setReference(next.reference);
  }, [location.search]);

  // The search reacts while typing; Enter commits immediately. A selected
  // record stays selected while it remains in the filtered result.
  useEffect(() => {
    const next = draftQuery.trim();
    if (next === query) return;
    const handle = window.setTimeout(() => setQuery(next), 300);
    return () => window.clearTimeout(handle);
  }, [draftQuery, query]);

  // A newly selected record always starts reading at the top; the previous
  // record's scroll position must not leak into the next one.
  useEffect(() => {
    if (detailRef.current) detailRef.current.scrollTop = 0;
  }, [selectedId]);

  return (
    <ManagementPage
      actions={<>
        <span className="memory-second-brain__mode" data-view={view}>
          <span><strong>{memoryViewLabel(view)}</strong><small>{memoryViewStatus(view, rows.length, summaryPayload, summaryState)}</small></span>
        </span>
        <Button leadingIcon={<RefreshCw size={15} />} loading={summary.isRefetching || pages.isRefetching} onClick={refresh} size="small">刷新</Button>
      </>}
      description="查看已整理的记忆、它们的来源和主题；需要时可以回到原始记录核对。"
      routeId="memory"
      title="我的记忆"
    >
      <div className="memory-second-brain" data-layer={layer} data-topic-open={kind === 'books' && selected && !catalogDetailCollapsed ? true : undefined} data-view={view}>
        {view === 'catalog' ? <MemoryLibraryNavigation
          activeLayer={layer}
          showOrganizeAction={!appSurface}
          onOpenLayer={openCatalogLayer}
          onOpenOrganize={() => openView('organize')}
          onRetry={refresh}
          summary={summaryPayload}
          summaryState={summaryState}
        /> : null}
        <ViewTabs
          className="memory-view-tabs"
          onValueChange={(next) => openView(normalizeMemoryView(next))}
          value={view}
        >
          {!appSurface ? (
            <TabsList aria-label="记忆视图">
              <TabsTrigger value="profile">关于我</TabsTrigger>
              <TabsTrigger value="catalog">记忆</TabsTrigger>
              <TabsTrigger value="roleBooks">伙伴记忆</TabsTrigger>
              <TabsTrigger value="timeline">时间线</TabsTrigger>
              <TabsTrigger value="relations">关系图</TabsTrigger>
              <TabsTrigger value="organize">让{identity.assistantName}整理</TabsTrigger>
              <TabsTrigger value="preferences">记忆偏好</TabsTrigger>
            </TabsList>
          ) : null}
          {(view === 'profile' || profileVisited) ? <TabsContent value="profile" forceMount hidden={view !== 'profile'}><MemoryProfile onOpenReference={setReference} onSaved={refresh} /></TabsContent> : null}
          <TabsContent value="catalog">
            <section className="mgmt-section memory-catalog-section">
              <div className="memory-layer-workspace" data-detail-open={selected && !catalogDetailCollapsed ? true : undefined}>
                <aside className="memory-layer-list" aria-label={`${kindLabel(kind)}目录`}>
                  <div className="memory-layer-list__controls">
                    <header className="memory-layer-list__heading">
                      <h2>{kindLabel(kind)} 目录</h2>
                      {kind !== 'books' ? <p>{memoryLayerDescription(kind)}</p> : null}
                    </header>
                    <div className="mgmt-filter-row memory-catalog-filters">
                      <Field className="memory-catalog-filters__query" htmlFor="memory-search" label="搜索">
                        <span className="memory-catalog-filters__query-box">
                          <Search aria-hidden="true" size={14} />
                          <Input id="memory-search" onChange={(event) => setDraftQuery(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) setQuery(draftQuery.trim()); }} placeholder="标题、正文或标签，输入即筛选" ref={searchRef} value={draftQuery} />
                          {draftQuery ? <IconButton className="memory-catalog-filters__clear" icon={<X size={14} />} label="清除搜索" onClick={() => { setDraftQuery(''); setQuery(''); searchRef.current?.focus(); }} size="small" /> : null}
                        </span>
                      </Field>
                      <Disclosure className="memory-catalog-filters__secondary" contentClassName="memory-catalog-filters__options" summary={<span>筛选 · {memoryStatusOptions(kind).find((option) => option.value === status)?.label ?? status}{ownerAwareKind(kind) ? ` · ${ownerOptions.find((option) => option.value === ownerKey)?.label ?? '全部归属'}` : ''}</span>}>
                      <Field className="memory-catalog-filters__status" htmlFor="memory-status-filter" label="状态">
                        <Select
                          id="memory-status-filter"
                          onValueChange={(value) => {
                            setStatus(value);
                            setSelectedId('');
                            setEditOpen(false);
                          }}
                          options={memoryStatusOptions(kind)}
                          value={status}
                        />
                      </Field>
                      {ownerAwareKind(kind) ? (
                        <Field className="memory-catalog-filters__owner" htmlFor="memory-owner-filter" label="归属">
                          <Select
                            id="memory-owner-filter"
                            onValueChange={(value) => {
                              setOwnerKey(value);
                              setSelectedId('');
                              setEditOpen(false);
                            }}
                            options={ownerOptions}
                            value={ownerKey}
                          />
                        </Field>
                      ) : null}
                      </Disclosure>
                    </div>
                  </div>
                  <QueryState error={error} isPending={pending} onRetry={() => void pages.refetch()}>
                  {rows.length ? (
                    <>
                      <OperationalList items={rows.map((row) => {
                        const id = stringValue(row.id);
                        const rowStatus = stringValue(row.status, 'unknown');
                        return {
                          id,
                          title: publicMemoryText(stringValue(row.title, '未命名记忆')),
                          detail: publicMemoryText(stringValue(row.detail, '暂无摘要')),
                          meta: catalogRowMeta(kind, row, identity.assistantName),
                          status: <StatusBadge label={catalogStatusLabel(kind, rowStatus)} tone={catalogStatusTone(kind, rowStatus)} />,
                          onClick: () => {
                            if (kind === 'books') { openBook(id); return; }
                            setSelectedId(id);
                            setEditOpen(false);
                          },
                          selected: id === selectedId,
                        };
                      })} />
                      <PaginationBar count={rows.length} hasMore={pages.hasNextPage} isFetching={pages.isFetchingNextPage} onLoadMore={() => void pages.fetchNextPage()} />
                    </>
                  ) : catalogFiltered ? (
                    <EmptyState
                      action={<Button onClick={clearCatalogFilters} size="small" variant="quiet">清除筛选</Button>}
                      description={`当前筛选没有 ${kindLabel(kind)} 记录；切换状态可查看保留的历史版本。`}
                      icon={Search}
                      title="没有匹配结果"
                    />
                  ) : (
                    <MemoryColdLead
                      kind={kind}
                      onStartWork={desktop ? () => openPawOsRoute(desktop, '/agent') : undefined}
                    />
                  )}
                  </QueryState>
                </aside>
                <div className="memory-layer-detail" ref={detailRef}>
                  {selected ? (
                    <>
                      <button aria-label="返回记忆目录" className="memory-layer-detail__back" onClick={() => {
                        if (kind === 'books') { setCatalogDetailCollapsed(true); setEditOpen(false); return; }
                        setSelectedId('');
                        setEditOpen(false);
                      }} type="button"><ArrowLeft aria-hidden size={15} />返回目录</button>
                      {kind === 'books' ? <MemoryTopicPage
                        bookId={selectedId}
                        fallbackSummary={selected.sensitive === true ? undefined : stringValue(selected.summary, stringValue(selected.detail))}
                        fallbackReferences={selected.sensitive === true ? [] : catalogReferences(selected, 'books')}
                        key={selectedId}
                        onOpenBook={openBook}
                        onOpenReference={setReference}
                        ownerLabel={ownerLabel(stringValue(selected.ownerKind), stringValue(selected.ownerId), memoryOwnerName(selected))}
                        redacted={selected.sensitive === true}
                        title={stringValue(selected.title)}
                      /> : <MemoryCatalogDetail
                        assistantName={identity.assistantName}
                        key={selectedId}
                        kind={kind}
                        onOpenReference={(next) => setReference(next)}
                        row={selected}
                      />}
                      <div className="memory-layer-actions">
                        {kind === 'evidence' ? (
                          selected.canForget === true ? (
                            <MemoryEvidenceDispositionAction
                              disabledReason={sourceDispositionBlockedReason()}
                              onChanged={refresh}
                              row={selected}
                            />
                          ) : null
                        ) : (
                          <DirectMemoryEditAction
                            disabledReason={memoryEditBlockedReason()}
                            onClick={() => setEditOpen(true)}
                          />
                        )}
                        {kind === 'books' ? (
                          archiveAvailability.state === 'unsupported' ? (
                            <UnavailableMemoryAction
                              description="归档或恢复长期主题，并保留事实与来源关系。"
                              reason={archiveAvailability.reason || '安全归档暂未开放。'}
                              risk="R2"
                              title="管理长期主题"
                            />
                          ) : (
                            <ManagementMutationWorkflow
                              availability={archiveAvailability}
                              description={archiveDraft.archived
                                ? '归档长期主题，以后不再自动参考；事实和来源关系仍会保留。'
                                : '恢复长期主题，恢复为可自动参考。'}
                              draftKey={JSON.stringify(archiveDraft)}
                              mutationKey={['memory', 'mutation', 'archive']}
                              onApply={async (preview) => parseManagementWorkReceipt(
                                await archiveBoundary.request({
                                  pathId: memoryBookArchivePathIds.apply,
                                  body: {
                                    ...preview.context,
                                    expectedRuntimeRevision: preview.expectedRuntimeRevision,
                                    previewToken: preview.previewToken,
                                    payloadSha256: preview.payloadSha256,
                                    confirmText: preview.requiredConfirm,
                                  },
                                }),
                                memoryBookArchivePathIds.apply,
                                preview.payloadSha256,
                              )}
                              onApplied={() => refresh()}
                              onPreview={async () => parseManagementWorkPreview(
                                await archiveBoundary.request({
                                  pathId: memoryBookArchivePathIds.preview,
                                  body: { ...archiveDraft, expectedRuntimeRevision: runtimeRevision },
                                }),
                                memoryBookArchivePathIds.apply,
                                archiveDraft,
                              )}
                              onRollback={async (receipt, preview) => parseManagementWorkReceipt(
                                await archiveBoundary.request({
                                  pathId: memoryBookArchivePathIds.rollback,
                                  body: {
                                    receiptId: receipt.receiptId,
                                    rollbackToken: receipt.rollbackToken,
                                    payloadSha256: receipt.payloadSha256,
                                    confirmText: 'rollback',
                                  },
                                }),
                                memoryBookArchivePathIds.rollback,
                                preview.payloadSha256,
                              )}
                              onRolledBack={() => refresh()}
                              risk="R2"
                              title={archiveDraft.archived ? '归档长期主题' : '恢复长期主题'}
                            />
                          )
                        ) : null}
                      </div>
                    </>
                  ) : (
                    <EmptyState description={kind === 'books' ? '选择一个主题，先读当前认识、最近变化和待确认问题，再按来源核对。' : '从左侧选择一项，查看正文、状态、证据来源，以及它最近被哪些 Session 装配。'} icon={BookOpen} title={kind === 'books' ? '选择一个主题' : '选择一条记录'} />
                  )}
                </div>
              </div>
            </section>
          </TabsContent>
          <TabsContent value="roleBooks">
            <RoleBookLayer
              enabled={view === 'roleBooks'}
              initialReferenceId={routeSelection.routeLayer === 'role-books' ? routeSelection.id : ''}
              onOpenGovernance={() => openView('organize')}
            />
          </TabsContent>
          <TabsContent value="relations">
            <Suspense fallback={<p role="status">正在读取记忆关系…</p>}>
              <MemoryRelations enabled={view === 'relations'} onOpenBook={openBook} />
            </Suspense>
          </TabsContent>
          <TabsContent value="timeline">
            {view === 'timeline' ? <ActivityTimeline
              initialDate={routeSelection.date}
              library={summaryState === 'ready' ? memoryLibraryCounts(summaryPayload) : undefined}
              latestTimelineDate={stringValue(asRecord(summaryPayload.latestActivityTimeline).date)}
              onBrowseMemories={() => openCatalogLayer('atoms')}
              onOpenOrganize={() => openView('organize')}
            /> : null}
          </TabsContent>
          <TabsContent value="organize">
            <MemoryCurationWorkbench
              enabled={view === 'organize'}
              onOpenTimeline={openTimeline}
            />
          </TabsContent>
          {(view === 'preferences' || preferencesVisited) ? <TabsContent value="preferences" forceMount hidden={view !== 'preferences'}>
            <MemoryPreferences />
          </TabsContent> : null}
        </ViewTabs>
        <MemoryEditDialog
          kind={kind}
          onOpenChange={setEditOpen}
          onSaved={refresh}
          open={editOpen && Boolean(selected)}
          row={selected}
        />
        {reference ? (
          <MemoryReferenceDialog
            {...reference}
            onOpenChange={(open) => {
              if (open) return;
              setReference(null);
              if (routeSelection.id && routeSelection.routeLayer !== 'books') {
                navigate({ pathname: location.pathname, search: `?layer=${routeSelection.routeLayer}` }, { replace: true });
              }
            }}
          />
        ) : null}
      </div>
    </ManagementPage>
  );

  function clearCatalogFilters() {
    setDraftQuery('');
    setQuery('');
    setStatus(defaultMemoryStatus(kind));
    setOwnerKey('');
    searchRef.current?.focus();
  }

  function openCatalogLayer(next: MemoryLayer) {
    setView('catalog');
    setLayer(next);
    setStatus(defaultMemoryStatus(next));
    setOwnerKey('');
    setSelectedId('');
    setCatalogDetailCollapsed(false);
    setEditOpen(false);
    setReference(null);
    navigate({ pathname: location.pathname, search: `?layer=${next}` }, { replace: true });
  }

  function openBook(bookId: string) {
    setView('catalog');
    setLayer('books');
    if (layer !== 'books') setStatus(defaultMemoryStatus('books'));
    setSelectedId(bookId);
    setCatalogDetailCollapsed(false);
    setEditOpen(false);
    setReference(null);
    navigate({ pathname: location.pathname, search: `?layer=books&id=${encodeURIComponent(bookId)}` }, { replace: true });
  }

  function openView(next: MemoryView) {
    setView(next);
    setEditOpen(false);
    setReference(null);
    if (next === 'catalog') {
      navigate({ pathname: location.pathname, search: `?layer=${layer}` }, { replace: true });
    } else if (next === 'timeline') {
      navigate({ pathname: location.pathname, search: '?layer=timelines' }, { replace: true });
    } else if (next === 'roleBooks') {
      navigate({ pathname: location.pathname, search: '?layer=role-books' }, { replace: true });
    } else {
      navigate({ pathname: location.pathname, search: `?view=${next}` }, { replace: true });
    }
  }

  function openTimeline(date: string) {
    setView('timeline');
    setEditOpen(false);
    setReference(null);
    navigate({
      pathname: location.pathname,
      search: `?layer=timelines&date=${encodeURIComponent(date)}`,
    }, { replace: true });
  }

  function archiveBlockedReason(): string {
    if (!selected) return '先从上方目录中选择一项。';
    if (kind !== 'books') return '当前只有长期主题支持可回滚归档。';
    if (selectedType !== 'topic') return '只有长期主题可以手动归档。';
    if (!['active', 'approved', 'archived'].includes(selectedStatus)) {
      return '当前记忆状态不支持归档或恢复。';
    }
    return '';
  }

  function memoryEditBlockedReason(): string {
    if (!selected) return '先从上方目录中选择一项。';
    if (archiveBoundary.capabilities.isPending) return '正在确认本机记忆编辑能力。';
    if (archiveBoundary.capabilities.error) return '无法确认本机记忆编辑能力，请刷新后重试。';
    if (!archiveBoundary.capabilities.data?.routeIds.includes('memory.edit')) {
      return '当前服务尚未开放本机记忆编辑。';
    }
    return '';
  }

  function sourceDispositionBlockedReason(): string {
    if (!selected) return '先从上方目录中选择一条来源记录。';
    if (archiveBoundary.capabilities.isPending) return '正在确认本机记忆管理能力。';
    if (archiveBoundary.capabilities.error) return '无法确认本机记忆管理能力，请刷新后重试。';
    if (!archiveBoundary.capabilities.data?.routeIds.includes('memory.source.disposition')) {
      return '当前服务尚未开放从记忆中移除来源。';
    }
    if (selectedStatus === 'consolidated') {
      return '这条来源已经整理成记忆，请改为编辑或移除对应的记忆。';
    }
    if (!['pending', 'remember', 'needs_review'].includes(selectedStatus)) {
      return '这条来源当前不能直接移出记忆。';
    }
    return '';
  }
}

interface MemoryRouteSelection {
  date: string;
  id: string;
  layer: MemoryLayer;
  reference: MemoryReferenceSelection | null;
  routeLayer: MemoryRouteLayer;
  view: MemoryView;
}

function memoryRouteSelection(search: string): MemoryRouteSelection {
  const params = new URLSearchParams(search);
  const rawLayer = params.get('layer') ?? 'books';
  const routeLayer: MemoryRouteLayer = isMemoryRouteLayer(rawLayer) ? rawLayer : 'books';
  const id = (params.get('id') ?? '').trim().slice(0, 500);
  const requestedDate = (params.get('date') ?? '').trim();
  const requestedView = normalizeMemoryView(params.get('view') ?? '');
  const view: MemoryView = requestedView !== 'catalog'
    ? requestedView
    : routeLayer === 'timelines'
      ? 'timeline'
      : routeLayer === 'role-books'
        ? 'roleBooks'
        : 'catalog';
  const layer: MemoryLayer = routeLayer === 'evidence' || routeLayer === 'books'
    ? routeLayer
    : 'atoms';
  return {
    date: /^\d{4}-\d{2}-\d{2}$/u.test(requestedDate) ? requestedDate : '',
    id,
    layer,
    reference: id && routeLayer !== 'books' ? {
      kind: referenceKindForRoute(routeLayer, id),
      referenceId: id,
    } : null,
    routeLayer,
    view,
  };
}

function isMemoryRouteLayer(value: string): value is MemoryRouteLayer {
  return ['evidence', 'atoms', 'books', 'timelines', 'role-books'].includes(value);
}

function referenceKindForRoute(layer: MemoryRouteLayer, id: string): MemoryReferenceSelection['kind'] {
  if (layer === 'atoms') return 'atom';
  if (layer === 'books') return 'book';
  if (layer === 'timelines') return 'timeline';
  if (layer === 'role-books') return 'role_book_revision';
  return /^\d+$/u.test(id) || id.startsWith('event:') || id.startsWith('input-memory:')
    ? 'event'
    : 'evidence';
}

function normalizeMemoryView(value: string): MemoryView {
  return value === 'profile' || value === 'roleBooks' || value === 'timeline' || value === 'relations' || value === 'organize' || value === 'preferences'
    ? value
    : 'catalog';
}

function memoryViewLabel(view: MemoryView): string {
  return ({
    profile: '关于我',
    catalog: '记忆库',
    roleBooks: '伙伴记忆',
    timeline: '时间线',
    relations: '关系图',
    organize: '记忆整理',
    preferences: '记忆偏好',
  } as const)[view];
}

function memoryViewStatus(
  view: MemoryView,
  visibleRows: number,
  summary: Record<string, unknown>,
  summaryState: 'ready' | 'pending' | 'error',
): string {
  // Preferences never claims a persistence state here; the panel itself
  // reports read-only, pending, and synced from the real write contract.
  if (view === 'profile') return '长期背景 · 可以核对与修改';
  if (view === 'preferences') return '影响整理与联想';
  // The workbench owns live backlog and job receipts. A separately refreshed
  // catalog summary must not contradict those counts in the page heading.
  if (view === 'organize') return '核对来源、继续整理和查看本批结果';
  if (summaryState === 'pending') return '正在读取记忆状态';
  if (summaryState === 'error') return '记忆状态暂不可用';
  if (view === 'catalog') return `${visibleRows} 条当前结果`;
  if (view === 'roleBooks') return `${numberValue(summary.roleBookCount, numberValue(summary.roleBookRevisionCount))} 个伙伴记忆`;
  if (view === 'timeline') return `${numberValue(summary.activityTimelineCount, numberValue(summary.timelineCount))} 条活动记录`;
  if (view === 'relations') return `${numberValue(summary.memoryTagCount)} 个关系标签`;
  return '';
}

function normalizeMemoryRow(item: Record<string, unknown>): Record<string, unknown> {
  const source = asRecord(item.source);
  return {
    id: item.id ?? item.bookId ?? item.atomId ?? item.tagId ?? item.groupId ?? item.phraseId,
    title: item.title ?? item.name ?? item.label ?? item.tag ?? item.text ?? item.phrase ?? item.reason ?? item.value,
    detail: item.detail ?? item.summary ?? item.note ?? item.description ?? item.text ?? item.reason ?? item.value ?? item.aliases,
    source: typeof item.source === 'string'
      ? item.source
      : source.type ?? source.kind ?? source.sourceType ?? item.sourceType ?? item.project ?? item.kind,
    sourceRecord: item.source,
    sourceChannel: item.sourceChannel,
    transportSource: item.transportSource,
    ref: item.ref,
    status: item.status ?? (item.active === false ? 'inactive' : 'active'),
    type: item.type ?? item.bookType ?? item.kind,
    text: item.text ?? item.textPreview ?? item.phrase,
    summary: item.summary,
    note: item.note,
    description: item.description,
    reason: item.reason,
    active: item.active,
    color: item.color ?? item.color_token,
    tags: safeCatalogStringList(item.tags),
    aliases: safeCatalogStringList(item.aliases),
    ownerKind: item.ownerKind ?? item.owner_kind,
    ownerId: item.ownerId ?? item.owner_id,
    ownerDisplayName: item.ownerDisplayName ?? item.owner_display_name,
    ownerName: item.ownerName ?? item.owner_name,
    ownerLabel: item.ownerLabel ?? item.owner_label,
    disposition: item.disposition,
    dispositionReason: item.dispositionReason,
    trustClass: item.trustClass,
    sensitive: item.sensitive,
    canForget: item.canForget,
    canRestore: item.canRestore,
    bundleId: item.bundleId ?? item.app,
    eventCount: item.eventCount,
    finalizedSegmentCount: item.finalizedSegmentCount,
    contextGroupCount: item.contextGroupCount,
    atomCount: item.atomCount,
    bookCount: item.bookCount,
    confidence: item.confidence,
    qualityScore: item.qualityScore,
    createdAtMs: item.createdAtMs ?? item.created_at_ms,
    latestAtMs: item.latestAtMs,
    updatedAtMs: item.updatedAtMs ?? item.updated_at_ms,
    revision: item.revision,
    evidenceRefs: item.evidenceRefs ?? item.sourceRefs ?? item.references ?? item.sourceEventIds,
    memories: item.memories,
  };
}

function safeCatalogStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (typeof item !== 'string') return [];
    const text = item.trim().slice(0, 64);
    return text ? [text] : [];
  }).slice(0, 64);
}

/**
 * 目录冷启动引导：这一层还没有任何记录时，讲清内容沿哪条链路沉淀进来，
 * 而不是把「空」说成筛选错误。文案只陈述本分支真实存在的链路——
 * 来源记录（工作回执）→ 分批审核 → 已整理记忆 → 长期主题，以及详情页里
 * 「最近被哪些 Session 装配」的回执区块。「交给 Agent 一件事」只在桌面
 * 能真正打开 Agent App 时出现，脱离桌面不渲染打不开的入口。
 */
function MemoryColdLead({
  kind,
  onStartWork,
}: {
  kind: MemoryKind;
  onStartWork?: () => void;
}) {
  const lead = ({
    evidence: {
      icon: Archive,
      title: '还没有来源记录',
      description: '来源记录是工作留下的回执：输入法、语音与伙伴主动记录先落在这里，经分批审核后才沉淀为记忆。',
    },
    atoms: {
      icon: Tags,
      title: '记忆从工作回执沉淀',
      description: '交给 Agent 一件事，工作留下来源记录，分批审核后沉淀成这里的记忆；每条记忆之后被哪些 Session 装配，都能在它的详情里看到。',
    },
    books: {
      icon: BookOpen,
      title: '还没有长期主题',
      description: '长期主题把相关记忆按主题组织，方便持续查找；先沉淀已整理记忆，再整理成主题。',
    },
  } as Partial<Record<MemoryKind, { icon: typeof Archive; title: string; description: string }>>)[kind];
  if (!lead) {
    return (
      <EmptyState
        description={`还没有 ${kindLabel(kind)} 记录。`}
        icon={Search}
        title="暂无记录"
      />
    );
  }
  return (
    <EmptyState
      action={onStartWork ? (
        <Button onClick={onStartWork} size="small">交给 Agent 一件事</Button>
      ) : undefined}
      description={lead.description}
      icon={lead.icon}
      title={lead.title}
    />
  );
}

function memoryLayerDescription(kind: MemoryKind): string {
  return ({
    evidence: '查看输入法、语音与伙伴主动记录形成的记忆来源。',
    atoms: '查看完整记忆目录、当前状态以及每条记忆的来源。',
    books: '查看长期主题如何整理相关记忆，并追溯到原始来源。',
  } as Partial<Record<MemoryKind, string>>)[kind] ?? '查看当前记忆层的内容与来源。';
}

function catalogRowMeta(
  kind: MemoryKind,
  row: Record<string, unknown>,
  assistantName: string,
): string {
  const references = catalogReferences(row, kind);
  const owner = ownerLabel(stringValue(row.ownerKind), stringValue(row.ownerId), memoryOwnerName(row));
  if (kind === 'books') {
    const atomCount = Math.max(
      numberValue(row.atomCount),
      references.filter((reference) => reference.kind === 'atom').length,
    );
    return [`${atomCount} 条记忆`, owner, formatUpdatedAt(numberValue(row.updatedAtMs))]
      .filter(Boolean)
      .join(' · ');
  }
  if (kind === 'atoms') {
    const evidenceCount = references.filter((reference) => (
      reference.kind === 'evidence' || reference.kind === 'event'
    )).length;
    return [atomTypeLabel(stringValue(row.type)), `${evidenceCount} 条来源`, owner]
      .filter(Boolean)
      .join(' · ');
  }
  return [
    catalogSourceLabel(kind, row, assistantName),
    owner,
    formatUpdatedAt(numberValue(row.updatedAtMs)),
  ].filter(Boolean).join(' · ');
}

function atomTypeLabel(value: string): string {
  return ({
    personal_fact: '个人事实',
    personal_habit: '稳定习惯',
    durable_preference: '稳定偏好',
    personal_principle: '长期原则',
    project_fact: '项目事实',
    project_requirement: '项目要求',
    project_decision: '项目决定',
    project_plan: '持续计划',
    project_constraint: '项目约束',
    security_constraint: '安全约束',
    source_event_archive: '来源归档',
  } as Record<string, string>)[value] ?? (value ? `类型 · ${value}` : '未分类记忆');
}

function MemoryCatalogDetail({
  assistantName,
  kind,
  onOpenReference,
  row,
}: {
  assistantName: string;
  kind: MemoryKind;
  onOpenReference: (reference: MemoryReferenceSelection) => void;
  row: Record<string, unknown>;
}) {
  const values = Array.isArray(row.tags)
    ? safeCatalogStringList(row.tags)
    : Array.isArray(row.aliases)
      ? safeCatalogStringList(row.aliases)
      : [];
  const references = catalogReferences(row, kind);
  const lineageReferences = primaryLineageReferences(kind, references);
  const rootReference = catalogRootReference(row, kind);
  const status = stringValue(row.status);
  const redacted = row.sensitive === true;
  const content = memoryCatalogContent(kind, row);
  return (
    <section className="memory-catalog-detail" aria-label={`${publicMemoryText(stringValue(row.title, '记忆'))} 详情`}>
      <div className="memory-catalog-detail__identity">
        <span><Fingerprint aria-hidden="true" size={16} />{kindLabel(kind)}</span>
        <h3>{publicMemoryText(stringValue(row.title, '未命名记忆'))}</h3>
        <div className="memory-catalog-detail__body" data-content={content.kind}>
          <small>{redacted ? '内容状态' : content.label}</small>
          <p>{redacted ? '正文因为隐私策略已隐藏，只保留可审计的来源和状态。' : publicMemoryText(content.text)}</p>
        </div>
      </div>
      <dl>
        <div><dt>状态</dt><dd>{catalogStatusLabel(kind, status)}</dd></div>
        <div><dt>来源</dt><dd>{catalogSourceLabel(kind, row, assistantName)}</dd></div>
        {stringValue(row.ownerKind) ? (
          <div><dt>归属</dt><dd>{ownerLabel(stringValue(row.ownerKind), stringValue(row.ownerId), memoryOwnerName(row))}</dd></div>
        ) : null}
        <div><dt>更新</dt><dd>{formatUpdatedAt(numberValue(row.updatedAtMs))}</dd></div>
        <div><dt>标签</dt><dd>{values.length ? `${values.length} 项` : '暂无'}</dd></div>
        <div><dt>关联来源</dt><dd>{references.length ? `${references.length} 条` : '暂无'}</dd></div>
      </dl>
      {values.length ? (
        <>
          <div className="memory-catalog-detail__tags">
            {values.slice(0, 8).map((value) => <span key={value}>{publicMemoryText(value)}</span>)}
          </div>
          {values.length > 8 ? (
            <Disclosure
              className="memory-catalog-detail__tag-disclosure"
              contentClassName="memory-catalog-detail__tag-disclosure-body"
              summary={`查看其余 ${values.length - 8} 项标签`}
            >
              <div className="memory-catalog-detail__tags">
                {values.slice(8).map((value) => <span key={value}>{publicMemoryText(value)}</span>)}
              </div>
            </Disclosure>
          ) : null}
        </>
      ) : null}
      {redacted ? (
        <InlineNotice title="敏感内容已脱敏" tone="warning">
          此处不会显示原文。来源标识、处理时间和处理状态仍可供核对。
        </InlineNotice>
      ) : null}
      <div className="memory-lineage-panel">
        <div>
          <span><GitBranch aria-hidden="true" size={15} />{lineageHeading(kind)}</span>
          <strong>{lineageSummary(kind, lineageReferences.length)}</strong>
          <p>{lineageDescription(kind, references.length - lineageReferences.length)}</p>
        </div>
        {lineageReferences.length ? (
          <div className="memory-reference-list" aria-label={`${kindLabel(kind)} 的下一层引用`}>
            {lineageReferences.map((reference) => (
              <button key={`${reference.kind}:${reference.referenceId}`} onClick={() => onOpenReference(reference)} type="button">
                <Fingerprint aria-hidden="true" size={14} />
                <span><small>{referenceKindCode(reference.kind)}</small>{publicMemoryText(reference.label || referenceKindLabel(reference.kind))}</span>
                <ChevronRight aria-hidden="true" size={14} />
              </button>
            ))}
          </div>
        ) : null}
        {rootReference ? (
          <Button
            leadingIcon={<GitBranch size={14} />}
            onClick={() => onOpenReference(rootReference)}
            size="small"
            variant="quiet"
          >
            查看最早的来源
          </Button>
        ) : null}
      </div>
      <EvidenceEchoUsage
        appId="memory"
        entityId={stringValue(row.id)}
        entityLabel={publicMemoryText(stringValue(row.title, '这条记忆'))}
      />
    </section>
  );
}

function memoryCatalogContent(
  kind: MemoryKind,
  row: Record<string, unknown>,
): { kind: 'full' | 'summary'; label: string; text: string } {
  const fullText = stringValue(row.text).trim();
  if (fullText) {
    return {
      kind: 'full',
      label: kind === 'evidence' ? '来源内容' : kind === 'books' ? '主题内容' : '记忆正文',
      text: fullText,
    };
  }
  return {
    kind: 'summary',
    label: kind === 'evidence' ? '来源说明' : kind === 'books' ? '主题摘要' : '记忆摘要',
    text: stringValue(row.detail, '暂无可显示内容'),
  };
}

function catalogRootReference(
  row: Record<string, unknown>,
  kind: MemoryKind,
): MemoryReferenceSelection | null {
  const canonicalRef = asRecord(row.ref);
  const referenceId = stringValue(
    canonicalRef.referenceId,
    stringValue(canonicalRef.refId, stringValue(canonicalRef.id, stringValue(row.id))),
  );
  if (!referenceId) return null;
  const explicitKind = stringValue(
    canonicalRef.kind,
    stringValue(canonicalRef.referenceKind, stringValue(canonicalRef.type)),
  );
  if (kind === 'atoms') return {
    kind: normalizeCatalogReferenceKind(explicitKind, referenceId, 'atom'),
    referenceId,
    label: stringValue(row.title),
  };
  if (kind === 'books') return {
    kind: normalizeCatalogReferenceKind(explicitKind, referenceId, 'book'),
    referenceId,
    label: stringValue(row.title),
  };
  if (kind === 'evidence') {
    return {
      kind: normalizeCatalogReferenceKind(
        explicitKind,
        referenceId,
        /^\d+$/u.test(referenceId) || referenceId.startsWith('input-memory:') ? 'event' : 'evidence',
      ),
      referenceId,
      label: stringValue(row.title),
    };
  }
  return null;
}

function catalogReferences(
  row: Record<string, unknown>,
  kind: MemoryKind,
): MemoryReferenceSelection[] {
  const candidates = [
    ...(Array.isArray(row.evidenceRefs) ? row.evidenceRefs : []),
    ...(Array.isArray(row.memories) ? row.memories : []),
  ];
  const fallback: MemoryReferenceSelection['kind'] = kind === 'books' ? 'atom' : 'event';
  const seen = new Set<string>();
  return candidates.flatMap((value): MemoryReferenceSelection[] => {
    const item = asRecord(value);
    const referenceId = typeof value === 'string' || typeof value === 'number'
      ? String(value)
      : stringValue(
        item.referenceId,
        stringValue(item.refId, stringValue(item.id, stringValue(item.sourceId))),
      );
    if (!referenceId) return [];
    const referenceKind = normalizeCatalogReferenceKind(
      stringValue(
        item.kind,
        stringValue(item.referenceKind, stringValue(item.type, stringValue(item.sourceType))),
      ),
      referenceId,
      fallback,
    );
    const key = `${referenceKind}:${referenceId}`;
    if (seen.has(key)) return [];
    seen.add(key);
    return [{
      kind: referenceKind,
      referenceId,
      label: stringValue(item.label, stringValue(item.title, stringValue(item.textPreview, stringValue(item.text)))),
    }];
  }).slice(0, 40);
}

function primaryLineageReferences(
  kind: MemoryKind,
  references: MemoryReferenceSelection[],
): MemoryReferenceSelection[] {
  if (kind === 'books') {
    const atoms = references.filter((reference) => reference.kind === 'atom');
    return atoms.length ? atoms : references;
  }
  if (kind === 'atoms') {
    const evidence = references.filter((reference) => (
      reference.kind === 'evidence' || reference.kind === 'event'
    ));
    return evidence.length ? evidence : references;
  }
  if (kind === 'evidence') {
    const events = references.filter((reference) => reference.kind === 'event');
    return events.length ? events : references;
  }
  return references;
}

function lineageHeading(kind: MemoryKind): string {
  return ({
    books: '包含的记忆',
    atoms: '相关来源',
    evidence: '原始来源',
  } as Partial<Record<MemoryKind, string>>)[kind] ?? '来源链';
}

function lineageSummary(kind: MemoryKind, count: number): string {
  if (!count) return '从稳定引用读取完整来源';
  if (kind === 'books') return `${count} 条记忆组成这个主题`;
  if (kind === 'atoms') return `${count} 条来源支持这条记忆`;
  if (kind === 'evidence') return `${count} 条原始记录构成这份证据`;
  return `${count} 条直接引用`;
}

function lineageDescription(kind: MemoryKind, shortcutCount: number): string {
  const base = kind === 'books'
    ? '先打开主题，再查看其中的记忆与来源；主题摘要不会替代原始记忆。'
    : kind === 'atoms'
      ? '相关来源是这条记忆的依据；继续展开可以查看原始记录与当时允许显示的上下文。'
      : '证据会保留原始来源；移出记忆或状态变化不会删除这条核对路径。';
  return shortcutCount > 0
    ? `${base} 另有 ${shortcutCount} 条快捷引用可供核对。`
    : base;
}

function normalizeCatalogReferenceKind(
  value: string,
  referenceId: string,
  fallback: MemoryReferenceSelection['kind'],
): MemoryReferenceSelection['kind'] {
  const normalized = value.toLocaleLowerCase('en-US').replaceAll('-', '_');
  if (normalized.includes('role_book')) return 'role_book_revision';
  if (normalized.includes('timeline')) return 'timeline';
  if (normalized.includes('book')) return 'book';
  if (normalized.includes('atom') || normalized === 'fact') return 'atom';
  if (normalized.includes('evidence')) return 'evidence';
  if (normalized.includes('event')) return 'event';
  if (referenceId.startsWith('atom:')) return 'atom';
  if (referenceId.startsWith('book:')) return 'book';
  if (referenceId.startsWith('timeline:')) return 'timeline';
  if (referenceId.startsWith('evidence:')) return 'evidence';
  return fallback;
}

function referenceKindCode(kind: MemoryReferenceSelection['kind']): string {
  return ({
    event: '来源',
    evidence: '证据',
    atom: '记忆',
    book: '主题',
    timeline: '活动',
    role_book_revision: '伙伴设定',
  } as const)[kind];
}

function referenceKindLabel(kind: MemoryReferenceSelection['kind']): string {
  return ({
    event: '原始来源',
    evidence: '引用证据',
    atom: '已整理记忆',
    book: '长期主题',
    timeline: '活动记录',
    role_book_revision: '伙伴记忆版本',
  } as const)[kind];
}

interface MemoryEditDraft {
  title: string;
  text: string;
  summary: string;
  note: string;
  description: string;
  tags: string;
  aliases: string;
  type: string;
  color: string;
  reason: string;
  active: boolean;
}

function DirectMemoryEditAction({
  disabledReason,
  onClick,
}: {
  disabledReason: string;
  onClick: () => void;
}) {
  return (
    <div className="mgmt-workflow" data-availability={disabledReason ? 'blocked' : 'available'} data-stage="idle">
      <div className="mgmt-workflow__heading">
        <div>
          <span className="mgmt-workflow__risk">本机写入</span>
          <strong>编辑内容</strong>
          <p>直接编辑当前选中的记忆，你保存的文字会按原样保留。</p>
        </div>
        <Button disabled={Boolean(disabledReason)} onClick={onClick} size="small">编辑</Button>
      </div>
      <p className="memory-action-unavailable">{disabledReason || '保存只会作用于当前选择的这项记忆。'}</p>
    </div>
  );
}

function MemoryEvidenceDispositionAction({
  disabledReason,
  onChanged,
  row,
}: {
  disabledReason: string;
  onChanged: () => Promise<unknown> | void;
  row: Record<string, unknown> | undefined;
}) {
  const transport = useControlTransport();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const evidenceId = stringValue(row?.id);
  const disposition = 'not_for_memory';

  async function submit() {
    if (!evidenceId || disabledReason) return;
    setSaving(true);
    setError('');
    try {
      const result = asRecord(await transport.request({
        pathId: 'memory.source.disposition',
        body: { evidenceId, disposition },
      }));
      if (result.ok !== true) throw new Error('memory source disposition rejected');
      await onChanged();
    } catch {
      setError('操作失败，证据状态没有改变。');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mgmt-workflow" data-availability={disabledReason ? 'blocked' : 'available'} data-confirmation="direct" data-stage="idle">
      <div className="mgmt-workflow__heading">
        <div>
          <strong>移出记忆</strong>
          <p>不再用于长期记忆与后续联想；原始输入仍保留在本机记录中。</p>
        </div>
        <Button
          disabled={Boolean(disabledReason)}
          leadingIcon={<EyeOff size={14} />}
          loading={saving}
          onClick={() => void submit()}
          size="small"
        >
          移出
        </Button>
      </div>
      <p className="memory-action-unavailable">
        {error || disabledReason || '移出后不再用于记忆；原始记录不会被删除。'}
      </p>
    </div>
  );
}

function MemoryEditDialog({
  kind,
  onOpenChange,
  onSaved,
  open,
  row,
}: {
  kind: MemoryKind;
  onOpenChange: (open: boolean) => void;
  onSaved: () => Promise<unknown> | void;
  open: boolean;
  row: Record<string, unknown> | undefined;
}) {
  const transport = useControlTransport();
  const identity = stringValue(row?.id);
  const editRevision = useRef(stringValue(row?.revision));
  const editAttempt = useRef<{ signature: string; id: string } | undefined>(undefined);
  const editLock = useRef(false);
  const [draft, setDraft] = useState<MemoryEditDraft>(() => memoryEditDraft(kind, row));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    setDraft(memoryEditDraft(kind, row));
    editRevision.current = stringValue(row?.revision);
    editAttempt.current = undefined;
    setError('');
  }, [identity, kind, open]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (editLock.current) return;
    const validation = memoryEditValidation(kind, draft);
    if (validation) {
      setError(validation);
      return;
    }
    if (!identity) {
      setError('这项记忆已经不可用，请关闭窗口后重新选择。');
      return;
    }
    editLock.current = true;
    setSaving(true);
    setError('');
    try {
      const body = memoryEditBody(kind, identity, draft);
      const signature = JSON.stringify(body);
      if (editAttempt.current?.signature !== signature) editAttempt.current = { signature, id: `memory-card-${crypto.randomUUID()}` };
      const result = asRecord(await transport.request({
        pathId: 'memory.edit',
        body: { ...body, ...(kind === 'atoms' ? { expectedRevision: editRevision.current, clientRequestId: editAttempt.current.id } : {}) },
      }));
      if (result.ok !== true) throw new Error('memory edit rejected');
      try {
        await onSaved();
      } catch {
        // The write succeeded; a later manual refresh can recover a failed read.
      }
      onOpenChange(false);
    } catch {
      setError('保存失败。草稿已保留，请确认这项记忆仍然存在后重试。');
    } finally {
      editLock.current = false;
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!saving) onOpenChange(next); }}>
      <DialogContent className="memory-edit-dialog">
        <DialogHeader>
          <DialogTitle>编辑 {kindLabel(kind)}</DialogTitle>
          <DialogDescription>修改只会写入当前选中的本机记忆。</DialogDescription>
        </DialogHeader>
        <form className="memory-edit-form" id="memory-edit-form" onSubmit={(event) => void submit(event)}>
          <MemoryEditFields draft={draft} kind={kind} setDraft={setDraft} />
          {error ? <p className="memory-edit-error" role="alert">{error}</p> : null}
        </form>
        <DialogFooter>
          <Button disabled={saving} onClick={() => onOpenChange(false)} variant="quiet">取消</Button>
          <Button form="memory-edit-form" loading={saving} type="submit" variant="primary">保存</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function MemoryEditFields({
  draft,
  kind,
  setDraft,
}: {
  draft: MemoryEditDraft;
  kind: MemoryKind;
  setDraft: (draft: MemoryEditDraft) => void;
}) {
  const update = <Key extends keyof MemoryEditDraft>(key: Key, value: MemoryEditDraft[Key]) => {
    setDraft({ ...draft, [key]: value });
  };
  const colorField = (
    <Field htmlFor="memory-edit-color" label="颜色">
      <Select id="memory-edit-color" onValueChange={(value) => update('color', value)} options={[
        { value: 'blue', label: '蓝色' },
        { value: 'teal', label: '青色' },
        { value: 'green', label: '绿色' },
        { value: 'orange', label: '橙色' },
        { value: 'pink', label: '粉色' },
        { value: 'purple', label: '紫色' },
        { value: 'gray', label: '灰色' },
      ]} value={draft.color} />
    </Field>
  );

  if (kind === 'apps') return null;

  if (kind === 'books') {
    return <>
      <Field htmlFor="memory-edit-title" label="标题" required><Input id="memory-edit-title" maxLength={240} onChange={(event) => update('title', event.target.value)} value={draft.title} /></Field>
      <Field htmlFor="memory-edit-summary" label="摘要"><TextArea id="memory-edit-summary" maxLength={4_000} onChange={(event) => update('summary', event.target.value)} rows={4} value={draft.summary} /></Field>
      <ListField id="memory-edit-tags" label="标签" onChange={(value) => update('tags', value)} value={draft.tags} />
    </>;
  }
  if (kind === 'atoms') {
    return <>
      <Field htmlFor="memory-edit-text" label="正文" required><TextArea id="memory-edit-text" maxLength={8_000} onChange={(event) => update('text', event.target.value)} rows={5} value={draft.text} /></Field>
      <ListField id="memory-edit-tags" label="标签" onChange={(value) => update('tags', value)} value={draft.tags} />
    </>;
  }
  if (kind === 'tags') {
    return <>
      <Field htmlFor="memory-edit-title" label="标签名" required><Input id="memory-edit-title" maxLength={120} onChange={(event) => update('title', event.target.value)} value={draft.title} /></Field>
      <Field htmlFor="memory-edit-description" label="说明"><TextArea id="memory-edit-description" maxLength={2_000} onChange={(event) => update('description', event.target.value)} rows={3} value={draft.description} /></Field>
      <div className="memory-edit-form__split">
        <Field htmlFor="memory-edit-type" label="类型"><Input id="memory-edit-type" maxLength={64} onChange={(event) => update('type', event.target.value)} value={draft.type} /></Field>
        {colorField}
      </div>
      <ListField id="memory-edit-aliases" label="别名" onChange={(value) => update('aliases', value)} value={draft.aliases} />
    </>;
  }
  if (kind === 'phrases') {
    return <Field htmlFor="memory-edit-text" label="短语" required><TextArea id="memory-edit-text" maxLength={2_000} onChange={(event) => update('text', event.target.value)} rows={4} value={draft.text} /></Field>;
  }
  if (kind === 'groups') {
    return <>
      <Field htmlFor="memory-edit-title" label="名称"><Input id="memory-edit-title" maxLength={240} onChange={(event) => update('title', event.target.value)} value={draft.title} /></Field>
      <Field htmlFor="memory-edit-note" label="说明"><TextArea id="memory-edit-note" maxLength={4_000} onChange={(event) => update('note', event.target.value)} rows={4} value={draft.note} /></Field>
      {colorField}
    </>;
  }
  return <>
    <Field htmlFor="memory-edit-reason" label="原因" required><TextArea id="memory-edit-reason" maxLength={2_000} onChange={(event) => update('reason', event.target.value)} rows={4} value={draft.reason} /></Field>
    <Switch checked={draft.active} description="关闭后，这条负反馈将不再参与过滤。" label="启用这条负反馈" onCheckedChange={(checked) => update('active', checked)} />
  </>;
}

function ListField({ id, label, onChange, value }: { id: string; label: string; onChange: (value: string) => void; value: string }) {
  return <Field description="用逗号或换行分隔" htmlFor={id} label={label}><TextArea id={id} maxLength={4_000} onChange={(event) => onChange(event.target.value)} rows={3} value={value} /></Field>;
}

function memoryEditDraft(kind: MemoryKind, row: Record<string, unknown> | undefined): MemoryEditDraft {
  const item = row ?? {};
  return {
    title: stringValue(item.title),
    text: stringValue(item.text),
    summary: stringValue(item.summary),
    note: stringValue(item.note),
    description: stringValue(item.description),
    tags: safeCatalogStringList(item.tags).join('，'),
    aliases: safeCatalogStringList(item.aliases).join('，'),
    type: stringValue(item.type, kind === 'tags' ? 'concept' : ''),
    color: stringValue(item.color, 'blue'),
    reason: stringValue(item.reason),
    active: item.active !== false && numberValue(item.active, 1) !== 0,
  };
}

function memoryEditBody(kind: MemoryKind, id: string, draft: MemoryEditDraft): Record<string, JsonValue> {
  if (kind === 'apps') return { kind, id };
  if (kind === 'books') return { kind, id, title: draft.title.trim(), summary: draft.summary.trim(), tags: splitList(draft.tags) };
  if (kind === 'atoms') return { kind, id, text: draft.text.trim(), tags: splitList(draft.tags) };
  if (kind === 'tags') return { kind, id, title: draft.title.trim(), description: draft.description.trim(), type: draft.type.trim() || 'concept', aliases: splitList(draft.aliases), color: draft.color };
  if (kind === 'phrases') return { kind, id, text: draft.text.trim() };
  if (kind === 'groups') return { kind, id, title: draft.title.trim(), note: draft.note.trim(), color: draft.color };
  return { kind, id, reason: draft.reason.trim(), active: draft.active };
}

function memoryEditValidation(kind: MemoryKind, draft: MemoryEditDraft): string {
  if (kind === 'apps') return '应用上下文由输入来源自动维护，不能手动编辑。';
  if ((kind === 'books' || kind === 'tags') && !draft.title.trim()) return '请填写标题。';
  if ((kind === 'atoms' || kind === 'phrases') && !draft.text.trim()) return '请填写内容。';
  if (kind === 'negative' && !draft.reason.trim()) return '请填写原因。';
  return '';
}

function splitList(value: string): string[] {
  return [...new Set(value.split(/[，,\n]/u).map((item) => item.trim()).filter(Boolean))].slice(0, 64);
}

function UnavailableMemoryAction({
  description,
  reason,
  title,
}: {
  description: string;
  reason: string;
  risk: 'R1' | 'R2';
  title: string;
}) {
  return (
    <div className="mgmt-workflow" data-availability="unsupported" data-confirmation="direct" data-stage="idle">
      <div className="mgmt-workflow__heading">
        <div>
          <strong>{title}</strong>
          <p>{description}</p>
        </div>
        <StatusBadge label="当前不可用" tone="neutral" />
      </div>
      <p className="memory-action-unavailable">{reason}</p>
    </div>
  );
}

function kindLabel(kind: MemoryKind): string {
  return {
    apps: '应用',
    books: '长期主题',
    atoms: '已整理记忆',
    tags: '标签',
    phrases: '短语',
    evidence: '记忆来源',
    groups: '分组',
    negative: '负反馈',
  }[kind];
}

function memoryStatusOptions(kind: MemoryKind) {
  if (kind === 'evidence') {
    return [
      { value: '', label: '全部' },
      { value: 'pending', label: '待整理' },
      { value: 'remember', label: '值得保留' },
      { value: 'needs_review', label: '待判断' },
      { value: 'consolidated', label: '已整理为记忆' },
    ];
  }
  return [
    { value: '', label: '全部' },
    ...(kind === 'atoms'
      ? [{ value: 'current', label: '使用中' }]
      : [{ value: 'active', label: '使用中' }]),
    { value: 'approved', label: '已确认' },
    { value: 'archived', label: '已归档' },
    { value: 'hidden', label: '历史保留' },
    { value: 'superseded', label: '已合并' },
    { value: 'conflict', label: '有冲突' },
    { value: 'source_archive', label: '来源归档' },
    { value: 'disabled', label: '已暂停' },
    { value: 'suppressed', label: '已抑制' },
  ];
}

function statusLabel(status: string): string {
  return {
    active: '使用中',
    current: '当前记忆',
    approved: '已确认',
    archived: '已归档',
    hidden: '历史保留',
    superseded: '已合并',
    source_archive: '来源归档',
    disabled: '已暂停',
    suppressed: '已抑制',
    inactive: '未启用',
    tombstoned: '已移除',
    pending: '待整理',
    remember: '值得保留',
    not_for_memory: '已遗忘',
    forgotten: '已遗忘',
    needs_review: '待判断',
    consolidated: '已整理为记忆',
    conflict: '有冲突',
    conflicted: '有冲突',
    redacted: '已脱敏',
    expired: '已过期',
  }[status] ?? '状态未知';
}

function catalogStatusLabel(kind: MemoryKind, status: string): string {
  if (kind === 'evidence' && status === 'active') return '已引用';
  return statusLabel(status);
}

function catalogStatusTone(kind: MemoryKind, status: string): 'success' | 'warning' | 'danger' | 'info' | 'neutral' {
  if (kind === 'evidence' && status === 'active') return 'info';
  return statusTone(status);
}

function statusTone(status: string): 'success' | 'warning' | 'danger' | 'info' | 'neutral' {
  if (status === 'active' || status === 'current' || status === 'approved' || status === 'consolidated') return 'success';
  if (status === 'archived' || status === 'hidden' || status === 'source_archive' || status === 'inactive' || status === 'not_for_memory' || status === 'forgotten' || status === 'expired') return 'info';
  if (status === 'disabled' || status === 'suppressed' || status === 'pending' || status === 'needs_review' || status === 'remember' || status === 'redacted' || status === 'conflict' || status === 'conflicted') return 'warning';
  if (status === 'tombstoned') return 'danger';
  return 'neutral';
}

function catalogSourceLabel(kind: MemoryKind, row: Record<string, unknown>, assistantName: string): string {
  if (kind !== 'evidence') return publicMemorySourceLabel(stringValue(row.source), assistantName);
  const channel = stringValue(row.sourceChannel);
  if (channel === 'input_method') return '输入法';
  if (channel === 'voice') return '语音';
  if (channel === 'agent_capture') return `${assistantName}主动记录`;
  const transport = stringValue(row.transportSource);
  if (transport.toLocaleLowerCase('en-US').includes('voice') || transport.toLocaleLowerCase('en-US').includes('asr')) {
    return '语音';
  }
  if (transport.toLocaleLowerCase('en-US').includes('rime')) return '输入法';
  if (stringValue(row.type) === 'session_digest') return `${assistantName}主动记录`;
  return '本机记录';
}

function ownerAwareKind(kind: MemoryKind): boolean {
  return kind === 'books' || kind === 'atoms' || kind === 'phrases' || kind === 'evidence';
}

function encodeOwnerKey(ownerKind: string, ownerId: string): string {
  return ownerKind && ownerId ? JSON.stringify([ownerKind, ownerId]) : '';
}

function parseOwnerKey(value: string): { ownerKind: string; ownerId: string } {
  try {
    const parsed: unknown = JSON.parse(value);
    if (
      Array.isArray(parsed)
      && parsed.length === 2
      && typeof parsed[0] === 'string'
      && typeof parsed[1] === 'string'
    ) {
      return { ownerKind: parsed[0], ownerId: parsed[1] };
    }
  } catch {
    // An empty or stale filter means "all owners".
  }
  return { ownerKind: '', ownerId: '' };
}

function ownerLabel(ownerKind: string, ownerId: string, ownerName = ''): string {
  return publicMemoryOwnerLabel(ownerKind, ownerId, ownerName);
}

function memoryOwnerName(value: Record<string, unknown>): string {
  return stringValue(
    value.ownerDisplayName,
    stringValue(value.ownerName, stringValue(value.displayName, stringValue(value.ownerLabel))),
  );
}

function formatUpdatedAt(value: number): string {
  if (!value) return '暂无';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(value);
}
