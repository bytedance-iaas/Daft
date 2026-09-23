// Reading report.json's module summaries (06 §6.2, F6.2) for the section views: chart-ready
// series, per-camera rows, the verdict counts - leniently, because a summary is a free object
// (C2) and reports written before the chart-ready keys have only some of them.
import { zh } from '../locales/zh';

export interface Item {
  name: string;
  value: number;
}

export type Summary = Record<string, unknown>;

export function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

export function str(v: unknown): string | null {
  return typeof v === 'string' && v ? v : null;
}

/** `[{name, count}]` (or `value`), or a `{name: count}` dict, as chart items; null otherwise. */
export function seriesOf(v: unknown, label: (name: string) => string = (n) => n): Item[] | null {
  if (Array.isArray(v)) {
    const out: Item[] = [];
    for (const x of v) {
      if (!x || typeof x !== 'object') return null;
      const o = x as Record<string, unknown>;
      const name = o.name ?? o.label ?? o.id ?? o.module;
      const value = num(o.count) ?? num(o.value);
      if ((typeof name !== 'string' && typeof name !== 'number') || value === null) return null;
      out.push({ name: label(String(name)), value });
    }
    return out;
  }
  if (v && typeof v === 'object') {
    const entries = Object.entries(v as Record<string, unknown>);
    if (!entries.length || !entries.every(([, x]) => num(x) !== null)) return null;
    return entries.map(([k, x]) => ({ name: label(k), value: x as number }));
  }
  return null;
}

/** Items with at least one non-zero value (a chart of zeros says nothing). */
export function anyValue(items: Item[] | null | undefined): items is Item[] {
  return Boolean(items && items.some((i) => i.value > 0));
}

export interface Counts {
  total: number;
  pass: number;
  fail: number;
  abstain: number;
  scored: number;
  error: number;
}

export function countsOf(s: Summary): Counts | null {
  const c = s.counts;
  if (!c || typeof c !== 'object') return null;
  const o = c as Record<string, unknown>;
  const n = (k: string) => num(o[k]) ?? 0;
  return { total: n('total'), pass: n('pass'), fail: n('fail'), abstain: n('abstain'), scored: n('scored'), error: n('error') };
}

/** The verdict distribution of a section, for the default chart. */
export function verdictItems(c: Counts): Item[] {
  return (['pass', 'fail', 'abstain', 'scored', 'error'] as const).filter((k) => c[k] > 0).map((k) => ({ name: zh.report.verdict[k] ?? k, value: c[k] }));
}

/** An array of objects (per-camera rows, the skill tree...), or null. */
export function rowsOf<T extends Record<string, unknown>>(v: unknown): T[] | null {
  if (!Array.isArray(v) || !v.every((x) => x && typeof x === 'object' && !Array.isArray(x))) return null;
  return v as T[];
}

/** 0.84 → "0.84" (two decimals at most), null → "—". */
export function fmt(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return '—';
  return String(Number(v.toFixed(digits)));
}

/** A signed lag: +0.07 / −0.01. */
export function signed(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined) return '—';
  const s = v.toFixed(digits);
  return v > 0 ? `+${s}` : v < 0 ? `−${s.slice(1)}` : s;
}

/** Does a summary carry any of `keys` (a report written after the chart-ready aggregates)? */
export function hasAny(s: Summary, keys: readonly string[]): boolean {
  return keys.some((k) => k in s);
}

/**
 * One line for tables (本次质检范围, the task detail's 判决摘要): the essence of a section,
 * read from the keys it has - «错位判废 0 条，已标注 3 条», «6 个技能族，标注分歧 5 条»,
 * «剔除 1 条», «平均分 0.84», «判废 5 条，转人工 6 条，出错 2 条». Null without counts.
 */
export function sectionDigest(s: Summary): string | null {
  const c = countsOf(s);
  const Z = zh.summaryDigest;
  const parts: string[] = [];
  const verdicts = seriesOf(s.verdicts);
  const mean = num(s.mean_score);
  if (verdicts) {
    const v = Object.fromEntries(verdicts.map((i) => [i.name, i.value]));
    parts.push(Z.syncVerdicts(v.misaligned ?? 0, (v.annotated ?? 0) + (v.suspect ?? 0)));
  } else if (num(s.families) !== null) {
    parts.push(Z.families(num(s.families)!));
    if (num(s.label_disagreements) !== null) parts.push(Z.disagreements(num(s.label_disagreements)!));
  } else if (num(s.removed) !== null) {
    parts.push(Z.removed(num(s.removed)!));
  } else if (num(s.candidates) !== null) {
    parts.push(Z.candidates(num(s.candidates)!));
  } else if (mean !== null) {
    parts.push(Z.mean(fmt(mean)));
  } else if (c) {
    if (c.fail) parts.push(Z.fail(c.fail));
    if (c.abstain) parts.push(Z.abstain(c.abstain));
  }
  if (c?.error) parts.push(Z.error(c.error));
  if (!parts.length && c) parts.push(c.total ? Z.allPass(c.total) : Z.nothing);
  return parts.length ? parts.join('，') : null;
}
