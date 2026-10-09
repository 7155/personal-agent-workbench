import {
  ChevronDown,
  ChevronRight,
  ExternalLink,
  File,
  FileCode2,
  FileDiff,
  FileImage,
  FileText,
  FileSpreadsheet,
  FileArchive,
  FileAudio,
  Film,
  Presentation,
  Globe2,
  LoaderCircle,
  RefreshCw,
  TriangleAlert,
} from 'lucide-react';
import { useEffect, useLayoutEffect, useRef, type ReactNode } from 'react';
import { useOptionalControlTransport } from '@/app/control-transport';
import { Button, IconButton } from '@/components/primitives';
import { AgentHtmlReportCard } from './AgentHtmlReportCard';
import { CopyTextButton } from './CopyTextButton';
import { filePreviewRequestFromBlock, fileSizeLabel, isHtmlReport } from './file-descriptor';
import { FilePreviewRenderer } from './renderer-registry';
import { useFilePreviewStore } from './file-preview-store';
import './file-preview.css';
import { richFileKind, RICH_FILE_LABELS, type RichFileKind } from '../timeline/rich/rich-data';
import '../timeline/rich/rich-conversation.css';

export function AgentFileBlock({ data, sessionId = '' }: { data: Record<string, unknown>; sessionId?: string }) {
  const transport = useOptionalControlTransport();
  const openPreview = useFilePreviewStore((state) => state.openPreview);
  const closePreview = useFilePreviewStore((state) => state.close);
  const retryPreview = useFilePreviewStore((state) => state.retry);
  const previewOpen = useFilePreviewStore((state) => state.open);
  const presentation = useFilePreviewStore((state) => state.presentation);
  const activeRequest = useFilePreviewStore((state) => state.request);
  const status = useFilePreviewStore((state) => state.status);
  const preview = useFilePreviewStore((state) => state.preview);
  const error = useFilePreviewStore((state) => state.error);
  const request = filePreviewRequestFromBlock(data, sessionId);
  const fileName = request?.fileNameHint || string(data.fileName ?? data.name ?? data.title) || '文件产物';
  const meta = [RICH_FILE_LABELS[richFileKind(fileName, request?.mimeTypeHint || string(data.mimeType))], fileSizeLabel(request?.byteSizeHint ?? 0)].filter(Boolean).join(' · ');
  const available = Boolean(request && transport);
  const expanded = Boolean(
    request
    && previewOpen
    && presentation === 'inline'
    && samePreviewRequest(request, activeRequest),
  );
  const regionId = request ? `agent-file-preview-${request.mediaId}` : undefined;
  const regionRef = useRef<HTMLElement>(null);
  const revealRequested = useRef(false);

  useLayoutEffect(() => {
    if (!expanded) { revealRequested.current = false; return undefined; }
    const region = regionRef.current;
    const reader = region?.closest<HTMLElement>('[data-virtuoso-scroller="true"]');
    const row = region?.parentElement?.querySelector<HTMLElement>('.agent-file-block');
    if (!region || !reader) return undefined;
    // The workspace owns dock clearance. Read its native scroll-padding,
    // instead of assuming a composer height or changing the saved turn anchor.
    const resize = () => {
      const box = reader.getBoundingClientRect();
      const style = getComputedStyle(reader);
      const pixels = (value: string) => value === 'auto' ? 0
        : /^\d+(?:\.\d+)?px$/u.test(value) ? Number.parseFloat(value) : NaN;
      const padding = pixels(style.scrollPaddingTop) + pixels(style.scrollPaddingBottom);
      if (!Number.isFinite(padding)) return;
      const available = Math.max(0, Math.min(box.bottom, window.innerHeight) - Math.max(0, box.top) - padding - (row?.getBoundingClientRect().height ?? 0));
      region.style.setProperty('--agent-file-preview-available-height', `${available}px`);
    };
    const cancelReveal = () => { revealRequested.current = false; };
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(reader);
    // useComposerClearance publishes changes directly to this existing host.
    const host = reader.closest('.paw-session-workspace__primary');
    const dockChanges = new MutationObserver(resize);
    if (host) dockChanges.observe(host, { attributes: true, attributeFilter: ['style'] });
    for (const name of ['wheel', 'touchmove', 'pointerdown', 'keydown']) reader.addEventListener(name, cancelReveal, { passive: true });
    return () => {
      observer.disconnect(); dockChanges.disconnect();
      for (const name of ['wheel', 'touchmove', 'pointerdown', 'keydown']) reader.removeEventListener(name, cancelReveal);
    };
  }, [expanded]);

  useEffect(() => {
    if (!expanded || !revealRequested.current || !['ready', 'error'].includes(status)) return undefined;
    const region = regionRef.current;
    if (!region?.closest('[data-virtuoso-scroller="true"]')) return undefined;
    const frame = requestAnimationFrame(() => {
      if (!revealRequested.current || !region.isConnected) return;
      revealRequested.current = false;
      // Reveal this original row and its region together, not the latest turn.
      // Native scroll-padding reserves the dock and keeps row focus visible.
      region.parentElement?.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'auto' });
    });
    return () => cancelAnimationFrame(frame);
  }, [expanded, status]);

  if (request && isHtmlReport(fileName, request.mimeTypeHint)) {
    return <AgentHtmlReportCard fileName={fileName} request={request} transport={transport ?? null} />;
  }

  return (
    <div className="agent-file-block-shell" data-expanded={expanded || undefined}>
      <button
        aria-controls={available ? regionId : undefined}
        aria-expanded={available ? expanded : undefined}
        aria-label={available ? `${expanded ? '收起' : '展开'} ${fileName}` : `${fileName} 的预览回执不可用`}
        className="agent-file-block"
        data-disabled={!available || undefined}
        data-kind={fileKind(fileName, request?.mimeTypeHint ?? '')}
        disabled={!available}
        onClick={() => {
          if (!request || !transport) return;
          if (expanded) closePreview();
          else { revealRequested.current = true; openPreview(request, transport, 'inline'); }
        }}
        type="button"
      >
        <span className="agent-file-block__icon">{fileIcon(fileName, request?.mimeTypeHint ?? '')}</span>
        <span><strong>{fileName}</strong><small>{meta || '受控文件'}</small></span>
        <span aria-hidden="true" className="agent-file-block__open">
          {expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
          {expanded ? '收起' : '展开'}
        </span>
      </button>
      {expanded ? (
        <section
          aria-label={`${fileName} 内联预览`}
          className="agent-file-preview-inline"
          data-status={status}
          id={regionId}
          ref={regionRef}
        >
          <header>
            <span>{inlinePreviewCaption(status, preview?.truncated ?? false, preview?.content)}</span>
            <span className="agent-file-preview-inline__actions">
              {status === 'ready' && typeof preview?.content === 'string' && preview.content ? (
                <CopyTextButton label={`${fileName} 内容`} value={preview.content} />
              ) : null}
              {preview?.descriptor ? (
                <IconButton
                  icon={<ExternalLink size={15} />}
                  label={`打开原文件 ${fileName}`}
                  onClick={() => window.open(preview.descriptor.contentUrl, '_blank', 'noopener,noreferrer')}
                  size="small"
                  tooltip
                />
              ) : null}
            </span>
          </header>
          <div aria-label={`${fileName} 预览内容`} className="agent-file-preview-inline__body" role="group" tabIndex={0} data-preview-kind={status === 'ready' ? preview?.descriptor.previewKind : undefined}>
            {status === 'loading' ? <div className="agent-file-preview-inline__state"><LoaderCircle size={19} /><span>正在读取文件</span></div> : null}
            {status === 'error' ? (
              <div className="agent-file-preview-inline__state" role="alert">
                <TriangleAlert size={19} />
                <span>{error}</span>
                <Button disabled={!transport} leadingIcon={<RefreshCw size={15} />} onClick={() => transport && retryPreview(transport)} size="small" variant="quiet">重试</Button>
              </div>
            ) : null}
            {status === 'ready' && preview ? <FilePreviewRenderer preview={preview} /> : null}
          </div>
        </section>
      ) : null}
    </div>
  );
}

