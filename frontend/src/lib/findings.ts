// The findings views (design doc 17 §5, F12.5): what a module found, by taxonomy item and level, in a
// form the report, the task detail and the Episode tab draw. Every name - levels, items, dimensions,
// codes, the reasons an item could not be assessed - comes from the registry (C1 2.x, GET /modules);
// nothing here knows a module. Pure functions, unit tested.
import type { EpisodeFinding, Finding, ModuleRegistry, ModuleSpec, Report, ReportModuleSectionV2, ReportV2 } from '../api/types';
import { zh } from '../locales/zh';

export type Level = 'blocking' | 'review' | 'info';
export const LEVELS: readonly Level[] = ['blocking', 'review', 'info'];

/** Bars and dots by level: Arco red-6, orange-6 and gray-6 (the info findings are reported only). */
export const LEVEL_COLOR: Record<Level, string> = { blocking: '#F53F3F', review: '#FF7D00', info: '#86909C' };
/** Arco Tag colours by level. */
export const LEVEL_TAG: Record<Level, string> = { blocking: 'red', review: 'orange', info: 'gray' };

/** A report of the policy verdicts (C2 2.0): its sections carry the findings statistics. */
export function isReportV2(r: unknown): r is ReportV2 {
  return Boolean(r && typeof r === 'object' && (r as { schema_version?: unknown }).schema_version === '2.0');
}

export function levelTitle(reg: ModuleRegistry | undefined, level: string): string {
  return reg?.finding_levels.find((l) => l.id === level)?.title_zh ?? zh.findings.level[level] ?? level;
}

type TaxonomyItem = ModuleRegistry['taxonomy']['items'][number];

export function taxonomyItem(reg: ModuleRegistry | undefined, id: string | null | undefined): TaxonomyItem | undefined {
  return id ? reg?.taxonomy.items.find((i) => i.id === id) : undefined;
}

/** «STRM-5 时间戳残段»; an info code of the platform's own has no item. */
export function itemLabel(reg: ModuleRegistry | undefined, id: string | null | undefined): string {
  if (!id) return zh.findings.platformItem;
  const it = taxonomyItem(reg, id);
  return it ? `${id} ${it.name_zh}` : id;
}

export function codeSpec(reg: ModuleRegistry | undefined, module: string, code: string) {
  return reg?.modules.find((m) => m.id === module)?.codes.find((c) => c.code === code);
}

/** A finding code's own name (the registry's), else the code. */
export function codeName(reg: ModuleRegistry | undefined, module: string, code: string): string {
  return codeSpec(reg, module, code)?.name_zh ?? code;
}

export function unassessableTitle(reg: ModuleRegistry | undefined, reason: string): string {
  return reg?.unassessable_reasons.find((r) => r.id === reason)?.title_zh ?? reason;
}

/**
 * Modules whose blocking codes reject even under 只报不拒 (pipeline/policy.py REPORT_ONLY_GATES, policy
 * version 2 on): data integrity - the modules after it cannot use an empty, cut or unreadable file.
 */
export const REPORT_ONLY_GATES: readonly string[] = ['data_integrity'];

/**
 * What a module can do to an episode, for its tag: reject it (a blocking code), ask a person (a review
 * code) or only report. The registry's default levels (P18); under the report_only preset nobody is asked
 * and only data integrity rejects - a task frozen with policy version 1 (before 2026-10-05) rejected
 * nothing (design doc 17 §4.1). No version: the policy a new task gets.
 */
export function moduleRole(m: Pick<ModuleSpec, 'id' | 'codes'> | undefined, preset?: string, policyVersion?: string | null): Level {
  // a retired code (C4 4.7.0) is no longer reported: it says nothing of what the module does now
  const levels = new Set((m?.codes ?? []).filter((c) => !c.retired).map((c) => c.level));
  if (preset === 'report_only') {
    const gate = policyVersion !== '1' && m !== undefined && REPORT_ONLY_GATES.includes(m.id);
    return gate && levels.has('blocking') ? 'blocking' : 'info';
  }
  return levels.has('blocking') ? 'blocking' : levels.has('review') ? 'review' : 'info';
}

/** «CPU 块 · 数值档»: where a module runs (registry 2.0 blocks and stages). */
export function placeLabel(reg: ModuleRegistry | undefined, m: Pick<ModuleSpec, 'block' | 'stage'> | undefined): string {
  if (!m) return '';
  const block = reg?.blocks.find((b) => b.id === m.block)?.title_zh ?? m.block;
  return `${block} · ${zh.stage[m.stage] ?? m.stage}`;
}

