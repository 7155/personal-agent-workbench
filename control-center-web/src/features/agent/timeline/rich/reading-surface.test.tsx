import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { RichReadingSurface } from './RichReadingSurface';
import { updateReadingPreferences } from '../../../conversation-ui/reading/reading-preferences';

afterEach(() => {
  cleanup();
  updateReadingPreferences({ size: 'standard', spacing: 'comfortable', motion: 'system' });
});
const prose = (title = '正文一') => <div className="paw-rich-prose"><h2>{title}</h2><h2>正文二</h2><h2>正文三</h2></div>;

describe('V3 reading surface', () => {
  it('adds no reading toolbar to a short answer or streaming tail', () => {
    const view = render(<RichReadingSurface source="short" documentKey="a"><div className="paw-rich-prose"><p>简短回答</p></div></RichReadingSurface>);
    expect(screen.queryByRole('button', { name: '阅读' })).toBeNull();
    view.rerender(<RichReadingSurface source="long" documentKey="a" streaming>{prose()}</RichReadingSurface>);
    expect(screen.queryByRole('button', { name: '章节' })).toBeNull();
  });
  it('indexes only real answer headings, not headings inside tool details', async () => {
    render(<RichReadingSurface source="headings" documentKey="a">{prose()}<details open><div className="paw-rich-prose"><h2>工具内部</h2></div></details></RichReadingSurface>);
    fireEvent.click(await screen.findByRole('button', { name: '章节' }));
    expect(screen.getByRole('navigation', { name: '本段章节导航' })).not.toHaveTextContent('工具内部');
    expect(screen.getByRole('navigation')).toHaveTextContent('正文三');
  });
  it('rescans deferred settled content without keeping detached heading references', async () => {
    const view = render(<RichReadingSurface source="settled" documentKey="a">{prose()}</RichReadingSurface>);
    fireEvent.click(await screen.findByRole('button', { name: '章节' }));
    view.rerender(<RichReadingSurface source="settled" documentKey="a">{prose('新的章节')}</RichReadingSurface>);
    await waitFor(() => expect(screen.getByRole('navigation')).toHaveTextContent('新的章节'));
    expect(screen.getByRole('navigation')).not.toHaveTextContent('正文一');
  });
  it('changes display preferences without replacing the answer and restores menu focus', async () => {
    const view = render(<RichReadingSurface source="headings" documentKey="a">{prose()}</RichReadingSurface>);
    const original = screen.getByRole('heading', { name: '正文一' });
    const opener = await screen.findByRole('button', { name: '阅读' }); fireEvent.click(opener);
    fireEvent.click(screen.getByRole('button', { name: /大字/ }));
    fireEvent.click(screen.getByRole('button', { name: '紧凑' }));
    expect(view.container.firstChild).toHaveAttribute('data-reading-size', 'large');
    expect(view.container.firstChild).toHaveAttribute('data-reading-spacing', 'compact');
    expect(screen.getByRole('heading', { name: '正文一' })).toBe(original);
    fireEvent.keyDown(screen.getByRole('button', { name: '紧凑' }), { key: 'Escape' });
    expect(opener).toHaveFocus();
  });
});
