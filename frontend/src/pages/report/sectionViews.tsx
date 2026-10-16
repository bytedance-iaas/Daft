// The report's module sections (06 §6.2, F6.2): a module's own view - a row of key figures and one to
// three charts drawn from its summary's chart-ready aggregates - after the findings statistics every
// section has (FindingsView). Statistics only, never a list of episodes (those are in the Episode tab)
// and never JSON. A module without an entry here has the findings statistics alone, so a new module
// needs no frontend change.
import { Table, Typography } from '@arco-design/web-react';
import type { ComponentType, ReactNode } from 'react';
import { CHART_COLORS, Chart, LEGEND_GRID_TOP, barOption, chartSummary, groupedBarOption, type ChartOption } from '../../components/Chart';
import { LazyVisible } from '../../components/LazyVisible';
import { StatCell } from '../../components/StatCell';
import { percent } from '../../lib/format';
import type { LegacySection } from '../../lib/records';
import { anyValue, checkedOf, countsOf, fmt, num, rowsOf, seriesOf, signed, str, verdictItems, type Item, type Summary } from '../../lib/sectionStats';
import { formatScalar } from '../../lib/summary';
import { zh } from '../../locales/zh';

export interface SectionViewProps {
  taskId: string;
  rev: number;
  section: LegacySection;
}

interface StatSpec {
  label: string;
  value: ReactNode;
  foot?: ReactNode;
  tone?: 'bad' | 'warn' | 'good';
}

export interface ChartSpec {
  key: string;
  title: string;
  desc?: string;
  items?: Item[];
  horizontal?: boolean;
  colors?: (string | undefined)[];
  band?: [number, number];
  valueName?: string;
  valueRange?: [number, number];
  /** A line under the chart (per-camera means...). */
  foot?: ReactNode;
  /** A ready option (grouped bars...) with its accessible summary. */
  option?: ChartOption;
  summary?: string;
  height?: number;
  /** Where the plot starts, to line up with a neighbour that has a legend on top. */
  gridTop?: number;
  /** Two columns of the section's chart grid (per-camera histograms need the width). */
  wide?: boolean;
}

interface ViewModel {
  stats: StatSpec[];
  charts: ChartSpec[];
  blocks?: ReactNode[];
  notes?: ReactNode[];
}

const S = () => zh.sections;
const GREEN = '#00B42A';
const RED = '#F53F3F';
const ORANGE = '#FF7D00';

// -------------------------------------------------------------------------- building blocks

const Stat = (s: StatSpec) => <StatCell {...s} />;

function chartHeight(c: ChartSpec): number {
  if (c.height) return c.height;
  const n = c.items?.length ?? 6;
  return c.horizontal ? Math.max(96, Math.min(420, n * 26 + 40)) : 200;
}

export function ChartBlock({ spec }: { spec: ChartSpec }) {
  const option = spec.option ?? barOption(spec.items ?? [], { horizontal: spec.horizontal, colors: spec.colors, band: spec.band, valueName: spec.valueName, valueRange: spec.valueRange, gridTop: spec.gridTop });
  const summary = `${spec.title}：${spec.summary ?? chartSummary(spec.items ?? [])}`;
  return (
    <div className={`section-chart${spec.wide ? ' wide' : ''}`} data-testid={`chart-${spec.key}`}>
      <div className="section-sub">
        {spec.title}
        {spec.desc ? <span className="muted">{spec.desc}</span> : null}
      </div>
      {/* at least its own height, and as tall as the tallest chart of its row (.section-charts) */}
      <LazyVisible className="section-chart-body" placeholder={<div style={{ height: chartHeight(spec) }} />}>
        <Chart option={option} summary={summary} height={chartHeight(spec)} />
      </LazyVisible>
      {spec.foot ? <div className="section-note muted">{spec.foot}</div> : null}
    </div>
  );
}

function ModelView({ id, model }: { id: string; model: ViewModel }) {
  return (
    <div className="section-view">
      {model.stats.length ? (
        <div className="stat-grid" data-testid={`summary-${id}`}>
          {model.stats.map((s, i) => (
            <Stat key={`${s.label}-${i}`} {...s} />
          ))}
        </div>
      ) : null}
      {model.charts.length ? (
        <div className="section-charts">
          {model.charts.map((c) => (
            <ChartBlock key={c.key} spec={c} />
          ))}
        </div>
      ) : null}
      {model.blocks?.map((b, i) => (
        <div key={i} className="section-block">
          {b}
        </div>
      ))}
      {model.notes?.filter(Boolean).map((n, i) => (
        <div key={i} className="section-note">
          {n}
        </div>
      ))}
    </div>
  );
}

/** Items labelled through a table of Chinese names (unknown codes stay as they are). */
function labelled(v: unknown, names: Record<string, string>): Item[] | null {
  return seriesOf(v, (n) => names[n] ?? n);
}

