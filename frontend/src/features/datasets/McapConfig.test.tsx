import { screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';
import type { VizMapping } from '../../api/types';
import { zh } from '../../locales/zh';
import { db } from '../../mocks/db';
import { server } from '../../mocks/server';
import { mappingInfoOf, vizStatusOf } from '../../mocks/vizWorld';
import { fill, findDrawer, pick } from '../../test/arco';
import { currentLocation, renderApp } from '../../test/render';
import { PREFLIGHT_DEBOUNCE } from '../preflight/usePreflight';

beforeAll(() => {
  PREFLIGHT_DEBOUNCE.ms = 0;
});

afterEach(() => {
  server.events.removeAllListeners();
  delete window.__CURATOR_FEATURES__;
});

/** The bodies of POST /datasets, as sent. */
function registrations(): Record<string, unknown>[] {
  const seen: Record<string, unknown>[] = [];
  server.events.on('request:start', async ({ request }) => {
    if (request.method === 'POST' && new URL(request.url).pathname.endsWith('/api/v1/datasets')) seen.push((await request.clone().json()) as Record<string, unknown>);
  });
  return seen;
}

const mappingIn = (root: HTMLElement): VizMapping => JSON.parse(within(root).getByTestId('mcap-json').textContent ?? '{}') as VizMapping;

async function openAdd(address: string) {
  const r = renderApp('/datasets');
  await r.user.click(await screen.findByRole('button', { name: '添加数据集' }));
  const drawer = await findDrawer('添加数据集');
  await fill(r.user, '数据集地址', address, drawer);
  await pick(r.user, '访问密钥', 'readonly-tos', drawer);
  const section = await within(drawer).findByTestId('mcap-config', {}, { timeout: 5000 });
  await within(section).findByTestId('mcap-table', {}, { timeout: 5000 });
  return { ...r, drawer, section };
}

function row(name: string): HTMLElement {
  return screen.getByRole('link', { name }).closest('tr') as HTMLElement;
}

/** warehouse_mcap without a confirmed mapping (映射待确认). */
function unconfirm(): void {
  db.vizMappings.delete('ds_mcap');
  const d = db.datasets.find((x) => x.id === 'ds_mcap')!;
  d.viz = vizStatusOf('mcap', false);
  d.viz_mapping = mappingInfoOf('mcap');
}

describe('mcap 配置 in 添加数据集 (design doc 18 §6.4, F13.7)', () => {
  it('① GenRobot: the probe matches the UMI template, a changed use goes into the JSON, saving confirms it', async () => {
    const seen = registrations();
    const { user, drawer, section } = await openAdd('tos://pai-kit-datasets/raw/genrobot_drawer');
    expect(within(section).getByTestId('mcap-match')).toHaveTextContent('已按探测结果自动匹配「UMI 手持夹爪（内置）」（覆盖 86%）');
    expect(within(section).getByTestId('mcap-summary')).toHaveTextContent('相机 1 路曲线 2 组（动作 2、状态 0、其他 0）');
    expect(within(section).getByRole('combobox', { name: '/robot0/sensor/imu 的用途' })).toHaveTextContent('忽略');
    expect(mappingIn(section).ignore).toContain('/robot0/sensor/imu');
    // the IMU drawn as a curve: the table, the summary and the JSON follow
    await pick(user, '/robot0/sensor/imu 的用途', '曲线 · 其他', section);
    await waitFor(() => expect(mappingIn(section).series.map((s) => s.topic)).toContain('/robot0/sensor/imu'));
    expect(mappingIn(section).ignore).not.toContain('/robot0/sensor/imu');
    expect(within(section).getByTestId('mcap-summary')).toHaveTextContent('曲线 3 组（动作 2、状态 0、其他 1）');
    expect(within(section).getByRole('textbox', { name: '/robot0/sensor/imu 的显示名' })).toHaveValue('robot0 imu');
    expect(within(drawer).getByText(zh.mcap.willConfirm)).toBeInTheDocument();
    await user.click(within(drawer).getByRole('button', { name: '保存' }));
    await waitFor(() => expect(currentLocation()).toMatch(/^\/datasets\/ds-[a-z]{9}$/));
    const sent = seen[0].viz_mapping as VizMapping;
    expect(sent.base).toBe('builtin:umi');
    expect(sent.series.find((s) => s.topic === '/robot0/sensor/imu')).toMatchObject({ role: 'other', name: 'robot0 imu' });
    // the registration says the mapping is confirmed, and the list says which
    expect(await screen.findByTestId('dataset-viz')).toHaveTextContent('UMI 手持夹爪 · 第 1 版');
    await user.click(screen.getByRole('link', { name: '数据集' }));
    expect(within(await screen.findByRole('link', { name: 'genrobot_drawer' }).then((l) => l.closest('tr') as HTMLElement)).getByTestId('mapping-line')).toHaveTextContent('映射：UMI 手持夹爪');
  });

  it('② ABC-130k: no template fits, the generic one drafts it and the custom schema fields are picked by hand', async () => {
    const seen = registrations();
    const { user, drawer, section } = await openAdd('tos://pai-kit-datasets/raw/abc130k_arrange_flowers');
    expect(drawer).toHaveTextContent(zh.mcap.defaultsUnreadable);
    expect(within(section).getByTestId('mcap-match')).toHaveTextContent('没有模版对得上这些 topic，按「Foxglove 通用」起草');
    // the check reader's known gaps are said before anything is saved
    expect(within(section).getAllByTestId('mcap-probe-warn-checks_gap')).toHaveLength(2);
    const arm = () => mappingIn(section).series.find((s) => s.topic === '/left-arm-state')!;
    expect(arm()).toMatchObject({ role: 'state', fields: ['position'], pair_with: '/left-arm-action' });
    // RobotState's candidate fields are offered; velocity is added to what is drawn
    await pick(user, '/left-arm-state 的字段', 'velocity ×6', section);
    await waitFor(() => expect(arm().fields).toEqual(['position', 'velocity']));
    // the task text from the metadata instead of the /instruction topic, which is ignored then
    await pick(user, zh.mcap.taskSource, /^metadata 键：task_name/, section);
    await waitFor(() => expect(mappingIn(section).task).toEqual({ metadata_key: 'task_name' }));
    expect(mappingIn(section).ignore).toContain('/instruction');
    await user.click(within(drawer).getByRole('button', { name: '保存' }));
    await waitFor(() => expect(currentLocation()).toMatch(/^\/datasets\/ds-[a-z]{9}$/));
    const sent = seen[0].viz_mapping as VizMapping;
    expect(sent.series.find((s) => s.topic === '/left-arm-state')?.fields).toEqual(['position', 'velocity']);
    expect(sent.task).toEqual({ metadata_key: 'task_name' });
  });

  it('a template picked by hand drafts again; 另存为模版 puts the mapping in the library for the next dataset', async () => {
    const { user, section } = await openAdd('tos://pai-kit-datasets/raw/genrobot_drawer');
    await pick(user, zh.mcap.template, '内置 · Foxglove 通用（内置）', section);
    await waitFor(() => expect(mappingIn(section).base).toBe('builtin:foxglove'));
    expect(within(section).getByTestId('mcap-match')).toHaveTextContent('按「Foxglove 通用（内置）」起草（覆盖 100%）');
    await user.click(within(section).getByRole('button', { name: zh.mcap.saveAs }));
    const form = within(section).getByTestId('mcap-saveas');
    await fill(user, zh.mcap.saveAsName, 'GenRobot 双臂', form);
    await user.click(within(form).getByRole('button', { name: zh.mcap.saveAsOk }));
    expect(await screen.findByText(zh.mcap.savedAs('GenRobot 双臂'))).toBeInTheDocument();
    expect(db.vizTemplates.map((t) => t.name)).toEqual(['GenRobot 双臂']);
    // the library offers it now
    await pick(user, zh.mcap.template, '团队模版 · GenRobot 双臂', section);
    await waitFor(() => expect(within(section).getByTestId('mcap-match')).toHaveTextContent('按「GenRobot 双臂」起草（覆盖 100%）'));
  });

  it('imports a mapping JSON checked like a save: a bad one is refused with every problem, a good one fills the table', async () => {
    const { user, section } = await openAdd('tos://pai-kit-datasets/raw/genrobot_drawer');
    const before = mappingIn(section);
    const input = within(section).getByLabelText(zh.mcap.importJson) as HTMLInputElement;
    const bad = { ...before, series: [{ topic: '/nope', name: 'x', role: 'both' }] };
    await user.upload(input, new File([JSON.stringify(bad)], 'bad.json', { type: 'application/json' }));
    const problems = await within(section).findByTestId('mcap-import-problems');
    expect(problems).toHaveTextContent('bad.json 不是合格的映射（1 处问题），没有导入');
    expect(problems).toHaveTextContent('series.0.role：只能是 state、action 或 other');
    expect(mappingIn(section)).toEqual(before);
    const good = { ...before, name: '自带的', cameras: [] };
    await user.upload(input, new File([JSON.stringify(good)], 'mine.json', { type: 'application/json' }));
    await waitFor(() => expect(mappingIn(section).name).toBe('自带的'));
    expect(within(section).getByTestId('mcap-match')).toHaveTextContent('已从 mine.json 导入');
    expect(within(section).getByTestId('mcap-warn-no_camera')).toHaveTextContent(zh.mcap.warn.no_camera);
    expect(within(section).queryByTestId('mcap-import-problems')).toBeNull();
  });

  it('an external annotation file is uploaded and attached with the registration; a local path can be added too', async () => {
    window.__CURATOR_FEATURES__ = { local_input: true };
    const seen = registrations();
    const { user } = renderApp('/datasets');
    await user.click(await screen.findByRole('button', { name: '添加数据集' }));
    const drawer = await findDrawer('添加数据集');
    await user.click(within(drawer).getByRole('radio', { name: /本地挂载路径/ }));
    await fill(user, zh.taskForm.localPath, '/data/datasets/droid_local', drawer);
    const doc = { timeline: [{ start: 0, end: 1, label: 'reach' }], key_events: [{ t: 0.5, label: 'grasp' }] };
    await user.upload(within(drawer).getByLabelText(zh.annotations.label), new File([JSON.stringify(doc)], 'episode_3.json', { type: 'application/json' }));
    expect(await within(drawer).findByTestId('annotations-uploaded')).toHaveTextContent('episode_3.json · 1 条 episode · 1 段 · 1 个事件');
    await within(drawer).findByText(/LeRobot v2 · 120 条 episode/);
    await user.click(within(drawer).getByRole('button', { name: '保存' }));
    await waitFor(() => expect(currentLocation()).toMatch(/^\/datasets\/ds-[a-z]{9}$/));
    expect(seen[0]).toMatchObject({ input: { source: 'local', uri: '/data/datasets/droid_local' } });
    expect(String(seen[0].annotations_upload)).toMatch(/^upl/);
    expect(await screen.findByTestId('dataset-annotations')).toHaveTextContent('episode_3.json · Argus 风格 · 1 条 episode');
  });
});

describe('mcap 配置 of a registered dataset (design doc 18 §6.4 step 5–7, F13.7)', () => {
  it('③ unconfirmed: the list says 映射待确认 and greys 可视化; confirming in the drawer makes it ready', async () => {
    unconfirm();
    const { user } = renderApp('/datasets');
    await screen.findByRole('link', { name: 'warehouse_mcap' });
    expect(within(row('warehouse_mcap')).getByTestId('mapping-line')).toHaveTextContent('映射：待确认');
    expect(within(row('warehouse_mcap')).getByRole('button', { name: '可视化' })).toBeDisabled();
    // a task still starts on it: the checks read it with the site's defaults
    expect(within(row('warehouse_mcap')).getByRole('button', { name: '新建任务' })).toBeEnabled();
    // the entry is in 更多; LeRobot rows have none
    await user.click(within(row('droid_100')).getByRole('button', { name: '更多操作：droid_100' }));
    const menu = await waitFor(() => document.querySelector('.arco-dropdown-menu') as HTMLElement);
    expect(within(menu).queryByText(zh.mcap.entry)).toBeNull();
    await user.click(within(row('warehouse_mcap')).getByRole('button', { name: '更多操作：warehouse_mcap' }));
    await user.click(await screen.findByText(zh.mcap.entry));
    const drawer = await findDrawer('mcap 配置 · warehouse_mcap');
    expect(await within(drawer).findByTestId('mcap-current')).toHaveTextContent(zh.mcap.none);
    await within(drawer).findByTestId('mcap-table', {}, { timeout: 5000 });
    await user.click(within(drawer).getByRole('button', { name: '确认为第 1 版' }));
    expect(await screen.findByText('已确认映射第 1 版')).toBeInTheDocument();
    await waitFor(() => expect(within(row('warehouse_mcap')).getByTestId('mapping-line')).toHaveTextContent('映射：Foxglove 通用'));
    expect(within(row('warehouse_mcap')).getByRole('link', { name: '可视化' })).toHaveAttribute('href', '/visualize?dataset=ds_mcap');
  });

  it('the detail page opens it from ?mcap=1 (去确认映射) with the confirmed version, and saving adds a version', async () => {
    const { user } = renderApp('/datasets/ds_mcap?mcap=1');
    const drawer = await findDrawer('mcap 配置 · warehouse_mcap');
    expect(await within(drawer).findByTestId('mcap-current')).toHaveTextContent('已确认第 1 版「warehouse（Foxglove 通用）」');
    const section = await within(drawer).findByTestId('mcap-config', {}, { timeout: 5000 });
    await within(section).findByTestId('mcap-table', {}, { timeout: 5000 });
    expect(within(section).getByTestId('mcap-match')).toHaveTextContent('下表是已确认的第 1 版');
    expect(mappingIn(section).name).toBe('warehouse（Foxglove 通用）');
    expect(within(drawer).getByTestId('mcap-check-mapping')).toHaveTextContent('"video_topics"');
    // nothing changed yet: confirming the same version again adds nothing
    expect(within(drawer).getByRole('button', { name: '确认为第 2 版' })).toBeDisabled();
    await fill(user, '/observation.images.front 的显示名', '前视', section);
    await user.click(within(drawer).getByRole('button', { name: '确认为第 2 版' }));
    expect(await screen.findByText('已确认映射第 2 版')).toBeInTheDocument();
    expect(db.vizMappings.get('ds_mcap')?.mapping.cameras[0].name).toBe('前视');
    await waitFor(() => expect(screen.getByTestId('dataset-viz')).toHaveTextContent('warehouse（Foxglove 通用） · 第 2 版'));
    expect(currentLocation()).toBe('/datasets/ds_mcap');
  });

  it('a mapping the dataset cannot take is refused with every problem the Daemon names', async () => {
    const { user } = renderApp('/datasets/ds_mcap?mcap=1');
    const drawer = await findDrawer('mcap 配置 · warehouse_mcap');
    const section = await within(drawer).findByTestId('mcap-config', {}, { timeout: 5000 });
    await within(section).findByTestId('mcap-table', {}, { timeout: 5000 });
    // an import naming a topic the file lacks is refused right away, as the save would be
    const bad = { ...mappingIn(section), cameras: [{ topic: '/elsewhere', name: 'x' }] };
    await user.upload(within(section).getByLabelText(zh.mcap.importJson), new File([JSON.stringify(bad)], 'other.json'));
    expect(await within(section).findByTestId('mcap-import-problems')).toHaveTextContent('cameras.0：数据集里没有 topic /elsewhere');
  });

  it('the detail page attaches, replaces and removes the external annotation file', async () => {
    const { user } = renderApp('/datasets/ds_droid100');
    const box = await screen.findByTestId('dataset-annotations');
    expect(box).toHaveTextContent(zh.annotations.none);
    // LeRobot: no field mapping row
    expect(screen.getByTestId('dataset-viz')).not.toHaveTextContent(zh.mcap.mapping);
    const zip = new File([new Uint8Array([0x50, 0x4b, 3, 4, 0, 0])], 'labels.zip', { type: 'application/zip' });
    await user.upload(within(box).getByLabelText(zh.annotations.label), zip);
    expect(await screen.findByText(zh.annotations.attached)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('dataset-annotations')).toHaveTextContent('labels.zip · Argus 风格 · 12 条 episode'));
    await user.click(within(screen.getByTestId('dataset-annotations')).getByRole('button', { name: zh.annotations.remove }));
    const dialog = await screen.findByRole('dialog', { name: zh.annotations.removeTitle });
    await user.click(within(dialog).getByRole('button', { name: zh.annotations.remove }));
    expect(await screen.findByText(zh.annotations.detached)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('dataset-annotations')).toHaveTextContent(zh.annotations.none));
  });

  it('a file the Daemon cannot read as annotations says why', async () => {
    const { user } = renderApp('/datasets/ds_droid100');
    const box = await screen.findByTestId('dataset-annotations');
    await user.upload(within(box).getByLabelText(zh.annotations.label), new File([JSON.stringify({ steps: [] })], 'weird.json', { type: 'application/json' }));
    expect(await within(box).findByTestId('annotations-error')).toHaveTextContent('标注格式不支持');
    expect(box).toHaveTextContent(zh.annotations.none);
  });
});
