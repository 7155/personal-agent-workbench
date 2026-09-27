import { useEffect, useId, useRef, type ReactNode } from 'react';
import { ArrowLeft, X } from 'lucide-react';

/** Non-modal: the user must still be able to draw an AOI while editing a plan. */
export function TaskDrawer({ open, title, onClose, onBack, children }: {
  open: boolean; title: string; onClose: () => void; onBack?: () => void; children: ReactNode;
}) {
  const id = useId(), titleRef = useRef<HTMLHeadingElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!open) return;
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    titleRef.current?.focus({ preventScroll: true });
    return () => {
      // Do not steal focus back if the user deliberately moved to the map.
      const active = document.activeElement;
      if (active === document.body || active instanceof Element && active.closest('.earth-drawer')) opener.current?.focus({ preventScroll: true });
    };
  }, [open]);
  return <aside className="earth-drawer earth-task-drawer" hidden={!open} aria-labelledby={id}
    onKeyDown={event => { if (event.key === 'Escape' && !event.defaultPrevented) { event.stopPropagation(); onClose(); } }}>
    <header className="earth-drawer__header">
      <div className="earth-drawer__heading">{onBack ? <button type="button" aria-label="返回分析工具" onClick={onBack}><ArrowLeft size={16} aria-hidden="true" /></button> : null}<h2 id={id} ref={titleRef} tabIndex={-1}>{title}</h2></div>
      <button type="button" aria-label="关闭任务面板" onClick={onClose}><X size={17} aria-hidden="true" /></button>
    </header>
    <div className="earth-task-drawer__body">{children}</div>
  </aside>;
}
