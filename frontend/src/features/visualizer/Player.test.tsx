import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { StrictMode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { CAMERA_PALETTE } from '../../lib/vizCurves';
import { zh } from '../../locales/zh';
import { db } from '../../mocks/db';
import { server } from '../../mocks/server';
import { sampleIndex } from '../../mocks/vizWorld';
import { DATASET_PROFILES, datasetDetail } from '../../mocks/world';
import { renderWithProviders } from '../../test/render';
import { PlayerClock } from './clock';
import { Player } from './Player';

const LEROBOT = { scope: 'dataset' as const, id: 'ds_droid200' };
const MCAP = { scope: 'dataset' as const, id: 'ds_mcap' };

async function ready() {
  return screen.findByTestId('vz-player', {}, { timeout: 5000 });
}

function frameInput(): HTMLInputElement {
  return screen.getByLabelText(zh.viz.frameInput) as HTMLInputElement;
}

describe('Player (design doc 18 §5)', () => {
  beforeEach(() => {
    // a browser that decodes H.264 and AV1 but not MPEG-4 Part 2 (jsdom answers '' to everything)
    HTMLMediaElement.prototype.canPlayType = (type: string) => (/avc1|av01/.test(type) ? 'probably' : '');
  });

  it('shows an episode: the head, the smart layout, a transcoded camera and the curve groups', async () => {
    renderWithProviders(
      <StrictMode>
        <Player source={LEROBOT} index={0} />
      </StrictMode>,
    );
    const player = await ready();
    expect(within(player).getByText('droid-200')).toBeInTheDocument();
    expect(within(player).getByText('ep 0')).toBeInTheDocument();
    expect(player.querySelector('.vz-head .meta')?.textContent).toContain('LeRobot v2');
    // three cameras and the first two smart curve groups, three columns
    await waitFor(() => expect(player.querySelectorAll('.vz-cell.kind-video')).toHaveLength(3));
    expect(player.querySelectorAll('.vz-cell.kind-curve')).toHaveLength(2);
    expect((player.querySelector('.vz-grid') as HTMLElement).style.gridTemplateColumns).toBe('repeat(3, minmax(0, 1fr))');
    // the mpeg4 wrist camera is the platform's H.264 copy, and says so
    const tags = player.querySelectorAll('.vz-tc');
    expect(tags).toHaveLength(1);
    expect(tags[0].closest('.vz-cell')?.textContent).toContain('wrist');
    // the dataset's own names, not translated (2026-10-04)
    expect(within(player).getByText('observation.state / action · rad')).toBeInTheDocument();
    // no native controls, no full screen (the <video> comes a render after its cell: the URL is chosen in an effect)
    const v = await waitFor(() => {
      const el = player.querySelector('video');
      expect(el).not.toBeNull();
      return el as HTMLVideoElement;
    });
    expect(v.controls).toBe(false);
    expect(v.getAttribute('controlslist')).toContain('nofullscreen');
    expect(v.hasAttribute('disablepictureinpicture')).toBe(true);
  });

  it('switches templates and lets a cell be cleared and chosen again', async () => {
    const { user } = renderWithProviders(
      <StrictMode>
        <Player source={LEROBOT} index={0} />
      </StrictMode>,
    );
    const player = await ready();
    await waitFor(() => expect(player.querySelectorAll('.vz-cell.kind-video')).toHaveLength(3));
    // only videos
    await user.click(within(player).getByText(zh.viz.layouts.smart));
    await user.click(await screen.findByText(zh.viz.layouts.video));
    await waitFor(() => expect(player.querySelectorAll('.vz-cell')).toHaveLength(3));
    expect(player.querySelectorAll('.vz-cell.kind-curve')).toHaveLength(0);
    // clear the first cell: it becomes a 「+」, and the layout is the user's now
    const first = player.querySelector('.vz-cell') as HTMLElement;
    await user.click(within(first).getByLabelText(zh.viz.cell.clear));
    expect(player.querySelector('.vz-cell')?.classList.contains('kind-empty')).toBe(true);
    expect(within(player).getByText(zh.viz.layouts.custom)).toBeInTheDocument();
    await user.click(within(player).getByText(zh.viz.cell.choose));
    const menu = await screen.findByTestId('vz-menu');
    expect(within(menu).getByText(zh.viz.menu.cameras)).toBeInTheDocument();
    await user.click(within(menu).getByText('observation.state / action · gripper'));
    await waitFor(() => expect(player.querySelector('.vz-cell')?.classList.contains('kind-curve')).toBe(true));
    expect(screen.queryByTestId('vz-menu')).toBeNull();
  });

  it('opens the info panel for the focused cell', async () => {
    const { user } = renderWithProviders(
      <StrictMode>
        <Player source={LEROBOT} index={0} />
      </StrictMode>,
    );
    const player = await ready();
    await waitFor(() => expect(player.querySelectorAll('.vz-cell.kind-video')).toHaveLength(3));
    await user.click(within(player).getByText(zh.viz.info));
    const side = await screen.findByTestId('vz-side');
    expect(within(side).getByText(zh.viz.side.none)).toBeInTheDocument();
    fireEvent.pointerDown(player.querySelectorAll('.vz-cell.kind-video')[2]);
    await waitFor(() => expect(within(side).getByText(zh.viz.side.camera('wrist'))).toBeInTheDocument());
    expect(within(side).getByText(zh.viz.side.codecTranscoded('mpeg4'))).toBeInTheDocument();
    expect(within(side).getByText(zh.viz.access.transcode)).toBeInTheDocument();
    fireEvent.pointerDown(player.querySelectorAll('.vz-cell.kind-curve')[0]);
    await waitFor(() => expect(within(side).getByText(zh.viz.side.curve('observation.state / action'))).toBeInTheDocument());
    expect(within(side).getAllByRole('checkbox').length).toBe(7);
    await user.click(within(side).getByLabelText(zh.viz.side.close));
    expect(screen.queryByTestId('vz-side')).toBeNull();
  });

  it('plays and pauses on a live clock (StrictMode runs the effects twice)', async () => {
    const { user } = renderWithProviders(
      <StrictMode>
        <Player source={LEROBOT} index={0} />
      </StrictMode>,
    );
    await ready();
    const play = await screen.findByTestId('vz-play');
    expect(play.querySelector('.arco-icon-play-arrow-fill')).not.toBeNull();
    await user.click(play);
    await waitFor(() => expect(screen.getByTestId('vz-play').querySelector('.arco-icon-pause')).not.toBeNull());
    await user.click(screen.getByTestId('vz-play'));
    await waitFor(() => expect(screen.getByTestId('vz-play').querySelector('.arco-icon-play-arrow-fill')).not.toBeNull());
  });

  it('steps frames with the keyboard and jumps to a typed frame', async () => {
    const { user } = renderWithProviders(
      <StrictMode>
        <Player source={LEROBOT} index={0} />
      </StrictMode>,
    );
    const player = await ready();
    // frames count from 1 on screen (design doc 18 §4.4): the first frame is 1, the box says of how many
    await waitFor(() => expect(frameInput().value).toBe('1'));
    expect(frameInput().parentElement?.textContent).toMatch(/\/\d+$/);
    player.focus();
    fireEvent.keyDown(player, { key: 'ArrowRight' });
    await waitFor(() => expect(frameInput().value).toBe('2'));
    fireEvent.keyDown(player, { key: 'ArrowRight', shiftKey: true }); // one second on
    await waitFor(() => expect(Number(frameInput().value)).toBeGreaterThan(5));
    fireEvent.keyDown(player, { key: 'ArrowLeft', shiftKey: true });
    await waitFor(() => expect(frameInput().value).toBe('2'));
    await user.clear(frameInput());
    await user.type(frameInput(), '42{Enter}');
    await waitFor(() => expect(frameInput().value).toBe('42'));
    expect(player.querySelector('.vz-stamp')?.textContent).toMatch(/帧 42$/);
    // typing in the frame box never reaches the player's keys
    fireEvent.keyDown(frameInput(), { key: 'ArrowRight' });
    expect(frameInput().value).toBe('42');
  });

  it('lays out ten cameras and two curve groups in four columns on a wide grid, three on a narrower one (design doc 19 §2)', async () => {
    const rh20t = DATASET_PROFILES.find((p) => p.name === 'rh20t_cfg1')!;
    db.datasets.push(datasetDetail('ds_rh20t', rh20t, Date.now(), { region: 'cn-beijing', credential: 'readonly-tos' }));
    const source = { scope: 'dataset' as const, id: 'ds_rh20t' };
    // jsdom lays nothing out: the grid is 1200 px wide by default, under the four-column width
    const first = renderWithProviders(<Player source={source} index={0} />);
    let player = await ready();
    await waitFor(() => expect(player.querySelectorAll('.vz-cell.kind-video')).toHaveLength(10));
    expect(player.querySelectorAll('.vz-cell.kind-curve')).toHaveLength(2);
    expect((player.querySelector('.vz-grid') as HTMLElement).style.gridTemplateColumns).toBe('repeat(3, minmax(0, 1fr))');
    expect(screen.queryByTestId('vz-overflow')).toBeNull();
    // sixteen colours: the tenth camera's dot is not the second's
    const dots = [...player.querySelectorAll('.vz-cell.kind-video .vz-cap .dot')].map((d) => (d as HTMLElement).style.color);
    expect(new Set(dots).size).toBe(10);
    expect(CAMERA_PALETTE).toHaveLength(16);
    first.unmount();
    // a 1400 px grid: four columns, three rows, and the tools of a 340 px cell keep their words
    const width = vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(1424);
    renderWithProviders(<Player source={source} index={0} />);
    player = await ready();
    await waitFor(() => expect((player.querySelector('.vz-grid') as HTMLElement).style.gridTemplateColumns).toBe('repeat(4, minmax(0, 1fr))'));
    expect(player.querySelectorAll('.vz-cell')).toHaveLength(12);
    expect(player.querySelector('.vz-grid')?.classList.contains('is-narrow')).toBe(false);
    width.mockRestore();
  });

  it('holds 播放 while a camera is being transcoded, and lets it go once the copy is ready (2026-10-05)', async () => {
    renderWithProviders(<Player source={LEROBOT} index={0} />);
    const player = await ready();
    const play = within(player).getByTestId('vz-play');
    // droid-200's mpeg4 wrist camera: the first ask starts the platform's transcode
    await waitFor(() => expect(play).toBeDisabled());
    expect(play).toHaveAttribute('title', zh.viz.playBlocked);
    fireEvent.keyDown(player, { code: 'Space' });
    expect(play).toBeDisabled();                                         // the space bar does not start it either
    // the next poll finds the copy ready: 播放 is back
    await waitFor(() => expect(play).not.toBeDisabled());
    expect(play).toHaveAttribute('title', zh.viz.play);
  });

  it('plays the camera of a Lance dataset out of its table, through the Daemon (design doc 19 §4)', async () => {
    const pusht = DATASET_PROFILES.find((p) => p.name === 'pusht_lance')!;
    db.datasets.push(datasetDetail('ds_pusht_lance', pusht, Date.now(), { region: 'cn-beijing', credential: 'readonly-tos' }));
    renderWithProviders(<Player source={{ scope: 'dataset', id: 'ds_pusht_lance' }} index={3} />);
    const player = await ready();
    expect(player.querySelector('.vz-head .meta')?.textContent).toContain('Lance（lerobot-lancedb 0.3 三表）');
    const v = await waitFor(() => {
      const el = player.querySelector('video');
      expect(el).not.toBeNull();
      return el as HTMLVideoElement;
    });
    expect(v.getAttribute('src') ?? v.currentSrc).toContain('/datasets/ds_pusht_lance/episodes/3/cameras/image.mp4');
  });

  it('draws an mcap JPEG camera from its frame pack and plays the H.264 one as a video', async () => {
    renderWithProviders(
      <StrictMode>
        <Player source={MCAP} index={0} />
      </StrictMode>,
    );
    const player = await ready();
    await waitFor(() => expect(player.querySelectorAll('.vz-cell.kind-video')).toHaveLength(2));
    await waitFor(() => expect(player.querySelector('canvas[data-testid^="vz-frames-"]')).not.toBeNull());
    await waitFor(() => expect(player.querySelector('video[data-testid^="vz-video-"]')).not.toBeNull());
    // the IMU is drawable but not part of the smart layout
    expect(within(player).queryByText(/^IMU/)).toBeNull();
  });

  it('decodes an mcap H.264 camera in the browser when WebCodecs can, and says so (design doc 19 §3)', async () => {
    const supported = vi.fn(async () => ({ supported: true }));
    class FakeDecoder {
      static isConfigSupported = supported;
      decodeQueueSize = 0;
      configure() {}
      decode() {}
      reset() {}
      close() {}
    }
    vi.stubGlobal('VideoDecoder', FakeDecoder);
    vi.stubGlobal('EncodedVideoChunk', class {});
    try {
      const { user } = renderWithProviders(<Player source={MCAP} index={0} />);
      const player = await ready();
      const canvas = await waitFor(() => {
        const el = player.querySelector('canvas[data-testid^="vz-samples-"]');
        expect(el).not.toBeNull();
        return el as HTMLCanvasElement;
      });
      expect(supported).toHaveBeenCalledWith(expect.objectContaining({ codec: 'avc1.64001f', codedWidth: 640, codedHeight: 480 }));
      expect(player.querySelector('video[data-testid^="vz-video-"]')).toBeNull();
      // the info panel of that camera: decoded by the browser
      const cell = canvas.closest('.vz-cell') as HTMLElement;
      fireEvent.pointerDown(cell);
      await user.click(within(player).getByText(zh.viz.info));
      expect(await within(player).findByText(zh.viz.access.client)).toBeInTheDocument();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('shows a camera that starts after the episode zero from its first frame, decoding it at once (2026-10-04)', async () => {
    const decode = vi.fn();
    class FakeDecoder {
      static isConfigSupported = vi.fn(async () => ({ supported: true }));
      decodeQueueSize = 0;
      configure() {}
      decode(chunk: unknown) {
        decode(chunk);
      }
      reset() {}
      close() {}
    }
    vi.stubGlobal('VideoDecoder', FakeDecoder);
    vi.stubGlobal('EncodedVideoChunk', class {});
    // the H.264 camera's first keyframe comes 1 s after the episode's zero (GenRobot's recordings start mid-GOP)
    server.use(
      http.get('*/episodes/:index/cameras/observation_images_wrist.json', () => {
        const index = sampleIndex('observation_images_wrist', 90, 30);
        return HttpResponse.json({ ...index, t: index.t.map((t) => t + 1) });
      }),
    );
    try {
      renderWithProviders(<Player source={MCAP} index={0} />);
      const player = await ready();
      const canvas = await waitFor(() => {
        const el = player.querySelector('canvas[data-testid="vz-samples-observation_images_wrist"]');
        expect(el).not.toBeNull();
        return el as HTMLCanvasElement;
      });
      // the clock is at 0, before that frame: the frame is fetched and fed now, not once the clock gets there
      await waitFor(() => expect(decode).toHaveBeenCalled());
      // the fake decoder gives nothing back: the cell says it is decoding
      expect(within(canvas.closest('.vz-cell') as HTMLElement).getByText(zh.viz.video.decoding)).toBeInTheDocument();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe('Player · two cells of one camera (design doc 21 §2)', () => {
  beforeEach(() => {
    HTMLMediaElement.prototype.canPlayType = (type: string) => (/avc1|av01/.test(type) ? 'probably' : '');
  });

  it('keeps both on the clock, each under its own id', async () => {
    const ids: string[] = [];
    const real = PlayerClock.prototype.attach;
    const spy = vi.spyOn(PlayerClock.prototype, 'attach').mockImplementation(function (this: PlayerClock, id, el, b) {
      ids.push(id);
      return real.call(this, id, el, b);
    });
    const key = 'exterior_1';
    renderWithProviders(<Player source={LEROBOT} index={0} arrangement={{ cells: [{ kind: 'video', key }, { kind: 'video', key }], shape: { cols: 2, rows: 1 } }} />);
    await ready();
    await waitFor(() => expect(new Set(ids.filter((i) => i.startsWith(`${key}:`))).size).toBe(2));
    spy.mockRestore();
  });
});
