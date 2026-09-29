import { memo } from 'react';
import type { UiAgentBlock } from '@/contracts/ui-events';
import { AgentFileCollection } from '../file-preview/AgentFileCollection';
import { isHtmlReport } from '../file-preview/file-descriptor';
import { UnknownBlockRenderer } from './MediaRenderers';
import { agentRendererPolicy } from './renderer-registry';

export { MarkdownBody } from './MarkdownRenderer';
export { SafeFieldList } from './StructuredRenderers';

interface AgentBlocksProps {
  allowTraceDiagnosticReceipt?: boolean;
  blocks: UiAgentBlock[];
  onApprovalDecision?: (
    approvalId: string,
    decision: 'approved' | 'rejected',
    hash: string,
  ) => void;
  sessionId?: string;
  streaming?: boolean;
}

export function AgentBlocks({
  allowTraceDiagnosticReceipt = true,
  blocks,
  onApprovalDecision,
  sessionId = '',
  streaming = false,
}: AgentBlocksProps) {
  const tailIndex = streaming ? findLastTextBlock(blocks) : -1;
  const displayEntries = groupFileResults(blocks);
  return (
    <div className="agent-blocks" data-has-stream-tail={tailIndex >= 0 || undefined}>
      {displayEntries.map((entry) => entry.kind === 'files' ? (
        <AgentFileCollection
          blocks={entry.blocks}
          key={`file-results:${entry.firstIndex}`}
          sessionId={sessionId}
        />
      ) : (
        <AgentBlock
          allowTraceDiagnosticReceipt={allowTraceDiagnosticReceipt}
          key={`${entry.block.id}:${entry.index}`}
          block={entry.block}
          onApprovalDecision={onApprovalDecision}
          sessionId={sessionId}
          streamingTail={entry.index === tailIndex}
        />
      ))}
    </div>
  );
}

export const AgentBlock = memo(function AgentBlock({
  allowTraceDiagnosticReceipt = true,
  block,
  onApprovalDecision,
  sessionId = '',
  streamingTail = false,
}: {
  block: UiAgentBlock;
  allowTraceDiagnosticReceipt?: boolean;
  onApprovalDecision?: AgentBlocksProps['onApprovalDecision'];
  sessionId?: string;
  streamingTail?: boolean;
}) {
  const descriptor = agentRendererPolicy(block.type);
  const Renderer = descriptor?.Renderer ?? UnknownBlockRenderer;
  return (
    <Renderer
      allowTraceDiagnosticReceipt={allowTraceDiagnosticReceipt}
      block={block}
      onApprovalDecision={onApprovalDecision}
      sessionId={sessionId}
      streamingTail={streamingTail}
    />
  );
});

function findLastTextBlock(blocks: readonly UiAgentBlock[]) {
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (
      block?.type === 'text'
      && block.status === 'running'
      && typeof (block.data.text ?? block.data.markdown) === 'string'
      && String(block.data.text ?? block.data.markdown)
    ) {
      return index;
    }
  }
  return -1;
}

type BlockDisplayEntry =
  | { kind: 'block'; block: UiAgentBlock; index: number }
  | { kind: 'files'; blocks: UiAgentBlock[]; firstIndex: number };

function groupFileResults(blocks: readonly UiAgentBlock[]): BlockDisplayEntry[] {
  const entries: BlockDisplayEntry[] = [];
  let fileRun: { block: UiAgentBlock; index: number }[] = [];
  const flushFiles = () => {
    if (fileRun.length === 1) {
      entries.push({ kind: 'block', ...fileRun[0]! });
    } else if (fileRun.length > 1) {
      entries.push({
        kind: 'files',
        blocks: fileRun.map(({ block }) => block),
        firstIndex: fileRun[0]!.index,
      });
    }
    fileRun = [];
  };

  blocks.forEach((block, index) => {
    if (collectibleFileBlock(block)) {
      fileRun.push({ block, index });
      return;
    }
    flushFiles();
    entries.push({ kind: 'block', block, index });
  });
  flushFiles();
  return entries;
}

function collectibleFileBlock(block: UiAgentBlock): boolean {
  if (block.type !== 'file') return false;
  // Native video playback retains a dedicated entry instead of becoming a generic file tile.
  if (string(block.data.mimeType).startsWith('video/')) return false;
  const fileName = string(block.data.fileName ?? block.data.name ?? block.data.title);
  return !isHtmlReport(fileName, string(block.data.mimeType));
}

function string(value: unknown): string {
  return typeof value === 'string' ? value : '';
}
