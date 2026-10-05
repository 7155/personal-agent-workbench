import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useControlTransport } from '@/app/control-transport';
import {
  parseCapabilityDefaults,
  requireCapabilityCatalog,
  requireSessionCapabilityCatalog,
  type CapabilityPreference,
} from './capability-policy';
import { PAW_EXTENSION_INSTALLATION_CHANGED_EVENT } from '@/paw-os/extensions/installation';
import { asRecord, stringValue } from '@/features/overview/management-ui';

export type SkillSourceKind = 'package' | 'bundled' | 'project';

export type SkillInventoryItem = {
  skillId: string;
  name: string;
  description: string;
  sourceKind: SkillSourceKind;
  packageId?: string;
  packageVersion?: string;
  resourcePath: string;
  enabled: boolean | null;
  installed: boolean;
  installState: string;
  digest: string;
  contentRevision: string;
  sizeBytes: number;
  management: 'package' | 'inspect_only';
  managementReason: string;
  actions: string[];
};

export type SkillInventoryResponse = {
  schemaVersion: 'rag-ime.skill-inventory.v1';
  ok: boolean;
  runtimeAvailable: boolean;
  revision: string;
  items: SkillInventoryItem[];
};

export type SkillDetailItem = SkillInventoryItem & {
  body: string;
  bodyBytes: number;
  bodyTruncated: boolean;
  contentBytes: number;
};

export type SkillDetailResponse = {
  schemaVersion: 'rag-ime.skill-detail.v1';
  ok: boolean;
  revision: string;
  item: SkillDetailItem;
};

export const pluginQueryKeys = {
  root: ['plugins'] as const,
  catalogs: () => [...pluginQueryKeys.root, 'catalog'] as const,
  catalog: (sessionId = '') => [...pluginQueryKeys.root, 'catalog', sessionId] as const,
  skills: () => [...pluginQueryKeys.root, 'skills'] as const,
  skill: (skillId: string) => [...pluginQueryKeys.root, 'skills', skillId] as const,
  defaults: () => [...pluginQueryKeys.root, 'defaults'] as const,
  installed: () => [...pluginQueryKeys.root, 'installed'] as const,
  versions: () => [...pluginQueryKeys.root, 'versions'] as const,
  proposals: () => [...pluginQueryKeys.root, 'proposals'] as const,
  lifecycle: () => [...pluginQueryKeys.root, 'lifecycle'] as const,
};

