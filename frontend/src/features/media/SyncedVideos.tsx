import { Button, Space } from '@arco-design/web-react';
import { useEffect, useState, useSyncExternalStore, type ReactNode } from 'react';
import { zh } from '../../locales/zh';
import { SignedVideo, type VideoRef } from './SignedMedia';
import { SyncController } from './syncPlayback';

/** A stable id for one camera of one episode. */
export function videoKey(v: VideoRef): string {
  return `${v.camera}|${v.scope}|${v.path}|${v.from_ts ?? ''}`;
}

/**
 * Every camera of one episode with 「同时播放」 (07 §9, F6.2). Nothing is signed or played until
 * the button is pressed; then all URLs are signed, buffered and started together, and a camera
 * that runs short pauses all of them (「缓冲中…」). The button turns into 「停止同步」. Give it a
 * `key` per episode: a new episode is a new group, and nothing carries over or starts by itself.
 */
export function SyncedVideos({ task, videos, caption, extra }: { task: string; videos: readonly VideoRef[]; caption?: (v: VideoRef) => ReactNode; extra?: ReactNode }) {
  const [controller] = useState(() => new SyncController());
  useEffect(() => () => controller.dispose(), [controller]);
  const status = useSyncExternalStore(controller.subscribe, controller.getStatus, controller.getStatus);
  const [load, setLoad] = useState(false);
  const synced = status !== 'idle';
  const toggle = () => {
    if (controller.isActive()) {
      controller.stop();
      return;
    }
    setLoad(true);
    controller.start(videos.map(videoKey));
  };
  return (
    <div className="synced-videos" data-sync-status={status}>
      {videos.length > 1 || extra ? (
        <Space wrap size={8} style={{ marginBottom: 8 }}>
          {videos.length > 1 ? (
            <Button size="small" type={synced ? 'secondary' : 'primary'} onClick={toggle}>
              {synced ? zh.report.stopSync : zh.report.playAll}
            </Button>
          ) : null}
          {synced ? (
            <span role="status" className={status === 'loading' || status === 'buffering' ? 'sync-note waiting' : 'sync-note'} data-testid="sync-status">
              {zh.report.syncStatus[status]}
            </span>
          ) : null}
          {extra}
        </Space>
      ) : null}
      <div className="video-grid">
        {videos.map((v) => (
          <SignedVideo key={videoKey(v)} task={task} video={v} load={load} controller={controller} syncId={videoKey(v)} caption={caption?.(v)} />
        ))}
      </div>
    </div>
  );
}
