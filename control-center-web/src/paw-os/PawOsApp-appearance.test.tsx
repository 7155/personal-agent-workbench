import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ThemeProvider } from '@/design/themes';
import { PawOsApp } from './PawOsApp';
import pawOsStyles from './styles/paw-os.css?raw';
import pawOsMotionStyles from './styles/paw-os-motion.css?raw';

vi.mock('./shell/PawDesktop', () => ({ PawDesktop: () => <main>Desktop</main> }));

beforeEach(() => {
  window.localStorage.clear();
  window.location.hash = '#/project-field';
});
afterEach(() => {
  cleanup();
  delete document.documentElement.dataset.theme;
  document.documentElement.style.removeProperty('color-scheme');
});

it.each([null, 'slate', 'ink-paper'])('keeps the current root appearance and dark mode without changing the stored variant %s', preference => {
  if (preference !== null) window.localStorage.setItem('paw-os.appearance.theme', preference);
  window.localStorage.setItem('rag-ime-control-theme', 'dark');

  render(<ThemeProvider><PawOsApp /></ThemeProvider>);

  expect(screen.getByTestId('paw-os-product-root')).toHaveAttribute('data-paw-theme', 'blueprint');
  expect(screen.getByTestId('paw-os-product-root')).toHaveAttribute('data-paw-visual', 'stellar');
  expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
  expect(window.localStorage.getItem('paw-os.appearance.theme')).toBe(preference);
  expect(window.localStorage.getItem('rag-ime-control-theme')).toBe('dark');
});

it('preserves the active visual tokens without reviving retired variant or shell styling', () => {
  const baseTheme = pawOsStyles.match(/\.paw-desktop-root\s*\{(?<body>[\s\S]*?)\n\}/)?.groups?.body;
  expect(baseTheme).toContain('--paw-panel: #ffffff');
  expect(baseTheme).toContain('--paw-accent: #2563eb');
  expect(baseTheme).not.toContain('--paw-ease-out:');
  expect(baseTheme).not.toContain('--paw-ease-in-out:');
  expect(pawOsMotionStyles.match(/--paw-ease-out:/g)).toHaveLength(1);
  expect(pawOsMotionStyles.match(/--paw-ease-in-out:/g)).toHaveLength(1);
  expect(pawOsMotionStyles).toContain('--paw-ease-out: cubic-bezier(.23, 1, .32, 1)');
  for (const theme of ['glacier', 'ink-paper', 'blueprint']) {
    expect(pawOsStyles).not.toContain(`.paw-desktop-root[data-paw-theme='${theme}']`);
  }
  expect(pawOsStyles).not.toContain('--paw-radius: 0px');
  expect(pawOsStyles).not.toContain('.app-shell');
});
