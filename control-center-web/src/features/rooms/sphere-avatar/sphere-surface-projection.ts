/** Original SVG landmarks lifted onto a unit sphere, then projected with a
 * fixed orthographic camera. Rotation changes longitude, foreshortening and
 * which hemisphere is visible; it never translates the face or illumination. */
export type SurfacePoint = { x: number; y: number; z: number };
export type SurfaceOutline = { points: SurfacePoint[]; closed: boolean };
const TAU = Math.PI * 2;
const clamp = (n: number, low: number, high: number) => Math.min(high, Math.max(low, n));
const decimal = (n: number) => Math.round(n * 100) / 100;

export function rotateLongitude(p: SurfacePoint, angle: number): SurfacePoint {
  const c = Math.cos(angle), s = Math.sin(angle);
  return { x: p.x * c + p.z * s, y: p.y, z: p.z * c - p.x * s };
}
export function surfaceCoordinate(longitude: number, latitude: number): SurfacePoint {
  return { x: Math.cos(latitude) * Math.sin(longitude), y: -Math.sin(latitude), z: Math.cos(latitude) * Math.cos(longitude) };
}
export function liftSurfacePoint(x: number, y: number, radius: number): SurfacePoint {
  let px = (x - 160) / radius, py = (y - 160) / radius;
  const distance = Math.hypot(px, py);
  if (distance > 1) { px /= distance; py /= distance; }
  return { x: px, y: py, z: Math.sqrt(Math.max(0, 1 - px * px - py * py)) };
}

/** Samples the existing authored absolute M/L/C/Q/Z curves once, not per frame. */
export function liftSvgOutlines(d: string, radius: number, steps = 6): SurfaceOutline[] {
  const tokens = d.match(/[MLCQZ]|-?\d*\.?\d+(?:e[-+]?\d+)?/gi) ?? [];
  const outlines: SurfaceOutline[] = [];
  let i = 0, command = '', x = 0, y = 0, startX = 0, startY = 0;
  let points: { x: number; y: number }[] = [];
  const finish = (closed: boolean) => {
    if (points.length > 1) {
      outlines.push({ points: points.map(p => liftSurfacePoint(p.x, p.y, radius)), closed });
    }
    points = [];
  };
  const num = () => Number(tokens[i++]);
  while (i < tokens.length) {
    if (/^[MLCQZ]$/i.test(tokens[i])) command = tokens[i++].toUpperCase();
    if (command === 'M') {
      finish(false); x = startX = num(); y = startY = num(); points.push({ x, y }); command = 'L';
    } else if (command === 'L') { x = num(); y = num(); points.push({ x, y }); }
    else if (command === 'C' || command === 'Q') {
      const x0 = x, y0 = y, x1 = num(), y1 = num();
      const x2 = num(), y2 = num(), x3 = command === 'C' ? num() : x2, y3 = command === 'C' ? num() : y2;
      for (let n = 1; n <= steps; n++) {
        const t = n / steps, u = 1 - t;
        points.push(command === 'C'
          ? { x: u ** 3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t ** 3 * x3, y: u ** 3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t ** 3 * y3 }
          : { x: u * u * x0 + 2 * u * t * x1 + t * t * x2, y: u * u * y0 + 2 * u * t * y1 + t * t * y2 });
      }
      x = x3; y = y3;
    } else if (command === 'Z') { x = startX; y = startY; finish(true); command = ''; }
    else throw new Error('Unsupported authored sphere surface path');
  }
  finish(false); return outlines;
}

function horizon(a: SurfacePoint, b: SurfacePoint): SurfacePoint {
  const t = a.z / (a.z - b.z), x = a.x + (b.x - a.x) * t, y = a.y + (b.y - a.y) * t, n = Math.hypot(x, y) || 1;
  return { x: x / n, y: y / n, z: 0 };
}
const xy = (p: SurfacePoint, r: number) => `${decimal(160 + r * p.x)} ${decimal(160 + r * p.y)}`;

