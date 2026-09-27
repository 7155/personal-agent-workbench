import { normalizeComposerAttachmentMimeType } from '@/contracts/attachment-policy';

const TEXT_LIMIT = 2 * 1024 * 1024;
// Text source formats use the managed-media service's UTF-8 text channel.
// Original filenames and bytes are preserved.
const TEXT_MIME_TYPES = new Set(['text/plain', 'text/markdown', 'text/html', 'text/x-diff', 'text/x-patch']);
const MEDIA_MIME_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'audio/mpeg', 'audio/mp4', 'audio/wav', 'application/pdf']);
const SOURCE_EXTENSION = /\.(?:geojson|json|csv|tsv|kml|gpx|xml|txt|md|markdown|html?|css|js|mjs|cjs|jsx|ts|tsx|py|r|sql|yaml|yml|toml|ini|log|patch|diff)$/i;

export class AttachmentImportError extends Error {}

export function attachmentImportErrorText(reason: unknown): string {
  return reason instanceof AttachmentImportError ? reason.message : '附件未导入，请检查连接后重新选择。输入内容已保留。';
}

function readBytes(file: File): Promise<ArrayBuffer> {
  if (typeof file.arrayBuffer === 'function') return file.arrayBuffer();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () => reject(new AttachmentImportError('无法读取附件，请重新选择文件。'));
    reader.readAsArrayBuffer(file);
  });
}

export async function prepareHttpAttachment(file: File): Promise<File> {
  const mime = normalizeComposerAttachmentMimeType(file.type);
  if (MEDIA_MIME_TYPES.has(mime)) return file;
  const textSource = TEXT_MIME_TYPES.has(mime) || SOURCE_EXTENSION.test(file.name)
    || mime === 'application/json' || mime === 'application/geo+json' || mime === 'application/xml' || mime.startsWith('text/');
  if (!textSource) throw new AttachmentImportError('当前附件通道不支持这个文件格式。GIS 数据包请从项目的数据入口导入。');
  if (file.size > TEXT_LIMIT) throw new AttachmentImportError('文本附件不能超过 2 MB，请缩小文件后重新选择。');
  const bytes = await readBytes(file);
  let content: string;
  try { content = new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
  catch { throw new AttachmentImportError('文本附件需要 UTF-8 编码，请转换编码后重新选择。'); }
  if (content.includes('\0')) throw new AttachmentImportError('文件包含二进制内容，不能作为文本附件导入。');
  return new File([bytes], file.name, { type: TEXT_MIME_TYPES.has(mime) ? mime : 'text/plain', lastModified: file.lastModified });
}
