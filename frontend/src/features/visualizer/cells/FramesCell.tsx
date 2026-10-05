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
 * A frame-pack cell (an mcap JPEG / PNG camera, design doc 18 §4.2): the frame under the clock,
 * drawn on a canvas at the picture's aspect ratio; the frames ahead are fetched while it plays.
 */
export function FramesCell({ cam, ep, clock }: { cam: VizCamera; ep: VizEpisodeCamera; clock: PlayerClock }) {
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const index = useFrameIndex(ep.index_url);
  const sourceId = `${cam.key}:${useId()}`;
  // nothing to draw yet: the frame is on its way
  const [waiting, setWaiting] = useState(true);
  const pack = useMemo(() => (ep.url && index.data ? new FramePack(ep.url, index.data) : null), [ep.url, index.data]);
  // the cleanup only drops the frames: StrictMode runs it and then reuses the same pack
  useEffect(() => () => pack?.clear(), [pack]);

  useEffect(() => {
    const el = canvas.current;
    if (!el || !pack) return undefined;
    let shown = -2;
    let playing = clock.getSnapshot().playing;
    const times = pack.index.t;
    // playing, the bytes of about readyAheadS more come too (fetched in batches): what the clock waits for
    const prefetch = framesIn(times, CLOCK_CONFIG.readyAheadS + 1);
    // before the camera's first frame it shows that frame, as a <video> parked at its start does
    const at = () => Math.max(0, bisectRight(times, clock.getSnapshot().t + 1e-6));
    const paint = () => {
      const s = clock.getSnapshot();
      const k = at();
      void pack.want(k, s.playing ? AHEAD : 2, s.playing ? prefetch : 0);
      const img = pack.get(k) ?? pack.nearest(k);
      if (k !== shown || img) drawFrame(el, img);
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
  }, [pack, clock, sourceId]);

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
