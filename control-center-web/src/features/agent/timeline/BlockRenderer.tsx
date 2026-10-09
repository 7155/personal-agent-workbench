import { memo } from 'react';
import type { UiAgentBlock } from '@/contracts/ui-events';
import { AgentFileCollection } from '../file-preview/AgentFileCollection';
import { MessageFileReferences } from './MarkdownRenderer';
import { UnknownBlockRenderer } from './MediaRenderers';
import { agentRendererPolicy } from './renderer-registry';
import { groupConversationEntries } from './conversation-content-groups';
import { ConversationImageGallery } from './rich/ConversationImageGallery';

export { MarkdownBody } from './MarkdownRenderer';
export { SafeFieldList } from './StructuredRenderers';

interface AgentBlocksProps {
  allowTraceDiagnosticReceipt?: boolean;
  blocks: UiAgentBlock[];
  onApprovalDecision?: (approvalId: string, decision: 'approved' | 'rejected', hash: string) => void;
  sessionId?: string;
  streaming?: boolean;
}
export function AgentBlocks({ allowTraceDiagnosticReceipt = true, blocks, onApprovalDecision, sessionId = '', streaming = false }: AgentBlocksProps) {
  const tailIndex = streaming ? findLastTextBlock(blocks) : -1;
  const displayEntries = groupConversationEntries(blocks);
  return <MessageFileReferences blocks={blocks} sessionId={sessionId}><div className="agent-blocks" data-has-stream-tail={tailIndex >= 0 || undefined}>
    {displayEntries.map(entry => entry.kind === 'images' ? (
      <ConversationImageGallery key={`image-results:${entry.blocks[0]?.id ?? entry.firstIndex}`} blocks={entry.blocks} sessionId={sessionId} />
    ) : entry.kind === 'files' ? (
      <AgentFileCollection blocks={entry.blocks} key={`file-results:${entry.blocks[0]?.id ?? entry.firstIndex}`} sessionId={sessionId} />
    ) : (
      <AgentBlock allowTraceDiagnosticReceipt={allowTraceDiagnosticReceipt} key={`${entry.block.id}:${entry.index}`}
        block={entry.block} onApprovalDecision={onApprovalDecision} sessionId={sessionId} streamingTail={entry.index === tailIndex} />
    ))}
  </div></MessageFileReferences>;
}
export const AgentBlock = memo(function AgentBlock({ allowTraceDiagnosticReceipt = true, block, onApprovalDecision, sessionId = '', streamingTail = false }: {
  block: UiAgentBlock; allowTraceDiagnosticReceipt?: boolean; onApprovalDecision?: AgentBlocksProps['onApprovalDecision']; sessionId?: string; streamingTail?: boolean;
}) {
  const descriptor = agentRendererPolicy(block.type);
  const Renderer = descriptor?.Renderer ?? UnknownBlockRenderer;
  return <Renderer allowTraceDiagnosticReceipt={allowTraceDiagnosticReceipt} block={block} onApprovalDecision={onApprovalDecision} sessionId={sessionId} streamingTail={streamingTail} />;
});
function findLastTextBlock(blocks: readonly UiAgentBlock[]) {
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (block?.type === 'text' && block.status === 'running'
      && typeof (block.data.text ?? block.data.markdown) === 'string' && String(block.data.text ?? block.data.markdown)) return index;
  }
  return -1;
}
