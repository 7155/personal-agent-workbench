import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import { ImageGallery } from './ImageGallery';
import type { GalleryImage } from './image-gallery-model';

afterEach(cleanup);
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
