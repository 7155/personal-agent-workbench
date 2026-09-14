import {
  Ban,
  CheckCircle2,
  CircleAlert,
  LibraryBig,
  PackageCheck,
  RefreshCw,
  RotateCcw,
  Upload,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, EmptyState } from '@/components/primitives';
import { InlineNotice, StatusBadge } from '@/features/overview/management-ui';
import { TeamApiError, teamApiErrorMessage } from './team-api';
import { useTeam } from './team-context';
import { PAW_EXTENSION_INSTALLATION_CHANGED_EVENT } from '@/paw-os/extensions/installation';
import type {
  TeamPublishedResource,
  TeamResourceCatalogEntry,
  TeamResourceSelection,
} from './types';
import './TeamSharedResources.css';

export interface TeamSharedResourcesProps {
  /** The App Center rail uses `catalog` for the administrator's source view. */
  pageId?: string;
}

type LoadOptions = {
  replaceDraft?: boolean;
};

type ResourceGroup = {
  packageId: string;
  displayName: string;
  resources: TeamPublishedResource[];
};

const MAX_SELECTED_PACKAGES = 16;

/**
 * Team's App Center surface. It only manages server-owned publication metadata
 * and the current space's version selection; installation and execution stay
 * behind the Team runtime boundary.
 */
