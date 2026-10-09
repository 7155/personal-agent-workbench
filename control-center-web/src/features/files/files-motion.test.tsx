import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { MotionActivityBoundary } from '@/design/motion';
import { MockControlTransport } from '@/test/mock-transport';
import { PawOsFilesApp } from './PawOsFilesApp';

afterEach(() => { cleanup(); localStorage.clear(); });

it('keeps an owned file read and unsent location draft while motion becomes inactive', async () => {
  let resolveRead!: (value: unknown) => void;
  const transport = new MockControlTransport({ routes: {
    'agent.sessions.list': { ok: true, items: [] },
    'files.list': { ok: true, scope: 'local', path: '/fixture', homePath: '/fixture', items: [{ path: '/fixture/notes.md', name: 'notes.md', kind: 'file' }] },
    'files.read': () => new Promise(resolve => { resolveRead = resolve; }),
  } });
  const query = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrap = (active: boolean) => <QueryClientProvider client={query}><ControlTransportProvider transport={transport}>
    <TooltipProvider><MotionActivityBoundary active={active}><PawOsFilesApp /></MotionActivityBoundary></TooltipProvider>
  </ControlTransportProvider></QueryClientProvider>;
  const view = render(wrap(true));
  fireEvent.click(await screen.findByRole('treeitem', { name: '打开文件 notes.md' }));
  const address = screen.getByRole('textbox', { name: '文件或文件夹路径' });
  fireEvent.change(address, { target: { value: '/unsent location' } });
  address.focus();
  view.rerender(wrap(false));
  expect(view.container.querySelector('.paw-files-app')).toHaveAttribute('data-motion-active', 'false');
  expect(address).toHaveValue('/unsent location'); expect(address).toHaveFocus();
  expect(transport.requests.filter(({ request }) => request.pathId === 'files.read')).toHaveLength(1);
  await act(async () => resolveRead({ ok: true, scope: 'local', requestedPath: '/fixture/notes.md', path: '/fixture/notes.md', content: '# Original result', byteSize: 17, nextOffset: 17, editability: { editable: false } }));
  expect(await screen.findByRole('heading', { name: 'Original result' })).toBeInTheDocument();
  expect(address).toHaveValue('/unsent location'); expect(address).toHaveFocus();
  expect(transport.requests.filter(({ request }) => request.pathId === 'files.read')).toHaveLength(1);
});
