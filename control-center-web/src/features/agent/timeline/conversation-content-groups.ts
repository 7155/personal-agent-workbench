import type { UiAgentBlock } from '@/contracts/ui-events';
import { isHtmlReport } from '../file-preview/file-descriptor';
export type ConversationContentEntry =
  | { kind: 'block'; block: UiAgentBlock; index: number }
  | { kind: 'files'; blocks: UiAgentBlock[]; firstIndex: number }
  | { kind: 'images'; blocks: UiAgentBlock[]; firstIndex: number };
export function isConversationImage(block: UiAgentBlock): boolean {
  return block.type === 'image' || block.type === 'file' && text(block.data.mimeType).startsWith('image/');
}
/** Group adjacent contents inside ONE message. Never reorder text or merge across a tool/status boundary. */
export function groupConversationEntries(blocks: readonly UiAgentBlock[]): ConversationContentEntry[] {
  const entries: ConversationContentEntry[] = [];
  let run: { block: UiAgentBlock; index: number }[] = []; let runKind: 'files' | 'images' | null = null;
  function flush() {
    const first = run[0]; if (!first) return;
    // A one-image run already uses the gallery: adding the next image must not unmount an open reader.
    if (runKind === 'images') entries.push({ kind: 'images', blocks: run.map(item => item.block), firstIndex: first.index });
    else if (run.length === 1) entries.push({ kind: 'block', ...first });
    else entries.push({ kind: 'files', blocks: run.map(item => item.block), firstIndex: first.index });
    run = []; runKind = null;
  }
  blocks.forEach((block, index) => {
    const image = isConversationImage(block);
    const file = block.type === 'file' && !image && !text(block.data.mimeType).startsWith('video/')
      && !isHtmlReport(text(block.data.fileName ?? block.data.name ?? block.data.title), text(block.data.mimeType));
    const kind = image && block.status === 'completed' ? 'images' : file ? 'files' : null;
    if (!kind) { flush(); entries.push({ kind: 'block', block, index }); return; }
    if (runKind && runKind !== kind) flush(); runKind = kind; run.push({ block, index });
  });
  flush(); return entries;
}
function text(value: unknown): string { return typeof value === 'string' ? value : ''; }
