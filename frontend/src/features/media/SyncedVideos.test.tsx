import { fireEvent, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { recordRequests } from '../../test/record';
import { renderWithProviders } from '../../test/render';
import type { VideoRef } from './SignedMedia';
import { SyncedVideos } from './SyncedVideos';

const TASK = 'task_01HXR2D8';
const videos = (ep: number): VideoRef[] =>
  ['exterior_image_1_left', 'wrist_image_left', 'exterior_image_2_left'].map((camera) => ({
    camera,
    scope: 'input' as const,
    origin: 'source_dataset' as const,
    path: `videos/chunk-000/${camera}/file-000.mp4`,
    from_ts: ep * 14,
    to_ts: ep * 14 + 14,
  }));

/** jsdom's <video> never loads: give one a loaded, buffered state the controller can read. */
function makePlayable(el: HTMLVideoElement, from: number) {
  let paused = true;
  Object.defineProperty(el, 'readyState', { configurable: true, get: () => 4 });
  Object.defineProperty(el, 'duration', { configurable: true, get: () => 140 });
  Object.defineProperty(el, 'paused', { configurable: true, get: () => paused });
  Object.defineProperty(el, 'buffered', { configurable: true, get: () => ({ length: 1, start: () => from, end: () => from + 14 }) });
  el.play = vi.fn(() => {
    if (paused) {
      paused = false;
      fireEvent(el, new Event('play'));
    }
    return Promise.resolve();
  });
  el.pause = vi.fn(() => {
    if (!paused) {
      paused = true;
      fireEvent(el, new Event('pause'));
    }
  });
  fireEvent(el, new Event('loadedmetadata'));
  fireEvent(el, new Event('canplay'));
}

const signed = (seen: ReturnType<typeof recordRequests>) => seen.filter((r) => r.path === '/media/sign' && r.query.get('path')?.includes('videos'));

describe('SyncedVideos: 同时播放 (F6.2)', () => {
  afterEach(() => vi.restoreAllMocks());

  it('signs and plays nothing until asked, buffers every camera first, then plays them together', async () => {
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play');
    const seen = recordRequests();
    const { user } = renderWithProviders(<SyncedVideos task={TASK} videos={videos(2)} />);
    expect(screen.getAllByRole('button', { name: /点击加载视频/ })).toHaveLength(3);
    expect(signed(seen)).toHaveLength(0);

    await user.click(screen.getByRole('button', { name: '同时播放' }));
    await waitFor(() => expect(signed(seen)).toHaveLength(3));
    const els = await Promise.all(['exterior_image_1_left', 'wrist_image_left', 'exterior_image_2_left'].map((c) => screen.findByTestId(`video-${c}`)));
    expect(screen.getByRole('status')).toHaveTextContent('缓冲中…');
    expect(screen.getByRole('button', { name: '停止同步' })).toBeInTheDocument();
    expect(play).not.toHaveBeenCalled();

    // Two cameras ready: still waiting for the third.
    makePlayable(els[0] as HTMLVideoElement, 28);
    makePlayable(els[1] as HTMLVideoElement, 28);
    expect(screen.getByRole('status')).toHaveTextContent('缓冲中…');
    expect((els[0] as HTMLVideoElement).play).not.toHaveBeenCalled();
    makePlayable(els[2] as HTMLVideoElement, 28);
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('同步播放中'));
    for (const el of els) {
      expect((el as HTMLVideoElement).play).toHaveBeenCalledTimes(1);
      expect((el as HTMLVideoElement).currentTime).toBe(28); // the episode's start in the v3 file
    }

    // 停止同步 pauses them and gives the button back.
    await user.click(screen.getByRole('button', { name: '停止同步' }));
    expect(screen.queryByRole('status')).toBeNull();
    for (const el of els) expect((el as HTMLVideoElement).pause).toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '同时播放' })).toBeInTheDocument();
  });

  it('a new episode is a new group: nothing is signed or played for it by itself', async () => {
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play');
    const seen = recordRequests();
    function Harness() {
      const [ep, setEp] = useState(2);
      return (
        <>
          <button type="button" onClick={() => setEp(3)}>
            下一条
          </button>
          <SyncedVideos key={`ep-${ep}`} task={TASK} videos={videos(ep)} />
        </>
      );
    }
    const { user } = renderWithProviders(<Harness />);
    await user.click(screen.getByRole('button', { name: '同时播放' }));
    await waitFor(() => expect(signed(seen)).toHaveLength(3));
    await user.click(screen.getByRole('button', { name: '下一条' }));
    expect(await screen.findAllByRole('button', { name: /点击加载视频/ })).toHaveLength(3);
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByRole('button', { name: '同时播放' })).toBeInTheDocument();
    expect(play).not.toHaveBeenCalled();
    expect(signed(seen)).toHaveLength(3);
  });

  it('one camera has no button to sync with', () => {
    renderWithProviders(<SyncedVideos task={TASK} videos={videos(1).slice(0, 1)} />);
    expect(screen.queryByRole('button', { name: '同时播放' })).toBeNull();
    expect(screen.getByRole('button', { name: /点击加载视频/ })).toBeInTheDocument();
  });
});
