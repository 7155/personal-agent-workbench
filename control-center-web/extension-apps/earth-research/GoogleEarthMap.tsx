import type { ComponentProps } from 'react';
import { EarthMap as LeafletEarthMap } from './EarthMap';

type Props = ComponentProps<typeof LeafletEarthMap>;

/**
 * Compatibility entry point for the Earth Research app.
 *
 * Keep the complete GEE renderer active even when a Google Maps key is set.
 * The previous Google JS renderer depended on DrawingManager (removed in
 * Maps JS 3.65) and omitted raster layers, result view, and layer/feature
 * commands. Loading the SDK successfully was not a feature-parity check.
 *
 * A future Google JS adapter must cover the full EarthMap contract before
 * replacing this path. The existing renderer still supplies its configured
 * basemap; it does not require loading the Google Maps JavaScript SDK.
 */
export function GoogleEarthMap(props: Props) {
  // Reset renderer-local state when its storage scope changes. Do not carry
  // geometry, draw mode, or map instances from one workspace into another.
  return <LeafletEarthMap key={props.workspaceKey} {...props} />;
}
