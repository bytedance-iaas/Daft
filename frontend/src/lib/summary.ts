// Module summaries in report.json are free-form objects per module (C2 report.schema: summary is
// `object`). The generic view: scalars as labelled numbers, arrays of {name, count} as charts.
import { zh } from '../locales/zh';

export interface SummaryScalar {
  key: string;
  label: string;
  value: string | number | boolean | null;
}

export interface SummarySeries {
  key: string;
  label: string;
  items: { name: string; value: number }[];
}

export function summaryLabel(key: string): string {
  return zh.summaryKeys[key] ?? key;
}

function asSeries(v: unknown): { name: string; value: number }[] | null {
  if (!Array.isArray(v) || !v.length) return null;
  const out: { name: string; value: number }[] = [];
  for (const x of v) {
    if (!x || typeof x !== 'object') return null;
    const o = x as Record<string, unknown>;
    const name = o.name ?? o.label ?? o.id ?? o.module;
    const value = o.count ?? o.value;
    if ((typeof name !== 'string' && typeof name !== 'number') || typeof value !== 'number') return null;
    out.push({ name: String(name), value });
  }
  return out;
}

export function splitSummary(summary: Record<string, unknown> | null | undefined): { scalars: SummaryScalar[]; series: SummarySeries[]; other: [string, unknown][] } {
  const scalars: SummaryScalar[] = [];
  const series: SummarySeries[] = [];
  const other: [string, unknown][] = [];
  for (const [key, v] of Object.entries(summary ?? {})) {
    if (v === null || ['string', 'number', 'boolean'].includes(typeof v)) scalars.push({ key, label: summaryLabel(key), value: v as SummaryScalar['value'] });
    else {
      const s = asSeries(v);
      if (s) series.push({ key, label: summaryLabel(key), items: s });
      else other.push([key, v]);
    }
  }
  return { scalars, series, other };
}

export function formatScalar(v: SummaryScalar['value']): string {
  if (v === null) return '—';
  if (typeof v === 'boolean') return v ? '是' : '否';
  if (typeof v === 'number') return Number.isInteger(v) ? v.toLocaleString('en-US') : String(Number(v.toFixed(3)));
  return v;
}

/** A one-line digest for tables (task detail 判决摘要). */
export function summaryDigest(summary: Record<string, unknown> | null | undefined, max = 4): string {
  const { scalars } = splitSummary(summary);
  return scalars
    .slice(0, max)
    .map((s) => `${s.label} ${formatScalar(s.value)}`)
    .join(' · ');
}
