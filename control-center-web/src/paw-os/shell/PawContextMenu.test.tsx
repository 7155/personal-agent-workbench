import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MotionActivityBoundary } from '@/design/motion';
import { PawContextMenu } from './PawContextMenu';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('remeasures the existing menu after viewport resize without losing original item focus or invoking an action', () => {
  let width = 900;
  let height = 700;
  vi.spyOn(window, 'innerWidth', 'get').mockImplementation(() => width);
  vi.spyOn(window, 'innerHeight', 'get').mockImplementation(() => height);
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ width: 220, height: 400, top: 0, left: 0, bottom: 400, right: 220, x: 0, y: 0, toJSON: () => ({}) });
  const action = vi.fn();
  render(<PawContextMenu ariaLabel="原窗口菜单" items={Array.from({ length: 10 }, (_, index) => ({ id: `original-${index}`, label: `原窗口 ${index}`, action }))} onClose={() => undefined} x={800} y={500} />);
  const menu = screen.getByRole('menu');
  fireEvent.keyDown(menu, { key: 'End' });
  const original = screen.getByRole('menuitem', { name: '原窗口 9' });
  expect(original).toHaveFocus();
  width = 375; height = 230;
  fireEvent(window, new Event('resize'));
  expect(Number.parseFloat(menu.style.left)).toBeLessThanOrEqual(147);
  expect(Number.parseFloat(menu.style.top)).toBe(48);
  expect(original).toHaveFocus();
  expect(action).not.toHaveBeenCalled();
});

it('keeps the original menu controls and focus while the existing motion boundary becomes quiet', () => {
  const action = vi.fn();
  const props = { ariaLabel: '原窗口菜单', items: [{ id: 'original', label: '原窗口', action }], onClose: vi.fn(), x: 80, y: 80 };
  const view = render(<MotionActivityBoundary active><PawContextMenu {...props} /></MotionActivityBoundary>);
  const item = screen.getByRole('menuitem', { name: '原窗口' });
  item.focus();
  view.rerender(<MotionActivityBoundary active={false}><PawContextMenu {...props} /></MotionActivityBoundary>);
  expect(screen.getByRole('menu')).toHaveAttribute('data-motion-active', 'false');
  expect(item).toHaveFocus();
  fireEvent.keyDown(item, { key: 'Escape' });
  expect(props.onClose).toHaveBeenCalledWith('keyboard');
  expect(action).not.toHaveBeenCalled();
});

it('keeps original menu identity while hidden-document motion is quiet and removes its resize listener on close', () => {
  let visibility: DocumentVisibilityState = 'visible';
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => visibility });
  try {
    const close = vi.fn();
    const remove = vi.spyOn(window, 'removeEventListener');
    const view = render(<PawContextMenu ariaLabel="原菜单" items={[{ id: 'original', label: '原窗口', action: vi.fn() }]} onClose={close} x={80} y={80} />);
    const menu = screen.getByRole('menu');
    const item = screen.getByRole('menuitem');
    item.focus();
    visibility = 'hidden'; fireEvent(document, new Event('visibilitychange'));
    expect(menu).toHaveAttribute('data-motion-active', 'false');
    expect(item).toHaveFocus();
    expect(close).not.toHaveBeenCalled();
    view.unmount();
    expect(remove.mock.calls.some(([name]) => name === 'resize')).toBe(true);
  } finally { Reflect.deleteProperty(document, 'visibilityState'); }
});

it('enters the original first item only after the measured menu becomes visible in the browser', () => {
  const focus = HTMLElement.prototype.focus;
  // Model native focus rejection for a hidden first-paint menu. jsdom alone
  // otherwise accepts focus on visibility:hidden elements, masking this path.
  vi.spyOn(HTMLElement.prototype, 'focus').mockImplementation(function(this: HTMLElement, options?: FocusOptions) {
    const menu = this.closest<HTMLElement>('[role="menu"]');
    if (menu?.style.visibility === 'hidden') return;
    focus.call(this, options);
  });
  render(<PawContextMenu ariaLabel="原菜单" items={[{ id: 'original', label: '原窗口', action: vi.fn() }]} onClose={() => undefined} x={80} y={80} />);
  expect(screen.getByRole('menuitem', { name: '原窗口' })).toHaveFocus();
});


it('places the resting layout box inside the viewport while the entrance transform scales its painted rectangle', () => {
  vi.spyOn(window, 'innerWidth', 'get').mockReturnValue(375);
  vi.spyOn(window, 'innerHeight', 'get').mockReturnValue(230);
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(220);
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(174);
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ width: 214.5, height: 169.65, top: 0, left: 0, bottom: 169.65, right: 214.5, x: 0, y: 0, toJSON: () => ({}) });
  const action = vi.fn();
  render(<PawContextMenu ariaLabel="原窗口菜单" items={[{ id: 'original', label: '原窗口', action }]} onClose={() => undefined} x={365} y={220} />);
  const menu = screen.getByRole('menu');
  expect(Number.parseFloat(menu.style.left) + menu.offsetWidth).toBeLessThanOrEqual(367);
  expect(Number.parseFloat(menu.style.top) + menu.offsetHeight).toBeLessThanOrEqual(222);
  expect(screen.getByRole('menuitem', { name: '原窗口' })).toHaveFocus();
  expect(action).not.toHaveBeenCalled();
});


