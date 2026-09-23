// Module summaries in report.json are free-form objects per module (C2 report.schema: summary is
// `object`). The generic view: scalars as labelled numbers, {name, count} arrays and dicts of
// counts as charts, anything else as readable lines (never JSON).
import { zh } from '../locales/zh';
import { sectionDigest, seriesOf } from './sectionStats';

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

export function splitSummary(
  summary: Record<string, unknown> | null | undefined,
  omit: readonly string[] = [],
): { scalars: SummaryScalar[]; series: SummarySeries[]; other: [string, unknown][] } {
  const scalars: SummaryScalar[] = [];
  const series: SummarySeries[] = [];
  const other: [string, unknown][] = [];
  for (const [key, v] of Object.entries(summary ?? {})) {
    if (omit.includes(key)) continue;
    if (v === null || ['string', 'number', 'boolean'].includes(typeof v)) scalars.push({ key, label: summaryLabel(key), value: v as SummaryScalar['value'] });
    else {
      const s = seriesOf(v, (n) => zh.summaryKeys[n] ?? n);
      if (s && s.length) series.push({ key, label: summaryLabel(key), items: s });
      else other.push([key, v]);
    }
  }
  return { scalars, series, other };
}

export function formatScalar(v: SummaryScalar['value']): string {
  if (v === null) return '—';
  if (typeof v === 'boolean') return v ? zh.common.yes : zh.common.no;
  if (typeof v === 'number') return Number.isInteger(v) ? v.toLocaleString('en-US') : String(Number(v.toFixed(3)));
  return v;
}

/** A one-line digest for tables (本次质检范围, task detail 判决摘要). */
export function summaryDigest(summary: Record<string, unknown> | null | undefined, max = 4): string {
  const essence = sectionDigest(summary ?? {});
  if (essence) return essence;
  const { scalars } = splitSummary(summary);
  return scalars
    .slice(0, max)
    .map((s) => `${s.label} ${formatScalar(s.value)}`)
    .join(' · ');
}
