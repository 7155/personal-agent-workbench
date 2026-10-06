import { describe, expect, it } from 'vitest';
import { compactContractText, textCodePointCount, trimContractText } from './text-budget';

describe('backend text budgets', () => {
  it('counts Unicode code points and keeps meaningful internal whitespace', () => {
    expect(textCodePointCount('中🙂')).toBe(2);
    expect(trimContractText(' \n中\t🙂\n ')).toBe('中\t🙂');
    expect(compactContractText(' \n中\t🙂\n ')).toBe('中 🙂');
  });
  it('matches Python whitespace boundaries without silently removing BOM', () => {
    expect(trimContractText('\u0085\u001c x \u001f\u0085')).toBe('x');
    expect(compactContractText('a\u0085\u001cb')).toBe('a b');
    expect(trimContractText('\uFEFFx\uFEFF')).toBe('\uFEFFx\uFEFF');
    expect(compactContractText('\uFEFF')).toBe('\uFEFF');
  });
});
