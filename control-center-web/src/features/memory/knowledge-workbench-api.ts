import { useQuery } from '@tanstack/react-query';
import { useControlTransport } from '@/app/control-transport';
import type { MutationAvailability } from '@/features/overview/management-mutation';
import type { ControlTransport, JsonValue } from '@/platform/transport';

export const knowledgeQueryKeys = {
  root: ['knowledge'] as const,
  capabilities: () => [...knowledgeQueryKeys.root, 'capabilities'] as const,
};

export const knowledgeMutationPathIds = {
  databaseDraftEdit: 'knowledge.database.draft.edit',
  databaseApplyPreview: 'knowledge.database.apply.preview',
  databaseApply: 'knowledge.database.apply',
  databaseRollback: 'knowledge.database.rollback',
} as const;

export type KnowledgeMutationPathId = (typeof knowledgeMutationPathIds)[keyof typeof knowledgeMutationPathIds];

export type KnowledgeMutationRequest = {
  pathId: KnowledgeMutationPathId;
  body: Record<string, JsonValue>;
};

export function useKnowledgeMutationBoundary() {
  const transport = useControlTransport();
  const capabilities = useQuery({
    queryKey: knowledgeQueryKeys.capabilities(),
    queryFn: () => transport.capabilities(),
    staleTime: 30_000,
  });

  const databaseAvailability = (blockedReason = ''): MutationAvailability => {
    if (capabilities.isPending) return { state: 'checking' };
    if (capabilities.error) return { state: 'unsupported', reason: '无法确认当前操作是否可用，请刷新后重试。' };
    const routeIds = new Set((capabilities.data?.routeIds ?? []) as readonly string[]);
    const required: readonly KnowledgeMutationPathId[] = [
      knowledgeMutationPathIds.databaseDraftEdit,
      knowledgeMutationPathIds.databaseApplyPreview,
      knowledgeMutationPathIds.databaseApply,
      knowledgeMutationPathIds.databaseRollback,
    ];
    if (required.some((pathId) => !routeIds.has(pathId))) {
      return { state: 'unsupported', reason: '当前版本还不能安全应用记忆整理草案；没有请求被发送。' };
    }
    const flags = capabilities.data?.features ?? {};
    if (!flags.managementWorkContract || !flags.knowledgeDatabaseWorkContract) {
      return { state: 'unsupported', reason: '当前版本还不能安全应用记忆整理草案；没有请求被发送。' };
    }
    return blockedReason ? { state: 'blocked', reason: blockedReason } : { state: 'available' };
  };

  return {
    capabilities,
    databaseAvailability,
    request: <Response,>(request: KnowledgeMutationRequest) => requestKnowledgeMutation<Response>(transport, request),
  };
}

export function requestKnowledgeMutation<Response>(
  transport: ControlTransport,
  request: KnowledgeMutationRequest,
): Promise<Response> {
  return transport.request<Response>({
    pathId: request.pathId,
    body: request.body,
  });
}
