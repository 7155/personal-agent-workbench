import { describe, expect, it } from 'vitest';
import { cellsToCsv, cellsToTsv, fencedPreviewKind, parseDelimited, richFileKind, safeDownloadName } from './rich-data';

describe('rich content data boundaries', () => {
  it('parses escaped quotes, multiline cells, BOM and CRLF', () => {
    expect(parseDelimited('\uFEFFname,value\r\n"a,""b""","one\ntwo"\r\n').rows).toEqual([
      ['name', 'value'], ['a,"b"', 'one\ntwo'],
    ]);
    expect(parseDelimited('a\tb\n1\t2', '\t').rows).toEqual([['a', 'b'], ['1', '2']]);
  });
  it('rejects unfinished and oversized inputs without partial success', () => {
    for (const source of ['"unfinished', '"a"oops,b', 'a'.repeat(200001), 'a\n'.repeat(2002), Array(130).fill('a').join(',')]) {
      expect(parseDelimited(source)).toMatchObject({ rows: [], error: expect.any(String) });
    }
  });
  it('escapes CSV and protects exported text against spreadsheet formulas', () => {
    expect(cellsToCsv([['=1+1', '-12', 'a"b', ' @SUM(A1)']])).toBe('"\'=1+1","-12","a""b","\' @SUM(A1)"');
    expect(cellsToTsv([['a\tb', 'c\nd', '+cmd']])).toBe("a b\tc d\t'+cmd");
  });
  it('identifies documents and media without treating unknown extensions as code', () => {
    for (const [name, kind] of [['a.pdf', 'document'], ['a.docx', 'document'], ['a.xlsx', 'sheet'], ['a.pptx', 'presentation'], ['a.mp4', 'video'], ['a.zip', 'archive'], ['a.ts', 'code'], ['a.patch', 'diff'], ['a.unknown', 'file']]) {
      expect(richFileKind(name!)).toBe(kind);
    }
    expect(safeDownloadName('../../a:b.txt')).toBe('a_b.txt');
    expect(fencedPreviewKind(' LATEX ')).toBe('math');
    expect(fencedPreviewKind('script')).toBeUndefined();
  });
});
