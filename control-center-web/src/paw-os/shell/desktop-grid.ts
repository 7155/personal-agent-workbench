import { useMemo, useSyncExternalStore } from 'react';
import { pawApps } from '../runtime/app-registry';

/* The desktop's one grid authority. App shortcuts, project folders and loose
 * conversation files all live on the same coordinate plane, so a single slot
 * rhythm — one origin, one pitch, one column count derived from the plane
 * width — is what keeps every identity looking placed by a system instead of
 * scattered. Persisted icon positions always win; these slots are only the
 * default flow for icons the user has never dragged. */
export const PAW_DESKTOP_GRID = {
  originX: 24,
  originY: 24,
  pitchX: 112,
  pitchY: 116,
  iconWidth: 96,
} as const;

/* Every registered App is a desktop identity. The work canvas starts one
 * clear band below however many rows that complete registry occupies. */
export const PAW_DESKTOP_SHORTCUT_COUNT = pawApps.length;

export type PawDesktopShortcutLayout = {
  columns: number;
  originY: number;
};

export function pawDesktopGridColumns(planeWidth: number): number {
  const remaining = planeWidth - PAW_DESKTOP_GRID.originX - PAW_DESKTOP_GRID.iconWidth;
  return Math.max(1, Math.floor(Math.max(0, remaining) / PAW_DESKTOP_GRID.pitchX) + 1);
}

/** The project-dashboard masthead is gone: every desktop width now shares one
 * full-plane grid, and the work-file row begins after every registered App. */
export function pawDesktopShortcutLayout(planeWidth: number): PawDesktopShortcutLayout {
  const safeWidth = Math.max(PAW_DESKTOP_GRID.originX + PAW_DESKTOP_GRID.iconWidth, planeWidth);
  return {
    columns: pawDesktopGridColumns(safeWidth),
    originY: PAW_DESKTOP_GRID.originY,
  };
}

export function pawDesktopGridPosition(
  slot: number,
  columns: number,
  originY: number = PAW_DESKTOP_GRID.originY,
): { x: number; y: number } {
  const safeColumns = Math.max(1, columns);
  return {
    x: PAW_DESKTOP_GRID.originX + (slot % safeColumns) * PAW_DESKTOP_GRID.pitchX,
    y: originY + Math.floor(slot / safeColumns) * PAW_DESKTOP_GRID.pitchY,
  };
}

export type PawDesktopGridPoint = { x: number; y: number };
export type PawDesktopGridEntry = { id: string; position: PawDesktopGridPoint };

function pawDesktopGridCell(position: PawDesktopGridPoint, columns: number): { column: number; row: number } {
  const safeColumns = Math.max(1, columns);
  return {
    column: Math.min(
      safeColumns - 1,
      Math.max(0, Math.round((position.x - PAW_DESKTOP_GRID.originX) / PAW_DESKTOP_GRID.pitchX)),
    ),
    row: Math.max(0, Math.round((position.y - PAW_DESKTOP_GRID.originY) / PAW_DESKTOP_GRID.pitchY)),
  };
}

/** Snap one completed desktop drop to the nearest vacant cell. The search is
 * intentionally pure and runs only at drop time: pointermove remains a native
 * drag, while the final store write is one deterministic, collision-free
 * coordinate. */
export function pawDesktopSnapPosition(
  position: PawDesktopGridPoint,
  columns: number,
  occupiedPositions: readonly PawDesktopGridPoint[] = [],
): PawDesktopGridPoint {
  const safeColumns = Math.max(1, columns);
  const occupied = new Set(occupiedPositions.map((entry) => {
    const cell = pawDesktopGridCell(entry, safeColumns);
    return cell.row * safeColumns + cell.column;
  }));
  const desired = pawDesktopGridCell(position, safeColumns);
  const highestOccupiedRow = occupied.size
    ? Math.max(...Array.from(occupied, (slot) => Math.floor(slot / safeColumns)))
    : 0;
  const lastRow = Math.max(
    desired.row + occupied.size + 2,
    highestOccupiedRow + 2,
    Math.ceil((occupied.size + 1) / safeColumns) + 1,
  );
  let best = pawDesktopGridPosition(desired.row * safeColumns + desired.column, safeColumns);
  let bestDistance = Number.POSITIVE_INFINITY;
  for (let row = 0; row <= lastRow; row += 1) {
    for (let column = 0; column < safeColumns; column += 1) {
      const slot = row * safeColumns + column;
      if (occupied.has(slot)) continue;
      const candidate = pawDesktopGridPosition(slot, safeColumns);
      const distance = (candidate.x - position.x) ** 2 + (candidate.y - position.y) ** 2;
      if (distance >= bestDistance) continue;
      best = candidate;
      bestDistance = distance;
    }
  }
  return best;
}

export function pawDesktopMovePosition(
  position: PawDesktopGridPoint,
  direction: 'ArrowDown' | 'ArrowUp' | 'ArrowLeft' | 'ArrowRight',
  columns: number,
  occupiedPositions: readonly PawDesktopGridPoint[] = [],
): PawDesktopGridPoint {
  const safeColumns = Math.max(1, columns);
  const current = pawDesktopGridCell(position, safeColumns);
  const occupied = new Set(occupiedPositions.map((entry) => {
    const cell = pawDesktopGridCell(entry, safeColumns);
    return cell.row * safeColumns + cell.column;
  }));
  const columnStep = direction === 'ArrowLeft' ? -1 : direction === 'ArrowRight' ? 1 : 0;
  const rowStep = direction === 'ArrowUp' ? -1 : direction === 'ArrowDown' ? 1 : 0;
  const limit = occupied.size + safeColumns + 1;
  for (let distance = 1; distance <= limit; distance += 1) {
    const column = current.column + columnStep * distance;
    const row = current.row + rowStep * distance;
    if (column < 0 || column >= safeColumns || row < 0) break;
    const slot = row * safeColumns + column;
    if (!occupied.has(slot)) return pawDesktopGridPosition(slot, safeColumns);
  }
  return pawDesktopGridPosition(current.row * safeColumns + current.column, safeColumns);
}

