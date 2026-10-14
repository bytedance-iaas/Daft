// The sync check's windowed lag chart (registry 8.1, MV-3): each camera's lag read window by window along the
// episode. Two cameras in step draw on top of each other; a camera whose line sits apart is the one out of step.
import { CHART_COLORS, Chart, lineOption } from '../../components/Chart';
import { lagAxis, windowSeries, windowSummaries } from '../../lib/syncWindows';
import type { SyncCurves } from '../../api/types';
import { zh } from '../../locales/zh';

export function SyncWindowsChart({ curves, height = 220 }: { curves: Pick<SyncCurves, 'cameras'>; height?: number }) {
  const S = zh.episodeTab.sync;
  const series = windowSeries(curves.cameras, CHART_COLORS, { trusted: S.windowTrusted, untrusted: S.windowUntrusted });
  if (!series.length) return null;
  const lim = lagAxis(curves.cameras);
  const summary = windowSummaries(curves.cameras).map((w) => S.windowSummary(w.camera, w.lag, w.iqr, w.trusted, w.windows)).join('；');
  const option = lineOption(series, { xName: S.timeAxis, yName: S.windowAxis });
  const y = option.yAxis as Record<string, unknown>;
  option.yAxis = { ...y, min: -lim, max: lim };
  // all four entries (trusted solid, untrusted dashed, per camera): a plain legend that wraps, with room between
  // entries - the scrolling legend measured the long names short and ran them into each other
  const legendOrder = series.filter((x) => !x.dashed).flatMap((x) => [x.name, ...series.filter((y) => y.dashed && y.color === x.color).map((y) => y.name)]);
  option.legend = { top: 0, type: 'plain', itemGap: 20, itemWidth: 28, padding: [0, 8], data: legendOrder };
  (option.grid as Record<string, unknown>).top = 44;
  return (
    <div data-testid="sync-windows">
      <div className="section-sub">
        {S.windows}
        <span className="muted">{S.windowsDesc}</span>
      </div>
      <Chart height={height} summary={`${S.windows}：${summary}`} option={option} />
    </div>
  );
}
