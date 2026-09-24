import { Pagination } from '@arco-design/web-react';
import { useEffect, useState, type ReactNode } from 'react';
import { ADJ_CONFIG } from './useAdjudication';

/**
 * Fetches every cursor page of a queue in the background: the page shows the cards ten at a time
 * with a pager (requester, third round), so it needs the whole list, not the next scroll's worth.
 */
export function useLoadAll(q: { hasNextPage: boolean; isFetchingNextPage: boolean; isError: boolean; fetchNextPage: () => unknown; data?: { pages: readonly unknown[] } }): void {
  const { hasNextPage, isFetchingNextPage, isError, fetchNextPage } = q;
  // The page count too: a page that arrives before the observer reports the fetch leaves the flags as they were.
  const loaded = q.data?.pages.length ?? 0;
  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage && !isError) void fetchNextPage();
  }, [hasNextPage, isFetchingNextPage, isError, fetchNextPage, loaded]);
}

/** One page of `items` and its pager; a new `reset` (other filters, another tab) goes back to page 1. */
export function usePaged<T>(items: readonly T[], reset: string, testId: string): { slice: readonly T[]; pager: ReactNode } {
  const [state, setState] = useState({ reset, page: 1, size: ADJ_CONFIG.cardsPerPage });
  // Remember the new key, or going back to the old filters would land on the old page again.
  if (state.reset !== reset) setState({ reset, page: 1, size: state.size });
  const page = state.reset === reset ? state.page : 1;
  const pages = Math.max(1, Math.ceil(items.length / state.size));
  const current = Math.min(page, pages);
  const slice = items.slice((current - 1) * state.size, current * state.size);
  const pager =
    items.length > Math.min(...ADJ_CONFIG.pageSizes, state.size) ? (
      <div className="adj-pager" data-testid={testId}>
        <Pagination
          size="small"
          current={current}
          pageSize={state.size}
          total={items.length}
          sizeCanChange
          sizeOptions={ADJ_CONFIG.pageSizes}
          showTotal
          onChange={(p: number, s: number) => setState({ reset, page: s === state.size ? p : 1, size: s })}
        />
      </div>
    ) : null;
  return { slice, pager };
}
