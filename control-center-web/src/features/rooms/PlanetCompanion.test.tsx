import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MotionActivityBoundary, MotionProvider, useMotionPreference } from '@/design/motion';
import { ChatPresentationProvider, CHAT_PRESENTATION_STORAGE_KEY, useChatPresentation } from '@/features/conversation-ui/reading/chat-presentation';
import { RoomPlanetAvatar, type RoomPlanetActivity } from './RoomPlanetAvatar';
import type { PlanetExpression, PlanetMotionMode, PlanetSignalState } from './planet-companion-protocol';
import { ROOM_PLANET_NAMES } from './room-copy';
import { PLANET_ACTIVITY_SIGNAL, PLANET_EXPRESSIONS } from './planet-companion-protocol';
import css from './planet-companion.css?inline';
let reduced = false;
const listeners = new Set<() => void>();
beforeEach(() => {
  reduced = false; listeners.clear();
  localStorage.removeItem('rag-ime-control-motion'); localStorage.removeItem(CHAT_PRESENTATION_STORAGE_KEY);
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  vi.stubGlobal('matchMedia', () => ({ get matches() { return reduced; }, addEventListener: (_: string, callback: () => void) => listeners.add(callback), removeEventListener: (_: string, callback: () => void) => listeners.delete(callback) }));
});
afterEach(() => {
  cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers();
  localStorage.removeItem('rag-ime-control-motion'); localStorage.removeItem(CHAT_PRESENTATION_STORAGE_KEY);
  delete document.documentElement.dataset.reduceMotion;
});
function Controls() {
  const display = useChatPresentation()!;
  const motion = useMotionPreference();
  return <><button onClick={() => display.setVersion('v1')}>经典</button><button onClick={() => display.rollback()}>返回</button><button onClick={() => motion.setPreference('reduce')}>减少</button></>;
}
function Hosted({ activity = 'working', active = true, expression, signalState, motion }: { activity?: RoomPlanetActivity; active?: boolean; expression?: PlanetExpression; signalState?: PlanetSignalState; motion?: PlanetMotionMode }) {
  return <MotionProvider><style>{css}</style><ChatPresentationProvider ownerKey="planet-behavior" defaultVersion="v2"><Controls/><MotionActivityBoundary active={active}><RoomPlanetAvatar family="paw" ordinal={4} activity={activity} expression={expression} signalState={signalState} motion={motion}/></MotionActivityBoundary></ChatPresentationProvider></MotionProvider>;
}
function progress() { return screen.getByRole('img', { name: 'Saturn' }).querySelector('.paw-planet-progress')!; }
function badge() { return screen.getByRole('img', { name: 'Saturn' }).querySelector('.paw-planet-badge')!; }
describe('PAW original planet family', () => {
  it('keeps legacy ownerless calls and explicit classic opt-out on the original atlas', () => {
    const view = render(<RoomPlanetAvatar ordinal={0}/>);
    expect(screen.getByRole('img', { name: 'Earth' }).querySelector('image')).toBeInTheDocument();
    view.rerender(<ChatPresentationProvider ownerKey="opt-out" defaultVersion="v2"><RoomPlanetAvatar ordinal={0} family="classic"/></ChatPresentationProvider>);
    expect(screen.getByRole('img', { name: 'Earth' })).toHaveAttribute('data-family', 'classic-v1');
  });
  it('uses all eight original identities only with explicit candidate selection', () => {
    render(<ChatPresentationProvider ownerKey="eight" defaultVersion="v2">{ROOM_PLANET_NAMES.map((_, ordinal) => <RoomPlanetAvatar family="paw" key={ordinal} ordinal={ordinal} size={24}/>)}<RoomPlanetAvatar family="paw" ordinal={0} decorative/></ChatPresentationProvider>);
    for (const [ordinal, name] of ROOM_PLANET_NAMES.entries()) {
      const image = screen.getByRole('img', { name });
      expect(image).toHaveAttribute('data-room-planet', String(ordinal));
      expect(image).toHaveAttribute('data-family', 'paw-v2');
      expect(image.querySelector('[data-surface]')).toHaveAttribute('data-surface', name);
      expect(image.querySelector('image')).not.toBeInTheDocument();
      expect(image).toHaveAttribute('width', '24');
    }
    expect(screen.getAllByRole('img')).toHaveLength(8);
  });
  it('keeps the rejected candidate out of v2 defaults and preserves classic across version rollback', () => {
    render(<MotionProvider><ChatPresentationProvider ownerKey="default-family" defaultVersion="v2"><Controls/><RoomPlanetAvatar ordinal={4} activity="working"/></ChatPresentationProvider></MotionProvider>);
    expect(screen.getByRole('img', { name: 'Saturn' })).toHaveAttribute('data-family', 'classic-v1');
    fireEvent.click(screen.getByRole('button', { name: '经典' }));
    expect(screen.getByRole('img', { name: 'Saturn' })).toHaveAttribute('data-family', 'classic-v1');
    fireEvent.click(screen.getByRole('button', { name: '返回' }));
    expect(screen.getByRole('img', { name: 'Saturn' })).toHaveAttribute('data-family', 'classic-v1');
    expect(screen.getByRole('img', { name: 'Saturn' })).toHaveAttribute('data-activity', 'working');
    expect(screen.getByRole('img', { name: 'Saturn' })).toHaveAttribute('data-room-planet', '4');
  });
  it.each(['static', 'idle', 'waiting', 'error', 'stopped'] as const)('keeps %s quiet with its truthful expression', activity => {
    render(<Hosted activity={activity}/>);
    expect(getComputedStyle(badge()).animation).toBe('none');
    expect(screen.getByRole('img', { name: 'Saturn' })).toHaveAttribute('data-signal-state', PLANET_ACTIVITY_SIGNAL[activity]);
    expect(screen.getByRole('img', { name: 'Saturn' })).toHaveAttribute('data-expression', 'neutral');
  });
  it('pauses current work immediately for inactive, hidden and OS/application reduced motion', () => {
    const view = render(<Hosted/>);
    const avatar = screen.getByRole('img', { name: 'Saturn' });
    expect(getComputedStyle(progress()).animation).toContain('paw-planet-progress');
    view.rerender(<Hosted active={false}/>);
    expect(getComputedStyle(progress()).animation).toBe('none');
    expect(avatar).toHaveAttribute('data-expression', 'neutral');
    view.rerender(<Hosted/>);
    act(() => { vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden'); document.dispatchEvent(new Event('visibilitychange')); });
    expect(getComputedStyle(progress()).animation).toBe('none');
    act(() => { vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible'); document.dispatchEvent(new Event('visibilitychange')); reduced = true; listeners.forEach(callback => callback()); });
    expect(getComputedStyle(progress()).animation).toBe('none');
    act(() => { reduced = false; listeners.forEach(callback => callback()); });
    expect(getComputedStyle(progress()).animation).toContain('paw-planet-progress');
    fireEvent.click(screen.getByRole('button', { name: '减少' }));
    expect(getComputedStyle(progress()).animation).toBe('none');
    expect(screen.getByRole('img', { name: 'Saturn' })).toBe(avatar);
  });
  it('plays done once on transition, never on history mount or reactivation', () => {
    vi.useFakeTimers();
    const view = render(<Hosted activity="done"/>);
    const avatar = screen.getByRole('img', { name: 'Saturn' });
    expect(avatar).toHaveAttribute('data-state-pulse', 'false');
    view.rerender(<Hosted activity="working"/>); view.rerender(<Hosted activity="done"/>);
    expect(avatar).toHaveAttribute('data-state-pulse', 'true');
    expect(getComputedStyle(badge()).animation).toContain('240ms');
    act(() => { vi.advanceTimersByTime(240); });
    expect(avatar).toHaveAttribute('data-state-pulse', 'false');
    view.rerender(<Hosted activity="done" active={false}/>); view.rerender(<Hosted activity="done"/>);
    expect(avatar).toHaveAttribute('data-state-pulse', 'false');
    expect(getComputedStyle(badge()).animation).toBe('none');
  });
  it('keeps completed history quiet on the first known snapshot and after unknown recovery', () => {
    const view = render(<Hosted activity="static"/>);
    const avatar = screen.getByRole('img', { name: 'Saturn' });
    view.rerender(<Hosted activity="done"/>);
    expect(avatar).toHaveAttribute('data-state-pulse', 'false');
    view.rerender(<Hosted activity="working"/>);
    view.rerender(<Hosted activity="static"/>);
    view.rerender(<Hosted activity="done"/>);
    expect(avatar).toHaveAttribute('data-state-pulse', 'false');
  });
  it('keeps the settled check fully drawn and renders a waiting bubble outside the face', () => {
    const view = render(<Hosted activity="done"/>);
    const check = screen.getByRole('img', { name: 'Saturn' }).querySelector('.paw-planet-check');
    expect(check).toHaveAttribute('pathLength', '1');
    expect(check).toHaveAttribute('stroke-dasharray', '1');
    expect(check).toHaveAttribute('stroke-dashoffset', '0');
    view.rerender(<Hosted activity="waiting"/>);
    const bubble = screen.getByRole('img', { name: 'Saturn' }).querySelector('[data-feature="question-bubble"]');
    expect(bubble).toBeInTheDocument();
    expect(bubble?.closest('.paw-planet-signal')).toBeInTheDocument();
    expect(bubble?.closest('.paw-planet-face')).toBeNull();
  });
  it('keeps a happy face unchanged when error, waiting, stopped and offline signals change', () => {
    const view = render(<Hosted expression="happy"/>);
    const avatar = screen.getByRole('img', { name: 'Saturn' });
    const face = Array.from(avatar.querySelectorAll('.paw-planet-face path')).map(path => path.getAttribute('d'));
    for (const activity of ['error', 'waiting', 'stopped'] as const) {
      view.rerender(<Hosted expression="happy" activity={activity}/>);
      expect(avatar).toHaveAttribute('data-expression', 'happy');
      expect(Array.from(avatar.querySelectorAll('.paw-planet-face path')).map(path => path.getAttribute('d'))).toEqual(face);
      expect(avatar).toHaveAttribute('data-signal-state', PLANET_ACTIVITY_SIGNAL[activity]);
    }
    view.rerender(<Hosted expression="happy" signalState="offline"/>);
    expect(avatar).toHaveAttribute('data-motion', 'static');
    expect(Array.from(avatar.querySelectorAll('.paw-planet-face path')).map(path => path.getAttribute('d'))).toEqual(face);
    expect(getComputedStyle(badge()).animation).toBe('none');
  });
  it('retains the status-specific signal color instead of applying the last status to every signal', () => {
    const view = render(<Hosted activity="idle"/>);
    const signal = screen.getByRole('img', { name: 'Saturn' }).querySelector('.paw-planet-signal')!;
    // The CSS cascade is checked even in hosts where semantic variables are resolved later.
    expect(getComputedStyle(signal).color).toBe('var(--color-workspace-secondary)');
    view.rerender(<Hosted activity="done"/>);
    expect(getComputedStyle(signal).color).toBe('var(--color-success)');
    view.rerender(<Hosted activity="error"/>);
    expect(getComputedStyle(signal).color).toBe('var(--color-danger)');
    view.rerender(<Hosted signalState="offline"/>);
    expect(getComputedStyle(signal).color).toBe('var(--color-text-tertiary)');
  });
  it.each(['thinking', 'working'] as const)('maps legacy %s to working without changing the default neutral face', activity => {
    render(<Hosted activity={activity}/>);
    expect(screen.getByRole('img', { name: 'Saturn' })).toHaveAttribute('data-signal-state', 'working');
    expect(screen.getByRole('img', { name: 'Saturn' })).toHaveAttribute('data-expression', 'neutral');
    expect(getComputedStyle(progress()).animation).toContain('paw-planet-progress');
  });
  it('uses a fixed working arc in transition/static modes and interrupts one-shot waiting/error signals', () => {
    const view = render(<Hosted motion="transition"/>);
    const avatar = screen.getByRole('img', { name: 'Saturn' });
    expect(getComputedStyle(progress()).animation).toBe('none');
    view.rerender(<Hosted motion="static"/>);
    expect(getComputedStyle(progress()).animation).toBe('none');
    view.rerender(<Hosted activity="waiting" motion="transition"/>);
    expect(avatar).toHaveAttribute('data-state-pulse', 'true');
    expect(getComputedStyle(badge()).animation).toContain('paw-planet-signal-enter');
    view.rerender(<Hosted activity="error" motion="transition"/>);
    expect(getComputedStyle(badge()).animation).toContain('paw-planet-error');
    view.rerender(<Hosted activity="error" active={false}/>);
    expect(avatar).toHaveAttribute('data-state-pulse', 'false');
    expect(getComputedStyle(badge()).animation).toBe('none');
    view.rerender(<Hosted activity="error"/>);
    expect(avatar).toHaveAttribute('data-state-pulse', 'false');
  });
  it('exposes sixteen distinct facial poses without changing body identity', () => {
    const view = render(<RoomPlanetAvatar ordinal={0} family="paw"/>);
    const faces = new Set<string>();
    for (const [expression] of PLANET_EXPRESSIONS) {
      view.rerender(<RoomPlanetAvatar ordinal={0} family="paw" expression={expression}/>);
      const avatar = screen.getByRole('img', { name: 'Earth' });
      expect(avatar).toHaveAttribute('data-expression', expression);
      expect(avatar.querySelector('[data-surface]')).toHaveAttribute('data-surface', 'Earth');
      faces.add(avatar.querySelector('.paw-planet-face')!.innerHTML);
    }
    expect(faces.size).toBe(16);
  });
  it('retargets one facial layer and immediately settles interrupted work for reduced motion', () => {
    const view = render(<Hosted expression="happy" activity="error"/>);
    const avatar = screen.getByRole('img', { name: 'Saturn' });
    const path = avatar.querySelector('.paw-planet-face path');
    view.rerender(<Hosted expression="curious" activity="error"/>);
    expect(avatar.querySelectorAll('.paw-planet-face')).toHaveLength(1);
    expect(avatar.querySelector('.paw-planet-face')).toHaveAttribute('data-face-expression', 'curious');
    expect(avatar.querySelector('.paw-planet-face path')).toBe(path);
    expect(avatar).toHaveAttribute('data-state-pulse', 'false');
    view.rerender(<Hosted expression="wink" activity="error"/>);
    expect(avatar.querySelector('.paw-planet-face path')).toBe(path);
    expect(avatar.querySelector('.paw-planet-face')).toHaveAttribute('data-face-expression', 'wink');
    fireEvent.click(screen.getByRole('button', { name: '减少' }));
    expect(avatar.querySelector('.paw-planet-face')).toHaveAttribute('data-face-motion', 'static');
    expect(avatar.querySelector('.paw-planet-face path')).not.toBe(path);
    view.rerender(<Hosted expression="happy" signalState="offline"/>);
    expect(avatar.querySelector('.paw-planet-face')).toHaveAttribute('data-face-motion', 'static');
    expect(avatar.querySelectorAll('.paw-planet-face')).toHaveLength(1);
  });
  it('preserves distinct silhouettes and face proportions in the shared expression language', () => {
    render(<ChatPresentationProvider ownerKey="proportions" defaultVersion="v2">{ROOM_PLANET_NAMES.map((_, ordinal) => <RoomPlanetAvatar family="paw" key={ordinal} ordinal={ordinal} size={32}/>)}</ChatPresentationProvider>);
    expect(screen.getByRole('img', { name: 'Mars' }).querySelector('[data-body="rock-flat"]')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Mercury' }).querySelector('[data-body="rock-compact"]')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Jupiter' }).querySelector('[data-body="wide"]')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Neptune' }).querySelector('[data-body="tall"]')).toBeInTheDocument();
    const faces = ROOM_PLANET_NAMES.map(name => screen.getByRole('img', { name }).querySelector('.paw-planet-face')!.innerHTML);
    expect(new Set(faces).size).toBe(8);
  });
  it('frames small bodies tightly while preserving both ring silhouettes and large framing', () => {
    const view = render(<RoomPlanetAvatar ordinal={0} family="paw" size={32}/>);
    expect(screen.getByRole('img', { name: 'Earth' })).toHaveAttribute('viewBox', '22 14 92 86');
    view.rerender(<RoomPlanetAvatar ordinal={4} family="paw" size={32}/>);
    expect(screen.getByRole('img', { name: 'Saturn' })).toHaveAttribute('viewBox', '8 14 106 84');
    view.rerender(<RoomPlanetAvatar ordinal={7} family="paw" size={32}/>);
    expect(screen.getByRole('img', { name: 'Uranus' })).toHaveAttribute('viewBox', '24 14 90 92');
    view.rerender(<RoomPlanetAvatar ordinal={0} family="paw" size={160}/>);
    expect(screen.getByRole('img', { name: 'Earth' })).toHaveAttribute('viewBox', '0 0 120 120');
  });
});
