import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Brain, Gauge, RefreshCw, Save } from 'lucide-react';
import { useMotionActivity } from '@/design/motion';
import { useControlTransport } from '@/app/control-transport';
import { Button, Field, Select, Switch } from '@/components/primitives';
import {
  InlineNotice,
  ManagementSection,
  QueryState,
  asRecord,
  booleanValue,
  numberValue,
  publicErrorText,
  stringValue,
} from '@/features/overview/management-ui';
import {
  parseManagementWorkPreview,
  parseManagementWorkReceipt,
} from '@/features/overview/management-mutation';

type PreferenceLevel = 'avoid' | 'weaken' | 'normal' | 'priority';
type StablePreferenceLevel = Exclude<PreferenceLevel, 'avoid'>;
type TemporaryDetailLevel = Exclude<PreferenceLevel, 'priority'>;
type RecallDetail = 'compact' | 'balanced' | 'detailed';
type TimelineMaxItems = '1' | '2' | '3' | '4';

type MemoryPreferenceDraft = {
  memoryEnabled: boolean;
  stablePreference: StablePreferenceLevel;
  temporaryDetails: TemporaryDetailLevel;
  includeAgentDialogue: boolean;
  recallDetail: RecallDetail;
  timelineEnabled: boolean;
  timelineMaxItems: TimelineMaxItems;
};

type MemoryRecallRuntimeContract = {
  memoryEnabled: boolean | null;
  agentDecides: boolean | null;
  maxPerCompactionCycle: number | null;
};

const stablePreferenceOptions = [
  { value: 'weaken', label: '弱化' },
  { value: 'normal', label: '正常记住' },
  { value: 'priority', label: '优先记住' },
] as const;
const temporaryDetailOptions = [
  { value: 'avoid', label: '尽快忘记' },
  { value: 'weaken', label: '弱化' },
  { value: 'normal', label: '正常记住' },
] as const;
const recallDetailOptions = [
  { value: 'compact', label: '简洁' },
  { value: 'balanced', label: '平衡' },
  { value: 'detailed', label: '详细' },
] as const;
const timelineMaxItemsOptions = [
  { value: '1', label: '1 条' },
  { value: '2', label: '2 条' },
  { value: '3', label: '3 条' },
  { value: '4', label: '4 条' },
] as const;

