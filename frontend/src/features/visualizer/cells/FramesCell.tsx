import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { VizCamera, VizEpisodeCamera } from '../../../api/types';
import { bisectRight, framesIn } from '../../../lib/vizTime';
import { zh } from '../../../locales/zh';
import { CLOCK_CONFIG, type PlayerClock } from '../clock';
import { useFrameIndex } from '../data';
import { FramePack, type Drawable } from '../framePack';

/** Frames asked for ahead of the one shown while playing (about a second at 30 fps). */
const AHEAD = 30;

/** A picture drawn on the canvas at its aspect ratio (letterboxed); nothing yet: the background. */
export function drawFrame(canvas: HTMLCanvasElement, img: Drawable | undefined): void {
  const ctx = canvas.getContext('2d');
  if (!ctx || typeof ctx.fillRect !== 'function') return;
  const w = canvas.width;
  const h = canvas.height;
  ctx.fillStyle = '#0f141b';
  ctx.fillRect(0, 0, w, h);
  if (!img) return;
  const iw = 'naturalWidth' in img ? img.naturalWidth : img.width;
  const ih = 'naturalHeight' in img ? img.naturalHeight : img.height;
  if (!iw || !ih) return;
  const k = Math.min(w / iw, h / ih);
  const dw = iw * k;
  const dh = ih * k;
  ctx.drawImage(img, (w - dw) / 2, (h - dh) / 2, dw, dh);
}

/**
 * Draws a frame pack on a canvas by the clock (D64: the frame at or before t; before the first frame the
 * first one, as a <video> parked at its start shows): asks the pack for the frames ahead while playing,
 * tells the clock how far its fetched frames reach (`attachSource`), redraws on resize. Shared by the JPEG
 * frame-pack cell and the depth cell (design doc 21 §5.5). Returns whether nothing could be drawn yet.
 */
export function usePackPainter(
  canvas: React.RefObject<HTMLCanvasElement | null>,
  pack: FramePack | null,
  clock: PlayerClock,
  sourceId: string,
  draw: (el: HTMLCanvasElement, img: Drawable | undefined) => void = drawFrame,
  onShown?: (img: Drawable | undefined) => void,
  /** frames decoded ahead of the one shown while playing (fewer than the pack keeps decoded) */
  ahead: number = AHEAD,
): boolean {
  const [waiting, setWaiting] = useState(true);
  useEffect(() => {
    const el = canvas.current;
    if (!el || !pack) return undefined;
    let shown = -2;
    let playing = clock.getSnapshot().playing;
    const times = pack.index.t;
    // playing, the bytes of about readyAheadS more come too (fetched in batches): what the clock waits for
    const prefetch = framesIn(times, CLOCK_CONFIG.readyAheadS + 1);
    const at = () => Math.max(0, bisectRight(times, clock.getSnapshot().t + 1e-6));
    const paint = () => {
      const s = clock.getSnapshot();
      const k = at();
      void pack.want(k, s.playing ? ahead : 2, s.playing ? prefetch : 0);
      const img = pack.get(k) ?? pack.nearest(k);
      if (k !== shown || img) {
        draw(el, img);
        onShown?.(img);
      }
      if (pack.get(k)) shown = k;
      setWaiting(!img);
    };
    const resize = () => {
      const r = el.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      el.width = Math.max(2, Math.round(r.width * dpr));
      el.height = Math.max(2, Math.round(r.height * dpr));
      shown = -2;
      paint();
    };
    pack.onFrame = (k) => {
      if (k === at()) {
        shown = -2;
        paint();
      }
    };
    const unsub = clock.subscribe(() => {
      const now = clock.getSnapshot().playing;
      if (at() !== shown || now !== playing) {
        playing = now;
        paint();
      }
    });
    const detach = clock.attachSource(sourceId, { ahead: (t) => pack.ahead(t) });
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
    ro?.observe(el);
    resize();
    return () => {
      unsub();
      detach();
      ro?.disconnect();
      pack.onFrame = null;
    };
  }, [canvas, pack, clock, sourceId, draw, onShown, ahead]);
  return waiting;
}

/**
 * A frame-pack cell (an mcap JPEG / PNG camera, design doc 18 §4.2): the frame under the clock,
 * drawn on a canvas at the picture's aspect ratio; the frames ahead are fetched while it plays.
 */
export function FramesCell({ cam, ep, clock }: { cam: VizCamera; ep: VizEpisodeCamera; clock: PlayerClock }) {
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const index = useFrameIndex(ep.index_url);
  const sourceId = `${cam.key}:${useId()}`;
  const pack = useMemo(() => (ep.url && index.data ? new FramePack(ep.url, index.data) : null), [ep.url, index.data]);
  // the cleanup only drops the frames: StrictMode runs it and then reuses the same pack
  useEffect(() => () => pack?.clear(), [pack]);
  const waiting = usePackPainter(canvas, pack, clock, sourceId);

  const failed = index.isError || (!ep.url && ep.access !== 'unsupported');
  return (
    <>
      <canvas ref={canvas} data-testid={`vz-frames-${cam.key}`} />
      {ep.access === 'unsupported' ? (
        <div className="vz-overlay" role="status">
          <span>
            {zh.viz.video.unsupported}
            <span className="why">{ep.reason ?? cam.reason ?? ''}</span>
          </span>
        </div>
      ) : failed ? (
        <div className="vz-overlay" role="status">
          {zh.viz.video.framesFailed}
        </div>
      ) : index.isLoading || waiting ? (
        <div className="vz-overlay" role="status">
          {zh.viz.video.loading}
        </div>
      ) : null}
    </>
  );
}
