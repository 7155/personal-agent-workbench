import { describe, expect, it } from 'vitest';
import { createPetGesture } from './desktop-pet-interaction';

const point = (x: number, y = 0, pointerId = 1) => ({ pointerId, screenX: x, screenY: y });

describe('desktop pet gestures', () => {
  it('allows a stationary double click, including tiny hand movement', () => {
    const gesture = createPetGesture();
    expect(gesture.start(point(0))).toBe(true);
    expect(gesture.move(point(2, 2))).toBe(false);
    expect(gesture.canActivate(100)).toBe(false);
    expect(gesture.end(1, 100)).toBe(true);
    expect(gesture.canActivate(101)).toBe(true);
  });
  it('suppresses a double click after a drag, even after the next pointer-down', () => {
    const gesture = createPetGesture();
    gesture.start(point(0)); expect(gesture.move(point(6))).toBe(true);
    expect(gesture.move(point(0))).toBe(true);
    gesture.end(1, 100);
    gesture.start(point(0)); gesture.end(1, 150);
    expect(gesture.canActivate(151)).toBe(false);
    expect(gesture.canActivate(701)).toBe(true);
  });
  it('cancels capture without launching and admits the next gesture', () => {
    const gesture = createPetGesture(); gesture.start(point(0));
    expect(gesture.end(1, 100, true)).toBe(true);
    expect(gesture.end(1, 110, true)).toBe(false);
    expect(gesture.canActivate(101)).toBe(false);
    expect(gesture.start(point(0))).toBe(true);
  });
  it('ignores an unrelated pointer without ending the active drag', () => {
    const gesture = createPetGesture(); gesture.start(point(0));
    expect(gesture.start(point(0, 0, 2))).toBe(false);
    expect(gesture.move(point(50, 0, 2))).toBe(false);
    expect(gesture.end(2, 100)).toBe(false);
    expect(gesture.move(point(5))).toBe(true);
  });
});
