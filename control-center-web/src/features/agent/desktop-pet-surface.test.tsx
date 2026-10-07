import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DesktopPetSurface } from './desktop-pet-surface';
import { emptyPetCounts, unavailablePetSnapshot, type PetSnapshot } from './desktop-pet-snapshot';

afterEach(() => { cleanup(); localStorage.removeItem('paw:chat-presentation:v1'); delete window.pawDesktopPet; vi.restoreAllMocks(); });
function renderPet(ready = Promise.resolve(unavailablePetSnapshot())) {
  let listener: (snapshot: PetSnapshot) => void = () => {};
  const unsubscribe = vi.fn();
  const host = { ready: vi.fn().mockReturnValue(ready),
    onSnapshot: vi.fn((next: typeof listener) => { listener = next; return unsubscribe; }),
    hide: vi.fn().mockResolvedValue(undefined), openAssistant: vi.fn().mockResolvedValue(undefined),
    openConversation: vi.fn().mockResolvedValue(undefined), setExpanded: vi.fn().mockResolvedValue(undefined),
    drag: vi.fn().mockResolvedValue(undefined), move: vi.fn().mockResolvedValue(undefined) };
  window.pawDesktopPet = host;
  const view = render(<DesktopPetSurface />);
  return { host, view, unsubscribe, push: (snapshot: PetSnapshot) => act(() => listener(snapshot)),
    button: screen.getByRole('button', { name: '查看后台对话' }), handle: screen.getByRole('button', { name: '移动桌面伙伴' }) };
}
function pointer(button: HTMLElement, type: string, options = {}) {
  const event = new Event(type, { bubbles: true });
  Object.assign(event, { pointerId: 1, button: 0, isPrimary: true, screenX: 0, screenY: 0, ...options });
  fireEvent(button, event);
}
function liveSnapshot(): PetSnapshot {
  return { schemaVersion: 1, producerEpoch: 3, revision: 2, sourceId: 'work-directory', scopeId: 's', freshness: 'synced',
    counts: { ...emptyPetCounts(), running: 2 }, conversations: [{ id: 'session-one', label: '检查项目', state: 'running' }] };
}