export function MemoryPreferences() {
  const motionActive = useMotionActivity();
  const transport = useControlTransport();
  const settingsQuery = useQuery({
    queryKey: ['memory', 'preferences', 'settings'],
    queryFn: ({ signal }) => transport.request({ pathId: 'configuration.settings', signal }),
    staleTime: 0,
  });
  const capabilitiesQuery = useQuery({
    queryKey: ['memory', 'preferences', 'capabilities'],
    queryFn: () => transport.capabilities(),
    staleTime: 30_000,
  });
  const persisted = useMemo(() => memoryPreferenceDraft(settingsQuery.data), [settingsQuery.data]);
  const recallContract = useMemo(() => memoryRecallRuntimeContract(settingsQuery.data), [settingsQuery.data]);
  const [draft, setDraft] = useState<Partial<MemoryPreferenceDraft>>({});
  const [saving, setSaving] = useState(false);
  const [success, setSuccess] = useState('');
  const [saveError, setSaveError] = useState('');

  const current = { ...persisted, ...draft };
  const changes = memoryPreferenceChanges(persisted, current);
  const routeIds = new Set(capabilitiesQuery.data?.routeIds ?? []);
  const writesSupported = Boolean(
    capabilitiesQuery.data?.features.managementWorkContract
    && capabilitiesQuery.data?.features.configurationSettingsWorkContract
    && routeIds.has('configuration.settings.preview')
    && routeIds.has('configuration.settings.apply'),
  );
  const rawError = settingsQuery.error ?? capabilitiesQuery.error;
  const queryError = rawError
    ? new Error(publicErrorText(rawError, '暂时无法读取本机记忆偏好，请刷新后重试。'))
    : null;
  const hasLoadedPreferences = Boolean(settingsQuery.data && capabilitiesQuery.data);
  const reading = settingsQuery.isFetching || capabilitiesQuery.isFetching;
  const editingDisabled = !writesSupported || saving || reading || Boolean(queryError);

  return (
    <div
      className="memory-preferences"
      data-dirty={Boolean(Object.keys(changes).length) || undefined}
      data-writable={writesSupported || undefined}
    >
      <ManagementSection
        description="这些选择直接影响本机记忆整理与召回：什么值得保留更久，什么应更快淡出，以及回答时带回多少上下文。"
        title="记忆偏好"
      >
      <QueryState
        error={hasLoadedPreferences ? null : queryError}
        isPending={settingsQuery.isPending || capabilitiesQuery.isPending}
        onRetry={() => void reread()}
      >
        {queryError && hasLoadedPreferences ? (
          <InlineNotice title="暂时无法更新记忆偏好" tone="warning">{queryError.message} 当前选择仍保留，重新读取成功后可继续保存。</InlineNotice>
        ) : null}
        <div className="memory-preferences__intro">
          <span aria-hidden="true"><Brain size={22} /></span>
          <div><strong>把第二大脑调成你的记忆方式</strong><p>这里不会删除既有记忆；偏好会从下一次整理和召回开始生效。</p></div>
          <span className="memory-preferences__persistence" data-busy={saving || reading} data-motion-active={motionActive} data-state={queryError ? 'pending' : !writesSupported ? 'read-only' : Object.keys(changes).length ? 'pending' : 'synced'}>
            <i aria-hidden="true" />
            {queryError ? '等待读取最新设置' : !writesSupported ? '只读' : Object.keys(changes).length ? '等待保存' : '已从本机读取'}
          </span>
        </div>
        <div className="memory-preferences__recall-policy" aria-label="Agent 记忆召回策略">
          <span aria-hidden="true"><Gauge size={17} /></span>
          <div>
            <strong>Agent 记忆召回</strong>
            <p>{recallPolicyDescription(recallContract)}</p>
          </div>
          <small>{recallBudgetDescription(recallContract)}</small>
        </div>
        <div className="memory-preferences__master">
          <Switch
            checked={current.memoryEnabled}
            disabled={editingDisabled}
            description="总开关：关闭后不记录、不整理，也不会把记忆召回到 Agent 上下文；不会删除已经保存的记忆。"
            label="启用记忆增强"
            onCheckedChange={(checked) => updateDraft({ memoryEnabled: checked })}
          />
        </div>
        <div className="memory-preferences__grid">
          <Field description="用户习惯、长期选择和反复确认的偏好会按这个强度保留。" htmlFor="memory-stable-preference" label="稳定偏好">
            <Select
              aria-label="稳定偏好"
              disabled={editingDisabled}
              id="memory-stable-preference"
              onValueChange={(value) => updateDraft({ stablePreference: value })}
              options={stablePreferenceOptions}
              value={current.stablePreference}
            />
          </Field>
          <Field description="一次性安排、临时状态和短期细节会按这个速度降低召回权重。" htmlFor="memory-temporary-details" label="临时事项">
            <Select
              aria-label="临时事项"
              disabled={editingDisabled}
              id="memory-temporary-details"
              onValueChange={(value) => updateDraft({ temporaryDetails: value })}
              options={temporaryDetailOptions}
              value={current.temporaryDetails}
            />
          </Field>
          <Field description="决定回答时带回多少已治理记忆；越详细，带回的上下文越多。" htmlFor="memory-recall-detail" label="召回详细程度">
            <Select
              aria-label="召回详细程度"
              disabled={editingDisabled}
              id="memory-recall-detail"
              onValueChange={(value) => updateDraft({ recallDetail: value })}
              options={recallDetailOptions}
              value={current.recallDetail}
            />
          </Field>
          <div className="memory-preferences__switch">
            <Switch
              checked={current.includeAgentDialogue}
              disabled={editingDisabled}
              description="关闭后，新的自动整理不再把 Agent 对话摘要作为辅助上下文；不会删除已经保存的记忆。"
              label="让 Agent 对话摘要参与整理"
              onCheckedChange={(checked) => updateDraft({ includeAgentDialogue: checked })}
            />
          </div>
          <div className="memory-preferences__switch">
            <Switch
              checked={current.timelineEnabled}
              disabled={editingDisabled}
              description="仅控制带明确日期或时间表达的召回是否进入时间线通道；它不是记忆召回总开关。"
              label="按需召回时间线"
              onCheckedChange={(checked) => updateDraft({ timelineEnabled: checked })}
            />
          </div>
          <Field description="限制一次时间线召回带回的条目数，避免挤占当前任务上下文。" htmlFor="memory-timeline-max-items" label="时间线召回上限">
            <Select
              aria-label="时间线召回上限"
              disabled={editingDisabled}
              id="memory-timeline-max-items"
              onValueChange={(value) => updateDraft({ timelineMaxItems: value })}
              options={timelineMaxItemsOptions}
              value={current.timelineMaxItems}
            />
          </Field>
        </div>
        {!writesSupported ? (
          <InlineNotice title="当前版本只能读取偏好" tone="warning">本机服务尚未开放安全保存接口；页面不会保留仅存在于前端的修改。</InlineNotice>
        ) : null}
        {success ? <InlineNotice title="记忆偏好已保存" tone="success">{success}</InlineNotice> : null}
        {saveError ? <InlineNotice title="记忆偏好没有保存" tone="danger">{saveError}</InlineNotice> : null}
        <div className="memory-preferences__actions">
          <Button
            disabled={editingDisabled || !Object.keys(changes).length}
            leadingIcon={<Save size={15} />}
            loading={saving}
            onClick={() => void save()}
            size="small"
          >保存记忆偏好</Button>
          <Button
            disabled={saving || reading}
            leadingIcon={<RefreshCw size={15} />}
            onClick={() => void reread()}
            size="small"
            variant="quiet"
          >重新读取</Button>
          <span>{queryError ? '当前偏好等待重新核对' : Object.keys(changes).length ? `${Object.keys(changes).length} 项待保存` : '已与本机设置同步'}</span>
        </div>
      </QueryState>
      </ManagementSection>
    </div>
  );

  function updateDraft(change: Partial<MemoryPreferenceDraft>) {
    setDraft((value) => {
      const next = { ...value, ...change };
      for (const key of Object.keys(change) as (keyof MemoryPreferenceDraft)[]) {
        if (next[key] === persisted[key]) delete next[key];
      }
      return next;
    });
    setSuccess('');
    setSaveError('');
  }

  async function reread(): Promise<void> {
    setSuccess('');
    setSaveError('');
    const [refreshed] = await Promise.all([settingsQuery.refetch(), capabilitiesQuery.refetch()]);
    if (!refreshed.isError && refreshed.data) setDraft({});
  }

  async function save(): Promise<void> {
    const runtimeRevision = settingsRuntimeRevision(settingsQuery.data);
    if (editingDisabled || runtimeRevision === null || !Object.keys(changes).length) return;
    setSaving(true);
    setSuccess('');
    setSaveError('');
    try {
      const context = { changes: { ...changes } };
      const preview = parseManagementWorkPreview(
        await transport.request({
          pathId: 'configuration.settings.preview',
          body: { ...context, expectedRuntimeRevision: runtimeRevision },
        }),
        'configuration.settings.apply',
        context,
      );
      parseManagementWorkReceipt(
        await transport.request({
          pathId: 'configuration.settings.apply',
          body: {
            changes: preview.context.changes,
            expectedRuntimeRevision: preview.expectedRuntimeRevision,
            previewToken: preview.previewToken,
            payloadSha256: preview.payloadSha256,
            confirmText: preview.requiredConfirm,
          },
        }),
        'configuration.settings.apply',
        preview.payloadSha256,
      );
      const refreshed = await settingsQuery.refetch();
      if (refreshed.isError || !refreshed.data) {
        setSuccess('已写入本机设置，但尚未读到最新偏好。请重新读取后核对。');
        return;
      }
      setDraft({});
      setSuccess('已写入本机设置并重新读取；之后的记忆整理与召回会使用这些偏好。');
    } catch (error) {
      setSaveError(publicErrorText(error, '保存失败，本次修改没有被当作已生效；请重新读取后重试。'));
    } finally {
      setSaving(false);
    }
  }
}

