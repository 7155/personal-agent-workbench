import type { ReactNode } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useKnowledgeReadingContext } from './reading-context';

const clients: QueryClient[] = [];
function mount(baseId = 'library-a', windowId = 'window-a') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  return renderHook(() => useKnowledgeReadingContext(baseId, windowId), {
    wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  });
}
function enter(view: ReturnType<typeof mount>, draft: string) {
  act(() => { view.result.current.update(current => ({ ...current, search: { ...current.search, draft } })); });
}

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  clients.splice(0).forEach(client => client.clear());
  vi.restoreAllMocks();
  localStorage.clear();
});

describe('Knowledge reading recovery', () => {
  it('restores unsent text and reading selection with a new QueryClient, without recovering a request or results', () => {
    const first = mount();
    act(() => { first.result.current.update(current => ({
      ...current, documentId: 'document-a', materialsFilter: '报告', readerOrigin: 'search',
      search: { ...current.search, draft: '未发送\n原搜索草稿', query: 'submitted query', selectedId: 'old-hit',
        status: 'pending', error: 'old error', request: 4, overrides: { topK: 3 } },
    })); });
    first.unmount();
    clients[0]?.clear();
    const fresh = mount();
    expect(fresh.result.current.context).toMatchObject({
      documentId: 'document-a', materialsFilter: '报告', readerOrigin: 'search', focusHit: null,
      search: { draft: '未发送\n原搜索草稿', query: '', config: null, overrides: null,
        hits: [], retrieval: null, selectedId: '', status: 'idle', error: '', request: 0 },
    });
    const saved = JSON.parse(localStorage.getItem(localStorage.key(0)!)!);
    expect(Object.keys(saved).sort()).toEqual(['baseId', 'documentId', 'draft', 'materialsFilter', 'readerOrigin', 'version', 'windowId']);
  });

  it('keeps each library and window separate, including IDs containing separators', () => {
    const scopes = [['a:b', 'c', 'first'], ['a', 'b:c', 'second'], ['a:b', 'other', 'third']];
    for (const [base, window, draft] of scopes) {
      const view = mount(base, window);
      enter(view, draft!);
      view.unmount();
    }
    clients.forEach(client => client.clear());
    for (const [base, window, draft] of scopes) {
      const view = mount(base, window);
      expect(view.result.current.context.search.draft).toBe(draft);
      view.unmount();
    }
    expect(mount('missing-library', 'c').result.current.context.search.draft).toBe('');
  });

  it('clears the saved draft rather than restoring older text on another cold mount', () => {
    const first = mount();
    enter(first, 'old draft');
    first.unmount();
    const second = mount();
    expect(second.result.current.context.search.draft).toBe('old draft');
    enter(second, '');
    second.unmount();
    expect(mount().result.current.context.search.draft).toBe('');
  });

  it.each(['{broken', 'null', '[]', '{"version":99}', '{"version":1,"draft":42}'])('ignores damaged or old storage (%s) while keeping new input usable', raw => {
    const first = mount();
    enter(first, 'seed');
    first.unmount();
    localStorage.setItem(localStorage.key(0)!, raw);
    const fresh = mount();
    expect(fresh.result.current.context.search.draft).toBe('');
    enter(fresh, 'replacement');
    fresh.unmount();
    expect(mount().result.current.context.search.draft).toBe('replacement');
  });

  it('rejects a valid record copied from a different window or library', () => {
    const first = mount();
    enter(first, 'private to original window');
    first.unmount();
    const key = localStorage.key(0)!;
    const record = JSON.parse(localStorage.getItem(key)!);
    localStorage.setItem(key, JSON.stringify({ ...record, windowId: 'other-window' }));
    expect(mount().result.current.context.search.draft).toBe('');
    localStorage.setItem(key, JSON.stringify({ ...record, baseId: 'other-library' }));
    expect(mount().result.current.context.search.draft).toBe('');
  });

  it.each(['getItem', 'setItem'] as const)('preserves current input when storage %s is unavailable', async method => {
    vi.spyOn(Storage.prototype, method).mockImplementation(() => { throw new DOMException('Unavailable', 'SecurityError'); });
    const view = mount();
    enter(view, 'still editable');
    await waitFor(() => expect(view.result.current.context.search.draft).toBe('still editable'));
  });

  it('does not persist an unidentified library or window', () => {
    enter(mount(''), 'unbound library');
    enter(mount('library-a', ''), 'unbound window');
    expect(localStorage.length).toBe(0);
  });

  it('keeps input usable when access to localStorage itself is denied', async () => {
    vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => { throw new DOMException('Denied', 'SecurityError'); });
    const view = mount();
    enter(view, 'current input');
    await waitFor(() => expect(view.result.current.context.search.draft).toBe('current input'));
  });
});
