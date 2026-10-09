import { describe, expect, it } from 'vitest';
import { liftSvgOutlines, projectSurfaceOutline, rotateLongitude, surfaceCoordinate, projectLatitudeBand, type SurfaceOutline } from './sphere-surface-projection';
const patch: SurfaceOutline = { closed: true, points: [[-.2, .2], [.2, .2], [.2, -.2], [-.2, -.2]].map(([lon, lat]) => surfaceCoordinate(lon, lat)) };
describe('spherical surface projection', () => {
  it('changes longitude with foreshortening rather than preserving a translated contour', () => {
    const left = patch.points[0], right = patch.points[1];
    const width = (angle: number) => rotateLongitude(right, angle).x - rotateLongitude(left, angle).x;
    expect(width(Math.PI / 3)).toBeCloseTo(width(0) / 2, 8);
    expect(rotateLongitude(left, Math.PI / 3).y).toBe(left.y);
    expect(Math.hypot(...Object.values(rotateLongitude(left, 1.2)))).toBeCloseTo(1, 8);
  });
  it('occludes the far hemisphere and closes partially visible paint along the spherical limb', () => {
    expect(projectSurfaceOutline(patch, 0, 126).visible).toBe(1);
    expect(projectSurfaceOutline(patch, Math.PI, 126)).toEqual({ d: '', visible: 0 });
    const limb = projectSurfaceOutline(patch, Math.PI / 2, 126);
    expect(limb.visible).toBeGreaterThan(0); expect(limb.visible).toBeLessThan(1);
    expect(limb.d).toContain('A 126 126'); expect(limb.d).not.toContain('NaN');
    expect(projectSurfaceOutline(patch, Math.PI * 2, 126).d).toBe(projectSurfaceOutline(patch, 0, 126).d);
  });
  it('does not paint the complementary globe cap when a regional coast crosses the limb', () => {
    const [coast] = liftSvgOutlines('M 238 64 C 247 62 278 81 279 98 L 248 217 226 184 220 164 238 64 Z', 126);
    const leaving = projectSurfaceOutline(coast, .33, 126);
    expect(leaving.visible).toBeGreaterThan(0); expect(leaving.visible).toBeLessThan(1);
    expect(leaving.d).toContain('A 126 126 0 0');
    expect(leaving.d).not.toMatch(/A 126 126 0 1/u);
  });
  it('lifts authored curved landmarks without requiring DOM or external textures', () => {
    const [outline] = liftSvgOutlines('M 100 100 C 120 80 200 80 220 100 Q 240 180 160 220 L 100 100 Z', 126);
    expect(outline.closed).toBe(true); expect(outline.points.length).toBeGreaterThan(10);
    const drawn = projectSurfaceOutline(outline, .3, 126);
    expect(drawn.d).toMatch(/^M /); expect(drawn.visible).toBeGreaterThan(0);
  });
  it('turns longitude variation in Saturn latitude boundaries instead of sliding the full band', () => {
    const front = projectLatitudeBand(104, 20, .67, 0, 116);
    const turned = projectLatitudeBand(104, 20, .67, .4, 116);
    expect(turned).not.toBe(front); expect(turned).toContain('A 116 116');
    expect(projectLatitudeBand(104, 20, .67, Math.PI * 2, 116)).toBe(front);
  });
});
