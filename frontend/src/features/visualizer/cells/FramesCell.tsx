import { useEffect, useMemo, useRef, useState } from 'react';
import type { VizCamera, VizEpisodeCamera } from '../../../api/types';
import { bisectRight } from '../../../lib/vizTime';
import { zh } from '../../../locales/zh';
import type { PlayerClock } from '../clock';
import { useFrameIndex } from '../data';
import { FramePack, type Drawable } from '../framePack';

/** Frames asked for ahead of the one shown while playing (about a second at 30 fps). */
const AHEAD = 30;

function draw(canvas: HTMLCanvasElement, img: Drawable | undefined): void {
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
  const [notYet, setNotYet] = useState(false);
  const pack = useMemo(() => (ep.url && index.data ? new FramePack(ep.url, index.data) : null), [ep.url, index.data]);
  // the cleanup only drops the frames: StrictMode runs it and then reuses the same pack
  useEffect(() => () => pack?.clear(), [pack]);

  useEffect(() => {
    const el = canvas.current;
    if (!el || !pack) return undefined;
    let shown = -2;
    const times = pack.index.t;
    const paint = () => {
      const s = clock.getSnapshot();
      const k = bisectRight(times, s.t + 1e-6);
      setNotYet(k < 0);
      void pack.want(Math.max(0, k), s.playing ? AHEAD : 2);
      const img = k >= 0 ? (pack.get(k) ?? pack.nearest(k)) : undefined;
      if (k !== shown || img) draw(el, img);
      if (pack.get(k)) shown = k;
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
      if (k === bisectRight(times, clock.getSnapshot().t + 1e-6)) {
        shown = -2;
        paint();
      }
    };
    const unsub = clock.subscribe(() => {
      const k = bisectRight(times, clock.getSnapshot().t + 1e-6);
      if (k !== shown) paint();
    });
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
    ro?.observe(el);
    resize();
    return () => {
      unsub();
      ro?.disconnect();
      pack.onFrame = null;
    };
  }, [pack, clock]);

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
      ) : index.isLoading ? (
        <div className="vz-overlay" role="status">
          {zh.viz.video.loading}
        </div>
      ) : notYet ? (
        <div className="vz-overlay" role="status">
          {zh.viz.video.notYet}
        </div>
      ) : null}
    </>
  );
}
