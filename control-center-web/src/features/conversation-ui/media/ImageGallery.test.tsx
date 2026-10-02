import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ImageGallery } from './ImageGallery';
import { GalleryImageElement } from './ImageGalleryView';
import { updateReadingPreferences } from '../reading/reading-preferences';
import type { GalleryImage } from './image-gallery-model';
import { PawOsAppSurfaceProvider } from '@/features/paw-os/surface-context';

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers(); updateReadingPreferences({ motion: 'system' }); });
const a: GalleryImage = { id: 'a', name: 'first.png', source: '/first.png' };
const b: GalleryImage = { id: 'b', name: 'second.png', source: '/second.png' };
const c: GalleryImage = { id: 'c', name: 'third.png', source: '/third.png' };
function load(name: string) {
  for (const img of screen.getAllByRole('img', { name })) {
    Object.defineProperties(img, { naturalWidth: { value: 800, configurable: true }, naturalHeight: { value: 600, configurable: true } });
    fireEvent.load(img);
  }
}
describe('ImageGallery incremental receipt lifecycle', () => {
  it('keeps decoded images visible but stops decorative entry in an inactive app window', () => {
    const surface = (active: boolean) => <PawOsAppSurfaceProvider appId="agent" windowId="image-window" active={active} width={900} height={600}>
      <ImageGallery items={[a]} />
    </PawOsAppSurfaceProvider>;
    const view = render(surface(true));
    load(a.name);
    const image = screen.getByRole('img', { name: a.name });
    expect(image).toHaveAttribute('data-loaded', 'true');
    view.rerender(surface(false));
    expect(screen.getByRole('img', { name: a.name })).toBe(image);
    expect(image).toHaveAttribute('data-loaded', 'true');
    expect(view.container.querySelector('.paw-image-gallery')).toHaveAttribute('data-motion', 'false');
    expect(image).toHaveAttribute('data-reveal', 'settled');
    view.rerender(surface(true));
    expect(image).toHaveAttribute('data-reveal', 'settled');
  });
  it('keeps the open single-image viewer and zoom when a second image arrives', async () => {
    const user = userEvent.setup(); const view = render(<ImageGallery items={[a]}/>);
    load(a.name); await user.click(screen.getByRole('button', { name: '查看图片 first.png' }));
    await user.click(screen.getByRole('button', { name: '原尺寸' }));
    await user.click(screen.getByRole('button', { name: '放大图片' }));
    view.rerender(<ImageGallery items={[a, b]}/>);
    expect(screen.getByRole('dialog')).toHaveAccessibleName(a.name);
    expect(screen.getByRole('status')).toHaveTextContent('125%');
    expect(screen.getByRole('button', { name: '下一张图片' })).toBeEnabled();
  });
  it('does not reuse dimensions or failure state for a replacement URL with the same ID', async () => {
    const user = userEvent.setup(); const view = render(<ImageGallery items={[a]}/>);
    load(a.name); await user.click(screen.getByRole('button', { name: '查看图片 first.png' }));
    expect(screen.getByRole('button', { name: '原尺寸' })).toBeEnabled();
    view.rerender(<ImageGallery items={[{ ...a, source: '/replacement.png' }]}/>);
    expect(screen.getByRole('button', { name: '原尺寸' })).toBeDisabled();
    load(a.name); expect(screen.getByRole('button', { name: '原尺寸' })).toBeEnabled();
  });
  it('closes an image removed by the owner rather than displaying stale bytes', async () => {
    const user = userEvent.setup(); const view = render(<ImageGallery items={[a, b]}/>);
    await user.click(screen.getByRole('button', { name: '查看图片 first.png' }));
    view.rerender(<ImageGallery items={[b]}/>);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
  it('retains focused image across overview and focus without reordering', async () => {
    const user = userEvent.setup(); render(<ImageGallery items={[a, b, c]}/>);
    await user.click(screen.getByRole('button', { name: '聚焦下一张' }));
    await user.click(screen.getByRole('button', { name: '总览' }));
    expect(screen.getAllByRole('button', { name: /^查看图片/ }).map(n => n.getAttribute('data-image-id'))).toEqual(['a', 'b', 'c']);
    await user.click(screen.getByRole('button', { name: '聚焦' }));
    expect(screen.getByRole('button', { name: '查看图片 second.png' })).toBeInTheDocument();
  });
  it('keeps compact drafts compact regardless of count and identifies unsent bytes', async () => {
    const user = userEvent.setup(); render(<ImageGallery compact items={[{ ...a, origin: 'local_draft' }]}/>);
    expect(screen.queryByRole('group', { name: '图片浏览布局' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '查看图片 first.png' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('尚未发送');
    expect(screen.queryByRole('button', { name: '独立窗口' })).not.toBeInTheDocument();
  });
  it('supports retry after decode error without claiming a successful receipt', async () => {
    const user = userEvent.setup(); render(<ImageGallery items={[a]}/>);
    fireEvent.error(screen.getByRole('img', { name: a.name }));
    await user.click(screen.getByRole('button', { name: '查看图片 first.png' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('图片未能加载');
    await user.click(screen.getByRole('button', { name: '重新加载' }));
    expect(within(screen.getByRole('dialog')).getByRole('img', { name: a.name })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '原尺寸' })).toBeDisabled();
  });
});


function rect(left: number, top: number, width: number, height: number): DOMRect {
  return { left, top, width, height, x: left, y: top, right: left + width, bottom: top + height, toJSON: () => ({}) };
}
function deferred() {
  let resolve!: () => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function imageWithDecode(image: HTMLElement, promise: Promise<void>) {
  Object.defineProperties(image, {
    naturalWidth: { value: 1000, configurable: true }, naturalHeight: { value: 4000, configurable: true },
    decode: { value: vi.fn(() => promise), configurable: true },
  });
}

describe('ImageGallery reading continuity', () => {
  it('keeps the point in the viewport center while zooming a long screenshot', async () => {
    const user = userEvent.setup();
    render(<ImageGallery items={[{ ...a, width: 1000, height: 4000 }]}/>);
    await user.click(screen.getByRole('button', { name: '查看图片 first.png' }));
    await user.click(screen.getByRole('button', { name: '原尺寸' }));
    const viewport = screen.getByRole('region', { name: '可滚动的图片画面' });
    const img = within(viewport).getByRole('img') as HTMLImageElement;
    Object.defineProperties(viewport, { clientWidth: { value: 1000 }, clientHeight: { value: 600 } });
    vi.spyOn(viewport, 'getBoundingClientRect').mockImplementation(() => rect(100, 100, 1000, 600));
    vi.spyOn(img, 'getBoundingClientRect').mockImplementation(() => {
      const width = Number.parseFloat(img.style.width);
      return rect(100 - viewport.scrollLeft, 100 - viewport.scrollTop, width, width * 4);
    });
    viewport.scrollTop = 1200;
    await user.click(screen.getByRole('button', { name: '放大图片' }));
    expect(viewport.scrollTop).toBe(1575);
    expect(viewport.scrollLeft).toBe(125);
    fireEvent.keyDown(viewport, { key: '-' });
    expect(viewport.scrollTop).toBe(1200);
    expect(viewport.scrollLeft).toBe(0);
  });
  it('anchors the centered fitted image before entering its original size', async () => {
    const user = userEvent.setup();
    render(<ImageGallery items={[{ ...a, width: 1000, height: 4000 }]}/>);
    await user.click(screen.getByRole('button', { name: '查看图片 first.png' }));
    const viewport = screen.getByRole('region', { name: '可滚动的图片画面' });
    const img = within(viewport).getByRole('img') as HTMLImageElement;
    Object.defineProperties(viewport, { clientWidth: { value: 800 }, clientHeight: { value: 600 } });
    vi.spyOn(viewport, 'getBoundingClientRect').mockImplementation(() => rect(100, 100, 800, 600));
    vi.spyOn(img, 'getBoundingClientRect').mockImplementation(() => img.style.width
      ? rect(100 - viewport.scrollLeft, 100 - viewport.scrollTop, Number.parseFloat(img.style.width), 4000)
      : rect(425, 100, 150, 600));
    await user.click(screen.getByRole('button', { name: '原尺寸' }));
    expect(viewport.scrollTop).toBe(1700);
    expect(viewport.scrollLeft).toBe(100);
    await user.click(screen.getByRole('button', { name: '适应' }));
    expect(viewport.scrollTop).toBe(0);
    expect(viewport.scrollLeft).toBe(0);
  });
  it('waits for the selected image decode and ignores late A and B after rapid A → B → C', async () => {
    const user = userEvent.setup(); render(<ImageGallery items={[a, b, c]}/>);
    await user.click(screen.getByRole('button', { name: '查看图片 first.png' }));
    const viewport = screen.getByRole('region', { name: '可滚动的图片画面' });
    const pendingA = deferred(); const pendingB = deferred(); const pendingC = deferred();
    const imgA = within(viewport).getByRole('img'); imageWithDecode(imgA, pendingA.promise); fireEvent.load(imgA);
    expect(imgA).toHaveAttribute('data-loaded', 'false');
    await user.click(screen.getByRole('button', { name: '下一张图片' }));
    const imgB = within(viewport).getByRole('img'); imageWithDecode(imgB, pendingB.promise); fireEvent.load(imgB);
    await user.click(screen.getByRole('button', { name: '下一张图片' }));
    const imgC = within(viewport).getByRole('img'); imageWithDecode(imgC, pendingC.promise); fireEvent.load(imgC);
    await act(async () => { pendingB.resolve(); pendingA.resolve(); });
    expect(within(viewport).getByRole('img')).toBe(imgC);
    expect(imgC).toHaveAttribute('data-loaded', 'false');
    expect(screen.getByRole('button', { name: '原尺寸' })).toBeDisabled();
    await act(async () => { pendingC.resolve(); });
    expect(imgC).toHaveAttribute('data-loaded', 'true');
    expect(screen.getByRole('button', { name: '原尺寸' })).toBeEnabled();
  });
  it('treats decode rejection as retryable failure and never publishes a stale attempt', async () => {
    const pending = deferred(); const success = vi.fn(); const failure = vi.fn();
    const props = { image: a, loads: {}, retries: {}, onLoad: success, onError: failure };
    const view = render(<GalleryImageElement {...props}/>);
    const first = screen.getByRole('img'); imageWithDecode(first, pending.promise); fireEvent.load(first);
    expect(success).not.toHaveBeenCalled();
    await act(async () => { pending.reject(new Error('decode failed')); });
    expect(failure).toHaveBeenCalledExactlyOnceWith(a);
    view.rerender(<GalleryImageElement {...props} retries={{ '["a","/first.png"]': 1 }}/>);
    const retry = screen.getByRole('img'); const late = deferred(); imageWithDecode(retry, late.promise); fireEvent.load(retry);
    view.rerender(<GalleryImageElement {...props} image={{ ...a, source: '/replacement.png' }}/>);
    await act(async () => { late.resolve(); });
    expect(success).not.toHaveBeenCalled();
    expect(screen.getByRole('img')).toHaveAttribute('src', '/replacement.png');
    expect(screen.getByRole('img')).toHaveAttribute('data-loaded', 'false');
  });
  it('reveals once after decode, preserves the node through streaming, and terminates motion while hidden', async () => {
    const user = userEvent.setup(); const view = render(<ImageGallery items={[a]}/>);
    await user.click(screen.getByRole('button', { name: '查看图片 first.png' }));
    const viewport = screen.getByRole('region', { name: '可滚动的图片画面' });
    const img = within(viewport).getByRole('img'); const pending = deferred(); imageWithDecode(img, pending.promise);
    fireEvent.load(img); await act(async () => { pending.resolve(); });
    expect(img).toHaveAttribute('data-reveal', 'enter');
    await user.click(screen.getByRole('button', { name: '原尺寸' }));
    viewport.scrollTop = 900;
    view.rerender(<ImageGallery items={[{ ...a, caption: 'streaming metadata' }, b]}/>);
    expect(within(viewport).getByRole('img')).toBe(img);
    expect(viewport.scrollTop).toBe(900);
    expect(screen.getByRole('status')).toHaveTextContent('100%');
    const visibility = vi.spyOn(document, 'visibilityState', 'get'); visibility.mockReturnValue('hidden');
    fireEvent(document, new Event('visibilitychange'));
    expect(img).toHaveAttribute('data-reveal', 'settled');
    visibility.mockReturnValue('visible'); fireEvent(document, new Event('visibilitychange'));
    fireEvent.load(img);
    expect(img).toHaveAttribute('data-reveal', 'settled');
    fireEvent.keyDown(viewport, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: '查看图片 first.png' })).toHaveFocus());
  });
  it('settles the reveal within a finite deadline and does not replay it on another load event', async () => {
    vi.useFakeTimers();
    const success = vi.fn();
    render(<GalleryImageElement image={a} loads={{}} retries={{}} onLoad={success} onError={vi.fn()} motion/>);
    const img = screen.getByRole('img'); const pending = deferred(); imageWithDecode(img, pending.promise);
    fireEvent.load(img); await act(async () => { pending.resolve(); });
    expect(img).toHaveAttribute('data-reveal', 'enter');
    act(() => { vi.advanceTimersByTime(280); });
    expect(img).toHaveAttribute('data-reveal', 'settled');
    fireEvent.load(img);
    expect(success).toHaveBeenCalledTimes(1);
    expect(img).toHaveAttribute('data-reveal', 'settled');
    expect(vi.getTimerCount()).toBe(0);
  });
  it('invalidates a pending decode after an image error or unmount', async () => {
    const success = vi.fn(); const failure = vi.fn(); const pending = deferred();
    const view = render(<GalleryImageElement image={a} loads={{}} retries={{}} onLoad={success} onError={failure}/>);
    const img = screen.getByRole('img'); imageWithDecode(img, pending.promise); fireEvent.load(img);
    fireEvent.error(img); fireEvent.error(img);
    await act(async () => { pending.resolve(); });
    expect(failure).toHaveBeenCalledTimes(1); expect(success).not.toHaveBeenCalled();
    view.rerender(<GalleryImageElement image={b} loads={{}} retries={{}} onLoad={success} onError={failure}/>);
    const next = screen.getByRole('img'); const late = deferred(); imageWithDecode(next, late.promise); fireEvent.load(next);
    view.unmount(); await act(async () => { late.resolve(); });
    expect(success).not.toHaveBeenCalled();
  });
  it('skips image reveal for reduced motion without hiding decoded pixels', async () => {
    updateReadingPreferences({ motion: 'reduced' });
    const user = userEvent.setup(); render(<ImageGallery items={[a]}/>);
    await user.click(screen.getByRole('button', { name: '查看图片 first.png' }));
    const img = within(screen.getByRole('region', { name: '可滚动的图片画面' })).getByRole('img');
    const pending = deferred(); imageWithDecode(img, pending.promise); fireEvent.load(img);
    await act(async () => { pending.resolve(); });
    expect(img).toHaveAttribute('data-loaded', 'true');
    expect(img).toHaveAttribute('data-reveal', 'settled');
  });
});
