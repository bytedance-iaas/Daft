// 数据包完整性 (07 §5, F6.2): report.json's `integrity` and `overview.dataset` as Chinese label /
// value pairs for the horizontal grid - no raw keys, no JSON. The CLI writes the preflight's
// findings there (format, validation, warnings, labels, semantics profile, robot type; C2
// report / preflight); keys this page does not know are still shown, readably.
import type { Report, Task } from '../api/types';
import { zh } from '../locales/zh';
import { bytes } from './format';
import { fieldLabel, readable } from './reportView';

export interface IntegrityItem {
  key: string;
  label: string;
  value: string;
  /** Spans the whole row (long values: warnings, validation). */
  full?: boolean;
  /** Something the reader should look at. */
  warn?: boolean;
}

const P = () => zh.reportPage;

/** `sha256:9f3c…e21a` */
export function shortDigest(d: unknown): string | null {
  if (typeof d !== 'string' || !d) return null;
  const m = /^(sha256:)?([0-9a-f]{8,})$/i.exec(d);
  if (!m) return d;
  const hex = m[2];
  return `${m[1] ?? ''}${hex.slice(0, 4)}…${hex.slice(-4)}`;
}

/** A preflight warning (English in the CLI, C2 preflight.warnings) in Chinese. */
export function warningText(w: string): string {
  let m = /^(\d+) episodes? miss their parquet or a camera's video \(([^)]*)\)/.exec(w);
  if (m) return P().warnMissingV2(m[1], m[2]);
  m = /^(\d+) episodes? miss data or video files \(([^)]*)\)/.exec(w);
  if (m) return P().warnMissingV3(m[1], m[2]);
  m = /^camera (\S+) has no video files$/.exec(w);
  if (m) return P().warnNoVideo(m[1]);
  m = /^info\.json total_episodes is (\d+) but the episode table lists (\d+)$/.exec(w);
  if (m) return P().warnTotal(m[1], m[2]);
  m = /^episode indices are not 0\.\.(\d+)/.exec(w);
  if (m) return P().warnIndices(m[1]);
  // D52 (design doc 14 §1): empty or too small files, mcap recordings cut off, summary CRC
  m = /^(\d+) files? (?:is|are) empty or too small to be valid \((.*)\)$/.exec(w);
  if (m) return P().warnEmptyFiles(m[1], m[2]);
  m = /^(\d+) episodes? \(([^)]*)\) (?:was|were) cut off while recording/.exec(w);
  if (m) return P().warnCutOff(m[1], m[2]);
  m = /^(\d+) episodes? \(([^)]*)\) ha(?:s|ve) a summary section that fails its CRC/.exec(w);
  if (m) return P().warnSummaryCrc(m[1], m[2]);
  return /[\u4e00-\u9fff]/.test(w) ? w : P().warnRaw(w);
}

function formatValue(f: unknown, validation: unknown[]): string {
  if (typeof f === 'string') return f;
  if (!f || typeof f !== 'object') return P().notRead;
  const o = f as { kind?: string; version?: string | null; supported?: boolean; detail?: string };
  const name = [P().formatKind[o.kind ?? 'unknown'] ?? o.kind ?? '', o.version ?? ''].filter(Boolean).join(' ');
  if (o.supported === false) return `${name}${P().formatUnsupported(o.detail ?? '')}`;
  return `${name}${validation.length ? P().formatIssues : P().formatOk}`;
}

function episodesValue(report: Report, task: Task | undefined, dataset: Record<string, unknown>): string {
  const c = report.overview.counts;
  const count = typeof dataset.episode_count === 'number' ? dataset.episode_count : null;
  const sel = task?.episodes;
  let scope: string;
  if (sel?.mode === 'head') scope = P().episodesHead(sel.n);
  else if (sel?.mode === 'explicit') scope = P().episodesExplicit(sel.expr, c.total);
  else if (sel?.mode === 'all') scope = P().episodesAll(c.total);
  else scope = P().episodesChecked(c.total);
  return P().episodesValue(count, scope) + (c.skipped ? P().episodesSkipped(c.skipped) : '');
}

/**
 * mcap / lance (D44): how the run's dataset is delivered and v1's container findings
 * ([{项, 状态, 说明}], export/report.container_findings) as one line.
 */