export function TeamSharedResources({ pageId = 'installed' }: TeamSharedResourcesProps) {
  const team = useTeam();
  const activeSpace = team.activeSpace;
  const spaceId = activeSpace?.id ?? '';
  const scopeKey = team.scopeKey ?? '';
  const isAdmin = team.user?.role === 'admin';
  const canManageSpace = activeSpace?.role === 'owner' || activeSpace?.role === 'maintainer';
  const showCatalog = pageId === 'catalog' && isAdmin;
  const [resources, setResources] = useState<TeamPublishedResource[] | null>(null);
  const [catalog, setCatalog] = useState<TeamResourceCatalogEntry[] | null>(null);
  const [selection, setSelection] = useState<TeamResourceSelection | null>(null);
  const [draftIds, setDraftIds] = useState<string[]>([]);
  const [draftDirty, setDraftDirty] = useState(false);
  const [loading, setLoading] = useState(false);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [error, setError] = useState('');
  const [catalogError, setCatalogError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');
  const generationRef = useRef(0);
  const mutationRef = useRef(0);
  const scopeEpochRef = useRef(0);
  const mountedRef = useRef(false);
  const scopeRef = useRef(scopeKey);
  const draftDirtyRef = useRef(draftDirty);

  scopeRef.current = scopeKey;
  draftDirtyRef.current = draftDirty;

  const isCurrent = useCallback((expectedScope: string, generation: number): boolean => (
    mountedRef.current
    && scopeRef.current === expectedScope
    && generationRef.current === generation
  ), []);

  const isCurrentMutation = useCallback((expectedScope: string, expectedEpoch: number, mutation: number): boolean => (
    mountedRef.current
    && scopeRef.current === expectedScope
    && scopeEpochRef.current === expectedEpoch
    && mutationRef.current === mutation
  ), []);

  const errorMessage = useCallback((reason: unknown, fallback: string): string => {
    if (reason instanceof TeamApiError && reason.status === 401) void team.retry().catch(() => undefined);
    return readableError(reason, fallback);
  }, [team.retry]);

  const load = useCallback(async ({ replaceDraft = false }: LoadOptions = {}): Promise<void> => {
    if (!spaceId || !scopeKey || team.phase !== 'authenticated') return;
    const expectedScope = scopeKey;
    const generation = ++generationRef.current;
    setLoading(true);
    setError('');
    if (replaceDraft) {
      setConflict(false);
      setNotice('');
    }

    const resourceRequest = team.api.listResources(team.csrfToken ?? undefined);
    const selectionRequest = team.api.getSpaceResourceSelection(spaceId, team.csrfToken ?? undefined);
    const catalogRequest = showCatalog
      ? team.api.listResourceCatalog(team.csrfToken ?? undefined)
      : Promise.resolve(null);
    const [resourceResult, selectionResult, catalogResult] = await Promise.allSettled([
      resourceRequest,
      selectionRequest,
      catalogRequest,
    ]);
    if (!isCurrent(expectedScope, generation)) return;

    if (resourceResult.status === 'fulfilled') {
      setResources(resourceResult.value);
    } else {
      setError(errorMessage(resourceResult.reason, '共享资源没有读取成功，请刷新后重试。'));
    }
    if (selectionResult.status === 'fulfilled') {
      setSelection(selectionResult.value);
      if (replaceDraft || !draftDirtyRef.current) {
        setDraftIds(selectionResult.value.publicationIds);
        setDraftDirty(false);
      }
    } else {
      setError(errorMessage(selectionResult.reason, '当前空间的资源选择没有读取成功，请刷新后重试。'));
    }
    if (showCatalog) {
      setCatalogLoading(true);
      if (catalogResult.status === 'fulfilled' && catalogResult.value) {
        setCatalog(catalogResult.value);
        setCatalogError('');
      } else if (catalogResult.status === 'rejected') {
        setCatalogError(errorMessage(catalogResult.reason, '发布目录没有读取成功，请刷新后重试。'));
      }
      setCatalogLoading(false);
    } else {
      setCatalog(null);
      setCatalogError('');
    }
    setLoading(false);
  }, [errorMessage, isCurrent, scopeKey, showCatalog, spaceId, team.api, team.csrfToken, team.phase]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
      scopeEpochRef.current += 1;
      mutationRef.current += 1;
      scopeRef.current = '';
    };
  }, []);

  useEffect(() => {
    scopeEpochRef.current += 1;
    generationRef.current += 1;
    setResources(null);
    setCatalog(null);
    setSelection(null);
    setDraftIds([]);
    setDraftDirty(false);
    setLoading(false);
    setError('');
    setCatalogError('');
    setConflict(false);
    setNotice('');
    setBusy('');
    if (spaceId && scopeKey && team.phase === 'authenticated') void load();
    return () => {
      generationRef.current += 1;
    };
  }, [load, scopeKey, spaceId, team.phase]);

  const allResources = useMemo(() => mergeResources(resources ?? [], selection?.items ?? []), [resources, selection]);
  const groups = useMemo(() => groupResources(allResources), [allResources]);
  const selectedResourceIds = useMemo(() => new Set(draftIds), [draftIds]);
  const selectedPackageCount = useMemo(() => {
    const packageIds = new Set<string>();
    for (const resource of allResources) {
      if (selectedResourceIds.has(resource.publicationId)) packageIds.add(resource.packageId);
    }
    return packageIds.size;
  }, [allResources, selectedResourceIds]);
  const selectionDirty = Boolean(selection && draftDirty && !sameIds(selection.publicationIds, draftIds));
  const spaceLabel = activeSpace
    ? `${activeSpace.name} · ${activeSpace.kind === 'project' ? '项目成员可见' : '仅本人可见'}`
    : '正在读取当前空间';

  function changeSelection(packageId: string, publicationId: string): void {
    if (!canManageSpace || busy || conflict) return;
    const group = groups.find((candidate) => candidate.packageId === packageId);
    if (!group) return;
    const current = draftIds.find((id) => group.resources.some((resource) => resource.publicationId === id));
    const nextIds = draftIds.filter((id) => id !== current);
    if (publicationId) {
      if (!current && selectedPackageCount >= MAX_SELECTED_PACKAGES) {
        setError(`一个空间最多选择 ${MAX_SELECTED_PACKAGES} 个 Package。`);
        return;
      }
      const candidate = group.resources.find((resource) => resource.publicationId === publicationId);
      if (!candidate || (candidate.status !== 'published' && candidate.publicationId !== current)) return;
      nextIds.push(publicationId);
    }
    setDraftIds(unique(nextIds));
    setDraftDirty(true);
    setError('');
    setNotice('');
  }

  async function saveSelection(): Promise<void> {
    if (!activeSpace || !selection || !canManageSpace || !selectionDirty || busy || !team.csrfToken) return;
    const expectedScope = scopeKey;
    const expectedEpoch = scopeEpochRef.current;
    const mutation = ++mutationRef.current;
    setBusy('selection');
    setError('');
    setConflict(false);
    setNotice('');
    try {
      const next = await team.api.updateSpaceResourceSelection(
        activeSpace.id,
        { baseRevision: selection.revision, publicationIds: draftIds },
        team.csrfToken,
      );
      if (!isCurrentMutation(expectedScope, expectedEpoch, mutation)) return;
      // A read started while the write was in flight is older than this
      // response. Invalidate it before committing the server's CAS result.
      generationRef.current += 1;
      setSelection(next);
      setDraftIds(next.publicationIds);
      setDraftDirty(false);
      setNotice('当前空间的版本选择已保存；新任务会固定这些版本。');
      window.dispatchEvent(new Event(PAW_EXTENSION_INSTALLATION_CHANGED_EVENT));
    } catch (reason) {
      if (!isCurrentMutation(expectedScope, expectedEpoch, mutation)) return;
      if (reason instanceof TeamApiError && reason.status === 409) {
        setConflict(true);
        setError('当前空间的资源选择已变化，已保留你的编辑。读取最新空间选择后，再重新选择并保存。');
      } else {
        setError(errorMessage(reason, '当前空间的资源选择没有保存成功，请刷新后重试。'));
      }
    } finally {
      if (isCurrentMutation(expectedScope, expectedEpoch, mutation)) setBusy('');
    }
  }

  async function publish(entry: TeamResourceCatalogEntry): Promise<void> {
    if (!isAdmin || !entry.installable || !entry.version || busy || !team.csrfToken) return;
    const expectedScope = scopeKey;
    const expectedEpoch = scopeEpochRef.current;
    const mutation = ++mutationRef.current;
    setBusy(`publish:${entry.packageId}:${entry.version}`);
    setError('');
    setNotice('');
    try {
      await team.api.publishResource(entry.packageId, entry.version, team.csrfToken);
      if (!isCurrentMutation(expectedScope, expectedEpoch, mutation)) return;
      generationRef.current += 1;
      setNotice(`已发布 ${entry.displayName} v${entry.version}；请在空间选择中决定新任务是否使用它。`);
      await load();
    } catch (reason) {
      if (isCurrentMutation(expectedScope, expectedEpoch, mutation)) setError(errorMessage(reason, '版本发布没有完成，请刷新后重试。'));
    } finally {
      if (isCurrentMutation(expectedScope, expectedEpoch, mutation)) setBusy('');
    }
  }

  async function setStatus(resource: TeamPublishedResource, status: 'published' | 'withdrawn'): Promise<void> {
    if (!isAdmin || busy || !team.csrfToken) return;
    const expectedScope = scopeKey;
    const expectedEpoch = scopeEpochRef.current;
    const mutation = ++mutationRef.current;
    setBusy(`status:${resource.publicationId}`);
    setError('');
    setNotice('');
    try {
      await team.api.setPublishedResourceStatus(resource.publicationId, status, team.csrfToken);
      if (!isCurrentMutation(expectedScope, expectedEpoch, mutation)) return;
      generationRef.current += 1;
      setNotice(status === 'withdrawn'
        ? `已停止分发 ${resource.metadata.displayName} v${resource.version}；旧任务仍保留原版本。`
        : `已恢复分发 ${resource.metadata.displayName} v${resource.version}。`);
      await load();
    } catch (reason) {
      if (isCurrentMutation(expectedScope, expectedEpoch, mutation)) setError(errorMessage(reason, '版本状态没有更新，请刷新后重试。'));
    } finally {
      if (isCurrentMutation(expectedScope, expectedEpoch, mutation)) setBusy('');
    }
  }

  const showInitialLoading = loading && resources === null && selection === null;

  return (
    <div className="team-shared-resources">
      <header className="team-shared-resources__header">
        <div>
          <span className="team-shared-resources__eyebrow">团队 App Center</span>
          <h1>团队共享资源</h1>
          <p>查看团队已发布的 Package 版本，并为当前空间选择新任务使用的版本。</p>
        </div>
        <Button
          disabled={!spaceId || Boolean(busy)}
          leadingIcon={<RefreshCw size={15} />}
          loading={loading}
          onClick={() => void load()}
          size="small"
          variant="quiet"
        >刷新</Button>
      </header>

      <div className="team-shared-resources__scope" aria-label="当前团队空间">
        <PackageCheck aria-hidden="true" size={16} />
        <strong>{spaceLabel}</strong>
        <span>{team.user?.displayName || team.user?.username || ''} · </span>
        <strong>{canManageSpace ? '空间维护者可编辑' : '当前空间只读'}</strong>
      </div>
      <p className="team-shared-resources__disclosure">新任务会使用当前空间已选择的版本；旧任务及其子任务继续使用原快照。这里显示的是发布与选择记录，不代表已安装或正在运行。</p>

      {error ? <InlineNotice title="共享资源操作没有完成" tone="danger">{error}</InlineNotice> : null}
      {notice ? <InlineNotice title="共享资源状态已更新" tone="success">{notice}</InlineNotice> : null}
      {pageId === 'catalog' && !isAdmin ? (
        <InlineNotice title="发布目录仅管理员可查看" tone="warning">你仍可以查看当前空间的已发布版本与选择；发布操作由平台管理员负责。</InlineNotice>
      ) : null}
      {conflict ? (
        <div aria-live="polite" className="team-shared-resources__conflict">
          <CircleAlert aria-hidden="true" size={16} />
          <span>服务端保存的选择已经变化。你的编辑仍在页面上，读取最新选择后再决定要保留哪些版本。</span>
          <Button leadingIcon={<RotateCcw size={14} />} onClick={() => void load({ replaceDraft: true })} size="small" variant="secondary">读取最新空间选择</Button>
        </div>
      ) : null}

      {showInitialLoading ? <div className="team-shared-resources__loading" role="status">正在读取共享资源…</div> : null}
      {!showInitialLoading && resources !== null ? (
        <section aria-labelledby="team-shared-resources-published" className="team-shared-resources__section">
          <div className="team-shared-resources__section-heading">
            <div>
              <h2 id="team-shared-resources-published">已发布版本</h2>
              <p>全体团队成员可以查看；停止分发不会改写已创建任务的版本快照。</p>
            </div>
            <StatusBadge label={`${resources.length} 个版本`} tone="info" />
          </div>
          {resources.length ? (
            <div className="team-shared-resources__list">
              {resources.map((resource) => (
                <PublishedResourceRow
                  busy={busy === `status:${resource.publicationId}`}
                  canManage={isAdmin}
                  key={resource.publicationId}
                  onStatus={(status) => void setStatus(resource, status)}
                  resource={resource}
                />
              ))}
            </div>
          ) : (
            <EmptyState
              description={isAdmin ? '管理员可以从发布目录选择一个经过校验的精确版本。' : '管理员还没有发布可供团队使用的版本。'}
              headingLevel={3}
              icon={LibraryBig}
              title="还没有公共发布版本"
            />
          )}
        </section>
      ) : null}

      {activeSpace && selection ? (
        <section aria-labelledby="team-shared-resources-selection" className="team-shared-resources__section">
          <div className="team-shared-resources__section-heading">
            <div>
              <h2 id="team-shared-resources-selection">当前空间选择</h2>
              <p>每个 Package 只能选择一个版本，最多选择 {MAX_SELECTED_PACKAGES} 个。</p>
            </div>
            <StatusBadge label={`版本记录 v${selection.revision}`} tone="neutral" />
          </div>
          {canManageSpace ? (
            <>
              <div className="team-shared-resources__selection-grid">
                {groups.map((group) => {
                  const selected = group.resources.find((resource) => selectedResourceIds.has(resource.publicationId));
                  return (
                    <label className="team-shared-resources__selection-row" key={group.packageId}>
                      <span>
                        <strong>{group.displayName}</strong>
                        <small>{selected ? `当前为 v${selected.version}` : '不加入新任务'}</small>
                      </span>
                      <select
                        aria-label={`${group.displayName}版本`}
                        className="paw-select"
                        disabled={Boolean(busy) || conflict}
                        onChange={(event) => changeSelection(group.packageId, event.target.value)}
                        value={selected?.publicationId ?? ''}
                      >
                        <option value="">不加入新任务</option>
                        {group.resources.map((resource) => {
                          const selectable = resource.status === 'published' || resource.publicationId === selected?.publicationId;
                          return (
                            <option disabled={!selectable} key={resource.publicationId} value={resource.publicationId}>
                              v{resource.version} · {resource.status === 'published' ? '已发布' : '已停止分发（旧任务保留）'}
                            </option>
                          );
                        })}
                      </select>
                    </label>
                  );
                })}
              </div>
              {selectionDirty ? <p className="team-shared-resources__edit-note">你正在编辑当前空间选择；普通刷新会保留这份编辑。</p> : null}
              <div className="team-shared-resources__actions">
                <Button disabled={!selectionDirty || Boolean(busy) || conflict} loading={busy === 'selection'} onClick={() => void saveSelection()} leadingIcon={<CheckCircle2 size={14} />} variant="primary">保存当前空间选择</Button>
                <span>新任务会固定当前已保存的版本。</span>
              </div>
            </>
          ) : (
            <>
              {selection.items.length ? (
                <div aria-label="当前空间已选版本" className="team-shared-resources__selection-grid" role="list">
                  {selection.items.map((resource) => (
                    <div className="team-shared-resources__selection-row" key={resource.publicationId} role="listitem">
                      <span>
                        <strong>{resource.metadata.displayName}</strong>
                        <small>{resource.packageId}</small>
                      </span>
                      <div className="team-shared-resources__resource-heading">
                        <StatusBadge label={`v${resource.version}`} tone="info" />
                        <StatusBadge label={resource.status === 'published' ? '已发布' : '已停止分发'} tone={resource.status === 'published' ? 'success' : 'neutral'} />
                      </div>
                    </div>
                  ))}
                </div>
              ) : <p className="team-shared-resources__empty">当前空间尚未选择共享版本。</p>}
              <div className="team-shared-resources__readonly">
                <CircleAlert aria-hidden="true" size={16} />
                <span>当前空间只读；只有空间所有者或维护者可以选择新任务版本。</span>
              </div>
            </>
          )}
          {canManageSpace && !groups.length ? <p className="team-shared-resources__empty">当前没有可选择的已发布版本。</p> : null}
        </section>
      ) : null}

      {showCatalog ? (
        <section aria-labelledby="team-shared-resources-catalog" className="team-shared-resources__section">
          <div className="team-shared-resources__section-heading">
            <div>
              <h2 id="team-shared-resources-catalog">发布目录</h2>
              <p>管理员可以从现有 Package 清单发布精确版本；发布证据不表示已经安装或运行。</p>
            </div>
            {catalogLoading ? <span className="team-shared-resources__muted">正在读取目录…</span> : null}
          </div>
          {catalogError ? <InlineNotice title="发布目录没有读取" tone="danger">{catalogError}</InlineNotice> : null}
          {catalog && catalog.length ? (
            <div className="team-shared-resources__catalog">
              {catalog.map((entry) => (
                <CatalogRow busy={busy === `publish:${entry.packageId}:${entry.version ?? ''}`} entry={entry} key={`${entry.packageId}:${entry.version ?? 'review'}`} onPublish={() => void publish(entry)} />
              ))}
            </div>
          ) : catalog && !catalog.length ? <p className="team-shared-resources__empty">当前没有可供管理员发布的 Package 版本。</p> : null}
        </section>
      ) : null}
    </div>
  );
}

