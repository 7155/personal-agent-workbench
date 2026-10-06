import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MotionProvider, useMotionPreference } from '@/design/motion';
import { PetStatusSignal } from './desktop-pet-status';
import type { PetConversationState } from './desktop-pet-snapshot';
import petCss from './desktop-pet.css?inline';

beforeEach(() => {
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  localStorage.removeItem('rag-ime-control-motion');
  delete document.documentElement.dataset.reduceMotion;
});
afterEach(() => {
  cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals();
  localStorage.removeItem('rag-ime-control-motion');
  delete document.documentElement.dataset.reduceMotion;
  delete document.documentElement.dataset.motionPreference;
});

function Controls() {
  const { setPreference } = useMotionPreference();
  return <button onClick={() => setPreference('reduce')}>减少动态效果</button>;
}

describe('planet task status shapes', () => {
  it('retains seven distinct geometries without color or animation', () => {
    const states: PetConversationState[] = ['running', 'attention', 'error', 'paused', 'idle', 'terminal', 'unknown'];
    const { container } = render(<><style>{petCss}</style>{states.map(state => <PetStatusSignal key={state} state={state} />)}</>);
    const signals = [...container.querySelectorAll('.desktop-pet-status')];
    expect(new Set(signals.map(signal => signal.innerHTML)).size).toBe(states.length);
    for (const signal of signals) {
      expect(signal).toHaveAttribute('aria-hidden', 'true');
      expect(signal).toHaveAttribute('data-motion-active', 'false');
      expect(getComputedStyle(signal).animation).toBe('none');
    }
    const arc = container.querySelector('.desktop-pet-status__orbit')!;
    expect(getComputedStyle(arc).animation).toBe('none');
    expect(arc).toHaveAttribute('d', 'M 12 3 A 9 9 0 1 1 3 12');
  });

  it('loops only the running arc; attention and error arrive once', () => {
    const { container, rerender } = render(<><style>{petCss}</style><PetStatusSignal state="running" animate /></>);
    expect(getComputedStyle(container.querySelector('.desktop-pet-status__orbit')!).animation).toContain('infinite');
    for (const state of ['attention', 'error', 'paused', 'idle', 'terminal', 'unknown'] as const) {
      rerender(<><style>{petCss}</style><PetStatusSignal state={state} animate /></>);
      const signal = container.querySelector('.desktop-pet-status')!;
      expect(getComputedStyle(signal).animation).not.toContain('infinite');
      expect(signal.querySelector('.desktop-pet-status__orbit')).toBeNull();
    }
  });

  it('honors the real reduced-motion setting without removing the static running shape', () => {
    const { container } = render(<MotionProvider><style>{petCss}</style><Controls /><PetStatusSignal state="running" animate /></MotionProvider>);
    const signal = container.querySelector('.desktop-pet-status')!;
    const arc = signal.querySelector('.desktop-pet-status__orbit')!;
    expect(getComputedStyle(arc).animation).toContain('pet-status-orbit');
    fireEvent.click(screen.getByRole('button', { name: '减少动态效果' }));
    expect(signal).toHaveAttribute('data-state', 'running');
    expect(signal).toHaveAttribute('data-motion-active', 'false');
    expect(getComputedStyle(arc).animation).toBe('none');
    expect(arc.getAttribute('d')).toBeTruthy();
  });

  it('honors OS reduced motion and hidden surfaces', () => {
    vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
    const { container, unmount } = render(<PetStatusSignal state="error" animate />);
    expect(container.querySelector('svg')).toHaveAttribute('data-motion-active', 'false');
    unmount(); vi.unstubAllGlobals();
    const view = render(<PetStatusSignal state="running" animate />);
    act(() => {
      vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(view.container.querySelector('svg')).toHaveAttribute('data-motion-active', 'false');
  });
});