/** Normalize only user-persisted coordinates while reserving every implicit
 * default cell first. This preserves a person's rough placement across a
 * layout migration/resize, clamps it to the new column count, and resolves
 * malformed duplicate snapshots without moving untouched system icons. */
export function pawDesktopResolvePersistedPositions(
  entries: readonly PawDesktopGridEntry[],
  persistedPositions: Readonly<Record<string, PawDesktopGridPoint>>,
  columns: number,
): Record<string, PawDesktopGridPoint> {
  const resolved = { ...persistedPositions };
  const occupied = entries
    .filter((entry) => !Object.prototype.hasOwnProperty.call(persistedPositions, entry.id))
    .map((entry) => entry.position);
  for (const entry of entries) {
    if (!Object.prototype.hasOwnProperty.call(persistedPositions, entry.id)) continue;
    /* The DOM can still carry the previous render while sibling layout
     * effects run. Persisted state is the authority for moved icons; using it
     * here makes concurrent parent/child normalization idempotent instead of
     * bouncing between a stale inline coordinate and the latest store value. */
    const position = pawDesktopSnapPosition(persistedPositions[entry.id]!, columns, occupied);
    resolved[entry.id] = position;
    occupied.push(position);
  }
  return resolved;
}

export function pawDesktopGridEntries(root: ParentNode): PawDesktopGridEntry[] {
  return Array.from(root.querySelectorAll<HTMLElement>('[data-wayfinder-grid-position]')).flatMap((element) => {
    const id = element.dataset.wayfinderGridPosition;
    const x = Number.parseFloat(element.style.getPropertyValue('--wayfinder-x'));
    const y = Number.parseFloat(element.style.getPropertyValue('--wayfinder-y'));
    return id && Number.isFinite(x) && Number.isFinite(y) ? [{ id, position: { x, y } }] : [];
  });
}

/** Read only the inline grid coordinates owned by top-level desktop icons.
 * Nested dialogue rows deliberately do not carry this marker. */
export function pawDesktopOccupiedPositions(root: ParentNode, excludingIconId: string): PawDesktopGridPoint[] {
  return pawDesktopGridEntries(root)
    .filter((entry) => entry.id !== excludingIconId)
    .map((entry) => entry.position);
}

/** The y where the work canvas's first default row starts. It is the next row
 * on the same grid, which leaves the pitch's built-in breathing room below the
 * App shortcuts without introducing a second, visibly misaligned origin.
 * Utility notices can supply the count of their visible shortcut projection. */
export function pawDesktopWorkOriginY(
  columns: number,
  shortcutOriginY: number = PAW_DESKTOP_GRID.originY,
  shortcutCount: number = PAW_DESKTOP_SHORTCUT_COUNT,
): number {
  const appRows = Math.ceil(Math.max(0, shortcutCount) / Math.max(1, columns));
  return shortcutOriginY + appRows * PAW_DESKTOP_GRID.pitchY;
}

/** Default slot for a work-canvas icon (project folder or loose conversation
 * file): the same pitch as the App rail, starting below it. */
export function pawDesktopWorkPosition(
  slot: number,
  columns: number,
  shortcutOriginY: number = PAW_DESKTOP_GRID.originY,
): { x: number; y: number } {
  const safeColumns = Math.max(1, columns);
  return {
    x: PAW_DESKTOP_GRID.originX + (slot % safeColumns) * PAW_DESKTOP_GRID.pitchX,
    y: pawDesktopWorkOriginY(safeColumns, shortcutOriginY) + Math.floor(slot / safeColumns) * PAW_DESKTOP_GRID.pitchY,
  };
}

/* Both desktop planes span the full viewport. A shared external-store listener
 * gives App shortcuts and Wayfinder work icons one resize sample and one exact
 * furniture-aware layout instead of registering independent window handlers. */
const viewportListeners = new Set<() => void>();

function notifyViewportListeners(): void {
  for (const listener of viewportListeners) listener();
}

function subscribeViewportGrid(listener: () => void): () => void {
  if (typeof window === 'undefined') return () => undefined;
  if (viewportListeners.size === 0) window.addEventListener('resize', notifyViewportListeners);
  viewportListeners.add(listener);
  return () => {
    viewportListeners.delete(listener);
    if (viewportListeners.size === 0) window.removeEventListener('resize', notifyViewportListeners);
  };
}

function readViewportColumns(): number {
  return pawDesktopGridColumns(typeof window === 'undefined' ? 1280 : window.innerWidth);
}

function readServerViewportColumns(): number {
  return pawDesktopGridColumns(1280);
}

export function usePawDesktopGridLayout(): PawDesktopShortcutLayout {
  /* Consumers care about cells, not every physical pixel. Returning the
   * column count as the external snapshot keeps the desktop inert while a
   * viewport resize stays inside the same breakpoint. */
  const columns = useSyncExternalStore(
    subscribeViewportGrid,
    readViewportColumns,
    readServerViewportColumns,
  );
  return useMemo(() => ({ columns, originY: PAW_DESKTOP_GRID.originY }), [columns]);
}

export function usePawDesktopGridColumns(): number {
  return usePawDesktopGridLayout().columns;
}