/** Whether a section shows why its module abstained (always, unless the module says when not). */
type ViewOpts = { abstain?: boolean | ((s: Summary) => boolean) };

/** The charts every verdict module may add: why it abstained, where the errors stopped. */
function commonCharts(s: Summary, model: ViewModel, opts: ViewOpts = {}): ViewModel {
  const charts = [...model.charts];
  const abstain = seriesOf(s.abstain_reason_counts);
  const shows = typeof opts.abstain === 'function' ? opts.abstain(s) : opts.abstain !== false;
  if (shows && anyValue(abstain) && !charts.some((c) => c.key === 'abstain')) {
    charts.push({ key: 'abstain', title: S().abstainReasons, items: abstain, horizontal: true });
  }
  const steps = labelled(s.error_steps, S().step);
  if (anyValue(steps)) charts.push({ key: 'errors', title: S().errorSteps, items: steps, horizontal: true, colors: steps.map(() => ORANGE) });
  return { ...model, charts };
}

/** Without any chart of its own, a section still shows its verdict distribution. */
function withFallback(s: Summary, model: ViewModel): ViewModel {
  if (model.charts.length) return model;
  const c = countsOf(s);
  const items = c ? verdictItems(c) : [];
  if (!items.length) return { ...model, notes: [...(model.notes ?? []), <span className="muted">{S().noStats}</span>] };
  return { ...model, charts: [{ key: 'counts', title: S().counts, items }] };
}

const scoreHist = (s: Summary): ChartSpec | null => {
  const items = seriesOf(s.score_hist);
  return items?.length ? { key: 'score', title: S().score.hist, desc: S().score.histDesc, items } : null;
};

/** A lag axis that shows the tolerance band and every reading, on round tenths. */
function lagRange(lags: number[], tol: number): [number, number] {
  const lo = Math.min(-tol, ...lags) * 1.2;
  const hi = Math.max(tol, ...lags) * 1.2;
  return [Math.floor(lo * 10) / 10, Math.ceil(hi * 10) / 10];
}

const pieces = (items: Item[] | null, names: Record<string, string> = {}) => (items ?? []).map((i) => `${names[i.name] ?? i.name} ${i.value}`).join(' · ');

// -------------------------------------------------------------------------- the modules

function timestampModel(s: Summary): ViewModel {
  const Z = S().timestamp;
  const c = countsOf(s);
  const raw = seriesOf(s.fail_reasons) ?? seriesOf(s.fail_kinds) ?? [];
  const byKind = Object.fromEntries(raw.map((i) => [i.name, i.value]));
  const stats: StatSpec[] = [];
  if (c) stats.push({ label: Z.failed, value: c.fail, tone: c.fail ? 'bad' : undefined });
  for (const k of ['out_of_order', 'gap', 'fragment', 'jitter', 'camera_gap']) if (k in byKind) stats.push({ label: Z.kinds[k], value: byKind[k] });
  if (c) stats.push({ label: S().checked, value: checkedOf(c) });
  if (num(s.duration_total_s) !== null) stats.push({ label: Z.durationTotal, value: zh.time.duration(num(s.duration_total_s)) });
  if (num(s.duration_median_s) !== null) stats.push({ label: Z.durationMedian, value: Z.seconds(num(s.duration_median_s)!) });
  if (num(s.duration_min_s) !== null && num(s.duration_max_s) !== null) stats.push({ label: Z.durationRange, value: `${Z.seconds(num(s.duration_min_s)!)} / ${Z.seconds(num(s.duration_max_s)!)}` });
  const charts: ChartSpec[] = [];
  const reasons = raw.map((i) => ({ name: Z.kinds[i.name] ?? i.name, value: i.value }));
  if (anyValue(reasons)) charts.push({ key: 'fail', title: Z.failChart, items: reasons, colors: reasons.map(() => RED) });
  const hist = seriesOf(s.duration_hist);
  if (hist?.length) charts.push({ key: 'duration', title: Z.durationChart, desc: Z.durationDesc, items: hist });
  return { stats, charts };
}

function kinematicsModel(s: Summary): ViewModel {
  const Z = S().kinematics;
  const c = countsOf(s);
  const stats: StatSpec[] = [];
  const episodes = num(s.violation_episodes);
  stats.push({ label: Z.episodes, value: episodes ?? c?.fail ?? '—', tone: (episodes ?? c?.fail) ? 'bad' : undefined });
  if (c) {
    stats.push({ label: S().task.fail, value: c.fail });
    if (c.abstain) stats.push({ label: zh.report.verdict.abstain, value: c.abstain, tone: 'warn' });
    stats.push({ label: S().checked, value: checkedOf(c) });
  }
  if (str(s.limits_profile)) stats.push({ label: Z.profile, value: str(s.limits_profile) });
  const charts: ChartSpec[] = [];
  const byType = labelled(s.violations_by_type, Z.types);
  if (anyValue(byType)) charts.push({ key: 'type', title: Z.byType, desc: Z.byDesc, items: byType, horizontal: true, colors: byType.map(() => RED) });
  const byJoint = seriesOf(s.violations_by_joint, Z.joint);
  if (anyValue(byJoint)) charts.push({ key: 'joint', title: Z.byJoint, desc: Z.byDesc, items: byJoint });
  const notes = episodes === 0 ? [Z.none] : [];
  return { stats, charts, notes };
}

