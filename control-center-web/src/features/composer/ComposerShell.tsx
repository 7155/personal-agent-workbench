import { Paperclip, X } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import { useOptionalControlTransport } from '@/app/control-transport';
import {
  composerAttachmentBadge,
  composerAttachmentKind,
} from '@/contracts/attachment-policy';
import { managedAgentMediaContentPath } from '@/platform/transport';
import './composer-enhancements.css';

/**
 * UR-042: the one Composer skeleton Session and Room both render.
 *
 * The shell owns the shared DOM order — banner, attachment chips, textarea,
 * then a toolbar split into left context controls and right actions — and
 * emits the canonical `agent-composer` class family for every internal node,
 * so both dialogs resolve to the same stylesheet rules for height, focus
 * state, attachment area and send position. Surface-specific behaviour
 * (Session runtime pickers, Room mention menu) plugs into the slots without
 * forking the skeleton.
 */

export interface ComposerShellAttachment {
  id: string;
  name: string;
  mimeType: string;
  byteSize?: number;
  sha256?: string;
  /** Browser-only bytes for an instant thumbnail right after paste/import. */
  previewFile?: File;
  /** Managed owner binding; enables server thumbnails for image receipts. */
  sessionId?: string;
  roomId?: string;
  description?: string;
}

export function ComposerShell({
  surface,
  className,
  busy,
  jumpLatest,
  banner,
  attachments,
  attachmentsLabel = '待发送附件',
  onRemoveAttachment,
  textarea,
  editorAction,
  expanded,
  controls,
  actions,
  onSurfacePress,
}: {
  surface: 'session' | 'room';
  className?: string;
  busy?: boolean;
  jumpLatest?: boolean;
  banner?: ReactNode;
  attachments: readonly ComposerShellAttachment[];
  attachmentsLabel?: string;
  onRemoveAttachment: (id: string) => void;
  textarea: ReactNode;
  editorAction?: ReactNode;
  expanded?: boolean;
  controls: ReactNode;
  actions: ReactNode;
  onSurfacePress?: () => void;
}) {
  return (
    <div
      className={['agent-composer', 'paw-unified-composer', className].filter(Boolean).join(' ')}
      data-surface={surface}
      data-busy={busy || undefined}
      data-jump-latest={jumpLatest || undefined}
      data-expanded={expanded || undefined}
      onMouseDown={(event) => {
        // The dock is taller than its text line; clicks landing on chrome
        // rather than a real control put the caret back into the message.
        if (event.button !== 0 || !onSurfacePress) return;
        const target = event.target as HTMLElement;
        if (target.closest('button, a, input, textarea, select, [role="radiogroup"], [contenteditable]')) return;
        event.preventDefault();
        onSurfacePress();
      }}
    >
      {busy ? <span aria-hidden="true" className="agent-composer__busy-frame"><i /></span> : null}
      {banner}
      {attachments.length ? (
        <div className="agent-composer__attachments" aria-label={attachmentsLabel} role="list">
          {attachments.map((attachment) => (
            <span
              className="agent-composer__attachment-chip"
              data-attachment-kind={composerAttachmentKind(attachment.mimeType)}
              key={attachment.id}
              role="listitem"
            >
              <ComposerAttachmentPreview attachment={attachment} />
              {attachment.description ? <span className="composer-attachment__copy"><b title={attachment.name}>{attachment.name}</b><small title={attachment.description}>{attachment.description}</small></span> : <b title={attachment.name}>{attachment.name}</b>}
              <button
                type="button"
                aria-label={`移除 ${attachment.name}`}
                onClick={() => onRemoveAttachment(attachment.id)}
              ><X size={12} /></button>
            </span>
          ))}
        </div>
      ) : null}
      <div className="agent-composer__editor">{textarea}{editorAction}</div>
      <div className="agent-composer__toolbar">
        <div className="agent-composer__controls">{controls}</div>
        <div className="agent-composer__actions">{actions}</div>
      </div>
    </div>
  );
}

/**
 * Chip preview: images get a thumbnail (local paste bytes first, managed
 * receipt second); every other file shows its type as a compact badge instead
 * of a broken image frame.
 */
export function ComposerAttachmentPreview({ attachment }: { attachment: ComposerShellAttachment }) {
  const transport = useOptionalControlTransport();
  const [localUrl, setLocalUrl] = useState('');
  const [failed, setFailed] = useState(false);
  const isImage = composerAttachmentKind(attachment.mimeType) === 'image';
  const previewFile = isImage ? attachment.previewFile : undefined;

  const managedPath = isImage && attachment.sha256
    ? managedAttachmentContentPath(attachment)
    : null;
  const managedUrl = managedPath
    ? transport?.agentMediaContentUrl?.(managedPath) ?? managedPath
    : null;

  useEffect(() => {
    setFailed(false);
    if (!previewFile || typeof URL.createObjectURL !== 'function') {
      setLocalUrl('');
      return undefined;
    }
    const nextUrl = URL.createObjectURL(previewFile);
    setLocalUrl(nextUrl);
    return () => URL.revokeObjectURL(nextUrl);
  }, [previewFile]);

  if (!isImage) {
    const badge = composerAttachmentBadge(attachment.name, attachment.mimeType);
    return badge
      ? <i aria-hidden="true" className="agent-composer__attachment-badge">{badge}</i>
      : <Paperclip aria-hidden="true" size={16} />;
  }
  const previewUrl = previewFile ? localUrl : managedUrl;
  if (!previewUrl || failed) return <Paperclip aria-hidden="true" size={16} />;
  return (
    <img
      alt=""
      draggable={false}
      src={previewUrl}
      onError={() => setFailed(true)}
    />
  );
}

function managedAttachmentContentPath(attachment: ComposerShellAttachment): string | null {
  const owner = attachment.roomId
    ? { key: 'roomId', value: attachment.roomId }
    : attachment.sessionId
      ? { key: 'sessionId', value: attachment.sessionId }
      : null;
  if (!owner) return null;
  const candidate = `/api/agent/media/${encodeURIComponent(attachment.id)}/content?${owner.key}=${encodeURIComponent(owner.value)}`;
  return managedAgentMediaContentPath(candidate);
}
