import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { pawApps } from '../runtime/app-registry';
import { PawAppIcon, PawBrandMark, type PawIdentityIconId } from './PawAppIcon';

afterEach(cleanup);

const approvedAssets: ReadonlyArray<{ appId: PawIdentityIconId; symbol: string; color: string }> = [
  { appId: 'agent', symbol: 'app-agent', color: '#0A84FF' },
  { appId: 'agent-capsule', symbol: 'app-agent-capsule', color: '#7A5AF8' },
  { appId: 'room', symbol: 'app-room', color: '#7A5AF8' },
  { appId: 'browser', symbol: 'app-browser', color: '#14B8C8' },
  { appId: 'terminal', symbol: 'app-terminal', color: '#1D1D1F' },
  { appId: 'files', symbol: 'app-files', color: '#F5A623' },
  { appId: 'project-workbench', symbol: 'app-workbench', color: '#FF6B4A' },
  { appId: 'memory', symbol: 'app-memory', color: '#E85D9E' },
  { appId: 'knowledge', symbol: 'app-knowledge', color: '#1FA54A' },
  { appId: 'input-studio', symbol: 'app-input', color: '#5E5CE6' },
  { appId: 'app-center', symbol: 'app-appcenter', color: '#0E9F8A' },
  { appId: 'system-monitor', symbol: 'app-monitor', color: '#3E4C59' },
  { appId: 'eval-lab', symbol: 'app-eval-lab', color: '#C97718' },
  { appId: 'system-settings', symbol: 'app-settings', color: '#8E8E93' },
];

