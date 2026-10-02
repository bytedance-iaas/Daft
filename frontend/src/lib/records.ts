// Result records of either C2 format (design doc 17 §1): the console's views still render a record by 1.0's
// verdict, score and gate until the 2.0 views of F12.5, so a record 2.0 (findings) is read through the same
// compatibility rules as the backend's `records.legacy_verdict` - a finding whose code blocks by default is a
// fail, a review finding an abstention, a score makes it scored; none of the items its own per-episode codes
// report on assessed, or not what the module rejects on, is an abstention too. Details are the module's own and
// stay as they are.
import type { ModuleRegistry, Report, ReportResponse, ResultRecord, ResultRecordV2 } from '../api/types';
import { funnelGate } from './registry';

export type AnyRecord = ResultRecord | ResultRecordV2;
type Verdict = ResultRecord['verdict'];

export function isRecordV2(r: AnyRecord | null | undefined): r is ResultRecordV2 {
  return Boolean(r && typeof r === 'object' && 'status' in r);
}

export function recordScore(r: AnyRecord | null | undefined): number | null {
  if (!r) return null;
  if (isRecordV2(r)) {
    const s = (r.readings as Record<string, unknown> | undefined)?.score;
    return typeof s === 'number' ? s : null;
  }
  return r.score ?? null;
}

export function recordVerdict(r: AnyRecord | null | undefined, reg: ModuleRegistry | undefined): Verdict | null {
  if (!r) return null;
  if (!isRecordV2(r)) return r.verdict;
  if (r.status === 'error') return 'error';
  const spec = reg?.modules.find((m) => m.id === r.module);
  const levelOf = (code: string) => spec?.codes.find((c) => c.code === code)?.level ?? 'info';
  const levels = new Set(r.findings.map((f) => levelOf(f.code)));
  if (levels.has('blocking')) return 'fail';
  if (levels.has('review')) return 'abstain';
  if (recordScore(r) !== null) return 'scored';
  const codes = spec?.codes ?? [];
  const gateItems = new Set(codes.filter((c) => c.level === 'blocking').map((c) => c.item));
  const ownItems = new Set(codes.filter((c) => c.item && c.scope_kind !== 'dataset').map((c) => c.item));
  const assessed = ownItems.size ? r.assessed.filter((i) => ownItems.has(i)) : r.assessed;
  if (!assessed.length || r.unassessable.some((u) => gateItems.has(u.item))) return 'abstain';
  return 'pass';
}

/** A record of either format as the 1.0 views read it. */
export function asLegacyRecord(r: AnyRecord, reg: ModuleRegistry | undefined): ResultRecord {
  if (!isRecordV2(r)) return r;
  const verdict = recordVerdict(r, reg) ?? 'error';
  const spec = reg?.modules.find((m) => m.id === r.module);
  return {
    episode_index: r.episode_index,
    module: r.module,
    verdict,
    passed: verdict === 'pass' ? true : verdict === 'fail' ? false : null,
    score: recordScore(r),
    gate: (spec ? funnelGate(spec) : 'none') as ResultRecord['gate'],
    details: r.details,
    evidence: r.evidence,
    elapsed_s: r.elapsed_s,
    error: r.error,
  };
}

/** The statistics report 2.0 adds to every module section (design doc 17 §5.1): the 2.0 views of F12.5 show them;
 * the 1.0 views would list them as unknown fields. */
const V2_SUMMARY_KEYS = ['assessed_episodes', 'items', 'unassessable', 'dataset_findings', 'dataset_readings', 'delivered_family_distribution'];

/** A report of either format as the 1.0 views read it: a 2.0 section gets the funnel's gate back, its score
 * distribution (2.0: one per 0-1 reading) is the composite score's again and the 2.0 statistics are left out,
 * and the rejects counted per finding are summed per module (an episode with findings of several codes of one
 * module counts once per code there - the 2.0 views of F12.5 show them per finding). */
export function asLegacyReport(raw: unknown): Report {
  const r = raw as Report;
  if ((raw as { schema_version?: string }).schema_version !== '2.0') return r;
  const perModule = new Map<string, number>();
  for (const x of (r.overview.reject_reasons ?? []) as { module: string; count: number }[]) perModule.set(x.module, (perModule.get(x.module) ?? 0) + x.count);
  const legacySummary = (summary: Record<string, unknown>) => {
    const out = Object.fromEntries(Object.entries(summary).filter(([k]) => !V2_SUMMARY_KEYS.includes(k)));
    const hist = out.score_hist;
    if (hist === undefined || Array.isArray(hist)) return out;
    const score = (hist as Record<string, unknown>).score;
    delete out.score_hist;
    return Array.isArray(score) ? { ...out, score_hist: score } : out;
  };
  return {
    ...r,
    overview: { ...r.overview, reject_reasons: [...perModule].map(([module, count]) => ({ module, count })) },
    modules: r.modules.map((s) => ({
      ...s,
      gate: (s as { gate?: string }).gate ?? funnelGate({ id: s.id, codes: [] }),
      summary: legacySummary(s.summary as Record<string, unknown>),
    })) as Report['modules'],
  } as Report;
}

/** The report response with its report read as 1.0. */
export function legacyReportResponse(raw: unknown): ReportResponse {
  const r = raw as ReportResponse;
  return { ...r, report: asLegacyReport(r.report) };
}
