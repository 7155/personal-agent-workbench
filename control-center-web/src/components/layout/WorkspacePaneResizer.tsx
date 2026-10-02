import { GripVertical } from 'lucide-react';
import './workspace-pane-resizer.css';
import { useEffect, useRef } from 'react';

export interface WorkspacePaneResizerConfig {
  className: string;
  defaultSize: number;
  label: string;
  max: number;
  min: number;
  side: 'rail' | 'status';
  storageKey: string;
  variable: string;
  workspaceSelector: string;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, Math.round(value)));
}

export function WorkspacePaneResizer(config: WorkspacePaneResizerConfig) {
  const handleRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handle = handleRef.current;
    const workspace = handle?.closest<HTMLElement>(config.workspaceSelector);
    if (!handle || !workspace) return;
    const stored = Number(readStoredSize(config.storageKey));
    const initialSize = Number.isFinite(stored) && stored > 0
      ? clamp(stored, config.min, config.max)
      : config.defaultSize;
    applySize(workspace, handle, config.variable, initialSize);
  }, [config.defaultSize, config.max, config.min, config.storageKey, config.variable, config.workspaceSelector]);

  function setSize(size: number, persist = false): void {
    const handle = handleRef.current;
    const workspace = handle?.closest<HTMLElement>(config.workspaceSelector);
    if (!handle || !workspace) return;
    const nextSize = clamp(size, config.min, config.max);
    applySize(workspace, handle, config.variable, nextSize);
    if (persist) storeSize(config.storageKey, nextSize);
  }

  function currentSize(): number {
    return Number(handleRef.current?.getAttribute('aria-valuenow')) || config.defaultSize;
  }

  return (
    <div
      ref={handleRef}
      aria-label={config.label}
      aria-orientation="vertical"
      aria-valuemax={config.max}
      aria-valuemin={config.min}
      aria-valuenow={config.defaultSize}
      aria-valuetext={`${config.defaultSize} 像素`}
      className={`workspace-pane-resizer ${config.className}`}
      data-side={config.side}
      role="separator"
      tabIndex={0}
      onDoubleClick={() => setSize(config.defaultSize, true)}
      onKeyDown={(event) => {
        if (event.key === 'Home' || event.key === 'End') {
          event.preventDefault();
          setSize(event.key === 'Home' ? config.min : config.max, true);
          return;
        }
        const direction = event.key === 'ArrowLeft' ? -1 : event.key === 'ArrowRight' ? 1 : 0;
        if (!direction) return;
        event.preventDefault();
        const geometricDirection = config.side === 'status' ? -direction : direction;
        setSize(currentSize() + geometricDirection * (event.shiftKey ? 32 : 8), true);
      }}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        const handle = event.currentTarget;
        const workspace = handle.closest<HTMLElement>(config.workspaceSelector);
        if (!workspace) return;
        event.preventDefault();
        handle.focus();
        handle.setPointerCapture(event.pointerId);
        workspace.dataset.resizing = config.side;
        const startCoordinate = event.clientX;
        const startSize = currentSize();
        const direction = config.side === 'rail' ? 1 : -1;
        const onPointerMove = (pointerEvent: PointerEvent) => {
          setSize(startSize + direction * (pointerEvent.clientX - startCoordinate));
        };
        const onPointerEnd = () => {
          handle.removeEventListener('pointermove', onPointerMove);
          handle.removeEventListener('pointerup', onPointerEnd);
          handle.removeEventListener('pointercancel', onPointerEnd);
          delete workspace.dataset.resizing;
          storeSize(config.storageKey, currentSize());
        };
        handle.addEventListener('pointermove', onPointerMove);
        handle.addEventListener('pointerup', onPointerEnd);
        handle.addEventListener('pointercancel', onPointerEnd);
      }}
    >
      <GripVertical aria-hidden="true" size={12} />
    </div>
  );
}

function applySize(
  workspace: HTMLElement,
  handle: HTMLElement,
  variable: string,
  size: number,
): void {
  workspace.style.setProperty(variable, `${size}px`);
  handle.setAttribute('aria-valuenow', String(size));
  handle.setAttribute('aria-valuetext', `${size} 像素`);
  if (size <= Number(handle.getAttribute('aria-valuemin'))) handle.dataset.clamped = 'min';
  else if (size >= Number(handle.getAttribute('aria-valuemax'))) handle.dataset.clamped = 'max';
  else delete handle.dataset.clamped;
}

function readStoredSize(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storeSize(key: string, size: number): void {
  try {
    window.localStorage.setItem(key, String(size));
  } catch {
    // Storage can be unavailable in privacy mode; resizing should still work.
  }
}
