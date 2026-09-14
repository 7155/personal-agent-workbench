import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GoogleEarthMap } from './GoogleEarthMap';

// Bind the key before module evaluation: an API key must not opt the app into
// an adapter that cannot render GEE rasters or handle drawing/Agent commands.
const renderer = vi.hoisted(() => {
  vi.stubEnv('VITE_GOOGLE_MAPS_API_KEY', 'test-only-not-a-real-api-key');
  return { props: [] as Record<string, unknown>[], mounts: 0, unmounts: 0 };
});
vi.mock('./EarthMap', async () => {
  const { useEffect, useState } = await import('react');
  return {
    EarthMap: (props: Record<string, unknown>) => {
      renderer.props.push(props);
      const [count, setCount] = useState(0);
      useEffect(() => {
        renderer.mounts += 1;
        return () => { renderer.unmounts += 1; };
      }, []);
      return <button onClick={() => setCount(value => value + 1)}>
        {String(props.workspaceKey)}:{count}
      </button>;
    },
  };
});

const makeProps = () => ({
  run: null,
  selection: [] as GeoJSON.Feature[],
  workspaceKey: 'workspace-A',
  onSelect: vi.fn(),
  onActivity: vi.fn(),
  command: { requestId: 'focus-1', action: 'focus' as const,
    center: [120, 30] as [number, number], zoom: 8 },
});

beforeEach(() => {
  renderer.props.length = 0;
  renderer.mounts = 0;
  renderer.unmounts = 0;
  localStorage.clear();
});
afterEach(cleanup);
afterAll(() => vi.unstubAllEnvs());

describe('Earth Research renderer compatibility boundary', () => {
  it('passes the complete contract to the existing renderer with a configured key', () => {
    const props = makeProps();
    render(<GoogleEarthMap {...props} />);
    const forwarded = renderer.props.at(-1)!;
    for (const name of Object.keys(props) as (keyof typeof props)[]) {
      expect(forwarded[name]).toBe(props[name]);
    }
    expect(props.onSelect).not.toHaveBeenCalled();
    expect(props.onActivity).not.toHaveBeenCalled();
    expect(document.querySelector('script[src*="maps.googleapis.com/maps/api/js"]')).toBeNull();
  });

  it('does not recreate the map for selection or run updates in the same workspace', () => {
    const props = makeProps();
    const view = render(<GoogleEarthMap {...props} />);
    fireEvent.click(screen.getByRole('button'));
    view.rerender(<GoogleEarthMap {...props} selection={[]} />);
    expect(screen.getByRole('button').textContent).toBe('workspace-A:1');
    expect(renderer.mounts).toBe(1);
    expect(renderer.unmounts).toBe(0);
  });

  it('disposes workspace-local renderer state before rendering another workspace', () => {
    const props = makeProps();
    const view = render(<GoogleEarthMap {...props} />);
    fireEvent.click(screen.getByRole('button'));
    view.rerender(<GoogleEarthMap {...props} workspaceKey="workspace-B" />);
    expect(screen.getByRole('button').textContent).toBe('workspace-B:0');
    expect(renderer.mounts).toBe(2);
    expect(renderer.unmounts).toBe(1);
  });

  it('does not overwrite either workspace storage when its scope changes', () => {
    const a = JSON.stringify([{ id: 'a' }]);
    const b = JSON.stringify([{ id: 'b' }]);
    localStorage.setItem('paw-earth-geometries:workspace-A', a);
    localStorage.setItem('paw-earth-geometries:workspace-B', b);
    const props = makeProps();
    const view = render(<GoogleEarthMap {...props} />);
    view.rerender(<GoogleEarthMap {...props} workspaceKey="workspace-B" />);
    expect(localStorage.getItem('paw-earth-geometries:workspace-A')).toBe(a);
    expect(localStorage.getItem('paw-earth-geometries:workspace-B')).toBe(b);
  });
});