function motionModel(s: Summary): ViewModel {
  const Z = S().motion;
  const c = countsOf(s);
  const stats: StatSpec[] = [];
  if (num(s.mean_score) !== null) stats.push({ label: S().score.mean, value: fmt(num(s.mean_score)) });
  if (num(s.stuck_episodes) !== null) {
    const na = num(s.stuck_unassessable);
    stats.push({ label: Z.stuck, value: num(s.stuck_episodes), foot: na ? `${Z.stuckFoot}；${Z.stuckNa(na)}` : Z.stuckFoot, tone: num(s.stuck_episodes) ? 'warn' : undefined });
  }
  if (num(s.active_ratio_mean) !== null) stats.push({ label: Z.activeRatio, value: percent(num(s.active_ratio_mean), 0), foot: Z.activeFoot });
  if (c) {
    stats.push({ label: zh.report.verdict.scored, value: c.scored });
    if (c.abstain) stats.push({ label: zh.report.verdict.abstain, value: c.abstain, tone: 'warn' });
  }
  const subs = rowsOf<{ name: string; mean: number | null; n: number; na: number; in_total: boolean; na_reason?: string }>(s.subscores) ?? [];
  const charts: ChartSpec[] = [];
  const scored = subs.filter((x) => num(x.mean) !== null);
  if (scored.length) {
    charts.push({
      key: 'subscores',
      title: Z.subscores,
      desc: Z.subscoresDesc,
      items: scored.map((x) => ({ name: `${Z.sub[x.name] ?? x.name}（${x.in_total ? Z.inTotal : Z.reportOnly}）`, value: Number(x.mean!.toFixed(3)) })),
      colors: scored.map((x) => (x.in_total ? CHART_COLORS[0] : CHART_COLORS[1])),
      horizontal: true,
      valueRange: [0, 1],
    });
  }
  const hist = scoreHist(s);
  if (hist) charts.push(hist);
  const idle = labelled(s.idle_episodes, Z.idleKinds);
  if (anyValue(idle)) charts.push({ key: 'idle', title: Z.idle, desc: Z.idleDesc, items: idle });
  const notes = subs
    .filter((x) => x.na > 0)
    .map((x) => (num(x.mean) === null ? Z.notApplicable(Z.sub[x.name] ?? x.name, x.na_reason ?? Z.naGeneric) : Z.partlyApplicable(Z.sub[x.name] ?? x.name, x.na, x.na_reason ?? Z.naGeneric)));
  return { stats, charts, notes };
}

function visualModel(s: Summary): ViewModel {
  const Z = S().visual;
  const c = countsOf(s);
  const cams = rowsOf<{ camera: string; n: number; mean: number | null; low: number; placeholder: number; hist: number[]; weight?: number }>(s.cameras) ?? [];
  const stats: StatSpec[] = [];
  if (num(s.mean_score) !== null) stats.push({ label: S().score.mean, value: fmt(num(s.mean_score)) });
  if (c) stats.push({ label: S().checked, value: checkedOf(c), foot: cams.length ? Z.perCamera(cams.length) : undefined });
  if (num(s.low_camera_readings) !== null) stats.push({ label: Z.low, value: num(s.low_camera_readings), tone: num(s.low_camera_readings) ? 'warn' : undefined });
  if (num(s.placeholder_readings) !== null) stats.push({ label: Z.placeholder, value: num(s.placeholder_readings) });
  if (num(s.blur_ref_var) !== null) stats.push({ label: Z.blurRef, value: formatScalar(num(s.blur_ref_var)), foot: num(s.frame_max_side) !== null ? `${Z.maxSide} ${Z.maxSideFoot(num(s.frame_max_side)!)}` : undefined });
  const weights = [...new Set(cams.map((x) => x.weight).filter((w): w is number => typeof w === 'number'))];
  if (weights.length === 1) stats.push({ label: Z.weights, value: Z.weightsSame(weights[0]) });
  else if (weights.length > 1) stats.push({ label: Z.weights, value: cams.map((x) => `${x.camera} ${x.weight ?? '—'}`).join('、') });
  const charts: ChartSpec[] = [];
  const bins = (seriesOf(s.score_hist) ?? Array.from({ length: 10 }, (_, i) => ({ name: `${(i / 10).toFixed(1)}–${((i + 1) / 10).toFixed(1)}`, value: 0 }))).map((b) => b.name);
  const withHist = cams.filter((x) => Array.isArray(x.hist) && x.hist.length === bins.length);
  if (withHist.length) {
    const series = withHist.map((x) => ({ name: x.camera, data: x.hist }));
    charts.push({
      key: 'cameras',
      title: Z.cameraChart,
      desc: Z.cameraChartDesc,
      option: groupedBarOption(bins, series),
      summary: withHist.map((x) => Z.cameraSummary(x.camera, fmt(x.mean), x.low)).join('；'),
      foot: withHist.map((x) => Z.cameraMean(x.camera, fmt(x.mean))).join('；'),
      height: 240,
      wide: true,
    });
  }
  // Next to the per-camera chart (the row makes them equally tall) the score histogram starts its
  // plot below the other's legend: both score axes and all grid lines on one line.
  const hist = scoreHist(s);
  if (hist) charts.push(withHist.length ? { ...hist, gridTop: LEGEND_GRID_TOP } : hist);
  return { stats, charts };
}

