import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MotionActivityBoundary, MotionProvider, useMotionPreference } from '@/design/motion';
import { RoomPlanetAvatar, type RoomPlanetActivity } from './RoomPlanetAvatar';
import avatarCss from './room-planet-avatar.css?inline';

let systemReduced = false;
const mediaListeners = new Set<() => void>();

beforeEach(() => {
  localStorage.removeItem('rag-ime-control-motion');
  systemReduced = false;
  mediaListeners.clear();
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  vi.stubGlobal('matchMedia', () => ({
    get matches() { return systemReduced; },
    addEventListener: (_type: string, listener: () => void) => mediaListeners.add(listener),
    removeEventListener: (_type: string, listener: () => void) => mediaListeners.delete(listener),
  }));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.removeItem('rag-ime-control-motion');
  delete document.documentElement.dataset.reduceMotion;
  delete document.documentElement.dataset.motionPreference;
});

function MotionControls() {
  const { setPreference } = useMotionPreference();
  return <>
    <button onClick={() => setPreference('reduce')}>减少动态效果</button>
    <button onClick={() => setPreference('full')}>恢复动态效果</button>
  </>;
}

function HostedAvatar({ active = true, activity = 'working' }: { active?: boolean; activity?: RoomPlanetActivity }) {
  return <MotionProvider><style>{avatarCss}</style><MotionControls />
    <MotionActivityBoundary active={active}><MotionActivityBoundary active>
      <RoomPlanetAvatar ordinal={4} size={40} activity={activity} />
    </MotionActivityBoundary></MotionActivityBoundary>
  </MotionProvider>;
}

function expectDecorationPaused(avatar: HTMLElement) {
  const decoration = [avatar, ...avatar.querySelectorAll('.room-planet-eye, .room-planet-gaze, .room-planet-mouth')];
  for (const node of decoration) {
    expect(getComputedStyle(node).animation).toBe('none');
    expect(getComputedStyle(node).transform).toBe('none');
  }
  expect(avatar).toHaveAttribute('data-motion-active', 'false');
}

describe('RoomPlanetAvatar shared decorative motion', () => {
  it('pauses every decoration through the real appearance preference while preserving the character and expression', () => {
    render(<HostedAvatar />);
    const avatar = screen.getByRole('img', { name: 'Saturn' });
    const body = avatar.querySelector('image');
    const source = body?.getAttribute('href');
    expect(getComputedStyle(avatar).animation).toContain('room-planet-work');

    fireEvent.click(screen.getByRole('button', { name: '减少动态效果' }));
    expectDecorationPaused(avatar);
    expect(avatar).toHaveAttribute('data-activity', 'working');
    expect(avatar).toHaveAttribute('data-expression', 'talking');
    expect(getComputedStyle(avatar.querySelector('.room-planet-open-mouth')!).opacity).toBe('1');
    expect(avatar).toHaveAttribute('viewBox', '0 0 528 391');
    expect(avatar).toHaveAttribute('preserveAspectRatio', 'xMidYMid meet');
    expect(avatar.querySelector('image')).toBe(body);
    expect(body).toHaveAttribute('href', source);

    fireEvent.click(screen.getByRole('button', { name: '恢复动态效果' }));
    expect(screen.getByRole('img', { name: 'Saturn' })).toBe(avatar);
    expect(avatar).toHaveAttribute('data-motion-active', 'true');
    expect(getComputedStyle(avatar).animation).toContain('room-planet-work');
  });

  it('pauses an inactive host without replacing failed or completed expressions with the default face', () => {
    const view = render(<HostedAvatar active={false} />);
    const avatar = screen.getByRole('img', { name: 'Saturn' });
    expectDecorationPaused(avatar);
    view.rerender(<HostedAvatar active={false} activity="error" />);
    expectDecorationPaused(avatar);
    expect(avatar).toHaveAttribute('data-expression', 'concerned');
    expect(getComputedStyle(avatar.querySelector('.room-planet-concerned-mouth')!).opacity).toBe('1');

    view.rerender(<HostedAvatar active={false} activity="done" />);
    expect(screen.getByRole('img', { name: 'Saturn' })).toBe(avatar);
    expectDecorationPaused(avatar);
    expect(avatar).toHaveAttribute('data-activity', 'done');
    expect(avatar).toHaveAttribute('data-expression', 'happy');
    expect(getComputedStyle(avatar.querySelector('.room-planet-happy-eyes')!).opacity).toBe('1');

    view.rerender(<HostedAvatar activity="thinking" />);
    expect(avatar).toHaveAttribute('data-motion-active', 'true');
    expect(avatar).toHaveAttribute('data-expression', 'curious');
    expect(getComputedStyle(avatar.querySelector('.room-planet-gaze')!).animation).toContain('room-planet-look');
  });

  it.each(['provider', 'isolated'] as const)('reacts to the OS accessibility floor in a %s host', (host) => {
    localStorage.setItem('rag-ime-control-motion', 'full');
    const isolated = <><style>{avatarCss}</style><RoomPlanetAvatar ordinal={0} activity="thinking" /></>;
    render(host === 'provider' ? <MotionProvider>{isolated}</MotionProvider> : isolated);
    const avatar = screen.getByRole('img', { name: 'Earth' });
    act(() => { systemReduced = true; mediaListeners.forEach(listener => listener()); });
    expectDecorationPaused(avatar);
    expect(avatar).toHaveAttribute('data-expression', 'curious');
    act(() => { systemReduced = false; mediaListeners.forEach(listener => listener()); });
    expect(avatar).toHaveAttribute('data-motion-active', 'true');
    expect(getComputedStyle(avatar).animation).toContain('room-planet-think');
  });

  it('stops hidden-document decorations and resumes them without changing current activity', () => {
    render(<HostedAvatar activity="thinking" />);
    const avatar = screen.getByRole('img', { name: 'Saturn' });
    act(() => {
      vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expectDecorationPaused(avatar);
    expect(avatar).toHaveAttribute('data-activity', 'thinking');
    act(() => {
      vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(avatar).toHaveAttribute('data-motion-active', 'true');
  });

  it('retains all eight stable identities and decorative accessibility under an inactive host', () => {
    render(<MotionProvider><MotionActivityBoundary active={false}>
      {Array.from({ length: 8 }, (_, ordinal) => <RoomPlanetAvatar key={ordinal} ordinal={ordinal} />)}
      <RoomPlanetAvatar ordinal={0} decorative />
    </MotionActivityBoundary></MotionProvider>);
    const names = ['Earth', 'Mars', 'Venus', 'Jupiter', 'Saturn', 'Mercury', 'Neptune', 'Uranus'];
    for (const [ordinal, name] of names.entries()) {
      expect(screen.getByRole('img', { name })).toHaveAttribute('data-room-planet', String(ordinal));
    }
    expect(screen.getAllByRole('img')).toHaveLength(8);
  });
});
