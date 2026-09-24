import { useEffect, useRef } from 'react';

/** Series colours from the mockups' README (checked for colour-blind separation, ≥3:1 on white). */
export const CHART_COLORS = ['#165DFF', '#0DA5AA', '#E8590C'];

export type ChartOption = Record<string, unknown>;

/**
 * An ECharts chart (SVG renderer), loaded lazily so the charts library never weighs on pages
 * without charts. `summary` is the accessible text of the chart (also what tests read).
 */
export function Chart({ option, height = 220, summary }: { option: ChartOption; height?: number; summary: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  // The key only says when the data changed; the option itself goes to ECharts, because a JSON
  // round trip drops functions (axis label formatters such as compactAxis).
  const key = JSON.stringify(option);
  const latest = useRef(option);
  latest.current = option;
  useEffect(() => {
    let disposed = false;
    let dispose: (() => void) | null = null;
    void import('./echarts')
      .then(({ echarts }) => {
        const el = ref.current;
        if (disposed || !el) return;
        const chart = echarts.init(el, undefined, { renderer: 'svg' });
        chart.setOption({ color: CHART_COLORS, textStyle: { fontFamily: 'inherit' }, ...latest.current });
        const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => chart.resize()) : null;
        ro?.observe(el);
        dispose = () => {
          ro?.disconnect();
          chart.dispose();
        };
      })
      .catch(() => undefined);
    return () => {
      disposed = true;
      dispose?.();
    };
  }, [key]);
  return <div ref={ref} className="chart-box" style={{ height }} role="img" aria-label={summary} data-testid="chart" />;
}

/** The tolerance band on charts (aligned sync lags): a pale green. */
export const BAND_COLOR = 'rgba(0, 180, 42, 0.1)';

/** Axis numbers in K / M: 0, 500K, 1M, 1.5M. */
export function compactAxis(v: number): string {
  const abs = Math.abs(v);
  const trim = (x: number) => Number(x.toFixed(1)).toString();
  if (abs >= 1e9) return `${trim(v / 1e9)}B`;
  if (abs >= 1e6) return `${trim(v / 1e6)}M`;
  if (abs >= 1e3) return `${trim(v / 1e3)}K`;
  return String(v);
}

/**
 * A horizontal or vertical bar chart of {name, value} items (one series, primary colour).
 * `colors` colours bars one by one; `band` shades a range of the value axis; `valueName`
 * titles the value axis.
 */
export function barOption(
  items: { name: string; value: number }[],
  opts: { horizontal?: boolean; unit?: string; colors?: (string | undefined)[]; band?: [number, number]; valueName?: string; valueRange?: [number, number]; compactValues?: boolean } = {},
): ChartOption {
  const names = items.map((i) => i.name);
  const values = items.map((i, k) => (opts.colors?.[k] ? { value: i.value, itemStyle: { color: opts.colors[k] } } : i.value));
  const cat = { type: 'category', data: names, axisTick: { show: false } };
  // Counts never get a 0.2 tick; a given range (scores 0-1, lags around the band) is kept.
  const counts = items.every((i) => Number.isInteger(i.value));
  const val = {
    type: 'value',
    splitLine: { lineStyle: { color: '#E5E6EB' } },
    ...(counts ? { minInterval: 1 } : {}),
    ...(opts.valueRange ? { min: opts.valueRange[0], max: opts.valueRange[1] } : {}),
    ...(opts.valueName ? { name: opts.valueName, nameGap: 8 } : {}),
    // Token-sized counts: 2M instead of 2,000,000, and a handful of ticks, not one per 500K
    ...(opts.compactValues ? { splitNumber: 3, axisLabel: { formatter: (v: number) => compactAxis(v) } } : {}),
  };
  const band = opts.band ? { markArea: { silent: true, itemStyle: { color: BAND_COLOR }, data: [[opts.horizontal ? { xAxis: opts.band[0] } : { yAxis: opts.band[0] }, opts.horizontal ? { xAxis: opts.band[1] } : { yAxis: opts.band[1] }]] } } : {};
  return {
    // containLabel sizes the margin to the axis labels themselves: a fixed left margin on top of it
    // pushed every horizontal chart to the right and left half its width empty
    grid: { left: 8, right: 24, top: opts.valueName && !opts.horizontal ? 28 : 12, bottom: 8, containLabel: true },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: opts.horizontal ? val : cat,
    yAxis: opts.horizontal ? { ...cat, inverse: true } : val,
    series: [{ type: 'bar', data: values, barMaxWidth: 28, name: opts.unit ?? '', ...band }],
  };
}

/** Several series over the same categories (per-camera histograms), side by side. */
export function groupedBarOption(categories: string[], series: { name: string; data: number[] }[], opts: { valueName?: string } = {}): ChartOption {
  return {
    grid: { left: 8, right: 16, top: 36, bottom: 8, containLabel: true },
    legend: { top: 0, type: 'scroll' },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'category', data: categories, axisTick: { show: false } },
    yAxis: { type: 'value', minInterval: 1, splitLine: { lineStyle: { color: '#E5E6EB' } }, ...(opts.valueName ? { name: opts.valueName } : {}) },
    series: series.map((s, i) => ({ type: 'bar', name: s.name, data: s.data, barMaxWidth: 14, itemStyle: { color: CHART_COLORS[i % CHART_COLORS.length] } })),
  };
}

export interface LinePoint {
  name: string;
  x: number;
  y: number;
  color?: string;
}

/**
 * Lines over a numeric x axis (curves over time or lag). `band` shades an x range, `points`
 * marks readings on the curves.
 */
export function lineOption(
  series: { name: string; data: [number, number | null][]; color?: string; dashed?: boolean }[],
  opts: { xName?: string; yName?: string; band?: [number, number]; points?: LinePoint[]; xMin?: number; xMax?: number; legend?: boolean } = {},
): ChartOption {
  const band = opts.band ? { markArea: { silent: true, itemStyle: { color: BAND_COLOR }, data: [[{ xAxis: opts.band[0] }, { xAxis: opts.band[1] }]] } } : {};
  return {
    grid: { left: 44, right: 16, top: opts.legend === false ? 16 : 36, bottom: 40 },
    legend: opts.legend === false ? undefined : { top: 0, type: 'scroll' },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'value', name: opts.xName, nameLocation: 'middle', nameGap: 24, min: opts.xMin, max: opts.xMax, splitLine: { show: false } },
    yAxis: { type: 'value', name: opts.yName, splitLine: { lineStyle: { color: '#E5E6EB' } } },
    series: series.map((s, i) => ({
      type: 'line',
      name: s.name,
      data: s.data,
      showSymbol: false,
      connectNulls: false,
      lineStyle: { width: 1.5, color: s.color ?? CHART_COLORS[i % CHART_COLORS.length], type: s.dashed ? 'dashed' : 'solid' },
      itemStyle: { color: s.color ?? CHART_COLORS[i % CHART_COLORS.length] },
      ...(i === 0 ? band : {}),
      ...(opts.points?.length && i === 0
        ? { markPoint: { symbol: 'circle', symbolSize: 9, label: { show: false }, data: opts.points.map((p) => ({ name: p.name, coord: [p.x, p.y], itemStyle: { color: p.color ?? CHART_COLORS[0], borderColor: '#fff', borderWidth: 1.5 } })) } }
        : {}),
    })),
  };
}

export function chartSummary(items: { name: string; value: number }[]): string {
  return items.map((i) => `${i.name} ${i.value}`).join('，');
}
