import { screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { zh } from '../../locales/zh';
import { db } from '../../mocks/db';
import { currentLocation, renderApp } from '../../test/render';

describe('可视化 page (design doc 18 §5.0, §5.7)', () => {
  beforeEach(() => {
    window.localStorage.clear();
    HTMLMediaElement.prototype.canPlayType = (type: string) => (/avc1|av01/.test(type) ? 'probably' : '');
  });

  it('lists the episodes on the left and plays the one clicked', async () => {
    const { user } = renderApp('/visualize?dataset=ds_droid200');
    const rail = await screen.findByTestId('vz-rail');
    const rows = await within(rail).findAllByRole('listitem');
    expect(rows.length).toBeGreaterThan(10);
    expect(rows[0].textContent).toContain('ep 0');
    // the first episode plays when the address names none
    const player = await screen.findByTestId('vz-player', {}, { timeout: 5000 });
    expect(within(player).getByText('ep 0')).toBeInTheDocument();
    expect(rows[0]).toHaveClass('cur');
    await user.click(rows[3]);
    await waitFor(() => expect(currentLocation()).toBe('/visualize?dataset=ds_droid200&ep=3'));
    await waitFor(() => expect(within(screen.getByTestId('vz-player')).getByText('ep 3')).toBeInTheDocument());
    // 下一条 goes on in the list's order
    await user.click(within(screen.getByTestId('vz-player')).getByRole('button', { name: zh.viz.nextEpisode }));
    await waitFor(() => expect(currentLocation()).toBe('/visualize?dataset=ds_droid200&ep=4'));
    expect(document.title).toContain('droid-200');
  });

  it('filters the episodes and folds the rail away', async () => {
    const { user } = renderApp('/visualize?dataset=ds_droid200&ep=12');
    const rail = await screen.findByTestId('vz-rail');
    await within(rail).findAllByRole('listitem');
    await user.type(within(rail).getByLabelText(zh.vizPage.filter), '12');
    await waitFor(() => expect(within(rail).getAllByRole('listitem').map((r) => r.querySelector('.id')?.textContent)).toEqual(['ep 12']));
    expect(within(rail).getAllByRole('listitem')[0]).toHaveClass('cur');
    await user.click(screen.getByRole('button', { name: zh.vizPage.railToggle }));
    expect(document.querySelector('.vzpage')).toHaveClass('collapsed');
  });

  it('opens the dataset shown last, and the info tree adds a camera to the player', async () => {
    window.localStorage.setItem('curator.ui.prefs', JSON.stringify({ pageSize: {}, lastVizDataset: 'ds_droid100' }));
    const { user } = renderApp('/visualize');
    const info = await screen.findByTestId('vz-info', {}, { timeout: 5000 });
    await screen.findByTestId('vz-player', {}, { timeout: 5000 });
    expect(within(info).getByText('相机')).toBeInTheDocument();
    // the first leaf is shown: a camera, with its attributes and 加入播放器
    await user.click(within(info).getAllByRole('button', { name: /wrist_image_left/ })[0]);
    await user.click(within(info).getByRole('button', { name: zh.vizPage.addToPlayer }));
    expect(await screen.findByText(/已加到空格子|替换了最后一个格子/)).toBeInTheDocument();
    // a metadata file previews its text
    await user.click(within(info).getByRole('button', { name: /info\.json/ }));
    expect(await within(info).findByTestId('vz-meta')).toBeInTheDocument();
  });

  it('says an mcap dataset needs its mapping confirmed first', async () => {
    db.vizMappings.delete('ds_mcap');
    renderApp('/visualize?dataset=ds_mcap');
    expect(await screen.findByText(/还没有确认字段映射/, {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: zh.vizPage.goMapping })).toBeInTheDocument();
    expect(screen.queryByTestId('vz-player')).toBeNull();
  });

  it('asks for a dataset when there is none to show', async () => {
    renderApp('/visualize');
    expect(await screen.findByText(zh.vizPage.pickFirst)).toBeInTheDocument();
  });
});
