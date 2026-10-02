import { describe, expect, it } from 'vitest';
import pawOsAppSource from '../PawOsApp.tsx?raw';
import controlsCss from './paw-os-controls.css?raw';
import pawOsCss from './paw-os.css?raw';
import sysAppsCss from './paw-os-system-apps.css?raw';

/* The shared control language (select / menu / button / panel) promises that
 * every default control surface is opaque and keeps WCAG AA ink contrast.
 * These tests read the stylesheet as a contract so a future "glass dropdown"
 * or washed-out ink cannot land silently. */

function token(name: string): string {
  const match = controlsCss.match(new RegExp(`--${name}:\\s*([^;]+);`));
  expect(match?.[1], `--${name}`).toBeTruthy();
  return match?.[1]?.trim() ?? '';
}

function hexToRgb(value: string): [number, number, number] {
  const hex = value.replace('#', '');
  const full = hex.length === 3 ? hex.split('').map((c) => c + c).join('') : hex;
  expect(full, `${value} must be a 6-digit opaque hex colour`).toMatch(/^[0-9a-f]{6}$/i);
  return [
    Number.parseInt(full.slice(0, 2), 16),
    Number.parseInt(full.slice(2, 4), 16),
    Number.parseInt(full.slice(4, 6), 16),
  ];
}

function luminance(value: string): number {
  const [r, g, b] = hexToRgb(value).map((channel) => {
    const srgb = channel / 255;
    return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (light + 0.05) / (dark + 0.05);
}

describe('PAWOS shared control language', () => {
  it('matches the literal shared control scale: 32/8/10/12', () => {
    // The one PAWOS control recipe is a fixed scale, not a per-surface guess:
    // 32px control height, 8px control radius, 10px menu radius, 12px panel
    // radius. Any drift here is a silent fork of the shared language.
    expect(token('paw-control-h')).toBe('32px');
    expect(token('paw-control-radius')).toBe('8px');
    expect(token('paw-menu-radius')).toBe('10px');
    expect(token('paw-panel-radius')).toBe('12px');
  });

  it('keeps every default control and menu surface an opaque hex colour', () => {
    for (const name of [
      'paw-control-bg',
      'paw-control-bg-soft',
      'paw-menu-bg',
      'paw-menu-selected-bg',
      'paw-panel-bg',
      'paw-button-primary-bg',
      'paw-button-danger-bg',
    ]) {
      const value = token(name);
      expect(value, `--${name} must be opaque (no color-mix/transparent/alpha)`).toMatch(/^#[0-9a-f]{3}(?:[0-9a-f]{3})?$/i);
    }
  });

  it('keeps WCAG AA contrast on every default ink/surface pair', () => {
    // Resting field and menu text keep AAA-level headroom.
    expect(contrast(token('paw-control-ink'), token('paw-control-bg'))).toBeGreaterThanOrEqual(7);
    expect(contrast(token('paw-menu-ink'), token('paw-menu-bg'))).toBeGreaterThanOrEqual(7);
    // Muted/disabled ink and every saturated variant stay at least AA.
    expect(contrast(token('paw-control-muted'), token('paw-control-bg'))).toBeGreaterThanOrEqual(4.5);
    expect(contrast(token('paw-control-muted'), token('paw-control-bg-soft'))).toBeGreaterThanOrEqual(4.5);
    expect(contrast(token('paw-menu-selected-ink'), token('paw-menu-selected-bg'))).toBeGreaterThanOrEqual(4.5);
    expect(contrast(token('paw-button-primary-ink'), token('paw-button-primary-bg'))).toBeGreaterThanOrEqual(4.5);
    expect(contrast(token('paw-button-danger-ink'), token('paw-button-danger-bg'))).toBeGreaterThanOrEqual(4.5);
  });

  it('gives native selects an opaque field, a chevron, and a light UA popup', () => {
    const selectRule = controlsCss.match(/\.paw-desktop-root :is\(select[^{]*\{(?<body>[^}]*)\}/s)?.groups?.body ?? '';
    expect(selectRule).toContain('appearance: none');
    expect(selectRule).toContain('background-color: var(--paw-control-bg)');
    expect(selectRule).toContain('color: var(--paw-control-ink)');
    expect(selectRule).toContain('color-scheme: light');
    expect(selectRule).toContain("background-image: url(\"data:image/svg+xml");
    expect(selectRule).toContain('border-radius: var(--paw-control-radius)');
    // The dropdown list itself is pinned to ink-on-white, never inherited.
    expect(controlsCss).toMatch(/select option,\s*\n\.paw-desktop-root select optgroup \{\s*\n\s*color: var\(--paw-control-ink\);\s*\n\s*background: var\(--paw-control-bg\);/);
  });

  it('bans glass and gradient treatments from the control layer', () => {
    // backdrop-filter may only appear as an explicit `none` reset.
    for (const match of controlsCss.matchAll(/backdrop-filter:\s*([^;]+);/g)) {
      expect(match[1]?.trim()).toBe('none');
    }
    expect(controlsCss).not.toContain('gradient(');
    expect(controlsCss).not.toContain('text-shadow');
    // Menus highlight with a solid selected pair, not translucency.
    expect(controlsCss).toMatch(/:is\(:hover, :focus-visible\):not\(:disabled\) \{\s*\n\s*color: var\(--paw-menu-selected-ink\);\s*\n\s*background: var\(--paw-menu-selected-bg\);/);
  });

  it('keeps the desktop context menu on the shared opaque menu material', () => {
    const contextRule = pawOsCss.match(/\.paw-context-menu \{(?<body>[^}]*)\}/s)?.groups?.body ?? '';
    // Structure only in paw-os.css: no local background or blur may return.
    expect(contextRule).not.toContain('background');
    expect(contextRule).not.toContain('backdrop-filter');
    expect(pawOsCss).not.toMatch(/\.paw-context-menu[^{]*\{[^}]*backdrop-filter/s);
    expect(controlsCss).toContain('.paw-desktop-root :is(.paw-menu, .paw-context-menu)');
  });

  it('stays wired into the shell after the shell visual owner', () => {
    const shellIndex = pawOsAppSource.indexOf("./styles/paw-os-shell.css");
    const controlsIndex = pawOsAppSource.indexOf("./styles/paw-os-controls.css");
    expect(shellIndex).toBeGreaterThan(-1);
    expect(controlsIndex).toBeGreaterThan(shellIndex);
  });

  it('keeps System App selects on the shared skin instead of a local shorthand', () => {
    // A `background:` shorthand on these selects would erase the shared
    // chevron and re-fork the field skin; layout-only overrides are fine.
    expect(sysAppsCss).not.toMatch(/\.paw-agent-model__field select\s*\{[^}]*background/s);
    // The compact ui-* sizing rule must not reclaim native selects.
    expect(sysAppsCss).toContain(':is(.ui-button, .ui-input, .ui-select__trigger) {');
  });
});