export function usePluginCatalog(
  sessionId = '',
  enabled = true,
  skillsEnabled = false,
  skillId = '',
) {
  const transport = useControlTransport();
  const catalog = useQuery({
    queryKey: pluginQueryKeys.catalog(sessionId),
    queryFn: async ({ signal }) => {
      const response = await transport.request({
        pathId: 'agent.tools.list',
        ...(sessionId ? { query: { sessionId } } : {}),
        signal,
      });
      return sessionId
        ? requireSessionCapabilityCatalog(response, sessionId)
        : requireCapabilityCatalog(response);
    },
    staleTime: 30_000,
    enabled,
    refetchOnReconnect: 'always',
  });
  const defaults = useQuery({
    queryKey: pluginQueryKeys.defaults(),
    queryFn: async ({ signal }) => {
      const response = await transport.request({ pathId: 'agent.configuration.get', signal });
      const parsed = parseCapabilityDefaults(response);
      if (!parsed) throw new Error('默认能力设置版本未知，当前设置不会被猜测或修改。');
      return parsed;
    },
    staleTime: 30_000,
    enabled,
    refetchOnReconnect: 'always',
  });
  const installed = useQuery({
    queryKey: pluginQueryKeys.installed(),
    queryFn: ({ signal }) => transport.request({ pathId: 'agent.extensions.list', signal }),
    enabled,
    staleTime: 5_000,
  });
  const skills = useQuery({
    queryKey: pluginQueryKeys.skills(),
    queryFn: ({ signal }) => transport.request({ pathId: 'agent.extensions.skills.list', signal }),
    enabled: enabled && skillsEnabled,
    staleTime: 5_000,
    refetchOnReconnect: 'always',
  });
  const skill = useQuery({
    queryKey: pluginQueryKeys.skill(skillId),
    queryFn: ({ signal }) => transport.request({
      pathId: 'agent.extensions.skills.get',
      query: { skillId },
      signal,
    }),
    enabled: enabled && skillsEnabled && Boolean(skillId),
    staleTime: 5_000,
    refetchOnReconnect: 'always',
  });
  const versions = useQuery({
    queryKey: pluginQueryKeys.versions(),
    queryFn: ({ signal }) => transport.request({ pathId: 'agent.extensions.catalog', signal }),
    enabled,
    staleTime: 30_000,
  });
  const proposals = useQuery({
    queryKey: pluginQueryKeys.proposals(),
    queryFn: ({ signal }) => transport.request({ pathId: 'agent.extensions.proposals', signal }),
    enabled,
    refetchInterval: enabled ? 5_000 : false,
  });
  const queryClient = useQueryClient();
  const validate = useMutation({
    mutationFn: (body: { sourcePath?: string; packageSource?: string; catalogId?: string; catalogVersion?: string }) => transport.request({ pathId: 'agent.extensions.validate', body }),
  });
  const preview = useMutation({
    mutationFn: (body: { action: string; validationToken?: string; pluginId?: string; enable?: boolean }) => (
      transport.request({ pathId: 'agent.extensions.preview', body })
    ),
  });
  const apply = useMutation({
    mutationFn: async (body: { previewToken: string; payloadSha256: string; confirmText: string }) => {
      const response = asRecord(await transport.request({ pathId: 'agent.extensions.apply', body }));
      if (response.ok !== true || !stringValue(asRecord(response.receipt).receiptId)) {
        throw new Error('未收到有效的更改回执，结果尚未确认。');
      }
      return response;
    },
    retry: false,
    onSuccess: async () => {
      // A confirmed change must reach other surfaces even while this page's
      // inventory refresh is slow. This event never represents an attempt.
      window.dispatchEvent(new Event(PAW_EXTENSION_INSTALLATION_CHANGED_EVENT));
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: pluginQueryKeys.installed() }),
        queryClient.invalidateQueries({ queryKey: pluginQueryKeys.skills() }),
        queryClient.invalidateQueries({ queryKey: pluginQueryKeys.proposals() }),
        // Invalidate all session snapshots; only mounted observers refetch.
        queryClient.invalidateQueries({ queryKey: pluginQueryKeys.catalogs() }),
      ]);
    },
  });
  const updateDefaults = useMutation({
    mutationFn: (input: {
      expectedRevision: number;
      preferences: Record<string, CapabilityPreference>;
    }) => transport.request({
      pathId: 'agent.configuration.update',
      body: {
        expectedRevision: input.expectedRevision,
        changes: {
          'sessionDefaults.capabilityDisclosurePreferences': input.preferences,
        },
        updatedBy: 'capability-settings-ui',
      },
    }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: pluginQueryKeys.catalog(sessionId) }),
        queryClient.invalidateQueries({ queryKey: pluginQueryKeys.defaults() }),
      ]);
    },
  });
  const updateProjectDefaults = useMutation({
    mutationFn: (input: {
      expectedRevision: number;
      projectPreferences: Record<string, Record<string, CapabilityPreference>>;
    }) => transport.request({
      pathId: 'agent.configuration.update',
      body: {
        expectedRevision: input.expectedRevision,
        changes: {
          'capabilityDisclosure.projectPreferences': input.projectPreferences,
        },
        updatedBy: 'capability-settings-ui',
      },
    }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: pluginQueryKeys.catalog(sessionId) }),
        queryClient.invalidateQueries({ queryKey: pluginQueryKeys.defaults() }),
      ]);
    },
  });
  const lifecycle = useQuery({
    queryKey: pluginQueryKeys.lifecycle(),
    queryFn: ({ signal }) => transport.request({ pathId: 'agent.lifecycleHooks.get', query: { limit: 20 }, signal }),
    enabled,
    staleTime: 5_000,
  });
  const updateLifecycle = useMutation({
    mutationFn: (body: { eventType: string; enabled?: boolean; tokenLimit?: number; cooldownSeconds?: number }) => (
      transport.request({ pathId: 'agent.lifecycleHooks.update', body })
    ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: pluginQueryKeys.lifecycle() });
    },
  });
  const refreshAll = async () => {
    await Promise.all([
      catalog.refetch(),
      defaults.refetch(),
      installed.refetch(),
      versions.refetch(),
      proposals.refetch(),
      lifecycle.refetch(),
      ...(skillsEnabled ? [skills.refetch()] : []),
      ...(skillsEnabled && skillId ? [skill.refetch()] : []),
    ]);
  };
  return {
    catalog,
    defaults,
    installed,
    skills,
    skill,
    versions,
    proposals,
    lifecycle,
    validate,
    preview,
    apply,
    updateDefaults,
    updateProjectDefaults,
    updateLifecycle,
    refreshAll,
    transport,
  };
}
