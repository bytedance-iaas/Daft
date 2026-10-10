import { describe, expect, it } from 'vitest';
import type { EefOverlayCamera, EefOverlayLayer } from '../api/types';
import { containFit, DEFAULT_CHOICE, drawFrame, gapHint, layerColor, layerEntries, layerOn, overlayTimeline, parseChoice, sampleAt, toggleLayer, type OverlayChoice } from './eefOverlay';

const layer = (id: string, group: EefOverlayLayer['group'], more: Partial<EefOverlayLayer> = {}): EefOverlayLayer => ({
  id, group, title: id, kind: 'point', label: null, color: '#123456', width: 2, frames: [[10, 20], null, [30, 40]], default_on: true, hand: 'eef', in_model: false, model_color: null, ...more,
});

const LAYERS = [
  layer('trail_past', 'trail_past', { kind: 'polyline', in_model: true, frames: [[1, 1, 2, 2, null, null, 3, 3], null, null] }),
  layer('trail_future', 'trail_future', { kind: 'polyline', default_on: false, frames: [null, null, null] }),
  layer('axis_x', 'axes', { kind: 'arrow', frames: [[0, 0, 10, 0], null, [0, 0, 10, 0]] }),
  layer('axis', 'declared', { kind: 'arrow', default_on: false, in_model: true, color: '#b26bff', model_color: '#ff0000', frames: [[0, 0, 0, 10], null, null] }),
  layer('observed_point', 'observed', { kind: 'cross' }),
  layer('point', 'declared', { in_model: true, label: 'P' }),
];

const cam = (layers = LAYERS): EefOverlayCamera => ({
  camera_id: 'ext', viz_camera: 'front', image_size_wh: [640, 480], fps: 15, media_frames: [0, 1, 2], times_s: [0, 0.1, 0.2],
  hands: [{ id: 'eef', title: 'panda_link8', color: '#ff0000', opening_m: [0.05, null, 0.04] }], skipped: null, layers,
});

describe('eefOverlay: time (design doc 22 §3.4)', () => {
  it('draws the last sample shown at or before the time, never before the first, never a stale one', () => {
    const tl = overlayTimeline([0.1, null, 0.2, 0.3, 0.3, 1.0]);
    expect(tl.t).toEqual([0.1, 0.2, 0.3, 0.3, 1.0]);
    expect(tl.step).toBeCloseTo(0.1);
    expect(sampleAt(tl, 0.05)).toBeNull();
    expect(sampleAt(tl, 0.1)).toBe(0);
    expect(sampleAt(tl, 0.29)).toBe(2);
    expect(sampleAt(tl, 0.3)).toBe(4);                     // two samples on one video frame: the last
    expect(sampleAt(tl, 0.44)).toBe(4);                    // within 1.5 steps
    expect(sampleAt(tl, 0.5)).toBeNull();                  // a gap: no old marks on new pictures
    expect(sampleAt(tl, 1.0)).toBe(5);
  });

  it('sorts samples by the time they are shown', () => {
    const tl = overlayTimeline([0.2, 0.1]);
    expect(tl.f).toEqual([1, 0]);
    expect(overlayTimeline([]).t).toEqual([]);
    expect(sampleAt(overlayTimeline([]), 1)).toBeNull();
  });
});

describe('eefOverlay: what is drawn (design doc 22 §3.3)', () => {
  const ids = (c: OverlayChoice) => LAYERS.filter((l) => layerOn(c, l)).map((l) => l.id);

  it('draws the defaults, the model preset, the custom ticks, the original or only the observation', () => {
    expect(ids(DEFAULT_CHOICE)).toEqual(['trail_past', 'axis_x', 'observed_point', 'point']);
    expect(ids({ ...DEFAULT_CHOICE, preset: 'model' })).toEqual(['trail_past', 'axis', 'point']);
    expect(ids({ ...DEFAULT_CHOICE, mode: 'off' })).toEqual([]);
    expect(ids({ ...DEFAULT_CHOICE, mode: 'observed' })).toEqual(['observed_point']);
    expect(ids({ ...DEFAULT_CHOICE, hands: { eef: false } })).toEqual([]);
    const custom = toggleLayer(DEFAULT_CHOICE, LAYERS, 'trail_future', true);
    expect(custom.preset).toBe('custom');
    expect(ids(custom)).toEqual(['trail_past', 'trail_future', 'axis_x', 'observed_point', 'point']);
    // ticking from the model preset keeps what it drew
    expect(ids(toggleLayer({ ...DEFAULT_CHOICE, preset: 'model' }, LAYERS, 'axis_x', true))).toEqual(['trail_past', 'axis_x', 'axis', 'point']);
    // ticking with everything off turns the marks back on
    expect(toggleLayer({ ...DEFAULT_CHOICE, mode: 'off' }, LAYERS, 'point', true).mode).toBe('on');
  });

  it('colours a layer as the model saw it under that preset only', () => {
    const a = LAYERS[3];
    expect(layerColor(DEFAULT_CHOICE, a)).toBe('#b26bff');
    expect(layerColor({ ...DEFAULT_CHOICE, preset: 'model' }, a)).toBe('#ff0000');
    expect(layerColor({ ...DEFAULT_CHOICE, preset: 'model' }, LAYERS[0])).toBe('#123456');
  });

  it('reads a stored choice safely', () => {
    expect(parseChoice(null)).toEqual(DEFAULT_CHOICE);
    expect(parseChoice({ mode: 'weird', preset: 'model', layers: { a: true, b: 'x' }, labels: false })).toEqual({ ...DEFAULT_CHOICE, preset: 'model', layers: { a: true }, labels: false });
    expect(parseChoice({ maxGapMs: 150 }).maxGapMs).toBe(150);
    for (const bad of [0, -5, 5000, '150', null]) expect(parseChoice({ maxGapMs: bad }).maxGapMs).toBeNull();
  });

  it('says the gap a handheld gripper is bridged with in ms, with the 2-5 interval range of its rate (design doc 22 §5.2)', () => {
    expect(gapHint({ default_s: 0.1, step_s: 1 / 30, range_steps: [2, 5] })).toEqual({ defaultMs: 100, lo: 67, hi: 167, fps: 30 });
    expect(gapHint({ default_s: 0.2, step_s: 1 / 15, range_steps: [2, 5] })).toEqual({ defaultMs: 200, lo: 133, hi: 333, fps: 15 });
    expect(gapHint(null)).toBeNull();
    expect(gapHint({ default_s: null, step_s: null, range_steps: [2, 5] })).toBeNull();
  });

  it('lists every layer once, across cameras and hands', () => {
    const other = cam(LAYERS.map((l) => ({ ...l, hand: 'robot1' })));
    const entries = layerEntries([cam(), other]);
    expect(entries.map((e) => e.id)).toEqual(LAYERS.map((l) => l.id));
    expect(entries[0].hands).toEqual(['eef', 'robot1']);
  });
});

