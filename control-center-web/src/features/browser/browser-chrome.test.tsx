import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => cleanup());
import { BrowserOmnibox, committedUrlParts, omniboxCommitPreview } from './BrowserOmnibox';
import { BrowserPageStatus } from './BrowserPageStatus';
import { BrowserTabStrip, browserTabTooltip, type BrowserTabItem } from './BrowserTabStrip';
import { BrowserFindBar } from './BrowserFindBar';

const tabs: BrowserTabItem[] = [
  { id: 'a', title: '文档', active: true },
  { id: 'b', title: '加载中页面', active: false, loading: true },
  { id: 'c', title: '', active: false, favicon: 'https://example.com/icon.png' },
  { id: 'd', title: '坏页面', active: false, failed: true },
];

describe('BrowserTabStrip', () => {
  it('returns keyboard focus to a surviving tab and then the new-tab action after closing', async () => {
    const onClose = vi.fn();
    const props = { onClose, onNewTab: vi.fn(), onSelect: vi.fn() };
    const view = render(<BrowserTabStrip {...props} tabs={tabs.slice(0, 2)} />);
    const first = screen.getByRole('tab', { name: '文档' });
    first.focus();
    fireEvent.keyDown(first, { key: 'Delete' });
    expect(onClose).toHaveBeenLastCalledWith('a');

    view.rerender(<BrowserTabStrip {...props} tabs={[{ ...tabs[1], active: true }]} />);
    const remaining = screen.getByRole('tab', { name: '加载中页面' });
    await waitFor(() => expect(remaining).toHaveFocus());
    fireEvent.keyDown(remaining, { key: 'Delete' });
    view.rerender(<BrowserTabStrip {...props} tabs={[]} />);
    await waitFor(() => expect(screen.getByRole('button', { name: '新建标签页' })).toHaveFocus());
  });

  it('renders one real tab per guest with loading, favicon, and failure identity', () => {
    render(
      <BrowserTabStrip onClose={() => undefined} onNewTab={() => undefined} onSelect={() => undefined} tabs={tabs} />,
    );
    const strip = screen.getByRole('tablist', { name: 'PAW Browser 标签页' });
    const rendered = within(strip).getAllByRole('tab');
    expect(rendered).toHaveLength(4);
    expect(rendered[0]).toHaveAttribute('aria-selected', 'true');
    expect(rendered[1]).toHaveAttribute('aria-selected', 'false');
    expect(rendered[2]).toHaveTextContent('新标签页');
    expect(rendered[1].querySelector('.ui-spin')).not.toBeNull();
    expect(rendered[1]).toHaveAttribute('aria-busy', 'true');
    expect(rendered[2].querySelector('img')).toHaveAttribute('src', 'https://example.com/icon.png');
    expect(strip.querySelector('.paw-browser-tab[data-failed]')).not.toBeNull();
    // The scrolling tablist holds nothing but tabs; new-tab is a strip-level
    // sibling that stays reachable however far the tabs scroll.
    expect(within(strip).queryByRole('button', { name: '新建标签页' })).toBeNull();
    expect(screen.getByRole('button', { name: '新建标签页' }).parentElement).toHaveClass('paw-browser-tabstrip');
  });

  it('names each tab with its real load state and keeps one resting close affordance', () => {
    render(
      <BrowserTabStrip onClose={() => undefined} onNewTab={() => undefined} onSelect={() => undefined} tabs={tabs} />,
    );
    const strip = screen.getByRole('tablist', { name: 'PAW Browser 标签页' });
    const rendered = within(strip).getAllByRole('tab');
    expect(rendered[0]).toHaveAttribute('title', '文档');
    expect(rendered[1]).toHaveAttribute('title', '加载中页面（正在加载）');
    expect(rendered[3]).toHaveAttribute('title', '坏页面（加载失败）');
    // The tablist owns tabs only. Close affordances are inert pointer targets
    // on the tab surface so they cannot violate aria-required-children.
    expect(within(strip).queryByRole('button')).toBeNull();
    expect(document.querySelectorAll('.paw-browser-tab-close')).toHaveLength(4);
    expect(document.querySelector('.paw-browser-tab-close') as HTMLElement).toHaveAttribute('data-tab-close-label', '关闭标签页');
    expect(document.querySelector('[data-tab-close-label="坏页面"]')).toBeInTheDocument();
    expect(document.querySelector('[data-tab-close-label="新标签页"]')).toBeInTheDocument();
    expect(browserTabTooltip({ id: 'x', title: '', active: false, loading: true })).toBe('新标签页（正在加载）');
  });

  it('reports selection, close, and new-tab intents with the exact tab id', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const onNewTab = vi.fn();
    const onSelect = vi.fn();
    render(<BrowserTabStrip onClose={onClose} onNewTab={onNewTab} onSelect={onSelect} tabs={tabs} />);
    await user.click(screen.getByRole('tab', { name: /加载中页面/ }));
    expect(onSelect).toHaveBeenCalledWith('b');
    await user.click(document.querySelector('.paw-browser-tab-close') as HTMLElement);
    expect(onClose).toHaveBeenCalledWith('a');
    await user.click(document.querySelector('[data-tab-close-label="坏页面"]') as HTMLElement);
    expect(onClose).toHaveBeenCalledWith('d');
    await user.click(screen.getByRole('button', { name: '新建标签页' }));
    expect(onNewTab).toHaveBeenCalledTimes(1);
  });

  it('closes any tab on middle click without changing the selection', () => {
    const onClose = vi.fn();
    const onSelect = vi.fn();
    render(<BrowserTabStrip onClose={onClose} onNewTab={() => undefined} onSelect={onSelect} tabs={tabs} />);
    fireEvent(
      screen.getByRole('tab', { name: /坏页面/ }),
      new MouseEvent('auxclick', { bubbles: true, button: 1 }),
    );
    expect(onClose).toHaveBeenCalledWith('d');
    expect(onSelect).not.toHaveBeenCalled();
  });

  it('follows the tablist keyboard pattern with a roving tab stop', () => {
    const onClose = vi.fn();
    const onSelect = vi.fn();
    render(<BrowserTabStrip onClose={onClose} onNewTab={() => undefined} onSelect={onSelect} tabs={tabs} />);
    const rendered = screen.getAllByRole('tab');
    expect(rendered[0]).toHaveAttribute('tabindex', '0');
    expect(rendered[1]).toHaveAttribute('tabindex', '-1');
    fireEvent.keyDown(rendered[0], { key: 'ArrowRight' });
    expect(onSelect).toHaveBeenCalledWith('b');
    fireEvent.keyDown(rendered[0], { key: 'End' });
    expect(onSelect).toHaveBeenCalledWith('d');
    fireEvent.keyDown(rendered[0], { key: 'ArrowLeft' });
    expect(onSelect).toHaveBeenCalledTimes(2);
    fireEvent.keyDown(rendered[0], { key: 'Delete' });
    expect(onClose).toHaveBeenCalledWith('a');
  });

  it('falls back to the neutral globe when a reported favicon fails to load', () => {
    render(
      <BrowserTabStrip
        onClose={() => undefined}
        onNewTab={() => undefined}
        onSelect={() => undefined}
        tabs={[{ id: 'c', title: '图标页', active: true, favicon: 'https://example.com/broken.png' }]}
      />,
    );
    const icon = document.querySelector('.paw-browser-tab-icon img');
    expect(icon).not.toBeNull();
    fireEvent.error(icon as Element);
    expect(document.querySelector('.paw-browser-tab-icon img')).toBeNull();
    expect(document.querySelector('.paw-browser-tab-icon svg')).not.toBeNull();
  });
});

