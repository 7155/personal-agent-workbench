import { useMotionActivity } from '@/design/motion';
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from 'react';

export type PawContextMenuItem = {
  id: string;
  label: string;
  icon?: ReactNode;
  /* Real keyboard bindings only, rendered as a dimmed right-aligned hint the
   * way native menus print ⌘H — never a decorative chord for a verb that has
   * no binding. */
  shortcut?: string;
  danger?: boolean;
  disabled?: boolean;
  separatorBefore?: boolean;
  action: () => void;
};

export type PawContextMenuCloseReason = 'action' | 'keyboard' | 'pointer' | 'blur';

/* Menus measure themselves after mount instead of trusting a size estimate:
 * the real box decides the clamp, and near the bottom edge the menu flips to
 * open upward from the anchor like a native menu. The entrance rises from the
 * corner the menu grew out of (--paw-menu-origin / --paw-menu-rise), so a
 * flipped menu reads as unfolding from the pointer, not sliding past it. */
export function PawContextMenu({
  anchor,
  ariaLabel,
  items,
  onClose,
  onHorizontalNavigate,
  x,
  y,
}: {
  anchor?: { readonly current: HTMLElement | null };
  ariaLabel: string;
  items: readonly PawContextMenuItem[];
  onClose: (reason: PawContextMenuCloseReason) => void;
  /* macOS menu-bar behaviour: ArrowLeft/ArrowRight walk to the neighbouring
   * menu while one is open. Only menu-bar menus pass this; context menus
   * keep the arrows for future submenu use. */
  onHorizontalNavigate?: (direction: -1 | 1) => void;
  x: number;
  y: number;
}) {
  const motionActive = useMotionActivity();
  const menuRef = useRef<HTMLDivElement>(null);
  const [placement, setPlacement] = useState<{ left: number; top: number; origin: string; rise: number } | null>(null);

  useLayoutEffect(() => {
    const menu = menuRef.current;
    if (!menu) return;
    const measure = () => {
      // Measure the resting layout box, not the scaled entrance rectangle.
      // CSS bounds the real scroll box; keep a bounded zero-layout fallback
      // for jsdom so a short viewport cannot fit the content estimate.
      const width = Math.min(menu.offsetWidth || 220, Math.max(0, window.innerWidth - 16));
      const height = Math.min(menu.offsetHeight || items.length * 38 + 20, Math.max(0, window.innerHeight - 56));
      const left = Math.max(8, Math.min(x, window.innerWidth - width - 8));
      const fitsBelow = y + height + 8 <= window.innerHeight;
      // Keep the same 48px menu-bar floor and an 8px bottom margin, including
      // after zoom/resize leaves the original anchor outside the viewport.
      const top = Math.max(48, Math.min(fitsBelow ? y : y - height, window.innerHeight - height - 8));
      const next = {
        left,
        top,
        origin: `${x - left > width / 2 ? '100%' : '0'} ${fitsBelow ? '0' : '100%'}`,
        rise: fitsBelow ? 4 : -4,
      };
      setPlacement((current) => current && current.left === next.left && current.top === next.top
        && current.origin === next.origin && current.rise === next.rise ? current : next);
      // A height-only resize can shrink the scroll box without changing its
      // placement or keyboard target. Reveal that same target only if clipped.
      const focused = document.activeElement;
      if (focused instanceof HTMLElement && menu.contains(focused)) {
        const box = menu.getBoundingClientRect();
        const item = focused.getBoundingClientRect();
        if (item.top < box.top || item.bottom > box.bottom || item.left < box.left || item.right > box.right) {
          focused.scrollIntoView({ block: 'nearest', inline: 'nearest' });
        }
      }
    };
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, [items, x, y]);

  const placed = placement !== null;
  useLayoutEffect(() => {
    if (!placed) return;
    const menu = menuRef.current;
    const target = menu?.querySelector<HTMLButtonElement>('button:not(:disabled)');
    if (!menu || !target) return;
    const entry = document.activeElement;
    target.focus({ preventScroll: true });
    if (document.activeElement === target || document.activeElement !== entry) return;
    // Chromium may reject even the measured commit while its native style is
    // still hidden. Give that same entry intent one natural rendering frame,
    // without polling or resetting keyboard selection on later resizes.
    let changedIntent = false;
    const trackIntent = (event: FocusEvent) => {
      if (event.target !== entry) changedIntent = true;
    };
    document.addEventListener('focusin', trackIntent, true);
    let frame: number | null = window.requestAnimationFrame(() => {
      frame = null;
      document.removeEventListener('focusin', trackIntent, true);
      if (changedIntent || document.activeElement !== entry || menuRef.current !== menu
        || !menu.isConnected || !target.isConnected || !menu.contains(target) || target.disabled) return;
      const visible = (element: HTMLElement) => {
        const style = window.getComputedStyle(element);
        return style.visibility !== 'hidden' && style.visibility !== 'collapse' && style.display !== 'none';
      };
      if (visible(menu) && visible(target)) target.focus({ preventScroll: true });
    });
    return () => {
      if (frame !== null) window.cancelAnimationFrame(frame);
      document.removeEventListener('focusin', trackIntent, true);
    };
  }, [placed]);

  useEffect(() => {
    const menu = menuRef.current;
    const dismiss = (event: PointerEvent) => {
      const target = event.target as Node;
      if (menu?.contains(target) || anchor?.current?.contains(target)) return;
      onClose('pointer');
    };
    const blur = () => onClose('blur');
    window.addEventListener('pointerdown', dismiss);
    window.addEventListener('blur', blur);
    return () => {
      window.removeEventListener('pointerdown', dismiss);
      window.removeEventListener('blur', blur);
    };
  }, [anchor, onClose]);

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    const enabled = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'),
    );
    const current = enabled.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === 'Escape' || event.key === 'Tab') {
      event.preventDefault();
      event.stopPropagation();
      onClose('keyboard');
      return;
    }
    if ((event.key === 'ArrowLeft' || event.key === 'ArrowRight') && onHorizontalNavigate) {
      event.preventDefault();
      onHorizontalNavigate(event.key === 'ArrowRight' ? 1 : -1);
      return;
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key) || enabled.length === 0) return;
    event.preventDefault();
    if (event.key === 'Home') enabled[0]?.focus();
    else if (event.key === 'End') enabled.at(-1)?.focus();
    else {
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      enabled[(current + delta + enabled.length) % enabled.length]?.focus();
    }
  }

  // Keep the unmeasured box transparent and pointer-inactive until the clamp
  // lands in the same pre-paint commit. visibility:hidden is inherited by
  // items, and Chromium can retain that hidden child style past placement and
  // reject both keyboard-entry calls even while the menu itself is visible.
  const style: CSSProperties = placement
    ? {
      left: placement.left,
      top: placement.top,
      '--paw-menu-origin': placement.origin,
      '--paw-menu-rise': `${placement.rise}px`,
    } as CSSProperties
    : { left: x, top: y, opacity: 0, pointerEvents: 'none' };

  return (
    <div
      aria-hidden={placed ? undefined : true}
      aria-label={ariaLabel}
      className="paw-context-menu"
      data-motion-active={motionActive}
      onContextMenu={(event) => event.preventDefault()}
      onKeyDown={handleKeyDown}
      ref={menuRef}
      role="menu"
      style={style}
    >
      {items.map((item) => (
        <div className="paw-context-menu__row" data-separator={item.separatorBefore || undefined} key={item.id}>
          <button
            data-danger={item.danger || undefined}
            disabled={item.disabled}
            onClick={() => {
              item.action();
              onClose('action');
            }}
            role="menuitem"
            type="button"
          >
            <span aria-hidden="true">{item.icon}</span>
            <strong>{item.label}</strong>
            {item.shortcut ? <kbd aria-hidden="true">{item.shortcut}</kbd> : null}
          </button>
        </div>
      ))}
    </div>
  );
}
