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
  const key = JSON.stringify(option);
  useEffect(() => {
    let disposed = false;
    let dispose: (() => void) | null = null;
    void import('./echarts')
      .then(({ echarts }) => {
        const el = ref.current;
        if (disposed || !el) return;
        const chart = echarts.init(el, undefined, { renderer: 'svg' });
        chart.setOption({ color: CHART_COLORS, textStyle: { fontFamily: 'inherit' }, ...JSON.parse(key) });
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

/** A horizontal or vertical bar chart of {name, value} items (one series, primary colour). */
export function barOption(items: { name: string; value: number }[], opts: { horizontal?: boolean; unit?: string } = {}): ChartOption {
  const names = items.map((i) => i.name);
  const values = items.map((i) => i.value);
  const cat = { type: 'category', data: names, axisTick: { show: false } };
  const val = { type: 'value', splitLine: { lineStyle: { color: '#E5E6EB' } } };
  return {
    grid: { left: opts.horizontal ? 96 : 40, right: 24, top: 16, bottom: 28 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: opts.horizontal ? val : cat,
    yAxis: opts.horizontal ? { ...cat, inverse: true } : val,
    series: [{ type: 'bar', data: values, barMaxWidth: 24, name: opts.unit ?? '' }],
  };
}

export function chartSummary(items: { name: string; value: number }[]): string {
  return items.map((i) => `${i.name} ${i.value}`).join('，');
}
