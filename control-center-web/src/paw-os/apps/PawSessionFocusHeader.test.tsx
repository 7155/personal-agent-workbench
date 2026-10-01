import { render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { PawSessionFocusHeader } from './PawSessionFocusHeader';
it('keeps status and tools without repeating the window title', () => {
  render(<PawSessionFocusHeader title="窗口中已有的标题" busy active hasMessages stopping={false} needsAttention={false} panel="none" onOpenTasks={vi.fn()} onOpenFiles={vi.fn()} onOpenSubagents={vi.fn()} />);
  expect(screen.queryByText('窗口中已有的标题')).not.toBeInTheDocument();
  expect(screen.queryByText('SESSION')).not.toBeInTheDocument();
  expect(screen.getByText('正在执行')).toBeVisible();
  expect(screen.getByRole('button', { name: '打开任务与状态' })).toBeVisible();
  expect(screen.getByRole('button', { name: '打开对话文件' })).toBeVisible();
});
