import { useId, useLayoutEffect, useRef, useState, type ReactNode } from 'react';

/** Keep the panel mounted while a narrow native modal blocks the rest of the
 * window. Wide panels remain non-modal and do not steal Composer focus.
 */
export function OrganizationSurface({ children, onClose }: { children: (close: () => void) => ReactNode; onClose: () => void }) {
  const slot = useRef<HTMLDivElement>(null); const dialog = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLElement | null>(null); const [narrow, setNarrow] = useState(false);
  const titleId = useId(); const openedModal = useRef(false);
  useLayoutEffect(() => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const parent = slot.current?.parentElement;
    if (!parent) return;
    const measure = () => setNarrow(parent.getBoundingClientRect().width < 900);
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(measure);
    observer?.observe(parent); window.addEventListener('resize', measure);
    return () => { observer?.disconnect(); window.removeEventListener('resize', measure); };
  }, []);
  useLayoutEffect(() => {
    const node = dialog.current;
    if (!node) return;
    if (narrow && typeof node.showModal === 'function') {
      if (node.open) node.close();
      node.showModal(); openedModal.current = true;
    } else {
      if (node.open && openedModal.current && typeof node.close === 'function') node.close();
      openedModal.current = false;
      // Setting open on a non-modal dialog does not trigger autofocus.
      node.setAttribute('open', '');
    }
    return () => {
      const wasModal = openedModal.current; const hadPanelFocus = node.contains(document.activeElement);
      openedModal.current = false;
      if (node.open && typeof node.close === 'function') node.close();
      else node.removeAttribute('open');
      if ((wasModal || hadPanelFocus) && opener.current?.isConnected) opener.current.focus({ preventScroll: true });
    };
  }, [narrow]);
  const close = () => {
    // Release native modality before the parent focuses its return button.
    // Cleanup must not subsequently override that deliberate focus target.
    const node = dialog.current;
    if (node?.open && typeof node.close === 'function') node.close();
    else node?.removeAttribute('open');
    openedModal.current = false;
    onClose();
  };
  return <div className="jev-workspace-slot" data-overlay={narrow} ref={slot}>
    <dialog aria-labelledby={titleId} className="jev-workspace-dialog" data-overlay={narrow}
      onCancel={event => { event.preventDefault(); close(); }}
      onKeyDown={event => {
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); return; }
        if (!narrow || event.key !== 'Tab') return;
        const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], summary, [tabindex="0"]',
        )).filter(node => node.tabIndex >= 0 && node.getClientRects().length > 0);
        const first = controls[0]; const last = controls.at(-1);
        if (first && last && ((event.shiftKey && document.activeElement === first)
          || (!event.shiftKey && document.activeElement === last))) {
          event.preventDefault(); (event.shiftKey ? last : first).focus();
        }
      }} ref={dialog}>
      <h2 className="jev-sr-only" id={titleId}>Jev 工作空间</h2>
      {children(close)}
    </dialog>
  </div>;
}