function PublishedResourceRow({
  busy,
  canManage,
  onStatus,
  resource,
}: {
  busy: boolean;
  canManage: boolean;
  onStatus(status: 'published' | 'withdrawn'): void;
  resource: TeamPublishedResource;
}) {
  const { metadata } = resource;
  const published = resource.status === 'published';
  return (
    <article className="team-shared-resources__resource">
      <div className="team-shared-resources__resource-main">
        <div className="team-shared-resources__resource-heading">
          <strong>{metadata.displayName}</strong>
          <StatusBadge label={`v${resource.version}`} tone="info" />
          <StatusBadge label={published ? '已发布' : '已停止分发'} tone={published ? 'success' : 'neutral'} />
        </div>
        <p>{metadata.description || '没有附加说明。'}</p>
        <small>{metadata.publisher || '未知发布者'} · {metadata.source.label} · {metadata.permissions.length ? `权限 ${metadata.permissions.length} 项` : '无额外权限说明'}</small>
      </div>
      {canManage ? (
        <Button
          disabled={busy}
          leadingIcon={published ? <Ban size={13} /> : <RotateCcw size={13} />}
          loading={busy}
          onClick={() => onStatus(published ? 'withdrawn' : 'published')}
          size="small"
          variant="quiet"
        >{published ? '停止分发' : '恢复分发'}</Button>
      ) : null}
    </article>
  );
}

