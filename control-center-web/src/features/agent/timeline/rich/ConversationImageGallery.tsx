import type { UiAgentBlock } from '@/contracts/ui-events';
import { useOptionalControlTransport } from '@/app/control-transport';
import { managedAgentMediaContentPath } from '@/platform/transport';
import { usePawOsDesktop } from '@/features/paw-os/surface-context';
import { ImageGallery } from '@/features/conversation-ui/media/ImageGallery';
import { positiveDimension, type GalleryImage } from '@/features/conversation-ui/media/image-gallery-model';
import { AgentFileBlock } from '../../file-preview/AgentFileBlock';

/** Only the existing public block/receipt reaches the browser, never raw Pi base64 or extension code. */
export function ConversationImageGallery({ blocks, sessionId = '' }: { blocks: readonly UiAgentBlock[]; sessionId?: string }) {
  const transport = useOptionalControlTransport(); const desktop = usePawOsDesktop();
  const images = blocks.map((block): GalleryImage => {
    const data = block.data; const rawPath = managedAgentMediaContentPath(string(data.receiptUrl));
    let path = rawPath;
    if (path && sessionId) {
      const owner = new URL(path, 'http://rag-ime.local').searchParams.get('sessionId');
      if ((owner && owner !== sessionId) || (string(data.sessionId) && data.sessionId !== sessionId)) path = null;
    }
    const name = string(data.fileName ?? data.name ?? data.alt) || '对话图片';
    return { id: block.id, name, source: path ? transport?.agentMediaContentUrl?.(path) ?? path : '',
      alt: string(data.alt) || name, caption: string(data.caption),
      width: positiveDimension(data.width ?? data.pixelWidth), height: positiveDimension(data.height ?? data.pixelHeight),
      mimeType: string(data.mimeType), byteSize: nonnegative(data.byteSize),
      // These labels are optional metadata; never infer a generator from a file extension or caption.
      origin: string(data.origin), originTool: string(data.originTool), receipt: path || undefined };
  });
  const files = blocks.filter(block => block.type === 'file');
  return <>
    <ImageGallery items={images} {...(desktop ? { onOpenOriginal: (image: GalleryImage) => desktop.openWindow({ appId: 'agent', target: {
      kind: 'result', id: `image:${sessionId}:${image.id}`, title: image.name, resultKind: 'image', source: image.source,
      subtitle: image.caption || '来自当前消息的图片回执',
    } }) } : {})} />
    {files.length ? <details className="paw-conversation-image-files"><summary>源文件与文件操作 · {files.length}</summary><div>{files.map(block => <AgentFileBlock key={block.id} data={block.data} sessionId={sessionId} />)}</div></details> : null}
  </>;
}
function string(value: unknown): string { return typeof value === 'string' ? value : ''; }
function nonnegative(value: unknown): number | undefined { return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : undefined; }
