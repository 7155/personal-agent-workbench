import { cleanup, render } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { JevActivityIcon } from './JevActivityIcon';
import type { JevTaskStage } from './jev-execution';

afterEach(cleanup);
it.each<JevTaskStage>(['planning', 'dispatching', 'running', 'verifying', 'synthesizing', 'revising', 'reclaiming'])('animates observed %s activity, and stops when observation is inactive', state => {
  const view = render(<JevActivityIcon state={state} />);
  expect(view.container.firstChild).toHaveAttribute('data-animated');
  view.rerender(<JevActivityIcon state={state} active={false} />);
  expect(view.container.firstChild).not.toHaveAttribute('data-animated');
});
it.each<JevTaskStage>(['queued', 'blocked', 'review', 'unknown', 'done', 'failed', 'cancelled', 'submitted', 'dispatched', 'superseded', 'returned', 'reassigning'])('never suggests live work for %s', state => {
  const view = render(<JevActivityIcon state={state} />);
  expect(view.container.firstChild).not.toHaveAttribute('data-animated');
});
