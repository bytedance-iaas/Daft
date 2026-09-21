// Videos and evidence frames are read straight from TOS through presigned URLs (D16, 03 §7):
// the Daemon signs, the browser fetches. A URL that fails (403, expired) is signed again and the
// element retried automatically, keeping the playback position (07 §6).
import { Button, Spin } from '@arco-design/web-react';
import { IconPlayArrow } from '@arco-design/web-react/icon';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { api, unwrap } from '../../api/client';
import { errorMessage } from '../../api/errors';
import { LazyVisible } from '../../components/LazyVisible';
import { zh } from '../../locales/zh';

export type MediaScope = 'delivery' | 'input';

export interface MediaTarget {
  task: string;
  scope: MediaScope;
  path: string;
}

export interface VideoRef {
  camera: string;
  scope: MediaScope;
  path: string;
  origin?: 'clip' | 'delivery_dataset' | 'source_dataset';
  from_ts?: number;
  to_ts?: number;
}

/** How many times in a row a failing URL is signed again before the element gives up. */
export const MEDIA_CONFIG = { maxResign: 2, expirySlackMs: 30_000 };

export const signKey = (t: MediaTarget) => ['media-sign', t.task, t.scope, t.path] as const;

/** A presigned URL for one object; `resign()` fetches a fresh one (the old one failed or expired). */
export function useSignedUrl(target: MediaTarget | null, enabled: boolean) {
  const q = useQuery({
    queryKey: target ? signKey(target) : ['media-sign', 'none'],
    queryFn: () => unwrap(api().GET('/media/sign', { params: { query: { task: target!.task, scope: target!.scope, path: target!.path } } })),
    enabled: enabled && Boolean(target),
    // We decide when to sign again: on a failed load, or when a stale URL is about to be used.
    staleTime: Infinity,
    retry: false,
  });
  const expired = q.data ? q.data.expires_at - Date.now() < MEDIA_CONFIG.expirySlackMs : false;
  return {
    data: q.data,
    loading: q.isFetching && !q.data,
    error: q.error,
    expired,
    resign: () => q.refetch(),
  };
}

/** `url#t=from,to` for sources where one file holds several episodes (LeRobot v3). */
export function withFragment(url: string, from?: number | null, to?: number | null): string {
  if (from === undefined || from === null) return url;
  return `${url}#t=${from}${to !== undefined && to !== null ? `,${to}` : ''}`;
}

/**
 * One camera's video. Nothing is signed until the user asks for it (click, or 「同时播放」 via
 * `playSignal`), so opening a page never signs every video on it (07 §9).
 */
export function SignedVideo({ task, video, playSignal = 0, caption }: { task: string; video: VideoRef; playSignal?: number; caption?: ReactNode }) {
  const [requested, setRequested] = useState(false);
  const [failed, setFailed] = useState(false);
  const sign = useSignedUrl({ task, scope: video.scope, path: video.path }, requested);
  const el = useRef<HTMLVideoElement | null>(null);
  const failures = useRef(0);
  const wantPlay = useRef(false);
  const resume = useRef<{ time: number; play: boolean } | null>(null);
  const url = sign.data ? withFragment(sign.data.url, sign.data.from_ts ?? video.from_ts, sign.data.to_ts ?? video.to_ts) : null;

  useEffect(() => {
    if (!playSignal) return;
    wantPlay.current = true;
    setRequested(true);
    if (url && el.current) {
      wantPlay.current = false;
      void el.current.play()?.catch(() => undefined);
    }
    // Only a new signal plays; url changes are handled below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playSignal]);

  useEffect(() => {
    if (url && wantPlay.current && el.current) {
      wantPlay.current = false;
      void el.current.play()?.catch(() => undefined);
    }
  }, [url]);

  const retryFromScratch = () => {
    failures.current = 0;
    setFailed(false);
    void sign.resign();
  };

  const onError = () => {
    if (failures.current >= MEDIA_CONFIG.maxResign) {
      setFailed(true);
      return;
    }
    failures.current += 1;
    const v = el.current;
    resume.current = v ? { time: v.currentTime, play: !v.paused } : null;
    void sign.resign();
  };

  const onLoaded = () => {
    const v = el.current;
    const r = resume.current;
    resume.current = null;
    if (!v || !r) return;
    if (r.time) v.currentTime = r.time;
    if (r.play) void v.play()?.catch(() => undefined);
  };

  const onPlay = () => {
    // A URL that expired while paused is signed again before the browser tries it.
    if (sign.expired) onError();
  };

  let body: ReactNode;
  if (!requested) {
    body = (
      <button type="button" className="video-placeholder" onClick={() => setRequested(true)} aria-label={`${zh.report.videoLoad}：${video.camera}`}>
        <IconPlayArrow style={{ fontSize: 28 }} />
        <span>{zh.report.videoLoad}</span>
      </button>
    );
  } else if (failed || (sign.error && !sign.data)) {
    body = (
      <div className="video-placeholder" role="alert">
        <span>{sign.error && !sign.data ? errorMessage(sign.error) : zh.report.videoFailed}</span>
        <Button size="mini" onClick={retryFromScratch}>
          {zh.report.videoRetry}
        </Button>
      </div>
    );
  } else if (!url) {
    body = (
      <div className="video-placeholder">
        <Spin size={16} />
        <span>{zh.report.videoSigning}</span>
      </div>
    );
  } else {
    body = (
      <video
        ref={el}
        src={url}
        controls
        muted
        playsInline
        preload="metadata"
        onError={onError}
        onLoadedMetadata={onLoaded}
        onLoadedData={() => {
          failures.current = 0;
        }}
        onPlay={onPlay}
        data-testid={`video-${video.camera}`}
        aria-label={video.camera}
      />
    );
  }
  return (
    <figure className="video-box">
      {body}
      <figcaption className="muted mono">
        {video.camera}
        {caption ? <> · {caption}</> : null}
      </figcaption>
    </figure>
  );
}

/** An evidence frame, signed when it scrolls into view; a failed load is signed again once. */
export function SignedImage({ task, scope, path, alt }: { task: string; scope: MediaScope; path: string; alt: string }) {
  return (
    <LazyVisible placeholder={<div className="evidence-frame" />}>
      <SignedImageInner task={task} scope={scope} path={path} alt={alt} />
    </LazyVisible>
  );
}

function SignedImageInner({ task, scope, path, alt }: { task: string; scope: MediaScope; path: string; alt: string }) {
  const sign = useSignedUrl({ task, scope, path }, true);
  const failures = useRef(0);
  const [failed, setFailed] = useState(false);
  if (failed || (sign.error && !sign.data)) {
    return (
      <div className="evidence-frame muted" role="alert">
        {zh.report.imageFailed}
      </div>
    );
  }
  if (!sign.data) return <div className="evidence-frame" />;
  return (
    <img
      className="evidence-frame"
      src={sign.data.url}
      alt={alt}
      loading="lazy"
      onError={() => {
        if (failures.current >= MEDIA_CONFIG.maxResign) setFailed(true);
        else {
          failures.current += 1;
          void sign.resign();
        }
      }}
    />
  );
}
