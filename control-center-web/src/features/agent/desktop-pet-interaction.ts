type Pointer = { pointerId: number; screenX: number; screenY: number };

export function createPetGesture() {
  let pointer: Pointer | null = null;
  let moved = false;
  let suppressUntil = 0;
  return {
    start(event: Pointer) {
      if (pointer) return false;
      pointer = event;
      moved = false;
      return true;
    },
    move(event: Pointer) {
      if (!pointer || event.pointerId !== pointer.pointerId) return false;
      moved ||= Math.hypot(event.screenX - pointer.screenX, event.screenY - pointer.screenY) >= 5;
      return moved;
    },
    end(pointerId: number, now: number, cancelled = false) {
      if (!pointer || pointerId !== pointer.pointerId) return false;
      if (moved || cancelled) suppressUntil = now + 600;
      pointer = null;
      return true;
    },
    canActivate(now: number) { return !pointer && now >= suppressUntil; },
  };
}