it.each([360, 375])('reveals the same focused item after shrinking the viewport to %ipx wide, even when placement is unchanged', (nextWidth) => {
  let width = 375; let height = 230; let revealed = false;
  vi.spyOn(window, 'innerWidth', 'get').mockImplementation(() => width);
  vi.spyOn(window, 'innerHeight', 'get').mockImplementation(() => height);
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(220);
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockImplementation(() => height - 56);
  const rectangle = (top: number, bottom: number) => ({ width: 220, height: bottom - top, top, left: 0, bottom, right: 220, x: 0, y: top, toJSON: () => ({}) });
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function(this: HTMLElement) {
    if (this.getAttribute('role') === 'menu') return rectangle(48, height - 8);
    return revealed ? rectangle(height - 38, height - 8) : rectangle(191, 221);
  });
  const scroll = vi.spyOn(HTMLElement.prototype, 'scrollIntoView').mockImplementation(() => { revealed = true; });
  const action = vi.fn();
  render(<PawContextMenu ariaLabel="原窗口菜单" items={[{ id: 'first', label: '首项', action }, { id: 'last', label: '原关闭窗口', action }]} onClose={() => undefined} x={345} y={190} />);
  const menu = screen.getByRole('menu'); const last = screen.getByRole('menuitem', { name: '原关闭窗口' });
  fireEvent.keyDown(menu, { key: 'End' }); expect(last).toHaveFocus();
  fireEvent(window, new Event('resize')); expect(scroll).not.toHaveBeenCalled();
  width = nextWidth; height = 210; fireEvent(window, new Event('resize'));
  expect(scroll).toHaveBeenCalledExactlyOnceWith({ block: 'nearest', inline: 'nearest' });
  expect(last).toHaveFocus(); expect(last.getBoundingClientRect().bottom).toBeLessThanOrEqual(menu.getBoundingClientRect().bottom);
  expect(menu.style.top).toBe('48px');
  fireEvent(window, new Event('resize')); expect(scroll).toHaveBeenCalledTimes(1);
  expect(action).not.toHaveBeenCalled();
});

// Model actual native rejection after inline placement until one natural RAF.
function rejectedMenu() {
  let visible = false; let id = 0;
  const frames = new Map<number, FrameRequestCallback>();
  const request = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => { frames.set(++id, callback); return id; });
  const cancel = vi.spyOn(window, 'cancelAnimationFrame').mockImplementation((frame) => { frames.delete(frame); });
  const nativeFocus = HTMLElement.prototype.focus;
  const focus = vi.spyOn(HTMLElement.prototype, 'focus').mockImplementation(function(this: HTMLElement, options?: FocusOptions) {
    if (!visible && this.closest('[role="menu"]')) return;
    nativeFocus.call(this, options);
  });
  const nativeStyle = window.getComputedStyle;
  vi.spyOn(window, 'getComputedStyle').mockImplementation((element, pseudo) => {
    const style = nativeStyle.call(window, element, pseudo);
    return !visible && element.getAttribute('role') === 'menu'
      ? new Proxy(style, { get: (target, name) => name === 'visibility' ? 'hidden' : Reflect.get(target, name, target) }) : style;
  });
  const opener = document.createElement('button'); opener.textContent = '原入口'; document.body.append(opener); opener.focus();
  const action = vi.fn(); const close = vi.fn();
  const view = render(<PawContextMenu ariaLabel="自然帧菜单" items={[
    { id: 'disabled', label: '不可用项', disabled: true, action },
    { id: 'first', label: '原首项', action }, { id: 'last', label: '原末项', action },
  ]} onClose={close} x={80} y={80} />);
  return { opener, action, close, view, request, cancel, focus, frames,
    first: screen.getByRole('menuitem', { name: '原首项' }), last: screen.getByRole('menuitem', { name: '原末项' }),
    show: () => { visible = true; },
    frame: () => act(() => { const queued = [...frames.values()]; frames.clear(); queued.forEach((callback) => callback(16)); }),
    dispose: () => opener.remove(),
  };
}

