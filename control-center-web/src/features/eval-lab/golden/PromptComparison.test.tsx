import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { PromptComparison } from './PromptComparison';
afterEach(cleanup);
it('preserves both frozen bodies and exposes the complete replacement diff', () => {
  render(<PromptComparison baseline={'旧规则\n保留空行\n'} candidate={'新规则\n引用原文'} />);
  expect(screen.getByText('基线 Prompt')).toBeVisible(); expect(screen.getByText('候选 Prompt')).toBeVisible();
  fireEvent.click(screen.getByText('查看完整替换差异'));
  expect(screen.getByText(/− 旧规则/)).toBeVisible(); expect(screen.getByText(/\+ 新规则/)).toBeVisible();
});
it('keeps absent and intentionally empty prompts distinct without inventing a diff', () => {
  render(<PromptComparison baseline={undefined} candidate="" />);
  expect(screen.getByText('正文未完整返回')).toBeVisible();
  expect(screen.getByText('未附加独立 Prompt（空正文）')).toBeVisible();
  expect(screen.queryByText('查看完整替换差异')).not.toBeInTheDocument();
});
