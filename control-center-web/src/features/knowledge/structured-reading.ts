import type { KnowledgeAsset, KnowledgeChunk, KnowledgeTableArtifact } from './api';

export function chunkTables(chunk: KnowledgeChunk, tables: readonly KnowledgeTableArtifact[]): KnowledgeTableArtifact[] {
  const orders = new Set(chunk.provenance?.sourceBlocks.map((block) => block.order).filter((order): order is number => typeof order === 'number') ?? []);
  return tables.filter((table) => table.sourceBlockOrders?.some((order) => orders.has(order)));
}

export function chunkAssets(chunk: KnowledgeChunk, assets: readonly KnowledgeAsset[]): KnowledgeAsset[] {
  const blocks = chunk.provenance?.sourceBlocks ?? [];
  return assets.filter((asset) => asset.mimeType.startsWith('image/') && asset.readPath && blocks.some((block) => (
    block.assetSha256 && block.assetSha256 === asset.sha256
    || typeof block.order === 'number' && asset.locations?.some((location) => location.sourceBlockOrder === block.order)
  )));
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
