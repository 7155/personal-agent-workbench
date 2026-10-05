// Match Python str.strip()/re \s used by the form endpoints. JavaScript's
// whitespace set differs (notably NEL, record separators and BOM).
const boundaryWhitespace = /^[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]+|[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]+$/gu;
const whitespace = /[\u0009-\u000D\u001C-\u0020\u0085\u00A0\u1680\u2000-\u200A\u2028\u2029\u202F\u205F\u3000]+/gu;

export function trimContractText(text: string): string { return text.replace(boundaryWhitespace, ''); }
export function compactContractText(text: string): string { return trimContractText(text.replace(whitespace, ' ')); }
export function textCodePointCount(text: string): number { return [...text].length; }
