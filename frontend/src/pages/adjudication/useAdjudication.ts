import { Message } from '@arco-design/web-react';
import { useInfiniteQuery } from '@tanstack/react-query';
import { useCallback, useState } from 'react';
import { api, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { qk } from '../../api/queries';
import type { AdjudicationCounts, AdjudicationLine, AdjudicationPage, DecisionValue } from '../../api/types';
import { clicked, decisionKey, type LocalDecisions } from '../../lib/adjudication';

/**
 * `pageSize`: cards per request of the cursor-paged queue. The page fetches the whole queue in
 * the background (the server rebuilds it for every request, so the contract's maximum);
 * `cardsPerPage` / `pageSizes`: how many cards one page of the pager shows (default 10).
 */
export const ADJ_CONFIG = { pageSize: 200, cardsPerPage: 10, pageSizes: [10, 20, 50] };

export type AdjTab = 'review' | 'appeals';
export type AdjStatus = 'pending' | 'decided' | 'unapplied' | 'all';

export function useAdjudicationList(taskId: string, q: { tab: AdjTab; status: AdjStatus; source: string | null }, enabled = true) {
  return useInfiniteQuery({
    queryKey: qk.adjudication(taskId, q),
    queryFn: ({ pageParam }): Promise<AdjudicationPage> =>
      unwrap(
        api().GET('/tasks/{id}/adjudication', {
          params: {
            path: { id: taskId },
            query: { tab: q.tab, status: q.status, limit: ADJ_CONFIG.pageSize, ...(q.source ? { source: q.source } : {}), ...(pageParam ? { cursor: pageParam } : {}) },
          },
        }),
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => (last.has_more && last.next_cursor ? last.next_cursor : undefined),
    enabled,
  });
}

/**
 * Decisions are saved one click at a time (append only, the last one wins) and nothing is
 * executed until 执行裁决 (D10). The page shows its own clicks on top of the loaded list.
 */
export function useDecisions(taskId: string) {
  const [local, setLocal] = useState<LocalDecisions>({});
  const [counts, setCounts] = useState<AdjudicationCounts | null>(null);
  const [saving, setSaving] = useState<Record<string, boolean>>({});
  const save = useCallback(
    async (ep: number, line: AdjudicationLine, decision: DecisionValue, newLabel: string | null = null): Promise<boolean> => {
      const key = decisionKey(ep, line);
      // Numbered in click order: a follow-up's answer stands only when newer than the one that opened it.
      const entry = clicked(decision, newLabel);
      let previous: LocalDecisions[string] | undefined;
      setLocal((cur) => {
        previous = cur[key];
        return { ...cur, [key]: entry };
      });
      setSaving((s) => ({ ...s, [key]: true }));
      try {
        const c = await unwrap(
          api().POST('/tasks/{id}/adjudication', {
            params: { path: { id: taskId } },
            body: { decisions: [{ episode_index: ep, line, decision, ...(newLabel ? { new_label: newLabel } : {}) }] },
          }),
        );
        setCounts(c);
        return true;
      } catch (e) {
        setLocal((cur) => {
          const next = { ...cur };
          if (previous) next[key] = previous;
          else delete next[key];
          return next;
        });
        Message.error(errorMessage(e));
        return false;
      } finally {
        setSaving((s) => ({ ...s, [key]: false }));
      }
    },
    [taskId],
  );
  const reset = useCallback(() => {
    setLocal({});
    setCounts(null);
  }, []);
  return { local, counts, saving, save, reset };
}
