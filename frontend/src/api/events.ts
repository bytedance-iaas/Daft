// Live task updates (03 §5): EventSource on {base}/events/tasks/{id}; the browser reconnects with
// Last-Event-ID on its own. Whenever the stream is not open (never opened, dropped, or EventSource
// is missing) the page polls every 5 s instead (F3.2 ②) — callers read `mode` to decide.
import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { taskEventsUrl } from '../base';
import { qk } from './queries';
import { isTerminal, type LogLine, type SseDone, type SseLog, type SseProgress, type SseState, type SseUsage, type StageProgress, type Subtask, type Task } from './types';

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

/** A stage's cumulative update merged into a stage list; says whether its state changed. */
function mergeStage(stages: readonly StageProgress[], d: StageProgress): { stages: StageProgress[]; changed: boolean } {
  const out = [...stages];
  const i = out.findIndex((s) => s.id === d.id);
  if (i === -1) {
    out.push(d);
    return { stages: out, changed: true };
  }
  const changed = out[i].state !== d.state;
  out[i] = { ...out[i], ...d };
  return { stages: out, changed };
}

function stagesOf(progress: unknown): StageProgress[] {
  const stages = progress && typeof progress === 'object' ? (progress as { stages?: unknown }).stages : undefined;
  return Array.isArray(stages) ? (stages as StageProgress[]) : [];
}

function withStage(sub: Subtask, d: StageProgress): { sub: Subtask; changed: boolean } {
  const { stages, changed } = mergeStage(stagesOf(sub.progress), d);
  const doc = sub.progress && typeof sub.progress === 'object' ? sub.progress : {};
  return { sub: { ...sub, progress: { ...doc, stages } }, changed };
}

/** A subtask as the subtasks list of the detail page holds it. */
function patchListedSubtask(qc: QueryClient, taskId: string, subId: string, patch: (s: Subtask) => Subtask): void {
  qc.setQueryData<{ items: Subtask[] }>(qk.subtasks(taskId), (l) => (l?.items ? { ...l, items: l.items.map((s) => (s.id === subId ? patch(s) : s)) } : l));
}

/**
 * A `state` or `done` event that carries `subtask_id` is the subtask's (C4 SseState): it moves
 * the task's active_subtask, never the task's own state (01 §3: the parent does not go back to
 * running). A final state ends it; one the cache does not know yet reloads the task.
 */
function applySubtaskState(qc: QueryClient, taskId: string, subId: string, next: Pick<Subtask, 'state'> & Partial<Pick<Subtask, 'pause_reason' | 'state_reason'>>): void {
  const done = isTerminal(next.state);
  let known = false;
  qc.setQueryData<Task>(qk.task(taskId), (t) => {
    if (!t || t.active_subtask?.id !== subId) return t;
    known = true;
    return { ...t, active_subtask: done ? null : { ...t.active_subtask, ...next } };
  });
  patchListedSubtask(qc, taskId, subId, (s) => ({ ...s, ...next }));
  if (!known && !done) void qc.invalidateQueries({ queryKey: qk.task(taskId) });
}

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
      if (d.subtask_id) applySubtaskState(qc, id, d.subtask_id, { state: d.state, pause_reason: d.pause_reason ?? null, ...(d.reason !== undefined ? { state_reason: d.reason } : {}) });
      else qc.setQueryData<Task>(key, (t) => (t ? { ...t, state: d.state, pause_reason: d.pause_reason ?? null } : t));
      refetchSoon();
      break;
    }
    case 'progress': {
      // While a subtask runs, the stages reported are its own (the Daemon writes them to the
      // subtask's progress): they must not overwrite the main run's.
      const d = data as SseProgress;
      let changedState = false;
      let sub: Subtask | null = null;
      qc.setQueryData<Task>(key, (t) => {
        if (!t) return t;
        const active = t.active_subtask && !isTerminal(t.active_subtask.state) ? t.active_subtask : null;
        if (active) {
          const r = withStage(active, d);
          changedState = r.changed;
          sub = r.sub;
          return { ...t, active_subtask: r.sub };
        }
        const r = mergeStage(t.progress.stages, d);
        changedState = r.changed;
        return { ...t, progress: { ...t.progress, stages: r.stages } };
      });
      const s = sub as Subtask | null;
      if (s) patchListedSubtask(qc, id, s.id, (x) => withStage(x, d).sub);
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
      // A subtask's end: the task's recomputed state came as a `state` event before it (D25).
      const d = data as SseDone;
      if (d.subtask_id) applySubtaskState(qc, id, d.subtask_id, { state: d.state, ...(d.reason !== undefined ? { state_reason: d.reason } : {}) });
      else qc.setQueryData<Task>(key, (t) => (t ? { ...t, state: d.state } : t));
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
