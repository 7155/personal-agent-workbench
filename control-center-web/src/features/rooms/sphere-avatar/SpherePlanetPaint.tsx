import { useEffect, useMemo, useRef } from 'react';
import { animate, useMotionValue } from 'motion/react';
import type { Identity } from './sphere-avatar-geometry';
import { ellipseSurface, liftSvgOutlines, projectLatitudeBand, projectSurfaceOutline, rotateLongitude, surfaceCoordinate, type SurfaceOutline } from './sphere-surface-projection';

// Original vector landmarks, simplified for an orthographic character globe.
// One connected Americas silhouette keeps the isthmus and tapered south;
// the eastern edge reads as Europe/Africa rather than unrelated color blobs.
const AMERICAS = `M 38 100 C 43 83 51 69 66 66 C 73 61 81 59 90 62
  C 94 58 97 52 106 53 C 111 57 117 54 121 58 C 127 55 135 55 142 61
  C 148 66 148 72 140 76 C 142 80 154 78 152 86 C 146 87 145 96 136 97
  C 132 96 130 99 127 104 C 137 107 140 104 144 108 C 140 114 129 110 125 116
  C 127 122 122 123 119 127 C 118 132 111 133 110 140 C 116 143 117 150 123 154
  C 126 154 128 151 131 153 C 137 157 132 161 137 167 C 139 171 144 169 149 173
  C 152 177 161 176 163 183 C 167 187 174 191 172 198 C 168 204 170 209 164 216
  C 162 224 156 229 154 238 C 147 245 150 254 143 263 C 139 271 138 279 132 283
  C 126 280 123 274 121 264 C 120 255 115 248 114 241 C 110 235 112 226 107 221
  C 104 216 99 211 100 203 C 97 197 101 191 98 185 C 101 176 109 173 112 166
  C 109 162 105 157 102 154 C 95 151 92 146 89 143 C 82 141 78 136 76 130
  C 78 126 82 124 84 125 C 86 122 86 118 91 115 C 90 108 99 105 99 99
  C 94 102 90 99 85 103 C 83 108 77 111 71 113 C 67 111 65 105 65 101
  C 60 99 57 104 52 102 C 48 100 43 105 38 100 Z`;
const EASTERN_LAND = `M 238 64 C 247 62 254 69 260 69 C 265 74 273 74 278 81
  C 278 88 284 92 279 98 C 273 99 269 92 264 94 C 260 99 254 97 252 103
  C 246 105 244 111 237 111 C 231 114 236 119 241 121 C 246 125 254 121 258 127
  C 267 128 273 137 279 143 C 283 150 276 154 279 161 C 282 168 275 172 273 178
  C 266 181 267 189 261 195 C 257 204 250 207 248 217 C 241 213 241 205 237 200
  C 230 197 232 188 226 184 C 223 177 218 171 220 164 C 218 156 224 152 221 146
  C 222 138 230 133 232 127 C 227 124 220 122 223 116 C 227 111 224 108 228 103
  C 233 100 232 95 237 94 C 239 89 235 87 233 82 C 228 78 233 69 238 64 Z`;
const GREENLAND = 'M 169 41 C 177 39 188 45 191 52 C 190 57 187 62 184 67 C 179 70 179 77 173 78 C 168 74 166 67 168 62 C 163 56 166 49 169 41 Z';
const CRATERS = [
  { x: 233, y: 84, rx: 28, ry: 19, angle: 27 },
  { x: 75, y: 222, rx: 18, ry: 26, angle: -23 },
  { x: 250, y: 224, rx: 21, ry: 29, angle: 22 },
  { x: 69, y: 102, rx: 12, ry: 17, angle: 25 },
  { x: 135, y: 63, rx: 8, ry: 4, angle: 0 },
  { x: 245, y: 147, rx: 8, ry: 14, angle: -22 },
  { x: 127, y: 252, rx: 9, ry: 11, angle: 0 },
  { x: 180, y: 268, rx: 7, ry: 8, angle: 15 },
];
const BANDS = [
  { y: 54, width: 17, color: '#f1d7a6', opacity: .40 },
  { y: 80, width: 9, color: '#c79555', opacity: .28 },
  { y: 104, width: 20, color: '#f5d9a4', opacity: .34 },
  { y: 135, width: 8, color: '#bd874b', opacity: .19 },
  { y: 162, width: 19, color: '#f0ce90', opacity: .32 },
  { y: 193, width: 12, color: '#a97e49', opacity: .22 },
  { y: 221, width: 22, color: '#ecd1a0', opacity: .35 },
  { y: 253, width: 9, color: '#ad8752', opacity: .19 },
];

type PaintPath = { key: string; project: (angle: number) => { d: string; visible: number }; fill: string; opacity?: number; stroke?: string; strokeWidth?: number };
const PERIOD_SECONDS = 48;

