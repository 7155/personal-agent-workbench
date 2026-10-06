import { useQuery } from '@tanstack/react-query';
import { useControlTransport } from '@/app/control-transport';
import { usePawOsAppActive } from '@/features/paw-os/surface-context';
import {
  configurationMutationPathIds,
  requestConfigurationMutation,
  type ConfigurationMutationRequest,
} from '@/features/configuration/api';
import type { MutationAvailability } from '@/features/overview/management-mutation';
import type { ControlTransport } from '@/platform/transport';

export type LexiconReviewEntry = {
  reviewKey: string;
  text: string;
  pinyin: string;
  weight: number;
  positiveCount: number;
  negativeCount: number;
  reasons: readonly string[];
  reviewSource: string;
  reviewReason: string;
  selected: boolean;
  defaultSelected: boolean;
  riskLabel: string;
};

export type LexiconOrganizationRun = {
  runId: string;
  status: 'running' | 'succeeded' | 'failed' | '';
  startedAtMs: number;
  completedAtMs: number;
  candidateCount: number;
  filteredEntryCount: number;
  errorCode: string;
  error: string;
};

export type LexiconOrganizationStatus = {
  schemaVersion: 'rag-ime.lexicon-organization-status.v1';
  owner: 'maintenance_poll';
  decoderOwner: 'rime';
  enabled: boolean;
  runsPerDay: number;
  intervalMs: number;
  candidateLimit: number;
  lastRunAtMs: number;
  lastSucceededAtMs: number;
  nextRunAtMs: number | null;
  due: boolean;
  lastRun: LexiconOrganizationRun;
};

export type LexiconReview = {
  schemaVersion: 'rag-ime.rime-lexicon-review.v1';
  project: string;
  entryCount: number;
  entries: readonly LexiconReviewEntry[];
  reviewToken: string;
  confirmText: string;
  applySupported: boolean;
  reviewRequired: boolean;
  filteredEntryCount?: number;
  selectionPolicy?: string;
  organization: LexiconOrganizationStatus;
};

export type LexiconMutationReceipt = {
  rollbackId: string;
  entryCount: number;
  requiresRedeploy: boolean;
};

const lexiconPathIds = [
  'input.lexicon.review',
  'input.lexicon.apply',
  'input.lexicon.rollback',
] as const;

export const inputSettingsMutationPathIds = configurationMutationPathIds;

export const inputMethodQueryKeys = {
  root: ['input-method'] as const,
  source: () => [...inputMethodQueryKeys.root, 'source'] as const,
  overview: () => [...inputMethodQueryKeys.root, 'overview'] as const,
  models: () => [...inputMethodQueryKeys.root, 'models'] as const,
  settings: () => [...inputMethodQueryKeys.root, 'settings'] as const,
  schema: () => [...inputMethodQueryKeys.root, 'schema'] as const,
  capabilities: () => [...inputMethodQueryKeys.root, 'capabilities'] as const,
  predictionLiveTrace: () => [...inputMethodQueryKeys.root, 'prediction-live-trace'] as const,
  lexiconReview: () => [...inputMethodQueryKeys.root, 'lexicon-review'] as const,
};

export type InputMethodQueryScope = 'input' | 'lexicon';

