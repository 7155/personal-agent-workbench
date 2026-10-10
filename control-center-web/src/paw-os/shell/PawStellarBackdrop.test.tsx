import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PawStellarBackdrop } from './PawStellarBackdrop';
import { projectStellarAgents } from './stellar-agent-projection';

const media = new Map<string, { matches: boolean; listeners: Set<() => void> }>();
function mountScene() {
  const view = render(<div className="paw-desktop-root" data-paw-visual="stellar"><div className="paw-desktop"><PawStellarBackdrop /></div></div>);
  return { ...view,
    scene: view.container.querySelector<HTMLElement>('.paw-stellar-scene')!,
    desktop: view.container.querySelector<HTMLElement>('.paw-desktop')!,
    root: view.container.querySelector<HTMLElement>('.paw-desktop-root')!,
  };
}

beforeEach(() => {
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  vi.stubGlobal('matchMedia', vi.fn((query: string) => {
    const state = { matches: query.includes('pointer: fine'), listeners: new Set<() => void>() };
    media.set(query, state);
    return {
      get matches() { return state.matches; }, media: query,
      addEventListener: (_: string, listener: () => void) => state.listeners.add(listener),
      removeEventListener: (_: string, listener: () => void) => state.listeners.delete(listener),
      addListener: (listener: () => void) => state.listeners.add(listener),
      removeListener: (listener: () => void) => state.listeners.delete(listener),
    };
  }));
});
afterEach(() => {
  cleanup(); media.clear(); vi.unstubAllGlobals();
  delete document.documentElement.dataset.reduceMotion;
  delete (document as { hidden?: boolean }).hidden;
});

describe('stellar wallpaper suspension', () => {
  it('keeps simultaneous real Session identities and removes terminal or stale activity', () => {
    const sources = [
      { id: 'agent:root', title: 'Root', status: 'busy' },
      { id: 'agent:child', title: 'Child', status: 'running', parentSessionId: 'agent:root' },
      { id: 'agent:peer', title: 'Peer', status: 'busy' },
    ];
    const projection = (fresh: boolean) => projectStellarAgents({
      nowMs: 1_000, sessions: sources, rooms: [], sessionStatusFresh: fresh, roomStatusFresh: true,
    });
    const view = render(<PawStellarBackdrop agents={projection(true)} />);
    expect(view.container.querySelectorAll('[data-agent-session]')).toHaveLength(3);
    expect(view.container.querySelectorAll('.paw-stellar-agents__links line')).toHaveLength(1);
    expect(view.container.querySelectorAll('.paw-stellar-scene__stars > i')).toHaveLength(96);
    sources[1]!.status = 'idle';
    view.rerender(<PawStellarBackdrop agents={projection(true)} />);
    expect(view.container.querySelector('[data-agent-session="agent:child"]')).toBeNull();
    expect(view.container.querySelectorAll('[data-agent-session]')).toHaveLength(2);
    view.rerender(<PawStellarBackdrop agents={projection(false)} />);
    expect(view.container.querySelectorAll('[data-agent-session]')).toHaveLength(0);
  });

  it('rests while an App, overview or collaboration owns attention, and resumes afterwards', async () => {
    const { scene, desktop } = mountScene();
    expect(scene.dataset.stellarPaused).toBe('false');
    for (const attribute of ['data-ambient-paused', 'data-overview', 'data-collaboration-focus']) {
      await act(async () => { desktop.setAttribute(attribute, 'true'); });
      expect(scene.dataset.stellarPaused).toBe('true');
      await act(async () => { desktop.removeAttribute(attribute); });
      expect(scene.dataset.stellarPaused).toBe('false');
    }
  });

  it('stops for window gestures and a hidden document', async () => {
    const { scene, root } = mountScene();
    await act(async () => { root.dataset.windowInteraction = 'drag'; });
    expect(scene.dataset.stellarPaused).toBe('true');
    await act(async () => { delete root.dataset.windowInteraction; });
    expect(scene.dataset.stellarPaused).toBe('false');
    act(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, value: true });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(scene.dataset.stellarPaused).toBe('true');
  });

  it('reacts to reduced motion and pointer capability changes without removing the sky', async () => {
    const { scene } = mountScene();
    await act(async () => { document.documentElement.dataset.reduceMotion = 'true'; });
    expect(scene.dataset.stellarPaused).toBe('true');
    await act(async () => { delete document.documentElement.dataset.reduceMotion; });
    const reduced = media.get('(prefers-reduced-motion: reduce)')!;
    act(() => { reduced.matches = true; reduced.listeners.forEach((listener) => listener()); });
    expect(scene.dataset.stellarPaused).toBe('true');
    act(() => { reduced.matches = false; reduced.listeners.forEach((listener) => listener()); });
    expect(scene.dataset.stellarPaused).toBe('false');
    const pointer = media.get('(hover: hover) and (pointer: fine)')!;
    act(() => { pointer.matches = false; pointer.listeners.forEach((listener) => listener()); });
    expect(scene.dataset.stellarPaused).toBe('true');
    expect(scene.querySelectorAll('img')).toHaveLength(2);
  });

  it('stops outside the stellar theme and removes its listeners on unmount', async () => {
    const { scene, root, desktop, unmount } = mountScene();
    await act(async () => { root.dataset.pawVisual = 'classic'; });
    expect(scene.dataset.stellarPaused).toBe('true');
    const remove = vi.spyOn(desktop, 'removeEventListener');
    unmount();
    expect(remove).toHaveBeenCalledWith('pointermove', expect.any(Function));
    expect(remove).toHaveBeenCalledWith('pointerleave', expect.any(Function));
    expect(media.get('(hover: hover) and (pointer: fine)')!.listeners.size).toBe(0);
  });
});


