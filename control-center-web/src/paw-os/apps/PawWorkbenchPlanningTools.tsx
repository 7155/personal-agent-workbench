import {
  CalendarClock,
  CalendarDays,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  ListTodo,
  Sparkles,
} from 'lucide-react';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import type { PawWorkbenchRecord } from './PawWorkbenchMigrated';

type AgentHandoffIntent = 'organize' | 'breakdown' | 'review';

export function PawWorkbenchPlanningTools({
  date,
  onDateChange,
  onOpenAgent,
  planning,
  projectName,
  projectPath,
  selectedTask,
}: {
  date: string;
  onDateChange: (date: string) => void;
  onOpenAgent: (draft: string) => void;
  planning: PawWorkbenchRecord;
  projectName: string;
  projectPath: string;
  selectedTask?: PawWorkbenchRecord | null;
}) {
  const desktop = usePawOsDesktop();
  const plan = record(planning.plan);
  const summary = record(planning.summary);
  const tasks = rows(planning, ['tasks', 'items']);
  const goals = rows(planning, ['goals']);
  const openTasks = tasks.filter((task) => !['done', 'completed', 'cancelled'].includes(text(task.status).toLowerCase()));
  const firstTask = openTasks[0] ?? tasks[0];
  const taskForBreakdown = selectedTask === undefined ? firstTask : selectedTask;
  const firstGoal = goals.find((goal) => ['active', 'in_progress', 'running'].includes(text(goal.status).toLowerCase())) ?? goals[0];
  const focus = text(plan.intention) || text(firstTask?.title) || '尚未设置当天重点';

  function moveDay(offset: number) {
    const next = new Date(`${date}T12:00:00`);
    if (Number.isNaN(next.getTime())) return;
    next.setDate(next.getDate() + offset);
    onDateChange(localDate(next));
  }

  function handoff(intent: AgentHandoffIntent) {
    const task = intent === 'breakdown' ? taskForBreakdown : firstTask;
    if (intent === 'breakdown' && !task) return;
    const taskTitle = text(task?.title) || focus;
    const taskDetail = text(task?.detail) || text(task?.description);
    const completedCount = finiteNumber(summary.completedTaskCount) ?? 0;
    const prompts: Record<AgentHandoffIntent, string> = {
      organize: `帮我整理 ${date} 的工作。当天重点是“${focus}”，还有 ${openTasks.length} 项待继续，已完成 ${completedCount} 项。请给我清晰的优先级和下一步。`,
      breakdown: `帮我把“${taskTitle}”拆成可以逐项完成的步骤${taskDetail ? `。补充说明：${taskDetail}` : ''}。`,
      review: `陪我复盘 ${date} 的工作：已完成 ${completedCount} 项，还有 ${openTasks.length} 项待继续。已有复盘记录：${text(plan.reflection) || '尚未填写'}。`,
    };
    const goalTitle = text(firstGoal?.title);
    const goalId = text(firstGoal?.id) || text(firstGoal?.goalId);
    const taskId = text(task?.id) || text(task?.taskId);
    const bindings = [
      projectName ? `项目：${projectName}` : '',
      projectPath ? `工作区：${projectPath}` : '',
      goalTitle ? `目标：${goalTitle}${goalId ? `（${goalId}）` : ''}` : '',
      task ? `任务：${taskTitle}${taskId ? `（${taskId}）` : ''}` : '',
      `日期：${date}`,
    ].filter(Boolean);
    onOpenAgent(`${bindings.join('\n')}\n\n${prompts[intent]}`);
  }

  return (
    <>
      <section aria-label="规划工具" className="paw-wb-planning-tools">
        <div className="paw-wb-planning-tools__date">
          <CalendarDays aria-hidden size={15} />
          <button aria-label="前一天" onClick={() => moveDay(-1)} type="button"><ChevronLeft aria-hidden size={15} /></button>
          <input aria-label="规划日期" onChange={(event) => onDateChange(event.target.value)} type="date" value={date} />
          <button aria-label="后一天" onClick={() => moveDay(1)} type="button"><ChevronRight aria-hidden size={15} /></button>
          <button onClick={() => onDateChange(localDate(new Date()))} type="button">今天</button>
        </div>
        <div className="paw-wb-planning-tools__actions">
          <button onClick={() => handoff('organize')} type="button"><Sparkles aria-hidden size={14} />交给 Agent 安排</button>
          <button disabled={!taskForBreakdown} onClick={() => handoff('breakdown')} type="button"><ListTodo aria-hidden size={14} />拆解当前任务</button>
          <button onClick={() => handoff('review')} type="button"><CheckCircle2 aria-hidden size={14} />一起复盘</button>
          <button onClick={() => desktop?.openApp ? desktop.openApp('schedules', '/schedules?view=agent') : openPawOsRoute(desktop, '/schedules?view=agent')} type="button"><CalendarClock aria-hidden size={14} />定时安排</button>
        </div>
      </section>


    </>
  );
}

function rows(envelope: PawWorkbenchRecord, keys: string[]): PawWorkbenchRecord[] {
  for (const key of keys) if (Array.isArray(envelope[key])) return (envelope[key] as unknown[]).map(record);
  return [];
}

function record(value: unknown): PawWorkbenchRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as PawWorkbenchRecord : {};
}

function text(value: unknown): string { return typeof value === 'string' ? value.trim() : ''; }
function finiteNumber(value: unknown): number | null { return typeof value === 'number' && Number.isFinite(value) ? value : null; }
function localDate(value: Date): string {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, '0');
  const day = String(value.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}
