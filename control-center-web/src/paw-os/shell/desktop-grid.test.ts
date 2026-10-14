import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import {
  PAW_DESKTOP_GRID,
  pawDesktopGridPosition,
  pawDesktopMovePosition,
  pawDesktopResolvePersistedPositions,
  pawDesktopSnapPosition,
  pawDesktopShortcutLayout,
  pawDesktopWorkOriginY,
  usePawDesktopGridLayout,
} from './desktop-grid';

const SHORTCUT_HEIGHT = 86;

describe('PAWOS desktop grid', () => {
  it('keeps all eleven desktop Apps above the first work-file row', () => {
    const layout = pawDesktopShortcutLayout(900);

    expect(layout).toMatchObject({ columns: 7, originY: PAW_DESKTOP_GRID.originY });
    expect(pawDesktopGridPosition(10, layout.columns, layout.originY).y).toBe(
      PAW_DESKTOP_GRID.originY + PAW_DESKTOP_GRID.pitchY,
    );
    expect(pawDesktopWorkOriginY(layout.columns, layout.originY)).toBeGreaterThan(
      pawDesktopGridPosition(10, layout.columns, layout.originY).y + SHORTCUT_HEIGHT,
    );
  });

  it('wraps all eleven desktop Apps into a stable compact grid on narrow screens', () => {
    const layout = pawDesktopShortcutLayout(420);

    expect(layout).toMatchObject({ columns: 3, originY: PAW_DESKTOP_GRID.originY });
    expect(pawDesktopGridPosition(10, layout.columns, layout.originY).y).toBe(
      PAW_DESKTOP_GRID.originY + PAW_DESKTOP_GRID.pitchY * 3,
    );
    expect(pawDesktopWorkOriginY(layout.columns, layout.originY)).toBeGreaterThan(
      pawDesktopGridPosition(10, layout.columns, layout.originY).y + SHORTCUT_HEIGHT,
    );
  });

  it('uses one column only when a second full icon would be clipped', () => {
    expect(pawDesktopShortcutLayout(220).columns).toBe(1);
    expect(pawDesktopShortcutLayout(240).columns).toBe(2);
  });

  it('keeps the wide desktop shortcut row in its established top-left position', () => {
    const layout = pawDesktopShortcutLayout(1440);

    expect(layout.originY).toBe(PAW_DESKTOP_GRID.originY);
    expect(pawDesktopGridPosition(0, layout.columns, layout.originY)).toEqual({ x: 24, y: 24 });
  });

  it('snaps a freely dropped icon to the nearest shared desktop cell', () => {
    expect(pawDesktopSnapPosition({ x: 162, y: 124 }, 6)).toEqual({ x: 136, y: 140 });
  });

  it('chooses the nearest empty cell instead of stacking two icons', () => {
    expect(pawDesktopSnapPosition(
      { x: 162, y: 124 },
      6,
      [{ x: 136, y: 140 }],
    )).toEqual({ x: 248, y: 140 });
  });

  it('starts work files on the same vertical grid rhythm as system Apps', () => {
    const layout = pawDesktopShortcutLayout(900);
    expect((pawDesktopWorkOriginY(layout.columns) - PAW_DESKTOP_GRID.originY) % PAW_DESKTOP_GRID.pitchY).toBe(0);
  });

  it('keeps a notice below the shortcuts actually present on a narrow desktop', () => {
    const layout = pawDesktopShortcutLayout(375);
    const visibleShortcutCount = 15;
    const lastShortcut = pawDesktopGridPosition(visibleShortcutCount - 1, layout.columns, layout.originY);
    const noticeY = pawDesktopWorkOriginY(layout.columns, layout.originY, visibleShortcutCount);

    expect(noticeY).toBeGreaterThan(lastShortcut.y + SHORTCUT_HEIGHT);
    expect(noticeY + 54).toBeLessThan(812 - 48 - 70);
    expect(pawDesktopWorkOriginY(layout.columns, layout.originY, 0)).toBe(layout.originY);
  });

  it('repairs persisted off-grid and duplicate coordinates around fixed default icons', () => {
    const persisted = {
      'app:agent': { x: 731, y: 418 },
      'app:memory': { x: 731, y: 418 },
    };
    const resolved = pawDesktopResolvePersistedPositions([
      { id: 'app:browser', position: { x: 24, y: 24 } },
      { id: 'app:agent', position: persisted['app:agent'] },
      { id: 'app:memory', position: persisted['app:memory'] },
    ], persisted, 3);

    expect(resolved['app:agent']).not.toEqual(resolved['app:memory']);
    expect([24, 136, 248]).toContain(resolved['app:agent']?.x);
    expect([24, 136, 248]).toContain(resolved['app:memory']?.x);
    expect((resolved['app:agent']!.y - 24) % 116).toBe(0);
    expect((resolved['app:memory']!.y - 24) % 116).toBe(0);
  });

  it('moves a keyboard-reordered icon by whole cells and skips an occupied cell', () => {
    expect(pawDesktopMovePosition(
      { x: 136, y: 140 },
      'ArrowRight',
      6,
      [{ x: 248, y: 140 }],
    )).toEqual({ x: 360, y: 140 });
  });

  it('does not rerender desktop consumers for resize pixels inside one column breakpoint', () => {
    const originalWidth = window.innerWidth;
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 900 });
    let renders = 0;
    try {
      const probe = renderHook(() => {
        renders += 1;
        return usePawDesktopGridLayout();
      });
      expect(probe.result.current.columns).toBe(7);
      expect(renders).toBe(1);

      act(() => {
        Object.defineProperty(window, 'innerWidth', { configurable: true, value: 880 });
        window.dispatchEvent(new Event('resize'));
      });
      expect(renders).toBe(1);

      act(() => {
        Object.defineProperty(window, 'innerWidth', { configurable: true, value: 760 });
        window.dispatchEvent(new Event('resize'));
      });
      expect(probe.result.current.columns).toBe(6);
      expect(renders).toBe(2);
    } finally {
      Object.defineProperty(window, 'innerWidth', { configurable: true, value: originalWidth });
    }
  });
});
