// The report's module sections (06 §6.2, F6.2): for every module a row of key figures and one to
// three charts drawn from its summary's chart-ready aggregates - statistics only, never a list of
// episodes (those are in the Episode tab) and never JSON. A module without an entry here gets the
// default view: its verdict distribution, its scalars, its series and dicts of counts as charts,
// anything else as readable lines - so a new module needs no frontend change. Reports written
// before the chart-ready aggregates have only counts and a few scalars: the views show what there
// is, with a short note.
import { Table, Typography } from '@arco-design/web-react';
import type { ComponentType, ReactNode } from 'react';
import type { ReportModuleSection } from '../../api/types';
import { CHART_COLORS, Chart, barOption, chartSummary, groupedBarOption, type ChartOption } from '../../components/Chart';
import { LazyVisible } from '../../components/LazyVisible';
import { percent } from '../../lib/format';
import { fieldLabel, readable } from '../../lib/reportView';
import { anyValue, countsOf, fmt, hasAny, num, rowsOf, seriesOf, signed, str, verdictItems, type Item, type Summary } from '../../lib/sectionStats';
import { formatScalar, splitSummary } from '../../lib/summary';
import { zh } from '../../locales/zh';

export interface SectionViewProps {
  taskId: string;
  rev: number;
  section: ReportModuleSection;
}

interface StatSpec {
  label: string;
  value: ReactNode;
  foot?: ReactNode;
  tone?: 'bad' | 'warn' | 'good';
}

interface ChartSpec {
  key: string;
  title: string;
  desc?: string;
  items?: Item[];
  horizontal?: boolean;
  colors?: (string | undefined)[];
  band?: [number, number];
  valueName?: string;
  /** A ready option (grouped bars...) with its accessible summary. */
  option?: ChartOption;
  summary?: string;
  height?: number;
}

interface ViewModel {
  stats: StatSpec[];
  charts: ChartSpec[];
  blocks?: ReactNode[];
  notes?: ReactNode[];
  /** The summary has the chart-ready aggregates (written by a report after 06 §6.2's). */
  fresh: boolean;
}

const S = () => zh.sections;
const GREEN = '#00B42A';
const RED = '#F53F3F';
const ORANGE = '#FF7D00';

// -------------------------------------------------------------------------- building blocks