function CatalogRow({
  busy,
  entry,
  onPublish,
}: {
  busy: boolean;
  entry: TeamResourceCatalogEntry;
  onPublish(): void;
}) {
  const installable = entry.installable && Boolean(entry.version);
  return (
    <article className="team-shared-resources__catalog-row">
      <div>
        <div className="team-shared-resources__resource-heading">
          <strong>{entry.displayName}</strong>
          <StatusBadge label={entry.version ? `v${entry.version}` : '版本待审阅'} tone={entry.version ? 'info' : 'neutral'} />
        </div>
        <p>{entry.description || '没有附加说明。'}</p>
        <small>{entry.publisher || '未知发布者'} · {entry.source.label} · {installable ? '可发布' : '仅供审阅'}</small>
      </div>
      {installable ? <Button disabled={busy} loading={busy} leadingIcon={<Upload size={13} />} onClick={onPublish} size="small" variant="primary">发布{entry.displayName} v{entry.version}</Button> : <span className="team-shared-resources__review-only">仅供审阅</span>}
    </article>
  );
}

function mergeResources(primary: TeamPublishedResource[], selectionItems: TeamPublishedResource[]): TeamPublishedResource[] {
  const byId = new Map<string, TeamPublishedResource>();
  for (const resource of primary) byId.set(resource.publicationId, resource);
  for (const resource of selectionItems) if (!byId.has(resource.publicationId)) byId.set(resource.publicationId, resource);
  return [...byId.values()];
}