describe('single planet companion', () => {
  it('rolls back only its body while retaining the authoritative seven-state signal and native keyboard action', async () => {
    const { button, push, host } = renderPet(); push(liveSnapshot());
    expect(button.querySelector('[data-avatar-variant="sphere"]')).toBeTruthy();
    expect(button.querySelector('.sphere-signal')).toBeNull();
    expect(button.querySelector('[data-avatar-variant="sphere"]')).toHaveAttribute('data-signal', 'working');
    const signal = button.querySelector('.desktop-pet-status');
    act(() => {
      localStorage.setItem('paw:chat-presentation:v1', JSON.stringify({ 'builtin:desktop-pet': { version: 'v1' } }));
      window.dispatchEvent(new StorageEvent('storage', { key: 'paw:chat-presentation:v1' }));
    });
    expect(button.querySelector('image')).toBeTruthy();
    expect(button.querySelector('.desktop-pet-status')).toBe(signal);
    expect(signal).toHaveAttribute('data-state', 'running');
    button.focus(); await userEvent.setup().keyboard('{Enter}');
    expect(host.setExpanded).toHaveBeenCalledWith(true);
  });

  it('moves from the keyboard and ends move mode without hiding the companion', async () => {
    const { host, handle } = renderPet(); const user = userEvent.setup();
    handle.focus(); await user.keyboard('{Enter}');
    expect(handle).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('status')).toHaveTextContent('方向键移动，Esc 结束');
    await user.keyboard('{ArrowLeft}{ArrowDown}');
    expect(host.move.mock.calls).toEqual([['left'], ['down']]);
    await user.keyboard('{Escape}');
    expect(handle).toHaveAttribute('aria-pressed', 'false');
    expect(host.hide).not.toHaveBeenCalled();
    expect(handle).toHaveFocus();
    await user.keyboard(' ');
    expect(handle).toHaveAttribute('aria-pressed', 'true');
    await user.keyboard('{Enter}');
    expect(handle).toHaveAttribute('aria-pressed', 'false');
  });
  it('reuses the bundled planet with a separate running signal and opens a bounded list before any conversation action', async () => {
    const { host, button, push, view } = renderPet(); push(liveSnapshot());
    expect(view.container.querySelector('[data-room-planet="0"]')).toBeTruthy();
    expect(view.container.querySelector('[data-activity="static"]')).toBeTruthy();
    expect(button.querySelector('.desktop-pet-status')).toHaveAttribute('data-state', 'running');
    expect(button).toHaveAccessibleDescription('2 个对话进行中');
    expect(host.ready).toHaveBeenCalledTimes(1);
    fireEvent.click(button);
    await screen.findByRole('region', { name: '后台对话' });
    expect(host.setExpanded).toHaveBeenCalledWith(true); expect(host.openAssistant).not.toHaveBeenCalled();
    expect(screen.getByText('当前目录另有 1 个')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /检查项目/ }));
    expect(host.openConversation).toHaveBeenCalledWith({ id: 'session-one', producerEpoch: 3, sourceId: 'work-directory', scopeId: 's' });
  });
  it('keeps one stable face across status changes and gives every list state a shape plus text', async () => {
    const { button, push, view } = renderPet(); push(liveSnapshot());
    const avatar = button.querySelector('[data-room-planet="0"]');
    const expression = avatar?.getAttribute('data-expression');
    const states = ['running', 'attention', 'error', 'paused', 'idle', 'terminal', 'unknown'] as const;
    const snapshot: PetSnapshot = { ...liveSnapshot(), revision: 3,
      counts: { running: 1, attention: 1, error: 1, paused: 1, idle: 1, terminal: 1, unknown: 1 },
      conversations: states.map(state => ({ id: state, label: `对话 ${state}`, state })) };
    push(snapshot);
    expect(button.querySelector('[data-room-planet="0"]')).toBe(avatar);
    expect(avatar).toHaveAttribute('data-expression', expression);
    expect(button.querySelector('.desktop-pet-status')).toHaveAttribute('data-state', 'attention');
    expect(screen.getByRole('status')).toHaveTextContent('1 个对话待查看 · 1 个进行中');
    push({ ...snapshot, revision: 4, counts: { ...snapshot.counts, attention: 0 },
      conversations: snapshot.conversations.filter(item => item.state !== 'attention') });
    const signal = button.querySelector('.desktop-pet-status');
    expect(signal).toHaveAttribute('data-state', 'error');
    expect(avatar).toHaveAttribute('data-signal', 'idle');
    expect(avatar).toHaveAttribute('data-expression', expression);
    push({ ...snapshot, revision: 5, counts: { ...snapshot.counts, attention: 0 },
      conversations: snapshot.conversations.filter(item => item.state !== 'attention') });
    expect(button.querySelector('.desktop-pet-status')).toBe(signal);
    push({ ...snapshot, revision: 6 });
    fireEvent.click(button); await screen.findByRole('region', { name: '后台对话' });
    expect(view.container.querySelectorAll('[data-room-planet]')).toHaveLength(1);
    for (const [state, label] of [['running', '进行中'], ['attention', '待查看'], ['error', '出错'],
      ['paused', '已暂停'], ['idle', '空闲'], ['terminal', '已结束'], ['unknown', '未同步']]) {
      const row = screen.getByRole('button', { name: `对话 ${state}${label}` });
      expect(row.querySelector('.desktop-pet-status')).toHaveAttribute('data-state', state);
      expect(row.querySelector('.desktop-pet-status')).toHaveAttribute('data-motion-active', 'false');
    }
  });
  it('separates dragging from opening and cleans cancelled gestures', () => {
    const { host, handle, button } = renderPet();
    pointer(handle, 'pointerdown'); pointer(handle, 'pointermove', { screenX: 10 }); pointer(handle, 'pointercancel');
    fireEvent.click(button);
    expect(host.drag.mock.calls).toEqual([['start'], ['move'], ['cancel']]);
    expect(host.setExpanded).not.toHaveBeenCalled();
    expect(host.openConversation).not.toHaveBeenCalled();
  });
  it('bounds pointer movement to one in-flight IPC call', () => {
    const { host, handle } = renderPet();
    host.drag.mockImplementation((phase: string) => phase === 'move' ? new Promise(() => {}) : Promise.resolve());
    pointer(handle, 'pointerdown');
    for (let x = 5; x < 100; x += 1) pointer(handle, 'pointermove', { screenX: x });
    pointer(handle, 'pointerup');
    expect(host.drag.mock.calls).toEqual([['start'], ['move'], ['end']]);
  });
  it('supports keyboard expand, Escape collapse with focus return, then Escape hide', async () => {
    const { host, button, push } = renderPet(); push(liveSnapshot()); const user = userEvent.setup();
    button.focus(); await user.keyboard('{Enter}');
    await screen.findByRole('region', { name: '后台对话' });
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('region')).toBeNull());
    expect(button).toHaveFocus(); expect(host.hide).not.toHaveBeenCalled();
    await user.keyboard(' '); await screen.findByRole('region', { name: '后台对话' });
    await user.keyboard('{Escape}'); await waitFor(() => expect(screen.queryByRole('region')).toBeNull());
    await user.keyboard('{Escape}'); expect(host.hide).toHaveBeenCalledTimes(1);
  });
  it('reports expansion failure and keeps the compact state', async () => {
    const { host, button } = renderPet(); host.setExpanded.mockRejectedValueOnce(new Error('unavailable'));
    fireEvent.click(button); await screen.findByRole('alert');
    expect(screen.queryByRole('region')).toBeNull();
  });
  it('does not pretend the ordinary browser has native window actions', () => {
    render(<DesktopPetSurface />);
    expect(screen.getByText('请从 PAW 桌面端开启')).toBeTruthy();
    expect(screen.getByRole('button', { name: '查看后台对话' })).toBeDisabled();
  });
  it('accepts pushed state before ready resolves and rejects late replay', async () => {
    let resolve!: (snapshot: PetSnapshot) => void;
    const { push, host, view } = renderPet(new Promise(done => { resolve = done; }));
    const snapshot = liveSnapshot(); push(snapshot);
    expect(screen.getByRole('status')).toHaveTextContent('2 个对话进行中');
    await act(async () => resolve({ ...snapshot, revision: 1, counts: emptyPetCounts(), conversations: [] }));
    expect(screen.getByRole('status')).toHaveTextContent('2 个对话进行中');
    expect(host.onSnapshot.mock.invocationCallOrder[0]).toBeLessThan(host.ready.mock.invocationCallOrder[0]);
    push({ ...unavailablePetSnapshot(), producerEpoch: 4 });
    expect(screen.getByRole('status')).toHaveTextContent('状态未同步');
    expect(view.container.querySelector('[data-activity="static"]')).toBeTruthy();
  });
  it('unsubscribes and ignores a late ready response after unmount', async () => {
    let resolve!: (snapshot: PetSnapshot) => void;
    const { view, unsubscribe } = renderPet(new Promise(done => { resolve = done; }));
    view.unmount(); expect(unsubscribe).toHaveBeenCalledTimes(1);
    await act(async () => resolve(unavailablePetSnapshot())); expect(screen.queryByRole('status')).toBeNull();
  });
  it('recovers focus when the focused conversation disappears from the current window', async () => {
    vi.spyOn(document, 'hasFocus').mockReturnValue(true);
    const { button, push } = renderPet(); push(liveSnapshot()); fireEvent.click(button);
    const conversation = await screen.findByRole('button', { name: /检查项目/ });
    expect(conversation).toHaveFocus();
    push({ ...unavailablePetSnapshot(), producerEpoch: 4 });
    expect(button).toHaveFocus();
  });
  it('does not transfer focused conversation identity across a producer replacement', async () => {
    vi.spyOn(document, 'hasFocus').mockReturnValue(true);
    const { button, push } = renderPet(); push(liveSnapshot()); fireEvent.click(button);
    const previous = await screen.findByRole('button', { name: /检查项目/ }); expect(previous).toHaveFocus();
    push({ ...liveSnapshot(), producerEpoch: 4, scopeId: 'other', conversations: [{ id: 'session-one', label: '另一个连接', state: 'idle' }] });
    expect(previous.isConnected).toBe(false); expect(button).toHaveFocus();
  });
  it('does not steal focus from another control or an inactive window after a list update', async () => {
    const active = vi.spyOn(document, 'hasFocus').mockReturnValue(true);
    const { button, push } = renderPet(); push(liveSnapshot()); fireEvent.click(button);
    await screen.findByRole('button', { name: /检查项目/ });
    const close = screen.getByRole('button', { name: '收起对话列表' }); close.focus();
    push({ ...liveSnapshot(), revision: 3, conversations: [] }); expect(close).toHaveFocus();
    push({ ...liveSnapshot(), revision: 4 });
    screen.getByRole('button', { name: /检查项目/ }).focus(); active.mockReturnValue(false);
    fireEvent(window, new Event('blur'));
    push({ ...unavailablePetSnapshot(), producerEpoch: 4 });
    expect(button).not.toHaveFocus();
  });
});
