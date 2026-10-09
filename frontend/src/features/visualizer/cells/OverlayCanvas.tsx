import { useEffect, useMemo, useRef } from 'react';
import type { EefOverlayCamera, VizEpisodeCamera } from '../../../api/types';
import { containFit, drawFrame, overlayTimeline, sampleAt, type Emphasis, type OverlayChoice } from '../../../lib/eefOverlay';
import { mediaBinding } from '../../../lib/vizTime';
import { zh } from '../../../locales/zh';
import type { PlayerClock } from '../clock';

/** `requestVideoFrameCallback` (Chromium, Safari, Firefox 132+): the media time of the frame put on screen. */
type FramedVideo = HTMLVideoElement & {
  requestVideoFrameCallback?: (cb: (now: number, meta: { mediaTime: number }) => void) => number;
  cancelVideoFrameCallback?: (handle: number) => void;
};

/**
 * The EEF marks over a camera cell (design doc 22 §3.3): a transparent canvas above whatever the cell
 * shows (a <video>, a frame pack, the browser's decoder), drawing the sample frame of the moment - the
 * one the camera shows at the clock's time (`times_s`), or, while a <video> plays, at the media time of
 * the frame it actually presented, so the marks stay on their frame when the video drifts a little.
 */
export function OverlayCanvas({ cam, ep, clock, choice, emphasis }: { cam: EefOverlayCamera; ep: VizEpisodeCamera; clock: PlayerClock; choice: OverlayChoice; emphasis: Emphasis }) {
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const tl = useMemo(() => overlayTimeline(cam.times_s), [cam]);
  const opening = useMemo(() => {
    const values = cam.hands[0]?.opening_m;
    if (!values) return undefined;
    return (f: number) => {
      const v = values[f];
      return v === null || v === undefined ? null : zh.viz.overlay.opening(v);
    };
  }, [cam]);

  useEffect(() => {
    const el = canvas.current;
    if (!el) return undefined;
    let presented: number | null = null;
    let video: FramedVideo | null = null;
    let handle = 0;
    const paint = () => {
      const s = clock.getSnapshot();
      const t = s.playing && presented !== null ? presented : s.t;
      const r = el.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const w = Math.max(2, Math.round(r.width * dpr));
      const h = Math.max(2, Math.round(r.height * dpr));
      if (el.width !== w || el.height !== h) {
        el.width = w;
        el.height = h;
      }
      const ctx = el.getContext('2d');
      if (!ctx || typeof ctx.setTransform !== 'function' || !r.width || !r.height) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const fit = containFit(r.width, r.height, cam.image_size_wh[0], cam.image_size_wh[1]);
      drawFrame(ctx, cam, sampleAt(tl, t), fit, { w: r.width, h: r.height }, { choice, emphasis, opening });
    };
    const stop = () => {
      if (video && handle && video.cancelVideoFrameCallback) video.cancelVideoFrameCallback(handle);
      handle = 0;
    };
    // the cell's <video>, if any (a direct one, or the remux a browser decoder fell back to); it may be
    // replaced when its URL is renewed, so it is looked up again as the clock moves
    const watch = () => {
      const v = (el.parentElement?.querySelector('video') ?? null) as FramedVideo | null;
      if (v === video) return;
      stop();
      video = v;
      presented = null;
      if (!v || typeof v.requestVideoFrameCallback !== 'function') return;
      const onFrame = (_now: number, meta: { mediaTime: number }) => {
        if (video !== v) return;
        const b = mediaBinding(ep, v.getAttribute('src'));
        presented = meta.mediaTime - b.from + b.offset;
        paint();
        handle = v.requestVideoFrameCallback?.(onFrame) ?? 0;
      };
      handle = v.requestVideoFrameCallback(onFrame);
    };
    let last = Number.NaN;
    let lastPlaying = false;
    const unsub = clock.subscribe(() => {
      const s = clock.getSnapshot();
      watch();
      // playing a <video>, its own frames repaint; otherwise every move of the clock does
      if ((!s.playing || presented === null) && (s.t !== last || s.playing !== lastPlaying)) paint();
      last = s.t;
      lastPlaying = s.playing;
    });
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(paint) : null;
    ro?.observe(el);
    watch();
    paint();
    return () => {
      unsub();
      ro?.disconnect();
      stop();
      video = null;
    };
  }, [cam, ep, clock, tl, choice, emphasis, opening]);

  return <canvas ref={canvas} className="vz-eef" aria-hidden="true" data-testid={`vz-eef-${cam.camera_id}`} />;
}
