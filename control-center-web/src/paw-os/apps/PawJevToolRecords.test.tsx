import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import { useState } from 'react';
import type { AssistantBlock, AssistantMessage, ToolCallBlock } from '@/features/conversation-ui';
import { toolExecutionOutcome } from '@/features/conversation-ui/model/tool-receipt';
import { jevToolGroups, PawJevToolRecordDialog, PawJevToolRecords } from './PawJevToolRecords';

afterEach(cleanup);
const tool = (id: string): ToolCallBlock => ({ id, kind: 'tool', name: `读取 ${id}`, status: 'success', input: id, output: `原始回执 ${id}` });
const turn = (id: string, blocks: AssistantBlock[]): AssistantMessage => ({ id, role: 'assistant', timestamp: 1, blocks });
function Records({ blocks, showOpener = true }: { blocks: ToolCallBlock[]; showOpener?: boolean }) {
  const [selected, setSelected] = useState<ToolCallBlock[] | null>(null);
  return <>{showOpener ? <PawJevToolRecords blocks={blocks} onOpen={setSelected} /> : null}<PawJevToolRecordDialog open={Boolean(selected)} blocks={selected ?? []} onClose={() => setSelected(null)} renderDetail={() => undefined} /></>;
}

describe('JEV compact tool history', () => {
  it('preserves chronology, text boundaries, actor turns and visible actions', () => {
    const groups = jevToolGroups([
      turn('earth', [tool('1'), tool('2'), { kind: 'text', id: 'text', text: '关键进展' }, tool('3'), tool('approval'), tool('4'), tool('5'), tool('6')]),
      turn('mars', [tool('7')]),
    ], block => block.id === 'approval');
    expect(groups.get('1')?.map(block => block.id)).toEqual(['1', '2']);
    expect(groups.get('2')).toBeNull();
    expect(groups.has('3')).toBe(false);
    expect(groups.has('approval')).toBe(false);
    expect(groups.get('4')?.map(block => block.id)).toEqual(['4', '5', '6']);
    expect(groups.has('7')).toBe(false);
  });

  it('opens original receipts in a dialog, exposes uncertainty and returns keyboard focus', async () => {
    const user = userEvent.setup();
    render(<Records blocks={[tool('1'), { ...tool('2'), status: 'error', executionOutcome: 'unknown', summary: 'Tool gateway request timed out after 30000ms' }]} />);
    const opener = screen.getByRole('button', { name: /工具记录 · 2 项/ });
    expect(opener).toHaveTextContent('1 项回执待核实');
    expect(opener).not.toHaveTextContent('失败');
    expect(screen.queryByText('原始回执 1')).not.toBeInTheDocument();
    await user.click(opener);
    const dialog = screen.getByRole('dialog', { name: /工具记录/ });
    expect(within(dialog).getByText('回执待核实')).toBeVisible();
    expect(within(dialog).getByText('Tool gateway request timed out after 30000ms')).toBeVisible();
    await user.click(within(dialog).getByRole('button', { name: /读取 1/ }));
    expect(within(dialog).getByText('原始回执 1')).toBeVisible();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('reads structured outcome and exact legacy errors without trusting written content', () => {
    expect(toolExecutionOutcome({ result: { details: { executionOutcome: 'unknown' } } })).toBe('unknown');
    expect(toolExecutionOutcome({ result: { details: { executionOutcome: 'applied' } }, error: 'Tool gateway request timed out after 30000ms' })).toBe('applied');
    expect(toolExecutionOutcome({ details: { executionOutcome: 'not_started' } })).toBe('not_started');
    expect(toolExecutionOutcome({ error: 'Tool gateway request timed out after 30000ms' })).toBe('unknown');
    expect(toolExecutionOutcome({ arguments: { executionOutcome: 'unknown' }, result: { outputPreview: 'Tool gateway request timed out after 30000ms' } })).toBeUndefined();
    expect(toolExecutionOutcome({ error: 'file not found' })).toBeUndefined();
  });
  it('keeps cancelled and unsent calls distinct from awaiting a sent receipt', () => {
    render(<Records blocks={[{ ...tool('1'), status: 'cancelled' }, { ...tool('2'), executionOutcome: 'not_started' }]} />);
    const opener = screen.getByRole('button', { name: /工具记录/ });
    expect(opener).toHaveTextContent('1 项已停止');
    expect(opener).toHaveTextContent('1 项尚未执行');
    expect(opener).not.toHaveTextContent('等待执行回执');
    expect(opener).not.toHaveTextContent('项完成');
  });
  it('keeps the evidence window open when a virtual row is removed by a live update', async () => {
    const user = userEvent.setup();
    const { rerender } = render(<Records blocks={[tool('1'), tool('2')]} />);
    await user.click(screen.getByRole('button', { name: /工具记录/ }));
    rerender(<Records blocks={[tool('3')]} showOpener={false} />);
    expect(screen.getByRole('dialog', { name: /工具记录 · 2 项/ })).toBeVisible();
    await user.click(screen.getByRole('button', { name: /读取 2/ }));
    expect(screen.getByText('原始回执 2')).toBeVisible();
  });
});
