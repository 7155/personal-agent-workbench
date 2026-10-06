import { useQuery } from '@tanstack/react-query';
import { useControlTransport } from '@/app/control-transport';
import type { MutationAvailability } from '@/features/overview/management-mutation';
import { usePawOsAppActive } from '@/features/paw-os/surface-context';
import type { ControlTransport, JsonValue } from '@/platform/transport';

export const configurationQueryKeys = {
  root: ['configuration'] as const,
  settings: () => [...configurationQueryKeys.root, 'settings'] as const,
  schema: () => [...configurationQueryKeys.root, 'schema'] as const,
  capabilities: () => [...configurationQueryKeys.root, 'capabilities'] as const,
  models: () => [...configurationQueryKeys.root, 'pi-models'] as const,
  providers: () => [...configurationQueryKeys.root, 'providers'] as const,
  oauth: (loginId: string) => [...configurationQueryKeys.root, 'provider-oauth', loginId] as const,
};

export const configurationMutationPathIds = {
  preview: 'configuration.settings.preview',
  apply: 'configuration.settings.apply',
  rollback: 'configuration.settings.rollback',
} as const;

export type ConfigurationMutationPathId =
  (typeof configurationMutationPathIds)[keyof typeof configurationMutationPathIds];

export type ConfigurationMutationRequest = {
  pathId: ConfigurationMutationPathId;
  body: Record<string, JsonValue>;
};

export function useConfigurationQueries() {
  const transport = useControlTransport();
  const active = usePawOsAppActive() ?? true;
  const settings = useQuery({
    enabled: active,
    queryKey: configurationQueryKeys.settings(),
    queryFn: ({ signal }) => transport.request({ pathId: 'configuration.settings', signal }),
  });
  const schema = useQuery({
    enabled: active,
    queryKey: configurationQueryKeys.schema(),
    queryFn: ({ signal }) => transport.request({ pathId: 'configuration.schema', signal }),
    staleTime: 60_000,
  });
  const capabilities = useQuery({
    enabled: active,
    queryKey: configurationQueryKeys.capabilities(),
    queryFn: () => transport.capabilities(),
    staleTime: Infinity,
  });
  const modelCatalogSupported = Boolean(
    capabilities.data?.routeIds?.includes('agent.role.models'),
  );
  const modelCatalog = useQuery({
    queryKey: configurationQueryKeys.models(),
    queryFn: ({ signal }) => transport.request({ pathId: 'agent.role.models', signal }),
    enabled: active && modelCatalogSupported,
    staleTime: 0,
  });
  return {
    capabilities,
    modelCatalog,
    modelCatalogSupported,
    schema,
    settings,
    transport,
    transportKind: transport.kind,
  };
}

export function usePiProviderCatalog() {
  const transport = useControlTransport();
  const active = usePawOsAppActive() ?? true;
  const capabilities = useQuery({
    enabled: active,
    queryKey: configurationQueryKeys.capabilities(),
    queryFn: () => transport.capabilities(),
    staleTime: 30_000,
  });
  const routeIds = new Set(capabilities.data?.routeIds ?? []);
  const supported = Boolean(
    capabilities.data?.features.piProviderCredentials
    && routeIds.has('agent.providers.get'),
  );
  const authChangesSupported = Boolean(
    supported
    && routeIds.has('agent.provider.auth.preview')
    && routeIds.has('agent.provider.auth.apply'),
  );
  const oauthStatusSupported = Boolean(supported && routeIds.has('agent.provider.oauth.status'));
  const oauthCancelSupported = Boolean(supported && routeIds.has('agent.provider.oauth.cancel'));
  const catalog = useQuery({
    queryKey: configurationQueryKeys.providers(),
    queryFn: ({ signal }) => transport.request({ pathId: 'agent.providers.get', signal }),
    enabled: active && supported,
  });
  return {
    authChangesSupported,
    capabilities,
    catalog,
    oauthCancelSupported,
    oauthStatusSupported,
    supported,
    transport,
  };
}

export function useConfigurationMutationBoundary() {
  const transport = useControlTransport();
  const active = usePawOsAppActive() ?? true;
  const capabilities = useQuery({
    enabled: active,
    queryKey: configurationQueryKeys.capabilities(),
    queryFn: () => transport.capabilities(),
    staleTime: 30_000,
  });

  const availability = (blockedReason = ''): MutationAvailability => {
    if (capabilities.isPending) return { state: 'checking' };
    if (capabilities.error) {
      return {
        state: 'blocked',
        reason: '无法确认本机是否支持安全保存，请刷新后重试。',
      };
    }
    const flags = capabilities.data?.features ?? {};
    const routeIds = new Set((capabilities.data?.routeIds ?? []) as readonly string[]);
    const required = Object.values(configurationMutationPathIds);
    if (
      !flags.managementWorkContract
      || !flags.configurationSettingsWorkContract
      || required.some((pathId) => !routeIds.has(pathId))
    ) {
      return {
        state: 'blocked',
        reason: '当前版本还不能安全应用设置，本次修改不会发送。',
      };
    }
    if (blockedReason) return { state: 'blocked', reason: blockedReason };
    return { state: 'available' };
  };

  return {
    availability,
    capabilities,
    request: <Response,>(request: ConfigurationMutationRequest) => (
      requestConfigurationMutation<Response>(transport, request)
    ),
  };
}

export function requestConfigurationMutation<Response>(
  transport: ControlTransport,
  request: ConfigurationMutationRequest,
): Promise<Response> {
  if (containsSecretConfigurationChange(request.body.changes)) {
    throw new Error('秘密设置必须通过专用安全流程修改；本次请求未发送。');
  }
  return transport.request<Response>({ pathId: request.pathId, body: request.body });
}

export function isSecretConfigurationKey(key: string): boolean {
  return key
    .split(/[.\[\]]/)
    .filter(Boolean)
    .some((segment) => /^(?:api[-_]?key|secret|password|authorization|cookie|token|access[-_]?token|refresh[-_]?token|auth[-_]?token|bearer[-_]?token)$/i.test(segment));
}

function containsSecretConfigurationChange(value: JsonValue | undefined): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  return Object.entries(value).some(([key, child]) => (
    isSecretConfigurationKey(key) || containsSecretConfigurationChange(child)
  ));
}
