import { ExternalLink, Image as ImageIcon } from 'lucide-react';
import { useOptionalControlTransport } from '@/app/control-transport';
import { managedAgentMediaContentPath } from '@/platform/transport';
import { ImageGallery } from '@/features/conversation-ui/media/ImageGallery';
import './rich-conversation.css';

/** Existing API retained. The shared image reader now owns every zoom/lightbox interaction. */
export function RichImage({ source, alt = '对话图片', caption = '', width, height, onOpenExternal }: {
  source: string; alt?: string; caption?: string; width?: number; height?: number; onOpenExternal?: () => void;
}) {
  return <ImageGallery items={[{ id: 'inline-image', source, name: alt, alt, caption, width, height }]}
    {...(onOpenExternal ? { onOpenOriginal: () => onOpenExternal() } : {})} />;
}
export function MarkdownImage({ src, alt }: { src?: string; alt?: string }) {
  const transport = useOptionalControlTransport(); const path = managedAgentMediaContentPath(src ?? '');
  const source = path ? transport?.agentMediaContentUrl?.(path) ?? path : '';
  if (source) return <RichImage key={source} source={source} alt={alt || '对话图片'} />;
  let external = '';
  try { const url = new URL(src ?? ''); if (url.protocol === 'https:' || url.protocol === 'http:') external = url.href; } catch { /* No guessed local or managed path. */ }
  return <span className="paw-rich-image-reference"><ImageIcon size={17} aria-hidden /><span><strong>{alt || '图片引用'}</strong><small>没有受控回执，未自动加载外部图片</small></span>
    {external ? <a href={external} target="_blank" rel="noopener noreferrer" aria-label="在外部打开图片来源">查看来源<ExternalLink size={13} aria-hidden /></a> : null}
  </span>;
}