export function useInputMethodQueries(scope: InputMethodQueryScope = 'input') {
  const transport = useControlTransport();
  const active = usePawOsAppActive() ?? true;
  const inputEnabled = scope === 'input' && active;
  const source = useQuery({
    enabled: inputEnabled,
    queryKey: inputMethodQueryKeys.source(),
    queryFn: ({ signal }) => transport.request({ pathId: 'input.source.get', signal }),
    refetchInterval: inputEnabled ? 10_000 : false,
  });
  const overview = useQuery({
    enabled: inputEnabled,
    queryKey: inputMethodQueryKeys.overview(),
    queryFn: ({ signal }) => transport.request({ pathId: 'overview.get', signal }),
  });
  const models = useQuery({
    enabled: inputEnabled,
    queryKey: inputMethodQueryKeys.models(),
    queryFn: async ({ signal }) => {
      try {
        return await transport.request({ pathId: 'diagnostics.models', signal });
      } catch (error) {
        if (signal.aborted) throw error;
        return {
          ok: false,
          schemaVersion: 'rag-ime.models-status.v4',
          statusUnavailable: true,
        };
      }
    },
  });
  const settings = useQuery({
    enabled: inputEnabled,
    queryKey: inputMethodQueryKeys.settings(),
    queryFn: ({ signal }) => transport.request({ pathId: 'configuration.settings', signal }),
  });
  const schema = useQuery({
    enabled: inputEnabled,
    queryKey: inputMethodQueryKeys.schema(),
    queryFn: ({ signal }) => transport.request({ pathId: 'configuration.schema', signal }),
  });
  const capabilities = useQuery({
    enabled: active,
    queryKey: inputMethodQueryKeys.capabilities(),
    queryFn: () => transport.capabilities(),
    staleTime: Infinity,
  });
  const predictionLiveTrace = useQuery({
    enabled: inputEnabled,
    queryKey: inputMethodQueryKeys.predictionLiveTrace(),
    queryFn: ({ signal }) => transport.request({
      pathId: 'input.prediction.liveTrace',
      query: { limit: 5 },
      signal,
    }),
    refetchInterval: inputEnabled ? 5_000 : false,
  });
  const lexiconAvailable = Boolean(
    capabilities.data
      && lexiconPathIds.every((pathId) => capabilities.data.routeIds.includes(pathId)),
  );
  const lexiconReview = useQuery({
    enabled: active && scope === 'lexicon' && lexiconAvailable,
    queryKey: inputMethodQueryKeys.lexiconReview(),
    queryFn: async ({ signal }) => parseLexiconReview(await transport.request({
      pathId: 'input.lexicon.review',
      query: { limit: 200 },
      signal,
    })),
    staleTime: 10_000,
  });
  const settingsMutationAvailability = (blockedReason = ''): MutationAvailability => {
    if (capabilities.isPending) return { state: 'checking' };
    if (capabilities.error) {
      return {
        state: 'blocked',
        reason: '无法确认本机是否支持安全保存，请刷新后重试。',
      };
    }
    const flags = capabilities.data?.features ?? {};
    const routeIds = new Set(capabilities.data?.routeIds ?? []);
    if (
      !flags.managementWorkContract
      || !flags.configurationSettingsWorkContract
      || Object.values(inputSettingsMutationPathIds).some((pathId) => !routeIds.has(pathId))
    ) {
      return {
        state: 'blocked',
        reason: '当前应用不支持安全保存这项设置，本次修改不会发送。',
      };
    }
    if (blockedReason) return { state: 'blocked', reason: blockedReason };
    return { state: 'available' };
  };
  return {
    capabilities,
    lexiconAvailable,
    lexiconReview,
    models,
    overview,
    predictionLiveTrace,
    schema,
    settings,
    settingsMutationAvailability,
    requestSettingsMutation: <Response,>(request: ConfigurationMutationRequest) => (
      requestConfigurationMutation<Response>(transport, request)
    ),
    source,
    transport,
    transportKind: transport.kind,
  };
}

export async function applyLexiconReview(
  transport: ControlTransport,
  review: LexiconReview,
  selectedKeys: readonly string[],
): Promise<LexiconMutationReceipt> {
  const payload = await transport.request({
    pathId: 'input.lexicon.apply',
    body: {
      reviewToken: review.reviewToken,
      selectedKeys: [...selectedKeys],
      confirmText: review.confirmText,
      project: review.project,
      limit: 200,
    },
  });
  return parseLexiconMutation(payload, 'apply');
}

export async function rollbackLexiconReview(
  transport: ControlTransport,
  rollbackId: string,
): Promise<LexiconMutationReceipt> {
  const payload = await transport.request({
    pathId: 'input.lexicon.rollback',
    body: { rollbackId },
  });
  return parseLexiconMutation(payload, 'rollback');
}

function parseLexiconReview(value: unknown): LexiconReview {
  const payload = record(value);
  if (payload.ok !== true) throw new Error(failureMessage(payload.reason ?? payload.error, payload));
  if (payload.schemaVersion !== 'rag-ime.rime-lexicon-review.v1') {
    throw new Error('词库审阅返回了不兼容的数据。');
  }
  const reviewToken = requiredString(payload.reviewToken, 'reviewToken');
  const confirmText = requiredString(payload.confirmText, 'confirmText');
  const entries = Array.isArray(payload.entries) ? payload.entries.map(parseLexiconEntry) : [];
  const entryCount = nonNegativeInteger(payload.entryCount, 'entryCount');
  if (entryCount !== entries.length) throw new Error('词库审阅信息不一致，请刷新后重试。');
  return {
    schemaVersion: payload.schemaVersion,
    project: stringValue(payload.project),
    entryCount,
    entries,
    reviewToken,
    confirmText,
    applySupported: payload.applySupported === true,
    reviewRequired: payload.reviewRequired === true,
    filteredEntryCount: optionalNonNegativeInteger(payload.filteredEntryCount),
    selectionPolicy: stringValue(payload.selectionPolicy),
    organization: parseLexiconOrganization(payload.organization),
  };
}

