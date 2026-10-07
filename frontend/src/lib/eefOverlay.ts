// The EEF opinion's marks over the camera's own video (design doc 20; C4 2.6.0 EefOverlay): which sample
// frame a playing clip shows, where that frame starts, and the drawing of one frame's layers on a canvas
// laid over a `object-fit: contain` video.
import type { EefOverlayCamera, EefOverlayLayer } from '../api/types';

/** sample frame -> clip frame, and back (the first sample frame of a clip frame). */
export interface FrameMap {
  fps: number;
  media: (number | null)[];
  sample: Map<number, number>;
  /** clip frames that have a sample frame, ascending */
  shown: number[];
}

export function frameMap(cam: EefOverlayCamera, fallbackFps: number | null): FrameMap | null {
  const fps = cam.fps ?? fallbackFps;
  if (!fps || !(fps > 0)) return null;
  const sample = new Map<number, number>();
  cam.media_frames.forEach((k, f) => {
    if (k !== null && !sample.has(k)) sample.set(k, f);
  });
  return { fps, media: cam.media_frames, sample, shown: [...sample.keys()].sort((a, b) => a - b) };
}

/** The sample frame shown at `clipTime` seconds into the camera's clip: the nearest earlier clip frame that has one. */
export function sampleFrameAt(m: FrameMap, clipTime: number): number | null {
  const k = Math.floor(clipTime * m.fps + 1e-3);
  const exact = m.sample.get(k);
  if (exact !== undefined) return exact;
  let lo = 0;
  let hi = m.shown.length - 1;
  let best = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (m.shown[mid] <= k) {
      best = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return best < 0 || k - m.shown[best] > 1 ? null : (m.sample.get(m.shown[best]) ?? null);
}

/** Clip seconds at which sample frame `f` is shown (the middle of its clip frame); null when no video frame pairs with it. */
export function clipTimeOf(m: FrameMap, f: number): number | null {
  const k = m.media[f];
  return k === null || k === undefined ? null : (k + 0.5) / m.fps;
}

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

function line(ctx: CanvasRenderingContext2D, pts: (number | null)[], fit: Fit) {
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

function drawLayer(ctx: CanvasRenderingContext2D, layer: EefOverlayLayer, v: (number | null)[], fit: Fit) {
  ctx.strokeStyle = layer.color;
  ctx.fillStyle = layer.color;
  ctx.lineWidth = Math.max(1.5, layer.width * Math.max(fit.scale, 0.75));
  if (layer.kind === 'polyline') {
    line(ctx, v, fit);
    return;
  }
  if (v.some((n) => n === null)) return;
  const p = (v as number[]).map((n, i) => (i % 2 ? fit.y : fit.x) + n * fit.scale);
  if (layer.kind === 'point') {
    ctx.beginPath();
    ctx.arc(p[0], p[1], 6, 0, 2 * Math.PI);
    ctx.stroke();
    if (layer.label) label(ctx, layer.label, p[0] + 8, p[1] - 6);
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
  if (layer.label) label(ctx, layer.label, p[2] + 6, p[3] - 6);
}

/** Clears the canvas and draws sample frame `f`'s marks (null: only clears). Sizes are CSS pixels. */
export function drawFrame(ctx: CanvasRenderingContext2D, cam: EefOverlayCamera, f: number | null, fit: Fit, size: { w: number; h: number }) {
  ctx.clearRect(0, 0, size.w, size.h);
  if (f === null) return;
  ctx.save();
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  for (const layer of cam.layers) {
    const v = layer.frames[f];
    if (v) drawLayer(ctx, layer, v, fit);
  }
  ctx.fillStyle = '#ffffff';
  label(ctx, `frame ${f + 1}`, fit.x + 8, fit.y + cam.image_size_wh[1] * fit.scale - 10);
  ctx.restore();
}
