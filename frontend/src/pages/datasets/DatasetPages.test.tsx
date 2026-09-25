import { screen, waitFor, within } from '@testing-library/react';
import { http } from 'msw';
import { beforeAll, describe, expect, it } from 'vitest';
import { PREFLIGHT_DEBOUNCE } from '../../features/preflight/usePreflight';
import { db } from '../../mocks/db';
import { server } from '../../mocks/server';
import { fill, findDrawer, pick } from '../../test/arco';
import { fieldErrors, requiredFieldLabels } from '../../test/forms';
import { currentLocation, renderApp } from '../../test/render';

beforeAll(() => {
  PREFLIGHT_DEBOUNCE.ms = 0;
});

function row(name: string): HTMLElement {
  return screen.getByRole('link', { name }).closest('tr') as HTMLElement;
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

  it('row operations are 可视化, 新建任务 and a red 删除; 可视化 opens the ReRun viewer in a new tab (requester item 22)', async () => {
    renderApp('/datasets');
    await screen.findByRole('link', { name: 'droid_100' });
    const ops = row('droid_100').querySelector('td:last-child') as HTMLElement;
    expect([...ops.querySelectorAll('a, button')].map((b) => b.textContent)).toEqual(['可视化', '新建任务', '删除']);
    expect(within(ops).getByRole('button', { name: '删除' })).toHaveClass('arco-btn-status-danger');
    expect(within(row('droid-200')).queryByRole('button', { name: '重新检查' })).toBeNull();
    const viz = within(ops).getByRole('link', { name: '可视化' });
    expect(viz).toHaveAttribute('target', '_blank');
    expect(viz).toHaveAttribute('href', `${window.location.origin}/?url=${encodeURIComponent('tos://pai-kit-datasets/lerobot/droid_100/?region=cn-beijing')}`);
    // A public dataset works the same way (no region registered, none appended).
    expect(within(row('libero_10')).getByRole('link', { name: '可视化' })).toHaveAttribute('href', `${window.location.origin}/?url=${encodeURIComponent('tos://hf-cache/lerobot/libero_10/')}`);
  });

  it('the viewer is one level above the mount prefix (/dataverse/curation → /dataverse/)', async () => {
    window.__CURATOR_BASE__ = '/dataverse/curation';
    try {
      renderApp('/datasets');
      await screen.findByRole('link', { name: 'droid_100' });
      expect(within(row('droid_100')).getByRole('link', { name: '可视化' })).toHaveAttribute('href', `${window.location.origin}/dataverse/?url=${encodeURIComponent('tos://pai-kit-datasets/lerobot/droid_100/?region=cn-beijing')}`);
    } finally {
      delete window.__CURATOR_BASE__;
    }
  });

  it('a locally mounted dataset cannot be visualized, and says why', async () => {
    const base = db.datasets.find((d) => d.id === 'ds_droid100')!;
    db.datasets.push({ ...base, id: 'ds_local', name: 'local_droid', source: 'local', uri: '/mnt/datasets/local_droid', region: null, credential: null, created_at: base.created_at + 1 });
    const { user } = renderApp('/datasets');
    await screen.findByRole('link', { name: 'local_droid' });
    const viz = within(row('local_droid')).getByRole('button', { name: '可视化' });
    expect(viz).toBeDisabled();
    await user.hover(viz.parentElement!);
    expect(await screen.findByText('本地挂载的数据集不支持可视化')).toBeInTheDocument();
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
    await user.click(within(row('umi_640_notask')).getByRole('button', { name: '删除' }));
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
    expect(screen.getByRole('link', { name: '可视化' })).toHaveAttribute('href', `${window.location.origin}/?url=${encodeURIComponent('tos://pai-kit-datasets/lerobot/droid-200/?region=cn-beijing')}`);
    await user.click(screen.getByRole('button', { name: '新建质检任务' }));
    await waitFor(() => expect(currentLocation()).toBe('/tasks/new?dataset_id=ds_droid200'));
    expect(await screen.findByDisplayValue('tos://pai-kit-datasets/lerobot/droid-200')).toBeInTheDocument();
    expect(screen.getByText(/已添加的数据集「droid-200」/)).toBeInTheDocument();
  });
});
