import { describe, expect, it } from 'vitest';
import { createPreviewTransport } from './preview-control-transport';
import { PREVIEW_PDF_BASE, PREVIEW_PDF_ID } from './preview-knowledge-data';

describe('structured Knowledge preview fixtures', () => {
  it('reads the same Markdown source as the document body and binds structured assets to blocks', async () => {
    const transport = createPreviewTransport();
    const input = { kbId: PREVIEW_PDF_BASE, fileId: 'file:preview-yuxi' };
    const detail = await transport.request<Record<string, any>>({ pathId: 'knowledgeBases.document.get', params: input });
    expect(detail.document.fileName).toBe('伙伴运行笔记.md');
    const source = await transport.readKnowledgeDocumentSource(input);
    expect(await blobText(source.blob)).toBe(detail.contentWindow.items.map((line: { content: string }) => line.content).join('\n'));
    expect(source.byteSize).toBe(source.blob.size);
    expect(source.sha256).toBe(await textHash(await blobText(source.blob)));
    expect(detail.chunks.items).toHaveLength(36);
    expect(detail.chunks.items[1].chunkId).toBe('chunk:preview-agent-loop');
    expect(detail.tables[0]).toMatchObject({ sourceBlockOrders: [2], dataAvailable: true, truncated: false, totalRowCount: 2 });
    expect(detail.assets[0]).toMatchObject({ sourcePaths: ['images/preview-flow.svg'], locations: [{ sourceBlockOrder: 3, page: null }] });
    const asset = await transport.readKnowledgeAsset({ ...input, assetId: detail.assets[0].assetId });
    expect(asset.sha256).toBe(detail.chunks.items[3].provenance.sourceBlocks[0].metadata.assetSha256);
    expect(await blobText(asset.blob)).toContain('Synthetic preview diagram');
    expect(asset.sha256).toBe(await textHash(await blobText(asset.blob)));
    expect(asset.byteSize).toBe(detail.assets[0].byteSize);
  });

  it('provides a real two-page PDF with valid xref offsets and a page-two search hit', async () => {
    const transport = createPreviewTransport();
    const input = { kbId: PREVIEW_PDF_BASE, fileId: PREVIEW_PDF_ID };
    const detail = await transport.request<Record<string, any>>({ pathId: 'knowledgeBases.document.get', params: input });
    expect(detail.document).toMatchObject({ fileName: '结构化阅读演示.pdf', pageCount: 2, mimeType: 'application/pdf' });
    const source = await transport.readKnowledgeDocumentSource(input);
    const pdf = await blobText(source.blob);
    expect(pdf.startsWith('%PDF-1.4')).toBe(true);
    expect(pdf.match(/\/Type \/Page\b/g)).toHaveLength(2);
    expect(pdf).toContain('Structured reading demo - page 2');
    const xref = Number(pdf.match(/startxref\n(\d+)/)?.[1]);
    expect(pdf.slice(xref, xref + 4)).toBe('xref');
    const offsets = [...pdf.matchAll(/^(\d{10}) 00000 n /gm)].map((match) => Number(match[1]));
    offsets.forEach((offset, index) => expect(pdf.slice(offset)).toMatch(new RegExp(`^${index + 1} 0 obj`)));
    expect(source.sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(source.sha256).toBe(await textHash(pdf));
    expect(source.blob.type).toBe('application/pdf');
    const search = await transport.request<Record<string, any>>({ pathId: 'knowledgeBases.search', params: { kbId: input.kbId }, body: { query: '结构化阅读演示 PDF' } });
    expect(search.items[0]).toMatchObject({ documentId: PREVIEW_PDF_ID, citation: { page: 2 } });
    const base = await transport.request<Record<string, any>>({ pathId: 'knowledgeBases.get', params: { kbId: input.kbId } });
    expect(base.base).toMatchObject({ documentCount: 2, chunkCount: 40 });
  });

  it('keeps pagination truthful and rejects unknown or cross-base document/asset identities', async () => {
    const transport = createPreviewTransport();
    const params = { kbId: PREVIEW_PDF_BASE, fileId: 'file:preview-yuxi' };
    const first = await transport.request<Record<string, any>>({ pathId: 'knowledgeBases.document.get', params, query: { offset: 1, limit: 1, lineOffset: 2, lineLimit: 3 } });
    expect(first.chunks).toMatchObject({ total: 36, hasMore: true });
    expect(first.chunks.items).toHaveLength(1);
    expect(first.contentWindow.items.map((row: { lineNumber: number }) => row.lineNumber)).toEqual([3, 4, 5]);
    for (const input of [{ kbId: 'missing', fileId: PREVIEW_PDF_ID }, { kbId: PREVIEW_PDF_BASE, fileId: 'missing' }, { kbId: 'kb:preview-antarctic-papers', fileId: 'file:preview-antarctic-papers' }]) {
      await expect(transport.request({ pathId: 'knowledgeBases.document.get', params: input })).rejects.toThrow();
      await expect(transport.readKnowledgeDocumentSource(input)).rejects.toThrow();
    }
    await expect(transport.readKnowledgeAsset({ ...params, assetId: 'missing' })).rejects.toThrow();
    await expect(transport.readKnowledgeAsset({ kbId: 'missing', fileId: params.fileId, assetId: first.assets[0].assetId })).rejects.toThrow();
    const manifest = await transport.request<Record<string, any>>({ pathId: 'knowledgeBases.documents.list', params: { kbId: 'kb:preview-antarctic-papers' } });
    expect(manifest.items).toHaveLength(1);
    expect(manifest.items[0]).toMatchObject({ fileName: 'Zotero 南极论文库（198 个 PDF）.manifest.md', mimeType: 'text/markdown', status: 'queued' });
  });
});

function blobText(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

async function textHash(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}
