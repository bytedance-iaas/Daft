// The EEF marks over a camera of the mini player (design docs 20, 22 §3; C4 4.3.0 EefOverlay): which
// sample frame the camera shows at an episode time, which layers are drawn (the viewer's choice, the
// defaults, the "same as sent to the model" preset), and the drawing of one frame's layers on a canvas
// laid over an `object-fit: contain` picture. Pure functions; the canvas component calls them.
import type { EefOverlayCamera, EefOverlayLayer } from '../api/types';
import { bisectRight } from './vizTime';

// ------------------------------------------------------------------ time

/** A camera's sample frames in the order the player shows them: their episode times and the frames. */
export interface OverlayTimeline {
  t: number[];
  f: number[];
  /** the usual time between two sample frames (the staleness bound is 1.5 of it) */
  step: number;
}

export function overlayTimeline(times: readonly (number | null)[]): OverlayTimeline {
  const pairs: [number, number][] = [];
  times.forEach((t, f) => {
    if (t !== null && Number.isFinite(t)) pairs.push([t, f]);
  });
  pairs.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const t = pairs.map((p) => p[0]);
  const gaps = t.slice(1).map((v, i) => v - t[i]).filter((d) => d > 1e-9).sort((a, b) => a - b);
  const step = gaps.length ? gaps[Math.floor(gaps.length / 2)] : 1 / 30;
  return { t, f: pairs.map((p) => p[1]), step };
}

/**
 * The sample frame drawn at episode time `t`: the last one shown at or before it (several on one video
 * frame: the last of them), none before the first, nor once it is more than 1.5 steps old - a missing
 * or sparse stretch does not leave old marks on new pictures.
 */
export function sampleAt(tl: OverlayTimeline, t: number): number | null {
  const i = bisectRight(tl.t, t + 1e-6);
  if (i < 0) return null;
  return t - tl.t[i] > 1.5 * tl.step + 1e-6 ? null : tl.f[i];
}

// ------------------------------------------------------------------ what is drawn

/** 原图 / 叠加 / 只看观测 */
export type OverlayMode = 'off' | 'on' | 'observed';
/** the layer defaults, the marks the model was shown, or the viewer's own ticks */
export type OverlayPreset = 'default' | 'model' | 'custom';

/** The viewer's choice, kept in the browser for every task (design doc 22 §3.3). */
export interface OverlayChoice {
  mode: OverlayMode;
  preset: OverlayPreset;
  /** custom: layer id -> drawn; a layer not named takes its default */
  layers: Record<string, boolean>;
  /** hand id -> drawn (default true) */
  hands: Record<string, boolean>;
  labels: boolean;
  /** a handheld gripper's pose gaps bridged up to this long (ms); null: the checks' default (design doc 22 §5.2) */
  maxGapMs: number | null;
}

export const DEFAULT_CHOICE: OverlayChoice = { mode: 'on', preset: 'default', layers: {}, hands: {}, labels: true, maxGapMs: null };

/** The longest gap the overlay may be asked to bridge (C4 `max_gap_ms`). */
export const MAX_GAP_MS = 2000;

const MODES: readonly OverlayMode[] = ['off', 'on', 'observed'];
const PRESETS: readonly OverlayPreset[] = ['default', 'model', 'custom'];

/** A stored choice made safe (an older or hand-edited one falls back field by field). */
export function parseChoice(raw: unknown): OverlayChoice {
  const o = raw && typeof raw === 'object' ? (raw as Partial<OverlayChoice>) : {};
  const flags = (v: unknown) =>
    v && typeof v === 'object' ? Object.fromEntries(Object.entries(v as Record<string, unknown>).filter(([, b]) => typeof b === 'boolean')) as Record<string, boolean> : {};
  return {
    mode: MODES.includes(o.mode as OverlayMode) ? (o.mode as OverlayMode) : DEFAULT_CHOICE.mode,
    preset: PRESETS.includes(o.preset as OverlayPreset) ? (o.preset as OverlayPreset) : DEFAULT_CHOICE.preset,
    layers: flags(o.layers),
    hands: flags(o.hands),
    labels: typeof o.labels === 'boolean' ? o.labels : DEFAULT_CHOICE.labels,
    maxGapMs: typeof o.maxGapMs === 'number' && o.maxGapMs > 0 && o.maxGapMs <= MAX_GAP_MS ? o.maxGapMs : null,
  };
}

