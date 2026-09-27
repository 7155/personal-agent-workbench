import { ChevronRight, ListChecks } from 'lucide-react';
import type { ReactNode } from 'react';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/primitives';
import { ToolCard, type AssistantBlock, type ToolCallBlock, type TranscriptMessage } from '@/features/conversation-ui';
import { ToolStatusMark } from '@/features/conversation-ui/components/ToolStatusMark';
import { toolReceiptPresentation } from '@/features/conversation-ui/model/tool-receipt';
import './paw-jev-conversation.css';

/** Presentation-only grouping. Never cross text, thinking, actors or turns;
 * approval and background-process actions stay on the central transcript. */
export function jevToolGroups(messages: TranscriptMessage[], keepVisible: (block: ToolCallBlock) => boolean) {
  const groups = new Map<string, ToolCallBlock[] | null>();
  for (const message of messages) {
    if (message.role !== 'assistant') continue;
    let current: ToolCallBlock[] = [];
    const flush = () => {
      if (current.length > 1) {
        groups.set(current[0].id, current);
        current.slice(1).forEach(block => groups.set(block.id, null));
      }
      current = [];
    };
    for (const block of message.blocks) {
      if (block.kind !== 'tool' || keepVisible(block)) { flush(); continue; }
      current.push(block);
    }
    flush();
  }
  return groups;
}

function recordSummary(blocks: ToolCallBlock[]) {
  const outcomes = blocks.map(toolReceiptPresentation);
  const unknown = blocks.filter(block => block.executionOutcome === 'unknown').length;
  const failed = outcomes.filter(receipt => receipt.status === 'error').length;
  const running = outcomes.filter(receipt => receipt.status === 'running').length;
  const complete = outcomes.filter(receipt => receipt.status === 'success').length;
  const cancelled = outcomes.filter(receipt => receipt.status === 'cancelled').length;
  const notStarted = blocks.filter(block => block.executionOutcome === 'not_started').length;
  const pending = outcomes.filter(receipt => receipt.status === 'pending').length - unknown - notStarted;
  const latest = [...blocks].reverse().find(block => block.status === 'running') ?? blocks.at(-1);
  const status = running ? 'running' : unknown || pending || notStarted ? 'pending' : failed ? 'error' : cancelled ? 'cancelled' : 'success';
  const counts = [unknown ? `${unknown} 项回执待核实` : '', failed ? `${failed} 项失败` : '', running ? `${running} 项执行中` : '', cancelled ? `${cancelled} 项已停止` : '', notStarted ? `${notStarted} 项尚未执行` : '', pending ? `${pending} 项等待` : '', complete ? `${complete} 项完成` : ''].filter(Boolean).join(' · ');
  return { status, counts, latest } as const;
}

export function PawJevToolRecords({ blocks, onOpen }: { blocks: ToolCallBlock[]; onOpen: (blocks: ToolCallBlock[]) => void }) {
  const { status, counts, latest } = recordSummary(blocks);
  return <button className="paw-jev-tool-records__trigger" type="button" aria-haspopup="dialog" onClick={() => onOpen(blocks)}>
        <ToolStatusMark status={status} />
        <span><strong>工具记录 · {blocks.length} 项</strong><small>{counts || '等待执行回执'}</small></span>
        <span className="paw-jev-tool-records__latest" title={latest?.summary || latest?.name}>{latest?.summary || latest?.name}</span>
        <ChevronRight size={15} aria-hidden="true" />
      </button>;
}

/** Mounted by the conversation owner, outside virtual rows. Live updates may
 * remove an opener from the DOM without dismissing its evidence window. */
export function PawJevToolRecordDialog({ blocks, open, onClose, renderDetail }: {
  blocks: ToolCallBlock[];
  open: boolean;
  onClose: () => void;
  renderDetail: (block: AssistantBlock) => ReactNode;
}) {
  const { counts } = recordSummary(blocks);
  return <Dialog open={open} onOpenChange={value => { if (!value) onClose(); }}>
    <DialogContent className="ccui-tool-records paw-jev-tool-records">
      <DialogTitle><ListChecks size={18} aria-hidden="true" /> 工具记录 · {blocks.length} 项</DialogTitle>
      <DialogDescription>{counts || '等待执行回执'}。按发生顺序查看调用和原始证据。</DialogDescription>
      <div className="paw-jev-tool-records__list">
        {blocks.map(block => <ToolCard key={block.id} block={block} detail={renderDetail(block)} />)}
      </div>
    </DialogContent>
  </Dialog>;
}
