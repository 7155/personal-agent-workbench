import { arrayRecords, asRecord, numberValue, stringValue, type JsonRecord } from '@/features/overview/management-ui';

export type ScheduleGroup = 'agent' | 'eval' | 'memory';
export type ScheduleRow = { id: string; group: ScheduleGroup; title: string; detail: string; status: string; nextAt: number; cadence: string; error?: string };
export const scheduleStatusLabels: Record<string, string> = { scheduled: '等待执行', running: '执行中', paused: '已暂停', completed: '已完成', failed: '需要处理', cancelled: '已取消' };
export const scheduleGroupLabels = { agent: 'Agent 安排', eval: '周期评测', memory: '后台维护' };
export const memoryScheduleDefinitions = [
  { id: 'automaticOrganization', title: '自动整理记忆', detail: '整理新增来源，保留可追溯的记忆。', frequency: 'runsPerDay', max: 6, unit: '次 / 天' },
  { id: 'dreaming', title: '记忆做梦', detail: '回顾近期工作，整理跨对话的连续性。', frequency: 'runsPerDay', max: 6, unit: '次 / 天' },
  { id: 'catalogConsolidation', title: '记忆目录整理', detail: '定期核对已有记忆目录与重复内容。', frequency: 'cadenceDays', max: 365, unit: '天 / 次' },
] as const;

export function memoryScheduleRows(settingsResponse: unknown, report: unknown): ScheduleRow[] {
  const memory = asRecord(asRecord(asRecord(settingsResponse).settings).memory);
  const status = asRecord(report);
  return memoryScheduleDefinitions.flatMap((definition) => {
    const config = asRecord(memory[definition.id]);
    // No settings means unknown, never a fabricated enabled background job.
    if (typeof config.enabled !== 'boolean') return [];
    const runtime = definition.id === 'catalogConsolidation' ? asRecord(status.catalogConsolidation) : {};
    const enabled = memory.enabled !== false && config.enabled && (definition.id !== 'catalogConsolidation' || asRecord(memory.automaticOrganization).enabled !== false);
    return [{ id: definition.id, group: 'memory', title: definition.title, detail: definition.detail,
      status: !enabled ? 'paused' : stringValue(runtime.lastError) ? 'failed' : runtime.status === 'running' ? 'running' : 'scheduled',
      nextAt: enabled ? numberValue(runtime.nextDueAtMs) : 0,
      cadence: `${numberValue(config[definition.frequency])} ${definition.unit}`, error: stringValue(runtime.lastError),
    }];
  });
}

export function agentScheduleRows(value: unknown): ScheduleRow[] {
  return arrayRecords(asRecord(value).items).filter((item) => asRecord(item.metadata).kind !== 'room_partner_completion').map((item) => ({
    id: stringValue(item.id), group: 'agent', title: stringValue(item.title, '未命名安排'), detail: stringValue(item.instruction),
    status: stringValue(item.status), nextAt: numberValue(item.nextWakeAtMs), cadence: recurrenceText(item), error: stringValue(item.lastError),
  }));
}
export function evalScheduleRows(value: unknown): ScheduleRow[] {
  return arrayRecords(asRecord(value).items).map((item) => ({ id: stringValue(item.id), group: 'eval', title: stringValue(item.suiteId), detail: stringValue(item.suiteRevision), status: stringValue(item.status), nextAt: numberValue(item.nextDueAtMs), cadence: recurrenceText(item), error: stringValue(item.lastErrorCode) }));
}
export function recurrenceText(item: JsonRecord) {
  return item.recurrenceKind === 'once' ? '仅一次' : `每 ${numberValue(item.recurrenceInterval, 1)} ${item.recurrenceKind === 'weekly' ? '周' : '天'} · ${numberValue(item.runCount)} / ${numberValue(item.maxRuns)} 次`;
}
export function scheduleTime(value: number) {
  return new Date(value).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
}
