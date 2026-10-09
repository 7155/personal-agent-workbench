import { Check, CircleAlert, Clock3, LoaderCircle, Square } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { usePresentationMotion } from '../reading/reading-preferences';
import { useChatPresentation } from '../reading/chat-presentation';
import type { ToolStatus } from '../model/types';
import { usePageVisibility } from '@/platform/use-page-visibility';
import './tool-status-mark.css';

const ICONS = { pending: Clock3, running: LoaderCircle, success: Check, error: CircleAlert, cancelled: Square };
/** Render-only: the host's receipt determines status. No timers invent progress. */
export function ToolStatusMark({ status, size = 15, active = true }: { status: ToolStatus; size?: number; active?: boolean }) {
  const Icon = ICONS[status];
  const visible = usePageVisibility();
  const motion = usePresentationMotion(active);
  const version = useChatPresentation()?.version;
  const previousStatus = useRef(status);
  const [changed, setChanged] = useState(false);
  useEffect(() => {
    // Cold history and virtualized remounts are receipts, not new outcomes.
    // A hidden/reduced/inactive surface consumes the change without replay.
    const next = previousStatus.current !== status;
    previousStatus.current = status;
    setChanged(next && motion);
  }, [status, motion]);
  return <span className="ccui-execution-mark" data-state={status}
    data-active={version === 'v2' ? motion : active && visible}
    data-feedback={version === 'v2' ? 'current' : undefined}
    data-changed={version === 'v2' && changed && motion || undefined}
    aria-hidden="true"><span key={status}><Icon size={size} /></span></span>;
}

export function toolReceiptStatus(status: string, settledByTurnStatus?: string): ToolStatus {
  if (settledByTurnStatus === 'aborted') return 'cancelled';
  if (status === 'running') return 'running';
  if (['completed', 'done', 'success', 'succeeded'].includes(status)) return 'success';
  if (['error', 'failed'].includes(status)) return 'error';
  if (['aborted', 'cancelled', 'canceled'].includes(status)) return 'cancelled';
  return 'pending';
}
