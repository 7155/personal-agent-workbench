import { ExternalLink, Image as ImageIcon, Maximize2, Minus, Plus, RefreshCw, RotateCcw } from 'lucide-react';
import { useState } from 'react';
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from '@/components/primitives';
import { useOptionalControlTransport } from '@/app/control-transport';
import { managedAgentMediaContentPath } from '@/platform/transport';
import './rich-conversation.css';

/** The caller supplies a managed URL, not arbitrary model-authored src. */
export function RichImage({ source, alt = '对话图片', caption = '', width, height, onOpenExternal }: {
  source: string; alt?: string; caption?: string; width?: number; height?: number; onOpenExternal?: () => void;
}) {
  const [failedSource, setFailedSource] = useState('');
  const [open, setOpen] = useState(false);
  const [zoom, setZoom] = useState(1);
  const failed = failedSource === source;
  return <Dialog open={open} onOpenChange={setOpen}><span className="paw-rich-image">
    {failed ? <span className="paw-rich-media-failure" role="status"><ImageIcon size={22} aria-hidden /><strong>图片暂时无法读取</strong>
      <span>回执仍保留，未替换成示例图片。</span><button type="button" className="paw-rich-text-action" onClick={() => setFailedSource('')}><RefreshCw size={14} />重试读取</button>
    </span> : <DialogTrigger asChild><button type="button" className="paw-rich-image__button" aria-label={`放大图片：${alt}`} onClick={() => setZoom(1)}>
      <img src={source} alt={alt} loading="lazy" decoding="async" width={width} height={height} onError={() => setFailedSource(source)} referrerPolicy="no-referrer" />
      <span className="paw-rich-image__zoom"><Maximize2 size={15} aria-hidden /></span>
    </button></DialogTrigger>}
    <span className="paw-rich-image__caption"><span>{caption || alt}</span><small>{width && height ? `${width} × ${height}` : '图片'}</small>
      {onOpenExternal ? <button className="paw-rich-action" type="button" onClick={onOpenExternal} aria-label="在独立窗口打开图片"><ExternalLink size={14} aria-hidden /></button> : null}
    </span>
      <DialogContent className="paw-rich-lightbox">
        <DialogTitle>{alt}</DialogTitle><DialogDescription>{caption || '受控图片预览。使用加减键缩放，0 恢复，Escape 关闭。'}</DialogDescription>
        <div className="paw-rich-lightbox__tools" role="group" aria-label="图片缩放">
          <button type="button" className="paw-rich-action" aria-label="缩小图片" disabled={zoom <= .5} onClick={() => setZoom(v => Math.max(.5, v - .25))}><Minus size={16} /></button>
          <output>{Math.round(zoom * 100)}%</output>
          <button type="button" className="paw-rich-action" aria-label="放大图片" disabled={zoom >= 3} onClick={() => setZoom(v => Math.min(3, v + .25))}><Plus size={16} /></button>
          <button type="button" className="paw-rich-action" aria-label="恢复图片缩放" onClick={() => setZoom(1)}><RotateCcw size={14} /></button>
        </div>
        <div className="paw-rich-lightbox__viewport" tabIndex={0} role="region" aria-label="可滚动图片"
          onKeyDown={event => {
            if (event.key === '+' || event.key === '=') { event.preventDefault(); setZoom(v => Math.min(3, v + .25)); }
            else if (event.key === '-') { event.preventDefault(); setZoom(v => Math.max(.5, v - .25)); }
            else if (event.key === '0') { event.preventDefault(); setZoom(1); }
          }}><img src={source} alt={alt} style={{ width: `${zoom * 100}%`, maxWidth: 'none' }} referrerPolicy="no-referrer" /></div>
      </DialogContent>
  </span></Dialog>;
}

/** Markdown images must cross exactly the same managed-receipt boundary as attachments. */
export function MarkdownImage({ src, alt }: { src?: string; alt?: string }) {
  const transport = useOptionalControlTransport();
  const path = managedAgentMediaContentPath(src ?? '');
  const source = path ? transport?.agentMediaContentUrl?.(path) ?? path : '';
  if (source) return <RichImage key={source} source={source} alt={alt || '对话图片'} />;
  let external = '';
  try { const url = new URL(src ?? ''); if (url.protocol === 'https:' || url.protocol === 'http:') external = url.href; } catch { /* A relative path without a receipt is not fetched. */ }
  return <span className="paw-rich-image-reference"><ImageIcon size={17} aria-hidden /><span><strong>{alt || '图片引用'}</strong><small>没有受控回执，未自动加载外部图片</small></span>
    {external ? <a href={external} target="_blank" rel="noopener noreferrer" aria-label="在外部打开图片来源">查看来源<ExternalLink size={13} aria-hidden /></a> : null}
  </span>;
}
