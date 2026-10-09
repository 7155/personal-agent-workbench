import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MotionProvider, useMotionPreference } from '@/design/motion';
import { PawOsAppSurfaceProvider } from '@/features/paw-os/surface-context';
import { Button } from './Button';
import { SegmentedControl } from './SegmentedControl';
import { Skeleton } from './Skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './Tabs';

const visibility = Object.getOwnPropertyDescriptor(document, 'visibilityState');
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  localStorage.removeItem('rag-ime-control-motion');
  delete document.documentElement.dataset.reduceMotion;
  delete document.documentElement.dataset.motionPreference;
  if (visibility) Object.defineProperty(document, 'visibilityState', visibility);
  else delete (document as unknown as Record<string, unknown>).visibilityState;
});

function Controls() {
  const [value, setValue] = useState('session');
  return <>
    <SegmentedControl aria-label="工作视图" value={value} onValueChange={setValue}
      items={[{ value: 'session', label: '对话' }, { value: 'lab', label: '实验' }]} />
    <SegmentedControl aria-label="内容布局" value="list" onValueChange={() => {}}
      items={[{ value: 'list', label: '列表' }, { value: 'grid', label: '卡片' }]} />
    <Button loading>导入材料</Button>
    <Skeleton data-testid="loading-skeleton" />
  </>;
}

function RestoreMotion() {
  const { setPreference } = useMotionPreference();
  return <button onClick={() => setPreference('full')} type="button">恢复完整动效</button>;
}