function surfacePaths(identity: Identity, id: string, detailed: boolean): PaintPath[] {
  const radius = identity === 'Saturn' ? 116 : 126;
  const paths: PaintPath[] = [];
  const add = (key: string, outline: SurfaceOutline, fill: string, opacity = 1, stroke?: string, strokeWidth?: number) => {
    paths.push({ key, project: angle => projectSurfaceOutline(outline, angle, radius), fill, opacity, stroke, strokeWidth });
  };
  if (identity === 'Earth') {
    for (const [name, d] of [['americas', AMERICAS], ['europe-africa', EASTERN_LAND], ['greenland', GREENLAND]]) {
      liftSvgOutlines(d, radius, detailed ? 6 : 2).forEach((outline, n) => add(name + n, outline, `url(#${id}-land)`, 1, '#466f5a', .5));
    }
    // Original rear-hemisphere outlines, not external map/texture assets. They
    // preserve an Earth identity when the original front lands turn out of view.
    for (const [name, degrees] of [
      ['asia', [[140, 64], [167, 70], [207, 61], [225, 46], [214, 30], [231, 13], [212, 4], [194, 19], [185, 3], [174, 17], [165, 23], [160, 40], [145, 45]]],
      ['australia', [[198, -14], [214, -12], [232, -20], [238, -32], [220, -42], [205, -36], [199, -27]]],
    ] as const) add(name, { points: degrees.map(([lon, lat]) => surfaceCoordinate(lon * Math.PI / 180, lat * Math.PI / 180)), closed: true }, `url(#${id}-land)`);
    if (detailed) {
      for (const [n, d] of [
        ['andes', 'M 108 183 C 111 198 112 215 121 236 C 123 247 128 255 130 266'],
        ['clouds', 'M 59 84 Q 84 75 107 78 Q 123 82 139 74 M 228 237 Q 246 234 261 227'],
        ['thin-clouds', 'M 83 60 Q 105 56 124 62 M 45 257 Q 75 264 89 255'],
      ]) liftSvgOutlines(d, radius, detailed ? 6 : 2).forEach((outline, i) => add(n + i, outline, 'none', n === 'andes' ? .23 : .16, n === 'andes' ? '#6e995a' : '#e7f3ee', n === 'andes' ? 4 : 3));
    }
  } else if (identity === 'Mars') {
    const craters = CRATERS.slice(0, detailed ? CRATERS.length : 3);
    for (const [n, crater] of craters.entries()) {
      for (const rear of [false, true]) {
        const rim = ellipseSurface(crater.x, crater.y, crater.rx + 1.8, crater.ry + 1.4, crater.angle, radius, detailed ? 40 : 16);
        const depth = ellipseSurface(crater.x, crater.y, crater.rx, crater.ry, crater.angle, radius, detailed ? 40 : 16);
        for (const [name, outline, paint, alpha] of [['rim', rim, 'crater-rim', .82], ['depth', depth, 'crater-depth', .76]] as const) {
          add(`crater-${n}-${rear ? 'rear-' : ''}${name}`, rear ? { ...outline, points: outline.points.map(p => rotateLongitude(p, Math.PI)) } : outline,
            `url(#${id}-${paint})`, detailed ? alpha : .45);
        }
      }
    }
  } else {
    for (const [n, band] of BANDS.entries()) paths.push({ key: 'band-' + n, fill: band.color, opacity: band.opacity,
      project: angle => ({ d: projectLatitudeBand(band.y, band.width, n * .67, angle, radius, detailed ? 48 : 16), visible: 1 }) });
    // Two restrained storms make longitude visible even within nearly zonal
    // bands. Their tangent contours foreshorten before passing behind the limb.
    for (const [n, x, y, rx, ry] of [[0, 247, 102, 12, 4], [1, 77, 225, 9, 3]])
      add('storm-' + n, ellipseSurface(x, y, rx, ry, 0, radius), '#a88450', detailed ? .17 : .11);
  }
  return paths;
}

/** One existing Motion scheduler drives the longitude value. Only paint paths
 * update; React, the face, fixed light and task lifecycle are not frame owners. */
export function SpherePlanetSurface({ identity, id, size, rotating }: { identity: Identity; id: string; size: number; rotating: boolean }) {
  const root = useRef<SVGGElement>(null), longitude = useMotionValue(0);
  const paths = useMemo(() => surfacePaths(identity, id, size > 48), [identity, id, size > 48]);
  useEffect(() => {
    let frames = 0, lastPaint = -Infinity;
    const nodes = root.current?.querySelectorAll<SVGPathElement>('[data-surface-feature]');
    const paint = (angle: number) => {
      const now = performance.now();
      if (now - lastPaint < 1000 / 30) return;
      lastPaint = now;
      nodes?.forEach((node, n) => {
        const projected = paths[n].project(angle);
        if (node.getAttribute('d') !== projected.d) node.setAttribute('d', projected.d);
        const fraction = projected.visible.toFixed(3);
        if (node.dataset.visibleFraction !== fraction) node.dataset.visibleFraction = fraction;
      });
      if (root.current) { root.current.dataset.longitude = (angle * 180 / Math.PI).toFixed(3); root.current.dataset.surfaceFrames = String(++frames); }
    };
    paint(longitude.get());
    if (!rotating) return;
    // Stop keeps the last phase. Resume continues from it; no done/history
    // signal entry or new task timer is created by a material animation.
    const controls = animate(longitude, longitude.get() + Math.PI * 2, { duration: PERIOD_SECONDS, ease: 'linear', repeat: Infinity, onUpdate: paint });
    return () => controls.stop();
  }, [paths, rotating, longitude]);
  return <g ref={root} className="sphere-surface" data-surface={identity === 'Earth' ? 'continents' : identity === 'Mars' ? 'craters' : 'bands'}
    data-projection="orthographic" data-rotation-active={rotating} data-longitude="0" data-surface-frames="0">
    {paths.map(path => { const initial = path.project(longitude.get()); return <path key={path.key} data-surface-feature={path.key}
      d={initial.d} data-visible-fraction={initial.visible.toFixed(3)} fill={path.fill} opacity={path.opacity}
      stroke={path.stroke} strokeWidth={path.strokeWidth} strokeLinecap="round" strokeLinejoin="round"/>; })}
  </g>;
}