function syncModel(s: Summary): ViewModel {
  const Z = S().sync;
  const c = countsOf(s);
  const raw = seriesOf(s.verdicts);
  const v = Object.fromEntries((raw ?? []).map((i) => [i.name, i.value]));
  const stats: StatSpec[] = [];
  if (raw) {
    for (const k of ['misaligned', 'annotated', 'suspect', 'undecidable', 'aligned']) {
      if (k in v) stats.push({ label: Z.verdicts[k], value: v[k], foot: Z.verdictFoot[k], tone: k === 'misaligned' && v[k] ? 'bad' : undefined });
    }
  } else if (c) {
    stats.push({ label: Z.verdicts.misaligned, value: c.fail, tone: c.fail ? 'bad' : undefined }, { label: S().checked, value: checkedOf(c) });
  }
  if (num(s.flagged_camera_readings) !== null) stats.push({ label: Z.flagged, value: `${num(s.flagged_camera_readings)} ${Z.flaggedUnit}` });
  // the windowed comparison's coverage (registry 8.1): on how many episodes it could speak at all
  if (num(s.window_comparable_episodes) !== null && num(s.episodes_with_readings)) {
    stats.push({ label: Z.windowCoverage, value: `${num(s.window_comparable_episodes)}/${num(s.episodes_with_readings)}`, foot: Z.windowCoverageFoot });
  }
  const tol = num(s.lag_tol_s) ?? 0.25;
  const charts: ChartSpec[] = [];
  if (raw && anyValue(raw)) {
    const color: Record<string, string> = { aligned: GREEN, misaligned: RED, annotated: ORANGE, suspect: ORANGE, undecidable: CHART_COLORS[0] };
    charts.push({ key: 'verdicts', title: Z.verdictChart, items: raw.map((i) => ({ name: Z.verdicts[i.name] ?? i.name, value: i.value })), colors: raw.map((i) => color[i.name]), horizontal: true });
  }
  const cams = rowsOf<{ camera: string; readings?: number; n: number; median_lag_s: number | null; iqr_s: number | null; n_flagged: number; n_suspect: number; n_noisy?: number; n_abstained: number; n_window_stable?: number }>(s.cameras) ?? [];
  const measured = cams.filter((x) => num(x.median_lag_s) !== null);
  if (measured.length) {
    charts.push({
      key: 'lag',
      title: Z.lagChart,
      desc: Z.lagChartDesc(tol),
      items: measured.map((x) => ({ name: x.camera, value: x.median_lag_s! })),
      summary: measured.map((x) => Z.lagSummary(x.camera, signed(x.median_lag_s))).join('，'),
      horizontal: true,
      band: [-tol, tol],
      valueRange: lagRange(measured.map((x) => x.median_lag_s!), tol),
      height: Math.max(140, measured.length * 34 + 44),
    });
  }
  const blocks: ReactNode[] = [];
  if (cams.length) {
    blocks.push(
      <>
        <div className="section-sub">{Z.cameraTable}</div>
        <Table
          rowKey="camera"
          size="small"
          pagination={false}
          data={cams}
          data-testid="sync-cameras"
          columns={[
            { title: Z.cols.camera, dataIndex: 'camera', render: (v: string) => <span className="mono">{v}</span> },
            ...(cams.some((x) => typeof x.readings === 'number') ? [{ title: Z.cols.readings, dataIndex: 'readings', align: 'right' as const }] : []),
            { title: Z.cols.trusted, dataIndex: 'n', align: 'right' as const },
            { title: Z.cols.median, dataIndex: 'median_lag_s', align: 'right' as const, render: (x: number | null) => signed(x) },
            { title: Z.cols.iqr, dataIndex: 'iqr_s', align: 'right' as const, render: (x: number | null) => fmt(x) },
            { title: Z.cols.suspect, dataIndex: 'n_suspect', align: 'right' as const },
            { title: Z.cols.abstained, dataIndex: 'n_abstained', align: 'right' as const },
            ...(cams.some((x) => typeof x.n_window_stable === 'number') ? [{ title: Z.cols.windowStable, dataIndex: 'n_window_stable', align: 'right' as const }] : []),
            { title: Z.cols.flagged, dataIndex: 'n_flagged', align: 'right' as const },
          ]}
        />
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, margin: '6px 0 0' }}>
          {Z.note}
        </Typography.Paragraph>
      </>,
    );
  }
  const notes: ReactNode[] = [];
  if (str(s.sync_advice)) {
    notes.push(
      <span data-testid="sync-advice">
        <b>{Z.advice}：</b>
        {str(s.sync_advice)}
      </span>,
    );
  }
  if (num(s.negative_lag_episodes)) notes.push(<span style={{ color: 'var(--c-warning)' }}>{Z.negative(num(s.negative_lag_episodes)!)}</span>);
  return { stats, charts, blocks, notes };
}

