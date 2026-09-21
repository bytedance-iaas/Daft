// Query keys and the small read hooks shared by several pages. Everything is fetched from the
// Daemon (D17); nothing here is kept beyond the query cache.
import { useQuery } from '@tanstack/react-query';
import { api, unwrap } from './client';
import type { ModuleRegistry, ModuleSpec } from './types';

export const qk = {
  modules: ['modules'] as const,
  overview: ['overview'] as const,
  credentials: ['credentials'] as const,
  backends: ['vlm-backends'] as const,
  datasets: (params: Record<string, unknown>) => ['datasets', params] as const,
  datasetsAll: ['datasets'] as const,
  dataset: (id: string) => ['dataset', id] as const,
  tasks: (params: Record<string, unknown>) => ['tasks', params] as const,
  tasksAll: ['tasks'] as const,
  task: (id: string) => ['task', id] as const,
  subtasks: (id: string) => ['task', id, 'subtasks'] as const,
  timeline: (id: string) => ['task', id, 'timeline'] as const,
  plan: (id: string) => ['task', id, 'plan'] as const,
  usage: (id: string) => ['task', id, 'usage'] as const,
  logs: (id: string, params: Record<string, unknown>) => ['task', id, 'logs', params] as const,
  report: (id: string, rev: number | null) => ['task', id, 'report', rev] as const,
  reportTable: (id: string, table: string, rev: number | null, sort: string, order: string) => ['task', id, 'table', table, rev, sort, order] as const,
  episode: (id: string, ep: number, rev: number | null) => ['task', id, 'episode', ep, rev] as const,
  perf: (id: string, rev: number | null, scope: string, subtask: string | null) => ['task', id, 'perf', rev, scope, subtask] as const,
  adjudication: (id: string, params: Record<string, unknown>) => ['task', id, 'adjudication', params] as const,
  adjudicationAll: (id: string) => ['task', id, 'adjudication'] as const,
  publicCatalog: ['datasets-browse', 'public'] as const,
};

/** The module registry (C1). It only changes with a deployment, so it is fetched once. */
export function useModules() {
  return useQuery({
    queryKey: qk.modules,
    queryFn: () => unwrap(api().GET('/modules')),
    staleTime: Infinity,
  });
}

export function moduleById(reg: ModuleRegistry | undefined, id: string): ModuleSpec | undefined {
  return reg?.modules.find((m) => m.id === id);
}

/** A module's Chinese name, straight from the registry (never hard-coded). */
export function moduleName(reg: ModuleRegistry | undefined, id: string): string {
  return moduleById(reg, id)?.name_zh ?? id;
}

export function useCredentials() {
  return useQuery({ queryKey: qk.credentials, queryFn: () => unwrap(api().GET('/credentials')) });
}

export function useBackends() {
  return useQuery({ queryKey: qk.backends, queryFn: () => unwrap(api().GET('/vlm-backends')) });
}

export function useTask(id: string | undefined, refetchInterval?: number | false) {
  return useQuery({
    queryKey: qk.task(id ?? ''),
    queryFn: () => unwrap(api().GET('/tasks/{id}', { params: { path: { id: id! } } })),
    enabled: Boolean(id),
    refetchInterval,
  });
}

/**
 * Whether the HuggingFace cache bucket exists on this site. C4 has no site-config endpoint, so
 * a successful public browse is the signal (see README «契约缺口»).
 */
export function usePublicCatalog() {
  return useQuery({
    queryKey: qk.publicCatalog,
    queryFn: async () => {
      try {
        const page = await unwrap(api().GET('/datasets/browse', { params: { query: { source: 'public', limit: 200 } } }));
        return page.items ?? [];
      } catch {
        return null;
      }
    },
    staleTime: 5 * 60_000,
  });
}
