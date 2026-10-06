import { ExternalLink, FileText, LoaderCircle, RefreshCw, TriangleAlert } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  IconButton,
} from '@/components/primitives';
import { CodePreview } from '../file-preview/CodePreview';
import { DiffPreview } from '../file-preview/DiffPreview';
import { MarkdownPreview } from '../file-preview/MarkdownPreview';
import { RichHtmlPreview } from '../file-preview/RichHtmlPreview';
import '../file-preview/file-preview.css';

interface WorkspaceFilePreviewDialogProps {
  sessionId: string;
  path: string;
  byteSize?: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

interface WorkspaceFilePreview {
  path: string;
  content: string;
  byteSize: number;
  truncated: boolean;
}

export function WorkspaceFilePreviewDialog({
  sessionId,
  path,
  byteSize = 0,
  open,
  onOpenChange,
}: WorkspaceFilePreviewDialogProps) {
  const transport = useControlTransport();
  const requestGenerationRef = useRef(0);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const [preview, setPreview] = useState<WorkspaceFilePreview>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const fileName = pathName(path) || '文件预览';
  const kind = previewKind(fileName);
  const language = fileLanguage(fileName);

  const load = useCallback(async () => {
    if (!open || !sessionId || !path) return;
    const generation = ++requestGenerationRef.current;
    setLoading(true);
    setError('');
    try {
      const response = await transport.request({
        pathId: 'agent.session.workspace.read',
        params: { sessionId },
        query: { path, offset: 0, limit: 65_536 },
      });
      if (generation !== requestGenerationRef.current) return;
      setPreview(workspacePreview(response, path));
    } catch (loadError) {
      if (generation !== requestGenerationRef.current) return;
      setPreview(undefined);
      setError(publicError(loadError));
    } finally {
      if (generation === requestGenerationRef.current) setLoading(false);
    }
  }, [open, path, sessionId, transport]);

  useEffect(() => {
    setPreview(undefined);
    if (open) void load();
    return () => { requestGenerationRef.current += 1; };
  }, [load, open, path]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="agent-file-preview-dialog agent-workspace-file-dialog"
        onOpenAutoFocus={event => { event.preventDefault(); titleRef.current?.focus({ preventScroll: true }); }}
      >
        <DialogHeader>
          <span className="agent-file-preview-dialog__icon"><FileText size={20} /></span>
          <DialogTitle ref={titleRef} tabIndex={-1}>{fileName}</DialogTitle>
          <DialogDescription>{[languageLabel(kind, language), formatBytes(preview?.byteSize ?? byteSize)].filter(Boolean).join(' · ')}</DialogDescription>
          {transport.revealPath ? (
            <IconButton
              className="agent-file-preview-dialog__download"
              icon={<ExternalLink size={16} />}
              label="在 Finder 中显示"
              onClick={() => void transport.revealPath?.(path)}
              tooltip
            />
          ) : null}
        </DialogHeader>
        {preview?.truncated ? <div className="agent-file-preview-dialog__notice">文件较大，当前显示前 64 KB。</div> : null}
        <div className="agent-file-preview-dialog__body agent-workspace-file-dialog__body" data-preview-kind={kind} data-status={loading ? 'loading' : error ? 'error' : 'ready'}>
          {loading ? <div className="agent-file-preview-dialog__state" role="status"><LoaderCircle size={20} /><span>正在读取文件</span></div> : null}
          {error ? (
            <div className="agent-file-preview-dialog__state" role="alert">
              <TriangleAlert size={20} />
              <span>{error}</span>
              <Button leadingIcon={<RefreshCw size={15} />} onClick={() => void load()} size="small" variant="quiet">重试</Button>
            </div>
          ) : null}
          {!loading && !error && preview ? renderPreview(kind, preview.content, fileName, language) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function renderPreview(kind: 'markdown' | 'html' | 'diff' | 'code', content: string, fileName: string, language: string) {
  if (kind === 'markdown') return <MarkdownPreview content={content} />;
  if (kind === 'html') return <RichHtmlPreview content={content} title={fileName} />;
  if (kind === 'diff') return <DiffPreview content={content} fileName={fileName} />;
  return <CodePreview content={content} fileName={fileName} language={language} />;
}

function workspacePreview(value: unknown, expectedPath: string): WorkspaceFilePreview {
  if (!isRecord(value) || value.path !== expectedPath || typeof value.content !== 'string') {
    throw new Error('文件服务返回了无法识别的数据。');
  }
  return {
    path: expectedPath,
    content: value.content,
    byteSize: finiteNumber(value.byteSize),
    truncated: value.truncated === true,
  };
}

function previewKind(fileName: string): 'markdown' | 'html' | 'diff' | 'code' {
  const extension = fileExtension(fileName);
  if (['md', 'mdx', 'markdown'].includes(extension)) return 'markdown';
  if (['html', 'htm'].includes(extension)) return 'html';
  if (['diff', 'patch'].includes(extension)) return 'diff';
  return 'code';
}

function fileLanguage(fileName: string): string {
  const extension = fileExtension(fileName);
  return ({
    c: 'c', cc: 'cpp', cpp: 'cpp', cs: 'csharp', css: 'css', go: 'go', h: 'c', hpp: 'cpp',
    html: 'html', htm: 'html', java: 'java', js: 'javascript', jsx: 'jsx', json: 'json',
    kt: 'kotlin', kts: 'kotlin', md: 'markdown', py: 'python', rs: 'rust', sh: 'shellscript',
    sql: 'sql', swift: 'swift', toml: 'toml', ts: 'typescript', tsx: 'tsx', vue: 'vue',
    xml: 'xml', yaml: 'yaml', yml: 'yaml', zsh: 'shellscript',
  } as Record<string, string>)[extension] ?? 'text';
}

function languageLabel(kind: string, language: string): string {
  if (kind === 'markdown') return 'Markdown';
  if (kind === 'html') return 'HTML 交互预览';
  if (kind === 'diff') return 'Diff';
  return language === 'text' ? '文本' : language;
}

function fileExtension(value: string): string {
  const match = /\.([^.]+)$/u.exec(value);
  return match?.[1]?.toLowerCase() ?? '';
}

function pathName(path: string): string {
  return path.split('/').filter(Boolean).at(-1) ?? path;
}

function finiteNumber(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : 0;
}

function formatBytes(value: number): string {
  if (!value) return '';
  if (value < 1_024) return `${value} B`;
  if (value < 1_048_576) return `${Math.max(1, Math.round(value / 1_024))} KB`;
  return `${Math.max(1, Math.round(value / 1_048_576))} MB`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function publicError(error: unknown): string {
  return error instanceof Error && error.message.trim()
    ? error.message
    : '文件预览读取失败。';
}
