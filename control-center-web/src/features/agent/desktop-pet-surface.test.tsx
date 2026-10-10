import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MotionProvider, useMotionPreference } from '@/design/motion';
import { DesktopPetSurface } from './desktop-pet-surface';
import { emptyPetCounts, unavailablePetSnapshot, type PetSnapshot } from './desktop-pet-snapshot';
import { RoomPlanetAvatar } from '@/features/rooms/RoomPlanetAvatar';
import petCss from './desktop-pet.css?inline';

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
  it('keeps a readable small mouth on the expanded pet without changing its face or other avatar consumers', async () => {
    const neighbors = render(<MotionProvider><style>{petCss}</style>{[0, 1, 4].map(ordinal =>
      <RoomPlanetAvatar key={ordinal} ordinal={ordinal} variant="sphere" size={64} activity="static" />,
    )}</MotionProvider>);
    const neighborMouths = Array.from(neighbors.container.querySelectorAll<SVGPathElement>('[data-mouth]'));
    const appearance = (mouth: SVGPathElement) => ['d', 'fill', 'stroke', 'stroke-width', 'transform'].map(name => mouth.getAttribute(name));
    const neighborAppearance = neighborMouths.map(appearance);
    const stroke = (mouth: SVGPathElement) => Number.parseFloat(getComputedStyle(mouth).getPropertyValue('stroke-width') || mouth.getAttribute('stroke-width')!);
    const neighborStrokes = neighborMouths.map(stroke);
    const { button, directory, push, host } = renderPet(); push(liveSnapshot());
    const avatar = button.querySelector<SVGSVGElement>('[data-avatar-variant="sphere"]')!;
    const face = avatar.querySelector('[data-face]')!;
    const eyes = avatar.querySelector('.sphere-eye-rig')!;
    const mouth = avatar.querySelector<SVGPathElement>('[data-mouth]')!;
    const original = appearance(mouth), originalStroke = stroke(mouth);
    expect(Number(avatar.getAttribute('width'))).toBe(112);
    fireEvent.click(directory); await screen.findByRole('region', { name: '后台对话' });
    expect(button.querySelector('[data-avatar-variant="sphere"]')).toBe(avatar);
    expect(avatar.querySelector('[data-face]')).toBe(face);
    expect(avatar.querySelector('.sphere-eye-rig')).toBe(eyes);
    expect(avatar.querySelector('[data-mouth]')).toBe(mouth);
    expect(appearance(mouth)).toEqual(original);
    // The real local cascade must leave at least a half CSS pixel of outline
    // at 64px. This is a source sizing check, not a raster/visual verdict.
    const cssStroke = stroke(mouth) * Number(avatar.getAttribute('width')) / 320;
    expect(cssStroke).toBeGreaterThanOrEqual(.5);
    expect(cssStroke).toBeLessThanOrEqual(1);
    push({ ...liveSnapshot(), revision: 3, counts: { ...emptyPetCounts(), error: 1 } });
    expect(avatar.querySelector('[data-mouth]')).toBe(mouth);
    expect(appearance(mouth)).toEqual(original);
    expect(neighborMouths.map(appearance)).toEqual(neighborAppearance);
    expect(neighborMouths.map(stroke)).toEqual(neighborStrokes);
    fireEvent.keyDown(directory, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('region')).toBeNull());
    expect(Number(avatar.getAttribute('width'))).toBe(112);
    expect(stroke(mouth)).toBe(originalStroke);
    expect(appearance(mouth)).toEqual(original);
    expect(directory).toHaveFocus();
    expect(host.openAssistant).not.toHaveBeenCalled(); expect(host.openConversation).not.toHaveBeenCalled();
  });

  it('uses at least caption-sized text for the original status, row state and directory summary', async () => {
    render(<style>{petCss}</style>);
    const { directory, push } = renderPet(); push(liveSnapshot());
    expect(Number.parseFloat(getComputedStyle(screen.getByRole('status')).fontSize)).toBeGreaterThanOrEqual(12);
    fireEvent.click(directory); await screen.findByRole('region', { name: '后台对话' });
    const row = screen.getByRole('button', { name: /检查项目/ });
    expect(Number.parseFloat(getComputedStyle(row.querySelector('small')!).fontSize)).toBeGreaterThanOrEqual(12);
    expect(Number.parseFloat(getComputedStyle(screen.getByText('当前目录另有 1 个')).fontSize)).toBeGreaterThanOrEqual(12);
    expect(row).toHaveAccessibleName('检查项目进行中');
  });

  it('renders accepted waiting and exact completion on one unchanged face, with only one arrival each', () => {
    const { button, push } = renderPet();
    const avatar = button.querySelector('[data-avatar-variant="sphere"]')!;
    const face = avatar.querySelector('[data-face]'), mouth = avatar.querySelector('[data-mouth]');
    const signal = avatar.querySelector('.sphere-signal');
    const working: PetSnapshot = { ...liveSnapshot(), visual: { signal: 'working', motion: 'full', label: '2 个对话进行中', arrivalKey: null } };
    push(working);
    const waiting: PetSnapshot = { ...working, revision: 3, visual: { signal: 'waiting', motion: 'full', label: '有对话等回复', arrivalKey: '3:3' } };
    push(waiting); expect(avatar).toHaveAttribute('data-pulse', 'true');
    expect(screen.getByRole('status')).toHaveTextContent('有对话等回复');
    // A repeated native key neither replaces the signal node nor starts another motion.
    push({ ...waiting, revision: 4 }); expect(avatar).toHaveAttribute('data-pulse', 'true');
    push({ ...working, revision: 5 });
    const done: PetSnapshot = { ...working, revision: 6, counts: { ...emptyPetCounts(), terminal: 1 },
      visual: { signal: 'done', motion: 'full', label: '有对话已完成', arrivalKey: '3:6' } };
    push(done); expect(avatar).toHaveAttribute('data-signal', 'done'); expect(avatar).toHaveAttribute('data-pulse', 'true');
    expect(screen.getByRole('status')).toHaveTextContent('有对话已完成');
    expect(avatar.querySelector('[data-face]')).toBe(face); expect(avatar.querySelector('[data-mouth]')).toBe(mouth);
    expect(avatar.querySelector('.sphere-signal')).toBe(signal);
  });

  it.each(['waiting', 'done'] as const)('seeds retained %s without replay on remount or producer replacement', async signal => {
    const snapshot: PetSnapshot = { ...liveSnapshot(), visual: { signal, motion: 'full', label: '原可信状态', arrivalKey: '3:2' } };
    const first = renderPet(Promise.resolve(snapshot));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('原可信状态'));
    expect(first.button.querySelector('[data-avatar-variant="sphere"]')).toHaveAttribute('data-pulse', 'false');
    first.view.unmount();
    const reopened = renderPet(Promise.resolve(snapshot));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('原可信状态'));
    const avatar = reopened.button.querySelector('[data-avatar-variant="sphere"]')!;
    expect(avatar).toHaveAttribute('data-pulse', 'false');
    reopened.push({ ...snapshot, producerEpoch: 4, revision: 1, visual: { ...snapshot.visual!, arrivalKey: '4:1' } });
    expect(avatar).toHaveAttribute('data-pulse', 'false');
  });

  it.each(['waiting', 'done'] as const)('consumes %s arrivals received while hidden or reduced without replay on resume', signal => {
    const { button, push } = renderPet(undefined, true);
    const working: PetSnapshot = { ...liveSnapshot(), visual: { signal: 'working', motion: 'full', label: '进行中', arrivalKey: null } };
    push(working); const avatar = button.querySelector('[data-avatar-variant="sphere"]')!;
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    fireEvent(document, new Event('visibilitychange'));
    push({ ...working, revision: 3, visual: { signal, motion: 'full', label: '可信状态', arrivalKey: '3:3' } });
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    fireEvent(document, new Event('visibilitychange')); expect(avatar).toHaveAttribute('data-pulse', 'false');
    push({ ...working, revision: 4 });
    fireEvent.click(screen.getByRole('button', { name: '减少动态效果' }));
    push({ ...working, revision: 5, visual: { signal, motion: 'full', label: '可信状态', arrivalKey: '3:5' } });
    fireEvent.click(screen.getByRole('button', { name: '恢复动态效果' }));
    expect(avatar).toHaveAttribute('data-pulse', 'false');
  });

  it('uses trusted shared satellites without guessing waiting, completion or offline', () => {
    const { button, push } = renderPet();
    const avatar = button.querySelector('[data-avatar-variant="sphere"]')!;
    const face = avatar.querySelector('[data-face]');
    const mouth = avatar.querySelector('[data-mouth]');
    const satellite = avatar.querySelector('.sphere-signal');
    expect(satellite).toBeTruthy();
    expect(button.querySelector('.desktop-pet-status')).toBeNull();
    push(liveSnapshot());
    expect(avatar).toHaveAttribute('data-signal', 'working');
    expect(avatar.querySelector('.sphere-signal')).toBe(satellite);
    push({ ...liveSnapshot(), revision: 3, counts: { ...emptyPetCounts(), error: 1 } });
    expect(avatar).toHaveAttribute('data-signal', 'error');
    expect(avatar).toHaveAttribute('data-pulse', 'true');
    expect(avatar.querySelector('.sphere-signal')).toBe(satellite);
    push({ ...liveSnapshot(), revision: 4, counts: { ...emptyPetCounts(), error: 2 } });
    expect(avatar).toHaveAttribute('data-pulse', 'true');
    for (const [state, text] of [['attention', '待查看'], ['paused', '已暂停'], ['terminal', '没有运行中的对话'], ['unknown', '未同步']] as const) {
      push({ ...liveSnapshot(), revision: 5 + ['attention', 'paused', 'terminal', 'unknown'].indexOf(state), counts: { ...emptyPetCounts(), [state]: 1 } });
      expect(avatar).toHaveAttribute('data-signal', 'idle');
      expect(avatar).toHaveAttribute('data-motion', 'static');
      expect(avatar).toHaveAttribute('data-pulse', 'false');
      expect(screen.getByRole('status')).toHaveTextContent(text);
    }
    expect(avatar.querySelector('[data-face]')).toBe(face);
    expect(avatar.querySelector('[data-mouth]')).toBe(mouth);
    expect(button.querySelector('[data-avatar-variant="sphere"]')).toBe(avatar);
  });

  it('seeds historical errors and recovering epochs without replaying shared arrivals', async () => {
    const error: PetSnapshot = { ...liveSnapshot(), counts: { ...emptyPetCounts(), error: 1 } };
    const { button, push, view } = renderPet(Promise.resolve(error));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('出错'));
    const avatar = button.querySelector('[data-avatar-variant="sphere"]')!;
    expect(avatar).toHaveAttribute('data-signal', 'error');
    expect(avatar).toHaveAttribute('data-pulse', 'false');
    push({ ...error, revision: 3, freshness: 'recovering' });
    expect(screen.getByRole('status')).toHaveTextContent('正在重新同步');
    push({ ...error, revision: 4 });
    expect(avatar).toHaveAttribute('data-pulse', 'false');
    push({ ...error, producerEpoch: 4, revision: 1 });
    expect(avatar).toHaveAttribute('data-pulse', 'false');
    view.unmount();
    const reopened = renderPet(Promise.resolve(error));
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('出错'));
    expect(reopened.button.querySelector('[data-avatar-variant="sphere"]')).toHaveAttribute('data-pulse', 'false');
  });

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

  it('rolls back to the original seven-state signal and preserves the native keyboard action', async () => {
    const { button, push, host } = renderPet(); push(liveSnapshot());
    expect(button.querySelector('[data-avatar-variant="sphere"]')).toBeTruthy();
    expect(button.querySelector('.sphere-signal')).toBeTruthy();
    expect(button.querySelector('[data-avatar-variant="sphere"]')).toHaveAttribute('data-signal', 'working');
    expect(button.querySelector('.desktop-pet-status')).toBeNull();
    act(() => {
      localStorage.setItem('paw:chat-presentation:v1', JSON.stringify({ 'builtin:desktop-pet': { version: 'v1' } }));
      window.dispatchEvent(new StorageEvent('storage', { key: 'paw:chat-presentation:v1' }));
    });
    expect(button.querySelector('image')).toBeTruthy();
    const signal = button.querySelector('.desktop-pet-status');
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
    expect(button.querySelector('.sphere-signal')).toHaveAttribute('data-state', 'working');
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
    expect(button).toHaveAttribute('data-directory-state', 'attention');
    expect(avatar).toHaveAttribute('data-signal', 'idle');
    expect(screen.getByRole('status')).toHaveTextContent('1 个对话待查看 · 1 个进行中');
    push({ ...snapshot, revision: 4, counts: { ...snapshot.counts, attention: 0 },
      conversations: snapshot.conversations.filter(item => item.state !== 'attention') });
    const signal = button.querySelector('.sphere-signal');
    expect(signal).toHaveAttribute('data-state', 'error');
    expect(avatar).toHaveAttribute('data-signal', 'error');
    expect(avatar).toHaveAttribute('data-expression', expression);
    push({ ...snapshot, revision: 5, counts: { ...snapshot.counts, attention: 0 },
      conversations: snapshot.conversations.filter(item => item.state !== 'attention') });
    expect(button.querySelector('.sphere-signal')).toBe(signal);
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
    const signal = button.querySelector('.sphere-signal')!;
    await waitFor(() => expect(Number(surface.dataset.longitude)).toBeGreaterThan(0));
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    fireEvent(document, new Event('visibilitychange'));
    expect(avatar).toHaveAttribute('data-motion', 'static');
    expect(avatar).toHaveAttribute('data-motion-active', 'false');
    const paused = Number(surface.dataset.longitude);
    push({ ...liveSnapshot(), revision: 3 });
    expect(Number(surface.dataset.longitude)).toBe(paused);
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    fireEvent(document, new Event('visibilitychange'));
    expect(button.querySelector('[data-avatar-variant="sphere"]')).toBe(avatar);
    expect(button.querySelector('.sphere-surface')).toBe(surface);
    expect(avatar).toHaveAttribute('data-motion', 'full');
    expect(button.querySelector('.sphere-signal')).toBe(signal);
    expect(avatar).toHaveAttribute('data-motion-active', 'true');
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
    const signal = button.querySelector('.sphere-signal')!;
    expect(avatar).toHaveAttribute('data-motion', 'full');
    expect(avatar).toHaveAttribute('data-motion-active', 'true');
    await waitFor(() => expect(Number(surface.dataset.longitude)).toBeGreaterThan(0));
    fireEvent(window, new Event('blur'));
    const continued = Number(surface.dataset.longitude);
    await waitFor(() => expect(Number(surface.dataset.longitude)).toBeGreaterThan(continued));
    expect(avatar).toHaveAttribute('data-motion', 'full');
    expect(button.querySelector('.sphere-signal')).toBe(signal);
    expect(avatar).toHaveAttribute('data-motion-active', 'true');
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
    push(liveSnapshot());
    const next: PetSnapshot = { ...liveSnapshot(), counts: { ...emptyPetCounts(), [state]: 1 }, conversations: [{ id: 'session-one', label: '检查项目', state }] };
    push({ ...next, revision: 3 }); const signal = button.querySelector('.sphere-signal')!;
    const avatar = button.querySelector('[data-avatar-variant="sphere"]')!;
    expect(avatar).toHaveAttribute('data-pulse', state === 'error' ? 'true' : 'false');
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    fireEvent(document, new Event('visibilitychange'));
    expect(avatar).toHaveAttribute('data-pulse', 'false');
    push({ ...next, revision: 4 });
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    fireEvent(document, new Event('visibilitychange'));
    expect(button.querySelector('.sphere-signal')).toBe(signal);
    expect(signal).toHaveAttribute('data-state', state === 'error' ? 'error' : 'idle');
    expect(button).toHaveAttribute('data-directory-state', state);
    expect(avatar).toHaveAttribute('data-pulse', 'false');
  });

  it('does not replay an error received during user-reduced motion', () => {
    const { button, push } = renderPet(undefined, true); push(liveSnapshot());
    const avatar = button.querySelector('[data-avatar-variant="sphere"]')!;
    fireEvent.click(screen.getByRole('button', { name: '减少动态效果' }));
    push({ ...liveSnapshot(), revision: 3, counts: { ...emptyPetCounts(), error: 1 } });
    expect(avatar).toHaveAttribute('data-signal', 'error');
    expect(avatar).toHaveAttribute('data-pulse', 'false');
    fireEvent.click(screen.getByRole('button', { name: '恢复动态效果' }));
    expect(avatar).toHaveAttribute('data-pulse', 'false');
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
