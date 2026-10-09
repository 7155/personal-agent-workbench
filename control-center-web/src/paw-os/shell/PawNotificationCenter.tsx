import { Bell, Info, ShieldAlert, TriangleAlert, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useMotionActivity } from '@/design/motion';
import { useGlobalFeedback, type GlobalNotice } from '@/components/feedback';
import './paw-shell-status.css';

const noticeIcons = {
  info: Info,
  warning: TriangleAlert,
  danger: ShieldAlert,
} as const;

export function PawNotificationCenter() {
  const motionActive = useMotionActivity();
  const { clearNotices, dismissNotice, notices } = useGlobalFeedback();
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const noticeButtonRefs = useRef(new Map<string, HTMLButtonElement>());

  useEffect(() => {
    if (!open) return undefined;
    closeButtonRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [open]);

  const visibleNotices = [...notices].reverse();
  const dismissWithFocus = (noticeId: string) => {
    const dismissibleIds = visibleNotices
      .filter((notice) => notice.dismissible !== false)
      .map((notice) => notice.id);
    const currentIndex = dismissibleIds.indexOf(noticeId);
    const fallbackId = dismissibleIds[currentIndex + 1] ?? dismissibleIds[currentIndex - 1];
    dismissNotice(noticeId);
    window.setTimeout(() => {
      const fallback = fallbackId ? noticeButtonRefs.current.get(fallbackId) : null;
      (fallback?.isConnected ? fallback : closeButtonRef.current)?.focus();
    }, 0);
  };

  const clearWithFocus = () => {
    clearNotices();
    window.setTimeout(() => closeButtonRef.current?.focus(), 0);
  };

  const label = notices.length ? `通知中心，${notices.length} 条通知` : '通知中心，无新通知';
  const panel = (
    <aside
      aria-hidden={!open}
      aria-label="通知中心"
      className="paw-notification-center"
      data-motion-active={motionActive}
      data-open={open || undefined}
      id="paw-notification-center-panel"
      inert={!open ? true : undefined}
      role="region"
    >
      <header>
        <span><strong>通知</strong><small>{notices.length ? `${notices.length} 条` : '无新通知'}</small></span>
        <div>
          <button aria-label="清除全部通知" disabled={!notices.length} onClick={clearWithFocus} type="button">清除全部</button>
          <button aria-label="关闭通知中心" onClick={() => { setOpen(false); triggerRef.current?.focus(); }} ref={closeButtonRef} type="button"><X aria-hidden="true" size={16} /></button>
        </div>
      </header>
      <div className="paw-notification-center__body">
        {notices.length ? (
          <section aria-label="今天的通知">
            <h2>今天</h2>
            <div>
              {visibleNotices.map((notice) => (
                <NoticeCard
                  buttonRef={(node) => {
                    if (node) noticeButtonRefs.current.set(notice.id, node);
                    else noticeButtonRefs.current.delete(notice.id);
                  }}
                  key={notice.id}
                  notice={notice}
                  onDismiss={() => dismissWithFocus(notice.id)}
                />
              ))}
            </div>
          </section>
        ) : (
          <div className="paw-notification-center__empty">
            <Bell aria-hidden="true" size={22} />
            <strong>还没有通知</strong>
            <span>任务结果、需要处理的状态和系统提醒会出现在这里。</span>
          </div>
        )}
      </div>
    </aside>
  );
  return (
    <>
      <button
        aria-expanded={open}
        aria-controls="paw-notification-center-panel"
        aria-label={label}
        className="paw-notification-center__trigger"
        onClick={() => setOpen((value) => !value)}
        ref={triggerRef}
        type="button"
      >
        <Bell aria-hidden="true" size={14} />
        {notices.length ? <b>{notices.length}</b> : null}
      </button>
      {typeof document === 'undefined' ? panel : createPortal(panel, document.body)}
    </>
  );
}

function NoticeCard({ buttonRef, notice, onDismiss }: {
  buttonRef: (node: HTMLButtonElement | null) => void;
  notice: GlobalNotice;
  onDismiss: () => void;
}) {
  const Icon = noticeIcons[notice.tone];
  return (
    <article className="paw-notification-center__notice" data-tone={notice.tone}>
      <Icon aria-hidden="true" size={16} />
      <span>
        <strong>{notice.title}</strong>
        {notice.message ? <span>{notice.message}</span> : null}
        <time>{noticeTime(notice.createdAtMs)}</time>
      </span>
      {notice.dismissible !== false ? (
        <button aria-label={`清除通知：${notice.title}`} onClick={onDismiss} ref={buttonRef} type="button"><X aria-hidden="true" size={13} /></button>
      ) : null}
    </article>
  );
}

function noticeTime(createdAtMs?: number): string {
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(createdAtMs ?? Date.now()));
}
