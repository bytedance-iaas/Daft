import { screen, waitFor, within } from '@testing-library/react';
import { useState } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { zh } from '../../locales/zh';
import { db } from '../../mocks/db';
import { renderWithProviders } from '../../test/render';
import { Player } from './Player';

// droid-200: cameras exterior_1, exterior_2, wrist; curve groups observation_state, observation_state.gripper
const DROID200 = { scope: 'dataset' as const, id: 'ds_droid200' };

async function ready() {
  return screen.findByTestId('vz-player', {}, { timeout: 5000 });
}

const videoKeys = (player: HTMLElement) => [...player.querySelectorAll('.vz-cell.kind-video .vz-cap')].map((e) => e.textContent);

/** A page that opens another episode of the same dataset (the visualize page's rail). */
function Episodes() {
  const [index, setIndex] = useState(0);
  return (
    <>
      <button type="button" onClick={() => setIndex(1)}>
        next-episode
      </button>
      <Player source={DROID200} index={index} />
    </>
  );
}

async function pickMenu(user: ReturnType<typeof renderWithProviders>['user'], player: HTMLElement, item: string) {
  await user.click(within(player).getByTestId('vz-layout-menu'));
  await user.click(await screen.findByText(item));
}

describe('the dataset’s default layout (design doc 21 §6.4)', () => {
  beforeEach(() => {
    HTMLMediaElement.prototype.canPlayType = (type: string) => (/avc1|av01/.test(type) ? 'probably' : '');
  });

  it('starts from the saved layout, its hidden lines and its playback', async () => {
    db.vizDisplays.set('ds_droid200', {
      config: {
        layout: { template: 'custom', cols: 2, rows: 1, cells: [{ kind: 'curve', key: 'observation_state' }, { kind: 'video', key: 'gone' }] },
        curves: { hidden: { observation_state: ['joint_0'] } },
        playback: { speed: 1.5, loop: true },
      },
      version: 1,
      updatedAt: 1,
    });
    renderWithProviders(<Player source={DROID200} index={0} />);
    const player = await ready();
    await waitFor(() => expect(player.querySelectorAll('.vz-cell')).toHaveLength(2));
    expect(within(player).getByText(zh.viz.layouts.custom)).toBeInTheDocument();
    expect(player.querySelector('.vz-cell')?.classList.contains('kind-curve')).toBe(true);
    // a camera the dataset no longer has: an empty cell
    expect(player.querySelectorAll('.vz-cell')[1].classList.contains('kind-empty')).toBe(true);
    await waitFor(() => expect(player.querySelector('.legend .li.off')?.textContent).toContain('joint_0'));
    expect(within(player).getByText('1.5x')).toBeInTheDocument();
    expect(within(player.querySelector('.loop') as HTMLElement).getByRole('switch')).toBeChecked();
  });

  it('leaves hidden cameras out of the templates and shows display names', async () => {
    db.vizDisplays.set('ds_droid200', { config: { cameras: [{ key: 'wrist', name: '腕部' }, { key: 'exterior_2', hidden: true }] }, version: 1, updatedAt: 1 });
    renderWithProviders(<Player source={DROID200} index={0} />);
    const player = await ready();
    await waitFor(() => expect(player.querySelectorAll('.vz-cell.kind-video')).toHaveLength(2));
    expect(videoKeys(player).map((t) => t?.replace('平台转码', ''))).toEqual(['腕部', 'exterior_1']);
  });

  it('saves the current layout for everyone, then restores the default', async () => {
    const { user } = renderWithProviders(<Player source={DROID200} index={0} />);
    const player = await ready();
    await waitFor(() => expect(player.querySelectorAll('.vz-cell.kind-video')).toHaveLength(3));
    // nothing saved yet: nothing to restore
    await user.click(within(player).getByTestId('vz-layout-menu'));
    expect(await screen.findByRole('menuitem', { name: zh.viz.display.restore })).toHaveClass('arco-dropdown-menu-disabled');
    await user.click(within(player).getByTestId('vz-layout-menu'));
    // only videos, at 2x
    await user.click(within(player).getByText(zh.viz.layouts.smart));
    await user.click(await screen.findByText(zh.viz.layouts.video));
    await user.click(within(player).getByText('1x'));
    await user.click(await screen.findByText('2x'));
    await pickMenu(user, player, zh.viz.display.save);
    const dialog = await screen.findByRole('dialog', { name: zh.viz.display.saveTitle });
    await user.click(within(dialog).getByRole('button', { name: zh.viz.display.save }));
    expect(await screen.findByText(zh.viz.display.saved)).toBeInTheDocument();
    expect(db.vizDisplays.get('ds_droid200')?.config).toMatchObject({ layout: { template: 'video' }, playback: { speed: 2, loop: false }, track: null });
    // restored: the smart layout at 1x, the configuration gone
    // the model says a layout is saved once it is asked for again
    await user.click(within(player).getByTestId('vz-layout-menu'));
    await waitFor(() => expect(screen.getByRole('menuitem', { name: zh.viz.display.restore })).not.toHaveClass('arco-dropdown-menu-disabled'));
    await user.click(screen.getByRole('menuitem', { name: zh.viz.display.restore }));
    const confirm = await screen.findByRole('dialog', { name: zh.viz.display.restoreTitle });
    await user.click(within(confirm).getByRole('button', { name: zh.viz.display.restore }));
    expect(await screen.findByText(zh.viz.display.restored)).toBeInTheDocument();
    expect(db.vizDisplays.get('ds_droid200')?.config ?? null).toBeNull();
    await waitFor(() => expect(within(player).getByText(zh.viz.layouts.smart)).toBeInTheDocument());
    expect(player.querySelectorAll('.vz-cell.kind-curve').length).toBeGreaterThan(0);
    expect(within(player).getByText('1x')).toBeInTheDocument();
  });

  it('keeps the layout, the hidden lines and the speed when another episode opens', async () => {
    const { user } = renderWithProviders(<Episodes />);
    const player = await ready();
    await waitFor(() => expect(player.querySelectorAll('.vz-cell.kind-video')).toHaveLength(3));
    await user.click(within(player).getByText(zh.viz.layouts.smart));
    await user.click(await screen.findByText(zh.viz.layouts.curve));
    await waitFor(() => expect(player.querySelectorAll('.vz-cell.kind-video')).toHaveLength(0));
    const legend = await waitFor(() => {
      const li = player.querySelector('.legend .li') as HTMLElement | null;
      expect(li).not.toBeNull();
      return li as HTMLElement;
    });
    await user.click(legend);
    await user.click(within(player).getByText('1x'));
    await user.click(await screen.findByText('1.5x'));
    await user.click(screen.getByText('next-episode'));
    await waitFor(() => expect(within(player).getByText('ep 1')).toBeInTheDocument());
    const again = await ready();
    expect(again).toBe(player);
    expect(within(again).getByText(zh.viz.layouts.curve)).toBeInTheDocument();
    expect(again.querySelectorAll('.vz-cell.kind-video')).toHaveLength(0);
    await waitFor(() => expect(again.querySelector('.legend .li.off')).not.toBeNull());
    expect(within(again).getByText('1.5x')).toBeInTheDocument();
  });
});