describe('BrowserFindBar', () => {
  it('leaves Enter and Escape to the input method until composition is complete', () => {
    const onNext = vi.fn();
    const onClose = vi.fn();
    render(<BrowserFindBar match={null} onChange={vi.fn()} onClose={onClose} onNext={onNext} onPrevious={vi.fn()} value="项目" />);
    const input = screen.getByRole('textbox', { name: '页内查找' });
    fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
    fireEvent.keyDown(input, { key: 'Escape', isComposing: true });
    expect(onNext).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onNext).toHaveBeenCalledTimes(1);
  });
});

describe('BrowserOmnibox', () => {
  it('shows a search icon while the human draft differs from the committed page', async () => {
    const user = userEvent.setup();
    render(<BrowserOmnibox committedUrl="https://example.com/" onNavigate={() => undefined} tabKey="tab-1" />);
    expect(document.querySelector('.paw-lock-icon')).not.toBeNull();
    const input = screen.getByRole('textbox', { name: '页面地址' });
    await user.clear(input);
    await user.type(input, 'paw');
    expect(document.querySelector(".paw-omnibox-icon[data-kind='search']")).not.toBeNull();
    expect(document.querySelector('.paw-lock-icon')).toBeNull();
  });

  it('locks only a committed HTTPS page and marks HTTP as unencrypted', () => {
    const { rerender } = render(
      <BrowserOmnibox committedUrl="https://example.com/" onNavigate={() => undefined} tabKey="tab-1" />,
    );
    expect(screen.getByTitle('连接已加密')).toHaveClass('paw-lock-icon');
    rerender(<BrowserOmnibox committedUrl="http://example.com/" onNavigate={() => undefined} tabKey="tab-1" />);
    expect(screen.getByTitle('连接未加密')).toBeInTheDocument();
    expect(document.querySelector('.paw-lock-icon')).toBeNull();
  });

  it('submits the typed draft and clears it through the clear control', async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    render(<BrowserOmnibox committedUrl="" onNavigate={onNavigate} tabKey="tab-1" />);
    const input = screen.getByRole('textbox', { name: '页面地址' });
    await user.type(input, 'example.com{Enter}');
    expect(onNavigate).toHaveBeenCalledWith('example.com');

    await user.type(input, 'draft text');
    await user.click(screen.getByRole('button', { name: '清除地址' }));
    expect(input).toHaveValue('');
    expect(input).toHaveFocus();
  });

  it('keeps the half-typed draft when the committed guest URL changes underneath', async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <BrowserOmnibox committedUrl="https://one.example/" onNavigate={() => undefined} tabKey="tab-1" />,
    );
    const input = screen.getByRole('textbox', { name: '页面地址' });
    await user.clear(input);
    await user.type(input, 'half-typed dr');
    // A loading/favicon/Agent navigation event updates the committed URL; the
    // human draft must survive it.
    rerender(<BrowserOmnibox committedUrl="https://two.example/" onNavigate={() => undefined} tabKey="tab-1" />);
    expect(input).toHaveValue('half-typed dr');
    expect(document.querySelector(".paw-omnibox-icon[data-kind='search']")).not.toBeNull();

    // Switching to another tab drops the draft and presents that tab's truth.
    rerender(<BrowserOmnibox committedUrl="https://three.example/" onNavigate={() => undefined} tabKey="tab-2" />);
    expect(input).toHaveValue('https://three.example/');
  });

  it('previews the exact URL Enter will open while editing', async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    render(<BrowserOmnibox committedUrl="https://old.example/" onNavigate={onNavigate} tabKey="tab-1" />);
    expect(screen.queryByRole('button', { name: '打开 https://example.com' })).toBeNull();
    const input = screen.getByRole('textbox', { name: '页面地址' });
    await user.clear(input);
    await user.type(input, 'example.com');
    const commit = screen.getByRole('button', { name: '打开 https://example.com' });
    await user.click(commit);
    expect(onNavigate).toHaveBeenCalledWith('example.com');
  });

  it('previews a web search for non-URL drafts and hides the preview at rest', async () => {
    const user = userEvent.setup();
    render(<BrowserOmnibox committedUrl="" onNavigate={() => undefined} tabKey="tab-1" />);
    await user.type(screen.getByRole('textbox', { name: '页面地址' }), 'paw workbench');
    const commit = screen.getByRole('button', { name: '搜索 paw workbench' });
    expect(commit).toHaveTextContent('Google');
    expect(omniboxCommitPreview('https://example.com/', 'https://example.com/')).toBeNull();
    expect(omniboxCommitPreview('', '')).toBeNull();
  });

  it('restores the committed address on Escape', async () => {
    const user = userEvent.setup();
    render(<BrowserOmnibox committedUrl="https://example.com/" onNavigate={() => undefined} tabKey="tab-1" />);
    const input = screen.getByRole('textbox', { name: '页面地址' });
    await user.clear(input);
    await user.type(input, 'half-typed draft');
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(input).toHaveValue('https://example.com/');
  });

  it('emphasizes the real host at rest without changing the field value', async () => {
    const user = userEvent.setup();
    render(
      <BrowserOmnibox committedUrl="https://example.com/path?q=1" onNavigate={() => undefined} tabKey="tab-1" />,
    );
    const input = screen.getByRole('textbox', { name: '页面地址' });
    expect(input).toHaveValue('https://example.com/path?q=1');
    expect(document.querySelector('.paw-omnibox-field[data-presenting]')).not.toBeNull();
    expect(document.querySelector('.paw-omnibox-url-host')).toHaveTextContent('example.com');
    expect(document.querySelector('.paw-omnibox-url-rest')).toHaveTextContent('/path?q=1');
    await user.click(input);
    expect(document.querySelector('.paw-omnibox-presentation')).toBeNull();
    expect(input).toHaveValue('https://example.com/path?q=1');
  });

  it('only styles URL parts that reassemble to the exact committed string', () => {
    expect(committedUrlParts('https://example.com/path?q=1#top')).toEqual({
      scheme: 'https://',
      host: 'example.com',
      rest: '/path?q=1#top',
    });
    expect(committedUrlParts('about:blank')).toBeNull();
    expect(committedUrlParts('not a url')).toBeNull();
    expect(committedUrlParts('https://user:pass@example.com/')).toBeNull();
  });

  it('hands the caret to the address bar on first open with nothing committed', () => {
    render(<BrowserOmnibox committedUrl="about:blank" onNavigate={() => undefined} tabKey="tab-1" />);
    expect(screen.getByRole('textbox', { name: '页面地址' })).toHaveFocus();
  });

  it('focuses the address bar when a fresh blank tab becomes current, not for pages', () => {
    const { rerender } = render(
      <BrowserOmnibox committedUrl="https://example.com/" onNavigate={() => undefined} tabKey="tab-1" />,
    );
    const input = screen.getByRole('textbox', { name: '页面地址' });
    expect(input).not.toHaveFocus();

    rerender(<BrowserOmnibox committedUrl="about:blank" onNavigate={() => undefined} tabKey="tab-2" />);
    expect(input).toHaveFocus();

    // Switching onward to a tab that already shows a page leaves focus alone…
    input.blur();
    rerender(<BrowserOmnibox committedUrl="https://example.com/" onNavigate={() => undefined} tabKey="tab-3" />);
    expect(input).not.toHaveFocus();
    // …and coming back to the still-blank tab hands the caret over again,
    // exactly like returning to a desktop browser's new-tab page.
    rerender(<BrowserOmnibox committedUrl="about:blank" onNavigate={() => undefined} tabKey="tab-2" />);
    expect(input).toHaveFocus();
  });

  it('never pulls the caret out of a field the person is already typing in', async () => {
    const user = userEvent.setup();
    const { rerender } = render(
      <>
        <input aria-label="外部输入" />
        <BrowserOmnibox committedUrl="https://example.com/" onNavigate={() => undefined} tabKey="tab-1" />
      </>,
    );
    const field = screen.getByRole('textbox', { name: '外部输入' });
    await user.click(field);

    rerender(
      <>
        <input aria-label="外部输入" />
        <BrowserOmnibox committedUrl="about:blank" onNavigate={() => undefined} tabKey="tab-2" />
      </>,
    );
    expect(field).toHaveFocus();
  });

  it('keeps focus on a selected blank tab so keyboard navigation and Delete remain available', () => {
    const content = (tabKey: string, committedUrl: string) => <>
      <button role="tab" aria-selected="true">新标签页</button>
      <BrowserOmnibox committedUrl={committedUrl} onNavigate={() => undefined} tabKey={tabKey} />
    </>;
    const view = render(content('tab-1', 'https://example.com/'));
    const tab = screen.getByRole('tab', { name: '新标签页' });
    tab.focus();
    view.rerender(content('tab-2', 'about:blank'));
    expect(tab).toHaveFocus();
  });
});

