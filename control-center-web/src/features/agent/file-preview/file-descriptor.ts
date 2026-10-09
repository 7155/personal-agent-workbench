import type { AgentFileDescriptorV1 } from '@/contracts/generated/agent-file-descriptor.v1';

const MEDIA_ID_PATTERN = /^media_[A-Za-z0-9_-]{12,80}$/u;
const SESSION_ID_PATTERN = /^[A-Za-z0-9._:-]{1,240}$/u;
const SHA256_PATTERN = /^[0-9a-f]{64}$/u;

export interface FilePreviewRequest {
  mediaId: string;
  sessionId: string;
  expectedSha256: string;
  fileNameHint: string;
  mimeTypeHint: string;
  byteSizeHint: number;
}

export function filePreviewRequestFromBlock(
  data: Record<string, unknown>,
  messageSessionId = '',
): FilePreviewRequest | null {
  const mediaId = text(data.mediaId);
  if (!MEDIA_ID_PATTERN.test(mediaId)) return null;

  const receiptUrl = text(data.receiptUrl);
  const receiptSessionId = sessionIdFromManagedUrl(receiptUrl, mediaId);
  if (receiptUrl && !receiptSessionId) return null;
  const declaredSessionId = text(data.sessionId);
  const sessionId = messageSessionId || declaredSessionId || receiptSessionId;
  if (!SESSION_ID_PATTERN.test(sessionId)) return null;
  if (messageSessionId && declaredSessionId && messageSessionId !== declaredSessionId) return null;
  if (messageSessionId && receiptSessionId && messageSessionId !== receiptSessionId) return null;
  if (declaredSessionId && receiptSessionId && declaredSessionId !== receiptSessionId) return null;

  const rawSha256 = text(data.sha256).toLowerCase();
  if (rawSha256 && !SHA256_PATTERN.test(rawSha256)) return null;
  const byteSize = number(data.byteSize ?? data.size);
  return {
    mediaId,
    sessionId,
    expectedSha256: rawSha256,
    fileNameHint: text(data.fileName ?? data.name ?? data.title) || '文件产物',
    mimeTypeHint: text(data.mimeType ?? data.type),
    byteSizeHint: byteSize > 0 ? Math.round(byteSize) : 0,
  };
}

export function safeManagedContentUrl(
  value: string,
  descriptor: Pick<AgentFileDescriptorV1, 'mediaId' | 'sessionId'>,
): string | null {
  if (!value.startsWith('/api/agent/media/')) return null;
  try {
    const url = new URL(value, 'http://rag-ime.local');
    const match = /^\/api\/agent\/media\/([^/]+)\/content$/u.exec(url.pathname);
    if (!match || url.hash) return null;
    if (decodeURIComponent(match[1] ?? '') !== descriptor.mediaId) return null;
    const sessionIds = url.searchParams.getAll('sessionId');
    if (sessionIds.length !== 1 || sessionIds[0] !== descriptor.sessionId) return null;
    if ([...url.searchParams.keys()].some((key) => key !== 'sessionId')) return null;
    return `${url.pathname}?sessionId=${encodeURIComponent(descriptor.sessionId)}`;
  } catch {
    return null;
  }
}

/**
 * The managed content URL for a file we have a validated request for, built
 * locally so a result card can offer "open the original" without first loading
 * a preview. Deliberately routed back through safeManagedContentUrl rather than
 * trusted as constructed: the card and the dialog then accept exactly the same
 * shape of URL, and there is one place where that shape is decided.
 */
export function managedContentUrl(request: FilePreviewRequest): string | null {
  if (!MEDIA_ID_PATTERN.test(request.mediaId) || !SESSION_ID_PATTERN.test(request.sessionId)) return null;
  const candidate = `/api/agent/media/${encodeURIComponent(request.mediaId)}/content?sessionId=${encodeURIComponent(request.sessionId)}`;
  return safeManagedContentUrl(candidate, { mediaId: request.mediaId, sessionId: request.sessionId });
}

export function isHtmlReport(fileName: string, mimeType: string): boolean {
  return /^text\/html\b/iu.test(mimeType.trim()) || /\.html?$/iu.test(fileName.trim());
}

export function fileSizeLabel(byteSize: number): string {
  if (!Number.isFinite(byteSize) || byteSize <= 0) return '';
  if (byteSize < 1_024) return `${Math.round(byteSize)} B`;
  if (byteSize >= 1_048_576) return `${(byteSize / 1_048_576).toFixed(1)} MB`;
  return `${Math.max(1, Math.ceil(byteSize / 1_024))} KB`;
}

function sessionIdFromManagedUrl(value: string, expectedMediaId: string): string {
  if (!value.startsWith('/api/agent/media/')) return '';
  try {
    const url = new URL(value, 'http://rag-ime.local');
    const match = /^\/api\/agent\/media\/([^/]+)\/(?:content|preview)$/u.exec(url.pathname);
    if (!match || decodeURIComponent(match[1] ?? '') !== expectedMediaId || url.hash) return '';
    if ([...url.searchParams.keys()].some((key) => key !== 'sessionId')) return '';
    const values = url.searchParams.getAll('sessionId');
    return values.length === 1 && SESSION_ID_PATTERN.test(values[0] ?? '') ? values[0]! : '';
  } catch {
    return '';
  }
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function number(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}
