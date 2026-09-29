/** Presentation helpers only. No content from these helpers becomes a command. */
export function safeDownloadName(value: string, fallback = 'content.txt'): string {
  const name = value.split(/[\\/]/u).at(-1)?.replace(/[\u0000-\u001f<>:"|?*]/gu, '_').trim();
  return name && !/^\.+$/u.test(name) ? name.slice(0, 180) : fallback;
}

export function downloadText(content: string, name: string, mime = 'text/plain;charset=utf-8'): void {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const anchor = document.createElement('a');
  anchor.href = url; anchor.download = safeDownloadName(name); anchor.hidden = true;
  document.body.append(anchor); anchor.click(); anchor.remove();
  // Delayed revocation also works in Safari, which may consume the URL later.
  window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
}

/** Keep numerical negatives numeric; prevent formula execution for textual cells. */
export function spreadsheetCell(value: string): string {
  const probe = value.replace(/^[\s\u0000-\u001f]+/u, '');
  const formula = /^[=+@]/u.test(probe)
    || /^-/u.test(probe) && !/^-\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/u.test(probe);
  return formula ? `'${value}` : value;
}

export function cellsToCsv(rows: readonly (readonly string[])[]): string {
  return rows.map(row => row.map(value => `"${spreadsheetCell(value).replace(/"/gu, '""')}"`).join(',')).join('\r\n');
}

export function cellsToTsv(rows: readonly (readonly string[])[]): string {
  return rows.map(row => row.map(value => spreadsheetCell(value).replace(/\r?\n|\t/gu, ' ')).join('\t')).join('\n');
}

/** Quoted CSV/TSV, including escaped quotes and newlines. Bounded for the chat UI. */
export function parseDelimited(source: string, delimiter = ','): { rows: string[][]; error?: string } {
  if (source.length > 200_000) return { rows: [], error: '内容较大，请查看源码或下载后打开。' };
  const rows: string[][] = []; let row: string[] = []; let cell = ''; let quoted = false; let closed = false;
  const input = source.replace(/^\uFEFF/u, '');
  const pushCell = () => { row.push(cell); cell = ''; closed = false; };
  const pushRow = () => { pushCell(); rows.push(row); row = []; };
  for (let i = 0; i < input.length; i++) {
    const c = input[i]!;
    if (quoted) {
      if (c === '"' && input[i + 1] === '"') { cell += '"'; i++; }
      else if (c === '"') { quoted = false; closed = true; }
      else cell += c;
    } else if (c === '"' && !cell && !closed) quoted = true;
    else if (c === delimiter) pushCell();
    else if (c === '\n' || c === '\r') { pushRow(); if (c === '\r' && input[i + 1] === '\n') i++; }
    else if (closed && !/\s/u.test(c)) return { rows: [], error: '引号后的 CSV 内容无法解析；原文已保留。' };
    else if (!closed) cell += c;
    if (rows.length > 2_000 || row.length > 128) return { rows: [], error: '数据超过对话预览范围，请下载完整源码。' };
  }
  if (quoted) return { rows: [], error: '引号尚未闭合，请查看原始内容。' };
  if (cell || row.length || closed) pushRow();
  if (rows.length > 2_000 || rows.some(cells => cells.length > 128)) return { rows: [], error: '数据超过对话预览范围，请下载完整源码。' };
  return { rows };
}

export type RichFileKind = 'document' | 'sheet' | 'presentation' | 'image' | 'audio' | 'video' | 'archive' | 'code' | 'diff' | 'file';
export function richFileKind(fileName: string, mime = ''): RichFileKind {
  const name = fileName.toLowerCase();
  if (mime.startsWith('image/') || /\.(png|jpe?g|gif|webp|avif|heic|svg)$/u.test(name)) return 'image';
  if (mime.startsWith('audio/') || /\.(mp3|wav|m4a|aac|flac|ogg)$/u.test(name)) return 'audio';
  if (mime.startsWith('video/') || /\.(mp4|webm|mov|m4v|mkv)$/u.test(name)) return 'video';
  if (/\.(csv|tsv|xlsx?|ods|parquet)$/u.test(name) || /spreadsheet|excel/u.test(mime)) return 'sheet';
  if (/\.(pptx?|odp)$/u.test(name) || /presentation/u.test(mime)) return 'presentation';
  if (/\.(pdf|docx?|odt|rtf|md|mdx|markdown)$/u.test(name)) return 'document';
  if (/\.(zip|tar|gz|7z|rar|bz2|xz)$/u.test(name)) return 'archive';
  if (/\.(diff|patch)$/u.test(name)) return 'diff';
  if (mime.startsWith('text/') || /\.(json|jsonl|ya?ml|toml|ini|xml|[cm]?[jt]sx?|css|scss|html?|py|swift|sql|sh|go|rs|java|rb|[ch](pp)?|vue|svelte)$/u.test(name)) return 'code';
  return 'file';
}

export const RICH_FILE_LABELS: Record<RichFileKind, string> = {
  document: '文档', sheet: '数据表', presentation: '演示文稿', image: '图片', audio: '音频',
  video: '视频', archive: '压缩包', code: '文本与代码', diff: '代码变更', file: '文件',
};

export type FencedPreviewKind = 'math' | 'mermaid' | 'json' | 'csv' | 'tsv' | 'svg';
export function fencedPreviewKind(language: string): FencedPreviewKind | undefined {
  const value = language.toLowerCase().trim();
  if (['math', 'latex', 'tex'].includes(value)) return 'math';
  return ['mermaid', 'json', 'csv', 'tsv', 'svg'].includes(value) ? value as FencedPreviewKind : undefined;
}