// ------------------------------------------------------------------ the coverage matrix

export type CoverageStatus = 'covered' | 'not_covered' | 'unassessable';

export interface CoverageItem {
  id: string;
  name: string;
  status: CoverageStatus;
  /** Covered, but some episodes could not be assessed for it, and why. */
  unassessable: { reason: string; title: string; count: number }[];
  /** Episodes with a finding of it (the report's findings_by_item). */
  episodes: number;
}

export interface CoverageDimension {
  id: string;
  name: string;
  items: CoverageItem[];
}

export interface CoverageMatrix {
  covered: number;
  /** Every check item of the taxonomy (control items are none). */
  total: number;
  taxonomyVersion: string;
  dimensions: CoverageDimension[];
}

/**
 * 本次覆盖分类表 N / 71 项 (design doc 17 §5.2): the taxonomy's check items by dimension - covered by a
 * module of this task, not covered by any, or covered but not assessable on some episodes (and why).
 */
export function coverageMatrix(reg: ModuleRegistry | undefined, overview: ReportV2['overview']): CoverageMatrix {
  const cov = overview.coverage;
  const covered = new Set(cov.covered);
  const checkable = new Set([...cov.covered, ...cov.not_covered]);
  const found = new Map((overview.findings_by_item ?? []).filter((f) => f.item).map((f) => [f.item as string, f.episodes]));
  const why = new Map<string, CoverageItem['unassessable']>();
  for (const u of cov.unassessable) {
    const list = why.get(u.item) ?? [];
    list.push({ reason: u.reason, title: unassessableTitle(reg, u.reason), count: u.count });
    why.set(u.item, list);
  }
  const items = (reg?.taxonomy.items ?? []).filter((i) => checkable.has(i.id));
  // without the registry's taxonomy the report's own lists still say what is covered
  const ids = items.length ? items.map((i) => i.id) : [...cov.covered, ...cov.not_covered];
  const dims = reg?.taxonomy.dimensions ?? [];
  const byDim = new Map<string, CoverageItem[]>();
  for (const id of ids) {
    const it = taxonomyItem(reg, id);
    const dim = it?.dimension ?? id.split('-')[0];
    const unassessable = why.get(id) ?? [];
    const status: CoverageStatus = !covered.has(id) ? 'not_covered' : unassessable.length ? 'unassessable' : 'covered';
    const row: CoverageItem = { id, name: it?.name_zh ?? id, status, unassessable, episodes: found.get(id) ?? 0 };
    byDim.set(dim, [...(byDim.get(dim) ?? []), row]);
  }
  const order = dims.length ? dims.map((d) => d.id) : [...byDim.keys()];
  return {
    covered: cov.covered.length,
    total: cov.covered.length + cov.not_covered.length,
    taxonomyVersion: cov.taxonomy_version,
    dimensions: order
      .filter((d) => byDim.has(d))
      .map((d) => ({ id: d, name: dims.find((x) => x.id === d)?.name_zh ?? d, items: byDim.get(d) ?? [] })),
  };
}

/** The modules (in report order) that report or cover an item: where a click on it goes. */
export function reportersOf(reg: ModuleRegistry | undefined, moduleIds: readonly string[], item: string): string[] {
  return moduleIds.filter((id) => {
    const m = reg?.modules.find((x) => x.id === id);
    return Boolean(m && (m.covers.includes(item) || m.codes.some((c) => c.item === item)));
  });
}

// ------------------------------------------------------------------ the overview

/**
 * 判废原因分布 by taxonomy item (design doc 17 §5.2): the report's reject_items (an episode once per item);
 * a report without them has its reasons summed per item (an episode with two codes of one item counts twice).
 */
export function rejectsByItem(reg: ModuleRegistry | undefined, overview: ReportV2['overview']): { name: string; value: number }[] {
  const rows = overview.reject_items ?? Object.values(
    overview.reject_reasons.reduce<Record<string, { item: string | null; count: number }>>((acc, r) => {
      const item = r.code ? r.item ?? null : null;
      const key = item ?? '';
      acc[key] = { item, count: (acc[key]?.count ?? 0) + r.count };
      return acc;
    }, {}),
  ).sort((a, b) => b.count - a.count);
  return rows.map((r) => ({ name: r.item ? itemLabel(reg, r.item) : zh.findings.humanDiscard, value: r.count }));
}

