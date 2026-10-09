import { expect, it } from 'vitest';
import { chunkAssets, chunkTables, formatKnowledgeMediaTime, knowledgeMediaLocationLabel, knowledgeMediaPositionLabel, localMarkdownAsset, readableTableMarkdown } from './structured-reading';
import type { KnowledgeAsset, KnowledgeChunk, KnowledgeTableArtifact } from './api';

const chunk: KnowledgeChunk = { id: 'hit', ordinal: 1, content: 'same table', page: 2, heading: '', lineStart: null, lineEnd: null, tokenCount: 0, provenance: { kind: 'table', parser: 'fixture', split: false, headingPath: [], sourceBlocks: [{ order: 5, page: 2, bbox: null, coordinateSystem: '', assetSha256: 'hash-one' }] } };
const table: KnowledgeTableArtifact = { id: 'table', title: 'Measurements', page: 2, columns: ['Sample', 'Value'], rows: [['A', '12']], markdown: '<table><tr><td>A</td><td>12</td></tr></table>', sourceBlockOrders: [5] };
const asset: KnowledgeAsset = { id: 'image', name: 'same.png', mimeType: 'image/png', byteSize: 10, sha256: 'hash-one', readPath: '/safe', page: 2, caption: '', sourcePaths: ['images/same.png'] };
it('binds tables by source order rather than identical text or page', () => {
  expect(chunkTables(chunk, [{ ...table, id: 'wrong', sourceBlockOrders: [2] }, table])).toEqual([table]);
  expect(chunkTables({ ...chunk, provenance: undefined }, [table])).toEqual([]);
});
it('binds images by recorded hash or block location without filename guesses', () => {
  expect(chunkAssets(chunk, [asset, { ...asset, id: 'wrong', sha256: 'other' }])).toEqual([asset]);
  expect(localMarkdownAsset('https://example.test/same.png', [asset])).toBeUndefined();
  expect(localMarkdownAsset('../same.png', [asset])).toBeUndefined();
  expect(localMarkdownAsset('images/same.png', [asset, { ...asset, id: 'ambiguous' }])).toBeUndefined();
  expect(localMarkdownAsset('images/same.png', [asset])).toBe(asset);
});

it('binds only WAV media assets by original hash or persisted order, never by filename or MIME wildcard', () => {
  const wave = { ...asset, id: 'audio', name: 'audio-0000.wav', mimeType: 'audio/wav' };
  const located = { ...wave, id: 'located', sha256: 'other', locations: [{ sourceBlockOrder: 5, page: null, caption: '' }] };
  expect(chunkAssets(chunk, [wave, located, { ...wave, id: 'unbound', sha256: 'not-linked' }, { ...wave, id: 'missing-read', readPath: '' },
    { ...wave, id: 'mp3', mimeType: 'audio/mpeg' }, { ...wave, id: 'svg', mimeType: 'image/svg+xml' }, { ...wave, id: 'html', mimeType: 'text/html' }])).toEqual([wave, located]);
  expect(localMarkdownAsset('audio-0000.wav', [{ ...wave, sourcePaths: ['audio-0000.wav'] }])).toBeUndefined();
});

it('keeps unknown and conflicting media source positions unavailable instead of guessing a first block', () => {
  const provenance = { ...chunk.provenance!, kind: 'audio', modality: 'audio' as const, startSeconds: 0, endSeconds: 2,
    sourceBlocks: [{ ...chunk.provenance!.sourceBlocks[0], startSeconds: 0, endSeconds: 2, timestampKind: 'segment-offset' as const }] };
  expect(knowledgeMediaLocationLabel(provenance)).toBe('音频片段 · 00:00–00:02');
  expect(knowledgeMediaPositionLabel({ ...provenance, endSeconds: 3 })).toBeNull();
  expect(knowledgeMediaPositionLabel({ ...provenance, sourceBlocks: [...provenance.sourceBlocks, { ...provenance.sourceBlocks[0], startSeconds: 30, endSeconds: 32 }] })).toBeNull();
  expect(knowledgeMediaLocationLabel({ ...provenance, sourceBlocks: [{ ...provenance.sourceBlocks[0], timestampKind: null }] })).toBe('音频片段 · 时间位置未提供');
  expect(knowledgeMediaPositionLabel({ ...provenance, startSeconds: null, endSeconds: null, sourceBlocks: [] })).toBeNull();
});

it('formats actual media offsets across minute boundaries without implying verified presentation timestamps', () => {
  expect(formatKnowledgeMediaTime(59.9997)).toBe('01:00');
  expect(formatKnowledgeMediaTime(3661.125)).toBe('1:01:01.125');
  expect(formatKnowledgeMediaTime(Number.POSITIVE_INFINITY)).toBe('时间位置未提供');
  expect(knowledgeMediaPositionLabel({ ...chunk.provenance!, startSeconds: 10, endSeconds: 10, timestampKind: 'seek-offset', sourceBlocks: [] })).toBe('00:10（采样偏移）');
});
it('replaces only backend-known HTML tables with safe Markdown and a truncation notice', () => {
  const converted = readableTableMarkdown(`Before\n${table.markdown}\nAfter`, [{ ...table, rows: [['<img src=x>', 'a|b']], truncated: true }]);
  expect(converted).toContain('| Sample | Value |');
  expect(converted).toContain('\\<img src=x\\>');
  expect(converted).toContain('a\\|b');
  expect(converted).toContain('表格预览已截断');
  expect(converted).toContain('Before');
  expect(converted).toContain('After');
  expect(readableTableMarkdown('<table>unknown</table>', [table])).toBe('<table>unknown</table>');
});
it('resolves encoded spaces but refuses ambiguous and encoded nonlocal paths', () => {
  const spaced = { ...asset, sourcePaths: ['images/figure one.png'] };
  expect(localMarkdownAsset('images/figure%20one.png', [spaced])).toBe(spaced);
  expect(localMarkdownAsset('images/figure%20one.png', [spaced, { ...asset, sourcePaths: ['images/figure%20one.png'] }])).toBeUndefined();
  expect(localMarkdownAsset('%2F%2Fremote.test/image.png', [spaced])).toBeUndefined();
  expect(localMarkdownAsset('%2E%2E/image.png', [spaced])).toBeUndefined();
});
it('renders a uniquely matched large table with a truncated backend HTML preview', () => {
  const raw = '<table>' + '<tr><td>Long source cell</td></tr>'.repeat(900) + '</table>';
  const bounded = { ...table, markdown: raw.slice(0, 16384), truncated: true };
  expect(readableTableMarkdown(raw, [bounded])).toContain('| Sample | Value |');
  expect(readableTableMarkdown(raw, [bounded])).not.toContain('<table>');
  expect(readableTableMarkdown(raw, [bounded, { ...bounded, id: 'ambiguous' }])).toBe(raw);
  expect(readableTableMarkdown(raw.slice(0, 8000), [bounded])).toBe(raw.slice(0, 8000));
});