function taskModel(s: Summary, section: LegacySection): ViewModel {
  const Z = S().task;
  const c = countsOf(s);
  const stats: StatSpec[] = [];
  if (c) {
    stats.push({ label: Z.checked, value: checkedOf(c) });
    stats.push({ label: Z.pass, value: c.pass, tone: 'good' });
    stats.push({ label: Z.fail, value: c.fail, tone: c.fail ? 'bad' : undefined, foot: section.adjudication?.appealable ? zh.report.goAppeal(section.adjudication.appealable) : undefined });
    stats.push({ label: Z.abstain, value: c.abstain, foot: Z.abstainFoot, tone: c.abstain ? 'warn' : undefined });
    if (c.error) stats.push({ label: Z.error, value: c.error, tone: 'warn' });
  }
  const sources = seriesOf(s.text_sources);
  if (sources?.length) stats.push({ label: Z.sources, value: <span style={{ fontSize: 15 }}>{pieces(sources, Z.sourceNames)}</span> });
  const charts: ChartSpec[] = [];
  const judgements = labelled(s.judgements, Z.judgement);
  if (anyValue(judgements)) charts.push({ key: 'judgements', title: Z.judgements, desc: Z.judgementsDesc, items: judgements, horizontal: true });
  const abstain = seriesOf(s.abstain_reason_counts) ?? seriesOf(s.abstain_reasons);
  if (anyValue(abstain)) charts.push({ key: 'abstain', title: S().abstainReasons, items: abstain, horizontal: true, colors: abstain.map(() => ORANGE) });
  const layers = labelled(s.layers, Z.layer);
  if (anyValue(layers)) charts.push({ key: 'layers', title: Z.layers, desc: Z.layersDesc, items: layers });
  const blocks = [
    <>
      <div className="section-sub">{Z.method}</div>
      <ul className="section-lines">
        {Z.methodLines.map((l) => (
          <li key={l}>{l}</li>
        ))}
      </ul>
    </>,
  ];
  return { stats, charts, blocks };
}

/** 重复检测 (design doc 26 §1, D87): the groups of identical episodes and how many episodes they hold - reported
 * only, nothing removed. */
function duplicateModel(s: Summary): ViewModel {
  const Z = S().duplicate_check;
  const c = countsOf(s);
  const stats: StatSpec[] = [];
  if (c) stats.push({ label: S().checked, value: checkedOf(c) });
  if (num(s.groups) !== null) stats.push({ label: Z.groups, value: num(s.groups) });
  if (num(s.duplicated) !== null) stats.push({ label: Z.duplicated, value: num(s.duplicated), foot: Z.duplicatedFoot, tone: num(s.duplicated) ? 'warn' : undefined });
  const charts: ChartSpec[] = [];
  const sizes = seriesOf(s.group_sizes, Z.size);
  if (anyValue(sizes)) charts.push({ key: 'sizes', title: Z.sizes, items: sizes });
  const notes: ReactNode[] = [];
  if (!num(s.groups)) notes.push(Z.none);
  return { stats, charts, notes };
}

/** The opinions (design doc 25 §7, D81): the episodes by label and by confidence, the conflicts asked, what single
 * sources missed and why some could not be told. */
