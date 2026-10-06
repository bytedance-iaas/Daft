import { screen, waitFor, within } from '@testing-library/react';
import { http } from 'msw';
import { beforeAll, describe, expect, it } from 'vitest';
import { PREFLIGHT_DEBOUNCE } from '../../features/preflight/usePreflight';
import { db } from '../../mocks/db';
import { server } from '../../mocks/server';
import { DATASET_PROFILES, datasetDetail } from '../../mocks/world';
import { fill, findDrawer, pick } from '../../test/arco';
import { fieldErrors, requiredFieldLabels } from '../../test/forms';
import { currentLocation, renderApp } from '../../test/render';

beforeAll(() => {
  PREFLIGHT_DEBOUNCE.ms = 0;
});

function row(name: string): HTMLElement {
  return screen.getByRole('link', { name }).closest('tr') as HTMLElement;
}

/** The open 「更多」 dropdown of a row (the side menu is a menu too). */
function moreMenu(): Promise<HTMLElement> {
  return waitFor(() => {
    const m = document.querySelector('.arco-dropdown-menu');
    if (!(m instanceof HTMLElement)) throw new Error('no dropdown menu');
    return m;
  });
}

describe('数据集列表 (07 §4.4)', () => {
  it('lists registered datasets with format, fingerprint state and the last task', async () => {
    renderApp('/datasets');
    await screen.findByRole('link', { name: 'droid-200' });
    expect(within(row('droid-200')).getByText('有变化')).toBeInTheDocument();         // the rest on hover (fifth round)
    expect(within(row('droid-200')).queryByText('有变化，待重新预检')).toBeNull();
    expect(within(row('droid_100')).getByText('一致')).toBeInTheDocument();
    expect(within(row('umi_640_notask')).getByText('未检查')).toBeInTheDocument();
    expect(within(row('warehouse_mcap')).getByText('mcap')).toBeInTheDocument(); // D44
    expect(within(row('libero_10')).getByText('HuggingFace 缓存桶')).toBeInTheDocument();
    const last = within(row('droid_100')).getByRole('link', { name: 'droid 前 50 条质检' });
    expect(last.closest('td')?.querySelector('.arco-tag')).toBeNull();          // the link alone, no state tag
    expect(screen.getByText('共 5 条')).toBeInTheDocument();
  });

  it('row operations are 可视化, 新建任务 and 更多 (2026-10-04); 更多 has 可视化（旧） in a new tab and a red 删除', async () => {
    const { user } = renderApp('/datasets');
    await screen.findByRole('link', { name: 'droid_100' });
    const ops = row('droid_100').querySelector('td:last-child') as HTMLElement;
    expect([...ops.querySelectorAll('a, button')].map((b) => b.textContent?.trim())).toEqual(['可视化', '新建任务', '更多']);
    const page = within(ops).getByRole('link', { name: '可视化' });
    expect(page).toHaveAttribute('target', '_blank');
    expect(page).toHaveAttribute('href', '/visualize?dataset=ds_droid100');
    expect(within(row('droid-200')).queryByRole('button', { name: '重新检查' })).toBeNull();
    await user.click(within(ops).getByRole('button', { name: '更多操作：droid_100' }));
    const menu = await moreMenu();
    expect([...menu.querySelectorAll('[role="menuitem"]')].map((m) => m.textContent)).toEqual(['可视化（旧）', '删除']);  // LeRobot: no mcap 配置
    const viz = within(menu).getByRole('link', { name: '可视化（旧）' });
    expect(viz).toHaveAttribute('target', '_blank');
    expect(viz).toHaveAttribute('href', `${window.location.origin}/?url=${encodeURIComponent('tos://pai-kit-datasets/lerobot/droid_100/?region=cn-beijing&curator_dataset=ds_droid100')}`);
    expect(within(menu).getByText('删除')).toHaveStyle({ color: 'var(--c-danger)' });
  });

  it('a public dataset\'s old viewer link carries no id and no region', async () => {
    const { user } = renderApp('/datasets');
    await screen.findByRole('link', { name: 'libero_10' });
    await user.click(within(row('libero_10')).getByRole('button', { name: '更多操作：libero_10' }));
    // the viewer reads a public bucket anonymously, and no region was registered
    expect(within(await moreMenu()).getByRole('link', { name: '可视化（旧）' })).toHaveAttribute('href', `${window.location.origin}/?url=${encodeURIComponent('tos://hf-cache/lerobot/libero_10/')}`);
  });

  it('the viewer is one level above the mount prefix (/dataverse/curation → /dataverse/)', async () => {
    window.__CURATOR_BASE__ = '/dataverse/curation';
    try {
      const { user } = renderApp('/datasets');
      await screen.findByRole('link', { name: 'droid_100' });
      await user.click(within(row('droid_100')).getByRole('button', { name: '更多操作：droid_100' }));
      expect(within(await moreMenu()).getByRole('link', { name: '可视化（旧）' })).toHaveAttribute('href', `${window.location.origin}/dataverse/?url=${encodeURIComponent('tos://pai-kit-datasets/lerobot/droid_100/?region=cn-beijing&curator_dataset=ds_droid100')}`);
    } finally {
      delete window.__CURATOR_BASE__;
    }
  });

  it('a locally mounted dataset cannot be visualized, and says why', async () => {
    const base = db.datasets.find((d) => d.id === 'ds_droid100')!;
    db.datasets.push({ ...base, id: 'ds_local', name: 'local_droid', source: 'local', uri: '/mnt/datasets/local_droid', region: null, credential: null, created_at: base.created_at + 1 });
    const { user } = renderApp('/datasets');
    await screen.findByRole('link', { name: 'local_droid' });
    // the visualize page reads a local dataset (design doc 18 §4.2); the old ReRun entry cannot
    expect(within(row('local_droid')).getByRole('link', { name: '可视化' })).toBeInTheDocument();
    await user.click(within(row('local_droid')).getByRole('button', { name: '更多操作：local_droid' }));
    const menu = await moreMenu();
    const viz = within(menu).getByText('可视化（旧）');
    expect(viz.closest('[role="menuitem"]')).toHaveClass('arco-dropdown-menu-disabled');
    expect(viz).toHaveAttribute('title', '本地挂载的数据集不支持可视化');
    expect(within(menu).queryByRole('link', { name: '可视化（旧）' })).toBeNull();
  });

  it('filters by fingerprint state through the API', async () => {
    renderApp('/datasets?check_state=changed');
    await screen.findByRole('link', { name: 'droid-200' });
    expect(screen.queryByRole('link', { name: 'droid_100' })).toBeNull();
  });

  it('重新检查 (on the detail page): a change opens the fingerprint dialog, 重新预检 refreshes it', async () => {
    const { user } = renderApp('/datasets/ds_droid200');
    await screen.findByRole('heading', { name: /droid-200/ });
    await user.click(screen.getByRole('button', { name: '重新检查' }));
    const dialog = await screen.findByRole('dialog', { name: '数据集和添加时不一样了' });
    expect(within(dialog).getByTestId('source-change')).toHaveTextContent('新增 12 个');
    await user.click(within(dialog).getByRole('button', { name: '重新预检' }));
    expect(await screen.findByText('已重新预检，指纹已更新')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('数据和上次预检时不一样了：重新预检后才能在上面开始新任务。')).toBeNull());
  });

  it('重新检查 with no change says so; deleting a dataset in use is refused with the reason', async () => {
    const { user } = renderApp('/datasets/ds_umi');
    await screen.findByRole('heading', { name: /umi_640_notask/ });
    await user.click(screen.getByRole('button', { name: '重新检查' }));
    expect(await screen.findByText('指纹一致，数据没有变化')).toBeInTheDocument();
    await user.click(screen.getByRole('link', { name: '数据集' }));
    await screen.findByRole('link', { name: 'umi_640_notask' });
    await user.click(within(row('umi_640_notask')).getByRole('button', { name: '更多操作：umi_640_notask' }));
    await user.click(within(await moreMenu()).getByText('删除'));
    const dialog = await screen.findByRole('dialog', { name: '删除数据集「umi_640_notask」的登记' });
    await user.click(within(dialog).getByRole('button', { name: '删除' }));
    expect(await screen.findByText('还有 1 个未结束的任务在用这个数据集，等它们结束后再删')).toBeInTheDocument();
  });

  it('添加数据集: required fields, automatic preflight, then save registers it', async () => {
    const seen: unknown[] = [];
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'POST' && new URL(request.url).pathname.endsWith('/api/v1/datasets')) seen.push(await request.clone().json());
    });
    const { user } = renderApp('/datasets');
    await user.click(await screen.findByRole('button', { name: '添加数据集' }));
    const drawer = await findDrawer('添加数据集');
    expect(requiredFieldLabels(drawer)).toEqual(['数据来源', '数据集地址', '地域', '访问密钥']);
    await user.click(within(drawer).getByRole('button', { name: '保存' }));
    await waitFor(() => expect(fieldErrors(drawer)).toEqual(['请填写数据集地址', '请选择访问密钥']));
    await fill(user, '数据集地址', 'tos://pai-kit-datasets/lerobot/brand_new', drawer);
    await pick(user, '访问密钥', 'readonly-tos', drawer);
    expect(await within(drawer).findByText(/LeRobot v2 · 120 条 episode/)).toBeInTheDocument();
    // While it registers, the button keeps its label and only shows it is loading (requester item 17).
    let release = () => {};
    const held = new Promise<void>((r) => (release = r));
    server.use(
      http.post('*/api/v1/datasets', async () => {
        await held;
      }),
    );
    await user.click(within(drawer).getByRole('button', { name: '保存' }));
    await waitFor(() => expect(within(drawer).getByRole('button', { name: '保存' })).toHaveClass('arco-btn-loading'));
    expect(drawer).not.toHaveTextContent('正在登记');
    release();
    await waitFor(() => expect(currentLocation()).toMatch(/^\/datasets\/ds-[a-z]{9}\b/));
    expect(seen[0]).toEqual({ input: { source: 'tos', uri: 'tos://pai-kit-datasets/lerobot/brand_new', region: 'cn-beijing', credential: 'readonly-tos' } });
    expect(await screen.findByText('已添加')).toBeInTheDocument();
    server.events.removeAllListeners();
  });

  it('添加数据集 has no intro text and starts with the default access key (sixth round)', async () => {
    db.credentials.find((c) => c.name === 'readonly-tos')!.is_default = true;
    const { user } = renderApp('/datasets');
    await user.click(await screen.findByRole('button', { name: '添加数据集' }));
    const drawer = await findDrawer('添加数据集');
    expect(drawer).not.toHaveTextContent('填好来源和地址会自动预检');
    expect(drawer).not.toHaveTextContent('只登记一次');
    const key = within(drawer).getByRole('combobox', { name: '访问密钥' }).closest('.arco-select') as HTMLElement;
    expect(key).toHaveTextContent('readonly-tos');
  });

  it('adding the same source + address + region again opens the existing registration', async () => {
    const { user } = renderApp('/datasets');
    await user.click(await screen.findByRole('button', { name: '添加数据集' }));
    const drawer = await findDrawer('添加数据集');
    await fill(user, '数据集地址', 'tos://pai-kit-datasets/lerobot/droid_100', drawer);
    await pick(user, '访问密钥', 'readonly-tos', drawer);
    await within(drawer).findByText(/LeRobot v3 · 100 条 episode/);
    await user.click(within(drawer).getByRole('button', { name: '保存' }));
    await waitFor(() => expect(currentLocation()).toBe('/datasets/ds_droid100'));
    expect(await screen.findByText('这个数据集已经添加过，已打开已有的那条')).toBeInTheDocument();
  });
});

