import { Check, CircleAlert, Clock3, LoaderCircle, Square } from 'lucide-react';
import type { ToolStatus } from '../model/types';
import { usePageVisibility } from '@/platform/use-page-visibility';
import './tool-status-mark.css';

const ICONS = { pending: Clock3, running: LoaderCircle, success: Check, error: CircleAlert, cancelled: Square };
/** Render-only: the host's receipt determines status. No timers invent progress. */
export function ToolStatusMark({ status, size = 15, active = true }: { status: ToolStatus; size?: number; active?: boolean }) {
  const Icon = ICONS[status];
  const visible = usePageVisibility();
  return <span className="ccui-execution-mark" data-state={status} data-active={active && visible} aria-hidden="true"><span key={status}><Icon size={size} /></span></span>;
}

export function toolReceiptStatus(status: string, settledByTurnStatus?: string): ToolStatus {
  if (settledByTurnStatus === 'aborted') return 'cancelled';
  if (status === 'running') return 'running';
  if (['completed', 'done', 'success', 'succeeded'].includes(status)) return 'success';
  if (['error', 'failed'].includes(status)) return 'error';
  if (['aborted', 'cancelled', 'canceled'].includes(status)) return 'cancelled';
  return 'pending';
}
