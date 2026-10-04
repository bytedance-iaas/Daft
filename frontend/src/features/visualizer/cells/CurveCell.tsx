import { memo, useEffect, useMemo, useRef, useState } from 'react';
import type { VizSeries, VizStream } from '../../../api/types';
import { fmtNum, legendEntries, linePath, pointsFor, timeTicks, valueAt, valueTicks, yRange, type LegendEntry } from '../../../lib/vizCurves';
import { fmtClock } from '../../../lib/vizTime';
import { zh } from '../../../locales/zh';
import type { PlayerClock } from '../clock';
import { useVizSeries, type VizRef } from '../data';
import { useClockValue } from '../useClock';

/** A finding's band on the curves (mini player). */
export interface Band {
  id: string;
  level: 'blocking' | 'review' | 'info';
  start: number;
  end: number;
}

// the top margin leaves the caption its own line: the cursor's time label sits below it
const M = { l: 44, r: 12, t: 42, b: 18 };

function useSize<T extends HTMLElement>(): [React.RefObject<T>, { w: number; h: number }] {
  const ref = useRef<T>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const measure = () => {
      const r = el.getBoundingClientRect();
      setSize((s) => (Math.abs(s.w - r.width) < 1 && Math.abs(s.h - r.height) < 1 ? s : { w: r.width, h: r.height }));
    };
    measure();
    if (typeof ResizeObserver !== 'function') return undefined;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, size];
}

/** The quantized point count, so resizing by a few pixels does not ask again. */
function pointsForWidth(w: number): number {
  return pointsFor(Math.max(400, Math.ceil(w / 200) * 200));
}

/**
 * A curve cell (design doc 18 §5.2): one colour per name, state solid and action dashed, a cursor
 * on the clock, the values under it in the legend; clicking the plot seeks, clicking a legend entry
 * hides it (the info panel shares the hidden set).
 */
export function CurveCell({
  source,
  index,
  stream,
  duration,
  clock,
  hidden,
  onToggle,
  bands,
  focusBand,
}: {
  source: VizRef;
  index: number;
  stream: VizStream;
  duration: number;
  clock: PlayerClock;
  hidden: ReadonlySet<string>;
  onToggle: (name: string) => void;
  bands: readonly Band[];
  focusBand: string | null;
}) {
  const [cellRef, cell] = useSize<HTMLDivElement>();
  const [legendRef, legend] = useSize<HTMLDivElement>();
  const points = pointsForWidth(cell.w);
  const series = useVizSeries(source, index, stream.key, points, cell.w > 0);
  const entries = useMemo(() => legendEntries(series.data?.lines ?? stream.lines.map((l) => ({ name: l.name, role: l.role, values: [] }))), [series.data, stream.lines]);
  const plotH = Math.max(40, cell.h - legend.h);
  const pw = Math.max(10, cell.w - M.l - M.r);
  const ph = Math.max(10, plotH - M.t - M.b);
  const x = (s: number) => M.l + (duration > 0 ? (s / duration) * pw : 0);

  return (
    <div ref={cellRef} style={{ position: 'absolute', inset: 0 }}>
      <span className="vz-cap" title={zh.viz.curve.tip}>
        {stream.name}
        {stream.unit ? ` · ${stream.unit}` : ''}
      </span>
      {series.data?.downsampled ? <span className="vz-note">{zh.viz.curve.downsampled(series.data.t.length, series.data.total_points)}</span> : null}
      <div
        className="plot"
        style={{ height: plotH }}
        title={zh.viz.curve.jumpTitle}
        onPointerDown={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          const px = e.clientX - r.left;
          if (px < M.l || duration <= 0) return;
          clock.seek(((px - M.l) / pw) * duration);
        }}
      >
        {cell.w > 0 && series.data ? (
          <Plot series={series.data} entries={entries} hidden={hidden} w={cell.w} h={plotH} pw={pw} ph={ph} duration={duration} bands={bands} focusBand={focusBand}>
            <Cursor clock={clock} x={x} top={M.t} bottom={plotH - M.b} w={cell.w} />
          </Plot>
        ) : null}
        {series.isError ? <div className="vz-overlay">{zh.viz.curve.failed}</div> : null}
        {series.data && !series.data.t.length ? <div className="vz-overlay">{zh.viz.curve.empty}</div> : null}
      </div>
      <div className="legend" ref={legendRef}>
        {entries.map((e) => (
          <button key={e.name} type="button" className={`li${hidden.has(e.name) ? ' off' : ''}`} title={zh.viz.curve.legendTitle} onClick={() => onToggle(e.name)}>
            <span className="sw" style={{ color: e.color }} />
            <span className="nm">{e.name}</span>
            {series.data ? <LegendValues clock={clock} series={series.data} entry={e} /> : null}
          </button>
        ))}
      </div>
    </div>
  );
}