it('recovers native-rejected initial focus once on the next visible natural frame, retaining keyboard navigation', () => {
  const t = rejectedMenu();
  try {
    expect(t.opener).toHaveFocus();
    t.show(); t.frame(); expect(t.first).toHaveFocus(); expect(t.request).toHaveBeenCalledTimes(1); expect(t.focus).toHaveBeenLastCalledWith({ preventScroll: true });
    expect(t.frames.size).toBe(0);
    fireEvent.keyDown(t.first, { key: 'End' }); expect(t.last).toHaveFocus();
    fireEvent(window, new Event('resize')); expect(t.last).toHaveFocus();
    fireEvent.keyDown(t.last, { key: 'Home' }); expect(t.first).toHaveFocus();
    fireEvent.keyDown(t.first, { key: 'ArrowDown' }); expect(t.last).toHaveFocus();
    fireEvent.keyDown(t.last, { key: 'Escape' }); expect(t.close).toHaveBeenCalledWith('keyboard');
    expect(t.action).not.toHaveBeenCalled(); expect(t.request).toHaveBeenCalledTimes(1);
  } finally { t.dispose(); }
});

it('does not poll or retry when its single fallback frame remains natively hidden', () => {
  const t = rejectedMenu();
  try { t.frame(); expect(t.opener).toHaveFocus(); expect(t.request).toHaveBeenCalledTimes(1); expect(t.frames.size).toBe(0);
    t.show(); t.frame(); expect(t.opener).toHaveFocus();
  } finally { t.dispose(); }
});

it.each(['external-button', 'input', 'own-input', 'own-item'] as const)('preserves newer %s focus before the fallback frame', (intent) => {
  const t = rejectedMenu(); const target = intent === 'own-item' ? t.last : document.createElement(intent === 'input' || intent === 'own-input' ? 'input' : 'button');
  if (intent === 'own-input') t.first.closest('[role="menu"]')?.append(target);
  else if (intent !== 'own-item') document.body.append(target);
  try { t.show(); target.focus(); t.frame(); expect(target).toHaveFocus(); expect(t.first).not.toHaveFocus(); expect(t.action).not.toHaveBeenCalled(); }
  finally { if (intent !== 'own-item') target.remove(); t.dispose(); }
});

it('does not borrow the opener again after a newer input intent returns focus to it', () => {
  const t = rejectedMenu(); const input = document.createElement('input'); document.body.append(input);
  try { input.addEventListener('focusin', (event) => event.stopPropagation());
    t.show(); input.focus(); t.opener.focus(); t.frame(); expect(t.opener).toHaveFocus(); expect(t.first).not.toHaveFocus(); }
  finally { input.remove(); t.dispose(); }
});

it('cancels the queued native frame when the menu closes before it', () => {
  const t = rejectedMenu();
  try { t.view.unmount(); expect(t.cancel).toHaveBeenCalledTimes(1); expect(t.frames.size).toBe(0); t.show(); t.frame(); expect(t.opener).toHaveFocus(); }
  finally { t.dispose(); }
});

it('does not focus a detached original target or a replacement menu', () => {
  const t = rejectedMenu();
  try { t.first.remove(); t.show(); t.frame(); expect(t.opener).toHaveFocus(); t.view.unmount();
    render(<PawContextMenu ariaLabel="新菜单" items={[{ id: 'new', label: '新首项', action: t.action }]} onClose={t.close} x={80} y={80} />);
    expect(screen.getByRole('menuitem', { name: '新首项' })).toHaveFocus(); t.frame(); expect(screen.getByRole('menuitem', { name: '新首项' })).toHaveFocus();
  } finally { t.dispose(); }
});

it('keeps successful immediate native focus without scheduling a fallback frame', () => {
  const request = vi.spyOn(window, 'requestAnimationFrame'); const focus = vi.spyOn(HTMLElement.prototype, 'focus');
  render(<PawContextMenu ariaLabel="立即菜单" items={[{ id: 'first', label: '立即首项', action: vi.fn() }]} onClose={() => undefined} x={80} y={80} />);
  expect(screen.getByRole('menuitem', { name: '立即首项' })).toHaveFocus(); expect(request).not.toHaveBeenCalled();
  expect(focus).toHaveBeenCalledExactlyOnceWith({ preventScroll: true });
});

it.each(['hidden', 'collapse', 'display-none'] as const)('does not focus while the fallback computed menu remains %s', (state) => {
  const t = rejectedMenu();
  try {
    t.show(); const menu = screen.getByRole('menu');
    if (state === 'display-none') menu.style.display = 'none'; else menu.style.visibility = state;
    t.frame(); expect(t.opener).toHaveFocus(); expect(t.frames.size).toBe(0); expect(t.request).toHaveBeenCalledTimes(1);
  } finally { t.dispose(); }
});

it('does not substitute another item when the original fallback target becomes disabled', () => {
  const t = rejectedMenu();
  try { (t.first as HTMLButtonElement).disabled = true; t.show(); t.frame(); expect(t.opener).toHaveFocus(); expect(t.last).not.toHaveFocus(); }
  finally { t.dispose(); }
});
