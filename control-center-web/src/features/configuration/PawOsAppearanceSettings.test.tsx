import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MotionProvider } from '@/design/motion';
import { ThemeProvider } from '@/design/themes';
import { pawOsAppRegistry } from '@/features/paw-os/model/app-registry';
import { PawOsAppearanceSettings } from './PawOsAppearanceSettings';

const LEGACY_APPEARANCE_KEY = 'paw-os.appearance.theme';

function renderAppearance() {
  return render(
    <ThemeProvider>
      <MotionProvider>
        <PawOsAppearanceSettings />
      </MotionProvider>
    </ThemeProvider>,
  );
}

describe('PAWOS appearance settings', () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => {
    cleanup();
    delete document.documentElement.dataset.theme;
    document.documentElement.style.removeProperty('color-scheme');
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('offers system, light, and dark choices and reflects the resolved theme', async () => {
    const user = userEvent.setup();
    window.localStorage.setItem('rag-ime-control-theme', 'dark');
    renderAppearance();

    const themes = screen.getByRole('radiogroup', { name: 'PAWOS 主题' });
    expect(within(themes).getAllByRole('radio')).toHaveLength(3);
    expect(within(themes).getByDisplayValue('dark')).toBeChecked();
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    expect(screen.getByText('当前生效：深色')).toBeInTheDocument();

    await user.click(within(themes).getByDisplayValue('light'));

    expect(within(themes).getByDisplayValue('light')).toBeChecked();
    expect(document.documentElement).toHaveAttribute('data-theme', 'light');
    expect(window.localStorage.getItem('rag-ime-control-theme')).toBe('light');

    await user.click(within(themes).getByDisplayValue('system'));
    expect(window.localStorage.getItem('rag-ime-control-theme')).toBe('system');
    expect(screen.getByText('当前生效：浅色（跟随系统设置）')).toBeInTheDocument();
  });

  it('keeps the PAWOS appearance identity storage independent from the global theme', () => {
    window.localStorage.setItem(LEGACY_APPEARANCE_KEY, 'ink-paper');
    renderAppearance();

    const themes = screen.getByRole('radiogroup', { name: 'PAWOS 主题' });
    expect(within(themes).getByDisplayValue('system')).toBeChecked();
    expect(window.localStorage.getItem(LEGACY_APPEARANCE_KEY)).toBe('ink-paper');
    expect(screen.queryByRole('radio', { name: /墨纸工作台|蓝图系统|冰川玻璃/ })).not.toBeInTheDocument();
  });

  it('saves the motion preference locally and reports the effective result', async () => {
    const user = userEvent.setup();
    renderAppearance();

    const motion = screen.getByRole('radiogroup', { name: '界面动效' });
    expect(within(motion).getAllByRole('radio')).toHaveLength(3);
    expect(within(motion).getByRole('radio', { name: /跟随系统/ })).toBeChecked();
    expect(screen.getByText('当前生效：完整动效（跟随系统设置）')).toBeInTheDocument();

    await user.click(within(motion).getByRole('radio', { name: /减少动效/ }));

    expect(within(motion).getByRole('radio', { name: /减少动效/ })).toBeChecked();
    expect(window.localStorage.getItem('rag-ime-control-motion')).toBe('reduce');
    expect(screen.getByText('当前生效：减少动效')).toBeInTheDocument();
  });

  it('keeps the system reduce-motion request effective when full motion is selected', async () => {
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
      matches: query === '(prefers-reduced-motion: reduce)',
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })));
    const user = userEvent.setup();
    renderAppearance();

    const motion = screen.getByRole('radiogroup', { name: '界面动效' });
    await user.click(within(motion).getByRole('radio', { name: /完整动效/ }));

    expect(within(motion).getByRole('radio', { name: /完整动效/ })).toBeChecked();
    expect(screen.getByText('当前生效：减少动效（系统设置优先）')).toBeInTheDocument();
    expect(document.documentElement).toHaveAttribute('data-reduce-motion', 'true');
  });

  it('lists every registered App next to its identity swatch', () => {
    renderAppearance();

    const apps = screen.getByRole('list', { name: 'App 身份色' });
    const items = within(apps).getAllByRole('listitem');
    expect(items).toHaveLength(pawOsAppRegistry.length);
    expect(within(apps).getByText('星伴')).toBeInTheDocument();
    expect(within(apps).getByText('Session 与 Room')).toBeInTheDocument();
    expect(within(apps).getByText('System Settings')).toBeInTheDocument();
    for (const app of pawOsAppRegistry) {
      const item = within(apps).getByText(app.label).closest('li');
      expect(item).toHaveAttribute('data-app', app.id);
      expect(item?.querySelector('i')).toHaveAttribute('aria-hidden', 'true');
    }
  });
});
