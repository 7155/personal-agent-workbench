import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

const pendingShell = vi.hoisted(() => {
  let resolve!: (value: unknown) => void;
  const promise = new Promise(value => { resolve = value; });
  return { promise, resolve };
});
vi.mock('@/paw-os/PawOsApp', () => pendingShell.promise);

import { App } from './App';

afterEach(() => { cleanup(); vi.unstubAllEnvs(); });

it('keeps a visible loading surface until the real desktop module is ready', async () => {
  vi.stubEnv('VITE_CONTROL_TRANSPORT', 'mock');
  render(<App />);
  const pending = screen.getByRole('status', { name: '正在打开工作台' });
  expect(pending).toHaveTextContent('正在准备工作台');
  expect(pending).toHaveTextContent('正在载入界面');
  expect(screen.queryByText('工作台已经打开')).not.toBeInTheDocument();
  await act(async () => { pendingShell.resolve({ PawOsApp: () => <main>工作台已经打开</main> }); });
  expect(await screen.findByText('工作台已经打开')).toBeVisible();
  expect(screen.queryByRole('status', { name: '正在打开工作台' })).not.toBeInTheDocument();
});