function samePreviewRequest(
  left: ReturnType<typeof filePreviewRequestFromBlock>,
  right: ReturnType<typeof filePreviewRequestFromBlock>,
): boolean {
  return Boolean(
    left
    && right
    && left.mediaId === right.mediaId
    && left.sessionId === right.sessionId
    && left.expectedSha256 === right.expectedSha256,
  );
}

function inlinePreviewCaption(
  status: string,
  truncated: boolean,
  content: string | undefined | null,
): string {
  if (status === 'loading') return '正在读取文件';
  const scope = truncated ? '显示前 512 KB' : '文件内容';
  if (typeof content === 'string' && content) {
    return `${scope} · ${content.split('\n').length.toLocaleString('zh-CN')} 行`;
  }
  return scope;
}

function fileKind(fileName: string, mimeType: string): RichFileKind { return richFileKind(fileName, mimeType); }

function fileIcon(fileName: string, mimeType: string): ReactNode {
  const kind = richFileKind(fileName, mimeType);
  if (kind === 'image') return <FileImage size={18} />;
  if (kind === 'document') return <FileText size={18} />;
  if (kind === 'sheet') return <FileSpreadsheet size={18} />;
  if (kind === 'presentation') return <Presentation size={18} />;
  if (kind === 'archive') return <FileArchive size={18} />;
  if (kind === 'audio') return <FileAudio size={18} />;
  if (kind === 'video') return <Film size={18} />;
  if (kind === 'diff') return <FileDiff size={18} />;
  if (/\.(html|htm)$/iu.test(fileName)) return <Globe2 size={18} />;
  if (kind === 'code') return <FileCode2 size={18} />;
  return <File size={18} />;
}

function string(value: unknown): string {
  return typeof value === 'string' ? value : '';
}