describe('BrowserPageStatus', () => {
  afterEach(() => {
    delete (navigator as { clipboard?: unknown }).clipboard;
  });

  it('renders nothing while the page is healthy', () => {
    const { container } = render(<BrowserPageStatus onRetry={() => undefined} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('explains a main-frame load failure and offers one real retry', async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    render(
      <BrowserPageStatus failure={{ code: -105, description: '', url: 'https://missing.example/' }} onRetry={onRetry} />,
    );
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('找不到这个网站');
    expect(alert).toHaveTextContent('https://missing.example/');
    expect(alert).toHaveTextContent('错误代码 -105');
    expect(alert).toHaveTextContent('恢复在当前共享标签页内完成');
    await user.click(screen.getByRole('button', { name: '重新加载' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('shows the raw error code as evidence next to a named description', () => {
    render(
      <BrowserPageStatus
        failure={{ code: -106, description: 'ERR_INTERNET_DISCONNECTED', url: 'https://x.example/' }}
        onRetry={() => undefined}
      />,
    );
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('网络已断开');
    expect(alert).toHaveTextContent('ERR_INTERNET_DISCONNECTED');
    expect(alert).toHaveTextContent('错误代码 -106');
  });

  it('copies the exact failed address and reports the copy truthfully', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(
      <BrowserPageStatus failure={{ code: -7, description: '', url: 'https://slow.example/a' }} onRetry={() => undefined} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /复制网址/ }));
    expect(writeText).toHaveBeenCalledWith('https://slow.example/a');
    expect(await screen.findByRole('button', { name: /已复制/ })).toBeInTheDocument();
  });

  it('reports a copy failure instead of pretending success', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {} });
    render(
      <BrowserPageStatus failure={{ code: -7, description: '', url: 'https://slow.example/a' }} onRetry={() => undefined} />,
    );
    fireEvent.click(screen.getByRole('button', { name: /复制网址/ }));
    expect(await screen.findByRole('button', { name: /复制失败/ })).toBeInTheDocument();
  });

  it('reports a gone page process ahead of an older load failure', () => {
    render(
      <BrowserPageStatus
        crashedReason="crashed"
        failure={{ code: -105, description: '', url: 'https://missing.example/' }}
        onRetry={() => undefined}
      />,
    );
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('页面渲染进程崩溃');
    expect(alert).toHaveTextContent('进程退出原因 crashed');
    expect(screen.queryByText('https://missing.example/')).toBeNull();
    expect(screen.queryByRole('button', { name: /复制网址/ })).toBeNull();
  });
});