describe('semantic interaction motion', () => {
  it('keeps opted-in loading focusable without allowing click, keyboard or form activation', async () => {
    const onClick = vi.fn();
    const onParentClick = vi.fn();
    const onSubmit = vi.fn((event) => event.preventDefault());
    const surface = (loading: boolean) => <form onClick={onParentClick} onSubmit={onSubmit}>
      <Button loading={loading} onClick={onClick} preserveFocusWhileLoading type="submit">重新读取实验</Button>
    </form>;
    const view = render(surface(false));
    const button = screen.getByRole('button', { name: '重新读取实验' });
    const label = button.querySelector('.ui-button__label');
    button.focus();
    view.rerender(surface(true));
    expect(screen.getByRole('button', { name: '重新读取实验' })).toBe(button);
    expect(button.querySelector('.ui-button__label')).toBe(label);
    expect(button).toHaveFocus();
    expect(button).toBeEnabled();
    expect(button).toHaveAttribute('aria-disabled', 'true');
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(button.querySelector('.ui-button__progress')).toHaveAttribute('aria-hidden', 'true');
    const user = userEvent.setup();
    await user.click(button);
    await user.keyboard('{Enter} ');
    expect(onClick).not.toHaveBeenCalled();
    expect(onParentClick).not.toHaveBeenCalled();
    expect(onSubmit).not.toHaveBeenCalled();
    view.rerender(surface(false));
    expect(button).not.toHaveAttribute('aria-disabled');
    expect(button).not.toHaveAttribute('aria-busy');
    expect(button).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(onParentClick).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it.each([false, true])('keeps explicit disabled native when loading-focus preservation is enabled (loading=%s)', async (loading) => {
    const onClick = vi.fn();
    render(<Button disabled loading={loading} onClick={onClick} preserveFocusWhileLoading>无法读取</Button>);
    const button = screen.getByRole('button', { name: '无法读取' });
    expect(button).toBeDisabled();
    await userEvent.setup().click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('moves only the selected surface while preserving button identity, focus and independent groups', () => {
    const view = render(<Controls />);
    const session = screen.getByRole('radio', { name: '对话' });
    const lab = screen.getByRole('radio', { name: '实验' });
    lab.focus();
    fireEvent.click(lab);
    expect(lab).toBeChecked();
    expect(lab).toHaveFocus();
    expect(lab.querySelector('.ui-segmented__selection')).toHaveAttribute('aria-hidden', 'true');
    expect(session.querySelector('.ui-segmented__selection')).toBeNull();
    const groups = screen.getAllByRole('radiogroup');
    expect(groups[0].dataset.motionGroup).toBeTruthy();
    expect(groups[0].dataset.motionGroup).not.toBe(groups[1].dataset.motionGroup);
    view.rerender(<Controls />);
    expect(screen.getByRole('radio', { name: '实验' })).toBe(lab);
    expect(lab).toHaveFocus();
  });

  it('pauses decorative motion when the document is hidden but keeps work truthful and operable', () => {
    render(<Controls />);
    const busy = screen.getByRole('button', { name: '导入材料' });
    expect(busy).toBeDisabled();
    expect(busy).toHaveAttribute('aria-busy', 'true');
    expect(busy.querySelector('.ui-button__progress')).toHaveAttribute('aria-hidden', 'true');
    act(() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(busy).toHaveAttribute('data-motion-active', 'false');
    expect(screen.getByTestId('loading-skeleton')).toHaveAttribute('data-motion-active', 'false');
    expect(screen.getByRole('radio', { name: '对话' })).toBeEnabled();
    expect(busy).toHaveAttribute('aria-busy', 'true');
  });

  it('honours the application comfort setting for all three feedback types', () => {
    localStorage.setItem('rag-ime-control-motion', 'reduce');
    render(<MotionProvider><Controls /></MotionProvider>);
    expect(screen.getByRole('radiogroup', { name: '工作视图' })).toHaveAttribute('data-motion-active', 'false');
    expect(screen.getByRole('button', { name: '导入材料' })).toHaveAttribute('data-motion-active', 'false');
    expect(screen.getByTestId('loading-skeleton')).toHaveAttribute('data-motion-active', 'false');
  });

  it('restores motion immediately when the authoritative preference leaves reduced mode', () => {
    localStorage.setItem('rag-ime-control-motion', 'reduce');
    render(<MotionProvider><RestoreMotion /><Controls /></MotionProvider>);
    expect(screen.getByTestId('loading-skeleton')).toHaveAttribute('data-motion-active', 'false');
    fireEvent.click(screen.getByRole('button', { name: '恢复完整动效' }));
    expect(screen.getByTestId('loading-skeleton')).toHaveAttribute('data-motion-active', 'true');
    expect(screen.getByRole('radiogroup', { name: '工作视图' })).toHaveAttribute('data-motion-active', 'true');
  });

  it('reacts to system comfort changes in an isolated host without a MotionProvider', () => {
    let reduced = false;
    const listeners = new Set<() => void>();
    const original = Object.getOwnPropertyDescriptor(window, 'matchMedia');
    Object.defineProperty(window, 'matchMedia', { configurable: true, value: () => ({
      get matches() { return reduced; },
      addEventListener: (_type: string, listener: () => void) => listeners.add(listener),
      removeEventListener: (_type: string, listener: () => void) => listeners.delete(listener),
      addListener: (listener: () => void) => listeners.add(listener),
      removeListener: (listener: () => void) => listeners.delete(listener),
    }) });
    try {
      const view = render(<Skeleton data-testid="isolated-placeholder" />);
      const placeholder = screen.getByTestId('isolated-placeholder');
      expect(placeholder).toHaveAttribute('data-motion-active', 'true');
      act(() => { reduced = true; listeners.forEach(listener => listener()); });
      expect(placeholder).toHaveAttribute('data-motion-active', 'false');
      act(() => { reduced = false; listeners.forEach(listener => listener()); });
      expect(placeholder).toHaveAttribute('data-motion-active', 'true');
      view.unmount();
    } finally {
      if (original) Object.defineProperty(window, 'matchMedia', original);
      else delete (window as unknown as Record<string, unknown>).matchMedia;
    }
  });

  it('stops decoration in an inactive PAW window without stopping its pending operation', () => {
    const surface = (active: boolean) => <PawOsAppSurfaceProvider appId="knowledge" windowId="knowledge-one"
      active={active} width={900} height={600}><Controls /></PawOsAppSurfaceProvider>;
    const view = render(surface(false));
    const pending = screen.getByRole('button', { name: '导入材料' });
    expect(pending).toHaveAttribute('data-motion-active', 'false');
    expect(pending).toHaveAttribute('aria-busy', 'true');
    view.rerender(surface(true));
    expect(screen.getByRole('button', { name: '导入材料' })).toBe(pending);
    expect(pending).toHaveAttribute('data-motion-active', 'true');
    expect(pending).toHaveAttribute('aria-busy', 'true');
  });

  it('keeps the same editable content node during updates instead of replaying an entrance', () => {
    const surface = (detail: string) => <Tabs defaultValue="ready">
      <TabsList><TabsTrigger value="ready">结果</TabsTrigger><TabsTrigger value="other">参数</TabsTrigger></TabsList>
      <TabsContent value="ready"><input aria-label="结果笔记" defaultValue="用户笔记" /><p>{detail}</p></TabsContent>
      <TabsContent value="other">其他参数</TabsContent>
    </Tabs>;
    const view = render(surface('片段一'));
    const editor = screen.getByRole('textbox', { name: '结果笔记' });
    editor.focus();
    const panel = screen.getByRole('tabpanel');
    view.rerender(surface('片段一和新片段'));
    expect(screen.getByRole('textbox', { name: '结果笔记' })).toBe(editor);
    expect(screen.getByRole('tabpanel')).toBe(panel);
    expect(editor).toHaveFocus();
    expect(editor).toHaveValue('用户笔记');
  });
});