function memoryPreferenceDraft(value: unknown): MemoryPreferenceDraft {
  const settings = asRecord(asRecord(value).settings);
  const memory = asRecord(settings.memory);
  const timeDecay = asRecord(memory.timeDecay);
  const automatic = asRecord(memory.automaticOrganization);
  const recall = asRecord(memory.recall);
  const stableDays = numberValue(timeDecay.stablePreferenceHalfLifeDays, 365);
  const temporaryDays = numberValue(timeDecay.temporaryHalfLifeDays, 14);
  const recallDetail = stringValue(recall.detailLevel, 'compact');
  const timelineMaxItems = Math.min(4, Math.max(1, Math.round(numberValue(recall.timelineMaxItems, 2))));
  return {
    memoryEnabled: booleanValue(memory.enabled, true),
    stablePreference: stableDays >= 730 ? 'priority' : stableDays <= 180 ? 'weaken' : 'normal',
    temporaryDetails: temporaryDays <= 1 ? 'avoid' : temporaryDays <= 7 ? 'weaken' : 'normal',
    includeAgentDialogue: booleanValue(automatic.includeAgentDialogue, true),
    recallDetail: ['compact', 'balanced', 'detailed'].includes(recallDetail) ? recallDetail as RecallDetail : 'compact',
    timelineEnabled: booleanValue(recall.timelineEnabled, true),
    timelineMaxItems: String(timelineMaxItems) as TimelineMaxItems,
  };
}

