import { ExternalLink, FileAudio, Image as ImageIcon, PackageOpen, PanelTopOpen } from 'lucide-react';
import { useOptionalControlTransport } from '@/app/control-transport';
import { Disclosure, IconButton } from '@/components/primitives';
import { managedAgentMediaContentPath } from '@/platform/transport';
import { usePawOsDesktop } from '@/features/paw-os/surface-context';
import { AgentFileBlock } from '../file-preview/AgentFileBlock';
import { stickerAsset } from './PersonaAvatar';
import { BlockedMedia } from './StructuredRenderers';
import type { AgentBlockRenderProps } from './renderer-contract';
import { finiteNumber, text } from './renderer-values';
import { RichImage } from './rich/RichImage';
import { RichMediaPlayer } from './rich/RichMediaPlayer';
import './rich/rich-conversation.css';

export function ArtifactBlockRenderer({ block }: AgentBlockRenderProps) {
  const desktop = usePawOsDesktop(); const data = block.data;
  const href = safeArtifactLink(text(data.receiptUrl ?? data.href ?? data.url));
  const name = text(data.title ?? data.name ?? data.fileName) || '任务产物';
  return <section className="agent-rich-artifact paw-rich-artifact" data-tone="success" aria-label={name}>
    <span className="agent-insert-icon"><PackageOpen size={19} /></span><span><strong>{name}</strong><small>{text(data.summary) || fileMeta(data)}</small></span>
    {href ? <span className="agent-rich-artifact__actions">
      <IconButton label="打开产物回执" icon={<ExternalLink size={16} />} onClick={() => window.open(href, '_blank', 'noopener,noreferrer')} tooltip />
      {desktop ? <IconButton label="在独立窗口打开产物" icon={<PanelTopOpen size={16} />} onClick={() => desktop.openWindow({ appId: 'agent', target: { kind: 'result', id: resultId('artifact'), title: name, resultKind: 'artifact', source: href, subtitle: '来自当前 Agent 消息的受控文件回执' } })} tooltip /> : null}
    </span> : <small className="paw-rich-unavailable">打开回执不可用</small>}
  </section>;
}

export function CitationBlockRenderer({ block }: AgentBlockRenderProps) {
  const data = block.data; const href = safeLink(text(data.href ?? data.url));
  let host = ''; try { if (href?.startsWith('http')) host = new URL(href).hostname; } catch { /* No invented source. */ }
  const title = text(data.title ?? data.label) || '引用来源';
  const content = <><span className="agent-citation__index">{finiteNumber(data.index) || '•'}</span>
    <span><strong>{title}</strong><small>{text(data.source ?? data.domain) || host || '未提供来源名称'}</small></span>
    {href ? <ExternalLink size={14} aria-hidden /> : null}</>;
  return <section className="paw-rich-citation" aria-label={`引用：${title}`}>
    {href ? <a className="agent-citation" data-tone="info" href={href} rel="noopener noreferrer" target={href.startsWith('http') ? '_blank' : undefined}>{content}</a>
      : <div className="agent-citation" data-tone="info">{content}</div>}
    {text(data.excerpt) ? <details><summary>查看引用片段</summary><blockquote>{text(data.excerpt)}</blockquote></details> : null}
  </section>;
}

export function ImageBlockRenderer({ block }: AgentBlockRenderProps) {
  const desktop = usePawOsDesktop(); const transport = useOptionalControlTransport(); const data = block.data;
  const receiptPath = managedAgentMediaContentPath(text(data.receiptUrl));
  const source = receiptPath ? transport?.agentMediaContentUrl?.(receiptPath) ?? receiptPath : '';
  if (!source) return <BlockedMedia detail="这条消息没有可验证的附件回执。请重新上传图片后发送。" icon={<ImageIcon size={16} />} label="图片回执不可用" />;
  return <RichImage key={source} source={source} alt={text(data.alt) || '对话图片'} caption={text(data.caption)}
    width={imageDimension(data.width ?? data.pixelWidth) || undefined} height={imageDimension(data.height ?? data.pixelHeight) || undefined}
    {...(desktop ? { onOpenExternal: () => desktop.openWindow({ appId: 'agent', target: { kind: 'result', id: resultId('image'), title: text(data.alt) || '对话图片', resultKind: 'image', source, subtitle: text(data.caption) || '来自当前 Agent 消息的受控图片回执' } }) } : {})} />;
}

