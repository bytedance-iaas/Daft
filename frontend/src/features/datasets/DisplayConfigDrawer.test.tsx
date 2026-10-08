import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { zh } from '../../locales/zh';
import { db } from '../../mocks/db';
import { server } from '../../mocks/server';
import { fill, findDrawer, pick } from '../../test/arco';
import { renderApp } from '../../test/render';

const T = zh.displayCfg;

async function openDrawer(id: string, name: string) {
  const r = renderApp(`/datasets/${id}`);
  const row = await screen.findByTestId('dataset-display', {}, { timeout: 5000 });
  await r.user.click(within(row).getByRole('button', { name: T.entry }));
  const drawer = await findDrawer(T.title(name));
  await within(drawer).findByTestId('display-drawer', {}, { timeout: 5000 });
  return { ...r, drawer, row };
}

function cameraOrder(drawer: HTMLElement): string[] {
  return [...within(drawer).getByTestId('display-cameras').querySelectorAll('tbody tr')].map((tr) => tr.querySelector('.mono')?.textContent ?? '');
}

describe('展示配置 (design doc 21 §6.5)', () => {
  it('orders, renames and hides cameras, and the detail page says the version saved', async () => {
    const { user, drawer, row } = await openDrawer('ds_droid200', 'droid-200');
    expect(row).toHaveTextContent(T.none);
    expect(within(drawer).getByTestId('display-current')).toHaveTextContent(T.none);
    expect(cameraOrder(drawer)).toEqual(['exterior_1', 'exterior_2', 'wrist']);
    // nothing changed: nothing to save
    expect(within(drawer).getByRole('button', { name: T.save })).toBeDisabled();
    await user.click(within(drawer).getByRole('button', { name: `${T.up} wrist` }));
    await user.click(within(drawer).getByRole('button', { name: `${T.up} wrist` }));
    expect(cameraOrder(drawer)).toEqual(['wrist', 'exterior_1', 'exterior_2']);
    await fill(user, T.nameAria('wrist'), '腕部', drawer);
    await user.click(within(drawer).getByRole('checkbox', { name: T.hiddenAria('exterior_2') }));
    await pick(user, T.speed, '1.5x', drawer);
    await user.click(within(drawer).getByRole('button', { name: T.save }));
    expect(await screen.findByText(T.saved)).toBeInTheDocument();
    expect(db.vizDisplays.get('ds_droid200')?.config).toEqual({
      cameras: [{ key: 'wrist', name: '腕部' }, { key: 'exterior_1' }, { key: 'exterior_2', hidden: true }],
      playback: { speed: 1.5, loop: false },
    });
    await waitFor(() => expect(screen.getByTestId('dataset-display')).toHaveTextContent(T.current(1)));
  });

  it('regroups the curves: a new group, a dimension not drawn, the automatic groups back', async () => {
    const { user, drawer } = await openDrawer('ds_droid200', 'droid-200');
    const groups = within(drawer).getByTestId('display-groups');
    expect(within(groups).getAllByRole('textbox', { name: /^分组 .* 的名字$/ }).map((x) => (x as HTMLInputElement).value)).toEqual([
      'observation.state / action',
      'observation.state / action · gripper',
    ]);
    await user.click(within(drawer).getByRole('button', { name: T.addGroup }));
    await waitFor(() => expect(within(drawer).getByText(T.emptyGroup)).toBeInTheDocument());
    // both gripper lines into the new group, the first joint's action not drawn
    await pick(user, T.inAria('observation.state#7'), T.newGroup, drawer);
    await pick(user, T.inAria('action#7'), T.newGroup, drawer);
    await pick(user, T.inAria('action#0'), T.notDrawn, drawer);
    await fill(user, T.lineAria('action#7'), 'gripper cmd', drawer);
    await user.click(within(drawer).getByRole('button', { name: T.save }));
    expect(await screen.findByText(T.saved)).toBeInTheDocument();
    const saved = db.vizDisplays.get('ds_droid200')?.config?.curves?.groups ?? [];
    // the gripper group is left without lines and is not saved
    expect(saved.map((g) => g.key)).toEqual(['observation_state', 'group']);
    expect(saved[0].lines.some((l) => l.source === 'action' && l.dim === 0)).toBe(false);
    expect(saved[1].lines).toEqual([
      { source: 'observation.state', dim: 7, name: 'gripper', role: 'state' },
      { source: 'action', dim: 7, name: 'gripper cmd', role: 'action' },
    ]);
    // back to the automatic groups: nothing of the curves is saved
    await user.click(within(screen.getByTestId('dataset-display')).getByRole('button', { name: T.entry }));
    const again = await findDrawer(T.title('droid-200'));
    await within(again).findByTestId('display-drawer');
    await user.click(within(again).getByRole('button', { name: T.autoGroups }));
    await user.click(within(again).getByRole('button', { name: T.save }));
    await waitFor(() => expect(db.vizDisplays.get('ds_droid200')?.config ?? null).toBeNull());
  });

  it('restores the defaults after asking', async () => {
    db.vizDisplays.set('ds_droid200', { config: { track: 'subtask', playback: { speed: 2, loop: true } }, version: 4, updatedAt: Date.now() });
    const { user, drawer } = await openDrawer('ds_droid200', 'droid-200');
    expect(within(drawer).getByTestId('display-current')).toHaveTextContent(T.current(4));
    await user.click(within(drawer).getByRole('button', { name: T.restore }));
    const dialog = await screen.findByRole('dialog', { name: T.restoreTitle });
    await user.click(within(dialog).getByRole('button', { name: T.restore }));
    expect(await screen.findByText(T.restored)).toBeInTheDocument();
    expect(db.vizDisplays.get('ds_droid200')).toMatchObject({ config: null, version: 5 });
  });

  it('leaves an mcap dataset’s curves to its field mapping', async () => {
    const { drawer } = await openDrawer('ds_mcap', 'warehouse_mcap');
    expect(within(drawer).getByText(T.groupsMcap)).toBeInTheDocument();
    expect(within(drawer).queryByTestId('display-dims')).toBeNull();
    expect(cameraOrder(drawer).length).toBeGreaterThan(0);
  });

  it('lists what the Daemon refuses', async () => {
    server.use(
      http.put('*/api/v1/datasets/:id/viz/display', () =>
        HttpResponse.json({ error: { code: 'validation_failed', message: '展示配置有 1 处对不上这个数据集', details: { errors: [{ field: 'cameras.0.key', problem: '数据集里没有相机 wrist' }] } } }, { status: 400 }),
      ),
    );
    const { user, drawer } = await openDrawer('ds_droid200', 'droid-200');
    await user.click(within(drawer).getByRole('checkbox', { name: T.hiddenAria('wrist') }));
    await user.click(within(drawer).getByRole('button', { name: T.save }));
    const problems = await within(drawer).findByTestId('display-problems');
    expect(problems).toHaveTextContent(T.problems(1));
    expect(problems).toHaveTextContent('cameras.0.key：数据集里没有相机 wrist');
  });
});
