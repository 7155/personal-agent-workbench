import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MotionProvider, useMotionPreference } from '@/design/motion';
import { DesktopPetSurface } from './desktop-pet-surface';
import { emptyPetCounts, unavailablePetSnapshot, type PetSnapshot } from './desktop-pet-snapshot';

beforeEach(() => { vi.spyOn(document, 'hasFocus').mockReturnValue(true); vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible'); localStorage.removeItem('rag-ime-control-motion'); });

afterEach(() => { cleanup(); localStorage.removeItem('paw:chat-presentation:v1'); delete window.pawDesktopPet; localStorage.removeItem('rag-ime-control-motion'); delete document.documentElement.dataset.reduceMotion; delete document.documentElement.dataset.motionPreference; vi.restoreAllMocks(); });
function MotionControls() { const { setPreference } = useMotionPreference(); return <><button onClick={() => setPreference('reduce')}>减少动态效果</button><button onClick={() => setPreference('full')}>恢复动态效果</button></>; }
function renderPet(ready = Promise.resolve(unavailablePetSnapshot()), controls = false) {
  let listener: (snapshot: PetSnapshot) => void = () => {};
  const unsubscribe = vi.fn();
  const host = { ready: vi.fn().mockReturnValue(ready),
    onSnapshot: vi.fn((next: typeof listener) => { listener = next; return unsubscribe; }),
    hide: vi.fn().mockResolvedValue(undefined), openAssistant: vi.fn().mockResolvedValue(undefined), openVoiceSettings: vi.fn().mockResolvedValue(undefined),
    openConversation: vi.fn().mockResolvedValue(undefined), setExpanded: vi.fn().mockResolvedValue(undefined),
    drag: vi.fn().mockResolvedValue(undefined), move: vi.fn().mockResolvedValue(undefined) };
  window.pawDesktopPet = host;
  const view = render(<MotionProvider>{controls ? <MotionControls /> : null}<DesktopPetSurface /></MotionProvider>);
  return { host, view, unsubscribe, push: (snapshot: PetSnapshot) => act(() => listener(snapshot)),
    button: view.container.querySelector<HTMLButtonElement>('.desktop-pet__planet')!, directory: screen.getByRole('button', { name: '查看后台对话' }), handle: screen.getByRole('button', { name: '移动桌面伙伴' }) };
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
  it('opens the same persistent assistant and the existing voice settings without dispatching a conversation', async () => {
    const { host } = renderPet();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '与星伴对话' }));
    await user.click(screen.getByRole('button', { name: '语音输入设置' }));
    expect(host.openAssistant).toHaveBeenCalledOnce();
    expect(host.openVoiceSettings).toHaveBeenCalledOnce();
    expect(host.openConversation).not.toHaveBeenCalled();
    expect(host.setExpanded).not.toHaveBeenCalled();
  });

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
    expect(host.openAssistant).toHaveBeenCalledOnce();
    expect(host.setExpanded).not.toHaveBeenCalled();
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
    const { host, button, directory, push, view } = renderPet(); push(liveSnapshot());
    expect(view.container.querySelector('[data-room-planet="0"]')).toBeTruthy();
    expect(view.container.querySelector('[data-activity="static"]')).toBeTruthy();
    expect(button.querySelector('.desktop-pet-status')).toHaveAttribute('data-state', 'running');
    expect(button).toHaveAccessibleDescription('2 个对话进行中');
    expect(host.ready).toHaveBeenCalledTimes(1);
    fireEvent.click(directory);
    await screen.findByRole('region', { name: '后台对话' });
    expect(host.setExpanded).toHaveBeenCalledWith(true); expect(host.openAssistant).not.toHaveBeenCalled();
    expect(screen.getByText('当前目录另有 1 个')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: /检查项目/ }));
    expect(host.openConversation).toHaveBeenCalledWith({ id: 'session-one', producerEpoch: 3, sourceId: 'work-directory', scopeId: 's' });
  });
  it('keeps one stable face across status changes and gives every list state a shape plus text', async () => {
    const { button, directory, push, view } = renderPet(); push(liveSnapshot());
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
    fireEvent.click(directory); await screen.findByRole('region', { name: '后台对话' });
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
    expect(host.openAssistant).not.toHaveBeenCalled();
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
    const { host, directory, push } = renderPet(); push(liveSnapshot()); const user = userEvent.setup();
    directory.focus(); await user.keyboard('{Enter}');
    await screen.findByRole('region', { name: '后台对话' });
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('region')).toBeNull());
    expect(directory).toHaveFocus(); expect(host.hide).not.toHaveBeenCalled();
    await user.keyboard(' '); await screen.findByRole('region', { name: '后台对话' });
    await user.keyboard('{Escape}'); await waitFor(() => expect(screen.queryByRole('region')).toBeNull());
    await user.keyboard('{Escape}'); expect(host.hide).toHaveBeenCalledTimes(1);
  });
  it('reports expansion failure and keeps the compact state', async () => {
    const { host, directory } = renderPet(); host.setExpanded.mockRejectedValueOnce(new Error('unavailable'));
    fireEvent.click(directory); await screen.findByRole('alert');
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
    const { directory, push } = renderPet(); push(liveSnapshot()); fireEvent.click(directory);
    const conversation = await screen.findByRole('button', { name: /检查项目/ });
    expect(conversation).toHaveFocus();
    push({ ...unavailablePetSnapshot(), producerEpoch: 4 });
    expect(directory).toHaveFocus();
  });
  it('does not transfer focused conversation identity across a producer replacement', async () => {
    vi.spyOn(document, 'hasFocus').mockReturnValue(true);
    const { directory, push } = renderPet(); push(liveSnapshot()); fireEvent.click(directory);
    const previous = await screen.findByRole('button', { name: /检查项目/ }); expect(previous).toHaveFocus();
    push({ ...liveSnapshot(), producerEpoch: 4, scopeId: 'other', conversations: [{ id: 'session-one', label: '另一个连接', state: 'idle' }] });
    expect(previous.isConnected).toBe(false); expect(directory).toHaveFocus();
  });
  it('does not steal focus from another control or an inactive window after a list update', async () => {
    const active = vi.spyOn(document, 'hasFocus').mockReturnValue(true);
    const { directory, push } = renderPet(); push(liveSnapshot()); fireEvent.click(directory);
    await screen.findByRole('button', { name: /检查项目/ });
    const close = screen.getByRole('button', { name: '收起对话列表' }); close.focus();
    push({ ...liveSnapshot(), revision: 3, conversations: [] }); expect(close).toHaveFocus();
    push({ ...liveSnapshot(), revision: 4 });
    screen.getByRole('button', { name: /检查项目/ }).focus(); active.mockReturnValue(false);
    fireEvent(window, new Event('blur'));
    push({ ...unavailablePetSnapshot(), producerEpoch: 4 });
    expect(directory).not.toHaveFocus();
  });
  it('uses the planet as the native assistant entry for pointer, Enter and Space without creating or expanding work', async () => {
    const { button, host } = renderPet(); const user = userEvent.setup();
    expect(button).toHaveAccessibleName('与星伴对话');
    await user.click(button);
    button.focus(); await user.keyboard('{Enter}'); await user.keyboard(' ');
    expect(host.openAssistant).toHaveBeenCalledTimes(3);
    expect(host.setExpanded).not.toHaveBeenCalled();
    expect(host.openConversation).not.toHaveBeenCalled();
    expect(host.ready).toHaveBeenCalledOnce();
  });
  it('pauses a hidden running planet and resumes its existing surface phase when visible', async () => {
    const { button, push, host } = renderPet(); push(liveSnapshot());
    const avatar = button.querySelector('[data-avatar-variant="sphere"]')!;
    const surface = button.querySelector<SVGGElement>('.sphere-surface')!;
    const signal = button.querySelector('.desktop-pet-status')!;
    await waitFor(() => expect(Number(surface.dataset.longitude)).toBeGreaterThan(0));
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    fireEvent(document, new Event('visibilitychange'));
    expect(avatar).toHaveAttribute('data-motion', 'static');
    expect(signal).toHaveAttribute('data-motion-active', 'false');
    const paused = Number(surface.dataset.longitude);
    push({ ...liveSnapshot(), revision: 3 });
    expect(Number(surface.dataset.longitude)).toBe(paused);
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    fireEvent(document, new Event('visibilitychange'));
    expect(button.querySelector('[data-avatar-variant="sphere"]')).toBe(avatar);
    expect(button.querySelector('.sphere-surface')).toBe(surface);
    expect(avatar).toHaveAttribute('data-motion', 'full');
    expect(signal).toHaveAttribute('data-motion-active', 'true');
    await waitFor(() => expect(Number(surface.dataset.longitude)).toBeGreaterThan(paused));
    expect(host.ready).toHaveBeenCalledOnce();
    expect(host.openAssistant).not.toHaveBeenCalled();
    expect(host.hide).not.toHaveBeenCalled();
  });
  it('shows a visible unfocused showInactive pet with working motion without claiming focus', async () => {
    vi.mocked(document.hasFocus).mockReturnValue(false);
    const focus = vi.spyOn(HTMLElement.prototype, 'focus');
    const { button, push, host } = renderPet(); push(liveSnapshot());
    const avatar = button.querySelector('[data-avatar-variant="sphere"]')!;
    const surface = button.querySelector<SVGGElement>('.sphere-surface')!;
    const signal = button.querySelector('.desktop-pet-status')!;
    expect(avatar).toHaveAttribute('data-motion', 'full');
    expect(signal).toHaveAttribute('data-motion-active', 'true');
    await waitFor(() => expect(Number(surface.dataset.longitude)).toBeGreaterThan(0));
    fireEvent(window, new Event('blur'));
    const continued = Number(surface.dataset.longitude);
    await waitFor(() => expect(Number(surface.dataset.longitude)).toBeGreaterThan(continued));
    expect(avatar).toHaveAttribute('data-motion', 'full');
    expect(signal).toHaveAttribute('data-motion-active', 'true');
    expect(screen.getByRole('status')).toHaveTextContent('2 个对话进行中');
    expect(focus).not.toHaveBeenCalled();
    expect(host.ready).toHaveBeenCalledOnce();
    expect(host.openAssistant).not.toHaveBeenCalled(); expect(host.hide).not.toHaveBeenCalled();
  });
  it('keeps the same working surface after focus moves back to another App', async () => {
    const { button, push } = renderPet(); push(liveSnapshot());
    const avatar = button.querySelector('[data-avatar-variant="sphere"]')!;
    const surface = button.querySelector<SVGGElement>('.sphere-surface')!;
    await waitFor(() => expect(Number(surface.dataset.longitude)).toBeGreaterThan(0));
    vi.mocked(document.hasFocus).mockReturnValue(false);
    fireEvent(window, new Event('blur'));
    expect(avatar).toHaveAttribute('data-motion', 'full');
    const continued = Number(surface.dataset.longitude);
    push({ ...liveSnapshot(), revision: 3 });
    fireEvent(window, new Event('focus'));
    expect(button.querySelector('.sphere-surface')).toBe(surface);
    await waitFor(() => expect(Number(surface.dataset.longitude)).toBeGreaterThan(continued));
  });
  it('keeps directory expansion failure and assistant failure on their original focused controls', async () => {
    const { button, directory, host } = renderPet(); const user = userEvent.setup();
    host.setExpanded.mockRejectedValueOnce(new Error('native rejected'));
    directory.focus(); await user.keyboard('{Enter}');
    await screen.findByRole('alert'); expect(directory).toHaveFocus();
    expect(screen.queryByRole('region', { name: '后台对话' })).toBeNull();
    host.openAssistant.mockRejectedValueOnce(new Error('native rejected'));
    button.focus(); await user.keyboard(' ');
    await screen.findByRole('alert'); expect(button).toHaveFocus();
    expect(host.openAssistant).toHaveBeenCalledOnce();
    expect(host.setExpanded).toHaveBeenCalledOnce();
  });

  it.each(['attention', 'error'] as const)('does not replay the original %s directory arrival after hidden/visible recovery', state => {
    const { button, push } = renderPet();
    const next: PetSnapshot = { ...liveSnapshot(), counts: { ...emptyPetCounts(), [state]: 1 }, conversations: [{ id: 'session-one', label: '检查项目', state }] };
    push(next); const signal = button.querySelector('.desktop-pet-status')!;
    expect(signal).toHaveAttribute('data-arrival-active', 'true');
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    fireEvent(document, new Event('visibilitychange'));
    expect(signal).toHaveAttribute('data-arrival-active', 'false');
    push({ ...next, revision: 3 });
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    fireEvent(document, new Event('visibilitychange'));
    expect(button.querySelector('.desktop-pet-status')).toBe(signal);
    expect(signal).toHaveAttribute('data-state', state);
    expect(signal).toHaveAttribute('data-arrival-active', 'false');
  });

  it('pauses user-reduced motion and resumes the original working phase without opening work', async () => {
    const { button, push, host } = renderPet(undefined, true); push(liveSnapshot());
    const surface = button.querySelector<SVGGElement>('.sphere-surface')!;
    const avatar = button.querySelector('[data-avatar-variant="sphere"]')!;
    await waitFor(() => expect(Number(surface.dataset.longitude)).toBeGreaterThan(0));
    fireEvent.click(screen.getByRole('button', { name: '减少动态效果' }));
    expect(avatar).toHaveAttribute('data-motion', 'static');
    const paused = Number(surface.dataset.longitude);
    push({ ...liveSnapshot(), revision: 3 }); expect(Number(surface.dataset.longitude)).toBe(paused);
    fireEvent.click(screen.getByRole('button', { name: '恢复动态效果' }));
    expect(button.querySelector('.sphere-surface')).toBe(surface);
    expect(avatar).toHaveAttribute('data-motion', 'full');
    await waitFor(() => expect(Number(surface.dataset.longitude)).toBeGreaterThan(paused));
    expect(host.openAssistant).not.toHaveBeenCalled(); expect(host.setExpanded).not.toHaveBeenCalled();
  });

});
