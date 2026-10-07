import {
  useEffect,
  useId,
  useState,
  type DetailsHTMLAttributes,
  type ReactNode,
} from 'react';
import { cn } from './utils';
import { useMotionActivity } from '@/design/motion';

/* Matches --motion-disclose / DISCLOSURE_MOTION (220ms bounded spring) so
   closing content stays mounted through the whole shared exit transition. */
const DEFAULT_EXIT_DURATION_MS = 220;
const INTERACTIVE_SUMMARY_TARGET_SELECTOR = [
  'a',
  'button',
  'input',
  'select',
  'textarea',
  '[contenteditable="true"]',
  '[role="button"]',
  '[role="checkbox"]',
  '[role="combobox"]',
  '[role="link"]',
  '[role="menuitem"]',
  '[role="radio"]',
  '[role="switch"]',
  '[role="tab"]',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

export type DisclosureProps = Omit<DetailsHTMLAttributes<HTMLDetailsElement>, 'children' | 'onToggle' | 'open'> & {
  children: ReactNode;
  contentClassName?: string;
  defaultOpen?: boolean;
  exitDurationMs?: number;
  onOpenChange?: (open: boolean) => void;
  revealClassName?: string;
  summary: ReactNode;
};

export function Disclosure({
  children,
  className,
  contentClassName,
  defaultOpen = false,
  exitDurationMs = DEFAULT_EXIT_DURATION_MS,
  onOpenChange,
  revealClassName,
  summary,
  ...props
}: DisclosureProps) {
  const [open, setOpen] = useState(defaultOpen);
  const [pointerTransition, setPointerTransition] = useState(true);
  const motionActive = useMotionActivity() && pointerTransition;
  const present = useDisclosurePresence(
    open,
    motionActive ? Math.max(0, exitDurationMs) : 0,
  );
  const revealId = useId();
  const toggle = (pointer: boolean) => {
    setPointerTransition(pointer);
    const nextOpen = !open;
    setOpen(nextOpen);
    onOpenChange?.(nextOpen);
  };

  return (
    <details
      {...props}
      className={cn('ui-disclosure', className)}
      data-expanded={open || undefined}
      data-motion-active={motionActive}
      open={open || present}
    >
      <summary
        aria-controls={revealId}
        aria-expanded={open}
        onClick={(event) => {
          if (isInteractiveSummaryTarget(event.target)) {
            // Interactive summary descendants are action-only; never let their
            // click also activate the native details disclosure.
            event.preventDefault();
            return;
          }
          event.preventDefault();
          toggle(event.detail > 0);
        }}
        onKeyDown={(event) => {
          if (isInteractiveSummaryTarget(event.target)) return;
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          toggle(false);
        }}
      >
        {summary}
      </summary>
      <div
        aria-hidden={!open}
        className={cn('ui-disclosure__reveal', revealClassName)}
        data-open={open || undefined}
        id={revealId}
        inert={open ? undefined : true}
      >
        <div className={contentClassName}>{present ? children : null}</div>
      </div>
    </details>
  );
}

function useDisclosurePresence(open: boolean, exitDurationMs: number): boolean {
  const [present, setPresent] = useState(open);

  useEffect(() => {
    if (open) {
      setPresent(true);
      return undefined;
    }
    if (!present) return undefined;
    if (exitDurationMs <= 0) {
      setPresent(false);
      return undefined;
    }
    const timeoutId = window.setTimeout(() => setPresent(false), exitDurationMs);
    return () => window.clearTimeout(timeoutId);
  }, [exitDurationMs, open, present]);

  return present;
}

function isInteractiveSummaryTarget(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return false;
  const owningSummary = target.closest('summary');
  if (!owningSummary) return false;
  const interactiveTarget = target.closest(INTERACTIVE_SUMMARY_TARGET_SELECTOR);
  return Boolean(
    interactiveTarget
    && interactiveTarget !== owningSummary
    && owningSummary.contains(interactiveTarget),
  );
}
