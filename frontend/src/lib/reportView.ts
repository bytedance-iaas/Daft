// Pure helpers for the report page (07 §5): field labels, value formatting, revision options,
// subtask names. Report contents are free-form objects per module (C2), so everything here
// degrades to the raw key or a compact JSON rendering instead of guessing.
import type { ModuleRegistry, ResultRecord, Subtask, TimelineEntry, UsageRow } from '../api/types';
import { zh } from '../locales/zh';
import { shortTime } from './format';
import { formatScalar } from './summary';

/** A readable label for a column, a summary key or an integrity key; the raw key otherwise. */
export function fieldLabel(key: string): string {
  return zh.report.columns[key] ?? zh.summaryKeys[key] ?? zh.report.integrityKeys[key] ?? key;
}

/** One cell or reading: numbers trimmed, null as a dash, lists joined, objects as compact JSON. */
export function formatValue(v: unknown): string {
  if (v === null || v === undefined || v === '') return '—';
  if (typeof v === 'number' || typeof v === 'boolean' || typeof v === 'string') return formatScalar(v);
  if (Array.isArray(v)) {
    if (!v.length) return zh.report.none;
    return v.every((x) => x === null || ['string', 'number', 'boolean'].includes(typeof x)) ? v.map((x) => formatValue(x)).join('、') : JSON.stringify(v);
  }
  return JSON.stringify(v);
}

/** Integrity values: a few shapes the report writer is known to use, generic otherwise. */
export function integrityValue(key: string, v: unknown): string {
  if (v === null || v === undefined) return zh.report.notRead;
  if (key === 'labels' && v && typeof v === 'object' && !Array.isArray(v)) {
    const o = v as Record<string, unknown>;
    if (typeof o.with_task === 'number' && typeof o.without_task === 'number') return zh.report.labelsValue(o.with_task, o.without_task);
  }
  if (key === 'fps' && typeof v === 'number') return `${v} fps`;
  if (v && typeof v === 'object' && !Array.isArray(v)) {
    return Object.entries(v as Record<string, unknown>)
      .map(([k, x]) => `${fieldLabel(k)} ${formatValue(x)}`)
      .join(' · ');
  }
  return formatValue(v);
}

/** A result record's module-specific readings as one line. */
export function detailsDigest(details: Record<string, unknown> | null | undefined, max = 6): string {
  const parts = Object.entries(details ?? {})
    .slice(0, max)
    .map(([k, v]) => `${fieldLabel(k)} ${formatValue(v)}`);
  return parts.join(' · ') || '—';
}

/** Where an execution error happened: «arbitration：timeout 60s（3 次）». */
export function recordError(e: ResultRecord['error']): string {
  if (!e || typeof e !== 'object') return '';
  const incidents = (e as { incidents?: { step: string; cause?: string; attempts?: number; camera?: string }[] }).incidents ?? [];
  return incidents
    .map((i) => `${i.step}${i.camera ? `（${i.camera}）` : ''}${i.cause ? `：${i.cause}` : ''}${i.attempts ? zh.report.attempts(i.attempts) : ''}`)
    .join('；');
}

/** A reason or review item of an episode view ({module, text} by convention, free-form in C4). */
export function reasonLine(item: Record<string, unknown>, registry: ModuleRegistry | undefined): string {
  const module = typeof item.module === 'string' ? registry?.modules.find((m) => m.id === item.module)?.name_zh ?? item.module : '';
  const text = typeof item.text === 'string' ? item.text : typeof item.reason === 'string' ? item.reason : '';
  if (!module && !text) return JSON.stringify(item);
  return [module, text].filter(Boolean).join(' · ');
}

/** 「重试 #1」: the kind plus its ordinal among subtasks of the same kind (oldest first). */
export function subtaskName(subtasks: readonly Subtask[], id: string | null | undefined): string {
  if (!id) return zh.taskDetail.mainRun;
  const i = subtasks.findIndex((s) => s.id === id);
  if (i < 0) return id;
  const s = subtasks[i];
  const n = subtasks.filter((x, j) => x.kind === s.kind && j <= i).length;
  return `${zh.taskDetail.subtaskKind[s.kind] ?? s.kind} #${n}`;
}

export interface RevisionOption {
  value: number;
  label: string;
}

/** Revisions for the selector, newest first: from the timeline, else 1..current. */
export function revisionOptions(entries: readonly TimelineEntry[], current: number, subtasks: readonly Subtask[] = []): RevisionOption[] {
  const seen = new Map<number, TimelineEntry>();
  for (const e of entries) if (e.kind === 'revision' && e.revision) seen.set(e.revision, e);
  const revs = new Set<number>([...seen.keys()]);
  for (let r = 1; r <= current; r += 1) revs.add(r);
  return [...revs]
    .sort((a, b) => b - a)
    .map((r) => {
      const e = seen.get(r);
      const parts = [zh.report.revLabel(r)];
      if (r === current) parts.push(zh.report.revCurrent);
      if (e) parts.push(e.subtask_id ? subtaskName(subtasks, e.subtask_id) : zh.taskDetail.timelineKind.finished, shortTime(e.at));
      return { value: r, label: parts.join(' · ') };
    });
}

export interface CallKindRow {
  call_kind: string;
  prompt: number;
  completion: number;
  reasoning: number;
  cached: number;
  requests: number;
  unknown: number;
}

/** Token usage by call kind for a perf scope (all, main run only, or one subtask). */
export function usageByCallKind(rows: readonly UsageRow[], scope: 'all' | 'main' | 'subtask', subtask: string | null): CallKindRow[] {
  const keep = rows.filter((r) => (scope === 'all' ? true : scope === 'main' ? r.subtask_id === '' : r.subtask_id === subtask));
  const out = new Map<string, CallKindRow>();
  for (const r of keep) {
    const cur = out.get(r.call_kind) ?? { call_kind: r.call_kind, prompt: 0, completion: 0, reasoning: 0, cached: 0, requests: 0, unknown: 0 };
    cur.prompt += r.prompt_tokens;
    cur.completion += r.completion_tokens;
    cur.reasoning += r.reasoning_tokens;
    cur.cached += r.cached_tokens;
    cur.requests += r.requests;
    cur.unknown += r.requests_unknown_usage;
    out.set(r.call_kind, cur);
  }
  return [...out.values()];
}

/** Call kinds keep their v1 labels (a data contract of vlm_latency.csv); the Chinese name is a hint. */
export function callKindLabel(kind: string): string {
  const zhName = zh.report.callKind[kind];
  return zhName ? `${zhName} · ${kind}` : kind;
}

/** Seconds for perf tables: 3.1 s, 6 分 42 秒 for long walls. */
export function seconds(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  if (v >= 90) return zh.time.duration(v);
  return zh.common.seconds(Number(v.toFixed(1)));
}
