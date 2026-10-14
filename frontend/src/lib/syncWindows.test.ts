import { describe, expect, it } from 'vitest';
import type { SyncCurveCamera } from '../api/types';
import { lagAxis, windowSeries, windowSummaries, windowedCameras } from './syncWindows';

function cam(camera: string, lags: (readonly [number, number, boolean])[], extra: Partial<NonNullable<SyncCurveCamera['windows']>> = {}): SyncCurveCamera {
  const points = lags.map(([t_s, lag_s, trusted]) => ({ t_s, lag_s, corr_peak: trusted ? 0.8 : 0.3, trusted }));
  return { camera, t: [], flow: [], speed: [], lags: [], xcorr: [], peak: null, windows: { win_s: 2, hop_s: 1, lag_s: 0.1, iqr_s: 0.02, n_trusted: points.filter((p) => p.trusted).length, n_windows: points.length, points, ...extra } };
}

describe('the sync check windows as drawn (registry 7.2)', () => {
  const label = { trusted: (c: string) => `${c} ok`, untrusted: (c: string) => `${c} ?` };
  it('one track per camera: the dashed whole track underneath only when a camera has untrusted windows, the solid one on top with holes at them; cameras without windows are left out', () => {
    const cams = [cam('front', [[1, 0.1, true], [3, 0.5, false], [2, 0.12, true], [4, 0.11, true]]), cam('wrist', [[1, -0.02, true], [2, 0.0, true]]), { ...cam('side', []), windows: null }];
    const s = windowSeries(cams, ['#1', '#2'], label);
    expect(s.map((x) => [x.name, x.dashed ?? false, x.color])).toEqual([
      ['front ?', true, '#1'],
      ['front ok', false, '#1'],
      ['wrist ok', false, '#2'],
    ]);
    expect(s[0].data).toEqual([[1, 0.1], [2, 0.12], [3, 0.5], [4, 0.11]]);          // the whole track, in time order
    expect(s[1].data).toEqual([[1, 0.1], [2, 0.12], [3, null], [4, 0.11]]);         // solid breaks at the untrusted window
    expect(s[2].data).toEqual([[1, -0.02], [2, 0.0]]);
    expect(windowedCameras(cams).map((c) => c.camera)).toEqual(['front', 'wrist']);
  });
  it('the summary says each camera\'s median, spread and trusted windows; the axis follows the trusted windows, at least ±0.3 s', () => {
    const cams = [cam('front', [[1, 0.1, true], [2, 0.12, true]], { lag_s: 0.11, iqr_s: 0.01 }), cam('wrist', [[1, -0.9, true]], { lag_s: -0.9, iqr_s: null })];
    expect(windowSummaries(cams)).toEqual([
      { camera: 'front', lag: '+0.11', iqr: '0.01', trusted: 2, windows: 2 },
      { camera: 'wrist', lag: '−0.90', iqr: '—', trusted: 1, windows: 1 },
    ]);
    expect(lagAxis(cams)).toBe(1);
    expect(lagAxis([cams[0]])).toBe(0.3);
    expect(lagAxis([cam('x', [[1, 0.1, true], [2, 0.9, false]])])).toBe(0.3);   // an untrusted window does not widen it
  });
});
