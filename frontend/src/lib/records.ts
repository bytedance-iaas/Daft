// The modules' own views - the Episode tab's per-module blocks, the specialised report sections - render a result
// record by a verdict word and a score, and a report section by the keys of its module's own summary (design doc 06
// §6.2). Records and reports are C2 2.0 only (D90); this is how those views read them, with the same rules as the
// backend's `records.legacy_verdict`: a finding whose code blocks by default is a fail, a review finding an
// abstention, a score makes it scored; none of the items its own per-episode codes report on assessed, or not what
// the module rejects on, is an abstention too; a record the module wrote without judging the episode (details.skipped:
// no task text, data integrity rejected it - D94) is skipped. Details are the module's own and stay as they are. The findings views
// (src/lib/findings.ts) read records and reports as they are.
import type { ModuleRegistry, Report, ReportModuleSection, ResultRecord } from '../api/types';

/** The verdict words the modules' own views use. */
export type LegacyVerdict = 'pass' | 'fail' | 'abstain' | 'scored' | 'error' | 'skipped';

/** A record as the modules' own views read it. */
export interface LegacyRecord {
  episode_index: number;
  module: string;
  verdict: LegacyVerdict;
  passed: boolean | null;
  score: number | null;
  details: Record<string, unknown>;
  evidence: string[];
  elapsed_s: number | null;
  error: ResultRecord['error'];
}

/** The module's 0-1 composite score among its readings, if it has one. */
export function recordScore(r: ResultRecord | null | undefined): number | null {
  const s = (r?.readings as Record<string, unknown> | undefined)?.score;
  return typeof s === 'number' ? s : null;
}

export function recordVerdict(r: ResultRecord | null | undefined, reg: ModuleRegistry | undefined): LegacyVerdict | null {
  if (!r) return null;
  if (r.status === 'error') return 'error';
  if (typeof r.details?.skipped === 'string' && r.details.skipped) return 'skipped';
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

/** A record as the modules' own views read it. */
export function asLegacyRecord(r: ResultRecord, reg: ModuleRegistry | undefined): LegacyRecord {
  const verdict = recordVerdict(r, reg) ?? 'error';
  return {
    episode_index: r.episode_index,
    module: r.module,
    verdict,
    passed: verdict === 'pass' ? true : verdict === 'fail' ? false : null,
    score: recordScore(r),
    details: r.details,
    evidence: r.evidence,
    elapsed_s: r.elapsed_s,
    error: r.error,
  };
}

/** A report section as its module's own view reads it: the summary is the module's own keys. */
export type LegacySection = Omit<ReportModuleSection, 'summary'> & { summary: Record<string, unknown> };

/** A report whose sections are read by the modules' own views. */
export type LegacyReport = Omit<Report, 'modules'> & { modules: LegacySection[] };

/** The statistics every module section has (design doc 17 §5.1): the findings views show them; the modules' own
 * views would list them as unknown fields. */
const FINDINGS_SUMMARY_KEYS = ['assessed_episodes', 'flagged_episodes', 'levels', 'items', 'unassessable', 'dataset_findings', 'dataset_readings', 'delivered_family_distribution'];

/** A section as its module's own view reads it: the findings statistics left out, and the composite score's
 * distribution (one of the 0-1 readings' in score_hist) as the module's own score_hist. */
export function asLegacySection(s: ReportModuleSection): LegacySection {
  const summary = Object.fromEntries(Object.entries(s.summary as Record<string, unknown>).filter(([k]) => !FINDINGS_SUMMARY_KEYS.includes(k)));
  const hist = summary.score_hist;
  if (hist !== undefined && !Array.isArray(hist)) {
    const score = (hist as Record<string, unknown>).score;
    delete summary.score_hist;
    if (Array.isArray(score)) summary.score_hist = score;
  }
  return { ...s, summary };
}

/** A report with its sections as the modules' own views read them. */
export function asLegacyReport(r: Report): LegacyReport {
  return { ...r, modules: r.modules.map(asLegacySection) };
}

/** The rejects per module (the overview counts them per blocking finding: an episode with findings of several
 * codes of one module counts once per code there). */
export function rejectsByModule(overview: Report['overview']): { module: string; count: number }[] {
  const perModule = new Map<string, number>();
  for (const x of overview.reject_reasons) perModule.set(x.module, (perModule.get(x.module) ?? 0) + x.count);
  return [...perModule].map(([module, count]) => ({ module, count }));
}
