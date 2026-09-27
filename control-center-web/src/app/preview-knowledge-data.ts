import type { ControlRequest, KnowledgeAssetReadInput, KnowledgeDocumentSourceReadInput } from '@/platform/transport';

export const PREVIEW_PDF_ID = 'file:preview-structured-reader';
export const PREVIEW_PDF_BASE = 'kb:preview-project-docs';
export const previewStructuredPdfDocument = {
  id: PREVIEW_PDF_ID, baseId: PREVIEW_PDF_BASE, fileName: '结构化阅读演示.pdf', mimeType: 'application/pdf',
  status: 'ready', stage: 'ready', chunkCount: 4, pageCount: 2, parserProvider: 'preview-synthetic', revision: 1,
  updatedAtMs: 1_789_776_000_000,
};

const diagram = '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="120" viewBox="0 0 480 120"><rect width="480" height="120" fill="#f4f6f8"/><g fill="#17476b" font-family="sans-serif" font-size="18"><text x="20" y="52">Source</text><text x="196" y="52">Chunk</text><text x="365" y="52">Search</text><text x="20" y="98" font-size="14">Synthetic preview diagram - no corpus evidence</text></g><g stroke="#17476b" stroke-width="2"><path d="M90 46H180m-9-6 9 6-9 6M265 46H350m-9-6 9 6-9 6" fill="none"/></g></svg>';
const diagramPath = 'images/preview-flow.svg';
const tableMarkdown = '| Stage | Output |\n| --- | --- |\n| Parse | Structured blocks |\n| Retrieve | Source citations |';

/** A tiny, genuine two-page PDF. ASCII keeps xref byte offsets exact without a PDF dependency. */
export function createPreviewPdf(): string {
  const page1 = 'BT /F1 20 Tf 54 738 Td (Structured reading demo - page 1) Tj 0 -36 Td /F1 12 Tf (Synthetic preview only. Not a real paper or corpus result.) Tj 0 -26 Td (Open the search hit to navigate to page 2.) Tj ET';
  const page2 = 'BT /F1 20 Tf 54 738 Td (Structured reading demo - page 2) Tj 0 -40 Td /F1 12 Tf (Stage          Output) Tj 0 -24 Td (Parse          Structured blocks) Tj 0 -24 Td (Retrieve       Source citations) Tj 0 -56 Td (Source  --->  Chunk  --->  Search) Tj 0 -36 Td (Synthetic preview diagram - no corpus evidence) Tj ET';
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    `<< /Length ${page1.length} >>\nstream\n${page1}\nendstream`, `<< /Length ${page2.length} >>\nstream\n${page2}\nendstream`,
  ];
  let output = '%PDF-1.4\n';
  const offsets = [0];
  objects.forEach((object, index) => { offsets.push(output.length); output += `${index + 1} 0 obj\n${object}\nendobj\n`; });
  const xref = output.length;
  output += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets.slice(1).map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`).join('')}trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return output;
}

function contents(document: Record<string, unknown>) {
  const isPdf = document.id === PREVIEW_PDF_ID;
  const count = isPdf ? 4 : document.id === 'file:preview-yuxi' ? 36 : 28;
  const sections = isPdf ? [
    '# Structured reading demo - page 1\n\nSynthetic preview only. Not a real paper or corpus result.\n\nOpen the search hit to navigate to page 2.',
    '# Structured reading demo - page 2',
    tableMarkdown,
    'Source ---> Chunk ---> Search\n\nSynthetic preview diagram - no corpus evidence',
  ] : [
    `# ${String(document.fileName)}\n\n公开合成演示数据，用于核对正文、表格、图片与来源导航；不代表真实资料解析或语料检索结果。`,
    '## Agent Tool 边界\n\n文档知识库通过只读 Tool 按需检索，不会进入输入法候选热路径。',
    `## 处理步骤（演示表格）\n\n${tableMarkdown}`,
    '## 来源流程（演示图片）\n\nSource → Chunk → Search。合成流程示意，不代表真实语料证据。',
    ...Array.from({ length: count - 4 }, (_, index) => `## 阅读检查 ${index + 1}\n\n演示检查项 ${index + 1}：从检索片段回到原始资料，核对来源后再使用结论。`),
  ];
  return { isPdf, sections, markdown: sections.join('\n\n') };
}

function readable(document: Record<string, unknown>): boolean {
  return (document.id === 'file:preview-yuxi' && document.baseId === PREVIEW_PDF_BASE)
    || (document.id === 'file:preview-google-earth' && document.baseId === 'kb:preview-google-earth')
    || (document.id === PREVIEW_PDF_ID && document.baseId === PREVIEW_PDF_BASE);
}

function findDocument(documents: Record<string, unknown>[], kbId: string, fileId: string) {
  const document = documents.find((row) => row.baseId === kbId && row.id === fileId);
  if (!document || !readable(document)) throw new Error('此演示材料没有可读取的文档内容。');
  return document;
}

export function previewReadableDocument(document: Record<string, unknown>): Record<string, unknown> {
  if (!readable(document)) return document;
  const content = contents(document);
  const source = content.isPdf ? createPreviewPdf() : content.markdown;
  return { ...document, byteSize: new TextEncoder().encode(source).length, pageCount: content.isPdf ? 2 : 0,
    sourceReadPath: `/api/knowledge-bases/${document.baseId}/documents/${document.id}/source` };
}