describe('数据集详情', () => {
  it('a failed preflight shows its error as it is: no 格式 label and no 模块可用性 (2026-10-05)', async () => {
    const rrd = DATASET_PROFILES.find((p) => p.name === 'warehouse_rrd')!;
    db.datasets.push(datasetDetail('ds_rrd', rrd, Date.now(), { region: 'cn-beijing', credential: 'readonly-tos' }));
    renderApp('/datasets/ds_rrd');
    const error = await screen.findByTestId('dataset-preflight-error');
    expect(error).toHaveTextContent('rrd');
    const card = error.closest('.arco-card') as HTMLElement;
    expect(within(card).queryByText('格式')).toBeNull();
    expect(screen.queryByTestId('dataset-modules')).toBeNull();
    expect(screen.queryByText('模块可用性')).toBeNull();
  });

  it('VLM modules ask the backends: one verified model is enough for 可用 (third round)', async () => {
    renderApp('/datasets/ds_droid200');
    const modules = await screen.findByTestId('dataset-modules');
    const row = (name: string) => within(modules).getByText(name).closest('tr') as HTMLElement;
    // ark-prod is verified and has vision models: no 「还没选 VLM 后端」 on the dataset page.
    await waitFor(() => expect(row('任务成败判定')).toHaveTextContent('可用'));
    expect(modules).not.toHaveTextContent('还没选 VLM 后端');
  });

  it('without a verified backend the VLM modules have no usable backend', async () => {
    db.backends = db.backends.map((b) => ({ ...b, verify_state: 'failed' as const }));
    renderApp('/datasets/ds_droid200');
    const modules = await screen.findByTestId('dataset-modules');
    const row = within(modules).getByText('任务成败判定').closest('tr') as HTMLElement;
    await waitFor(() => expect(row).toHaveTextContent('需要补充没有可用的 VLM 后端'));
  });

  it('shows the preflight result, the fingerprint history and the tasks; 新建质检任务 prefills the form', async () => {
    const { user } = renderApp('/datasets/ds_droid200');
    expect(await screen.findByRole('heading', { name: /droid-200/ })).toBeInTheDocument();
    const modules = screen.getByTestId('dataset-modules');
    expect(within(modules).getByText('数据集缺少 observation.state 列')).toBeInTheDocument();
    const checks = screen.getByTestId('dataset-checks');
    expect(within(checks).getByText('meta 有变化；新增 12 · 删除 0 · 改动 1 个文件')).toBeInTheDocument();
    expect(screen.getByText('数据和上次预检时不一样了：重新预检后才能在上面开始新任务。')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'droid-200 抽检' })).toBeInTheDocument();
    // The header can open the dataset in the ReRun viewer too (requester item 22).
    expect(screen.getByRole('link', { name: '可视化（旧）' })).toHaveAttribute('href', `${window.location.origin}/?url=${encodeURIComponent('tos://pai-kit-datasets/lerobot/droid-200/?region=cn-beijing&curator_dataset=ds_droid200')}`);
    await user.click(screen.getByRole('button', { name: '新建质检任务' }));
    await waitFor(() => expect(currentLocation()).toBe('/tasks/new?dataset_id=ds_droid200'));
    expect(await screen.findByDisplayValue('tos://pai-kit-datasets/lerobot/droid-200')).toBeInTheDocument();
    // the registered dataset comes along without a hint under the address (sixth round)
    expect(document.body).not.toHaveTextContent('一并带出');
  });
});