export function SphereMaterialDefs({ id, identity }: { id: string; identity: Identity }) {
  const colors = identity === 'Earth' ? ['#81c3e3', '#2488bb', '#073859']
    : identity === 'Mars' ? ['#f4b18a', '#db815b', '#743b2a'] : ['#fae2ad', '#dcaf70', '#85623b'];
  return <>
    <radialGradient id={`${id}-shell`} cx="27%" cy="19%" r="88%">
      <stop offset="0" stopColor={colors[0]}/><stop offset=".38" stopColor={colors[1]}/><stop offset="1" stopColor={colors[2]}/>
    </radialGradient>
    <linearGradient id={`${id}-land`} gradientUnits="userSpaceOnUse" x1="32" y1="34" x2="280" y2="290">
      <stop stopColor="#c0d899"/><stop offset=".45" stopColor="#99b776"/><stop offset="1" stopColor="#587b4c"/>
    </linearGradient>
    <radialGradient id={`${id}-shine`} cx="30%" cy="25%" r="70%">
      <stop stopColor="#fff0cf" stopOpacity=".22"/><stop offset="1" stopColor="#fff0cf" stopOpacity="0"/>
    </radialGradient>
    <radialGradient id={`${id}-edge`} cx="30%" cy="24%" r="75%">
      <stop offset=".56" stopColor="#071b28" stopOpacity="0"/><stop offset="1" stopColor="#071b28" stopOpacity=".26"/>
    </radialGradient>
    <linearGradient id={`${id}-crater-rim`} x1="15%" y1="0%" x2="80%" y2="100%">
      <stop stopColor="#a55538"/><stop offset=".48" stopColor="#c77550"/><stop offset="1" stopColor="#edac7d"/>
    </linearGradient>
    <radialGradient id={`${id}-crater-depth`} cx="35%" cy="20%" r="85%">
      <stop stopColor="#7f3e2b"/><stop offset=".5" stopColor="#9c5235"/><stop offset="1" stopColor="#c97c51"/>
    </radialGradient>
    <linearGradient id={`${id}-ring`} x1="15%" y1="0%" x2="65%" y2="100%">
      <stop stopColor="#f3debb"/><stop offset=".5" stopColor="#d4b384"/><stop offset="1" stopColor="#a88559"/>
    </linearGradient>
    <linearGradient id={`${id}-ring-edge`} x1="0%" y1="0%" x2="65%" y2="100%">
      <stop stopColor="#ddc6a1"/><stop offset="1" stopColor="#947149"/>
    </linearGradient>
    <filter id={`${id}-eye-depth`} x="-25%" y="-15%" width="160%" height="150%">
      <feDropShadow dx="1" dy="1.6" stdDeviation="1.1" floodColor="#102536" floodOpacity=".28"/>
    </filter>
  </>;
}

/** Rear ellipse is painted before the sphere; front half covers its lower limb. */
export function SaturnRing({ id, front = false, offline = false }: { id: string; front?: boolean; offline?: boolean }) {
  const d = 'M 12 176 A 148 48 0 0 0 308 176';
  const bands = [
    { width: 27, paint: `url(#${id}-ring-edge)`, opacity: 1 },
    { width: 23, paint: `url(#${id}-ring)`, opacity: 1 },
    { width: 5, paint: '#735b3b', opacity: .48 },
    { width: 1.1, paint: '#fff0d1', opacity: .58 },
  ];
  return <g data-ring={front ? 'front' : 'rear'} transform="rotate(-18 160 176)" style={offline ? { filter: 'grayscale(.9)', opacity: .63 } : undefined}>
    {bands.map((band, i) => front ? <path key={i} d={d} fill="none" stroke={band.paint} strokeWidth={band.width} strokeOpacity={band.opacity} strokeLinecap="round"/>
      : <ellipse key={i} cx="160" cy="176" rx="148" ry="48" fill="none" stroke={band.paint} strokeWidth={band.width} strokeOpacity={band.opacity}/>)}
  </g>;
}