function eefOpinions(s: Summary, section: LegacySection, stats: StatSpec[], charts: ChartSpec[]): void {
  const V = S().eef.v5;
  const O = zh.eefDetail.output;
  const raw = (rowsOf<{ name: string; count: number }>(s.labels) ?? []).reduce<Record<string, number>>((acc, x) => ({ ...acc, [x.name]: x.count }), {});
  const n = (k: string) => raw[k] ?? 0;
  stats.push({ label: O.label.inconsistent, value: n('inconsistent'), tone: n('inconsistent') ? 'warn' : undefined });
  stats.push({ label: O.label.possibly_inconsistent, value: n('possibly_inconsistent') });
  stats.push({ label: O.label.consistent, value: n('consistent') });
  stats.push({ label: O.label.cannot_tell, value: n('cannot_tell') });
  const conflicts = num(s.conflict_episodes) ?? 0;
  stats.push({ label: V.conflicts, value: conflicts, tone: conflicts ? 'warn' : undefined, foot: V.conflictsFoot(section.adjudication?.pending ?? 0) });
  const labels = labelled(s.labels, O.label);
  if (anyValue(labels)) charts.push({ key: 'labels', title: V.labelsChart, items: labels, colors: labels.map((x) => (x.name === O.label.inconsistent ? RED : x.name === O.label.possibly_inconsistent ? ORANGE : undefined)) });
  const bins = seriesOf(s.p_bins);
  if (anyValue(bins)) charts.push({ key: 'p-bins', title: V.pChart, desc: V.pChartDesc, items: bins!, colors: bins!.map((b) => (b.name.startsWith('<') || b.name.startsWith('0.2') ? undefined : ORANGE)) });
  const bySub = labelled(s.inconsistent_by_subitem, O.subitem);
  if (anyValue(bySub)) charts.push({ key: 'by-subitem', title: V.bySubitem, items: bySub, horizontal: true, colors: bySub.map(() => ORANGE) });
  const missing = labelled(s.single_source_missing, O.missing);
  if (anyValue(missing)) charts.push({ key: 'single-source', title: V.missingChart, desc: V.missingDesc, items: missing, horizontal: true });
  const cannot = seriesOf(s.cannot_tell_reasons);
  if (anyValue(cannot)) charts.push({ key: 'cannot-tell', title: V.cannotChart, items: cannot!, horizontal: true });
}

/** The EEF module (registry 5.0 on): its opinions, then the CPU's and the model review's readings, the trajectory
 * against the dataset's own record and the wrist cameras' own motion - the last two reported only. */
