import { useQuery, useQueryClient } from '@tanstack/react-query';
import { usePawOsAppActive } from '@/features/paw-os/surface-context';
import { useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import {
  configurationMutationPathIds,
  configurationQueryKeys,
  requestConfigurationMutation,
} from '@/features/configuration/api';
import {
  parseManagementWorkPreview,
  parseManagementWorkReceipt,
} from '@/features/overview/management-mutation';
import { asRecord, publicErrorText, stringValue } from '@/features/overview/management-ui';

export type AgentExecutionMode = 'read_only' | 'per_action' | 'workspace_managed' | 'full_trust';

export type AgentPreferences = {
  modelReference: string;
  thinking: string;
  executionMode: AgentExecutionMode;
};

export type AgentPreferencesAuthority = {
  preferences: AgentPreferences;
  isPending: boolean;
  readError: string;
  reload: () => void;
  save: (next: Partial<AgentPreferences>) => Promise<boolean>;
  saveError: string;
  saving: boolean;
  writesSupported: boolean;
};

export type AgentPreferencesRead = {
  preferences: AgentPreferences;
  isPending: boolean;
  readError: string;
  reload: () => void;
};

const defaults: AgentPreferences = {
  modelReference: '',
  thinking: 'high',
  executionMode: 'per_action',
};

const executionModes = new Set<AgentExecutionMode>([
  'read_only',
  'per_action',
  'workspace_managed',
  'full_trust',
]);

export function useAgentPreferences(): AgentPreferences {
  const query = useAgentPreferencesQuery();
  return agentPreferencesFromSettings(query.data);
}

export function useAgentPreferencesRead(): AgentPreferencesRead {
  const query = useAgentPreferencesQuery();
  return {
    preferences: agentPreferencesFromSettings(query.data),
    isPending: query.isPending,
    readError: query.error
      ? publicErrorText(query.error, '暂时无法读取本机 Agent 默认设置，请重新读取。')
      : '',
    reload: () => { void query.refetch(); },
  };
}

export function useAgentPreferencesAuthority(): AgentPreferencesAuthority {
  const transport = useControlTransport();
  const active = usePawOsAppActive() ?? true;
  const queryClient = useQueryClient();
  const settingsQuery = useAgentPreferencesQuery();
  const capabilitiesQuery = useQuery({
    enabled: active,
    queryKey: configurationQueryKeys.capabilities(),
    queryFn: () => transport.capabilities(),
    staleTime: 30_000,
  });
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const preferences = agentPreferencesFromSettings(settingsQuery.data);
  const routeIds = new Set(capabilitiesQuery.data?.routeIds ?? []);
  const writesSupported = Boolean(
    capabilitiesQuery.data?.features.managementWorkContract
    && capabilitiesQuery.data?.features.configurationSettingsWorkContract
    && routeIds.has(configurationMutationPathIds.preview)
    && routeIds.has(configurationMutationPathIds.apply),
  );

  return {
    preferences,
    isPending: settingsQuery.isPending || capabilitiesQuery.isPending,
    readError: settingsQuery.error
      ? publicErrorText(settingsQuery.error, '暂时无法读取本机 Agent 默认设置，请重新读取。')
      : capabilitiesQuery.error
        ? publicErrorText(capabilitiesQuery.error, '暂时无法确认 Agent 默认设置是否可保存，请重新读取。')
        : '',
    reload: () => {
      setSaveError('');
      void Promise.all([settingsQuery.refetch(), capabilitiesQuery.refetch()]);
    },
    save,
    saveError,
    saving,
    writesSupported,
  };

  async function save(next: Partial<AgentPreferences>): Promise<boolean> {
    if (saving) return false;
    if (!writesSupported) {
      setSaveError('当前版本不能安全保存 Agent 默认设置；请更新本机服务后重新读取。');
      return false;
    }
    const runtimeRevision = settingsRuntimeRevision(settingsQuery.data);
    if (runtimeRevision === null) {
      setSaveError('当前设置版本无法确认，请重新读取后再保存。');
      return false;
    }
    const merged = normalizePreferences({ ...preferences, ...next });
    const changes = preferenceChanges(preferences, merged);
    if (!Object.keys(changes).length) return true;

    const previous = queryClient.getQueryData(configurationQueryKeys.settings());
    setSaving(true);
    setSaveError('');
    queryClient.setQueryData(
      configurationQueryKeys.settings(),
      settingsWithAgentPreferences(previous ?? settingsQuery.data, merged),
    );
    try {
      const context = { changes };
      const preview = parseManagementWorkPreview(
        await requestConfigurationMutation(transport, {
          pathId: configurationMutationPathIds.preview,
          body: { changes, expectedRuntimeRevision: runtimeRevision },
        }),
        configurationMutationPathIds.apply,
        context,
      );
      parseManagementWorkReceipt(
        await requestConfigurationMutation(transport, {
          pathId: configurationMutationPathIds.apply,
          body: {
            changes: preview.context.changes,
            expectedRuntimeRevision: preview.expectedRuntimeRevision,
            previewToken: preview.previewToken,
            payloadSha256: preview.payloadSha256,
            confirmText: preview.requiredConfirm,
          },
        }),
        configurationMutationPathIds.apply,
        preview.payloadSha256,
      );
      const refreshed = await settingsQuery.refetch();
      if (!refreshed.data) throw new Error('保存完成，但暂时无法重新读取本机 Agent 默认设置，请重新读取。');
      return true;
    } catch (error) {
      queryClient.setQueryData(configurationQueryKeys.settings(), previous);
      setSaveError(publicErrorText(error, 'Agent 默认设置没有保存，请重新读取后重试。'));
      return false;
    } finally {
      setSaving(false);
    }
  }
}

function useAgentPreferencesQuery() {
  const transport = useControlTransport();
  const active = usePawOsAppActive() ?? true;
  return useQuery({
    enabled: active,
    queryKey: configurationQueryKeys.settings(),
    queryFn: ({ signal }) => transport.request({ pathId: 'configuration.settings', signal }),
    staleTime: 0,
  });
}

export function agentPreferencesFromSettings(value: unknown): AgentPreferences {
  const settings = asRecord(asRecord(value).settings);
  const agentDefaults = asRecord(asRecord(settings.agent).defaults);
  const modelReference = stringValue(agentDefaults.modelReference, 'inherit');
  return normalizePreferences({
    modelReference: modelReference === 'inherit' ? '' : modelReference,
    thinking: stringValue(agentDefaults.thinkingLevel, defaults.thinking),
    executionMode: stringValue(agentDefaults.executionMode, defaults.executionMode) as AgentExecutionMode,
  });
}

function normalizePreferences(value: AgentPreferences): AgentPreferences {
  return {
    modelReference: value.modelReference.trim(),
    thinking: value.thinking.trim() || defaults.thinking,
    executionMode: executionModes.has(value.executionMode) ? value.executionMode : defaults.executionMode,
  };
}

function preferenceChanges(
  current: AgentPreferences,
  next: AgentPreferences,
): Record<string, string> {
  const changes: Record<string, string> = {};
  if (next.modelReference !== current.modelReference) {
    changes['agent.defaults.modelReference'] = next.modelReference || 'inherit';
  }
  if (next.thinking !== current.thinking) changes['agent.defaults.thinkingLevel'] = next.thinking;
  if (next.executionMode !== current.executionMode) changes['agent.defaults.executionMode'] = next.executionMode;
  return changes;
}

function settingsWithAgentPreferences(value: unknown, preferences: AgentPreferences): Record<string, unknown> {
  const envelope = asRecord(value);
  const settings = asRecord(envelope.settings);
  const agent = asRecord(settings.agent);
  const agentDefaults = asRecord(agent.defaults);
  return {
    ...envelope,
    settings: {
      ...settings,
      agent: {
        ...agent,
        defaults: {
          ...agentDefaults,
          modelReference: preferences.modelReference || 'inherit',
          thinkingLevel: preferences.thinking,
          executionMode: preferences.executionMode,
        },
      },
    },
  };
}

function settingsRuntimeRevision(value: unknown): number | null {
  const envelope = asRecord(value);
  const revision = envelope.runtimeRevision ?? asRecord(envelope.runtimeConfig).runtimeRevision;
  return typeof revision === 'number' && Number.isInteger(revision) && revision >= 0 ? revision : null;
}
