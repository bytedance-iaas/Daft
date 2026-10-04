import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { StrictMode } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { zh } from '../../locales/zh';
import { renderWithProviders } from '../../test/render';
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
    expect(within(player).getByText('关节 · rad')).toBeInTheDocument();
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
    await user.click(within(menu).getByText('夹爪'));
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
    await waitFor(() => expect(within(side).getByText(zh.viz.side.curve('关节'))).toBeInTheDocument());
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
});
