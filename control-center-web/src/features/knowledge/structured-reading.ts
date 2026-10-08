import { KNOWLEDGE_AUDIO_ASSET_MIME_TYPES, KNOWLEDGE_IMAGE_ASSET_MIME_TYPES } from '@/platform/knowledge-media';
import type { KnowledgeAsset, KnowledgeChunk, KnowledgeProvenance, KnowledgeTableArtifact, KnowledgeTimestampKind } from './api';

export function chunkTables(chunk: KnowledgeChunk, tables: readonly KnowledgeTableArtifact[]): KnowledgeTableArtifact[] {
  const orders = new Set(chunk.provenance?.sourceBlocks.map((block) => block.order).filter((order): order is number => typeof order === 'number') ?? []);
  return tables.filter((table) => table.sourceBlockOrders?.some((order) => orders.has(order)));
}

export function chunkAssets(chunk: KnowledgeChunk, assets: readonly KnowledgeAsset[]): KnowledgeAsset[] {
  const blocks = chunk.provenance?.sourceBlocks ?? [];
  return assets.filter((asset) => [...KNOWLEDGE_IMAGE_ASSET_MIME_TYPES, ...KNOWLEDGE_AUDIO_ASSET_MIME_TYPES].includes(asset.mimeType) && asset.readPath && blocks.some((block) => (
    block.assetSha256 && block.assetSha256 === asset.sha256
    || typeof block.order === 'number' && asset.locations?.some((location) => location.sourceBlockOrder === block.order)
  )));
}

export function knowledgeMediaKind(provenance: KnowledgeProvenance | undefined): 'audio' | 'video-frame' | null {
  if (provenance?.modality === 'audio' || provenance?.kind === 'audio') return 'audio';
  return provenance?.sourceBlocks.some((block) => block.sourcePart === 'video-frame') ? 'video-frame' : null;
}

interface MediaPosition { startSeconds: number; endSeconds: number; timestampKind: KnowledgeTimestampKind }
type MediaProvenance = KnowledgeProvenance | KnowledgeProvenance['sourceBlocks'][number];

export function knowledgeMediaPosition(provenance: MediaProvenance | undefined): MediaPosition | null {
  if (!provenance) return null;
  const valid = (value: { startSeconds?: number | null; endSeconds?: number | null; timestampKind?: KnowledgeTimestampKind | null }): value is MediaPosition => (
    typeof value.startSeconds === 'number' && Number.isFinite(value.startSeconds) && value.startSeconds >= 0
    && typeof value.endSeconds === 'number' && Number.isFinite(value.endSeconds) && value.endSeconds >= value.startSeconds
    && (value.timestampKind === 'seek-offset' || value.timestampKind === 'segment-offset')
  );
  const positions: MediaPosition[] = [provenance, ...('sourceBlocks' in provenance ? provenance.sourceBlocks : [])].flatMap((value) => valid(value) ? [{ startSeconds: value.startSeconds, endSeconds: value.endSeconds, timestampKind: value.timestampKind }] : []);
  const first = positions[0];
  if (!first || positions.some((position) => position.startSeconds !== first.startSeconds || position.endSeconds !== first.endSeconds || position.timestampKind !== first.timestampKind)) return null;
  // A root range must agree with its source block; never mix two locations.
  if (typeof provenance.startSeconds === 'number' && typeof provenance.endSeconds === 'number'
    && (provenance.startSeconds !== first.startSeconds || provenance.endSeconds !== first.endSeconds)) return null;
  return first;
}

export function formatKnowledgeMediaTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '时间位置未提供';
  const rounded = Number(seconds.toFixed(3));
  const whole = Math.floor(rounded);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor(whole / 60) % 60;
  const fraction = (rounded - whole).toFixed(3).slice(1).replace(/0+$/u, '').replace(/\.$/u, '');
  return `${hours ? `${hours}:` : ''}${String(minutes).padStart(2, '0')}:${String(whole % 60).padStart(2, '0')}${fraction}`;
}

export function knowledgeMediaPositionLabel(provenance: MediaProvenance | undefined): string | null {
  const position = knowledgeMediaPosition(provenance);
  if (!position) return null;
  const start = formatKnowledgeMediaTime(position.startSeconds);
  const end = position.endSeconds === position.startSeconds ? '' : `–${formatKnowledgeMediaTime(position.endSeconds)}`;
  return `${start}${end}${position.timestampKind === 'seek-offset' ? '（采样偏移）' : ''}`;
}

export function knowledgeMediaLocationLabel(provenance: KnowledgeProvenance | undefined): string | null {
  const kind = knowledgeMediaKind(provenance);
  return kind ? `${kind === 'audio' ? '音频片段' : '采样画面'} · ${knowledgeMediaPositionLabel(provenance) ?? '时间位置未提供'}` : null;
}

function relativeAssetPath(value: string): boolean {
  return !/^(?:[a-z][a-z\d+.-]*:|\/|\\)/iu.test(value) && !value.split(/[\\/]/u).includes('..');
}

export function localMarkdownAsset(src: string | undefined, assets: readonly KnowledgeAsset[]): KnowledgeAsset | undefined {
  if (!src || !relativeAssetPath(src)) return undefined;
  let decoded = src;
  try { decoded = decodeURIComponent(src); } catch { /* Keep an exact literal percent path. */ }
  if (!relativeAssetPath(decoded)) return undefined;
  const matches = assets.filter((asset) => asset.mimeType.startsWith('image/') && asset.readPath && asset.sourcePaths?.some((path) => path === src || path === decoded));
  return matches.length === 1 ? matches[0] : undefined;
}

function markdownCell(value: string): string {
  return value.replace(/\\/gu, '\\\\').replace(/[|`*_~\[\]<>!]/gu, '\\$&').replace(/[\r\n]+/gu, ' ');
}

// Match only a unique backend-known table, including its bounded HTML prefix.
// Ambiguous prefixes and incomplete content windows remain verbatim.
export function readableTableMarkdown(markdown: string, tables: readonly KnowledgeTableArtifact[]): string {
  return markdown.replace(/<table\b[^>]*>[\s\S]*?<\/table>/giu, (raw) => {
    const matches = tables.filter((table) => {
      if (!table.columns.length || table.dataAvailable === false && !table.rows.length) return false;
      const start = table.markdown.search(/<table\b/iu);
      if (start < 0) return false;
      const html = table.markdown.slice(start);
      const complete = html.match(/^<table\b[^>]*>[\s\S]*?<\/table>/iu)?.[0];
      return complete ? complete === raw : table.truncated === true && html.length > 64 && raw.startsWith(html);
    });
    if (matches.length !== 1) return raw;
    const table = matches[0];
    const line = (cells: string[]) => `| ${cells.map(markdownCell).join(' | ')} |`;
    const replacement = [line(table.columns), line(table.columns.map(() => '---')), ...table.rows.map(line),
      ...(table.truncated ? ['', '表格预览已截断；完整内容请查看源文件。'] : []),
    ].join('\n');
    return `\n\n${replacement}\n\n`;
  });
}
