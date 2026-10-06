/** Native durable task identity, never a synthetic user-turn binding. */
export type AgentCompactionTarget = {
  kind: 'compaction';
  runtimeSessionId: string;
  taskIds: string[];
};

export function parseAgentCompactionTarget(value: unknown): AgentCompactionTarget | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const target = value as Record<string, unknown>;
  if (Object.keys(target).length !== 3 || !Object.keys(target).every(key => ['kind', 'runtimeSessionId', 'taskIds'].includes(key))
    || target.kind !== 'compaction' || typeof target.runtimeSessionId !== 'string'
    || !target.runtimeSessionId.trim() || target.runtimeSessionId !== target.runtimeSessionId.trim()
    || !Array.isArray(target.taskIds) || !target.taskIds.length
    || target.taskIds.some((id, index, ids) => typeof id !== 'string'
      || !/^durable:task:[1-9]\d*$/u.test(id) || !Number.isSafeInteger(Number(id.slice(13))) || index > 0 && ids[index - 1] >= id)) return undefined;
  return { kind: 'compaction', runtimeSessionId: target.runtimeSessionId, taskIds: [...target.taskIds] };
}

export function sameAgentCompactionTarget(left: unknown, right: unknown): boolean {
  const a = parseAgentCompactionTarget(left);
  const b = parseAgentCompactionTarget(right);
  return Boolean(a && b && a.runtimeSessionId === b.runtimeSessionId
    && a.taskIds.length === b.taskIds.length && a.taskIds.every((id, index) => id === b.taskIds[index]));
}
