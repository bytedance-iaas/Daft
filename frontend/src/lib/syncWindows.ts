// The sync check's windowed lag readings as drawn (registry 8.1, MV-3): pure functions over the
// sync-curves endpoint's `windows` per camera - one series of the trusted windows and one of the rest
// per camera, and a one-line summary per camera for the chart's text alternative.
import type { SyncCurveCamera } from '../api/types';
import { fmt, signed } from './sectionStats';

export interface WindowSeries {
  name: string;
  data: [number, number | null][];
  color?: string;
  dashed?: boolean;
}

export interface WindowSummary {
  camera: string;
  /** median lag of the trusted windows, formatted with its sign, or '—' */
  lag: string;
  iqr: string;
  trusted: number;
  windows: number;
}

type Windows = NonNullable<SyncCurveCamera['windows']>;

/** Cameras with windowed readings, in the endpoint's order. */
export function windowedCameras(cameras: readonly SyncCurveCamera[]): { camera: string; windows: Windows }[] {
  return cameras.flatMap((c) => (c.windows && c.windows.points.length ? [{ camera: c.camera, windows: c.windows }] : []));
}

/**
 * The chart's series: one track per camera through every window in time order. When a camera has untrusted
 * windows the whole track is drawn dashed underneath, and the solid series on top has a value only at trusted
 * windows - so a solid stretch joins neighbouring trusted windows and breaks at an untrusted one, where the dashed
 * track carries on (two separate point sets, as first drawn, joined windows that are not neighbours).
 */
export function windowSeries(cameras: readonly SyncCurveCamera[], colors: readonly string[], label: { trusted: (cam: string) => string; untrusted: (cam: string) => string }): WindowSeries[] {
  const out: WindowSeries[] = [];
  windowedCameras(cameras).forEach(({ camera, windows }, i) => {
    const color = colors[i % colors.length];
    const points = [...windows.points].sort((a, b) => a.t_s - b.t_s);
    if (points.some((p) => !p.trusted)) {
      out.push({ name: label.untrusted(camera), data: points.map((p): [number, number] => [p.t_s, p.lag_s]), color, dashed: true });
    }
    out.push({ name: label.trusted(camera), data: points.map((p): [number, number | null] => [p.t_s, p.trusted ? p.lag_s : null]), color });
  });
  return out;
}

export function windowSummaries(cameras: readonly SyncCurveCamera[]): WindowSummary[] {
  return windowedCameras(cameras).map(({ camera, windows }) => ({
    camera,
    lag: typeof windows.lag_s === 'number' ? signed(windows.lag_s) : '—',
    iqr: typeof windows.iqr_s === 'number' ? fmt(windows.iqr_s) : '—',
    trusted: windows.n_trusted ?? windows.points.filter((p) => p.trusted).length,
    windows: windows.n_windows ?? windows.points.length,
  }));
}

/**
 * The lag axis: symmetric around 0, sized to the trusted windows (at least ±0.3 s, rounded up to a tenth, with a
 * tenth of room) so a 0.2 s gap between two cameras is plain to see; an untrusted window outside it is clipped.
 */
export function lagAxis(cameras: readonly SyncCurveCamera[]): number {
  const m = Math.max(0.2, ...windowedCameras(cameras).flatMap(({ windows }) => windows.points.filter((p) => p.trusted).map((p) => Math.abs(p.lag_s))));
  return Math.ceil(m * 10 + 1) / 10;
}
