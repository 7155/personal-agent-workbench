import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Check, Copy, CircleAlert, LoaderCircle } from 'lucide-react';
import { useMotionActivity } from '@/design/motion';

/** Only the affordances the host can actually honour are rendered; a surface
 *  that cannot fork or rewind shows no dead control. */
export function MessageActions({
  copyLabel = '复制',
  onCopy,
  onEdit,
  onFork,
  onRetry,
  onRewind,
  retryLabel = '重试',
  retryIcon,
  retryPending = false,
  text,
}: {
  text?: string;
  copyLabel?: string;
  retryLabel?: string;
  retryIcon?: ReactNode;
  retryPending?: boolean;
  onCopy?: () => void;
  onEdit?: () => void;
  onRetry?: () => void;
  onFork?: () => void;
  onRewind?: () => void;
}) {
  const [feedback, setFeedback] = useState<{ text?: string; state: 'idle' | 'pending' | 'success' | 'error' }>({ state: 'idle' });
  const pending = useRef(false);
  const live = useRef(true);
  const resetTimer = useRef<number | undefined>(undefined);
  const currentText = useRef(text); currentText.current = text;
  const motionActive = useMotionActivity();
  useEffect(() => {
    live.current = true;
    return () => { live.current = false; window.clearTimeout(resetTimer.current); };
  }, []);
  const copyState = feedback.state === 'pending' ? 'pending' : feedback.text === text ? feedback.state : 'idle';
  const copyable = Boolean(text && onCopy !== undefined);
  if (!copyable && !onEdit && !onRetry && !onFork && !onRewind) return null;
  const copy = async () => {
    if (!text || pending.current) return;
    const source = text;
    pending.current = true;
    window.clearTimeout(resetTimer.current);
    setFeedback({ text: source, state: 'pending' });
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(source);
    } catch {
      if (live.current) setFeedback(currentText.current === source
        ? { text: source, state: 'error' }
        : { text: currentText.current, state: 'idle' });
      return;
    } finally { pending.current = false; }
    if (!live.current) return;
    if (currentText.current !== source) { setFeedback({ text: currentText.current, state: 'idle' }); return; }
    setFeedback({ text: source, state: 'success' });
    // Host notification is not the clipboard write and cannot undo it.
    try { onCopy?.(); } catch { /* Keep the truthful successful-copy receipt. */ }
    resetTimer.current = window.setTimeout(() => {
      if (live.current && currentText.current === source) setFeedback({ text: source, state: 'idle' });
    }, 1_200);
  };
  const CopyIcon = copyState === 'success' ? Check : copyState === 'error' ? CircleAlert : copyState === 'pending' ? LoaderCircle : Copy;
  return (
    <div aria-label="消息操作" className="ccui-message-actions" role="toolbar">
      {copyable ? <button className="ccui-copy-action" data-copy-state={copyState} data-motion-active={motionActive}
        aria-busy={copyState === 'pending' || undefined} disabled={copyState === 'pending'} onClick={() => void copy()} type="button">
        <CopyIcon aria-hidden="true" className={copyState === 'pending' ? 'ui-spin' : undefined} size={13} />
        <span>{copyState === 'success' ? '已复制' : copyState === 'pending' ? '正在复制' : copyState === 'error' ? '重试复制' : copyLabel}</span>
      </button> : null}
      {copyState === 'error' ? <span className="ccui-copy-error" role="status">无法复制，请选择文字后手动复制</span> : null}
      {onEdit ? <button onClick={onEdit} type="button">编辑</button> : null}
      {onRetry ? <button aria-label={retryPending ? '正在继续' : retryLabel} disabled={retryPending} onClick={onRetry} type="button">{retryIcon}{retryPending ? '正在继续' : retryLabel}</button> : null}
      {onFork ? <button onClick={onFork} type="button">分叉</button> : null}
      {onRewind ? <button onClick={onRewind} type="button">回到这里</button> : null}
    </div>
  );
}
