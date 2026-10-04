// What the player reads (C4 2.4.0, design doc 18 §7): the presentation model, one episode, a curve
// group, a frame pack's index and the transcode state of a camera - for a registered dataset
// (the full page) or a task's frozen input (the mini player). Everything is fetched from the Daemon.
import { useQuery, type QueryClient } from '@tanstack/react-query';
import { api, unwrap } from '../../api/client';
import { ApiError } from '../../api/errors';
import type { VizDataset, VizEpisode, VizFrameIndex, VizMediaPending, VizSeries } from '../../api/types';

/** Which reader input the player shows: a dataset's registration or a task's frozen input. */
export interface VizRef {
  scope: 'dataset' | 'task';
  id: string;
}

export const vizKeys = {
  all: ['viz'] as const,
  model: (r: VizRef) => ['viz', r.scope, r.id, 'model'] as const,
  episode: (r: VizRef, index: number) => ['viz', r.scope, r.id, 'episode', index] as const,
  series: (r: VizRef, index: number, stream: string, points: number) => ['viz', r.scope, r.id, 'series', index, stream, points] as const,
  frames: (url: string) => ['viz', 'frames', url] as const,
};

export async function fetchVizModel(r: VizRef): Promise<VizDataset> {
  const path = { path: { id: r.id } };
  return r.scope === 'dataset'
    ? unwrap(api().GET('/datasets/{id}/viz', { params: path }))
    : unwrap(api().GET('/tasks/{id}/viz', { params: path }));
}

export async function fetchVizEpisode(r: VizRef, index: number): Promise<VizEpisode> {
  const params = { path: { id: r.id, index } };
  return r.scope === 'dataset'
    ? unwrap(api().GET('/datasets/{id}/episodes/{index}/viz', { params }))
    : unwrap(api().GET('/tasks/{id}/episodes/{index}/viz', { params }));
}

export async function fetchVizSeries(r: VizRef, index: number, stream: string, points: number): Promise<VizSeries> {
  const params = { path: { id: r.id, index }, query: { stream, points } };
  return r.scope === 'dataset'
    ? unwrap(api().GET('/datasets/{id}/episodes/{index}/series', { params }))
    : unwrap(api().GET('/tasks/{id}/episodes/{index}/series', { params }));
}

/** The presentation model; it changes only with the registration (or never, for a task). */
export function useVizModel(r: VizRef | null) {
  return useQuery({
    queryKey: r ? vizKeys.model(r) : ['viz', 'none'],
    queryFn: () => fetchVizModel(r as VizRef),
    enabled: !!r,
    staleTime: 5 * 60_000,
    retry: (n, e) => n < 1 && !(e instanceof ApiError && e.status >= 400 && e.status < 500),
  });
}

/**
 * One episode. Presigned camera URLs expire (`expires_at`): the answer is fetched again two minutes
 * before the first one does, and the cells swap to the new URL where they are (design doc 18 §5.8).
 */
/**
 * Asks for an episode the viewer will likely open next, so the Daemon prepares it now: an mcap episode is
 * scanned once (the whole file) into its disk cache, and the answer waits here for the click.
 */
export function prefetchVizEpisode(qc: QueryClient, r: VizRef, index: number): Promise<void> {
  return qc.prefetchQuery({ queryKey: vizKeys.episode(r, index), queryFn: () => fetchVizEpisode(r, index), staleTime: Infinity });
}

export function useVizEpisode(r: VizRef | null, index: number | null) {
  return useQuery({
    queryKey: r && index !== null ? vizKeys.episode(r, index) : ['viz', 'none', 'episode'],
    queryFn: () => fetchVizEpisode(r as VizRef, index as number),
    enabled: !!r && index !== null,
    staleTime: Infinity,
    refetchInterval: (q) => {
      const ep = q.state.data;
      const exp = ep?.cameras.map((c) => c.expires_at).filter((v): v is number => typeof v === 'number') ?? [];
      if (!exp.length) return false;
      return Math.max(10_000, Math.min(...exp) - Date.now() - 120_000);
    },
    retry: (n, e) => n < 1 && !(e instanceof ApiError && e.status >= 400 && e.status < 500),
  });
}

export function useVizSeries(r: VizRef, index: number, stream: string, points: number, enabled = true) {
  return useQuery({
    queryKey: vizKeys.series(r, index, stream, points),
    queryFn: () => fetchVizSeries(r, index, stream, points),
    enabled,
    staleTime: Infinity,
    placeholderData: (prev) => (prev && prev.stream === stream ? prev : undefined),
  });
}

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, { headers: { Accept: 'application/json' }, signal });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    throw ApiError.network(cause);
  }
  const body = await res.json().catch(() => undefined);
  if (!res.ok) throw ApiError.fromResponse(res.status, body);
  return body as T;
}

/** A frame pack's index (`index_url` of a frames camera). */
export function useFrameIndex(url: string | null) {
  return useQuery({
    queryKey: url ? vizKeys.frames(url) : ['viz', 'frames', 'none'],
    queryFn: ({ signal }) => getJson<VizFrameIndex>(url as string, signal),
    enabled: !!url,
    staleTime: Infinity,
  });
}

export type MediaState = { state: 'ready' } | { state: 'pending'; progress: number | null; message: string } | { state: 'failed'; message: string };

/**
 * Whether a Daemon video URL can be played now: a transcode answers 202 with its progress until the
 * copy is done (design doc 18 §4.2). One byte is asked for, so a ready file costs nothing.
 */
export async function probeMedia(url: string, signal?: AbortSignal): Promise<MediaState> {
  let res: Response;
  try {
    res = await fetch(url, { headers: { Range: 'bytes=0-0' }, signal });
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
    return { state: 'failed', message: ApiError.network(cause).message };
  }
  if (res.status === 202) {
    const body = (await res.json().catch(() => ({}))) as Partial<VizMediaPending>;
    if (body.state === 'failed') return { state: 'failed', message: body.message ?? '' };
    return { state: 'pending', progress: typeof body.progress === 'number' ? body.progress : null, message: body.message ?? '' };
  }
  if (res.ok) {
    void res.body?.cancel().catch(() => undefined);
    return { state: 'ready' };
  }
  const body = await res.json().catch(() => undefined);
  return { state: 'failed', message: ApiError.fromResponse(res.status, body).message };
}
