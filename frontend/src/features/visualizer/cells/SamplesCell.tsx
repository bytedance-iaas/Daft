import { useCallback, useEffect, useRef, useState } from 'react';
import type { VizCamera, VizEpisodeCamera } from '../../../api/types';
import { bisectRight } from '../../../lib/vizTime';
import { zh } from '../../../locales/zh';
import type { PlayerClock } from '../clock';
import { useFrameIndex } from '../data';
import { canDecodeSamples, SampleStream } from '../sampleStream';
import { drawFrame } from './FramesCell';
import { VideoCell } from './VideoCell';

/** Frames fed ahead of the one shown while playing (about half a second at 30 fps). */
const AHEAD = 15;

/**
 * An mcap H.264 / H.265 camera the browser decodes itself (design doc 19 §3): its sample pack through
 * WebCodecs, the frame under the clock on a canvas - the same rule as a JPEG frame pack, so every camera
 * shows the frame of the clock's moment. A browser without WebCodecs for the codec, or a decoder that
 * fails, gets the Daemon's remux in a <video> instead (VideoCell, with its transcode fallback).
 */
export function SamplesCell({ cam, ep, clock, onMode }: { cam: VizCamera; ep: VizEpisodeCamera; clock: PlayerClock; onMode?: (key: string, client: boolean) => void }) {
  const index = useFrameIndex(ep.index_url);
  const [mode, setMode] = useState<'checking' | 'client' | 'video'>('checking');
  useEffect(() => {
    if (index.isError) {
      setMode('video');
      return undefined;
    }
    if (!index.data) return undefined;
    let live = true;
    void canDecodeSamples(index.data).then((ok) => {
      if (live) setMode(ok ? 'client' : 'video');
    });
    return () => {
      live = false;
    };
  }, [index.data, index.isError]);
  useEffect(() => {
    if (mode !== 'checking') onMode?.(cam.key, mode === 'client');
  }, [mode, cam.key, onMode]);
  const fail = useCallback(() => setMode('video'), []);
  if (mode === 'video') return <VideoCell cam={cam} ep={ep} clock={clock} />;
  if (mode === 'checking' || !index.data || !ep.samples_url) {
    return (
      <div className="vz-overlay" role="status">
        {zh.viz.video.loading}
      </div>
    );
  }
  return <Decoded cam={cam} url={ep.samples_url} index={index.data} clock={clock} onFail={fail} />;
}

function Decoded({ cam, url, index, clock, onFail }: { cam: VizCamera; url: string; index: NonNullable<ReturnType<typeof useFrameIndex>['data']>; clock: PlayerClock; onFail: () => void }) {
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const [notYet, setNotYet] = useState(false);
  // made and let go in one effect: StrictMode's mount - cleanup - mount must not reuse a closed decoder
  const [stream, setStream] = useState<SampleStream | null>(null);
  useEffect(() => {
    const s = new SampleStream(url, index);
    setStream(s);
    return () => s.dispose();
  }, [url, index]);

  useEffect(() => {
    const el = canvas.current;
    if (!el || !stream) return undefined;
    let shown = -2;
    const times = index.t;
    const at = () => bisectRight(times, clock.getSnapshot().t + 1e-6);
    const paint = () => {
      const s = clock.getSnapshot();
      const k = at();
      setNotYet(k < 0);
      if (k >= 0) stream.want(k, s.playing ? AHEAD : 0);
      const img = k >= 0 ? (stream.get(k) ?? stream.nearest(k)) : undefined;
      if (k !== shown || img) drawFrame(el, img);
      if (stream.get(k)) shown = k;
    };
    const resize = () => {
      const r = el.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      el.width = Math.max(2, Math.round(r.width * dpr));
      el.height = Math.max(2, Math.round(r.height * dpr));
      shown = -2;
      paint();
    };
    stream.onFrame = (k) => {
      if (k === at()) {
        shown = -2;
        paint();
      }
    };
    stream.onError = () => onFail();
    const unsub = clock.subscribe(() => {
      if (at() !== shown) paint();
    });
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
    ro?.observe(el);
    resize();
    return () => {
      unsub();
      ro?.disconnect();
      stream.onFrame = null;
      stream.onError = null;
    };
  }, [stream, index, clock, onFail]);

  return (
    <>
      <canvas ref={canvas} data-testid={`vz-samples-${cam.key}`} />
      {notYet ? (
        <div className="vz-overlay" role="status">
          {zh.viz.video.notYet}
        </div>
      ) : null}
    </>
  );
}
