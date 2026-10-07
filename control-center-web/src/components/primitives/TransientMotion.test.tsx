import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { MotionProvider, useMotionPreference } from '@/design/motion';
import { PawOsAppSurfaceProvider } from '@/features/paw-os/surface-context';
import { Dialog, DialogContent, DialogTitle } from './Dialog';
import { Menu, MenuContent, MenuItem, MenuSub, MenuSubContent, MenuSubTrigger, MenuTrigger } from './Menu';
import { Popover, PopoverContent, PopoverTrigger } from './Popover';
import { Tooltip, TooltipProvider } from './Tooltip';

const visibility = Object.getOwnPropertyDescriptor(document, 'visibilityState');
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.removeItem('rag-ime-control-motion');
  delete document.documentElement.dataset.reduceMotion;
  delete document.documentElement.dataset.motionPreference;
  if (visibility) Object.defineProperty(document, 'visibilityState', visibility);
  else delete (document as unknown as Record<string, unknown>).visibilityState;
});
function Surface({ active = true, children }: { active?: boolean; children: ReactNode }) {
  return <MotionProvider><PawOsAppSurfaceProvider appId="knowledge" active={active} width={900} height={700}>{children}</PawOsAppSurfaceProvider></MotionProvider>;
}
function Preferences() {
  const { setPreference } = useMotionPreference();
  return <><button onClick={() => setPreference('reduce')}>减少动效</button><button onClick={() => setPreference('full')}>完整动效</button></>;
}
function visibilityState(value: 'hidden' | 'visible') {
  act(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, value });
    document.dispatchEvent(new Event('visibilitychange'));
  });
}

function CallerGate({ kind, value }: {
  kind: 'Dialog' | 'Popover' | 'Menu' | 'MenuSub';
  value: boolean | 'false' | 'true';
}) {
  if (kind === 'Dialog') return <Dialog open><DialogContent data-motion-active={value}><DialogTitle>调用方限制</DialogTitle><input aria-label="调用方草稿" defaultValue="未提交" /></DialogContent></Dialog>;
  if (kind === 'Popover') return <Popover open><PopoverTrigger asChild><button>浮层入口</button></PopoverTrigger><PopoverContent data-motion-active={value}><input aria-label="调用方草稿" defaultValue="未提交" /></PopoverContent></Popover>;
  return <Menu open><MenuTrigger asChild><button>操作入口</button></MenuTrigger><MenuContent data-motion-active={kind === 'Menu' ? value : undefined}><MenuItem>原操作</MenuItem>{kind === 'MenuSub' && <MenuSub open><MenuSubTrigger>分组</MenuSubTrigger><MenuSubContent data-motion-active={value} aria-label="调用方子菜单"><MenuItem>子操作</MenuItem></MenuSubContent></MenuSub>}</MenuContent></Menu>;
}

function callerContent(kind: 'Dialog' | 'Popover' | 'Menu' | 'MenuSub') {
  return kind === 'Dialog' ? screen.getByRole('dialog')
    : kind === 'Popover' ? document.querySelector('.ui-popover')!
      : kind === 'MenuSub' ? document.querySelector('[aria-label="调用方子菜单"]')!
        : document.querySelector('.ui-menu')!;
}

it.each(['Dialog', 'Popover', 'Menu', 'MenuSub'] as const)('%s retains a caller false floor through canonical activity restoration', (kind) => {
  const view = render(<Surface><CallerGate kind={kind} value={false} /></Surface>);
  const content = callerContent(kind);
  const control = content.querySelector('input, [role="menuitem"]') as HTMLElement;
  control.focus();
  expect(content).toHaveAttribute('data-motion-active', 'false');
  if (kind === 'Dialog') expect(document.querySelector('.ui-dialog__overlay')).toHaveAttribute('data-motion-active', 'false');
  view.rerender(<Surface active={false}><CallerGate kind={kind} value="false" /></Surface>);
  view.rerender(<Surface><CallerGate kind={kind} value="false" /></Surface>);
  expect(callerContent(kind)).toBe(content);
  expect(content).toHaveAttribute('data-motion-active', 'false');
  expect(control).toHaveFocus();
  if (kind === 'Dialog' || kind === 'Popover') expect(control).toHaveValue('未提交');
  if (kind === 'Dialog') expect(document.querySelector('.ui-dialog__overlay')).toHaveAttribute('data-motion-active', 'false');
});

it.each(['Dialog', 'Popover', 'Menu', 'MenuSub'] as const)('%s cannot raise canonical inactivity with caller true', (kind) => {
  const view = render(<Surface active={false}><CallerGate kind={kind} value={true} /></Surface>);
  const content = callerContent(kind);
  expect(content).toHaveAttribute('data-motion-active', 'false');
  view.rerender(<Surface active={false}><CallerGate kind={kind} value="true" /></Surface>);
  expect(callerContent(kind)).toBe(content);
  expect(content).toHaveAttribute('data-motion-active', 'false');
  if (kind === 'Dialog') expect(document.querySelector('.ui-dialog__overlay')).toHaveAttribute('data-motion-active', 'false');
});