function eefModel(s: Summary, section: LegacySection): ViewModel {
  const Z = S().eef;
  const R = Z.review;
  const stats: StatSpec[] = [];
  const charts: ChartSpec[] = [];
  eefOpinions(s, section, stats, charts);
  if (num(s.model_cpu_agreement) !== null)
    stats.push({ label: Z.agreement, value: `${Math.round(num(s.model_cpu_agreement)! * 100)}%`, foot: Z.agreementFoot(num(s.model_votes) ?? 0) });
  if (num(s.windows) !== null) stats.push({ label: R.windows, value: num(s.windows), foot: Z.windowsValue(num(s.windows_answered) ?? 0, num(s.windows_failed) ?? 0) });
  if (num(s.coverage_median) !== null) stats.push({ label: Z.coverage, value: fmt(num(s.coverage_median)), foot: Z.coverageValue(fmt(num(s.coverage_median)), fmt(num(s.coverage_min))) });
  for (const k of ['tracking_suspect', 'vlm_requests', 'truncated_episodes']) if (num(s[k])) stats.push({ label: zh.summaryKeys[k] ?? k, value: num(s[k]) });
  if (str(s.threshold_profile)) stats.push({ label: zh.summaryKeys.threshold_profile ?? 'threshold_profile', value: <span style={{ fontSize: 15 }}>{str(s.threshold_profile)}</span> });
  // the upload against the dataset's own record (design doc 12 §8.7, D-E16): reported only
  const RC = Z.record;
  const RD = zh.eefDetail.record;
  if (num(s.record_compared) !== null) {
    stats.push({ label: RC.compared, value: num(s.record_compared), foot: RC.comparedFoot });
    stats.push({ label: RC.suspect, value: num(s.record_suspect) ?? 0, tone: num(s.record_suspect) ? 'warn' : undefined, foot: RC.suspectFoot });
    if (num(s.record_lag_median_frames) !== null) stats.push({ label: RC.lag, value: RC.lagValue(fmt(num(s.record_lag_median_frames))) });
    if (num(s.record_internal_inconsistent)) stats.push({ label: RC.internal, value: num(s.record_internal_inconsistent), tone: 'warn', foot: RC.internalFoot });
  }
  // a wrist camera's own motion against its poses (design doc 22 §5.3): reported only
  const EG = Z.ego;
  if (num(s.ego_motion_episodes) !== null) {
    stats.push({ label: EG.episodes, value: num(s.ego_motion_episodes), foot: EG.episodesFoot });
    stats.push({ label: EG.suspect, value: num(s.ego_motion_suspect) ?? 0, tone: num(s.ego_motion_suspect) ? 'warn' : undefined, foot: EG.suspectFoot });
    if (num(s.ego_motion_unknown)) stats.push({ label: EG.unknown, value: num(s.ego_motion_unknown) });
    if (num(s.ego_motion_lag_median_s) !== null) stats.push({ label: EG.lag, value: EG.lagValue(fmt(num(s.ego_motion_lag_median_s))) });
  }
  const egoBands = labelled(s.ego_motion_bands, EG.band);
  if (anyValue(egoBands)) charts.push({ key: 'ego-bands', title: EG.bandsChart, items: egoBands, colors: egoBands.map((b) => (b.name === EG.band.minor ? undefined : ORANGE)) });
  const egoReasons = labelled(s.ego_motion_reasons, EG.reason);
  if (anyValue(egoReasons)) charts.push({ key: 'ego-reasons', title: EG.reasonsChart, items: egoReasons, horizontal: true });
  const aspects = labelled(s.opinion_aspects, Z.opinion.aspect);
  if (anyValue(aspects)) charts.push({ key: 'opinion-aspects', title: Z.opinion.aspectChart, items: aspects!, horizontal: true });
  const suspect = labelled(s.suspect_by_subitem, Z.subitem);
  if (anyValue(suspect)) charts.push({ key: 'suspect', title: zh.summaryKeys.suspect_by_subitem ?? '', items: suspect, colors: suspect.map(() => ORANGE) });
  const classes = labelled(s.review_classes, Z.reviewClasses);
  if (anyValue(classes)) charts.push({ key: 'classes', title: zh.summaryKeys.review_classes ?? '', items: classes });
  const failures = seriesOf(s.failure_codes);
  if (anyValue(failures)) charts.push({ key: 'failures', title: zh.summaryKeys.failure_codes ?? '', items: failures, horizontal: true, colors: failures.map(() => ORANGE) });
  const hyps = seriesOf(s.supported_hypotheses);
  if (anyValue(hyps)) charts.push({ key: 'hypotheses', title: Z.hypotheses, items: hyps, horizontal: true });
  // "joints:constant_mismatch" -> 关节角正解 · 恒定差与映射声明的关系不符
  const recordReasons = seriesOf(s.record_reasons, (k) => {
    const [src, why] = k.includes(':') ? [k.slice(0, k.indexOf(':')), k.slice(k.indexOf(':') + 1)] : ['', k];
    return [RD.source[src] ?? src, RD.reason[why] ?? why].filter(Boolean).join(' · ');
  });
  if (anyValue(recordReasons)) charts.push({ key: 'record-reasons', title: RC.reasonsChart, items: recordReasons, horizontal: true, colors: recordReasons.map(() => ORANGE) });
  const blocks: ReactNode[] = [];
  const matrix = s.subitem_status && typeof s.subitem_status === 'object' ? (s.subitem_status as Record<string, Record<string, number>>) : null;
  if (matrix && Object.keys(matrix).length) {
    const statuses = [...new Set(Object.values(matrix).flatMap((row) => Object.keys(row ?? {})))];
    const rows = Object.entries(matrix).map(([k, row]) => ({ key: k, name: Z.subitem[k] ?? k, ...Object.fromEntries(statuses.map((st) => [st, row?.[st] ?? 0])) }));
    blocks.push(
      <>
        <div className="section-sub">
          {Z.matrix}
          <span className="muted">{Z.matrixDesc}</span>
        </div>
        <Table
          rowKey="key"
          size="small"
          pagination={false}
          data={rows}
          data-testid="eef-matrix"
          columns={[{ title: '', dataIndex: 'name' }, ...statuses.map((st) => ({ title: Z.status[st] ?? st, dataIndex: st, align: 'right' as const }))]}
        />
      </>,
    );
  }
  const bySource = s.record_by_source && typeof s.record_by_source === 'object' ? (s.record_by_source as Record<string, Record<string, number>>) : null;
  if (bySource && Object.keys(bySource).length) {
    const statuses = ['ok', 'suspect', 'unknown', 'unsupported', 'error'].filter((st) => Object.values(bySource).some((row) => row?.[st]));
    const rows = Object.entries(bySource).map(([k, row]) => ({ key: k, name: RD.source[k] ?? k, ...Object.fromEntries(statuses.map((st) => [st, row?.[st] ?? 0])) }));
    blocks.push(
      <>
        <div className="section-sub">
          {RC.bySource}
          <span className="muted">{RC.bySourceDesc}</span>
        </div>
        <Table
          rowKey="key"
          size="small"
          pagination={false}
          data={rows}
          data-testid="eef-record-sources"
          columns={[{ title: '', dataIndex: 'name' }, ...statuses.map((st) => ({ title: RD.status[st] ?? st, dataIndex: st, align: 'right' as const }))]}
        />
      </>,
    );
  }
  const fixed = Array.isArray(s.declared_fixed_cameras) ? (s.declared_fixed_cameras as unknown[]).map(String) : [];
  const notes: ReactNode[] = [
    ...(fixed.length ? [<span data-testid="eef-declared-fixed-note">{Z.declaredFixed(fixed.join('、'))}</span>] : []),
    // a wrist camera's own hand stays still in its picture (design doc 25 §5.4)
    ...(num(s.ego_motion_episodes) ? [<span className="muted" data-testid="eef-wrist-note">{zh.viz.overlay.wristNote}</span>] : []),
    <span className="muted">
      {Z.v5.note}
      {s.confidence_uncalibrated ? Z.v5.uncalibrated : ''}
    </span>,
  ];
  return { stats, charts, blocks, notes };
}