describe('stellar public textures across packaged stylesheet bases', () => {
  it.each([
    'http://127.0.0.1:8768/index.html?controlTransport=http#/agent',
    'https://example.test/paw/index.html#/agent',
    'file:///Applications/PAW%20Test.app/Contents/Resources/web/index.html',
  ])('anchors all six identity textures to the document public root: %s', async (documentUrl) => {
    vi.resetModules();
    vi.stubEnv('BASE_URL', './');
    const { StellarAgentField } = await import('./StellarAgentField');
    const base = vi.spyOn(document, 'baseURI', 'get').mockReturnValue(documentUrl);
    try {
      const projection = projectStellarAgents({ nowMs: 1000, sessions: [{ id: 'agent:public', title: 'Public fixture', status: 'busy' }], rooms: [], sessionStatusFresh: true, roomStatusFresh: true });
      const expectedNames = ['earth', 'mars', 'jupiter', 'neptune', 'venus', 'saturn'];
      for (let palette = 0; palette < expectedNames.length; palette++) {
        projection.runningPlanets[0]!.style.paletteIndex = palette;
        const view = render(<StellarAgentField projection={projection} />);
        const planet = view.container.querySelector<HTMLElement>('[data-agent-session="agent:public"]')!;
        const css = planet.style.getPropertyValue('--stellar-texture');
        const match = /^url\("([^"\n]+)"\)$/.exec(css);
        expect(match).not.toBeNull();
        const asset = `paw-media/starfield/${expectedNames[palette]}-1k.jpg`;
        const expected = new URL(asset, documentUrl).href;
        const stylesheet = new URL('assets/PawOsApp.css', documentUrl).href;
        expect(new URL(match![1]!, stylesheet).href).toBe(expected);
        expect(new URL(match![1]!, documentUrl).href).toBe(expected);
        expect(existsSync(resolve(process.cwd(), 'public', asset))).toBe(true);
        expect(planet).toHaveAttribute('data-running', 'true');
        expect(planet.querySelector('strong')).toHaveTextContent('Public fixture');
        view.unmount();
      }
    } finally { base.mockRestore(); vi.unstubAllEnvs(); }
  });
});
