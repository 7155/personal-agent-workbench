import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type AriaRole,
  type FocusEvent,
  type ReactNode,
  type TransitionEvent,
} from 'react';

/* One disclosure voice across the conversation: the bounded spring from the
   conversation baseline (--spring), shared by every reveal
   (.agent-smooth-reveal, .agent-turn-work__reveal, .paw-activity__detail).
   The duration also bounds the fallback finish timer. */
export const DISCLOSURE_MOTION = {
  durationMs: 220,
  easing: 'cubic-bezier(0.34, 1.4, 0.64, 1)',
} as const;

type DisclosurePhase = 'closed' | 'closing' | 'opening' | 'open';

/**
 * A height-measured disclosure for evidence that may be several pages tall.
 *
 * The shell always remains in the DOM so `aria-controls` never points at a
 * missing target. Children mount before an opening measure and remain mounted
 * until the closing height transition finishes. Reading the current rendered
 * height on every command makes an in-flight close immediately reversible.
 *
 * `keepMounted` keeps the children in the DOM even while closed, for hosts
 * whose collapsed records must remain readable in document order (e.g. the
 * Room chronology). Presence reporting and motion behave identically.
 */
export function SmoothDisclosureReveal({
  ariaLabel,
  children,
  className,
  id,
  innerClassName,
  keepMounted = false,
  onPresenceChange,
  open,
  role,
}: {
  ariaLabel?: string;
  children: ReactNode;
  className?: string;
  id: string;
  innerClassName?: string;
  keepMounted?: boolean;
  onPresenceChange?: (present: boolean) => void;
  open: boolean;
  role?: AriaRole;
}) {
  const [present, setPresent] = useState(open);
  const [height, setHeightState] = useState(open ? 'auto' : '0px');
  const [phase, setPhaseState] = useState<DisclosurePhase>(open ? 'open' : 'closed');
  const rootRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef(0);
  const fallbackTimerRef = useRef(0);
  const heightRef = useRef(height);
  const openRef = useRef(open);
  const phaseRef = useRef<DisclosurePhase>(phase);
  openRef.current = open;

  const setHeight = useCallback((value: string) => {
    heightRef.current = value;
    setHeightState(value);
  }, []);
  const setPhase = useCallback((value: DisclosurePhase) => {
    phaseRef.current = value;
    setPhaseState(value);
  }, []);
  const clearPendingMotion = useCallback(() => {
    window.cancelAnimationFrame(frameRef.current);
    window.clearTimeout(fallbackTimerRef.current);
    frameRef.current = 0;
    fallbackTimerRef.current = 0;
  }, []);
  const finish = useCallback((expanded: boolean) => {
    clearPendingMotion();
    if (expanded) {
      if (!openRef.current) return;
      setHeight('auto');
      setPhase('open');
      return;
    }
    if (openRef.current) return;
    setHeight('0px');
    setPhase('closed');
    setPresent(false);
  }, [clearPendingMotion, setHeight, setPhase]);

  useLayoutEffect(() => {
    clearPendingMotion();
    if (open && !present) {
      setPresent(true);
      return undefined;
    }
    if (!open && !present) {
      setHeight('0px');
      setPhase('closed');
      return undefined;
    }

    const root = rootRef.current;
    const inner = innerRef.current;
    if (!root || !inner) return undefined;
    const reduceMotion = disclosureMotionReduced();
    if (reduceMotion) {
      if (open) {
        setHeight('auto');
        setPhase('open');
      } else {
        setHeight('0px');
        setPhase('closed');
        setPresent(false);
      }
      return undefined;
    }

    if (open && phaseRef.current === 'open' && heightRef.current === 'auto') {
      return undefined;
    }

    const currentHeight = Math.max(0, root.getBoundingClientRect().height);
    const targetHeight = open
      ? Math.max(inner.scrollHeight, inner.getBoundingClientRect().height)
      : 0;
    setHeight(`${currentHeight}px`);
    setPhase(open ? 'opening' : 'closing');

    frameRef.current = window.requestAnimationFrame(() => {
      frameRef.current = 0;
      if (openRef.current !== open) return;
      setHeight(`${targetHeight}px`);
      // A zero-height measurement can mean that layout is temporarily
      // unavailable (for example while a parent is settling), not that a
      // requested close has visibly finished. Keep closing children present
      // until transition-end or the bounded fallback so evidence never
      // disappears in the same interaction that collapses it.
      if (open && Math.abs(currentHeight - targetHeight) < 0.5) finish(true);
    });
    fallbackTimerRef.current = window.setTimeout(
      () => finish(open),
      DISCLOSURE_MOTION.durationMs + 80,
    );
    return clearPendingMotion;
  }, [clearPendingMotion, finish, open, present, setHeight, setPhase]);

  useEffect(() => {
    if (!open || !present || typeof ResizeObserver === 'undefined') return undefined;
    const inner = innerRef.current;
    if (!inner) return undefined;
    const observer = new ResizeObserver(() => {
      if (phaseRef.current !== 'opening') return;
      const nextHeight = Math.max(inner.scrollHeight, inner.getBoundingClientRect().height);
      if (nextHeight > 0) setHeight(`${nextHeight}px`);
    });
    observer.observe(inner);
    return () => observer.disconnect();
  }, [open, present, setHeight]);

  useEffect(() => clearPendingMotion, [clearPendingMotion]);

  useEffect(() => {
    onPresenceChange?.(present);
  }, [onPresenceChange, present]);

  const handleTransitionEnd = (event: TransitionEvent<HTMLDivElement>) => {
    if (event.currentTarget !== event.target || event.propertyName !== 'height') return;
    finish(openRef.current);
  };

  const handleFocusCapture = (event: FocusEvent<HTMLDivElement>) => {
    if (!openRef.current || phaseRef.current !== 'opening') return;
    // Expose the complete scroll range before native focus scrolls this action
    // into view. The queued height frame must not clip or move it afterwards.
    // Write our owned height synchronously, then settle the same React state.
    event.currentTarget.style.height = 'auto';
    finish(true);
  };

  return (
    <div
      aria-hidden={!open}
      aria-label={ariaLabel}
      className={['agent-smooth-reveal', className].filter(Boolean).join(' ')}
      data-open={open || undefined}
      data-state={phase}
      id={id}
      inert={open ? undefined : true}
      onFocusCapture={handleFocusCapture}
      onTransitionEnd={handleTransitionEnd}
      ref={rootRef}
      role={role}
      style={{ height }}
    >
      {present || keepMounted ? (
        <div
          className={['agent-smooth-reveal__inner', innerClassName].filter(Boolean).join(' ')}
          ref={innerRef}
        >
          {children}
        </div>
      ) : null}
    </div>
  );
}

function disclosureMotionReduced(): boolean {
  if (typeof document !== 'undefined' && document.documentElement.dataset.reduceMotion === 'true') {
    return true;
  }
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}