export function containerValue(v: unknown): string | null {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return null;
  const o = v as { format?: unknown; delivery?: unknown; findings?: unknown };
  const findings = Array.isArray(o.findings)
    ? o.findings
        .filter((f): f is Record<string, unknown> => Boolean(f) && typeof f === 'object')
        .map((f) => zh.report.containerFinding(f))
        .join(zh.report.containerFindingSep)
    : '';
  return zh.report.containerValue(String(o.format ?? ''), String(o.delivery ?? '—'), findings);
}

const KNOWN = new Set(['format', 'validation', 'warnings', 'labels', 'profile', 'robot_type', 'skipped_episodes', 'container']);

/**
 * The integrity grid: format, episodes, cameras, frame rate, robot type, semantics profile, task
 * labels, structure check, preflight notes, source manifest - and any other key, readably.
 */
export function integrityItems(report: Report, task?: Task): IntegrityItem[] {
  const integ = (report.integrity ?? {}) as Record<string, unknown>;
  const dataset = (report.overview.dataset ?? {}) as Record<string, unknown>;
  const modules = new Set(report.modules.map((m) => m.id));
  const items: IntegrityItem[] = [];
  const validation = Array.isArray(integ.validation) ? integ.validation.map(String) : [];
  const warnings = Array.isArray(integ.warnings) ? integ.warnings.map(String) : [];
  const L = P().integrity;

  if ('format' in integ) items.push({ key: 'format', label: L.format, value: formatValue(integ.format, validation), warn: validation.length > 0 });
  items.push({ key: 'episodes', label: L.episodes, value: episodesValue(report, task, dataset) });
  const cams = Array.isArray(dataset.cameras) ? dataset.cameras.map(String) : null;
  items.push({ key: 'cameras', label: L.cameras, value: cams?.length ? P().camerasValue(cams) : P().notRead });
  items.push({ key: 'fps', label: L.fps, value: typeof dataset.fps === 'number' ? P().fpsValue(dataset.fps) : P().notRead });
  const robot = (integ.robot_type ?? dataset.robot_type) as unknown;
  const kinSkipped = report.skipped_modules.some((s) => s.id === 'kinematic_limits');
  items.push({ key: 'robot_type', label: L.robot, value: typeof robot === 'string' && robot ? robot : P().notRead + (kinSkipped ? P().robotKinematicsSkipped : '') });
  if ('profile' in integ) {
    const p = integ.profile as { matched?: string; by?: string } | null;
    items.push({ key: 'profile', label: L.profile, value: p && p.matched ? P().profileMatched(p.matched, p.by ?? '') : P().profileNone });
  }
  const labels = integ.labels as { with_task?: number; without_task?: number } | null | undefined;
  if (labels && typeof labels.with_task === 'number' && typeof labels.without_task === 'number') {
    const captioned = labels.without_task > 0 && (modules.has('task_success') || modules.has('skill_profile'));
    items.push({ key: 'labels', label: L.labels, value: P().labelsValue(labels.with_task, labels.without_task) + (captioned ? P().labelsCaptioned : '') });
  } else if ('labels' in integ) {
    items.push({ key: 'labels', label: L.labels, value: P().notRead });
  }
  const src = task?.source;
  const digest = shortDigest(src?.digest ?? dataset.source_digest);
  if (src && typeof src.objects === 'number') items.push({ key: 'source', label: L.source, value: P().sourceValue(src.objects, bytes(src.bytes), digest ?? '—') });
  else if (digest) items.push({ key: 'source', label: L.source, value: P().sourceDigest(digest) });
  const container = containerValue(integ.container);
  if (container) items.push({ key: 'container', label: fieldLabel('container'), value: container, full: true });
  if ('validation' in integ) items.push({ key: 'validation', label: L.validation, value: validation.length ? validation.join('；') : P().validationOk, full: validation.length > 0, warn: validation.length > 0 });
  if ('warnings' in integ) items.push({ key: 'warnings', label: L.warnings, value: warnings.length ? warnings.map(warningText).join('；') : P().none, full: warnings.length > 0, warn: warnings.length > 0 });
  // Anything else the report carries (older or newer writers): labelled and readable.
  for (const [k, v] of Object.entries(integ)) {
    if (KNOWN.has(k)) continue;
    items.push({ key: k, label: fieldLabel(k), value: readable(v) });
  }
  return items;
}
