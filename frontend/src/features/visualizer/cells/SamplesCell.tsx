import { useCallback, useEffect, useId, useRef, useState } from 'react';
import type { VizCamera, VizEpisodeCamera } from '../../../api/types';
import { bisectRight, framesIn } from '../../../lib/vizTime';
import { zh } from '../../../locales/zh';
import { CLOCK_CONFIG, type PlayerClock } from '../clock';
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
export function SamplesCell({ cam, ep, clock, onMode, onBusy }: { cam: VizCamera; ep: VizEpisodeCamera; clock: PlayerClock; onMode?: (key: string, client: boolean) => void; onBusy?: (id: string, busy: boolean) => void }) {
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
  if (mode === 'video') return <VideoCell cam={cam} ep={ep} clock={clock} onBusy={onBusy} />;
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
  const sourceId = `${cam.key}:${useId()}`;
  // nothing to draw yet: the GOP is on its way or in the decoder
  const [decoding, setDecoding] = useState(true);
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
    let playing = clock.getSnapshot().playing;
    const times = index.t;
    // playing, the GOPs of about readyAheadS more come too: what the clock waits for
    const prefetch = framesIn(times, CLOCK_CONFIG.readyAheadS + 1);
    // before the camera's first frame (its first keyframe comes a little after the episode's zero) it shows
    // that frame, as a <video> parked at its start does
    const at = () => Math.max(0, bisectRight(times, clock.getSnapshot().t + 1e-6));
    const paint = () => {
      const s = clock.getSnapshot();
      const k = at();
      stream.want(k, s.playing ? AHEAD : 0);
      if (s.playing) stream.prefetch(k, prefetch);
      const img = stream.get(k) ?? stream.nearest(k);
      if (k !== shown || img) drawFrame(el, img);
      if (stream.get(k)) shown = k;
      setDecoding(!img);
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
      const now = clock.getSnapshot().playing;
      if (at() !== shown || now !== playing) {
        playing = now;
        paint();
      }
    });
    const detach = clock.attachSource(sourceId, { ahead: (t) => stream.ahead(t) });
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
    ro?.observe(el);
    resize();
    return () => {
      unsub();
      detach();
      ro?.disconnect();
      stream.onFrame = null;
      stream.onError = null;
    };
  }, [stream, index, clock, onFail, sourceId]);

  return (
    <>
      <canvas ref={canvas} data-testid={`vz-samples-${cam.key}`} />
      {decoding ? (
        <div className="vz-overlay" role="status">
          {zh.viz.video.decoding}
        </div>
      ) : null}
    </>
  );
}