function groupResources(resources: TeamPublishedResource[]): ResourceGroup[] {
  const groups = new Map<string, ResourceGroup>();
  for (const resource of resources) {
    const displayName = resource.metadata.displayName || resource.packageId;
    const group = groups.get(resource.packageId) ?? { packageId: resource.packageId, displayName, resources: [] };
    group.resources.push(resource);
    groups.set(resource.packageId, group);
  }
  return [...groups.values()]
    .map((group) => ({
      ...group,
      resources: [...group.resources].sort((left, right) => compareVersions(right.version, left.version)),
    }))
    .sort((left, right) => left.displayName.localeCompare(right.displayName, 'zh-CN'));
}

function compareVersions(left: string, right: string): number {
  const a = left.split('.').map((part) => Number.parseInt(part, 10));
  const b = right.split('.').map((part) => Number.parseInt(part, 10));
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    const delta = (a[index] ?? 0) - (b[index] ?? 0);
    if (delta) return delta;
  }
  return left.localeCompare(right);
}

function sameIds(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function unique(values: readonly string[]): string[] {
  return [...new Set(values)];
}

function readableError(reason: unknown, fallback: string): string {
  if (reason instanceof TeamApiError && reason.status === 401) return '登录状态已失效，请重新登录。';
  return teamApiErrorMessage(reason, fallback);
}
