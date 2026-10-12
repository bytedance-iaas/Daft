import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { zh } from '../../locales/zh';
import { db } from '../../mocks/db';
import { server } from '../../mocks/server';
import { recordRequests } from '../../test/record';
import { currentLocation, renderApp } from '../../test/render';

/** The 轨迹 button once the page has looked and it can be clicked (grey while it looks, 2026-10-12). */
async function clickableOverlay(): Promise<HTMLElement> {
  return waitFor(
    () => {
      const b = screen.getByTestId('vz-overlay');
      if (b.closest('[data-testid="vz-overlay-off"]')) throw new Error('still grey');
      return b;
    },
    { timeout: 8000 },
  );
}

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

  it('fills the browser window from the transport bar and leaves it on Esc; no 数据集详情, no source-file link (2026-10-10)', async () => {
    const { user } = renderApp('/visualize?dataset=ds_droid200&ep=0');
    const player = await screen.findByTestId('vz-player', {}, { timeout: 5000 });
    expect(screen.queryByRole('button', { name: '数据集详情' })).toBeNull();
    expect(within(player).queryByTestId('vz-open-source')).toBeNull();
    await user.click(within(player).getByRole('button', { name: zh.viz.fill }));
    expect(player).toHaveClass('is-fill');
    await user.keyboard('{Escape}');
    expect(player).not.toHaveClass('is-fill');
    expect(currentLocation()).toBe('/visualize?dataset=ds_droid200&ep=0');
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

  it('opens the dataset’s 展示配置 from the header; a camera hidden there leaves the smart layout (design doc 21 §6)', async () => {
    const { user } = renderApp('/visualize?dataset=ds_droid200');
    const player = await screen.findByTestId('vz-player', {}, { timeout: 5000 });
    await waitFor(() => expect(player.querySelectorAll('.vz-cell.kind-video')).toHaveLength(3));
    await user.click(screen.getByRole('button', { name: zh.displayCfg.entry }));
    const drawer = await waitFor(() => {
      const d = [...document.querySelectorAll('.arco-drawer')].find((x) => x.textContent?.includes(zh.displayCfg.title('droid-200')));
      if (!(d instanceof HTMLElement)) throw new Error('no drawer');
      return d;
    });
    await user.click(await within(drawer).findByRole('checkbox', { name: zh.displayCfg.hiddenAria('exterior_2') }));
    await user.click(within(drawer).getByRole('button', { name: zh.displayCfg.save }));
    expect(await screen.findByText(zh.displayCfg.saved)).toBeInTheDocument();
    // the model is asked for again: two cameras in the smart layout, the third still in 「+」
    await waitFor(() => expect(player.querySelectorAll('.vz-cell.kind-video')).toHaveLength(2));
    expect(db.vizDisplays.get('ds_droid200')?.config?.cameras).toEqual([{ key: 'exterior_1' }, { key: 'exterior_2', hidden: true }, { key: 'wrist' }]);
  });

  it('makes the EEF marks the dataset declaration generates when asked, then draws them; the cameras it cannot draw on are greyed (design doc 25 §5.1)', async () => {
    const SRC = 'observation.images.exterior_image_1_left';
    db.declarations.set('ds_droid100', {
      doc: {
        schema_version: 'dataset-declaration/1.0',
        semantics: {
          pose: { key: 'observation.state.cartesian_position', layout: 'xyz_rpy_xyz_extrinsic', units: { position: 'm', angle: 'rad' }, frame_id: 'panda_link8', reference_frame: 'robot_base', pose_type: 'absolute' },
        },
        calibration: {
          cameras: { [SRC]: { camera_id: 'exterior_image_1_left', mount: 'fixed_external', intrinsics: { fx_cx_fy_cy: [500, 320, 500, 240], model: 'pinhole' }, extrinsics: { mode: 'static', xyz_rpy: [1, 0, 0.5, 0, 0, 0] } } },
          tool: { tcp_offset_m: [0, 0, 0.1] },
        },
      },
      version: 1,
      updatedAt: 1,
    });
    const seen = recordRequests();
    const { user } = renderApp('/visualize?dataset=ds_droid100&ep=0');
    // switching only looks (2026-10-12): the button is there and clickable, nothing is made yet
    const button = await clickableOverlay();
    expect(button).toHaveTextContent(/^轨迹$/);                                                         // 「叠加」 until 2026-10-10
    const looks = () => seen.filter((r) => r.path.endsWith('/eef-overlay'));
    expect(looks().length).toBeGreaterThan(0);
    expect(looks().every((r) => r.query.get('generate') === 'false')).toBe(true);
    await user.click(button);
    const ask = await screen.findByTestId('vz-overlay-generate');
    expect(ask).toHaveTextContent(zh.viz.overlay.notMade);
    await user.click(within(ask).getByTestId('vz-overlay-generate-link'));
    expect(looks().some((r) => r.query.get('generate') === 'true')).toBe(true);
    expect(await screen.findByTestId('vz-overlay-making')).toHaveTextContent('轨迹生成中 40%');       // 202 while it is made
    // made: the marks are drawn and the 轨迹 button opens the layer menu
    await waitFor(() => expect(document.querySelectorAll('[data-testid^="vz-eef-"]').length).toBeGreaterThan(0), { timeout: 8000 });
    await user.click(screen.getByTestId('vz-overlay'));
    const menu = await screen.findByTestId('vz-overlay-menu');
    expect(within(menu).queryByRole('button', { name: zh.viz.overlay.presets.model })).toBeNull();   // nothing a model saw
    const off = within(menu).getByTestId('vz-overlay-unavailable');
    expect(off).toHaveTextContent(zh.viz.overlay.reasons.mount_unknown);
    expect(screen.queryByTestId('vz-overlay-making')).toBeNull();
  });

  it('a handheld gripper\'s wrist cameras come with their note (design doc 25 §5.4)', async () => {
    const { user } = renderApp('/visualize?dataset=ds_umi&ep=0');
    await user.click(await clickableOverlay());
    await user.click(await screen.findByTestId('vz-overlay-generate-link'));
    await waitFor(() => expect(document.querySelectorAll('[data-testid^="vz-eef-"]').length).toBeGreaterThan(0), { timeout: 8000 });
    await user.click(screen.getByTestId('vz-overlay'));
    expect(await screen.findByTestId('vz-overlay-wrist-note')).toHaveTextContent(zh.viz.overlay.wristNote);
    // the menu says it; the 轨迹 section of 详细信息 does not again, nor how the marks are timed (2026-10-10)
    await user.click(screen.getByRole('button', { name: zh.viz.info }));
    const cell = (await screen.findAllByTestId(/^vz-eef-/))[0].closest('.vz-cell') as HTMLElement;
    fireEvent.pointerDown(cell);
    const side = await screen.findByTestId('vz-side');
    await waitFor(() => expect(side).toHaveTextContent(zh.viz.overlay.side.camera));
    expect(side).not.toHaveTextContent(zh.viz.overlay.wristNote);
    expect(side).not.toHaveTextContent('标记画在配对的那一帧画面上');
    expect(side).not.toHaveTextContent('轨迹由平台从数据集的记录与声明生成');
  });

  it('a dataset with a pose record but an unfinished declaration says so, and leads to the declaration (design doc 25 §5.1)', async () => {
    const { user } = renderApp('/visualize?dataset=ds_droid100&ep=0');
    const note = await screen.findByTestId('vz-overlay-declare', {}, { timeout: 8000 });
    expect(note).toHaveTextContent(zh.viz.overlay.declare);
    expect(screen.getByTestId('vz-overlay')).toBeDisabled();                              // there, grey: nothing to make
    expect(screen.getByTestId('vz-overlay-off')).toHaveAttribute('title', zh.viz.overlay.declare);
    await user.click(within(note).getByRole('button', { name: zh.viz.overlay.declareGo }));
    await waitFor(() => expect(currentLocation()).toBe('/datasets/ds_droid100?declaration=1'));
  });

  it('an episode the platform cannot make a trajectory for says why in the notice strip, its 轨迹 button grey', async () => {
    const why = '这一条画不了轨迹：robot0、robot1 缺相机内参，录制里没有 camera_info、标定文件也没有 intrinsics_fallback';
    server.use(
      http.get('*/api/v1/datasets/:id/episodes/:index/eef-overlay', () =>
        HttpResponse.json({ error: { code: 'not_found', message: why, details: { reason: 'not_generated', source: { kind: 'derived' } } } }, { status: 404 }),
      ),
    );
    renderApp('/visualize?dataset=ds_umi&ep=0');
    const notice = await screen.findByTestId('vz-notice', {}, { timeout: 8000 });
    expect(notice).toHaveTextContent(why);
    expect(notice).toHaveClass('vz-warn');                                                     // the yellow strip under the head
    expect(screen.getByTestId('vz-overlay')).toBeDisabled();
    expect(screen.getByTestId('vz-overlay-off')).toHaveAttribute('title', why);
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
