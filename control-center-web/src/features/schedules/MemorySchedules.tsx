import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button, Field, Input, Switch } from '@/components/primitives';
import { configurationQueryKeys } from '@/features/configuration/api';
import { memoryQueryKeys } from '@/features/memory/api';
import { parseManagementWorkPreview, parseManagementWorkReceipt } from '@/features/overview/management-mutation';
import { InlineNotice, arrayRecords, asRecord, numberValue, publicErrorText, stringValue } from '@/features/overview/management-ui';
import { memoryScheduleDefinitions, scheduleTime } from './schedule-model';

export function useMemoryScheduleSources() {
  const transport = useControlTransport();
  const settings = useQuery({ queryKey: configurationQueryKeys.settings(), queryFn: ({ signal }) => transport.request({ pathId: 'configuration.settings', signal }) });
  const status = useQuery({ queryKey: memoryQueryKeys.curationStatus(), queryFn: ({ signal }) => transport.request({ pathId: 'agent.memoryMaintenance.run', query: { limit: 12 }, signal }), refetchInterval: 30_000 });
  return { settings, status };
}

export function MemorySchedules() {
  const transport = useControlTransport();
  const client = useQueryClient();
  const { settings, status } = useMemoryScheduleSources();
  const memory = asRecord(asRecord(asRecord(settings.data).settings).memory);
  const capabilities = useQuery({ queryKey: configurationQueryKeys.capabilities(), queryFn: () => transport.capabilities(), staleTime: 30_000 });
  const [draft, setDraft] = useState<Record<string, number | boolean>>({});
  const [saved, setSaved] = useState(false);
  const mutation = useMutation({
    mutationFn: async () => {
      const current = asRecord((await settings.refetch()).data);
      const revision = current.runtimeRevision ?? asRecord(current.runtimeConfig).runtimeRevision;
      if (typeof revision !== 'number') throw new Error('暂时无法读取设置版本，请刷新后重试。');
      const context = { changes: { ...draft } };
      const preview = parseManagementWorkPreview(await transport.request({ pathId: 'configuration.settings.preview', body: { ...context, expectedRuntimeRevision: revision } }), 'configuration.settings.apply', context);
      parseManagementWorkReceipt(await transport.request({ pathId: 'configuration.settings.apply', body: {
        changes: preview.context.changes, expectedRuntimeRevision: preview.expectedRuntimeRevision, previewToken: preview.previewToken, payloadSha256: preview.payloadSha256, confirmText: preview.requiredConfirm,
      } }), 'configuration.settings.apply', preview.payloadSha256);
      const confirmed = asRecord(asRecord((await settings.refetch()).data).settings);
      for (const [path, value] of Object.entries(context.changes)) {
        const actual = path.split('.').reduce<unknown>((result, key) => asRecord(result)[key], confirmed);
        if (actual !== value) throw new Error('设置已提交，但尚未确认最新状态，请重新读取。');
      }
    },
    onSuccess: () => { setDraft({}); setSaved(true); void client.invalidateQueries({ queryKey: memoryQueryKeys.root }); },
  });
  const supported = capabilities.data?.features.configurationSettingsWorkContract && capabilities.data.routeIds?.includes('configuration.settings.apply');
  const invalid = Object.entries(draft).some(([key, value]) => typeof value === 'number' && (!Number.isInteger(value) || value < 1 || value > (key.endsWith('cadenceDays') ? 365 : 6)));
  function change(key: string, value: number | boolean) { setDraft((current) => ({ ...current, [key]: value })); setSaved(false); }
  return <section className="schedule-memory" aria-label="记忆后台任务管理">
    <header><h2>后台维护</h2><p>调整执行频率或暂停后续任务，已生成的记忆仍保留。</p></header>
    {settings.error ? <InlineNotice title="无法读取维护设置" tone="danger">{publicErrorText(settings.error)}<Button onClick={() => void settings.refetch()}>重新读取</Button></InlineNotice> : null}
    {settings.isPending ? <p role="status">正在读取后台维护设置…</p> : null}
    {memory.enabled === false ? <InlineNotice title="记忆总开关已关闭" tone="info">这些计划会保留，在记忆重新启用后继续。</InlineNotice> : null}
    {memoryScheduleDefinitions.map((definition) => {
      const config = asRecord(memory[definition.id]);
      if (typeof config.enabled !== 'boolean') return null;
      const enabledKey = `memory.${definition.id}.enabled`;
      const frequencyKey = `memory.${definition.id}.${definition.frequency}`;
      return <article key={definition.id}><div><strong>{definition.title}</strong><p>{definition.detail}</p></div><div className="schedule-memory__controls">
        <Field label={`${definition.title}频率`} htmlFor={`schedule-${definition.id}`}><Input id={`schedule-${definition.id}`} type="number" min={1} max={definition.max} value={Number(draft[frequencyKey] ?? config[definition.frequency])} disabled={!supported || mutation.isPending} onChange={(event) => change(frequencyKey, Number(event.target.value))} /><small>{definition.unit}</small></Field>
        <Switch label="自动执行" aria-label={`启用${definition.title}`} checked={Boolean(draft[enabledKey] ?? config.enabled)} disabled={!supported || mutation.isPending} onCheckedChange={(enabled) => change(enabledKey, enabled)} />
      </div></article>;
    })}
    {mutation.error ? <InlineNotice title="设置未确认" tone="danger">{publicErrorText(mutation.error)}</InlineNotice> : null}
    {saved ? <p role="status">已保存并重新读取，后续任务会使用新的安排。</p> : null}
    <Button variant="primary" loading={mutation.isPending} disabled={!supported || invalid || !Object.keys(draft).length} onClick={() => mutation.mutate()}>保存维护安排</Button>
    <h3>最近整理记录</h3>
    {status.error ? <p role="alert">暂时无法读取执行记录。<Button onClick={() => void status.refetch()}>重试</Button></p> : null}
    {arrayRecords(asRecord(status.data).runs).length ? <ol className="schedule-memory__history">{arrayRecords(asRecord(status.data).runs).map((run) => <li key={stringValue(run.runId)}><time>{scheduleTime(numberValue(run.createdAtMs))}</time><span>{stringValue(run.summary, '查看记忆整理结果')}</span><small>{({ applied: '已应用', draft: '待查看', rejected: '未采用', failed: '失败', empty: '无新内容', superseded: '已被后续整理覆盖' } as Record<string, string>)[stringValue(run.status)] ?? '已记录'}</small></li>)}</ol> : !status.isPending && !status.error ? <p className="schedule-muted">还没有可展示的整理记录。后台会在到期并满足执行条件时运行。</p> : null}
  </section>;
}
