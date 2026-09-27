import type { RoomFocusProjection, RoomFocusWorkItem } from './room-focus-projection';

export type RoomTaskFilter = 'all' | 'attention' | 'active' | 'settled';
export const roomTaskFilterLabels: Record<RoomTaskFilter, string> = {
  all: '全部', attention: '需处理', active: '执行中', settled: '已结束',
};

export interface RoomTaskView {
  tasks: RoomFocusWorkItem[];
  selectedTask?: RoomFocusWorkItem;
  selectedOutsideFilter: boolean;
  selectionUnavailable: boolean;
  counts: Record<RoomTaskFilter, number>;
}

export function roomTaskMatchesFilter(task: RoomFocusWorkItem, filter: RoomTaskFilter): boolean {
  if (filter === 'attention') return ['blocked', 'failed', 'review'].includes(task.state);
  if (filter === 'active') return task.state === 'running';
  if (filter === 'settled') return ['completed', 'stopped'].includes(task.state);
  return true;
}

/** Filtering is a reading choice, not task state or dispatch authority.
 * Keep source order and a manually selected task even when it leaves a filter.
 * An absent selected task is disclosed, never silently replaced by another. */
export function buildRoomTaskView(
  focus: RoomFocusProjection,
  filter: RoomTaskFilter,
  query: string,
  selectedId?: string,
): RoomTaskView {
  const words = query.toLocaleLowerCase().trim().split(/\s+/u).filter(Boolean);
  const counts = { all: focus.workItems.length, attention: 0, active: 0, settled: 0 };
  for (const task of focus.workItems) {
    for (const key of ['attention', 'active', 'settled'] as const) {
      if (roomTaskMatchesFilter(task, key)) counts[key] += 1;
    }
  }
  const tasks = focus.workItems.filter((task) => {
    if (!roomTaskMatchesFilter(task, filter)) return false;
    const people = [task.ownerParticipantId, task.offeredToParticipantId, task.accountableParticipantId, task.verifierParticipantId]
      .flatMap((id) => {
        const person = focus.partners.find((partner) => partner.participantId === id);
        return person ? [person.celestialName, person.displayName] : [];
      });
    const text = [task.objective, task.expectedOutput, task.currentAction,
      task.blocker?.reason, ...task.acceptanceCriteria, ...people].filter(Boolean).join(' ').toLocaleLowerCase();
    return words.every((word) => text.includes(word));
  });
  const priority = ['blocked', 'failed', 'running', 'review', 'waiting', 'idle', 'completed', 'stopped', 'disconnected'];
  const selectedTask = selectedId
    ? focus.workItems.find((task) => task.id === selectedId)
    : [...tasks].sort((a, b) => priority.indexOf(a.state) - priority.indexOf(b.state))[0];
  return {
    tasks, selectedTask, counts,
    selectedOutsideFilter: Boolean(selectedTask && !tasks.some((task) => task.id === selectedTask.id)),
    selectionUnavailable: Boolean(selectedId && !selectedTask),
  };
}