function Stat({ label, value, foot, tone }: StatSpec) {
  return (
    <div className={`stat-cell${tone ? ` tone-${tone}` : ''}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {foot ? <div className="stat-foot">{foot}</div> : null}
    </div>
  );
}

function chartHeight(c: ChartSpec): number {
  if (c.height) return c.height;
  const n = c.items?.length ?? 6;
  return c.horizontal ? Math.max(140, Math.min(420, n * 30 + 44)) : 220;
}

function ChartBlock({ spec }: { spec: ChartSpec }) {
  const option = spec.option ?? barOption(spec.items ?? [], { horizontal: spec.horizontal, colors: spec.colors, band: spec.band, valueName: spec.valueName });
  const summary = `${spec.title}：${spec.summary ?? chartSummary(spec.items ?? [])}`;
  return (
    <div className="section-chart" data-testid={`chart-${spec.key}`}>
      <div className="section-sub">
        {spec.title}
        {spec.desc ? <span className="muted">{spec.desc}</span> : null}
      </div>
      <LazyVisible placeholder={<div style={{ height: chartHeight(spec) }} />}>
        <Chart option={option} summary={summary} height={chartHeight(spec)} />
      </LazyVisible>
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
      {!model.fresh ? (
        <div className="section-note muted" data-testid={`old-report-${id}`}>
          {zh.reportPage.oldReport}
        </div>
      ) : null}
    </div>
  );
}

/** Items labelled through a table of Chinese names (unknown codes stay as they are). */
function labelled(v: unknown, names: Record<string, string>): Item[] | null {
  return seriesOf(v, (n) => names[n] ?? n);
}

/** The charts every verdict module may add: why it abstained, where the errors stopped. */
function commonCharts(s: Summary, model: ViewModel, opts: { abstain?: boolean } = {}): ViewModel {
  const charts = [...model.charts];
  const abstain = seriesOf(s.abstain_reason_counts);
  if (opts.abstain !== false && anyValue(abstain) && !charts.some((c) => c.key === 'abstain')) {
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

const pieces = (items: Item[] | null, names: Record<string, string> = {}) => (items ?? []).map((i) => `${names[i.name] ?? i.name} ${i.value}`).join(' · ');

// -------------------------------------------------------------------------- the modules

function timestampModel(s: Summary): ViewModel {
  const Z = S().timestamp;
  const c = countsOf(s);
  const raw = seriesOf(s.fail_reasons) ?? seriesOf(s.fail_kinds) ?? [];
  const byKind = Object.fromEntries(raw.map((i) => [i.name, i.value]));
  const stats: StatSpec[] = [];
  if (c) stats.push({ label: Z.failed, value: c.fail, tone: c.fail ? 'bad' : undefined });
  for (const k of ['out_of_order', 'gap', 'fragment', 'jitter']) if (k in byKind) stats.push({ label: Z.kinds[k], value: byKind[k] });
  if (c) stats.push({ label: S().checked, value: c.total });
  if (num(s.duration_total_s) !== null) stats.push({ label: Z.durationTotal, value: zh.time.duration(num(s.duration_total_s)) });
  if (num(s.duration_median_s) !== null) stats.push({ label: Z.durationMedian, value: Z.seconds(num(s.duration_median_s)!) });
  if (num(s.duration_min_s) !== null && num(s.duration_max_s) !== null) stats.push({ label: Z.durationRange, value: `${Z.seconds(num(s.duration_min_s)!)} / ${Z.seconds(num(s.duration_max_s)!)}` });
  const charts: ChartSpec[] = [];
  const reasons = raw.map((i) => ({ name: Z.kinds[i.name] ?? i.name, value: i.value }));
  if (anyValue(reasons)) charts.push({ key: 'fail', title: Z.failChart, items: reasons, colors: reasons.map(() => RED) });
  const hist = seriesOf(s.duration_hist);
  if (hist?.length) charts.push({ key: 'duration', title: Z.durationChart, desc: Z.durationDesc, items: hist });
  return { stats, charts, fresh: hasAny(s, ['fail_reasons', 'duration_hist']) };
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
    stats.push({ label: S().checked, value: c.total });
  }
  if (str(s.limits_profile)) stats.push({ label: Z.profile, value: str(s.limits_profile) });
  const charts: ChartSpec[] = [];
  const byType = labelled(s.violations_by_type, Z.types);
  if (anyValue(byType)) charts.push({ key: 'type', title: Z.byType, desc: Z.byDesc, items: byType, horizontal: true, colors: byType.map(() => RED) });
  const byJoint = seriesOf(s.violations_by_joint, Z.joint);
  if (anyValue(byJoint)) charts.push({ key: 'joint', title: Z.byJoint, desc: Z.byDesc, items: byJoint });
  const notes = episodes === 0 ? [Z.none] : [];
  return { stats, charts, notes, fresh: hasAny(s, ['violation_episodes', 'violations_by_type']) };
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
    });
  }
  const hist = scoreHist(s);
  if (hist) charts.push(hist);
  const idle = labelled(s.idle_episodes, Z.idleKinds);
  if (anyValue(idle)) charts.push({ key: 'idle', title: Z.idle, desc: Z.idleDesc, items: idle });
  const notes = subs
    .filter((x) => x.na > 0)
    .map((x) => (num(x.mean) === null ? Z.notApplicable(Z.sub[x.name] ?? x.name, x.na_reason ?? Z.naGeneric) : Z.partlyApplicable(Z.sub[x.name] ?? x.name, x.na, x.na_reason ?? Z.naGeneric)));
  return { stats, charts, notes, fresh: hasAny(s, ['subscores', 'score_hist']) };
}

function visualModel(s: Summary): ViewModel {
  const Z = S().visual;
  const c = countsOf(s);
  const cams = rowsOf<{ camera: string; n: number; mean: number | null; low: number; placeholder: number; hist: number[]; weight?: number }>(s.cameras) ?? [];
  const stats: StatSpec[] = [];
  if (num(s.mean_score) !== null) stats.push({ label: S().score.mean, value: fmt(num(s.mean_score)) });
  if (c) stats.push({ label: S().checked, value: c.total, foot: cams.length ? Z.perCamera(cams.length) : undefined });
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
    const series = withHist.map((x) => ({ name: Z.cameraMean(x.camera, fmt(x.mean)), data: x.hist }));
    charts.push({ key: 'cameras', title: Z.cameraChart, desc: Z.cameraChartDesc, option: groupedBarOption(bins, series), summary: withHist.map((x) => Z.cameraSummary(x.camera, fmt(x.mean), x.low)).join('；'), height: 240 });
  }
  const hist = scoreHist(s);
  if (hist) charts.push(hist);
  return { stats, charts, fresh: hasAny(s, ['cameras', 'score_hist']) };
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
    stats.push({ label: Z.verdicts.misaligned, value: c.fail, tone: c.fail ? 'bad' : undefined }, { label: S().checked, value: c.total });
  }
  if (num(s.flagged_camera_readings) !== null) stats.push({ label: Z.flagged, value: `${num(s.flagged_camera_readings)} ${Z.flaggedUnit}` });
  const tol = num(s.lag_tol_s) ?? 0.25;
  const charts: ChartSpec[] = [];
  if (raw && anyValue(raw)) {
    const color: Record<string, string> = { aligned: GREEN, misaligned: RED, annotated: ORANGE, suspect: ORANGE, undecidable: CHART_COLORS[0] };
    charts.push({ key: 'verdicts', title: Z.verdictChart, items: raw.map((i) => ({ name: Z.verdicts[i.name] ?? i.name, value: i.value })), colors: raw.map((i) => color[i.name]), horizontal: true });
  }
  const cams = rowsOf<{ camera: string; readings?: number; n: number; median_lag_s: number | null; iqr_s: number | null; n_flagged: number; n_suspect: number; n_noisy?: number; n_abstained: number }>(s.cameras) ?? [];
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
  return { stats, charts, blocks, notes, fresh: hasAny(s, ['verdicts', 'cameras']) };
}

function taskModel(s: Summary, section: ReportModuleSection): ViewModel {
  const Z = S().task;
  const c = countsOf(s);
  const stats: StatSpec[] = [];
  if (c) {
    stats.push({ label: Z.checked, value: c.total });
    stats.push({ label: Z.pass, value: c.pass, tone: 'good' });
    stats.push({ label: Z.fail, value: c.fail, tone: c.fail ? 'bad' : undefined, foot: section.adjudication?.appealable ? zh.report.goAppeal(section.adjudication.appealable) : undefined });
    stats.push({ label: Z.abstain, value: c.abstain, foot: Z.abstainFoot, tone: c.abstain ? 'warn' : undefined });
    if (c.error) stats.push({ label: Z.error, value: c.error, tone: 'warn' });
  }
  const sources = seriesOf(s.text_sources);
  if (sources?.length) stats.push({ label: Z.sources, value: <span style={{ fontSize: 15 }}>{pieces(sources, Z.sourceNames)}</span> });
  const arb = s.arbitration && typeof s.arbitration === 'object' ? (s.arbitration as Record<string, unknown>) : null;
  if (arb && num(arb.triggered)) stats.push({ label: Z.arbitration, value: num(arb.triggered), foot: Z.arbitrationValue(num(arb.triggered) ?? 0, num(arb.adopted_success) ?? 0, num(arb.adopted_failure) ?? 0, num(arb.abstained) ?? 0) });
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
  return { stats, charts, blocks, fresh: hasAny(s, ['judgements', 'layers']) };
}

function dedupModel(s: Summary): ViewModel {
  const Z = S().dedup;
  const c = countsOf(s);
  const stats: StatSpec[] = [];
  if (c) stats.push({ label: S().checked, value: c.total, foot: Z.checkedFoot });
  if (num(s.collision_groups) !== null) stats.push({ label: Z.groups, value: num(s.collision_groups) });
  if (num(s.removed) !== null) stats.push({ label: Z.removed, value: num(s.removed), tone: num(s.removed) ? 'warn' : undefined });
  const charts: ChartSpec[] = [];
  const sizes = seriesOf(s.group_sizes, Z.size);
  if (anyValue(sizes)) charts.push({ key: 'sizes', title: Z.sizes, items: sizes });
  const notes: ReactNode[] = [];
  if (sizes && !sizes.length) notes.push(Z.none);
  notes.push(<span className="muted">{Z.note}</span>);
  return { stats, charts, notes, fresh: hasAny(s, ['group_sizes']) };
}

function skillModel(s: Summary): ViewModel {
  const Z = S().skill;
  const c = countsOf(s);
  const stats: StatSpec[] = [];
  if (c) stats.push({ label: Z.covered, value: c.total, foot: Z.coveredFoot });
  if (num(s.families) !== null) stats.push({ label: Z.families, value: num(s.families) });
  if (num(s.subskills) !== null) stats.push({ label: Z.subskills, value: num(s.subskills) });
  if (Array.isArray(s.undersampled)) stats.push({ label: Z.undersampled, value: s.undersampled.length, foot: s.undersampled.length ? s.undersampled.map(String).join('、') : undefined, tone: s.undersampled.length ? 'warn' : undefined });
  if (num(s.label_disagreements) !== null) stats.push({ label: Z.disagreements, value: num(s.label_disagreements), foot: Z.disagreementFoot(num(s.disagreement_high) ?? 0, num(s.disagreement_review) ?? 0), tone: num(s.label_disagreements) ? 'warn' : undefined });
  if (num(s.unstable) !== null) stats.push({ label: Z.unstable, value: num(s.unstable), foot: Z.unstableFoot });
  const grouping = seriesOf(s.grouping_sources);
  if (grouping?.length) stats.push({ label: Z.groupingSources, value: <span style={{ fontSize: 15 }}>{pieces(grouping, S().task.sourceNames)}</span> });
  const charts: ChartSpec[] = [];
  const families = seriesOf(s.family_distribution);
  if (anyValue(families)) charts.push({ key: 'families', title: Z.familyChart, desc: Z.familyChartDesc, items: families, horizontal: true });
  const tree = rowsOf<{ name: string; subskills?: { name: string; count: number }[] }>(s.family_tree) ?? [];
  const subs = tree.flatMap((f) => (f.subskills ?? []).map((x) => ({ name: `${f.name} › ${x.name}`, value: x.count })));
  if (anyValue(subs)) charts.push({ key: 'subskills', title: Z.subskillChart, desc: Z.subskillChartDesc, items: subs, horizontal: true, colors: subs.map(() => CHART_COLORS[1]) });
  return { stats, charts, fresh: hasAny(s, ['family_distribution', 'family_tree']) };
}

function eefModel(s: Summary): ViewModel {
  const Z = S().eef;
  const stats: StatSpec[] = [];
  for (const k of ['candidates', 'assessed', 'partially_assessable', 'not_assessable', 'errors']) {
    if (num(s[k]) !== null) stats.push({ label: Z.overall[k], value: num(s[k]), tone: k === 'candidates' && num(s[k]) ? 'warn' : undefined });
  }
  if (num(s.coverage_median) !== null) stats.push({ label: Z.coverage, value: fmt(num(s.coverage_median)), foot: Z.coverageValue(fmt(num(s.coverage_median)), fmt(num(s.coverage_min))) });
  if (num(s.cameras_measured) !== null) stats.push({ label: zh.summaryKeys.cameras_measured ?? 'cameras_measured', value: num(s.cameras_measured) });
  if (str(s.threshold_profile)) stats.push({ label: zh.summaryKeys.threshold_profile ?? 'threshold_profile', value: <span style={{ fontSize: 15 }}>{str(s.threshold_profile)}</span> });
  const charts: ChartSpec[] = [];
  const overall = ['candidates', 'assessed', 'partially_assessable', 'not_assessable', 'errors'].filter((k) => num(s[k]) !== null).map((k) => ({ name: Z.overall[k], value: num(s[k])! }));
  if (anyValue(overall)) charts.push({ key: 'overall', title: Z.overallChart, items: overall, horizontal: true });
  const suspect = labelled(s.suspect_by_subitem, Z.subitem);
  if (anyValue(suspect)) charts.push({ key: 'suspect', title: zh.summaryKeys.suspect_by_subitem ?? '', items: suspect, colors: suspect.map(() => ORANGE) });
  const hyps = seriesOf(s.supported_hypotheses);
  if (anyValue(hyps)) charts.push({ key: 'hypotheses', title: Z.hypotheses, items: hyps, horizontal: true });
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
  const notes: ReactNode[] = [
    <span className="muted">
      {Z.advisory}
      {s.uncalibrated ? Z.uncalibrated : ''}
    </span>,
  ];
  return { stats, charts, blocks, notes, fresh: true };
}

function eefReviewModel(s: Summary): ViewModel {
  const Z = S().eef;
  const R = Z.review;
  const stats: StatSpec[] = [];
  const keys: [string, string][] = [
    ['reviewed', R.reviewed],
    ['incomplete', R.incomplete],
    ['not_reviewed', R.not_reviewed],
    ['errors', Z.overall.errors],
    ['needs_human', R.needs_human],
    ['conflicts', R.conflicts],
  ];
  for (const [k, label] of keys) if (num(s[k]) !== null) stats.push({ label, value: num(s[k]), tone: (k === 'needs_human' || k === 'conflicts') && num(s[k]) ? 'warn' : undefined });
  if (num(s.windows) !== null) stats.push({ label: R.windows, value: num(s.windows), foot: Z.windowsValue(num(s.windows_answered) ?? 0, num(s.windows_failed) ?? 0) });
  for (const k of ['tracking_suspect', 'vlm_requests', 'cache_hits', 'truncated_episodes']) if (num(s[k]) !== null) stats.push({ label: zh.summaryKeys[k] ?? k, value: num(s[k]) });
  const charts: ChartSpec[] = [];
  const status = keys.slice(0, 4).filter(([k]) => num(s[k]) !== null).map(([k, label]) => ({ name: label, value: num(s[k])! }));
  if (anyValue(status)) charts.push({ key: 'status', title: zh.summaryKeys.reviewed ?? '', items: status, horizontal: true });
  const classes = labelled(s.review_classes, Z.reviewClasses);
  if (anyValue(classes)) charts.push({ key: 'classes', title: zh.summaryKeys.review_classes ?? '', items: classes });
  const failures = seriesOf(s.failure_codes);
  if (anyValue(failures)) charts.push({ key: 'failures', title: zh.summaryKeys.failure_codes ?? '', items: failures, horizontal: true, colors: failures.map(() => ORANGE) });
  return { stats, charts, notes: [<span className="muted">{Z.advisory}</span>], fresh: true };
}

/** Any module: scalars, the verdict distribution, series and dicts of counts, the rest readable. */
function defaultModel(s: Summary): ViewModel {
  const { scalars, series, other } = splitSummary(s, ['counts']);
  const c = countsOf(s);
  const charts: ChartSpec[] = [];
  const verdicts = c ? verdictItems(c) : [];
  if (verdicts.length) charts.push({ key: 'counts', title: S().counts, items: verdicts });
  for (const x of series) charts.push({ key: x.key, title: x.label, items: x.items, horizontal: x.items.length > 6 });
  const blocks: ReactNode[] = other.length
    ? [
        <dl className="desc-grid" data-testid="section-other">
          {other.map(([k, v]) => (
            <div key={k} className="desc-item full">
              <dt>{fieldLabel(k)}</dt>
              <dd>{readable(v)}</dd>
            </div>
          ))}
        </dl>,
      ]
    : [];
  return { stats: scalars.map((x) => ({ label: x.label, value: formatScalar(x.value) })), charts, blocks, fresh: true };
}

// -------------------------------------------------------------------------- the registry

type Model = (s: Summary, section: ReportModuleSection) => ViewModel;

function view(id: string, model: Model, opts: { abstain?: boolean } = {}): ComponentType<SectionViewProps> {
  function SectionView({ section }: SectionViewProps) {
    const s = (section.summary ?? {}) as Summary;
    return <ModelView id={section.id} model={withFallback(s, commonCharts(s, model(s, section), opts))} />;
  }
  SectionView.displayName = `SectionView(${id})`;
  return SectionView;
}

/**
 * Section renderers by module id (06 §6.2): the eight v1 modules and the two EEF modules. A
 * module without an entry gets DefaultSectionView.
 */
export const SECTION_VIEWS: Record<string, ComponentType<SectionViewProps>> = {
  timestamp_check: view('timestamp_check', timestampModel),
  kinematic_limits: view('kinematic_limits', kinematicsModel),
  motion_quality: view('motion_quality', motionModel),
  visual_quality: view('visual_quality', visualModel),
  video_action_sync: view('video_action_sync', syncModel),
  task_success: view('task_success', taskModel, { abstain: false }),
  dedup: view('dedup', dedupModel),
  skill_profile: view('skill_profile', skillModel),
  eef_video_consistency: view('eef_video_consistency', eefModel),
  eef_video_review: view('eef_video_review', eefReviewModel),
};

export const DefaultSectionView = view('default', defaultModel);
