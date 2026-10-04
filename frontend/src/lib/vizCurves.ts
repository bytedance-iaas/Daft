// Curve cells of the player (design doc 18 §5.2): the legend (one entry per name, state solid and
// action dashed in the same colour), the y range, SVG paths with gaps, the value under the cursor
// and axis ticks. Pure functions over the C4 `VizSeries`.
import type { VizSeries } from '../api/types';
import { bisectRight } from './vizTime';

/** Arco blue / cyan / orange / purple / green / magenta / gold / lime, in turn. */
export const PALETTE = ['#165DFF', '#14C9C9', '#FF7D00', '#722ED1', '#00B42A', '#F5319D', '#F7BA1E', '#9FDB1D'] as const;
/** Camera dots: the eight above, then their darker shades - sixteen cameras without a repeat (design doc 19 §2.2). */
export const CAMERA_PALETTE = [...PALETTE, '#0E42D2', '#0DA5AA', '#D25F00', '#551DB0', '#009A29', '#CB1E83', '#CC9213', '#7EB712'] as const;

export interface LegendEntry {
  name: string;
  color: string;
  /** index into VizSeries.lines, or -1 */
  state: number;
  action: number;
  other: number;
}

/** One entry per name in the order the lines come; state and action of a name share it. */
export function legendEntries(lines: VizSeries['lines']): LegendEntry[] {
  const out: LegendEntry[] = [];
  const byName = new Map<string, LegendEntry>();
  lines.forEach((ln, i) => {
    let e = byName.get(ln.name);
    if (!e) {
      e = { name: ln.name, color: PALETTE[out.length % PALETTE.length], state: -1, action: -1, other: -1 };
      byName.set(ln.name, e);
      out.push(e);
    }
    if (ln.role === 'state' && e.state < 0) e.state = i;
    else if (ln.role === 'action' && e.action < 0) e.action = i;
    else if (e.other < 0) e.other = i;
  });
  return out;
}

/** The y range of the visible lines, padded by 8 % (a flat line gets ±0.5). */
export function yRange(series: Pick<VizSeries, 'lines'>, hidden: ReadonlySet<string>): [number, number] {
  let lo = Infinity;
  let hi = -Infinity;
  for (const ln of series.lines) {
    if (hidden.has(ln.name)) continue;
    for (const v of ln.values) {
      if (v == null || !Number.isFinite(v)) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
  }
  if (!Number.isFinite(lo)) return [0, 1];
  if (hi - lo < 1e-9) return [lo - 0.5, hi + 0.5];
  const pad = (hi - lo) * 0.08;
  return [lo - pad, hi + pad];
}

/** An SVG path through the samples; a null value breaks the line. */
export function linePath(t: readonly number[], values: readonly (number | null)[], x: (s: number) => number, y: (v: number) => number): string {
  const parts: string[] = [];
  let pen = false;
  const n = Math.min(t.length, values.length);
  for (let i = 0; i < n; i += 1) {
    const v = values[i];
    if (v == null || !Number.isFinite(v)) {
      pen = false;
      continue;
    }
    parts.push(`${pen ? 'L' : 'M'}${x(t[i]).toFixed(1)} ${y(v).toFixed(1)}`);
    pen = true;
  }
  return parts.join('');
}

/** The sample shown at time s: the last one at or before it (null before the first). */
export function valueAt(t: readonly number[], values: readonly (number | null)[], s: number): number | null {
  const i = bisectRight(t, s + 1e-9);
  if (i < 0) return null;
  const v = values[i];
  return v == null || !Number.isFinite(v) ? null : v;
}

export function fmtNum(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '–';
  const a = Math.abs(v);
  return a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : v.toFixed(2);
}

const TIME_STEPS = [0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600];

/** Ticks on the time axis: a round step giving at most `max` labels. */
export function timeTicks(from: number, to: number, max = 8): number[] {
  const span = Math.max(1e-6, to - from);
  const step = TIME_STEPS.find((s) => span / s <= max) ?? Math.ceil(span / max);
  const out: number[] = [];
  for (let v = Math.ceil(from / step - 1e-9) * step; v <= to + 1e-9; v += step) out.push(Math.round(v * 1000) / 1000 + 0); // + 0: never -0
  return out;
}

/** Evenly spaced value ticks (the y axis keeps the data's range, not round numbers). */
export function valueTicks(lo: number, hi: number, n = 4): number[] {
  return Array.from({ length: n + 1 }, (_, i) => lo + ((hi - lo) * i) / n);
}

/** How many points to ask the series endpoint for: two per pixel, within its 100–20000 bounds. */
export function pointsFor(plotWidth: number): number {
  return Math.max(100, Math.min(20000, Math.round(plotWidth * 2) || 1000));
}
