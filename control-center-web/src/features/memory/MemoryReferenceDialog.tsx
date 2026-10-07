import {
  ArrowLeft,
  ChevronRight,
  EyeOff,
  GitBranch,
  LoaderCircle,
  ShieldAlert,
} from 'lucide-react';
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Disclosure,
} from '@/components/primitives';
import {
  InlineNotice,
  StatusBadge,
  publicErrorText,
} from '@/features/overview/management-ui';
import { useMotionActivity } from '@/design/motion';
import { useMemoryReference, type MemoryReferenceKind } from './api';
import { publicMemoryOwnerLabel, publicMemoryText } from './public-copy';
import type { MemoryReferenceV1 } from '@/contracts/generated/memory-reference.v1';

export interface MemoryReferenceSelection {
  kind: MemoryReferenceKind;
  referenceId: string;
  label?: string;
}

interface MemoryReferenceDialogProps extends MemoryReferenceSelection {
  onOpenChange: (open: boolean) => void;
}

const maximumReferenceDepth = 6;

export function MemoryReferenceDialog({
  kind,
  label = '',
  onOpenChange,
  referenceId,
}: MemoryReferenceDialogProps) {
  const root = useMemo<MemoryReferenceSelection>(
    () => ({ kind, referenceId, label }),
    [kind, label, referenceId],
  );
  const motionActive = useMotionActivity();
  const lastReadError = useRef<{ key: string; error: unknown } | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const viewRef = useRef<HTMLDivElement>(null);
  const scrollPositions = useRef(new Map<string, number>());
  const childControls = useRef(new Map<string, HTMLButtonElement>());
  const navigation = useRef<{ key: string; childKey?: string } | null>(null);
  const [storedStack, setStack] = useState<MemoryReferenceSelection[]>(referenceId ? [root] : []);
  const rootKey = referenceKey(root);
  const previousRootKey = useRef(rootKey);
  const stack = storedStack[0] && referenceKey(storedStack[0]) === rootKey ? storedStack : referenceId ? [root] : [];

  useLayoutEffect(() => {
    const rootChanged = previousRootKey.current !== rootKey;
    previousRootKey.current = rootKey;
    lastReadError.current = null;
    scrollPositions.current.clear();
    navigation.current = rootChanged ? { key: rootKey } : null;
    setStack(referenceId ? [root] : []);
  }, [referenceId, root, rootKey]);

  const current = stack[stack.length - 1] ?? root;
  const query = useMemoryReference(current.kind, current.referenceId, Boolean(referenceId));
  const payload = query.data;
  const refetch = query.refetch;
  const item = payload?.item;
  const source = payload?.source;
  const resolvedReference = item && source ? { item, source } : undefined;
  const references: MemoryReferenceSelection[] = (payload?.evidenceRefs ?? []).map((reference) => ({
    kind: reference.referenceKind,
    referenceId: reference.referenceId,
    ...(reference.label ? { label: reference.label } : {}),
  }));
  const sourceContext = item?.sourceContext;
  const sourceContextAvailable = item?.sourceContextAvailable === true && Boolean(sourceContext);
  const sourceContextRedacted = sourceContext?.redacted === true;
  const disposition = item?.status ?? '';
  const redacted = item?.sensitive === true;
  const forgotten = ['not_for_memory', 'expired', 'tombstoned', 'forgotten'].includes(disposition);
  const title = item?.title || item?.textPreview || item?.text || current.label || current.referenceId;
  const displayTitle = displayReferenceLabel(title, current.kind);
  const content = redacted ? '' : publicMemoryText(item?.detail || item?.summary || item?.text || '');
  const heading = content && content.trim() === displayTitle.trim() ? referenceKindLabel(current.kind) : displayTitle;
  const currentKey = referenceKey(current);
  const visited = new Set(stack.map(referenceKey));
  // Keep only this exact reference's last real read error while its retry is
  // pending. Query still owns the request/data; changing roots cannot reuse it.
  const readError = query.error ?? (query.isPending && query.isFetching && lastReadError.current?.key === currentKey
    ? lastReadError.current.error : null);

  useEffect(() => {
    if (query.error) lastReadError.current = { key: currentKey, error: query.error };
    else if (!query.isPending && !query.isFetching) lastReadError.current = null;
  }, [currentKey, query.error, query.isPending, query.isFetching]);

  function openReference(next: MemoryReferenceSelection) {
    const nextKey = referenceKey(next);
    if (visited.has(nextKey) || stack.length >= maximumReferenceDepth) return;
    scrollPositions.current.set(currentKey, viewRef.current?.scrollTop ?? 0);
    navigation.current = { key: nextKey };
    setStack([...stack, next]);
  }

  function returnReference() {
    const previous = stack[stack.length - 2];
    if (!previous) return;
    scrollPositions.current.set(currentKey, viewRef.current?.scrollTop ?? 0);
    navigation.current = { key: referenceKey(previous), childKey: currentKey };
    setStack((items) => items.slice(0, -1));
  }

  useLayoutEffect(() => {
    const pending = navigation.current;
    if (!pending || pending.key !== currentKey || query.isPending) return;
    if (viewRef.current) viewRef.current.scrollTop = scrollPositions.current.get(currentKey) ?? 0;
    const focusTarget = pending.childKey ? childControls.current.get(pending.childKey) : headingRef.current;
    (focusTarget ?? headingRef.current)?.focus({ preventScroll: true });
    navigation.current = null;
  }, [currentKey, query.isPending, query.error, payload]);

  return (
    <Dialog open={Boolean(referenceId)} onOpenChange={onOpenChange}>
      <DialogContent className="memory-reference-dialog" data-motion-active={motionActive}
        onOpenAutoFocus={(event) => { event.preventDefault(); headingRef.current?.focus({ preventScroll: true }); }}>
        <DialogHeader>
          <DialogTitle ref={headingRef} tabIndex={-1} title={displayTitle}>{heading || '记忆来源'}</DialogTitle>
          <DialogDescription className="memory-reference-dialog__description">
            {referenceKindLabel(current.kind)} · 可追溯来源
          </DialogDescription>
        </DialogHeader>

        {query.isPending && !readError ? (
          <p aria-live="polite" className="memory-layer-loading" role="status"><LoaderCircle size={15} />正在读取引用详情</p>
        ) : null}
        {readError ? (
          <div className="memory-reference-dialog__feedback">
            <InlineNotice title={query.isPending ? '正在重新读取原引用' : '引用暂时无法读取'} tone={query.isPending ? 'info' : 'danger'}>
              {publicErrorText(readError, '引用可能已归档，或详情尚未准备好。')}
            </InlineNotice>
            <Button loading={query.isFetching} onClick={() => { navigation.current = { key: currentKey }; void refetch(); }} size="small" variant="quiet">
              重试读取
            </Button>
          </div>
        ) : null}
        {!query.isPending && !readError && !resolvedReference ? (
          <div className="memory-reference-dialog__feedback">
            <InlineNotice title="没有可显示的引用" tone="info">
              当前来源没有返回可安全显示的详情。它可能已归档，或不在当前控制中心的所属范围内。
            </InlineNotice>
            <Button onClick={() => void refetch()} size="small" variant="quiet">重新读取</Button>
          </div>
        ) : null}

        {!query.isPending && !readError && resolvedReference ? (
          <div className="memory-reference-view" data-reference-key={currentKey} ref={viewRef}>
            <div className="memory-reference-view__metadata">
            <nav className="memory-reference-path" aria-label="记忆来源路径">
              <ol>
                {stack.map((step, index) => (
                  <li data-current={index === stack.length - 1 || undefined} key={referenceKey(step)}>
                    <span>{referenceKindCode(step.kind)}</span>
                    {stack.length > 1 ? <small>{displayReferenceLabel(step.label, step.kind)}</small> : null}
                    {index < stack.length - 1 ? <ChevronRight aria-hidden="true" size={13} /> : null}
                  </li>
                ))}
              </ol>
            </nav>
              <StatusBadge label={referenceStatusLabel(disposition, current.kind)} tone={referenceStatusTone(disposition, current.kind)} />
              <time>{referenceTime(resolvedReference.item)}</time>
            </div>

            {redacted ? (
              <InlineNotice title="内容已脱敏" tone="warning">
                <span className="memory-reference-view__notice"><EyeOff size={14} />正文不会在控制中心显示；来源类别和处理状态仍会保留，方便核对。</span>
              </InlineNotice>
            ) : null}
            {!redacted && forgotten ? (
              <InlineNotice title="这条来源不再用于记忆联想" tone="info">
                这条来源已停止参与整理或联想；仍会保留最少的关联信息，避免后续记录失去来源。
              </InlineNotice>
            ) : null}
            {content ? <p className="memory-reference-view__content">{content}</p> : null}

            {current.kind === 'event' ? (
              <section className="memory-reference-view__source-context" aria-label="整理使用的输入上下文">
                <header>
                  <span><GitBranch size={15} /><strong>整理使用的输入上下文</strong></span>
                  <small>仅所属范围可读</small>
                </header>
                {!sourceContextAvailable ? (
                  <p>当前引用不在本控制中心的所属范围内，因此不返回输入上下文。</p>
                ) : sourceContextRedacted ? (
                  <InlineNotice title="输入上下文已脱敏" tone="warning">
                    上下文仅用于关联来源和整理内容，但正文不会显示。
                  </InlineNotice>
                ) : (
                  <>
                    <dl>
                      <ReferenceFact label="当时上下文" value={sourceContext?.recentContext || '空'} />
                      <ReferenceFact label="当时预编辑" value={sourceContext?.preedit || '空'} />
                    </dl>
                    <p>这些字段来自原始事件，只用于关联来源和整理内容；活动时间线只保留可稳定定位的信息。</p>
                  </>
                )}
              </section>
            ) : null}

            <section className="memory-reference-view__children" aria-label={referenceChildrenTitle(current.kind)}>
              <header><span><GitBranch size={15} /><strong>{referenceChildrenTitle(current.kind)}</strong></span><small>{references.length} 条</small></header>
              {references.length ? references.map((reference) => {
                const childKey = referenceKey(reference);
                const loop = visited.has(childKey);
                const depthLimited = stack.length >= maximumReferenceDepth;
                return (
                  <button
                    ref={(node) => { if (node) childControls.current.set(childKey, node); else childControls.current.delete(childKey); }}
                    disabled={loop || depthLimited}
                    key={childKey}
                    onClick={() => openReference(reference)}
                    type="button"
                  >
                    <span><small>{referenceKindLabel(reference.kind)}</small><strong>{displayReferenceLabel(reference.label, reference.kind)}</strong></span>
                    {loop ? <b>已在路径中</b> : depthLimited ? <b>已到最深层</b> : <ChevronRight size={15} />}
                  </button>
                );
              }) : (
                <p>这是当前来源的最末层记录，没有更深一层引用。</p>
              )}
            </section>

            <Disclosure className="memory-reference-view__advanced" summary="高级：引用详情">
              <dl className="memory-reference-view__facts">
                {displayTitle ? <ReferenceFact label="完整标题" value={displayTitle} /> : null}
                <ReferenceFact label="当前层" value={referenceKindLabel(current.kind)} />
                <ReferenceFact label="引用编号" value={current.referenceId} />
                <ReferenceFact label="来源类别" value={resolvedReference.source.sourceKind || resolvedReference.source.kind} />
                <ReferenceFact label="来源对象" value={resolvedReference.source.id} />
                <ReferenceFact
                  label="归属"
                  value={publicMemoryOwnerLabel(resolvedReference.item.ownerKind ?? '', resolvedReference.item.ownerId ?? '') || '未标注'}
                />
                {resolvedReference.item.ownerId ? <ReferenceFact label="内部归属编号" value={resolvedReference.item.ownerId} /> : null}
              </dl>
            </Disclosure>

            {stack.length >= maximumReferenceDepth ? (
              <p className="memory-reference-view__guard"><ShieldAlert size={14} />已达到 {maximumReferenceDepth} 层查看上限，避免异常引用链无限展开。</p>
            ) : null}
          </div>
        ) : null}

        <DialogFooter>
          {stack.length > 1 ? (
            <Button leadingIcon={<ArrowLeft size={14} />} onClick={returnReference} variant="quiet">
              返回 {referenceKindCode(stack[stack.length - 2]!.kind)}
            </Button>
          ) : null}
          <Button onClick={() => onOpenChange(false)}>关闭</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ReferenceFact({ label, value }: { label: string; value: string }) {
  return <div><dt>{label}</dt><dd>{value || '未标注'}</dd></div>;
}


function referenceTime(item: MemoryReferenceV1['item'] | undefined): string {
  const value = item?.updatedAtMs || item?.occurredAtMs || item?.createdAtMs;
  return value
    ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(value)
    : '未标注';
}

function referenceKindCode(kind: MemoryReferenceKind): string {
  return ({
    event: '来源',
    evidence: '来源',
    atom: '记忆',
    book: '主题',
    timeline: '活动',
    role_book_revision: '伙伴设定',
  } as const)[kind];
}

function displayReferenceLabel(value: string | undefined, kind: MemoryReferenceKind): string {
  const label = String(value ?? '').trim();
  if (!label || /^(?:event|evidence|atom|book|timeline|role[-_]book)(?::|$)/i.test(label)) {
    return referenceKindLabel(kind);
  }
  return publicMemoryText(label)
    .replace(/\bEvidence\b/gi, '来源')
    .replace(/\bAtom\b/gi, '记忆')
    .replace(/\bBook\b/gi, '主题')
    .replaceAll('证据', '来源');
}

function referenceKindLabel(kind: MemoryReferenceKind): string {
  return ({
    event: '原始来源',
    evidence: '来源记录',
    atom: '已整理记忆',
    book: '长期主题',
    timeline: '活动记录',
    role_book_revision: '伙伴记忆版本',
  } as const)[kind];
}

function referenceChildrenTitle(kind: MemoryReferenceKind): string {
  return ({
    book: '主题中的记忆',
    atom: '记忆的相关来源',
    evidence: '这条记录的原始来源',
    event: '与这条来源关联的记录',
    timeline: '活动使用的来源',
    role_book_revision: '伙伴记忆的相关来源',
  } as const)[kind];
}

function referenceStatusLabel(status: string, kind: MemoryReferenceKind): string {
  if (kind === 'evidence' && status === 'active') return '已引用';
  return ({
    active: '使用中',
    approved: '已确认',
    archived: '已归档',
    superseded: '历史版本',
    conflict: '有冲突',
    conflicted: '有冲突',
    tombstoned: '已移除',
    not_for_memory: '已遗忘',
    forgotten: '已遗忘',
    expired: '已过期',
  } as Record<string, string>)[status] ?? '可追溯';
}

function referenceStatusTone(status: string, kind: MemoryReferenceKind): 'success' | 'info' | 'danger' | 'neutral' {
  if (kind === 'evidence' && status === 'active') return 'info';
  if (status === 'active' || status === 'approved') return 'success';
  if (status === 'archived' || status === 'superseded' || status === 'not_for_memory' || status === 'forgotten' || status === 'expired') return 'info';
  if (status === 'tombstoned') return 'danger';
  return 'neutral';
}

function referenceKey(value: MemoryReferenceSelection): string {
  return `${value.kind}\u0000${value.referenceId}`;
}
