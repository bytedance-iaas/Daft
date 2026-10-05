import { useEffect, useId, useRef, useState } from 'react';
import type { VizCamera, VizEpisodeCamera } from '../../../api/types';
import { zh } from '../../../locales/zh';
import type { PlayerClock } from '../clock';
import { probeMedia } from '../data';

/** Whether this browser decodes a codec (RFC 6381 string); unknown codecs are tried. */
export function canDecode(codecString: string | null): boolean {
  if (!codecString) return true;
  const type = `video/mp4; codecs="${codecString}"`;
  try {
    const ms = (globalThis as { MediaSource?: { isTypeSupported?: (t: string) => boolean } }).MediaSource;
    if (ms && typeof ms.isTypeSupported === 'function' && ms.isTypeSupported(type)) return true;
    if (typeof document !== 'undefined') return document.createElement('video').canPlayType(type) !== '';
  } catch {
    return true;
  }
  return false;
}

type Phase =
  | { kind: 'loading'; resigning: boolean }
  | { kind: 'pending'; progress: number | null }
  | { kind: 'ready' }
  | { kind: 'failed'; why: string }
  | { kind: 'unsupported'; why: string };

const POLL_MS = 1500;

/**
 * A <video> cell (design doc 18 §4.2, §5.2): the URL the episode answer gives, or the platform's
 * H.264 transcode when the browser cannot decode the original (202 with progress while the copy is
 * made); a video that fails to load falls back to the transcode too. A new URL (a presigned one
 * renewed) is swapped in where the clock is. The clock drives it: no native controls, no full
 * screen, no picture-in-picture.
 */
export function VideoCell({ cam, ep, clock, onBusy }: { cam: VizCamera; ep: VizEpisodeCamera; clock: PlayerClock; onBusy?: (id: string, busy: boolean) => void }) {
  const video = useRef<HTMLVideoElement | null>(null);
  const [phase, setPhase] = useState<Phase>({ kind: 'loading', resigning: false });
  const [src, setSrc] = useState<string | null>(null);
  // switched to the transcode after the original failed to play; a new URL starts over
  const [forced, setForced] = useState<string | null>(null);
  const played = useRef<string | null>(null);

  const wantTranscode = ep.access === 'transcode' || (!!ep.transcode_url && !canDecode(cam.codec_string)) || (forced !== null && forced === ep.url);
  const target = ep.access === 'unsupported' ? null : ep.access === 'transcode' ? (ep.url ?? ep.transcode_url) : wantTranscode ? ep.transcode_url : ep.url;
  const reason = ep.reason ?? cam.reason;

  useEffect(() => {
    if (!target) {
      setSrc(null);
      setPhase({ kind: 'unsupported', why: reason ?? zh.viz.video.unsupported });
      return undefined;
    }
    if (!wantTranscode) {
      setSrc(target);
      setPhase({ kind: 'loading', resigning: played.current !== null && played.current !== target });
      return undefined;
    }
    // the transcode: wait for it (202 with progress), then play the copy
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const ctl = new AbortController();
    const poll = async () => {
      const state = await probeMedia(target, ctl.signal).catch(() => null);
      if (!alive || !state) return;
      if (state.state === 'ready') {
        setSrc(target);
        setPhase({ kind: 'loading', resigning: false });
      } else if (state.state === 'pending') {
        setPhase({ kind: 'pending', progress: state.progress });
        timer = setTimeout(() => void poll(), POLL_MS);
      } else {
        setPhase({ kind: 'failed', why: state.message || zh.viz.video.failed });
      }
    };
    setSrc(null);
    setPhase({ kind: 'pending', progress: null });
    void poll();
    return () => {
      alive = false;
      ctl.abort();
      if (timer) clearTimeout(timer);
    };
  }, [target, wantTranscode, reason]);

  // a camera the platform is still transcoding holds 播放 back (requester 2026-10-05: it could not play
  // yet); one id a cell, so two cells of one camera, or a cell going away, do not clear each other
  const busyId = `${cam.key}:${useId()}`;
  const transcoding = phase.kind === 'pending';
  useEffect(() => {
    onBusy?.(busyId, transcoding);
  }, [onBusy, busyId, transcoding]);
  useEffect(() => () => onBusy?.(busyId, false), [onBusy, busyId]);

  // on the clock while it plays this URL
  useEffect(() => {
    const el = video.current;
    if (!el || !src) return undefined;
    played.current = src;
    return clock.attach(cam.key, el, { offset: ep.offset_s ?? 0, from: ep.from_ts ?? 0, end: ep.to_ts ?? null });
  }, [clock, cam.key, src, ep.offset_s, ep.from_ts, ep.to_ts]);

  const onError = () => {
    if (!wantTranscode && ep.transcode_url && ep.url) {
      setForced(ep.url); // the browser could not play it after all: the platform's H.264 copy
      return;
    }
    setPhase({ kind: 'failed', why: ep.transcode_url || wantTranscode ? zh.viz.video.failed : zh.viz.video.noTranscode });
  };

  let message: string | null = null;
  // an mcap camera's <video> waits for the Daemon to rewrap its stream the first time (cached afterwards)
  if (phase.kind === 'loading') message = phase.resigning ? zh.viz.video.resigning : ep.access === 'remux' ? zh.viz.video.remuxing : zh.viz.video.loading;
  else if (phase.kind === 'pending') message = zh.viz.video.transcoding(phase.progress);
  else if (phase.kind === 'failed') message = zh.viz.video.failed;
  else if (phase.kind === 'unsupported') message = zh.viz.video.unsupported;

  return (
    <>
      {src ? (
        <video
          ref={video}
          key={src}
          src={src}
          muted
          playsInline
          preload="auto"
          disablePictureInPicture
          controls={false}
          controlsList="nofullscreen nodownload noremoteplayback noplaybackrate"
          onDoubleClick={(e) => e.preventDefault()}
          onLoadedData={() => setPhase({ kind: 'ready' })}
          onError={onError}
          data-testid={`vz-video-${cam.key}`}
        />
      ) : null}
      {message ? (
        <div className={`vz-overlay${src ? ' veil' : ''}`} role="status">
          <span>
            {message}
            {phase.kind === 'failed' || phase.kind === 'unsupported' ? <span className="why">{phase.why}</span> : null}
          </span>
        </div>
      ) : null}
    </>
  );
}