function parseLexiconOrganization(value: unknown): LexiconOrganizationStatus {
  const payload = record(value);
  if (payload.schemaVersion !== 'rag-ime.lexicon-organization-status.v1') {
    throw new Error('词库定期整理返回了不兼容的数据。');
  }
  if (payload.owner !== 'maintenance_poll' || payload.decoderOwner !== 'rime') {
    throw new Error('词库定期整理的运行归属不明确。');
  }
  const nextRunAtMs = payload.nextRunAtMs === null
    ? null
    : nonNegativeInteger(payload.nextRunAtMs, 'organization.nextRunAtMs');
  const rawLastRun = record(payload.lastRun);
  const rawStatus = stringValue(rawLastRun.status);
  const status = ['running', 'succeeded', 'failed'].includes(rawStatus)
    ? rawStatus as LexiconOrganizationRun['status']
    : '';
  return {
    schemaVersion: payload.schemaVersion,
    owner: payload.owner,
    decoderOwner: payload.decoderOwner,
    enabled: payload.enabled === true,
    runsPerDay: nonNegativeInteger(payload.runsPerDay, 'organization.runsPerDay'),
    intervalMs: nonNegativeInteger(payload.intervalMs, 'organization.intervalMs'),
    candidateLimit: nonNegativeInteger(payload.candidateLimit, 'organization.candidateLimit'),
    lastRunAtMs: nonNegativeInteger(payload.lastRunAtMs, 'organization.lastRunAtMs'),
    lastSucceededAtMs: nonNegativeInteger(payload.lastSucceededAtMs, 'organization.lastSucceededAtMs'),
    nextRunAtMs,
    due: payload.due === true,
    lastRun: {
      runId: stringValue(rawLastRun.runId),
      status,
      startedAtMs: optionalNonNegativeInteger(rawLastRun.startedAtMs) ?? 0,
      completedAtMs: optionalNonNegativeInteger(rawLastRun.completedAtMs) ?? 0,
      candidateCount: optionalNonNegativeInteger(rawLastRun.candidateCount) ?? 0,
      filteredEntryCount: optionalNonNegativeInteger(rawLastRun.filteredEntryCount) ?? 0,
      errorCode: stringValue(rawLastRun.errorCode),
      error: stringValue(rawLastRun.error),
    },
  };
}

function parseLexiconEntry(value: unknown): LexiconReviewEntry {
  const entry = record(value);
  return {
    reviewKey: requiredString(entry.reviewKey, 'reviewKey'),
    text: requiredString(entry.text, 'text'),
    pinyin: stringValue(entry.pinyin),
    weight: numberValue(entry.weight),
    positiveCount: numberValue(entry.positiveCount),
    negativeCount: numberValue(entry.negativeCount),
    reasons: Array.isArray(entry.reasons)
      ? entry.reasons.filter((item): item is string => typeof item === 'string' && item.length > 0)
      : [],
    reviewSource: stringValue(entry.reviewSource),
    reviewReason: stringValue(entry.reviewReason),
    selected: entry.selected === true,
    // 后端对每条候选给出真实的风险归类与默认勾选建议；丢掉它们会让
    // 审阅页把模型建议词和真实选词反馈混成同一种"待你判断"。
    defaultSelected: entry.defaultSelected === true,
    riskLabel: stringValue(entry.riskLabel),
  };
}

function parseLexiconMutation(value: unknown, kind: 'apply' | 'rollback'): LexiconMutationReceipt {
  const payload = record(value);
  const succeeded = kind === 'apply' ? payload.applied === true : payload.rolledBack === true;
  if (payload.ok !== true || !succeeded) {
    throw new Error(failureMessage(payload.reason ?? payload.error, payload));
  }
  if (payload.schemaVersion !== 'rag-ime.rime-lexicon-review.v1') {
    throw new Error('词库操作返回了不兼容的数据。');
  }
  return {
    rollbackId: requiredString(payload.rollbackId, 'rollbackId'),
    entryCount: payload.entryCount === undefined ? 0 : nonNegativeInteger(payload.entryCount, 'entryCount'),
    // Missing redeploy state is treated conservatively: never claim foreground activation.
    requiresRedeploy: payload.requiresRedeploy !== false,
  };
}

function failureMessage(value: unknown, payload: Record<string, unknown>): string {
  const reason = stringValue(value) || 'unknown_error';
  const message = ({
    review_token_stale: '词库审阅已变化，请刷新后重新选择。',
    no_reviewed_entries: '没有可应用的已审词条。',
    confirm_text_required: '本次词库审阅已失效，请刷新后重新选择。',
    rollback_manifest_missing: '找不到该词库操作记录。',
    rollback_manifest_invalid: '词库撤销记录已损坏，未执行撤销。',
    rollback_already_applied: '该词库操作已经撤销。',
    newer_rollback_required_first: '存在更新的词库写入，必须先撤销最新一次操作。',
  } as Record<string, string>)[reason] ?? '词库操作失败，请刷新后重试。';
  const blockingRollbackId = stringValue(payload.blockingRollbackId);
  return blockingRollbackId ? `${message} 存在更新的操作记录。` : message;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function requiredString(value: unknown, field: string): string {
  const text = stringValue(value);
  if (!text) throw new Error(field === 'reviewKey' || field === 'text' ? '词库条目数据不完整。' : '词库审阅信息不完整，未执行操作。');
  return text;
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function numberValue(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

function optionalNonNegativeInteger(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0
    ? value
    : undefined;
}

function nonNegativeInteger(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw new Error(field === 'entryCount' ? '词库条目数量无效。' : '词库返回的数据无效。');
  }
  return value;
}
