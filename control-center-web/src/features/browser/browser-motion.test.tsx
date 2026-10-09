import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { MotionActivityBoundary } from '@/design/motion';
import { PawOsAppSurfaceProvider } from '@/features/paw-os/surface-context';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import { PawBrowserApp } from '@/paw-os/apps/PawBrowserApp';
import { BrowserTabStrip } from './BrowserTabStrip';

afterEach(() => { cleanup(); localStorage.clear(); });

it('keeps loading identity and the focused tab while its surface motion becomes inactive', () => {
  const tabs = [{ id: 'reading', title: 'Reading document', active: true, loading: true }];
  const props = { tabs, inWindowChrome: true, onSelect: () => undefined, onNewTab: () => undefined, onClose: () => undefined };
  const view = render(<MotionActivityBoundary active><BrowserTabStrip {...props} /></MotionActivityBoundary>);
  const tab = screen.getByRole('tab', { name: 'Reading document' });
  tab.focus();
  expect(tab.closest('.paw-browser-tabstrip')).toHaveAttribute('data-motion-active', 'true');
  view.rerender(<MotionActivityBoundary active={false}><BrowserTabStrip {...props} /></MotionActivityBoundary>);
  expect(tab.closest('.paw-browser-tabstrip')).toHaveAttribute('data-motion-active', 'false');
  expect(tab).toHaveAttribute('aria-busy', 'true');
  expect(tab).toHaveAttribute('title', 'Reading document（正在加载）');
  expect(tab).toHaveFocus();
  view.rerender(<MotionActivityBoundary active><BrowserTabStrip {...props} tabs={[{ ...tabs[0], loading: false }]} /></MotionActivityBoundary>);
  expect(tab).not.toHaveAttribute('aria-busy');
  expect(tab.querySelector('.ui-spin')).toBeNull();
});

it('quietens Browser chrome when the document is hidden without changing tab load state', () => {
  let visibility: DocumentVisibilityState = 'visible';
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
  try {
    render(<BrowserTabStrip tabs={[{ id: 'reading', title: 'Reading document', active: true, loading: true }]}
      onSelect={() => undefined} onNewTab={() => undefined} onClose={() => undefined} />);
    const tab = screen.getByRole('tab', { name: 'Reading document' });
    visibility = 'hidden'; fireEvent(document, new Event('visibilitychange'));
    expect(tab.closest('.paw-browser-tabstrip')).toHaveAttribute('data-motion-active', 'false');
    expect(tab).toHaveAttribute('aria-busy', 'true');
    visibility = 'visible'; fireEvent(document, new Event('visibilitychange'));
    expect(tab.closest('.paw-browser-tabstrip')).toHaveAttribute('data-motion-active', 'true');
  } finally { Reflect.deleteProperty(document, 'visibilityState'); }
});

it('projects the existing inactive window boundary to the Browser workspace', () => {
  render(<ControlTransportProvider transport={new MockControlTransport()}>
    <PawOsAppSurfaceProvider active={false} appId="browser" width={1080} height={720}>
      <PawBrowserApp />
    </PawOsAppSurfaceProvider>
  </ControlTransportProvider>);
  expect(screen.getByRole('region', { name: 'Browser' })).toHaveAttribute('data-motion-active', 'false');
});

it('retains the original Browser trace and controls when its feedback becomes quiet', async () => {
  const transport = new MockControlTransport({ routes: {
    'browser.managed.start': { ok: true },
    'browser.tabs': { ok: true, items: [{ tabId: 41, title: 'Original page', url: 'about:blank' }] },
    'browser.traces': { ok: true, items: [{ commandId: 'original-command', sourceKind: 'agent', status: 'claimed', target: 'Original Browser task' }] },
  } });
  const wrap = (active: boolean) => <ControlTransportProvider transport={transport}>
    <MotionActivityBoundary active={active}><PawBrowserApp /></MotionActivityBoundary>
  </ControlTransportProvider>;
  const view = render(wrap(true));
  const task = await screen.findByRole('status', { name: 'Agent 浏览器任务状态' });
  const stop = within(screen.getByRole('region', { name: 'Agent 浏览器任务' })).getByRole('button', { name: '停止 Agent 浏览器操作' });
  stop.focus();
  const requestCount = transport.requests.length;
  view.rerender(wrap(false));
  expect(screen.getByRole('region', { name: 'Browser' })).toHaveAttribute('data-motion-active', 'false');
  expect(task).toHaveTextContent('Original Browser task');
  expect(task).toHaveTextContent('Agent 正在浏览');
  expect(stop).toHaveFocus();
  expect(stop).not.toBeDisabled();
  expect(transport.requests).toHaveLength(requestCount);
  expect(transport.requests.some(({ request }) => ['browser.stop', 'browser.command'].includes(request.pathId))).toBe(false);
});
