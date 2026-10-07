import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useState } from 'react';
import { ChatPresentationProvider, CHAT_PRESENTATION_STORAGE_KEY, useChatPresentation } from './chat-presentation';
import { ChatPresentationSettings } from './ChatPresentationSettings';

afterEach(() => { cleanup(); vi.restoreAllMocks(); localStorage.removeItem(CHAT_PRESENTATION_STORAGE_KEY); });

function Chat({ label }: { label: string }) {
  const presentation = useChatPresentation();
  const [draft, setDraft] = useState('');
  return <section aria-label={label} data-chat-presentation-version={presentation?.version}>
    <input aria-label={`${label}草稿`} value={draft} onChange={event => setDraft(event.target.value)} />
    <ChatPresentationSettings />
  </section>;
}

describe('per-owner chat presentation', () => {
  it('changes and rolls back one App without remounting its draft or changing another App', () => {
    render(<><ChatPresentationProvider ownerKey="extension:one"><Chat label="甲" /></ChatPresentationProvider>
      <ChatPresentationProvider ownerKey="extension:two"><Chat label="乙" /></ChatPresentationProvider></>);
    const draft = screen.getByRole('textbox', { name: '甲草稿' });
    fireEvent.change(draft, { target: { value: '保留当前草稿' } });
    fireEvent.click(screen.getAllByRole('button', { name: '新版 v2' })[0]!);
    expect(screen.getByRole('region', { name: '甲' })).toHaveAttribute('data-chat-presentation-version', 'v2');
    expect(screen.getByRole('region', { name: '乙' })).toHaveAttribute('data-chat-presentation-version', 'v1');
    expect(screen.getByRole('textbox', { name: '甲草稿' })).toBe(draft);
    expect(draft).toHaveValue('保留当前草稿');
    fireEvent.click(screen.getByRole('button', { name: '恢复上一显示版本 v1' }));
    expect(screen.getByRole('region', { name: '甲' })).toHaveAttribute('data-chat-presentation-version', 'v1');
    expect(draft).toHaveValue('保留当前草稿');
    expect(JSON.parse(localStorage.getItem(CHAT_PRESENTATION_STORAGE_KEY)!)).toEqual({
      'extension:one': { version: 'v1', previousVersion: 'v2' },
    });
  });

  it('restores the selected owner after remount and synchronizes storage changes without resetting input', () => {
    localStorage.setItem(CHAT_PRESENTATION_STORAGE_KEY, JSON.stringify({ 'extension:one': { version: 'v2', previousVersion: 'v1' } }));
    render(<ChatPresentationProvider ownerKey="extension:one"><Chat label="甲" /></ChatPresentationProvider>);
    const draft = screen.getByRole('textbox', { name: '甲草稿' });
    fireEvent.change(draft, { target: { value: '跨窗口更新保留' } });
    expect(screen.getByRole('region', { name: '甲' })).toHaveAttribute('data-chat-presentation-version', 'v2');
    act(() => {
      localStorage.setItem(CHAT_PRESENTATION_STORAGE_KEY, JSON.stringify({ 'extension:one': { version: 'v1' } }));
      window.dispatchEvent(new StorageEvent('storage', { key: CHAT_PRESENTATION_STORAGE_KEY }));
    });
    expect(screen.getByRole('region', { name: '甲' })).toHaveAttribute('data-chat-presentation-version', 'v1');
    expect(screen.getByRole('textbox', { name: '甲草稿' })).toBe(draft);
    expect(draft).toHaveValue('跨窗口更新保留');
  });

  it('uses the declared default for invalid persisted versions and keeps old reading preferences untouched', () => {
    localStorage.setItem('paw:conversation-reading:v1', '{"size":"large","spacing":"compact","motion":"reduced"}');
    localStorage.setItem(CHAT_PRESENTATION_STORAGE_KEY, JSON.stringify({ 'builtin:room': { version: 2, previousVersion: 'v9' } }));
    render(<ChatPresentationProvider ownerKey="builtin:room" defaultVersion="v2"><Chat label="Room" /></ChatPresentationProvider>);
    expect(screen.getByRole('region', { name: 'Room' })).toHaveAttribute('data-chat-presentation-version', 'v2');
    expect(screen.queryByRole('button', { name: /恢复上一显示版本/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '经典 v1' }));
    expect(localStorage.getItem('paw:conversation-reading:v1')).toBe('{"size":"large","spacing":"compact","motion":"reduced"}');
    localStorage.removeItem('paw:conversation-reading:v1');
  });

  it('only offers explicitly supported versions', () => {
    render(<ChatPresentationProvider ownerKey="extension:pinned" availableVersions={['v1']}><Chat label="固定" /></ChatPresentationProvider>);
    expect(screen.getByRole('button', { name: '经典 v1' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByRole('button', { name: '新版 v2' })).not.toBeInTheDocument();
  });

  it('keeps selection and rollback usable when storage denies writes', () => {
    render(<ChatPresentationProvider ownerKey="extension:private"><Chat label="私密" /></ChatPresentationProvider>);
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('storage unavailable'); });
    fireEvent.click(screen.getByRole('button', { name: '新版 v2' }));
    expect(screen.getByRole('region', { name: '私密' })).toHaveAttribute('data-chat-presentation-version', 'v2');
    fireEvent.click(screen.getByRole('button', { name: '恢复上一显示版本 v1' }));
    expect(screen.getByRole('region', { name: '私密' })).toHaveAttribute('data-chat-presentation-version', 'v1');
  });
});
