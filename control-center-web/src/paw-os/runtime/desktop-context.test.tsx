import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { PawDesktopProvider, usePawDesktopApi, usePawDesktopStore } from './desktop-context';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe('PawDesktopProvider persistence safety', () => {
  it('falls back to a clean desktop when the saved JSON is corrupt', () => {
    window.localStorage.setItem('pawos.desktop.v1', '{not-json');

    expect(() => render(<PawDesktopProvider><DesktopProbe /></PawDesktopProvider>)).not.toThrow();
    expect(screen.getByTestId('window-count')).toHaveTextContent('0');
  });

  it('keeps interaction alive when the browser refuses a snapshot write', () => {
    vi.useFakeTimers();
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError'); });
    render(<PawDesktopProvider><DesktopProbe /></PawDesktopProvider>);

    fireEvent.click(screen.getByRole('button', { name: '打开 Agent' }));
    expect(screen.getByTestId('window-count')).toHaveTextContent('1');
    expect(() => act(() => vi.advanceTimersByTime(250))).not.toThrow();
    expect(screen.getByTestId('window-count')).toHaveTextContent('1');
  });

  it('restores only known unique Dock App identities from the saved snapshot', () => {
    window.localStorage.setItem('pawos.desktop.v1', JSON.stringify({
      windows: {},
      stack: [],
      activeWindowId: null,
      dockAppIds: ['system-settings', 'unknown-app', 'system-settings', 'agent'],
    }));

    render(<PawDesktopProvider><DesktopProbe /></PawDesktopProvider>);

    expect(screen.getByTestId('dock-apps')).toHaveTextContent('system-settings,agent');
  });
  it('round-trips a valid Room collaboration focus through desktop persistence', () => {
    vi.useFakeTimers();
    const first = render(<PawDesktopProvider><DesktopProbe /></PawDesktopProvider>);

    fireEvent.click(screen.getByRole('button', { name: '聚焦 Room' }));
    expect(screen.getByTestId('focus-group')).toHaveTextContent('room:room-persist');
    act(() => vi.advanceTimersByTime(250));
    expect(JSON.parse(window.localStorage.getItem('pawos.desktop.v1') ?? '{}')).toMatchObject({
      collaborationFocusGroup: 'room:room-persist',
    });

    first.unmount();
    render(<PawDesktopProvider><DesktopProbe /></PawDesktopProvider>);

    expect(screen.getByTestId('focus-group')).toHaveTextContent('room:room-persist');
  });

  it('drops persisted focus when the matching Room main is not visible', () => {
    window.localStorage.setItem('pawos.desktop.v1', JSON.stringify({
      windows: {
        'agent:room-hidden': {
          id: 'agent:room-hidden',
          appId: 'agent',
          title: 'Room',
          target: { kind: 'room', id: 'room-hidden', title: 'Room', panel: 'focus' },
          bounds: { x: 0, y: 0, width: 500, height: 400 },
          minimized: false,
        },
      },
      stack: ['agent:room-hidden'],
      activeWindowId: 'agent:room-hidden',
      collaborationFocusGroup: 'room:room-hidden',
    }));

    render(<PawDesktopProvider><DesktopProbe /></PawDesktopProvider>);

    expect(screen.getByTestId('focus-group')).toHaveTextContent('none');
  });

  it('keeps a Team scope in its own desktop snapshot namespace', () => {
    vi.useFakeTimers();
    render(<PawDesktopProvider storageKey="pawos.desktop.team.v1:user-1:space-1"><DesktopProbe /></PawDesktopProvider>);

    fireEvent.click(screen.getByRole('button', { name: '打开 Agent' }));

    expect(window.localStorage.getItem('pawos.desktop.v1')).toBeNull();
    expect(window.localStorage.getItem('pawos.desktop.team.v1:user-1:space-1')).toBeNull();
    act(() => vi.runAllTimers());
    expect(JSON.parse(window.localStorage.getItem('pawos.desktop.team.v1:user-1:space-1') ?? '{}')).toMatchObject({
      windows: expect.objectContaining({ agent: expect.any(Object) }),
    });
  });
});

function DesktopProbe() {
  const api = usePawDesktopApi();
  const count = usePawDesktopStore((state) => Object.keys(state.windows).length);
  const dockApps = usePawDesktopStore((state) => state.dockAppIds.join(','));
  const focusGroup = usePawDesktopStore((state) => state.collaborationFocusGroup ?? 'none');
  return <>
    <button onClick={() => api.getState().openApp('agent')} type="button">打开 Agent</button>
    <button onClick={() => {
      api.getState().openApp('agent', {
        entityId: 'room-persist',
        target: { kind: 'room', id: 'room-persist', title: '持久化 Room' },
      });
      api.getState().setCollaborationFocusGroup('room:room-persist');
    }} type="button">聚焦 Room</button>
    <output data-testid="window-count">{count}</output>
    <output data-testid="dock-apps">{dockApps}</output>
    <output data-testid="focus-group">{focusGroup}</output>
  </>;
}
