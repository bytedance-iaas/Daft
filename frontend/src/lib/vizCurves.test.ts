import { describe, expect, it } from 'vitest';
import type { VizSeries } from '../api/types';
import { fmtNum, legendEntries, linePath, PALETTE, pointsFor, timeTicks, valueAt, valueTicks, yRange } from './vizCurves';

const SERIES: VizSeries = {
  stream: 'arm',
  unit: 'rad',
  from_s: 0,
  to_s: 0.3,
  t: [0, 0.1, 0.2, 0.3],
  lines: [
    { name: 'joint_0', role: 'state', values: [0, 0.5, 1, 1.5] },
    { name: 'joint_1', role: 'state', values: [2, 2, null, 2] },
    { name: 'joint_0', role: 'action', values: [0.1, 0.6, 1.1, 1.6] },
    { name: 'joint_1', role: 'action', values: [2.1, 2.1, 2.1, 2.1] },
  ],
  total_points: 4,
  downsampled: false,
};

describe('vizCurves', () => {
  it('pairs state and action of a name into one legend entry and colour', () => {
    const legend = legendEntries(SERIES.lines);
    expect(legend).toEqual([
      { name: 'joint_0', color: PALETTE[0], state: 0, action: 2, other: -1 },
      { name: 'joint_1', color: PALETTE[1], state: 1, action: 3, other: -1 },
    ]);
    const many = legendEntries(Array.from({ length: 9 }, (_, i) => ({ name: `d${i}`, role: 'other' as const, values: [] })));
    expect(many[8].color).toBe(PALETTE[0]);
    expect(many[8].other).toBe(8);
  });

  it('takes the y range of the visible lines with a margin', () => {
    const [lo, hi] = yRange(SERIES, new Set());
    expect(lo).toBeCloseTo(0 - 2.1 * 0.08);
    expect(hi).toBeCloseTo(2.1 + 2.1 * 0.08);
    expect(yRange(SERIES, new Set(['joint_0']))).toEqual([2 - 0.1 * 0.08, 2.1 + 0.1 * 0.08].map((v) => expect.closeTo(v, 9)) as unknown as [number, number]);
    expect(yRange({ lines: [{ name: 'flat', role: 'state', values: [3, 3] }] }, new Set())).toEqual([2.5, 3.5]);
    expect(yRange({ lines: [] }, new Set())).toEqual([0, 1]);
  });

  it('draws paths that break at missing samples', () => {
    const x = (s: number) => s * 100;
    const y = (v: number) => 10 - v;
    expect(linePath(SERIES.t, SERIES.lines[0].values, x, y)).toBe('M0.0 10.0L10.0 9.5L20.0 9.0L30.0 8.5');
    expect(linePath(SERIES.t, SERIES.lines[1].values, x, y)).toBe('M0.0 8.0L10.0 8.0M30.0 8.0');
    expect(linePath([], [], x, y)).toBe('');
  });

  it('reads the value under the cursor', () => {
    expect(valueAt(SERIES.t, SERIES.lines[0].values, 0.15)).toBe(0.5);
    expect(valueAt(SERIES.t, SERIES.lines[0].values, 0.3)).toBe(1.5);
    expect(valueAt(SERIES.t, SERIES.lines[0].values, -1)).toBeNull();
    expect(valueAt(SERIES.t, SERIES.lines[1].values, 0.25)).toBeNull();
  });

  it('formats numbers and ticks', () => {
    expect([fmtNum(123.4), fmtNum(-12.34), fmtNum(0.1234), fmtNum(null), fmtNum(Number.NaN)]).toEqual(['123', '-12.3', '0.12', '–', '–']);
    expect(timeTicks(0, 20)).toEqual([0, 5, 10, 15, 20]);
    expect(timeTicks(0, 3)).toEqual([0, 0.5, 1, 1.5, 2, 2.5, 3]);
    expect(timeTicks(2.2, 9.9)).toEqual([3, 4, 5, 6, 7, 8, 9]);
    expect(valueTicks(0, 4)).toEqual([0, 1, 2, 3, 4]);
    expect([pointsFor(10), pointsFor(600), pointsFor(50000)]).toEqual([100, 1200, 20000]);
  });
});
