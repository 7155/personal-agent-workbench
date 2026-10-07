import { cleanup, fireEvent, render, screen } from '@testing-library/react';
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
