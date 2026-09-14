import { useQuery } from '@tanstack/react-query';
import { useControlTransport } from '@/app/control-transport';
import { ScenarioSkillSettings } from '@/features/configuration/ScenarioSkillSettings';
import { ScenarioAgentPolicySettings } from '@/features/configuration/ScenarioAgentPolicySettings';
import { usePawOsAppActive } from '@/features/paw-os/surface-context';
import { usePageVisibility } from '@/platform/use-page-visibility';
import { QueryState } from '@/features/overview/management-ui';

export function PluginScenes() {
  const transport = useControlTransport();
  const surfaceActive = usePawOsAppActive();
  const visible = usePageVisibility();
  const active = (surfaceActive ?? true) && visible;
  const capabilities = useQuery({
    queryKey: ['configuration', 'capabilities'],
    queryFn: () => transport.capabilities(),
    enabled: active,
    staleTime: 30_000,
    retry: false,
  });
  return <div className="plugin-scenes">
    <QueryState error={capabilities.error} isPending={capabilities.isPending} onRetry={() => void capabilities.refetch()}>
      <ScenarioAgentPolicySettings active={active} routeIds={capabilities.data?.routeIds ?? []} transport={transport} />
      <ScenarioSkillSettings active={active} routeIds={capabilities.data?.routeIds ?? []} transport={transport} />
    </QueryState>
  </div>;
}
