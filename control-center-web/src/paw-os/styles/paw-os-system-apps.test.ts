import { describe, expect, it } from 'vitest';
import controlsCss from './paw-os-controls.css?raw';
import sysAppsCss from './paw-os-system-apps.css?raw';

/* Input Studio, App Center, System Monitor, and System Settings are one
 * PAWOS instrument, not four branded products. These tests guard the shared
 * cobalt accent and the paw-os-controls radius/height scale so a future
 * per-App palette (the retired #5e5ce6 purple / #0e9f8a teal / #42688a and
 * #5b6f96 blue-greys) cannot silently return. */

function token(css: string, name: string): string {
  const match = css.match(new RegExp(`--${name}:\\s*([^;]+);`));
  expect(match?.[1], `--${name}`).toBeTruthy();
  return match?.[1]?.trim() ?? '';
}

describe('PAWOS system Apps shared accent', () => {
  it('gives every system App the exact same --paw-system-accent (no per-App override block)', () => {
    const declarations = [...sysAppsCss.matchAll(/--paw-system-accent:\s*([^;]+);/g)].map((match) => match[1]?.trim());
    expect(declarations).toEqual(['#1e57e7']);
    // No [data-system-app='…'] selector may reintroduce its own accent.
    expect(sysAppsCss).not.toMatch(/\[data-system-app='[^']+'\]\s*\{[^}]*--paw-system-accent/s);
  });

  it('matches the shared cobalt from paw-os-controls.css instead of an invented hue', () => {
    expect(token(sysAppsCss, 'paw-system-accent')).toBe('#1e57e7');
    expect(token(controlsCss, 'paw-button-primary-bg')).toBe('#1e57e7');
    expect(token(controlsCss, 'paw-menu-selected-bg')).toBe('#1e57e7');
  });

  it('retires every previously competing per-App accent', () => {
    for (const retiredHue of ['#5e5ce6', '#0e9f8a', '#42688a', '#5b6f96']) {
      // The only allowed mention is the explanatory "no longer used" comment.
      const occurrences = sysAppsCss.split(retiredHue).length - 1;
      expect(occurrences, `${retiredHue} should only appear in a retirement comment`).toBeLessThanOrEqual(1);
    }
  });

  it('bridges the shared cobalt into the generic --color-accent-* tokens feature content reads', () => {
    const block = sysAppsCss.match(/\.paw-desktop-root \.paw-system-app \{([\s\S]*?)\n\}/)?.[1] ?? '';
    expect(block).toMatch(/--color-accent:\s*var\(--paw-system-accent\);/);
    expect(block).toMatch(/--color-accent-soft:\s*color-mix\(in srgb, var\(--paw-system-accent\)/);
    expect(block).toMatch(/--color-accent-border:\s*color-mix\(in srgb, var\(--paw-system-accent\)/);
    expect(block).toMatch(/--color-accent-text:\s*#fff;/);
  });

  it('derives its radius scale from paw-os-controls.css instead of ad hoc 9/11/13/14px values', () => {
    const block = sysAppsCss.match(/\.paw-desktop-root \.paw-system-app \{([\s\S]*?)\n\}/)?.[1] ?? '';
    expect(block).toContain('--paw-system-radius-sm: var(--paw-control-radius, 8px);');
    expect(block).toContain('--paw-system-radius-md: var(--paw-menu-radius, 10px);');
    expect(block).toContain('--paw-system-radius-lg: var(--paw-panel-radius, 12px);');
    // Structural surfaces reference the token scale, not a fourth hand-picked number.
    for (const radiusToken of ['--paw-system-radius-sm', '--paw-system-radius-md', '--paw-system-radius-lg']) {
      expect(sysAppsCss).toContain(`border-radius: var(${radiusToken})`);
    }
  });

  it('gives the rail and compact ui-* controls a shared paw-os-controls height instead of an invented 30px', () => {
    expect(sysAppsCss).toMatch(/\.paw-system-app__nav nav button\s*\{[^}]*min-height:\s*var\(--paw-control-h, 32px\);/s);
    expect(sysAppsCss).toMatch(/:is\(\.ui-button, \.ui-input, \.ui-select__trigger\)\s*\{[^}]*min-height:\s*var\(--paw-control-h-sm, 28px\);/s);
  });
});

/* A System App is a window, not a page. These tests hold the shape: the App
 * measures itself, the stage is one fixed chrome band above one scrolling
 * workspace, and every reflow answers the App's own box. */

describe('PAWOS system Apps window shape', () => {
  const appBlock = sysAppsCss.match(/\.paw-desktop-root \.paw-system-app \{([\s\S]*?)\n\}/)?.[1] ?? '';

  it('measures itself with a named size container instead of the viewport', () => {
    expect(appBlock).toContain('container: paw-sysapp / size;');
    expect(appBlock).toMatch(/width:\s*100%;/);
    expect(appBlock).toMatch(/height:\s*100%;/);
    // No viewport unit and no viewport media query may decide App layout.
    expect(sysAppsCss).not.toMatch(/\d+(?:\.\d+)?d?v(?:h|w|min|max)\b/);
    expect(sysAppsCss).not.toMatch(/@media \((?:max|min)-width/);
  });

  it('splits the stage into one fixed chrome band and one scrolling workspace', () => {
    expect(sysAppsCss).toMatch(
      /\.paw-system-app__stage\s*\{[^}]*grid-template-rows:\s*var\(--paw-stage-chrome-h\) minmax\(0, 1fr\);/s,
    );
    expect(sysAppsCss).toMatch(/\.paw-system-app__workspace\s*\{[^}]*overflow:\s*hidden;/s);
    // The page inside the workspace band owns scrolling, and it keeps both
    // axes reachable so nothing wide is silently clipped.
    expect(sysAppsCss).toMatch(
      /\.paw-system-app \.mgmt-page,\s*\n\.paw-desktop-root \.paw-system-app \.context-debug-feature\s*\{[^}]*overflow:\s*auto;[^}]*overscroll-behavior:\s*contain;/s,
    );
    // The retired 36px spacer pseudo-band must not come back.
    expect(sysAppsCss).not.toContain('.mgmt-page:not(:has(.mgmt-page__native-actions))::before');
    // A page's own toolbar is lifted into the App chrome band.
    expect(sysAppsCss).toMatch(
      /\.mgmt-page\[data-paw-os-app\] > \.mgmt-page__native-actions\s*\{[^}]*position:\s*absolute;[^}]*height:\s*var\(--paw-stage-chrome-h\);/s,
    );
  });

  it('collapses the rail to icons from the App container, never from a self-query', () => {
    // The rail width lives on the frame because a container cannot restyle
    // itself; the query has to reach a child to move it.
    expect(sysAppsCss).toMatch(
      /\.paw-system-app__frame\s*\{[^}]*--paw-system-rail-w:\s*176px;[^}]*grid-template-columns:\s*var\(--paw-system-rail-w\) minmax\(0, 1fr\);/s,
    );
    const narrow = sysAppsCss.match(/@container paw-sysapp \(max-width: 560px\) \{([\s\S]*?)\n\}\n\n/)?.[1] ?? '';
    expect(narrow, 'narrow rail container query').toBeTruthy();
    expect(narrow).toMatch(/\.paw-system-app__frame\s*\{\s*--paw-system-rail-w:\s*56px;\s*\}/);
    expect(narrow).toMatch(/\.paw-system-app__nav-label\s*\{\s*display:\s*none;\s*\}/);
    // Label only. A live decision or health count survives the collapse.
    expect(narrow).toMatch(/\.paw-system-app__nav-badge\s*\{[^}]*position:\s*absolute;/s);
    expect(narrow).not.toMatch(/\.paw-system-app__nav-badge\s*\{[^}]*display:\s*none;/s);
    // The colliding purpose line goes before the page name does.
    expect(narrow).toMatch(/\.paw-system-app__page-purpose\s*\{\s*display:\s*none;\s*\}/);
    expect(narrow).not.toContain('.paw-system-app__page-title { display: none;');
  });

  it('reflows every band from the App container, only deferring to the window where a feature owner does', () => {
    for (const query of [
      '@container paw-sysapp (max-width: 880px)',
      '@container paw-sysapp (max-width: 560px)',
      '@container paw-sysapp (max-width: 420px)',
      '@container paw-sysapp (max-height: 420px)',
    ]) expect(sysAppsCss).toContain(query);
    // Approvals stays keyed to paw-window because approvals.css collapses its
    // own two-column desk at that same window width.
    expect(sysAppsCss).toMatch(
      /@container paw-window \(min-width: 901px\)[\s\S]*?\.mgmt-page\[data-route-id='approvals'\] \.approvals-queue__list,[\s\S]*?overflow:\s*hidden auto;/s,
    );
    expect(sysAppsCss.match(/@container paw-window/g) ?? []).toHaveLength(1);
  });

  it('gives each App a purpose-specific pace without inventing a second accent', () => {
    for (const [stage, measure] of [
      ['studio', '1180px'],
      ['gallery', undefined],
      ['instrument', undefined],
      ['sheet', '940px'],
    ] as const) {
      const block = sysAppsCss.match(new RegExp(`\\[data-stage='${stage}'\\] \\{([^}]*)\\}`))?.[1] ?? '';
      expect(block, `[data-stage='${stage}']`).toMatch(/--paw-stage-gutter:/);
      expect(block).toMatch(/--paw-stage-rhythm:/);
      if (measure) expect(block).toContain(`--paw-stage-measure: ${measure};`);
      expect(block, 'stage identity is pacing, never a private hue').not.toMatch(/accent|color:|background/);
    }
  });

  it('carries three intentional motions and retires all of them under reduced motion', () => {
    // 1 rail collapse, 2 page/chrome enter, 3 badge settle.
    expect(sysAppsCss).toMatch(/\.paw-system-app__frame\s*\{[^}]*transition:\s*grid-template-columns 220ms/s);
    expect(sysAppsCss).toContain('@keyframes paw-system-chrome-in');
    expect(sysAppsCss).toContain('@keyframes paw-system-page-in');
    expect(sysAppsCss).toContain('@keyframes paw-system-badge-settle');
    const reduced = sysAppsCss.match(/@media \(prefers-reduced-motion: reduce\) \{([\s\S]*)\n\}/)?.[1] ?? '';
    for (const owner of [
      '.paw-system-app__chrome',
      '.paw-system-app__page',
      '.paw-system-app__nav-badge',
      '.paw-system-app__frame',
    ]) expect(reduced, owner).toContain(owner);
  });
});