export function projectSurfaceOutline(outline: SurfaceOutline, angle: number, radius: number): { d: string; visible: number } {
  const c = Math.cos(angle), s = Math.sin(angle);
  const points = outline.points.map(p => ({ x: p.x * c + p.z * s, y: p.y, z: p.z * c - p.x * s }));
  const front = points.filter(p => p.z >= 1e-6).length;
  if (!front) return { d: '', visible: 0 };
  if (front === points.length) return { d: 'M ' + points.map(p => xy(p, radius)).join(' L ') + (outline.closed ? ' Z' : ''), visible: 1 };
  const chains: SurfacePoint[][] = []; let chain: SurfacePoint[] = [];
  if (outline.closed) {
    // Start in the hidden hemisphere so a visible chain cannot wrap the array.
    const start = points.findIndex(p => p.z < 1e-6);
    const ordered = [...points.slice(start), ...points.slice(0, start), points[start]];
    for (let i = 1; i < ordered.length; i++) {
      const a = ordered[i - 1], b = ordered[i], av = a.z >= 1e-6, bv = b.z >= 1e-6;
      if (!av && bv) chain = [horizon(a, b), b];
      else if (av && bv) chain.push(b);
      else if (av && !bv) { chain.push(horizon(a, b)); chains.push(chain); chain = []; }
    }
  } else {
    for (let i = 0; i < points.length; i++) {
      const p = points[i], prev = points[i - 1];
      if (p.z >= 1e-6) { if (prev && prev.z < 1e-6) chain.push(horizon(prev, p)); chain.push(p); }
      else if (prev && prev.z >= 1e-6) { chain.push(horizon(prev, p)); chains.push(chain); chain = []; }
    }
    if (chain.length) chains.push(chain);
  }
  const paths = chains.filter(c => c.length > 1).map(c => {
    const first = c[0], last = c[c.length - 1];
    let d = 'M ' + c.map(p => xy(p, radius)).join(' L ');
    if (outline.closed) {
      const from = Math.atan2(last.y, last.x), to = Math.atan2(first.y, first.x);
      // These authored land/crater footprints cover less than a hemisphere.
      // Their visible piece closes along the short limb, never the complement
      // arc (which would paint the entire globe when a coast leaves view).
      const clockwiseArc = ((to - from) % TAU + TAU) % TAU;
      d += ` A ${radius} ${radius} 0 0 ${clockwiseArc <= Math.PI ? 1 : 0} ${xy(first, radius)} Z`;
    }
    return d;
  });
  return { d: paths.join(' '), visible: front / points.length };
}

export function ellipseSurface(x: number, y: number, rx: number, ry: number, tilt: number, radius: number, segments = 40): SurfaceOutline {
  const a = tilt * Math.PI / 180, points = Array.from({ length: segments }, (_, i) => {
    const t = i / segments * TAU, dx = rx * Math.cos(t), dy = ry * Math.sin(t);
    return liftSurfacePoint(x + dx * Math.cos(a) - dy * Math.sin(a), y + dx * Math.sin(a) + dy * Math.cos(a), radius);
  });
  return { points, closed: true };
}

/** Latitude boundaries have authored longitude variation. They turn over the
 * limb and change projected curvature, unlike invariant solid zonal stripes. */
export function projectLatitudeBand(y: number, width: number, seed: number, angle: number, radius: number, steps = 48): string {
  const latitude = Math.asin(clamp((160 - y) / radius, -.95, .95));
  const half = width / (2 * radius * Math.max(.25, Math.cos(latitude)));
  const boundary = (sign: number) => Array.from({ length: steps + 1 }, (_, i) => {
    const longitude = -Math.PI / 2 + i / steps * Math.PI, world = longitude - angle;
    const lat = clamp(latitude + sign * half + .035 * Math.sin(world * 2 + seed) + .012 * Math.cos(world * 5 - seed), -Math.PI / 2, Math.PI / 2);
    return surfaceCoordinate(longitude, lat);
  });
  const top = boundary(1), bottom = boundary(-1).reverse();
  return 'M ' + top.map(p => xy(p, radius)).join(' L ') + ` A ${radius} ${radius} 0 0 1 ${xy(bottom[0], radius)} L `
    + bottom.map(p => xy(p, radius)).join(' L ') + ` A ${radius} ${radius} 0 0 1 ${xy(top[0], radius)} Z`;
}