async function hash(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

export async function previewKnowledgeSource(input: KnowledgeDocumentSourceReadInput, documents: Record<string, unknown>[]) {
  input.signal?.throwIfAborted();
  const document = findDocument(documents, input.kbId, input.fileId);
  const content = contents(document);
  const text = content.isPdf ? createPreviewPdf() : content.markdown;
  const mimeType = content.isPdf ? 'application/pdf' : 'text/markdown';
  const blob = new Blob([text], { type: mimeType });
  return { kbId: input.kbId, fileId: input.fileId, mimeType, byteSize: blob.size, sha256: await hash(text), blob };
}

export async function previewKnowledgeAsset(input: KnowledgeAssetReadInput, documents: Record<string, unknown>[]) {
  input.signal?.throwIfAborted();
  findDocument(documents, input.kbId, input.fileId);
  const sha256 = await hash(diagram);
  if (input.assetId !== sha256) throw new Error('此演示材料没有这个解析产物。');
  const blob = new Blob([diagram], { type: 'image/svg+xml' });
  return { kbId: input.kbId, fileId: input.fileId, assetId: sha256, mimeType: blob.type, byteSize: blob.size, sha256, blob };
}

export async function previewKnowledgeDetail(request: ControlRequest, documents: Record<string, unknown>[]) {
  const kbId = String(request.params?.kbId ?? '');
  const fileId = String(request.params?.fileId ?? '');
  const document = findDocument(documents, kbId, fileId);
  const content = contents(document);
  const sha256 = await hash(diagram);
  const chunks = content.sections.map((text, order) => ({
    chunkId: fileId === 'file:preview-yuxi' && order === 1 ? 'chunk:preview-agent-loop' : `${fileId}:chunk:${order}`,
    ordinal: order, content: text, heading: text.split('\n')[0].replace(/^#+\s*/, ''), page: content.isPdf ? order === 0 ? 1 : 2 : null,
    provenance: { kind: order === 2 ? 'table' : order === 3 ? 'image' : 'text', parser: 'preview-synthetic', split: false,
      headingPath: ['公开合成演示'], sourceBlocks: [{ id: `block:${order}`, order, page: content.isPdf ? order === 0 ? 1 : 2 : null,
        bbox: null, metadata: order === 3 ? { assetSha256: sha256, imagePath: diagramPath, ocrApplied: false } : {} }] },
  }));
  const lines = content.markdown.split('\n').map((text, index) => ({ lineNumber: index + 1, content: text }));
  const bounded = (value: unknown, fallback: number, max: number) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? Math.min(value, max) : fallback;
  const offset = bounded(request.query?.offset, 0, chunks.length);
  const limit = bounded(request.query?.limit, 200, 200);
  const lineOffset = bounded(request.query?.lineOffset, 0, lines.length);
  const lineLimit = bounded(request.query?.lineLimit, 200, 200);
  const page = content.isPdf ? 2 : null;
  return { ok: true, document: previewReadableDocument(document),
    chunks: { items: chunks.slice(offset, offset + limit), total: chunks.length, hasMore: offset + limit < chunks.length },
    pages: content.isPdf ? [{ page: 1, chunkCount: 1 }, { page: 2, chunkCount: 3 }] : [],
    tables: [{ tableId: `${fileId}:table:2`, title: '处理步骤（合成演示）', page, columns: ['Stage', 'Output'], rows: [['Parse', 'Structured blocks'], ['Retrieve', 'Source citations']], markdown: tableMarkdown,
      kind: 'table', sourceBlockOrders: [2], dataAvailable: true, truncated: false, totalRowCount: 2 }],
    assets: [{ assetId: sha256, name: 'preview-flow.svg', mimeType: 'image/svg+xml', byteSize: new TextEncoder().encode(diagram).length, sha256,
      readPath: `/api/knowledge-bases/${kbId}/documents/${fileId}/assets/${sha256}`, page, pages: page ? [page] : [], caption: '来源流程（合成演示）',
      sourcePaths: [diagramPath], locations: [{ sourceBlockOrder: 3, page, caption: '来源流程（合成演示）' }], locationCount: 1, locationsTruncated: false }],
    artifact: { available: true, format: 'markdown', mimeType: 'text/markdown', byteSize: new TextEncoder().encode(content.markdown).length, lineCount: lines.length, sha256: await hash(content.markdown) },
    contentWindow: { items: lines.slice(lineOffset, lineOffset + lineLimit), total: lines.length, hasMore: lineOffset + lineLimit < lines.length },
  };
}

export function previewKnowledgePdfHit(baseId: string, query: string): Record<string, unknown> | null {
  if (baseId !== PREVIEW_PDF_BASE || !/结构化|pdf|第二页|页码/iu.test(query)) return null;
  return { chunkId: `${PREVIEW_PDF_ID}:chunk:2`, documentId: PREVIEW_PDF_ID, documentName: previewStructuredPdfDocument.fileName,
    heading: '处理步骤（合成演示）', content: tableMarkdown, score: 0.92, citation: { page: 2, heading: '处理步骤（合成演示）' },
    diagnostics: { effectiveMode: 'hybrid', lexicalRank: 1, denseRank: 1, retrievalRank: 1, retrievalScore: 0.92 } };
}
