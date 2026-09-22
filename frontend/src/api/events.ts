// Live task updates (03 §5): EventSource on {base}/events/tasks/{id}; the browser reconnects with
// Last-Event-ID on its own. Whenever the stream is not open (never opened, dropped, or EventSource
// is missing) the page polls every 5 s instead (F3.2 ②) — callers read `mode` to decide.
import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { taskEventsUrl } from '../base';
import { qk } from './queries';
import type { LogLine, SseDone, SseLog, SseProgress, SseState, SseUsage, Task } from './types';

export type EventMode = 'off' | 'connecting' | 'sse' | 'polling';

/** Tunables (tests shorten them). */
export const EVENTS_CONFIG = { pollMs: 5000, retrySseMs: 30_000 };

type Factory = (url: string) => EventSource;
let factory: Factory | null = null;

/** Tests inject a fake EventSource; null restores the browser's. */
export function setEventSourceFactory(f: Factory | null): void {
  factory = f;
}

function makeSource(url: string): EventSource | null {
  if (factory) return factory(url);
  if (typeof EventSource === 'undefined') return null;
  return new EventSource(url, { withCredentials: true });
}

/** A live log line (SSE `log`) in the shape of the /logs lines, for the log tab. */
export interface LiveLog extends LogLine {
  live: true;
}

export const liveLogsKey = (id: string) => ['task', id, 'live-logs'] as const;

function applyEvent(qc: QueryClient, id: string, type: string, raw: string, refetchSoon: () => void): void {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return;
  }
  const key = qk.task(id);
  switch (type) {
    case 'state': {
      const d = data as SseState;
      qc.setQueryData<Task>(key, (t) => (t ? { ...t, state: d.state, pause_reason: d.pause_reason ?? null } : t));
      refetchSoon();
      break;
    }
    case 'progress': {
      const d = data as SseProgress;
      let changedState = false;
      qc.setQueryData<Task>(key, (t) => {
        if (!t) return t;
        const stages = [...t.progress.stages];
        const i = stages.findIndex((s) => s.id === d.id);
        if (i === -1) stages.push(d);
        else {
          changedState = stages[i].state !== d.state;
          stages[i] = { ...stages[i], ...d };
        }
        return { ...t, progress: { ...t.progress, stages } };
      });
      if (changedState) refetchSoon();
      break;
    }
    case 'usage':
      qc.setQueryData<Task>(key, (t) => (t ? { ...t, usage: data as SseUsage } : t));
      break;
    case 'log': {
      const d = data as SseLog;
      const line: LiveLog = { ts: Date.now(), stage: d.stage, level: d.level, msg: d.msg, subtask_id: null, episode_index: null, live: true };
      qc.setQueryData<LiveLog[]>(liveLogsKey(id), (arr) => [...(arr ?? []), line].slice(-1000));
      break;
    }
    case 'done': {
      const d = data as SseDone;
      qc.setQueryData<Task>(key, (t) => (t ? { ...t, state: d.state } : t));
      void qc.invalidateQueries({ queryKey: ['task', id] });
      void qc.invalidateQueries({ queryKey: qk.tasksAll });
      break;
    }
    case 'reset':
      // The server lost our place (restart or buffer overflow): drop increments, reload the snapshot.
      qc.setQueryData<LiveLog[]>(liveLogsKey(id), []);
      void qc.invalidateQueries({ queryKey: key });
      break;
    default:
      break;
  }
}

/**
 * Subscribes to a task's events while `enabled`. Returns the transport in use: 'sse' while the
 * stream is open, 'connecting' / 'polling' otherwise (the caller polls every EVENTS_CONFIG.pollMs).
 */
export function useTaskEvents(taskId: string | undefined, enabled: boolean): EventMode {
  const qc = useQueryClient();
  const [mode, setMode] = useState<EventMode>('off');
  const lastRefetch = useRef(0);
  useEffect(() => {
    if (!taskId || !enabled) {
      setMode('off');
      return undefined;
    }
    let es: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let disposed = false;
    const refetchSoon = () => {
      const now = Date.now();
      if (now - lastRefetch.current < 2000) return;
      lastRefetch.current = now;
      void qc.invalidateQueries({ queryKey: qk.task(taskId) });
    };
    const connect = () => {
      if (disposed) return;
      es = makeSource(taskEventsUrl(taskId));
      if (!es) {
        setMode('polling');
        return;
      }
      setMode('connecting');
      const source = es;
      source.onopen = () => !disposed && setMode('sse');
      source.onerror = () => {
        if (disposed) return;
        setMode('polling');
        // CLOSED means the browser gave up (e.g. an HTTP error): try the stream again later.
        if (source.readyState === 2) {
          source.close();
          retry = setTimeout(connect, EVENTS_CONFIG.retrySseMs);
        }
      };
      for (const type of ['state', 'progress', 'usage', 'log', 'done', 'reset']) {
        source.addEventListener(type, (e) => applyEvent(qc, taskId, type, (e as MessageEvent<string>).data, refetchSoon));
      }
    };
    connect();
    return () => {
      disposed = true;
      if (retry) clearTimeout(retry);
      es?.close();
    };
  }, [taskId, enabled, qc]);
  return mode;
}

/** The refetch interval a live page should use for its task query. */
export function pollInterval(mode: EventMode, live: boolean): number | false {
  if (!live) return false;
  return mode === 'sse' ? false : EVENTS_CONFIG.pollMs;
}
