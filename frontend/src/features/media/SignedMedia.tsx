// Evidence frames are read straight from TOS through presigned URLs (D16, 03 §7): the Daemon signs,
// the browser fetches. A URL that fails (403, expired) is signed again and the image retried (07 §6).
// Camera videos are the visualizer's (features/visualizer), which signs through its own endpoints.
import { useQuery } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { api, unwrap } from '../../api/client';
import { LazyVisible } from '../../components/LazyVisible';
import { zh } from '../../locales/zh';

export type MediaScope = 'delivery' | 'input';

export interface MediaTarget {
  task: string;
  scope: MediaScope;
  path: string;
}

/** How many times in a row a failing URL is signed again before the element gives up. */
export const MEDIA_CONFIG = { maxResign: 2 };

export const signKey = (t: MediaTarget) => ['media-sign', t.task, t.scope, t.path] as const;

/** A presigned URL for one object; `resign()` fetches a fresh one (the old one failed). */
export function useSignedUrl(target: MediaTarget | null, enabled: boolean) {
  const q = useQuery({
    queryKey: target ? signKey(target) : ['media-sign', 'none'],
    queryFn: () => unwrap(api().GET('/media/sign', { params: { query: { task: target!.task, scope: target!.scope, path: target!.path } } })),
    enabled: enabled && Boolean(target),
    // We decide when to sign again: on a failed load.
    staleTime: Infinity,
    retry: false,
  });
  return {
    data: q.data,
    loading: q.isFetching && !q.data,
    error: q.error,
    resign: () => q.refetch(),
  };
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