/** The interpolation hint: the default and the reference range in ms, from the sample interval. */
export function gapHint(interp: { default_s: number | null; step_s: number | null; range_steps: number[] } | null): { defaultMs: number; lo: number; hi: number; fps: number } | null {
  if (!interp || !interp.default_s || !interp.step_s) return null;
  const ms = (s: number) => Math.round(s * 1000);
  return { defaultMs: ms(interp.default_s), lo: ms(interp.range_steps[0] * interp.step_s), hi: ms(interp.range_steps[1] * interp.step_s), fps: Math.round(1 / interp.step_s) };
}

const OBSERVED_GROUPS = new Set(['observed']);

/** Whether a layer is drawn under the choice. */
export function layerOn(choice: OverlayChoice, layer: EefOverlayLayer): boolean {
  if (choice.mode === 'off' || choice.hands[layer.hand] === false) return false;
  if (choice.mode === 'observed') return OBSERVED_GROUPS.has(layer.group);
  if (choice.preset === 'model') return layer.in_model;
  if (choice.preset === 'custom' && layer.id in choice.layers) return choice.layers[layer.id];
  return layer.default_on;
}

/** The colour a layer is drawn in: the model's own under its preset. */
export function layerColor(choice: OverlayChoice, layer: EefOverlayLayer): string {
  return choice.preset === 'model' && choice.mode === 'on' && layer.model_color ? layer.model_color : layer.color;
}

/** A layer ticked or unticked: the current drawing becomes the custom choice, with this one changed. */
export function toggleLayer(choice: OverlayChoice, layers: readonly EefOverlayLayer[], id: string, on: boolean): OverlayChoice {
  const now: Record<string, boolean> = {};
  for (const l of layers) now[l.id] = layerOn({ ...choice, mode: 'on' }, l);
  return { ...choice, mode: choice.mode === 'off' ? 'on' : choice.mode, preset: 'custom', layers: { ...choice.layers, ...now, [id]: on } };
}

/** One entry of the layer list: a layer id with its title and colour, once for all cameras. */
export interface LayerEntry {
  id: string;
  group: EefOverlayLayer['group'];
  title: string;
  color: string;
  hands: string[];
}

/** The layers of every camera, by id in their drawing order (the list the menu shows). */
export function layerEntries(cameras: readonly EefOverlayCamera[]): LayerEntry[] {
  const out = new Map<string, LayerEntry>();
  for (const c of cameras) {
    for (const l of c.layers) {
      const e = out.get(l.id);
      if (e) {
        if (!e.hands.includes(l.hand)) e.hands.push(l.hand);
      } else out.set(l.id, { id: l.id, group: l.group, title: l.title, color: l.color, hands: [l.hand] });
    }
  }
  return [...out.values()];
}

// ------------------------------------------------------------------ drawing

/** Source pixels -> canvas pixels of an `object-fit: contain` box. */
export interface Fit {
  scale: number;
  x: number;
  y: number;
}

export function containFit(boxW: number, boxH: number, imageW: number, imageH: number): Fit {
  const scale = Math.min(boxW / imageW, boxH / imageH);
  return { scale, x: (boxW - imageW * scale) / 2, y: (boxH - imageH * scale) / 2 };
}

/** How a camera's marks stand out: the focused finding's camera bold, the others faint. */
export type Emphasis = 'normal' | 'strong' | 'dim';

export interface DrawOptions {
  choice: OverlayChoice;
  emphasis?: Emphasis;
  /** text beside the fingers' line for a sample frame (the recorded opening), or null */
  opening?: (f: number) => string | null;
}