/** 数据完整性 (design doc 14 §5.1): outcomes, what was read, the findings by kind, the dataset's own. */
function integrityModel(s: Summary, section: LegacySection): ViewModel {
  const Z = S().integrity;
  const o = (s.integrity_outcomes ?? {}) as Record<string, unknown>;
  const f = (s.integrity_files ?? {}) as Record<string, unknown>;
  const t = (s.integrity_tiers ?? {}) as Record<string, unknown>;
  const stats: StatSpec[] = [];
  if (num(o.pass) !== null) stats.push({ label: Z.pass, value: num(o.pass) });
  if (num(o.reject) !== null) stats.push({ label: Z.reject, value: num(o.reject), tone: num(o.reject) ? 'bad' : undefined, foot: num(o.reject) ? Z.rejectFoot : undefined });
  if (num(o.suspect) !== null)
    stats.push({ label: Z.suspect, value: num(o.suspect), tone: num(o.suspect) ? 'warn' : undefined, foot: num(o.suspect) ? Z.suspectFoot(section.adjudication?.pending ?? 0) : undefined });
  if (num(f.files) !== null) stats.push({ label: Z.files, value: num(f.files), foot: Z.filesFoot(((num(f.bytes) ?? 0) / 1e6).toFixed(1)) });
  if (num(f.crc_files) !== null) stats.push({ label: Z.crc, value: num(f.crc_files), foot: Z.crcFoot });
  if (Object.keys(t).length) stats.push({ label: Z.fullRead, value: t.L2 ? Z.on : Z.off, foot: t.L2 ? undefined : Z.fullReadOffFoot });
  if (Object.keys(t).length) stats.push({ label: Z.decode, value: t.L3 ? Z.on : Z.off });
  const charts: ChartSpec[] = [];
  const rows = rowsOf<{ code: string; name?: string; level?: string; count: number }>(s.integrity_codes) ?? [];
  const items = rows.map((r) => ({ name: zh.integrityCodes[r.code] ?? r.name ?? r.code, value: r.count }));
  if (anyValue(items)) charts.push({ key: 'codes', title: Z.codes, desc: Z.codesDesc, items, horizontal: true, colors: rows.map((r) => (r.level === 'reject' ? RED : ORANGE)) });
  const dataset = rowsOf<{ code: string; message: string }>(s.integrity_dataset) ?? [];
  const blocks: ReactNode[] = dataset.length
    ? [
        <>
          <div className="section-sub">{Z.dataset}</div>
          <ul className="section-list" data-testid="integrity-dataset">
            {dataset.map((d, i) => (
              <li key={i}>
                {zh.integrityCodes[d.code] ?? d.code}：{d.message}
              </li>
            ))}
          </ul>
        </>,
      ]
    : [];
  const notes: ReactNode[] = [];
  if (!items.length && !dataset.length && Object.keys(o).length) notes.push(Z.none);
  notes.push(<span className="muted">{Z.note}</span>);
  return { stats, charts, blocks, notes };
}

// -------------------------------------------------------------------------- the registry

type Model = (s: Summary, section: LegacySection) => ViewModel;

function view(id: string, model: Model, opts: ViewOpts = {}): ComponentType<SectionViewProps> {
  function SectionView({ section }: SectionViewProps) {
    const s = (section.summary ?? {}) as Summary;
    return <ModelView id={section.id} model={withFallback(s, commonCharts(s, model(s, section), opts))} />;
  }
  SectionView.displayName = `SectionView(${id})`;
  return SectionView;
}

/**
 * A module's own view by module id (06 §6.2): the checks carried over from v1, duplicate check, the EEF module and the
 * data integrity module. A module without an entry has the findings statistics alone.
 */
export const SECTION_VIEWS: Record<string, ComponentType<SectionViewProps>> = {
  data_integrity: view('data_integrity', integrityModel),
  timestamp_check: view('timestamp_check', timestampModel),
  kinematic_limits: view('kinematic_limits', kinematicsModel),
  motion_quality: view('motion_quality', motionModel),
  visual_quality: view('visual_quality', visualModel),
  video_action_sync: view('video_action_sync', syncModel),
  task_success: view('task_success', taskModel, { abstain: false }),
  duplicate_check: view('duplicate_check', duplicateModel),
  // a person is asked only on a conflict, and why an episode could not be told has its own chart: no abstention chart
  eef_video_consistency: view('eef_video_consistency', eefModel, { abstain: false }),
};

