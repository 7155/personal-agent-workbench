import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { PawResultWindow } from './PawResultWindow';

afterEach(cleanup);

it('uses the isolated preview policy for a large interactive HTML result window', () => {
  render(<PawResultWindow target={{
    kind: 'result', id: 'large-report', resultKind: 'html', title: 'Large report',
    content: '<button onclick="this.textContent=\'clicked\'">Run</button>' + 'x'.repeat(1_600_000),
  }} />);
  const frame = screen.getByTitle('Large report');
  expect(frame).toHaveAttribute('src', expect.stringMatching(/^\/__paw_html_preview#message:[0-9a-f-]{36}$/u));
  expect(frame).not.toHaveAttribute('srcdoc');
  expect(frame.getAttribute('sandbox')).toContain('allow-scripts');
  expect(frame.getAttribute('sandbox')).not.toContain('allow-same-origin');
});