function line(ctx: CanvasRenderingContext2D, pts: readonly (number | null)[], fit: Fit) {
  ctx.beginPath();
  let pen = false;
  for (let i = 0; i + 1 < pts.length; i += 2) {
    const x = pts[i];
    const y = pts[i + 1];
    if (x === null || y === null) {
      pen = false;
      continue;
    }
    const px = fit.x + x * fit.scale;
    const py = fit.y + y * fit.scale;
    if (pen) ctx.lineTo(px, py);
    else ctx.moveTo(px, py);
    pen = true;
  }
  ctx.stroke();
}

function label(ctx: CanvasRenderingContext2D, text: string, x: number, y: number) {
  ctx.font = '12px sans-serif';
  ctx.lineWidth = 3;
  ctx.strokeStyle = 'rgba(0,0,0,0.6)';
  ctx.strokeText(text, x, y);
  ctx.fillText(text, x, y);
}

function drawLayer(ctx: CanvasRenderingContext2D, layer: EefOverlayLayer, v: readonly (number | null)[], fit: Fit, color: string, widen: number, labels: boolean, tag: string | null) {
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = Math.max(1.5, layer.width * Math.max(fit.scale, 0.75)) * widen;
  if (layer.kind === 'polyline') {
    line(ctx, v, fit);
    return;
  }
  if (v.some((n) => n === null)) return;
  const p = (v as number[]).map((n, i) => (i % 2 ? fit.y : fit.x) + n * fit.scale);
  if (layer.kind === 'point' || layer.kind === 'cross') {
    ctx.beginPath();
    if (layer.kind === 'point') ctx.arc(p[0], p[1], 6, 0, 2 * Math.PI);
    else {
      ctx.moveTo(p[0] - 6, p[1] - 6);
      ctx.lineTo(p[0] + 6, p[1] + 6);
      ctx.moveTo(p[0] + 6, p[1] - 6);
      ctx.lineTo(p[0] - 6, p[1] + 6);
    }
    ctx.stroke();
    if (labels && layer.label) label(ctx, layer.label, p[0] + 8, p[1] - 6);
    return;
  }
  line(ctx, v, fit);
  if (layer.kind === 'arrow') {
    const ang = Math.atan2(p[3] - p[1], p[2] - p[0]);
    const head = Math.max(6, 0.25 * Math.hypot(p[2] - p[0], p[3] - p[1]));
    ctx.beginPath();
    ctx.moveTo(p[2] - head * Math.cos(ang - 0.4), p[3] - head * Math.sin(ang - 0.4));
    ctx.lineTo(p[2], p[3]);
    ctx.lineTo(p[2] - head * Math.cos(ang + 0.4), p[3] - head * Math.sin(ang + 0.4));
    ctx.stroke();
  }
  // the opening goes under the line's end: the point's own label sits above it, to the right
  if (labels && (layer.label || tag)) label(ctx, [layer.label, tag].filter(Boolean).join(' '), p[2] + 6, tag ? p[3] + 16 : p[3] - 6);
}

/** Clears the canvas and draws sample frame `f`'s chosen marks (null: only clears). Sizes are CSS pixels. */
export function drawFrame(ctx: CanvasRenderingContext2D, cam: EefOverlayCamera, f: number | null, fit: Fit, size: { w: number; h: number }, opts: DrawOptions) {
  ctx.clearRect(0, 0, size.w, size.h);
  if (f === null || opts.choice.mode === 'off') return;
  const emphasis = opts.emphasis ?? 'normal';
  ctx.save();
  ctx.globalAlpha = emphasis === 'dim' ? 0.45 : 1;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  const widen = emphasis === 'strong' ? 1.6 : 1;
  let drew = false;
  for (const layer of cam.layers) {
    const v = layer.frames[f];
    if (!v || !layerOn(opts.choice, layer)) continue;
    const tag = layer.id === 'finger_axis' && opts.opening ? opts.opening(f) : null;
    drawLayer(ctx, layer, v, fit, layerColor(opts.choice, layer), widen, opts.choice.labels, tag);
    drew = true;
  }
  if (drew && opts.choice.labels) {
    ctx.fillStyle = '#ffffff';
    label(ctx, `frame ${f + 1}`, fit.x + 8, fit.y + cam.image_size_wh[1] * fit.scale - 30);
  }
  ctx.restore();
}
