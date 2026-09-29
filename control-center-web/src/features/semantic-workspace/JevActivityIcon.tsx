import { ArrowRight, Check, CircleAlert, Clock3, GitBranch, Layers2, ListChecks, LoaderCircle, Send, Square, Undo2, Workflow } from 'lucide-react';
import type { JevTaskStage } from './jev-execution';
import { usePageVisibility } from '@/platform/use-page-visibility';
import './jev-activity-icon.css';

const icons = { revising: LoaderCircle, superseded: Layers2, queued: Clock3, dispatching: Send, dispatched: ArrowRight, running: Workflow, planning: GitBranch, verifying: ListChecks, synthesizing: Layers2, submitted: ArrowRight, review: ListChecks, returned: Undo2, blocked: Clock3, unknown: CircleAlert, reclaiming: LoaderCircle, reassigning: ArrowRight, done: Check, failed: CircleAlert, cancelled: Square };
const moving = new Set<JevTaskStage>(['planning', 'dispatching', 'running', 'verifying', 'synthesizing', 'revising', 'reclaiming']);

/** Motion describes an observed activity. Pending, stale and terminal states stay still. */
export function JevActivityIcon({ state, active = true, size = 18 }: { state: JevTaskStage; active?: boolean; size?: number }) {
  const Icon = icons[state];
  const visible = usePageVisibility();
  return <span className="jev-activity-icon" data-state={state} data-animated={active && visible && moving.has(state) || undefined} style={{ width: size, height: size }} aria-hidden="true">
    <Icon size={size} strokeWidth={1.7} />
    {moving.has(state) && !['revising', 'reclaiming'].includes(state) ? <svg className="jev-activity-icon__orbit" viewBox="0 0 28 28"><circle cx="14" cy="14" r="12" /></svg> : null}
  </span>;
}
