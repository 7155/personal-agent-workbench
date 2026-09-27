import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { ChevronDown } from 'lucide-react';

/** A disclosure of ordinary controls, not an ARIA menu with incomplete keyboard semantics. */
export function DisclosureMenu({ label, children, className = '', disabled = false }: {
  label: ReactNode; children: (close: () => void) => ReactNode; className?: string; disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null);
  const id = useId();
  const close = () => { setOpen(false); trigger.current?.focus({ preventScroll: true }); };
  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent | FocusEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', dismiss);
    document.addEventListener('focusin', dismiss);
    return () => { document.removeEventListener('pointerdown', dismiss); document.removeEventListener('focusin', dismiss); };
  }, [open]);
  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  return <div ref={root} className={`earth-disclosure ${className}`} onKeyDown={event => {
    if (event.key === 'Escape' && open && !event.defaultPrevented) {
      event.preventDefault(); event.stopPropagation(); close();
    }
  }}>
    <button ref={trigger} type="button" aria-expanded={open} aria-controls={id} disabled={disabled} onClick={() => setOpen(value => !value)}>
      {label}<ChevronDown size={13} aria-hidden="true" />
    </button>
    <div id={id} className="earth-disclosure__body" hidden={!open}>{children(close)}</div>
  </div>;
}
