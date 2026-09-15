import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { WorkspaceProjectContext } from './WorkspaceProjectContext';
import type { WorkspaceComposerContext } from './workspace-draft';

const context = (onOpen: () => void, onClear: () => void): WorkspaceComposerContext => ({
  kind: 'project', label: '论文对照报告', detail: 'v4 · 随消息发送', text: '{}', onOpen, onClear,
});

describe('WorkspaceProjectContext', () => {
  it('exposes a left-result jump and clears the reference', () => {
    const onOpen = vi.fn(); const onClear = vi.fn();
    render(<WorkspaceProjectContext context={context(onOpen, onClear)} />);
    fireEvent.click(screen.getByRole('button', { name: '查看左侧对应结果：论文对照报告' }));
    fireEvent.click(screen.getByRole('button', { name: '移除本次项目上下文' }));
    expect(onOpen).toHaveBeenCalledTimes(1); expect(onClear).toHaveBeenCalledTimes(1);
    expect(screen.getByText('查看左侧')).toBeVisible();
  });
});