describe('eefOverlay: drawing', () => {
  function recorder() {
    const calls: string[] = [];
    const ctx = new Proxy({} as Record<string, unknown>, {
      get(target, prop: string) {
        if (prop in target) return target[prop];
        return (...args: unknown[]) => calls.push(`${prop}(${args.map((a) => (typeof a === 'number' ? Math.round(a) : String(a))).join(',')})`);
      },
      set(target, prop: string, v) {
        target[prop] = v;
        if (prop === 'strokeStyle' || prop === 'globalAlpha' || prop === 'lineWidth') calls.push(`${prop}=${v}`);
        return true;
      },
    });
    return { ctx: ctx as unknown as CanvasRenderingContext2D, calls };
  }

  it('fits the picture and draws the chosen marks of a frame, with the labels', () => {
    expect(containFit(320, 240, 640, 480)).toEqual({ scale: 0.5, x: 0, y: 0 });
    const { ctx, calls } = recorder();
    drawFrame(ctx, cam(), 0, containFit(640, 480, 640, 480), { w: 640, h: 480 }, { choice: DEFAULT_CHOICE, opening: () => '开口 50 mm' });
    expect(calls[0]).toBe('clearRect(0,0,640,480)');
    expect(calls).toContain('arc(10,20,6,0,6)');                            // P
    expect(calls.filter((c) => c.startsWith('moveTo(4,14)'))).toHaveLength(1);   // the observed cross
    expect(calls.some((c) => c.startsWith('fillText(P'))).toBe(true);
    expect(calls.some((c) => c.startsWith('fillText(frame 1'))).toBe(true);
    expect(calls).not.toContain('strokeStyle=#b26bff');                   // A is off by default
  });

  it('writes the opening under the fingers\' line, clear of the point\'s label', () => {
    const fingers = layer('finger_axis', 'declared', { kind: 'segment', label: 'B', frames: [[0, 20, 40, 20], null, null] });
    const { ctx, calls } = recorder();
    drawFrame(ctx, cam([fingers, layer('point', 'declared', { label: 'robot1' })]), 0, containFit(640, 480, 640, 480), { w: 640, h: 480 }, { choice: DEFAULT_CHOICE, opening: () => '开口 50 mm' });
    expect(calls).toContain('fillText(B 开口 50 mm,46,36)');
    expect(calls).toContain('fillText(robot1,18,14)');
  });

  it('only clears with the marks off, a frame without a sample, or nothing chosen there', () => {
    for (const [f, choice] of [[0, { ...DEFAULT_CHOICE, mode: 'off' as const }], [null, DEFAULT_CHOICE], [1, DEFAULT_CHOICE]] as const) {
      const { ctx, calls } = recorder();
      drawFrame(ctx, cam(), f, containFit(640, 480, 640, 480), { w: 640, h: 480 }, { choice });
      expect(calls.filter((c) => c.startsWith('fillText') || c.startsWith('arc') || c.startsWith('lineTo'))).toEqual([]);
    }
  });

  it('draws the dataset record\'s own marks dashed, the others solid (C4 5.3.0)', () => {
    const rec = layer('record_trail_past', 'record', { kind: 'polyline', dash: [6, 4], frames: [[0, 0, 10, 10], null, null] });
    const { ctx, calls } = recorder();
    drawFrame(ctx, cam([layer('point', 'declared', { label: 'P' }), rec]), 0, containFit(640, 480, 640, 480), { w: 640, h: 480 }, { choice: DEFAULT_CHOICE });
    expect(calls.filter((c) => c.startsWith('setLineDash'))).toEqual(['setLineDash()', 'setLineDash(6,4)']);
  });

  it('makes the focused camera bold and the others faint', () => {
    const strong = recorder();
    drawFrame(strong.ctx, cam(), 0, containFit(640, 480, 640, 480), { w: 640, h: 480 }, { choice: DEFAULT_CHOICE, emphasis: 'strong' });
    expect(strong.calls).toContain('lineWidth=3.2');                      // 2 px marks at 1.6x
    const dim = recorder();
    drawFrame(dim.ctx, cam(), 0, containFit(640, 480, 640, 480), { w: 640, h: 480 }, { choice: DEFAULT_CHOICE, emphasis: 'dim' });
    expect(dim.calls).toContain('globalAlpha=0.45');
  });
});
