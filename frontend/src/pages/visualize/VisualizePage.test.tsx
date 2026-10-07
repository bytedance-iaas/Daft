import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { zh } from '../../locales/zh';
import { db } from '../../mocks/db';
import { server } from '../../mocks/server';
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

  it('prepares the next episode in the background once this one is on screen (an mcap one is scanned once)', async () => {
    const asked: string[] = [];
    server.events.on('request:start', ({ request }) => {
      asked.push(new URL(request.url).pathname);
    });
    renderApp('/visualize?dataset=ds_droid200&ep=3');
    const player = await screen.findByTestId('vz-player');
    await waitFor(() => expect(within(player).getByText('ep 3')).toBeInTheDocument());
    await waitFor(() => expect(asked.some((p) => p.endsWith('/datasets/ds_droid200/episodes/4/viz'))).toBe(true));
  });

  it('says why a dataset does not read, once, instead of asking again and again', async () => {
    // the requester's pusht-lance (2026-10-05): its model answered an error; the page mounted a Player to
    // show it, whose own observer re-fetched the failed model - back to pending, unmounted, failed again
    const message = 'meta/episodes/chunk-000/file-000.parquet 是 Git LFS 指针文件（131 字节的占位），不是数据';
    let asked = 0;
    server.use(
      http.get('*/api/v1/datasets/:id/viz', () => {
        asked += 1;
        return HttpResponse.json({ error: { code: 'not_found', message } }, { status: 404 });
      }),
    );
    renderApp('/visualize?dataset=ds_droid200');
    await screen.findByText(`${zh.viz.modelFailed}：${message}`);
    await new Promise((r) => setTimeout(r, 300));
    // still on screen, in an alert, and the model was not asked for again and again
    expect(screen.getAllByRole('alert').some((a) => a.textContent === `${zh.viz.modelFailed}：${message}`)).toBe(true);
    expect(asked).toBeLessThanOrEqual(2);
  });

  it('falls back to the first episode when the address names one the dataset does not have', async () => {
    renderApp('/visualize?dataset=ds_droid200&ep=99999');
    const player = await screen.findByTestId('vz-player', {}, { timeout: 5000 });
    await waitFor(() => expect(within(player).getByText('ep 0')).toBeInTheDocument());
  });

  it('filters the episodes and folds the rail away', async () => {
    const { user } = renderApp('/visualize?dataset=ds_droid200&ep=12');
    const rail = await screen.findByTestId('vz-rail');
    await within(rail).findAllByRole('listitem');
    await user.type(within(rail).getByLabelText(zh.vizPage.filter), '12');
    await waitFor(() => expect(within(rail).getAllByRole('listitem').map((r) => r.querySelector('.id')?.textContent)).toEqual(['ep 12']));
    expect(within(rail).getAllByRole('listitem')[0]).toHaveClass('cur');
    // 收起侧栏 is on the rail itself; 展开侧栏 then sits before the player's title
    expect(within(rail).queryByRole('button', { name: zh.vizPage.railUnfold })).toBeNull();
    await user.click(within(rail).getByRole('button', { name: zh.vizPage.railFold }));
    expect(document.querySelector('.vzpage')).toHaveClass('collapsed');
    expect(JSON.parse(window.localStorage.getItem('curator.ui.prefs') ?? '{}').vizRailCollapsed).toBe(true);
    const player = await screen.findByTestId('vz-player', {}, { timeout: 5000 });
    const head = player.querySelector('.vz-head') as HTMLElement;
    const unfold = within(head).getByRole('button', { name: zh.vizPage.railUnfold });
    expect(head.firstElementChild).toContainElement(unfold);
    await user.click(unfold);
    expect(document.querySelector('.vzpage')).not.toHaveClass('collapsed');
    expect(screen.queryByRole('button', { name: zh.vizPage.railUnfold })).toBeNull();
    expect(JSON.parse(window.localStorage.getItem('curator.ui.prefs') ?? '{}').vizRailCollapsed).toBe(false);
  });

  it('opens the dataset shown last, and the info tree adds a camera to the player', async () => {
    window.localStorage.setItem('curator.ui.prefs', JSON.stringify({ pageSize: {}, lastVizDataset: 'ds_droid100' }));
    const { user } = renderApp('/visualize');
    const info = await screen.findByTestId('vz-info', {}, { timeout: 5000 });
    await screen.findByTestId('vz-player', {}, { timeout: 5000 });
    expect(within(info).getByText('相机')).toBeInTheDocument();
    // the first leaf is shown: a camera, with its attributes and 加入播放器
    await user.click(within(info).getAllByRole('button', { name: /wrist_image_left/ })[0]);
    // the feature's info.json entry as the dataset writes it, nothing translated (design doc 21 §3)
    expect(within(info).getByRole('heading', { name: 'observation.images.wrist_image_left' })).toBeInTheDocument();
    expect(within(info).getByText('info.video.codec')).toBeInTheDocument();
    expect(within(info).queryByText('分辨率')).toBeNull();
    expect(within(info).queryByText('读取方式')).toBeNull();
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

  it('lists a LeRobot dataset the checks refuse (no action column) without calling it mcap', async () => {
    const base = db.datasets.find((d) => d.id === 'ds_droid100')!;
    db.datasets.push({
      ...base,
      id: 'ds_galaxea',
      name: 'galaxea_tray',
      uri: 'tos://pai-kit-datasets/lerobot/galaxea_tray',
      format: 'unsupported',
      preflight: { ...base.preflight, format: { kind: 'lerobot', version: 'v2', supported: false, detail: "features 里没有 'action'" } },
      viz: { state: 'ready', reason: null },
      viz_mapping: null,
      created_at: base.created_at + 1,
    });
    const { user } = renderApp('/visualize?dataset=ds_droid100');
    const rail = await screen.findByTestId('vz-rail');
    await user.click(within(rail).getByRole('combobox', { name: zh.vizPage.dataset }));
    const option = await screen.findByRole('option', { name: /galaxea_tray/ });
    expect(option).not.toHaveTextContent('mcap');
    expect(option).not.toHaveAttribute('aria-disabled', 'true');
    // warehouse_mcap is mcap either way
    expect(screen.getByRole('option', { name: /warehouse_mcap/ })).toHaveTextContent('mcap');
  });

  it('asks for a dataset when there is none to show', async () => {
    renderApp('/visualize');
    expect(await screen.findByText(zh.vizPage.pickFirst)).toBeInTheDocument();
  });

  it('keeps 展开侧栏 where there is no player: the folded rail opens again', async () => {
    window.localStorage.setItem('curator.ui.prefs', JSON.stringify({ pageSize: {}, vizRailCollapsed: true }));
    const { user } = renderApp('/visualize');
    const box = (await screen.findByText(zh.vizPage.pickFirst)).closest('.vz-pending') as HTMLElement;
    expect(document.querySelector('.vzpage')).toHaveClass('collapsed');
    await user.click(within(box).getByRole('button', { name: zh.vizPage.railUnfold }));
    expect(document.querySelector('.vzpage')).not.toHaveClass('collapsed');
  });
});
