import { screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { zh } from '../../locales/zh';
import { db } from '../../mocks/db';
import { renderWithProviders } from '../../test/render';
import { Player } from './Player';

// droid_100's wrist camera has a depth stream in the mock world (design doc 21 §5): a 64 × 36 16-bit PNG pack
const DROID100 = { scope: 'dataset' as const, id: 'ds_droid100' };
const DEPTH = 'observation_images_wrist_image_left_depth';

describe('depth cells (design doc 21 §5.5)', () => {
  beforeEach(() => {
    HTMLMediaElement.prototype.canPlayType = (type: string) => (/avc1|av01/.test(type) ? 'probably' : '');
    db.vizDepthPacks.clear();
  });

  it('are picked from 「+」, say the pack is being made, then draw it with its range', async () => {
    const { user } = renderWithProviders(
      <Player source={DROID100} index={0} arrangement={{ cells: [{ kind: 'empty' }], shape: { cols: 1, rows: 1 } }} />,
    );
    const player = await screen.findByTestId('vz-player', {}, { timeout: 5000 });
    await user.click(within(player).getByText(zh.viz.cell.choose));
    const menu = await screen.findByTestId('vz-menu');
    expect(within(menu).getByText(zh.viz.menu.depth)).toBeInTheDocument();
    await user.click(within(menu).getByText('observation.images.wrist_image_left.depth'));
    // the Daemon answers 202 the first time (the pack is being made), then the index
    expect(await within(player).findByText(/深度图生成中/)).toBeInTheDocument();
    expect(await within(player).findByTestId(`vz-depth-${DEPTH}`)).toBeInTheDocument();
    expect(await within(player).findByText('560–1450 mm', {}, { timeout: 8000 })).toBeInTheDocument();
    expect(within(player).getByText(zh.viz.depth.tag)).toBeInTheDocument();
  });

  it('draw over their camera when asked, and change colours and range', async () => {
    const { user } = renderWithProviders(
      <Player source={DROID100} index={0} arrangement={{ cells: [{ kind: 'depth', key: DEPTH }], shape: { cols: 1, rows: 1 } }} />,
    );
    const player = await screen.findByTestId('vz-player', {}, { timeout: 5000 });
    const cell = await within(player).findByTestId('vz-cell-0');
    // the pack is made (202 first), its range known
    expect(await within(cell).findByText('560–1450 mm', {}, { timeout: 8000 })).toBeInTheDocument();
    expect(cell.querySelector('video')).toBeNull();
    await user.click(within(cell).getByTitle(zh.viz.depth.settingsTitle));
    const set = await screen.findByTestId('vz-depth-set');
    await user.click(within(set).getByRole('switch', { name: zh.viz.depth.overlay }));
    // the wrist camera now plays under the depth picture
    await waitFor(() => expect(cell.querySelector('video')).not.toBeNull());
    expect(within(set).getByText(zh.viz.depth.opacity)).toBeInTheDocument();
    await user.click(within(set).getByText(zh.viz.depth.gray));
    await waitFor(() => expect(within(set).getByRole('radio', { name: zh.viz.depth.gray })).toBeChecked());
    // a range of one's own starts at the episode's 2 % / 98 %
    await user.click(within(set).getByRole('checkbox', { name: zh.viz.depth.auto }));
    expect(await within(set).findByLabelText(zh.viz.depth.lo)).toHaveValue('560');
    expect(within(set).getByLabelText(zh.viz.depth.hi)).toHaveValue('1450');
  });

  it('say what they are in the info panel', async () => {
    const { user } = renderWithProviders(
      <Player source={DROID100} index={0} arrangement={{ cells: [{ kind: 'depth', key: DEPTH }], shape: { cols: 1, rows: 1 } }} sidebar />,
    );
    const player = await screen.findByTestId('vz-player', {}, { timeout: 5000 });
    await user.click(await within(player).findByTestId('vz-cell-0'));
    const side = await screen.findByTestId('vz-side');
    expect(within(side).getByText(zh.viz.side.depthTitle('observation.images.wrist_image_left.depth'))).toBeInTheDocument();
    expect(within(side).getByText(zh.viz.side.depthAccess)).toBeInTheDocument();
    expect(within(side).getByText('wrist_image_left')).toBeInTheDocument();
  });
});
