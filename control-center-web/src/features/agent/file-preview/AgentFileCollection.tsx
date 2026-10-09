import type { UiAgentBlock } from '@/contracts/ui-events';
import { AgentFileBlock } from './AgentFileBlock';
import './file-preview.css';

interface AgentFileCollectionProps {
  blocks: UiAgentBlock[];
  sessionId?: string;
}

export function AgentFileCollection({ blocks, sessionId = '' }: AgentFileCollectionProps) {
  const seen = new Set<string>();
  const receipts = blocks.filter(block => {
    // Same name or bytes do not prove a shared file/version lineage. Preserve
    // every distinct receipt, including diffs, and collapse only exact repeats.
    const identity = fileReceiptIdentity(block, sessionId);
    if (seen.has(identity)) return false;
    seen.add(identity);
    return true;
  });
  return <section aria-label="结果文件" className="agent-file-collection">
    {receipts.map(block => <AgentFileBlock data={block.data} key={fileReceiptIdentity(block, sessionId)} sessionId={sessionId} />)}
  </section>;
}

function fileReceiptIdentity(block: UiAgentBlock, sessionId: string): string {
  return JSON.stringify([block.data.sessionId ?? sessionId,
    block.data.mediaId ?? block.data.receiptUrl ?? block.id, block.data.sha256 ?? block.digest ?? '']);
}
