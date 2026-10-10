import { act, cleanup, fireEvent, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MotionActivityBoundary, MotionProvider } from '@/design/motion';
import { RoomPlanetAvatar } from './RoomPlanetAvatar';
import { PLANET_EXPRESSIONS, type PlanetSignalState } from './sphere-avatar/sphere-avatar-protocol';
import { pose } from './sphere-avatar/sphere-avatar-geometry';
import sphereCss from './sphere-avatar/sphere-planet-avatar.css?inline';
let osReduce = false;
const listeners = new Set<() => void>();
beforeEach(() => {
  localStorage.clear(); osReduce = false; listeners.clear();
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  vi.stubGlobal('matchMedia', (query: string) => ({ get matches() { return query.includes('prefers-reduced-motion') ? osReduce : true; },
    addEventListener: (_: string, f: () => void) => listeners.add(f), removeEventListener: (_: string, f: () => void) => listeners.delete(f) }));
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); vi.useRealTimers(); });
function Hosted({ signal = 'working', active = true, expression = 'happy', mode = 'full' }: {
  signal?: PlanetSignalState; active?: boolean; expression?: 'happy' | 'sad'; mode?: 'full' | 'transition' | 'static';
}) {
  return <MotionProvider><style>{sphereCss}</style><MotionActivityBoundary active={active}>
    <RoomPlanetAvatar ordinal={0} variant="sphere" signal={signal} expression={expression} motion={mode} interactive/>
  </MotionActivityBoundary></MotionProvider>;
}
describe('explicit sphere avatar integration', () => {
  it('accepts an explicit local arrival once while seeds and quiet recovery stay static', () => {
    // Only the notice timeout belongs to this check; retain the real surface clock.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const tree = (signal: PlanetSignalState, arrivalKey: string | null, active = true) => <MotionProvider><MotionActivityBoundary active={active}>
      <RoomPlanetAvatar ordinal={0} variant="sphere" signal={signal} motion="full" arrivalKey={arrivalKey}/>
    </MotionActivityBoundary></MotionProvider>;
    const view = render(tree('idle', null)), avatar = view.container.querySelector('svg')!;
    view.rerender(tree('error', null));
    expect(avatar).toHaveAttribute('data-pulse', 'false');
    view.rerender(tree('working', null));
    view.rerender(tree('error', 'public:1'));
    expect(avatar).toHaveAttribute('data-pulse', 'true');
    view.rerender(tree('error', 'public:1'));
    expect(avatar).toHaveAttribute('data-pulse', 'true');
    act(() => { vi.advanceTimersByTime(400); });
    expect(avatar).toHaveAttribute('data-pulse', 'false');
    view.rerender(tree('error', 'public:1'));
    expect(avatar).toHaveAttribute('data-pulse', 'false');
    view.rerender(tree('working', null, false));
    view.rerender(tree('error', 'public:2', false));
    view.rerender(tree('error', 'public:2'));
    expect(avatar).toHaveAttribute('data-pulse', 'false');
    expect(view.container.querySelector('svg')).toBe(avatar);
  });

  it('opts in only the three supported ordinal identities and keeps a real classic rollback', () => {
    const avatars = (variant: 'classic' | 'sphere') => <MotionProvider>{[0, 1, 2, 4].map(ordinal => <RoomPlanetAvatar key={ordinal} ordinal={ordinal} variant={variant}/>)}</MotionProvider>;
    const view = render(avatars('sphere'));
    expect(view.container.querySelectorAll('[data-avatar-variant="sphere"]')).toHaveLength(3);
    expect(view.container.querySelector('[data-room-planet="2"] image')).toBeTruthy();
    view.rerender(avatars('classic'));
    expect(view.container.querySelectorAll('image')).toHaveLength(4);
    expect(view.container.querySelector('[data-avatar-variant="sphere"]')).toBeNull();
  });
  it('never celebrates loaded history or unknown-to-done, but draws a single observed active completion', () => {
    vi.useFakeTimers();
    const view = render(<Hosted signal="done"/>), avatar = view.container.querySelector('svg')!;
    expect(avatar).toHaveAttribute('data-pulse', 'false');
    view.rerender(<Hosted signal="idle"/>); view.rerender(<Hosted signal="done"/>);
    expect(avatar).toHaveAttribute('data-pulse', 'false');
    view.rerender(<Hosted signal="working"/>); view.rerender(<Hosted signal="done"/>);
    expect(avatar).toHaveAttribute('data-pulse', 'true');
    act(() => { vi.advanceTimersByTime(400); });
    expect(avatar).toHaveAttribute('data-pulse', 'false');
    view.rerender(<Hosted signal="done" expression="sad"/>);
    expect(avatar).toHaveAttribute('data-pulse', 'false');
    expect(avatar.querySelector('.sphere-check')).toBeTruthy();
    view.rerender(<Hosted signal="working" active={false}/>); view.rerender(<Hosted signal="done" active={false}/>);
    view.rerender(<Hosted signal="done"/>);
    expect(avatar).toHaveAttribute('data-pulse', 'false');
  });
  it('keeps expression orthogonal to error/offline/Stop and retains a single face topology', () => {
    const view = render(<Hosted/>), avatar = view.container.querySelector('svg')!;
    const face = avatar.querySelector('[data-face]')!;
    const paths = Array.from(face.querySelectorAll('path'));
    for (const signal of ['error', 'offline', 'done', 'waiting'] as const) {
      view.rerender(<Hosted signal={signal}/>);
      expect(avatar).toHaveAttribute('data-expression', 'happy');
      expect(avatar.querySelector('[data-face]')).toBe(face);
      expect(Array.from(face.querySelectorAll('path'))).toEqual(paths);
    }
    view.rerender(<MotionProvider><RoomPlanetAvatar ordinal={0} variant="sphere" activity="stopped" expression="happy"/></MotionProvider>);
    const stopped = view.container.querySelector('svg')!;
    expect(stopped).toHaveAttribute('data-signal', 'idle');
    expect(stopped).toHaveAttribute('data-motion', 'static');
    expect(stopped).toHaveAttribute('data-expression', 'happy');
    expect(new Set(PLANET_EXPRESSIONS.map(([name]) => JSON.stringify(pose(name)))).size).toBe(16);
  });
  it('uses the real activity, visibility and OS owners to cancel blink/gaze and all signal loops', () => {
    const cancel = vi.fn(), animation = { cancel, onfinish: undefined };
    const animate = vi.fn(() => animation);
    const view = render(<Hosted/>), avatar = view.container.querySelector('svg')!;
    Object.defineProperty(avatar.querySelector('.sphere-eye-rig'), 'animate', { value: animate });
    fireEvent.click(avatar);
    expect(animate).toHaveBeenCalledTimes(1);
    expect(avatar).toHaveAttribute('data-blink-count', '1');
    view.rerender(<Hosted active={false}/>);
    expect(cancel).toHaveBeenCalled(); expect(avatar).toHaveAttribute('data-motion', 'static');
    expect(getComputedStyle(avatar.querySelector('.sphere-progress')!).animation).toBe('none');
    fireEvent.click(avatar); expect(animate).toHaveBeenCalledTimes(1);
    view.rerender(<Hosted/>);
    act(() => { osReduce = true; listeners.forEach(f => f()); });
    expect(avatar).toHaveAttribute('data-motion', 'static');
    act(() => { osReduce = false; listeners.forEach(f => f()); });
    act(() => { vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden'); document.dispatchEvent(new Event('visibilitychange')); });
    expect(avatar).toHaveAttribute('data-motion', 'static');
    act(() => { vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible'); document.dispatchEvent(new Event('visibilitychange')); });
    view.rerender(<Hosted signal="offline"/>); fireEvent.click(avatar);
    expect(animate).toHaveBeenCalledTimes(1);
    expect(avatar).toHaveAttribute('data-gaze', '0,0');
    expect(avatar.querySelector('.sphere-progress')).toBeNull();
  });
  it('retargets one mouth with the selected face while six signals change their color and preserve expression', () => {
    const view = render(<Hosted signal="idle"/>), avatar = view.container.querySelector('svg')!;
    const mouth = avatar.querySelector('[data-mouth]');
    expect(mouth).toBeTruthy();
    const colors = new Set<string>();
    for (const signal of ['idle', 'working', 'waiting', 'done', 'error', 'offline'] as const) {
      view.rerender(<Hosted signal={signal}/>);
      expect(avatar.querySelector('[data-mouth]')).toBe(mouth);
      expect(avatar).toHaveAttribute('data-expression', 'happy');
      expect(mouth).toHaveAttribute('data-mouth', 'happy');
      colors.add(avatar.querySelector('[data-satellite-color]')!.getAttribute('data-satellite-color')!);
    }
    expect(colors.size).toBe(6);
    expect(avatar.querySelectorAll('[data-mouth]')).toHaveLength(1);
  });

  it('projects only the three clipped textures and freezes their actual geometry for quiet boundaries', async () => {
    const tree = (signal: PlanetSignalState, mode: 'full' | 'transition' | 'static' = 'full', active = true) => <MotionProvider><style>{sphereCss}</style><MotionActivityBoundary active={active}>
      {[0, 1, 4].map(ordinal => <RoomPlanetAvatar key={ordinal} ordinal={ordinal} variant="sphere" signal={signal} motion={mode}/>)}
    </MotionActivityBoundary></MotionProvider>;
    const view = render(tree('working'));
    const textures = Array.from(view.container.querySelectorAll('[data-surface]'));
    expect(textures.map(node => node.getAttribute('data-surface'))).toEqual(['continents', 'craters', 'bands']);
    for (const texture of textures) {
      expect(texture.closest('[clip-path]')).toBeTruthy();
      expect(texture).toHaveAttribute('data-projection', 'orthographic');
      expect(texture).toHaveAttribute('data-rotation-active', 'true');
      expect(getComputedStyle(texture).transform).toBe('none');
      expect(texture.querySelector('[data-face]')).toBeNull();
      expect(texture.querySelector('.sphere-signal')).toBeNull();
    }
    const first = textures[0].querySelector('path')!, before = first.getAttribute('d');
    await waitFor(() => expect(first.getAttribute('d')).not.toBe(before));
    for (const signal of ['idle', 'waiting', 'done', 'error', 'offline'] as const) {
      view.rerender(tree(signal));
      for (const texture of textures) expect(texture).toHaveAttribute('data-rotation-active', 'false');
    }
    for (const mode of ['transition', 'static'] as const) {
      view.rerender(tree('working', mode));
      for (const texture of textures) expect(texture).toHaveAttribute('data-rotation-active', 'false');
    }
    view.rerender(tree('working', 'full', false));
    for (const texture of textures) expect(texture).toHaveAttribute('data-rotation-active', 'false');
    const paused = textures.map(texture => texture.innerHTML);
    await new Promise(resolve => setTimeout(resolve, 80));
    expect(textures.map(texture => texture.innerHTML)).toEqual(paused);
    view.rerender(tree('working'));
    act(() => { osReduce = true; listeners.forEach(f => f()); });
    for (const texture of textures) expect(texture).toHaveAttribute('data-rotation-active', 'false');
  });

  it('keeps a fixed work arc in transition/static and never creates a second clickable control', () => {
    const view = render(<Hosted mode="transition"/>), avatar = view.container.querySelector('svg')!;
    expect(avatar.querySelector('.sphere-progress')).toBeTruthy();
    expect(getComputedStyle(avatar.querySelector('.sphere-progress')!).animation).toBe('none');
    expect(view.container.querySelector('button')).toBeNull();
    view.rerender(<Hosted mode="static"/>);
    expect(avatar).toHaveAttribute('data-motion', 'static');
  });
});