it('gates both inactive Dialog content and its overlay through the canonical owner', () => {
  render(<Surface active={false}><Dialog open><DialogContent data-motion-active="true"><DialogTitle>只读弹窗</DialogTitle><input aria-label="弹窗草稿" defaultValue="保留草稿" /></DialogContent></Dialog></Surface>);
  expect(screen.getByRole('dialog')).toHaveAttribute('data-motion-active', 'false');
  expect(document.querySelector('.ui-dialog__overlay')).toHaveAttribute('data-motion-active', 'false');
  expect(screen.getByRole('textbox')).toHaveValue('保留草稿');
});

it('keeps hidden/restored Popover draft, node and focus without another open autofocus', () => {
  const opened = vi.fn();
  render(<Surface><Popover open><PopoverTrigger asChild><button>展开</button></PopoverTrigger><PopoverContent onOpenAutoFocus={opened}><input aria-label="浮层草稿" defaultValue="未提交" /></PopoverContent></Popover></Surface>);
  const content = document.querySelector('.ui-popover');
  const editor = screen.getByRole('textbox', { name: '浮层草稿' });
  editor.focus();
  visibilityState('hidden');
  expect(content).toHaveAttribute('data-motion-active', 'false');
  visibilityState('visible');
  expect(content).toHaveAttribute('data-motion-active', 'true');
  expect(document.querySelector('.ui-popover')).toBe(content);
  expect(editor).toHaveValue('未提交');
  expect(editor).toHaveFocus();
  expect(opened).toHaveBeenCalledTimes(1);
});

it('restores a mounted Dialog from reduce to full without remount or focus entry', () => {
  const opened = vi.fn();
  render(<Surface><Dialog open><DialogContent onOpenAutoFocus={opened}><DialogTitle>显示设置</DialogTitle><Preferences /><input aria-label="设置草稿" defaultValue="继续编辑" /></DialogContent></Dialog></Surface>);
  const content = screen.getByRole('dialog');
  const editor = screen.getByRole('textbox');
  editor.focus();
  fireEvent.click(screen.getByRole('button', { name: '减少动效' }));
  expect(content).toHaveAttribute('data-motion-active', 'false');
  fireEvent.click(screen.getByRole('button', { name: '完整动效' }));
  expect(content).toHaveAttribute('data-motion-active', 'true');
  expect(screen.getByRole('dialog')).toBe(content);
  expect(editor).toHaveFocus();
  expect(editor).toHaveValue('继续编辑');
  expect(opened).toHaveBeenCalledTimes(1);
});

it('respects the system floor even under full preference and cleans its subscription', () => {
  let reduced = true;
  const listeners = new Set<() => void>();
  vi.stubGlobal('matchMedia', () => ({ get matches() { return reduced; },
    addEventListener: (_event: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_event: string, listener: () => void) => listeners.delete(listener) }));
  localStorage.setItem('rag-ime-control-motion', 'full');
  const view = render(<Surface><Popover open><PopoverTrigger asChild><button>参数</button></PopoverTrigger><PopoverContent>系统设置优先</PopoverContent></Popover></Surface>);
  expect(document.querySelector('.ui-popover')).toHaveAttribute('data-motion-active', 'false');
  act(() => { reduced = false; listeners.forEach(listener => listener()); });
  expect(document.querySelector('.ui-popover')).toHaveAttribute('data-motion-active', 'true');
  view.unmount();
  expect(listeners.size).toBe(0);
});

it('keeps Menu and submenu activity scoped to their originating application', () => {
  const child = <Menu open><MenuTrigger asChild><button>更多</button></MenuTrigger><MenuContent><MenuItem>原操作</MenuItem><MenuSub open><MenuSubTrigger>分组</MenuSubTrigger><MenuSubContent aria-label="分组操作"><MenuItem>子操作</MenuItem></MenuSubContent></MenuSub></MenuContent></Menu>;
  const view = render(<Surface>{child}</Surface>);
  const menus = [...document.querySelectorAll('.ui-menu')];
  expect(menus).toHaveLength(2);
  const original = screen.getByRole('menuitem', { name: '原操作' });
  original.focus();
  view.rerender(<Surface active={false}>{child}</Surface>);
  menus.forEach(menu => expect(menu).toHaveAttribute('data-motion-active', 'false'));
  view.rerender(<Surface>{child}</Surface>);
  menus.forEach(menu => expect(menu).toHaveAttribute('data-motion-active', 'true'));
  expect([...document.querySelectorAll('.ui-menu')]).toEqual(menus);
  expect(original).toHaveFocus();
});

it('stops delayed Tooltip motion when its application becomes inactive', async () => {
  const user = userEvent.setup();
  const child = <TooltipProvider><Tooltip delayDuration={20} content="只读提示"><button>提示入口</button></Tooltip></TooltipProvider>;
  const view = render(<Surface>{child}</Surface>);
  await user.hover(screen.getByRole('button', { name: '提示入口' }));
  await waitFor(() => expect(document.querySelector('.ui-tooltip')).toHaveAttribute('data-state', 'delayed-open'));
  const content = document.querySelector('.ui-tooltip');
  view.rerender(<Surface active={false}>{child}</Surface>);
  expect(content).toHaveAttribute('data-motion-active', 'false');
  view.rerender(<Surface>{child}</Surface>);
  expect(document.querySelector('.ui-tooltip')).toBe(content);
});