describe('PAWOS approved App identity icons', () => {
  it('keeps the built-in Apps and the Room collaboration identity on the approved wall', () => {
    const { container } = render(<>{approvedAssets.map(({ appId }) => <PawAppIcon appId={appId} key={appId} />)}</>);
    const icons = [...container.querySelectorAll<SVGElement>('[data-paw-app-icon]')];

    expect(pawApps).toHaveLength(16);
    expect(pawApps.map((app) => app.id)).not.toContain('room');
    expect(icons).toHaveLength(14);
    for (const asset of approvedAssets) {
      const icon = container.querySelector(`[data-paw-app-icon="${asset.appId}"]`);
      expect(icon).toHaveAttribute('data-paw-approved-symbol', asset.symbol);
      expect(icon).toHaveAttribute('data-paw-icon-color', asset.color);
    }
  });

  it('renders a manifest-owned Extension App icon without adding it to the built-in wall', () => {
    const { container } = render(<PawAppIcon appId="extension:zhanggui-wenshu" />);
    const icon = container.querySelector('[data-paw-app-icon="extension:zhanggui-wenshu"]');

    expect(icon).toHaveAttribute('data-paw-approved-symbol', 'app-extension-analytics');
    expect(icon).toHaveAttribute('data-paw-icon-color', '#087F68');
    expect(icon?.querySelectorAll('[data-paw-icon-art] rect')).toHaveLength(3);
  });

  it('reuses the approved 48px colour tile and restrained sheen for every identity', () => {
    const { container } = render(<>{approvedAssets.map(({ appId }) => <PawAppIcon appId={appId} key={appId} />)}</>);

    for (const asset of approvedAssets) {
      const icon = container.querySelector<SVGElement>(`[data-paw-app-icon="${asset.appId}"]`);
      expect(icon).toHaveAttribute('viewBox', '0 0 48 48');
      expect(icon?.querySelector('[data-paw-icon-tile]')).toHaveAttribute('x', '1.5');
      expect(icon?.querySelector('[data-paw-icon-tile]')).toHaveAttribute('y', '1.5');
      expect(icon?.querySelector('[data-paw-icon-tile]')).toHaveAttribute('width', '45');
      expect(icon?.querySelector('[data-paw-icon-tile]')).toHaveAttribute('height', '45');
      expect(icon?.querySelector('[data-paw-icon-tile]')).toHaveAttribute('rx', '11.5');
      expect(icon?.querySelector('[data-paw-icon-tile]')).toHaveAttribute('fill', asset.color);
      expect(icon?.querySelector('[data-paw-icon-sheen]')).toBeInTheDocument();
      expect(icon?.querySelectorAll('stop')).toHaveLength(2);
    }
    expect(container.querySelectorAll('[fill="var(--paw-icon-paper)"], [stroke="var(--paw-icon-paper)"]').length)
      .toBeGreaterThan(0);
    expect(container.querySelector('[fill="#fff"], [stroke="#fff"], stop[stop-color="#fff"]')).toBeNull();
  });

  it('keeps per-instance sheen references unique when many icons share one document', () => {
    const { container } = render(<><PawAppIcon appId="agent" /><PawAppIcon appId="agent" /></>);
    const gradients = [...container.querySelectorAll<SVGLinearGradientElement>('linearGradient')];
    const sheens = [...container.querySelectorAll<SVGRectElement>('[data-paw-icon-sheen]')];

    expect(gradients).toHaveLength(2);
    expect(new Set(gradients.map((gradient) => gradient.id)).size).toBe(2);
    expect(sheens.map((sheen) => sheen.getAttribute('fill'))).toEqual(
      gradients.map((gradient) => `url(#${gradient.id})`),
    );
  });

  it('preserves the approved distinctive geometry instead of substituting generic glyphs', () => {
    const { container } = render(<>
      <PawAppIcon appId="agent" />
      <PawAppIcon appId="room" />
      <PawAppIcon appId="browser" />
      <PawAppIcon appId="terminal" />
      <PawAppIcon appId="memory" />
      <PawAppIcon appId="app-center" />
    </>);

    expect(container.querySelector('[data-paw-approved-symbol="app-agent"] circle[cx="24"][cy="22"][r="5.2"]')).toBeInTheDocument();
    expect(container.querySelectorAll('[data-paw-approved-symbol="app-room"] [data-paw-icon-art] circle')).toHaveLength(3);
    expect(container.querySelector('[data-paw-approved-symbol="app-browser"] circle[stroke="var(--paw-icon-paper)"]')).toBeInTheDocument();
    expect(container.querySelector('[data-paw-approved-symbol="app-terminal"] path[stroke="#30D158"]')).toBeInTheDocument();
    expect(container.querySelectorAll('[data-paw-approved-symbol="app-memory"] [data-paw-icon-art] circle')).toHaveLength(3);
    expect(container.querySelectorAll('[data-paw-approved-symbol="app-appcenter"] [data-paw-icon-art] path')).toHaveLength(2);
    expect(container.querySelector('[data-lucide]')).toBeNull();
  });

  it('is decorative by default and named when a title is supplied', () => {
    const { rerender, getByRole } = render(<PawAppIcon appId="browser" />);
    expect(document.querySelector('[data-paw-app-icon="browser"]')).toHaveAttribute('aria-hidden', 'true');
    rerender(<PawAppIcon appId="browser" title="Browser" />);
    expect(getByRole('img', { name: 'Browser' })).toBeInTheDocument();
  });

  it('ships the approved geometry at every optical size without changing identity', () => {
    const sizes = [16, 24, 32, 48];
    const { container } = render(<>{approvedAssets.flatMap(({ appId, symbol }) => sizes.map((size) => (
      <PawAppIcon appId={appId} key={`${appId}-${size}`} size={size} title={symbol} />
    )))}</>);

    for (const { appId, symbol } of approvedAssets) {
      const icons = [...container.querySelectorAll<SVGElement>(`[data-paw-app-icon="${appId}"]`)];
      expect(icons).toHaveLength(sizes.length);
      for (const [index, icon] of icons.entries()) {
        expect(icon).toHaveAttribute('width', String(sizes[index]));
        expect(icon).toHaveAttribute('height', String(sizes[index]));
        expect(icon).toHaveAttribute('data-paw-approved-symbol', symbol);
        expect(icon.querySelector('[data-paw-icon-tile]')).toBeInTheDocument();
      }
    }
  });

  it('marks compact placement without removing approved detail', () => {
    const { container } = render(<><PawAppIcon appId="knowledge" size={16} /><PawAppIcon appId="knowledge" size={32} /></>);
    const [small, large] = [...container.querySelectorAll<SVGElement>('[data-paw-app-icon="knowledge"]')];

    expect(small).toHaveAttribute('data-paw-icon-scale', 'small');
    expect(large).not.toHaveAttribute('data-paw-icon-scale');
    expect(small.querySelectorAll('[data-paw-icon-art] > *')).toHaveLength(2);
    expect(large.querySelectorAll('[data-paw-icon-art] > *')).toHaveLength(2);
    expect(small).toHaveAttribute('focusable', 'false');
  });

  it('keeps the stellar system mark decorative or explicitly named outside the App colour system', () => {
    const { container, getByRole, rerender } = render(<PawBrandMark />);
    const mark = container.querySelector('svg.paw-brand-mark');
    expect(mark).toHaveAttribute('aria-hidden', 'true');
    expect(mark).toHaveAttribute('fill', 'currentColor');
    expect(mark).toHaveAttribute('viewBox', '0 0 48 48');
    expect(mark).toHaveAttribute('data-paw-brand', 'stellar');
    expect(mark?.hasAttribute('data-paw-app-icon')).toBe(false);
    rerender(<PawBrandMark title="PAW" />);
    expect(getByRole('img', { name: 'PAW' })).toBeInTheDocument();
  });

  it('stays decorative inside a disabled owner while preserving the App identity', () => {
    const { container } = render(<button disabled type="button"><PawAppIcon appId="system-settings" size={18} />Settings</button>);
    expect(container.querySelector('button')).toBeDisabled();
    expect(container.querySelector('[data-paw-app-icon="system-settings"]')).toHaveAttribute('aria-hidden', 'true');
    expect(container.querySelector('[data-paw-app-icon="system-settings"]')).toHaveAttribute('data-paw-icon-scale', 'small');
  });
});