function memoryPreferenceChanges(persisted: MemoryPreferenceDraft, draft: MemoryPreferenceDraft): Record<string, boolean | number | string> {
  const changes: Record<string, boolean | number | string> = {};
  if (draft.memoryEnabled !== persisted.memoryEnabled) changes['memory.enabled'] = draft.memoryEnabled;
  if (draft.stablePreference !== persisted.stablePreference) {
    changes['memory.timeDecay.stablePreferenceHalfLifeDays'] = ({ weaken: 180, normal: 365, priority: 730 } as const)[draft.stablePreference];
  }
  if (draft.temporaryDetails !== persisted.temporaryDetails) {
    changes['memory.timeDecay.temporaryHalfLifeDays'] = ({ avoid: 1, weaken: 7, normal: 14 } as const)[draft.temporaryDetails];
  }
  if (draft.includeAgentDialogue !== persisted.includeAgentDialogue) {
    changes['memory.automaticOrganization.includeAgentDialogue'] = draft.includeAgentDialogue;
  }
  if (draft.recallDetail !== persisted.recallDetail) changes['memory.recall.detailLevel'] = draft.recallDetail;
  if (draft.timelineEnabled !== persisted.timelineEnabled) changes['memory.recall.timelineEnabled'] = draft.timelineEnabled;
  if (draft.timelineMaxItems !== persisted.timelineMaxItems) changes['memory.recall.timelineMaxItems'] = Number(draft.timelineMaxItems);
  return changes;
}

function memoryRecallRuntimeContract(value: unknown): MemoryRecallRuntimeContract {
  const memory = asRecord(asRecord(asRecord(value).settings).memory);
  const recall = memory.recall;
  const contract = asRecord(recall);
  return {
    memoryEnabled: typeof memory.enabled === 'boolean' ? memory.enabled : null,
    agentDecides: typeof contract.agentDecides === 'boolean' ? contract.agentDecides : null,
    maxPerCompactionCycle: Number.isInteger(contract.maxPerCompactionCycle) && Number(contract.maxPerCompactionCycle) > 0
      ? Number(contract.maxPerCompactionCycle)
      : null,
  };
}

function recallPolicyDescription(contract: MemoryRecallRuntimeContract): string {
  if (contract.memoryEnabled === null) {
    return '记忆总开关由 Runtime 管理；当前版本尚未返回 settings.memory.enabled。';
  }
  if (!contract.memoryEnabled) return '已关闭；不会把个人记忆加入回答上下文，也不会新增记忆记录。';
  return contract.agentDecides === true
    ? '已开启，由 Agent 根据当前任务判断是否调用。'
    : '已开启，Agent 会根据当前任务按需召回。';
}

function recallBudgetDescription(contract: MemoryRecallRuntimeContract): string {
  return contract.maxPerCompactionCycle === null || contract.maxPerCompactionCycle === 1
    ? '每个压缩周期最多调用一次'
    : `每个压缩周期最多调用 ${contract.maxPerCompactionCycle} 次`;
}

function settingsRuntimeRevision(value: unknown): number | null {
  const envelope = asRecord(value);
  const revision = envelope.runtimeRevision ?? asRecord(envelope.runtimeConfig).runtimeRevision;
  return typeof revision === 'number' && Number.isInteger(revision) && revision >= 0 ? revision : null;
}