// ------------------------------------------------------------------ a module's findings

export interface ModuleItemRow {
  code: string;
  name: string;
  item: string | null;
  itemName: string;
  level: Level;
  episodes: number;
  share: number | null;
  byCamera: { camera: string; episodes: number }[];
}

type Summary2 = ReportModuleSectionV2['summary'];

/** A section's 检出项 (summary.items), most episodes first; codes keep registry order on ties. */
export function moduleItems(reg: ModuleRegistry | undefined, module: string, summary: Summary2 | undefined): ModuleItemRow[] {
  const rows = (summary?.items ?? []).map((r, i) => ({
    order: i,
    row: {
      code: r.code,
      name: codeName(reg, module, r.code),
      item: r.item ?? null,
      itemName: itemLabel(reg, r.item),
      level: r.level as Level,
      episodes: r.episodes,
      share: r.share ?? null,
      byCamera: r.by_camera ?? [],
    },
  }));
  return rows.sort((a, b) => b.row.episodes - a.row.episodes || a.order - b.order).map((x) => x.row);
}

/** Episodes with a finding of the module per level; a summary written before F12.5 is counted per code. */
export function moduleLevels(summary: Summary2 | undefined): Record<Level, number> {
  if (summary?.levels) return summary.levels as Record<Level, number>;
  const out: Record<Level, number> = { blocking: 0, review: 0, info: 0 };
  for (const r of summary?.items ?? []) out[r.level as Level] = Math.max(out[r.level as Level], r.episodes);
  return out;
}

/** The 0-1 readings' distributions (summary.score_hist 2.0: reading -> ten bins). */
export function scoreHists(summary: Summary2 | undefined): { reading: string; bins: { name: string; value: number }[] }[] {
  const hist = summary?.score_hist;
  if (!hist || Array.isArray(hist)) return [];
  return Object.entries(hist)
    .filter(([, bins]) => Array.isArray(bins))
    .map(([reading, bins]) => ({
      reading,
      bins: (bins as { name: string; count: number }[]).map((b) => ({ name: b.name, value: b.count })),
    }));
}

export function readingName(reading: string): string {
  return zh.findings.reading[reading] ?? reading;
}

// ------------------------------------------------------------------ one episode's findings

/** An episode's findings by level, in the order the revision lists them. */
export function groupByLevel<T extends { level: string }>(findings: readonly T[]): Record<Level, T[]> {
  const out: Record<Level, T[]> = { blocking: [], review: [], info: [] };
  for (const f of findings) (out[f.level as Level] ?? out.info).push(f);
  return out;
}

/** What a finding is about: «wrist 相机», «夹爪通道», «文件 data/chunk-000/…»; the whole episode says nothing. */
export function scopeText(scope: Finding['scope']): string {
  if (!scope) return '';
  const parts: string[] = [];
  if (scope.camera) parts.push(zh.findings.scope.camera(scope.camera));
  if (scope.cameras?.length) parts.push(zh.findings.scope.camera(scope.cameras.join('、')));
  if (scope.channel) parts.push(zh.findings.scope.channel(scope.channel));
  if (scope.arm) parts.push(zh.findings.scope.arm(scope.arm));
  if (scope.clock) parts.push(zh.findings.scope.clock(scope.clock));
  if (scope.file) parts.push(zh.findings.scope.file(scope.file));
  return parts.join(' · ');
}

/** Where in the episode (P19): seconds when the module gave them, else frames; null when it gave neither. */
export function intervalOf(f: Pick<Finding, 'frames' | 'time_s'>): { text: string; seekS: number | null } | null {
  if (f.time_s) return { text: zh.findings.seconds(f.time_s[0], f.time_s[1]), seekS: f.time_s[0] };
  if (f.frames) return { text: zh.findings.frames(f.frames[0], f.frames[1]), seekS: null };
  return null;
}

/** The modules whose findings an episode view lists (2.0), in the report's order. */
export function findingModules(findings: readonly EpisodeFinding[]): string[] {
  return [...new Set(findings.map((f) => f.module))];
}

/** A report as the 2.0 views read it, or null for one of the funnel (C2 1.0). */
export function v2Of(raw: Report | ReportV2 | null | undefined): ReportV2 | null {
  return isReportV2(raw) ? raw : null;
}