const Plot = memo(function Plot({
  series,
  entries,
  hidden,
  w,
  h,
  pw,
  ph,
  duration,
  bands,
  focusBand,
  children,
}: {
  series: VizSeries;
  entries: LegendEntry[];
  hidden: ReadonlySet<string>;
  w: number;
  h: number;
  pw: number;
  ph: number;
  duration: number;
  bands: readonly Band[];
  focusBand: string | null;
  children: React.ReactNode;
}) {
  const [lo, hi] = yRange(series, hidden);
  const X = (s: number) => M.l + (duration > 0 ? (s / duration) * pw : 0);
  const Y = (v: number) => M.t + (1 - (v - lo) / (hi - lo)) * ph;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width={w} height={h} role="img" aria-label={series.stream}>
      <g className="grid">
        {valueTicks(lo, hi).map((v) => (
          <line key={`y${v}`} x1={M.l} x2={w - M.r} y1={Y(v)} y2={Y(v)} />
        ))}
        {timeTicks(0, duration).map((s) => (
          <line key={`x${s}`} x1={X(s)} x2={X(s)} y1={M.t} y2={h - M.b} />
        ))}
      </g>
      <g className="axis">
        {valueTicks(lo, hi).map((v) => (
          <text key={`y${v}`} x={M.l - 6} y={Y(v) + 3} textAnchor="end">
            {fmtNum(v)}
          </text>
        ))}
        {timeTicks(0, duration).map((s) => (
          <text key={`x${s}`} x={X(s)} y={h - 5} textAnchor="middle">
            {`${s}s`}
          </text>
        ))}
      </g>
      {bands.map((b) => (b.end > b.start ? <rect key={b.id} className={`band ${b.level}${b.id === focusBand ? ' focus' : ''}`} x={X(b.start)} y={M.t} width={Math.max(2, X(b.end) - X(b.start))} height={ph} /> : null))}
      {entries.map((e) =>
        hidden.has(e.name)
          ? null
          : [
              e.action >= 0 ? <path key={`${e.name}-a`} d={linePath(series.t, series.lines[e.action].values, X, Y)} fill="none" stroke={e.color} strokeWidth={1.3} strokeDasharray="4 3" opacity={0.8} /> : null,
              e.other >= 0 ? <path key={`${e.name}-o`} d={linePath(series.t, series.lines[e.other].values, X, Y)} fill="none" stroke={e.color} strokeWidth={1.6} /> : null,
              e.state >= 0 ? <path key={`${e.name}-s`} d={linePath(series.t, series.lines[e.state].values, X, Y)} fill="none" stroke={e.color} strokeWidth={2} /> : null,
            ],
      )}
      {children}
    </svg>
  );
});

function Cursor({ clock, x, top, bottom, w }: { clock: PlayerClock; x: (s: number) => number; top: number; bottom: number; w: number }) {
  // a tenth of a pixel is enough resolution for the line
  const t = useClockValue(clock, (s) => Math.round(s.t * 1000) / 1000);
  const px = x(t);
  return (
    <g>
      <line className="cursor" x1={px} x2={px} y1={top - 6} y2={bottom} />
      <text className="cursor-label" x={Math.max(M.l + 16, Math.min(w - 24, px))} y={top - 8} textAnchor="middle">
        {fmtClock(t)}
      </text>
    </g>
  );
}

function LegendValues({ clock, series, entry }: { clock: PlayerClock; series: VizSeries; entry: LegendEntry }) {
  // the sample index under the cursor: re-render only when it changes
  const i = useClockValue(clock, (s) => {
    let lo = 0;
    let hi = series.t.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (series.t[mid] <= s.t + 1e-9) lo = mid + 1;
      else hi = mid;
    }
    return lo - 1;
  });
  const at = i >= 0 ? series.t[i] : -1;
  const v = (k: number) => (k >= 0 ? fmtNum(valueAt(series.t, series.lines[k].values, at)) : null);
  const main = entry.state >= 0 ? v(entry.state) : v(entry.other);
  const action = entry.action >= 0 ? v(entry.action) : null;
  return (
    <>
      <span className="val">{main ?? action ?? '–'}</span>
      {main !== null && action !== null ? <span className="val a">{action}</span> : null}
    </>
  );
}
