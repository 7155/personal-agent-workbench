import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { TaskDrawer } from './TaskDrawer';
afterEach(cleanup);
it('is non-modal, focuses its title and closes on Escape', async () => {
  const close=vi.fn();
  render(<TaskDrawer open title="空间分析" onClose={close}><input aria-label="避让参数" /></TaskDrawer>);
  expect(screen.getByRole('heading',{name:'空间分析'})).toHaveFocus();
  expect(screen.getByRole('complementary')).not.toHaveAttribute('aria-modal');
  await userEvent.keyboard('{Escape}');expect(close).toHaveBeenCalledOnce();
});
it('does not discard input contents while hidden', async () => {
  const close=vi.fn(),view=render(<TaskDrawer open title="空间分析" onClose={close}><input aria-label="任务备注" /></TaskDrawer>);
  await userEvent.type(screen.getByLabelText('任务备注'),'保留这份方案');
  view.rerender(<TaskDrawer open={false} title="空间分析" onClose={close}><input aria-label="任务备注" /></TaskDrawer>);
  view.rerender(<TaskDrawer open title="空间分析" onClose={close}><input aria-label="任务备注" /></TaskDrawer>);
  expect(screen.getByLabelText('任务备注')).toHaveValue('保留这份方案');
});
