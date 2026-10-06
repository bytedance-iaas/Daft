// One camera of the EEF opinion (design doc 20): the camera's own video from the task's input, with the
// marks the model saw (C4 EefOverlay) drawn on a canvas above it. Nothing marked is saved anywhere: the
// Daemon computes the marks from the trajectory bundle on request and the browser paints them per frame.
import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api, unwrap } from '../../api/client';
import { ApiError } from '../../api/errors';
import type { EefOverlay } from '../../api/types';
import { clipTimeOf, containFit, drawFrame, frameMap, sampleFrameAt } from '../../lib/eefOverlay';
import { zh } from '../../locales/zh';
import { MEDIA_CONFIG } from '../media/SignedMedia';
import { useVizEpisode } from '../visualizer/data';

const O = () => zh.eefDetail.opinion;

export function useEefOverlay(taskId: string, episode: number) {
  return useQuery<EefOverlay>({
    queryKey: ['eef-overlay', taskId, episode],
    queryFn: () => unwrap(api().GET('/tasks/{id}/episodes/{index}/eef-overlay', { params: { path: { id: taskId, index: episode } } })),
    staleTime: Infinity,
    retry: (n, e) => n < 1 && !(e instanceof ApiError && e.status >= 400 && e.status < 500),
  });
}

/** Asks the player to show a sample frame; `n` changes on every ask, so the same frame can be asked twice. */
export interface SeekAsk {
  frame: number;
  n: number;
}

export function EefOverlayVideo({ taskId, episode, camera, seek }: { taskId: string; episode: number; camera: string; seek: SeekAsk | null }) {
  const overlay = useEefOverlay(taskId, episode);
  const ep = useVizEpisode({ scope: 'task', id: taskId }, episode);
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [attempts, setAttempts] = useState(0);
  const [failed, setFailed] = useState(false);
  const oc = overlay.data?.cameras.find((c) => c.camera_id === camera);
  const vc = oc?.viz_camera ? ep.data?.cameras.find((c) => c.key === oc.viz_camera) : undefined;
  const url = vc && vc.kind === 'video' ? vc.url : null;
  const from = vc?.from_ts ?? 0;
  const to = vc?.to_ts ?? null;
  const fallbackFps = ep.data?.fps ?? null;

  useEffect(() => {
    const video = videoRef.current;
    const canvas = canvasRef.current;
    if (!video || !canvas || !oc || !url) return undefined;
    const map = frameMap(oc, fallbackFps);
    let raf = 0;
    const paint = () => {
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      const dpr = window.devicePixelRatio || 1;
      if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
      }
      const ctx = canvas.getContext('2d');
      if (!ctx || !w || !h) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const fit = containFit(w, h, oc.image_size_wh[0], oc.image_size_wh[1]);
      drawFrame(ctx, oc, map ? sampleFrameAt(map, video.currentTime - from) : null, fit, { w, h });
    };
    const play = () => {
      cancelAnimationFrame(raf);
      const tick = () => {
        paint();
        if (!video.paused && !video.ended) raf = requestAnimationFrame(tick);
      };
      tick();
    };
    const meta = () => {
      if (from > 0 && video.currentTime < from) video.currentTime = from;      // a LeRobot v3 file holds several episodes
      paint();
    };
    const time = () => {
      if (to !== null && video.currentTime >= to) video.pause();
    };
    const on: [string, () => void][] = [['play', play], ['loadedmetadata', meta], ['loadeddata', paint], ['seeked', paint], ['pause', paint], ['timeupdate', time]];
    on.forEach(([e, fn]) => video.addEventListener(e, fn));
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(paint) : null;
    ro?.observe(canvas);
    return () => {
      cancelAnimationFrame(raf);
      on.forEach(([e, fn]) => video.removeEventListener(e, fn));
      ro?.disconnect();
    };
  }, [oc, url, from, to, fallbackFps, attempts]);

  useEffect(() => {
    const video = videoRef.current;
    if (!seek || !video || !oc) return;
    const map = frameMap(oc, fallbackFps);
    const t = map ? clipTimeOf(map, seek.frame) : null;
    if (t === null) return;
    video.pause();
    video.currentTime = from + t;
  }, [seek, oc, from, fallbackFps]);

  if (overlay.isLoading || (oc?.viz_camera && ep.isLoading)) return <div className="episode-line muted">{O().videoLoading}</div>;
  if (overlay.error || failed) return <div className="episode-line warn" role="alert">{O().videoFailed}</div>;
  if (!oc || oc.skipped) return null;
  if (!url) return <div className="episode-line muted">{O().noSource(vc?.reason ?? null)}</div>;
  const [w, h] = oc.image_size_wh;
  return (
    <div className="eef-opinion-video">
      <div className="episode-line">{O().video}</div>
      <div style={{ position: 'relative', width: '100%', maxWidth: 960, aspectRatio: `${w} / ${h}`, background: '#000' }}>
        <video key={`${url}:${attempts}`} ref={videoRef} controls preload="metadata" src={url} aria-label={O().video}
          style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }}
          onError={() => {
            if (attempts >= MEDIA_CONFIG.maxResign) setFailed(true);
            else void ep.refetch().then(() => setAttempts((n) => n + 1));
          }} />
        <canvas ref={canvasRef} aria-hidden="true" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }} />
      </div>
    </div>
  );
}
