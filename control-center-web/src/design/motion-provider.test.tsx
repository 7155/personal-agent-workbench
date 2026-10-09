import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useEffect, useState } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { MotionActivityBoundary, MotionProvider, useMotionActivity, useMotionPreference } from './motion';

const key = 'rag-ime-control-motion';
let osReduce = false;
let mediaListeners: Set<() => void>;
let previousMedia: PropertyDescriptor | undefined;
beforeEach(() => {
  window.localStorage.clear(); osReduce = false; mediaListeners = new Set();
  const media = {
    get matches() { return osReduce; }, media: '(prefers-reduced-motion: reduce)', onchange: null,
    addEventListener: (_type: string, listener: () => void) => mediaListeners.add(listener),
    removeEventListener: (_type: string, listener: () => void) => mediaListeners.delete(listener),
    addListener: (listener: () => void) => mediaListeners.add(listener),
    removeListener: (listener: () => void) => mediaListeners.delete(listener), dispatchEvent: () => true,
  // The fixture exposes subscription callbacks used by the shared owner;
  // the browser overloads additionally accept event-listener objects.
  } as unknown as MediaQueryList;
  previousMedia = Object.getOwnPropertyDescriptor(window, 'matchMedia');
  Object.defineProperty(window, 'matchMedia', { configurable: true, value: vi.fn(() => media) });
});
afterEach(() => {
  cleanup(); vi.restoreAllMocks();
  if (previousMedia) Object.defineProperty(window, 'matchMedia', previousMedia);
  else Reflect.deleteProperty(window, 'matchMedia');
  window.localStorage.clear();
  delete document.documentElement.dataset.reduceMotion;
  delete document.documentElement.dataset.motionPreference;
});

function Surface({ onMount }: { onMount?: () => void }) {
  const state = useMotionPreference();
  const active = useMotionActivity();
  const [draft, setDraft] = useState('');
  useEffect(() => { onMount?.(); }, [onMount]);
  return <><output data-testid="state" data-preference={state.preference} data-reduced={state.reduceMotion} data-active={active} />
    <input aria-label="未发送草稿" value={draft} onChange={event => setDraft(event.target.value)} />
    <button onClick={() => state.setPreference('reduce')}>本窗减少动效</button></>;
}
function externalPreference(value: string | null, eventKey: string | null = key, area: Storage | null = window.localStorage) {
  act(() => window.dispatchEvent(new StorageEvent('storage', { key: eventKey, newValue: value, storageArea: area })));
}

it('updates the existing surface on external reduce/full/system events without remounting, moving focus or echoing storage', () => {
  const mounted = vi.fn();
  render(<MotionProvider><Surface onMount={mounted} /></MotionProvider>);
  const input = screen.getByRole('textbox', { name: '未发送草稿' });
  fireEvent.change(input, { target: { value: '原输入保留' } }); input.focus();
  const output = screen.getByTestId('state');
  const write = vi.spyOn(Storage.prototype, 'setItem');
  externalPreference('reduce');
  expect(output).toHaveAttribute('data-preference', 'reduce'); expect(output).toHaveAttribute('data-active', 'false');
  expect(document.documentElement.dataset.reduceMotion).toBe('true');
  externalPreference('full'); externalPreference('full');
  expect(output).toHaveAttribute('data-preference', 'full'); expect(output).toHaveAttribute('data-active', 'true');
  expect(document.documentElement.dataset.reduceMotion).toBe('false');
  externalPreference('system');
  expect(document.documentElement.dataset.motionPreference).toBe('system');
  expect(write).not.toHaveBeenCalled(); expect(mounted).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('textbox', { name: '未发送草稿' })).toBe(input);
  expect(input).toHaveFocus(); expect(input).toHaveValue('原输入保留');
});

it('returns to system for removed, cleared or invalid preferences without writing back', () => {
  window.localStorage.setItem(key, 'reduce');
  render(<MotionProvider><Surface /></MotionProvider>);
  const output = screen.getByTestId('state'); const write = vi.spyOn(Storage.prototype, 'setItem');
  externalPreference(null);
  expect(output).toHaveAttribute('data-preference', 'system');
  externalPreference('reduce'); externalPreference(null, null);
  expect(output).toHaveAttribute('data-preference', 'system');
  externalPreference('reduce'); externalPreference('unrecognized');
  expect(output).toHaveAttribute('data-preference', 'system');
  expect(output).toHaveAttribute('data-active', 'true'); expect(write).not.toHaveBeenCalled();
});

it('ignores foreign storage areas and unrelated keys, while own-window selection still persists immediately', () => {
  render(<MotionProvider><Surface /></MotionProvider>);
  externalPreference('reduce', key, window.sessionStorage);
  externalPreference('reduce', key, null);
  externalPreference('reduce', 'rag-ime-control-theme');
  externalPreference(null, null, window.sessionStorage);
  expect(screen.getByTestId('state')).toHaveAttribute('data-preference', 'system');
  fireEvent.click(screen.getByRole('button', { name: '本窗减少动效' }));
  expect(window.localStorage.getItem(key)).toBe('reduce');
  expect(screen.getByTestId('state')).toHaveAttribute('data-preference', 'reduce');
  expect(document.documentElement.dataset.reduceMotion).toBe('true');
});

it('preserves the OS accessibility floor when another window requests full, including live OS changes', () => {
  render(<MotionProvider><Surface /></MotionProvider>);
  act(() => { osReduce = true; for (const listener of mediaListeners) listener(); });
  externalPreference('full');
  expect(screen.getByTestId('state')).toHaveAttribute('data-preference', 'full');
  expect(screen.getByTestId('state')).toHaveAttribute('data-reduced', 'true');
  expect(screen.getByTestId('state')).toHaveAttribute('data-active', 'false');
  expect(document.documentElement.dataset.reduceMotion).toBe('true');
  act(() => { osReduce = false; for (const listener of mediaListeners) listener(); });
  expect(screen.getByTestId('state')).toHaveAttribute('data-active', 'true');
  expect(document.documentElement.dataset.reduceMotion).toBe('false');
});

it('does not activate a hidden or inactive surface when the shared preference becomes full', () => {
  let visibility: DocumentVisibilityState = 'hidden';
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
  try {
    const wrap = (active: boolean) => <MotionProvider><MotionActivityBoundary active={active}><Surface /></MotionActivityBoundary></MotionProvider>;
    const view = render(wrap(false)); externalPreference('reduce'); externalPreference('full');
    expect(screen.getByTestId('state')).toHaveAttribute('data-preference', 'full');
    expect(screen.getByTestId('state')).toHaveAttribute('data-active', 'false');
    visibility = 'visible'; fireEvent(document, new Event('visibilitychange'));
    expect(screen.getByTestId('state')).toHaveAttribute('data-active', 'false');
    view.rerender(wrap(true)); expect(screen.getByTestId('state')).toHaveAttribute('data-active', 'true');
  } finally { Reflect.deleteProperty(document, 'visibilityState'); }
});

it('removes the exact storage listener on unmount and keeps one subscription across preference updates', () => {
  const add = vi.spyOn(window, 'addEventListener'); const remove = vi.spyOn(window, 'removeEventListener');
  const view = render(<MotionProvider><Surface /></MotionProvider>);
  externalPreference('reduce'); externalPreference('full');
  const subscriptions = add.mock.calls.filter(([type]) => type === 'storage');
  expect(subscriptions).toHaveLength(1);
  view.unmount();
  expect(remove.mock.calls.some(([type, listener]) => type === 'storage' && listener === subscriptions[0]?.[1])).toBe(true);
});
