import { FileQuestion } from 'lucide-react';
import { useState, type ComponentType } from 'react';
import { useOptionalControlTransport } from '@/app/control-transport';
import type { AgentFilePreviewV1 } from '@/contracts/generated/agent-file-preview.v1';
import { managedAgentMediaContentPath } from '@/platform/transport';
import { CodePreview } from './CodePreview';
import { DiffPreview } from './DiffPreview';
import { HtmlArtifactPreview } from './HtmlArtifactPreview';
import { MarkdownPreview } from './MarkdownPreview';

export type FilePreviewKind = AgentFilePreviewV1['descriptor']['previewKind'];
export type FilePreviewRenderer = ComponentType<{ preview: AgentFilePreviewV1; externalToolbar?: boolean }>;

const renderers = new Map<FilePreviewKind, FilePreviewRenderer>();

export function registerFilePreviewRenderer(kind: FilePreviewKind, renderer: FilePreviewRenderer): void {
  renderers.set(kind, renderer);
}

export function FilePreviewRenderer({ preview, externalToolbar = false }: { preview: AgentFilePreviewV1; externalToolbar?: boolean }) {
  const Renderer = renderers.get(preview.descriptor.previewKind) ?? UnsupportedPreview;
  return <Renderer preview={preview} externalToolbar={externalToolbar} />;
}

registerFilePreviewRenderer('markdown', ({ preview }) => <MarkdownPreview content={preview.content ?? ''} />);
registerFilePreviewRenderer('code', ({ preview, externalToolbar }) => <CodePreview content={preview.content ?? ''} fileName={preview.descriptor.fileName} language={preview.descriptor.language || 'text'} showHeader={!externalToolbar} />);
registerFilePreviewRenderer('diff', ({ preview, externalToolbar }) => <DiffPreview content={preview.content ?? ''} showCopy={!externalToolbar} />);
registerFilePreviewRenderer('html', HtmlArtifactPreview);
registerFilePreviewRenderer('image', ManagedImagePreview);
registerFilePreviewRenderer('unsupported', UnsupportedPreview);

function ManagedImagePreview({ preview }: { preview: AgentFilePreviewV1 }) {
  const transport = useOptionalControlTransport();
  const [failedSource, setFailedSource] = useState('');
  const receiptPath = managedAgentMediaContentPath(preview.descriptor.contentUrl);
  const source = receiptPath
    ? transport?.agentMediaContentUrl?.(receiptPath) ?? receiptPath
    : '';
  if (!source || failedSource === source) return <UnsupportedPreview preview={preview} />;
  return (
    <div className="agent-file-image-preview">
      <img
        alt={preview.descriptor.fileName}
        onError={() => setFailedSource(source)}
        src={source}
      />
    </div>
  );
}

function UnsupportedPreview({ preview }: { preview: AgentFilePreviewV1 }) {
  return (
    <div className="agent-file-preview__unsupported">
      <FileQuestion size={28} />
      <strong>暂不支持内嵌预览</strong>
      <small>{preview.descriptor.mimeType}</small>
    </div>
  );
}