export function AudioBlockRenderer({ block }: AgentBlockRenderProps) {
  const desktop = usePawOsDesktop(); const transport = useOptionalControlTransport(); const data = block.data;
  const safe = safeMediaSource(text(data.receiptUrl ?? data.src ?? data.url), 'audio');
  const receiptPath = safe ? managedAgentMediaContentPath(safe) : null;
  const source = receiptPath ? transport?.agentMediaContentUrl?.(receiptPath) ?? receiptPath : safe;
  if (!source) return <BlockedMedia detail="这条消息没有可验证的音频回执。请重新上传音频后发送。" icon={<FileAudio size={16} />} label="音频回执不可用" />;
  const name = text(data.name ?? data.fileName) || '音频附件';
  return <div className="paw-rich-media-shell"><RichMediaPlayer key={source} source={source} name={name} transcript={text(data.transcript)} />
    {desktop ? <IconButton label="在独立窗口打开音频" icon={<PanelTopOpen size={16} />} onClick={() => desktop.openWindow({ appId: 'agent', target: { kind: 'result', id: resultId('audio'), title: name, resultKind: 'audio', source, subtitle: '来自当前 Agent 消息的受控音频回执' } })} tooltip /> : null}
  </div>;
}

/** Video uses the existing file contract plus its validated MIME/receipt, not a new event type. */
export function FileBlockRenderer({ block, sessionId }: AgentBlockRenderProps) {
  const transport = useOptionalControlTransport();
  const path = text(block.data.mimeType).startsWith('video/') ? managedAgentMediaContentPath(text(block.data.receiptUrl)) : null;
  const source = path ? transport?.agentMediaContentUrl?.(path) ?? path : '';
  return <>{source ? <RichMediaPlayer key={source} kind="video" source={source} name={text(block.data.fileName ?? block.data.name) || '视频附件'} transcript={text(block.data.transcript)} /> : null}
    <AgentFileBlock data={block.data} sessionId={sessionId} /></>;
}

export function StickerBlockRenderer({ block }: AgentBlockRenderProps) {
  const data = block.data; const source = stickerAsset(text(data.assetId ?? data.stickerId));
  if (!source) return <BlockedMedia detail="贴纸资源已失效。请从当前 Persona 的贴纸列表重新选择。" icon={<ImageIcon size={16} />} label="贴纸资产不可用" />;
  return <img className="agent-sticker-block" src={source} alt={text(data.alt) || 'Persona 贴纸'} loading="lazy" />;
}

export function UnknownBlockRenderer({ block }: AgentBlockRenderProps) {
  const label = text(block.rawType) || text(block.presentationKind) || 'unknown';
  return <Disclosure className="agent-unknown-block paw-rich-unknown" summary={`暂不支持的内容 · ${label}`}>
    <p>{text(block.summary) || '内容已安全保留，可以继续对话或在审计区查看原始记录。'}</p>
  </Disclosure>;
}

function safeLink(value: string | undefined): string | undefined {
  if (!value || value.includes('\\') || /[\u0000-\u001f]/u.test(value) || value.startsWith('//')) return undefined;
  if (value.startsWith('#/') || value.startsWith('/')) return value;
  try { const url = new URL(value); return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : undefined; } catch { return undefined; }
}
function safeMediaSource(value: string, kind: 'image' | 'audio' | 'file'): string | null {
  if (!value || value.includes('\\') || /[\u0000-\u001f]/u.test(value)) return null;
  if (value.startsWith('/companions/') && kind === 'image') return value;
  if (value.startsWith('/api/agent/') || value.startsWith('/media/') || value.startsWith('blob:')) return value;
  return null;
}
function safeArtifactLink(value: string): string | null {
  const managed = safeMediaSource(value, 'file'); if (managed) return managed;
  const external = safeLink(value); return external?.startsWith('https://') ? external : null;
}
function fileMeta(data: Record<string, unknown>): string {
  const size = finiteNumber(data.byteSize ?? data.size);
  const sizeText = size ? size >= 1_048_576 ? `${(size / 1_048_576).toFixed(1)} MB` : `${Math.ceil(size / 1_024)} KB` : '';
  return [text(data.mimeType ?? data.type), sizeText].filter(Boolean).join(' · ') || '受控文件回执';
}
function imageDimension(value: unknown): number { const n = finiteNumber(value); return n >= 1 && n <= 8_192 ? Math.round(n) : 0; }
function resultId(kind: string): string { return `${kind}-${typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`}`; }
