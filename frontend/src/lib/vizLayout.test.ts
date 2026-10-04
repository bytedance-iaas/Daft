import { describe, expect, it } from 'vitest';
import type { VizCamera, VizStream } from '../api/types';
import { cellHeight, cellValid, fitCells, gridKey, parseGridKey, resizeCells, shapeFor, smartColumns, templateLayout } from './vizLayout';

function cam(key: string): VizCamera {
  return { key, name: key, source: `observation.images.${key}`, kind: 'video', access: 'direct', codec: 'h264', codec_string: 'avc1.640028', width: 640, height: 480, fps: 30, transcoded: false, reason: null };
}

function stream(key: string, smart = true, kind: VizStream['kind'] = 'series', available = true): VizStream {
  return { key, kind, name: key, unit: null, lines: [], smart, available, reason: null, sources: [key] };
}

const MODEL = {
  cameras: [cam('head'), cam('left_wrist'), cam('right_wrist')],
  streams: [stream('left_arm'), stream('right_arm'), stream('grippers'), stream('force', false), stream('depth', false, 'depth', false)],
};

describe('vizLayout', () => {
  it('reads and writes grid keys', () => {
    expect(parseGridKey('3x2')).toEqual({ cols: 3, rows: 2 });
    expect(parseGridKey('4x1')).toBeNull();
    expect(gridKey({ cols: 2, rows: 2 })).toBe('2x2');
  });

  it('lays out the smart template by the grid width', () => {
    const wide = templateLayout('smart', MODEL, 1200)!;
    expect(wide.shape).toEqual({ cols: 3, rows: 2 });
    expect(wide.cells).toEqual([
      { kind: 'video', key: 'head' },
      { kind: 'video', key: 'left_wrist' },
      { kind: 'video', key: 'right_wrist' },
      { kind: 'curve', key: 'left_arm' },
      { kind: 'curve', key: 'right_arm' },
      { kind: 'empty' },
    ]);
    // the info panel squeezes the grid below 1000 px: two columns, three rows
    const narrow = templateLayout('smart', MODEL, 900)!;
    expect(narrow.shape).toEqual({ cols: 2, rows: 3 });
    expect(narrow.cells.filter((c) => c.kind !== 'empty')).toHaveLength(5);
    expect(smartColumns(999)).toBe(2);
  });

  it('keeps at most nine cells and shrinks the grid for few contents', () => {
    const many = { cameras: Array.from({ length: 10 }, (_, i) => cam(`cam${i}`)), streams: [stream('arm')] };
    const big = templateLayout('smart', many, 1400)!;
    expect(big.shape).toEqual({ cols: 3, rows: 3 });
    expect(big.cells).toHaveLength(9);
    const one = templateLayout('smart', { cameras: [cam('head')], streams: [] }, 1400)!;
    expect(one.shape).toEqual({ cols: 1, rows: 1 });
    const none = templateLayout('smart', { cameras: [], streams: [] }, 1400)!;
    expect(none.cells).toEqual([{ kind: 'empty' }]);
  });

  it('offers video-only and curve-only templates; custom keeps the arrangement', () => {
    expect(templateLayout('video', MODEL, 1200)!.shape).toEqual({ cols: 3, rows: 1 });
    const curves = templateLayout('curve', MODEL, 1200)!;
    expect(curves.shape).toEqual({ cols: 2, rows: 2 });
    // every drawable series, smart or not; the depth stream is not drawable in this phase
    expect(curves.cells.map((c) => (c.kind === 'curve' ? c.key : c.kind))).toEqual(['left_arm', 'right_arm', 'grippers', 'force']);
    expect(templateLayout('custom', MODEL, 1200)).toBeNull();
  });

  it('pads, cuts and resizes cells', () => {
    expect(shapeFor(5, 3)).toEqual({ cols: 3, rows: 2 });
    expect(shapeFor(2, 3)).toEqual({ cols: 2, rows: 1 });
    expect(fitCells([{ kind: 'video', key: 'a' }], { cols: 2, rows: 1 })).toEqual([{ kind: 'video', key: 'a' }, { kind: 'empty' }]);
    const cells = [{ kind: 'empty' as const }, { kind: 'video' as const, key: 'a' }, { kind: 'curve' as const, key: 'b' }];
    expect(resizeCells(cells, { cols: 1, rows: 1 })).toEqual([{ kind: 'video', key: 'a' }]);
  });

  it('sizes cells at two thirds of their width within 160–420 px', () => {
    expect(cellHeight(1200, 3)).toBe(Math.round(((1200 - 20) / 3) * 0.66));
    expect(cellHeight(300, 3)).toBe(160);
    expect(cellHeight(2400, 1)).toBe(420);
  });

  it('drops cells the model no longer has', () => {
    expect(cellValid({ kind: 'video', key: 'head' }, MODEL)).toBe(true);
    expect(cellValid({ kind: 'curve', key: 'gone' }, MODEL)).toBe(false);
    expect(cellValid({ kind: 'empty' }, MODEL)).toBe(true);
  });
});
