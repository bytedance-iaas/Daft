import { QueryClient } from '@tanstack/react-query';
import { ApiError } from '../api/errors';

/** Retry only what can succeed on its own: network blips and 5xx, never a 4xx answer. */
function shouldRetry(failureCount: number, error: unknown): boolean {
  if (failureCount >= 2) return false;
  if (error instanceof ApiError) return error.code === 'network' || error.status >= 500;
  return false;
}

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: shouldRetry,
        staleTime: 3_000,
        refetchOnWindowFocus: false,
      },
      mutations: { retry: false },
    },
  });
}
