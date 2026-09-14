import { QueryClient } from '@tanstack/react-query';

type QueryStatusProjection = { state: { status: string } };

export function transientControlErrorRefetchInterval(active: boolean) {
  return (query: QueryStatusProjection): number | false => (
    active && query.state.status === 'error' ? 4_000 : false
  );
}

const queryClientOptions = {
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 15_000,
    },
    mutations: {
      retry: false,
    },
  },
} as const;

/**
 * Team workspaces must never reuse the cache of another account or space.
 * Keep the local singleton for the existing product path, while allowing the
 * team shell to create a disposable client for each scoped OS mount.
 */
export function createQueryClient(): QueryClient {
  return new QueryClient(queryClientOptions);
}

export const queryClient = createQueryClient();
