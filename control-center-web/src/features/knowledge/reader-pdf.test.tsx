import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MockControlTransport } from '@/test/mock-transport';
import type { KnowledgeDocumentDetail, KnowledgeSearchHit } from './api';
import { DocumentSource } from './document-workspace';

beforeEach(() => {
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:pdf-source');
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function detail(id = 'paper', pageCount = 20): KnowledgeDocumentDetail {
  return {
    document: { id, baseId: 'base', name: `${id}.pdf`, mimeType: 'application/pdf', byteSize: 1,
      status: 'ready', stage: 'ready', progress: 1, error: '', chunkCount: 0, parser: 'mineru',
      updatedAtMs: 1, revision: 1, sha256: '', pageCount, tokenCount: 0, parserVersion: '',
      sourceReadPath: '/source', indexedConfigRevision: 1 },
    chunks: [], chunkTotal: 0, chunkHasMore: false, pages: [{ page: 3, chunkCount: 1 }, { page: 12, chunkCount: 1 }],
    assets: [], tables: [], artifact: { available: false, format: '', mimeType: '', byteSize: 0, lineCount: 0, sha256: '' },
    contentWindow: [], contentLineTotal: 0, contentHasMore: false,
  };
}
function hit(page: number | null, documentId = 'paper', id = 'hit'): KnowledgeSearchHit {
  return { id, documentId, documentName: 'paper.pdf', title: 'Result', excerpt: 'Evidence', score: 1,
    page, heading: '', lineStart: null, lineEnd: null, diagnostics: {} as KnowledgeSearchHit['diagnostics'] };
}
function transport() {
  return new MockControlTransport({ knowledgeDocumentSource: async ({ kbId, fileId }) => ({
    kbId, fileId, mimeType: 'application/pdf', byteSize: 1, sha256: '', blob: new Blob(['%PDF'], { type: 'application/pdf' }),
  }) });
}

describe('PDF source reader page navigation', () => {
  it('opens the search hit page while keeping the download URL unfragmented', async () => {
    render(<DocumentSource detail={detail()} focusHit={hit(12)} transport={transport()} />);
    expect(await screen.findByTitle('paper.pdf 源文件')).toHaveAttribute('src', 'blob:pdf-source#page=12');
    expect(screen.getByText('检索命中：第 12 页')).toBeVisible();
    expect(screen.getByRole('link', { name: '下载源文件' })).toHaveAttribute('href', 'blob:pdf-source');
    expect(screen.getByRole('link', { name: '下载源文件' })).toHaveAttribute('download', 'paper.pdf');
  });

  it('allows any page within the reported count and rejects out-of-range or fractional pages', async () => {
    render(<DocumentSource detail={detail()} focusHit={hit(12)} transport={transport()} />);
    const frame = await screen.findByTitle('paper.pdf 源文件');
    const input = screen.getByRole('spinbutton', { name: 'PDF 页码' });
    fireEvent.change(input, { target: { value: '7' } });
    fireEvent.click(screen.getByRole('button', { name: '跳转' }));
    expect(frame).toHaveAttribute('src', 'blob:pdf-source#page=7');
    for (const value of ['0', '21', '1.5', '']) {
      fireEvent.change(input, { target: { value } });
      expect(screen.getByRole('button', { name: '跳转' })).toBeDisabled();
      expect(frame).toHaveAttribute('src', 'blob:pdf-source#page=7');
    }
  });

  it('offers only evidenced pages when the total is unknown, including the hit page', async () => {
    render(<DocumentSource detail={detail('paper', 0)} focusHit={hit(18)} transport={transport()} />);
    const frame = await screen.findByTitle('paper.pdf 源文件');
    expect(frame).toHaveAttribute('src', 'blob:pdf-source#page=18');
    expect(screen.getByText('总页数未提供')).toBeVisible();
    expect(screen.queryByRole('spinbutton')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('combobox', { name: 'PDF 已知页码' }));
    expect(screen.queryByRole('option', { name: '第 2 页' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('option', { name: '第 3 页' }));
    expect(frame).toHaveAttribute('src', 'blob:pdf-source#page=3');
  });

  it.each([0, -1, 1.5, 21, Number.NaN])('ignores invalid hit page %s', async (page) => {
    render(<DocumentSource detail={detail()} focusHit={hit(page)} transport={transport()} />);
    expect(await screen.findByTitle('paper.pdf 源文件')).toHaveAttribute('src', 'blob:pdf-source#page=1');
    expect(screen.queryByText(/^检索命中：第/)).not.toBeInTheDocument();
  });

  it('resets navigation when the hit or document changes and ignores another document’s hit', async () => {
    const sourceTransport = transport();
    const view = render(<DocumentSource detail={detail()} focusHit={hit(12)} transport={sourceTransport} />);
    await screen.findByTitle('paper.pdf 源文件');
    fireEvent.change(screen.getByRole('spinbutton', { name: 'PDF 页码' }), { target: { value: '7' } });
    fireEvent.click(screen.getByRole('button', { name: '跳转' }));
    view.rerender(<DocumentSource detail={detail()} focusHit={hit(4, 'paper', 'another')} transport={sourceTransport} />);
    expect(screen.getByTitle('paper.pdf 源文件')).toHaveAttribute('src', 'blob:pdf-source#page=4');
    expect(screen.getByRole('spinbutton', { name: 'PDF 页码' })).toHaveValue(4);
    view.rerender(<DocumentSource detail={detail('other')} focusHit={hit(4)} transport={sourceTransport} />);
    await waitFor(() => expect(screen.getByTitle('other.pdf 源文件')).toHaveAttribute('src', 'blob:pdf-source#page=1'));
    expect(screen.getByRole('spinbutton', { name: 'PDF 页码' })).toHaveValue(1);
    view.rerender(<DocumentSource detail={detail()} focusHit={hit(12)} transport={sourceTransport} />);
    await waitFor(() => expect(screen.getByTitle('paper.pdf 源文件')).toHaveAttribute('src', 'blob:pdf-source#page=12'));
  });

  it('does not invent a page or total for a source with no page metadata', async () => {
    const sourceDetail = detail('paper', 0);
    sourceDetail.pages = [];
    render(<DocumentSource detail={sourceDetail} focusHit={null} transport={transport()} />);
    expect(await screen.findByTitle('paper.pdf 源文件')).toHaveAttribute('src', 'blob:pdf-source');
    expect(screen.getByText('总页数未提供')).toBeVisible();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });
});
