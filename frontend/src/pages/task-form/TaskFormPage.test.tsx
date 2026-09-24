import { screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { beforeAll, describe, expect, it } from 'vitest';
import { db } from '../../mocks/db';
import { server } from '../../mocks/server';
import { fill, pick } from '../../test/arco';
import { fieldErrors, requiredFieldLabels } from '../../test/forms';
import { currentLocation, renderApp } from '../../test/render';
import { PREFLIGHT_DEBOUNCE } from '../../features/preflight/usePreflight';

beforeAll(() => {
  PREFLIGHT_DEBOUNCE.ms = 0;
});

interface Seen {
  method: string;
  path: string;
  body: unknown;
  headers: Record<string, string>;
}

/** The label a Select shows for what is picked (Arco keeps it next to the combobox). */
const chosen = (label: string) => screen.getByRole('combobox', { name: label }).closest('.arco-select')?.textContent ?? '';

function record(): Seen[] {
  const seen: Seen[] = [];
  server.events.on('request:start', async ({ request }) => {
    const url = new URL(request.url);
    if (!url.pathname.includes('/api/v1/')) return;
    const text = request.method === 'GET' ? '' : await request.clone().text();
    seen.push({ method: request.method, path: url.pathname.replace(/.*\/api\/v1/, ''), body: text ? JSON.parse(text) : null, headers: Object.fromEntries(request.headers.entries()) });
  });
  return seen;
}

const s1 = () => screen.getByTestId('screen-1');
const s2 = () => screen.getByTestId('screen-2');

/** The form item whose label starts with `label` (labels may carry a hint in brackets). */
function formItem(label: string): HTMLElement {
  const l = [...document.querySelectorAll('label')].find((x) => (x.textContent ?? '').replace(/\s+/g, '').startsWith(label.replace(/\s+/g, '')));
  const item = l?.closest('.arco-form-item');
  if (!(item instanceof HTMLElement)) throw new Error(`no form item ${label}`);
  return item;
}

describe('新建任务 · 第一屏 (07 §3)', () => {
  it('marks every required field with a red * and validates them before 下一步', async () => {
    const { user } = renderApp('/tasks/new');
    await screen.findByText('基本信息');
    expect(requiredFieldLabels(s1())).toEqual(
      expect.arrayContaining(['任务名称', '数据来源', '数据集地址', '地域', '访问密钥 管理', '交付目录（质检报告与交付数据集的写入位置）', '访问密钥']),
    );
    await user.click(screen.getByRole('button', { name: '下一步：模块设置' }));
    const errs = fieldErrors(s1());
    expect(errs).toEqual(expect.arrayContaining(['请填写任务名称', '请填写数据集地址', '请选择访问密钥', '请填写交付目录']));
    expect(screen.getByTestId('screen-2')).not.toBeVisible();
  });

  it('runs the preflight by itself and renders availability tri-state from reason_code', async () => {
    const { user } = renderApp('/tasks/new');
    await screen.findByText('基本信息');
    await fill(user, '数据集地址', 'tos://pai-kit-datasets/lerobot/droid-200');
    await pick(user, '访问密钥', 'readonly-tos');
    const card = await screen.findByTestId('preflight-card');
    expect(await within(card).findByText(/LeRobot v2 · 200 条 episode · 3 路相机/)).toBeInTheDocument();
    expect(within(card).getByText(/88 条没有任务标注/)).toBeInTheDocument();
    // motion_quality is unsupported (missing observation.state) → greyed with the Chinese reason
    const motion = await screen.findByTestId('module-motion_quality');
    expect(within(motion).getByRole('checkbox', { name: '运动质量' })).toBeDisabled();
    expect(motion).toHaveTextContent('数据集缺少 observation.state 列');
    // kinematic_limits needs input (robot type) → selectable, with the screen-2 hint
    expect(screen.getByTestId('module-kinematic_limits')).toHaveTextContent('需要补充机器人型号（下一屏填）');
    // unknown reason codes fall back to the English reason (checked in lib tests); details modal:
    await user.click(within(motion).getByRole('button', { name: '详细信息' }));
    expect(await screen.findByText('运动质量：为什么不能开启')).toBeInTheDocument();
  });

  it('screen-1 texts: 快速质检, no note by 高级设置, 全部 for every episode (requester item 15)', async () => {
    renderApp('/tasks/new');
    await screen.findByText('基本信息');
    expect(within(screen.getByRole('radiogroup', { name: '质检范围' })).getByRole('radio', { name: '快速质检' })).toBeInTheDocument();
    expect(within(screen.getByRole('radiogroup', { name: 'Episode 选择' })).getByRole('radio', { name: '全部' })).toBeInTheDocument();
    expect(screen.getByText('高级设置')).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent('不改也能跑');
    expect(document.body).not.toHaveTextContent('不调用模型的模块）');
  });

  it('quick preset hides the model block; any manual change switches to 自选', async () => {
    const { user } = renderApp('/tasks/new');
    await screen.findByText('基本信息');
    await fill(user, '数据集地址', 'tos://pai-kit-datasets/lerobot/droid-200');
    await pick(user, '访问密钥', 'readonly-tos');
    await screen.findByText(/LeRobot v2 · 200 条 episode/);
    expect(await screen.findByText('模型配置')).toBeInTheDocument();
    await user.click(screen.getByText('快速质检'));
    await waitFor(() => expect(screen.queryByText('模型配置')).toBeNull());
    expect(within(screen.getByTestId('module-task_success')).getByRole('checkbox')).not.toBeChecked();
    await user.click(within(screen.getByTestId('module-dedup')).getByRole('checkbox'));
    expect(within(screen.getByRole('radiogroup', { name: '质检范围' })).getByRole('radio', { name: '自选' })).toBeChecked();
  });
});

describe('新建任务 · 两屏与提交', () => {
  it('creates and starts: registers the typed address, sends it exactly, then starts (D36, D30)', async () => {
    const seen = record();
    const { user } = renderApp('/tasks/new');
    await screen.findByText('基本信息');
    await fill(user, '任务名称', 'new set 抽检');
    await fill(user, '数据集地址', 'tos://pai-kit-datasets/lerobot/new_set');
    await pick(user, '访问密钥', 'prod-tos');
    await fill(user, '交付目录', 'tos://pai-kit-deliveries/new-set-0921');
    await screen.findByText(/LeRobot v2 · 120 条 episode/);
    await user.click(screen.getByText('快速质检'));
    await user.click(screen.getByRole('button', { name: '下一步：模块设置' }));
    await waitFor(() => expect(s2()).toBeVisible());
    // Screen 2 is generated from param_schema: video_action_sync has one parameter
    const params = within(s2()).getByTestId('params-video_action_sync');
    expect(within(params).getByText('同步曲线证据图')).toBeInTheDocument();
    await user.click(within(params).getByText('全部'));
    // Modules without extra settings are not listed (requester item 16).
    expect(s2()).not.toHaveTextContent('无需额外设置');
    expect(within(s2()).queryByTestId('nothing-to-set')).toBeNull();
    // While the start checks run, the buttons show their loading state and nothing else is said
    // (requester item 12).
    let release = () => {};
    const held = new Promise<void>((r) => (release = r));
    server.use(
      http.post('*/api/v1/tasks/:id/actions/start', async () => {
        await held;
      }),
    );
    await user.click(screen.getByRole('button', { name: '创建并开始' }));
    await waitFor(() => expect(seen.some((s) => s.method === 'POST' && /\/actions\/start$/.test(s.path))).toBe(true));
    expect(screen.getByRole('button', { name: '创建并开始' })).toHaveClass('arco-btn-loading');
    expect(document.body).not.toHaveTextContent('正在做开始前检查');
    release();
    await waitFor(() => expect(currentLocation()).toMatch(/^\/tasks\/task-[a-z]{9}\b/));
    const reg = seen.find((s) => s.method === 'POST' && s.path === '/datasets');
    expect(reg?.body).toEqual({ input: { source: 'tos', uri: 'tos://pai-kit-datasets/lerobot/new_set', region: 'cn-beijing', credential: 'prod-tos' } });
    const create = seen.find((s) => s.method === 'POST' && s.path === '/tasks');
    const body = create?.body as Record<string, unknown>;
    expect(body.input).toEqual({ dataset_id: expect.stringMatching(/^ds-[a-z]{9}$/) });
    expect(body.output).toEqual({ uri: 'tos://pai-kit-deliveries/new-set-0921', region: 'cn-beijing', credential: 'prod-tos' });
    expect(body.modules).toEqual(['timestamp_check', 'kinematic_limits', 'motion_quality', 'visual_quality', { id: 'video_action_sync', params: { sync_plots: 'all' } }, 'dedup']);
    expect(body).not.toHaveProperty('vlm');
    expect((body.params as Record<string, unknown>).start_now).toBe(false);
    expect(seen.some((s) => s.method === 'POST' && /\/tasks\/[^/]+\/actions\/start$/.test(s.path))).toBe(true);
    expect(await screen.findByText('任务已创建，进入队列')).toBeInTheDocument();
  });

  it('screen 2 with nothing to set says so instead of showing an empty card', async () => {
    const { user } = renderApp('/tasks/new');
    await screen.findByText('基本信息');
    await fill(user, '任务名称', 'ts only');
    await fill(user, '数据集地址', 'tos://pai-kit-datasets/lerobot/new_set');
    await pick(user, '访问密钥', 'prod-tos');
    await fill(user, '交付目录', 'tos://pai-kit-deliveries/ts-only');
    await screen.findByText(/LeRobot v2 · 120 条 episode/);
    await user.click(screen.getByRole('button', { name: '清空' }));
    await user.click(within(screen.getByTestId('module-timestamp_check')).getByRole('checkbox'));
    await user.click(screen.getByRole('button', { name: '下一步：模块设置' }));
    await waitFor(() => expect(s2()).toBeVisible());
    expect(within(s2()).getByTestId('nothing-to-set')).toHaveTextContent('开启的模块都不需要额外设置，可以直接创建。');
    expect(s2()).not.toHaveTextContent('时间戳检查');
  });

  it('the EEF module is opted into by hand, needs a model and takes its trajectory.json as an upload (F5.5, D49)', async () => {
    const seen = record();
    const { user } = renderApp('/tasks/new');
    await screen.findByText('基本信息');
    await fill(user, '任务名称', 'eef demo');
    await fill(user, '数据集地址', 'tos://pai-kit-datasets/lerobot/new_set');
    await pick(user, '访问密钥', 'prod-tos');
    await fill(user, '交付目录', 'tos://pai-kit-deliveries/eef-demo');
    await screen.findByText(/LeRobot v2 · 120 条 episode/);
    await user.click(screen.getByText('快速质检'));
    const eef = screen.getByRole('checkbox', { name: 'EEF–视频一致性' });
    expect(eef).not.toBeChecked();                            // not in any preset
    expect(screen.queryByRole('checkbox', { name: 'EEF–视频一致性 · VLM 复核' })).toBeNull();   // folded in (D49)
    await user.click(eef);
    await user.click(screen.getByRole('button', { name: '下一步：模块设置' }));
    await waitFor(() => expect(s2()).toBeVisible());
    await user.click(screen.getByRole('button', { name: '保存为待启动' }));
    await waitFor(() => expect(fieldErrors(s2())).toEqual(['请填写trajectory.json', '请上传夹爪参考（观测种子或夹爪外观模板，二选一）']));
    const input = within(s2()).getByLabelText('trajectory.json', { selector: 'input[type=file]' });
    const bad = { samples: [{ episode_index: 0, sample: { sample_id: 'new_set_000000' }, frames: [{ truth: [1, 2] }] }] };
    await user.upload(input, new File([JSON.stringify(bad)], 'trajectory.json', { type: 'application/json' }));
    const problem = await within(s2()).findByTestId('upload-error-trajectory_json');
    expect(problem).toHaveTextContent('评估字段不能进检测输入');
    expect(problem).toHaveTextContent('new_set_000000');
    const good = { schema_version: 'eef-video/1.0.0', samples: [0, 1].map((i) => ({ episode_index: i, sample: { sample_id: `new_set_00000${i}` }, frames: [{}, {}, {}] })) };
    await user.upload(input, new File([JSON.stringify(good)], 'trajectory.json', { type: 'application/json' }));
    expect(await within(s2()).findByTestId('upload-done-trajectory_json')).toHaveTextContent('trajectory.json');
    expect(within(s2()).queryByTestId('upload-error-trajectory_json')).toBeNull();
    // still no seeds and no template: nothing would find the gripper, every episode would go to a person
    await user.click(screen.getByRole('button', { name: '保存为待启动' }));
    await waitFor(() => expect(fieldErrors(s2())).toEqual(['请上传夹爪参考（观测种子或夹爪外观模板，二选一）']));
    await user.upload(within(s2()).getByLabelText('观测种子', { selector: 'input[type=file]' }), new File([JSON.stringify([{ sample_id: 'new_set_000000' }])], 'seeds.json', { type: 'application/json' }));
    expect(await within(s2()).findByTestId('upload-done-observation_seeds')).toHaveTextContent('seeds.json');
    await user.click(screen.getByRole('button', { name: '保存为待启动' }));
    await waitFor(() => expect(currentLocation()).toMatch(/^\/tasks\/task-[a-z]{9}\b/));
    const up = seen.filter((x) => x.method === 'POST' && x.path.startsWith('/uploads'));
    expect(up).toHaveLength(3);
    const body = seen.find((x) => x.method === 'POST' && x.path === '/tasks')?.body as { modules: unknown[]; vlm?: unknown };
    expect(body.modules).toContainEqual({
      id: 'eef_video_consistency',
      params: { trajectory_json: expect.stringMatching(/^upload:upl-[a-z]{9}$/), observation_seeds: expect.stringMatching(/^upload:upl-[a-z]{9}$/) },
    });
    expect(body.vlm).toBeTruthy();                             // it reviews with a model
  });

  it('夹爪参考 is one field: pick 观测种子 or 夹爪外观模板, one upload button, the help text follows; only the chosen one is sent (fourth round)', async () => {
    const seen = record();
    const { user } = renderApp('/tasks/new');
    await screen.findByText('基本信息');
    await fill(user, '任务名称', 'eef template');
    await fill(user, '数据集地址', 'tos://pai-kit-datasets/lerobot/new_set');
    await pick(user, '访问密钥', 'prod-tos');
    await fill(user, '交付目录', 'tos://pai-kit-deliveries/eef-template');
    await screen.findByText(/LeRobot v2 · 120 条 episode/);
    await user.click(screen.getByText('快速质检'));
    // screen 1 does not warn about the files screen 2 asks for
    expect(screen.getByTestId('module-eef_video_consistency')).not.toHaveTextContent('trajectory.json');
    await user.click(screen.getByRole('checkbox', { name: 'EEF–视频一致性' }));
    // 超时对冲 is a dropdown like the rest of its row (fourth round)
    await user.click(screen.getByText('高级设置'));
    await pick(user, '超时对冲', '关闭');
    await user.click(screen.getByRole('button', { name: '下一步：模块设置' }));
    await waitFor(() => expect(s2()).toBeVisible());
    expect(s2()).not.toHaveTextContent('可选设置');
    expect(within(s2()).getByRole('heading', { name: 'EEF–视频一致性' })).toBeInTheDocument();
    const group = within(s2()).getByTestId('choice-gripper_reference');
    const item = group.closest('.arco-form-item, .field') as HTMLElement;
    expect(item).toHaveTextContent('P-A 跟踪的种子');
    const traj = { schema_version: 'eef-video/1.0.0', samples: [0, 1].map((i) => ({ episode_index: i, sample: { sample_id: `new_set_00000${i}` }, frames: [{}, {}] })) };
    await user.upload(within(s2()).getByLabelText('trajectory.json', { selector: 'input[type=file]' }), new File([JSON.stringify(traj)], 'trajectory.json', { type: 'application/json' }));
    await within(s2()).findByTestId('upload-done-trajectory_json');
    await user.upload(within(s2()).getByLabelText('观测种子', { selector: 'input[type=file]' }), new File([JSON.stringify([{ sample_id: 'new_set_000000' }])], 'seeds.json', { type: 'application/json' }));
    const seedsDone = await within(s2()).findByTestId('upload-done-observation_seeds');
    // the result sits under the whole row, lined up with the dropdown, not beside it
    expect(within(group).getByTestId('upload-done-observation_seeds')).toBe(seedsDone);
    expect(seedsDone.closest('.choice-group')).toBeNull();
    // switching drops the seeds: 二选一
    await pick(user, '夹爪参考', '夹爪外观模板', s2());
    expect(item).toHaveTextContent('gripper-template/1.0');
    expect(within(s2()).queryByLabelText('观测种子', { selector: 'input[type=file]' })).toBeNull();
    await user.click(screen.getByRole('button', { name: '保存为待启动' }));
    await waitFor(() => expect(fieldErrors(s2())).toEqual(['请上传夹爪参考（观测种子或夹爪外观模板，二选一）']));
    const template = { schema_version: 'gripper-template/1.0', entries: [{ camera_id: 'front' }, { camera_id: 'wrist' }, { camera_id: 'front' }] };
    await user.upload(within(s2()).getByLabelText('夹爪外观模板', { selector: 'input[type=file]' }), new File([JSON.stringify(template)], 'template.json', { type: 'application/json' }));
    const done = await within(s2()).findByTestId('upload-done-gripper_template');
    expect(done).toHaveTextContent('template.json');
    expect(done).toHaveTextContent('3 个模板条目');
    expect(done).toHaveTextContent('相机 front、wrist');
    await user.click(screen.getByRole('button', { name: '保存为待启动' }));
    await waitFor(() => expect(currentLocation()).toMatch(/^\/tasks\/task-[a-z]{9}\b/));
    const body = seen.find((x) => x.method === 'POST' && x.path === '/tasks')?.body as { modules: { id: string; params?: Record<string, unknown> }[] };
    const eef = body.modules.find((m) => m.id === 'eef_video_consistency');
    expect(Object.keys(eef?.params ?? {}).sort()).toEqual(['gripper_template', 'trajectory_json']);
    expect(((body as unknown as { params: Record<string, unknown> }).params).vlm_hedge).toBe(false);
  });

  it('two uploads at once: the one finishing last does not drop the other (galbot 2026-09-24: seeds lost behind a slow trajectory.json)', async () => {
    const seen = record();
    // trajectory.json is large and validated on arrival: it finishes after the seeds
    let release = () => {};
    const held = new Promise<void>((r) => (release = r));
    server.use(
      http.post('*/api/v1/uploads', async ({ request }) => {
        if (new URL(request.url).searchParams.get('kind') === 'eef_trajectory') await held;
      }),
    );
    const { user } = renderApp('/tasks/new');
    await screen.findByText('基本信息');
    await fill(user, '任务名称', 'eef two files');
    await fill(user, '数据集地址', 'tos://pai-kit-datasets/lerobot/new_set');
    await pick(user, '访问密钥', 'prod-tos');
    await fill(user, '交付目录', 'tos://pai-kit-deliveries/eef-two');
    await screen.findByText(/LeRobot v2 · 120 条 episode/);
    await user.click(screen.getByText('快速质检'));
    await user.click(screen.getByRole('checkbox', { name: 'EEF–视频一致性' }));
    await user.click(screen.getByRole('button', { name: '下一步：模块设置' }));
    await waitFor(() => expect(s2()).toBeVisible());
    const traj = { schema_version: 'eef-video/1.0.0', samples: [0, 1].map((i) => ({ episode_index: i, sample: { sample_id: `new_set_00000${i}` }, frames: [{}, {}] })) };
    await user.upload(within(s2()).getByLabelText('trajectory.json', { selector: 'input[type=file]' }), new File([JSON.stringify(traj)], 'trajectory.json', { type: 'application/json' }));
    const seeds = [{ sample_id: 'new_set_000000', frame_index: 0 }];
    await user.upload(within(s2()).getByLabelText('观测种子', { selector: 'input[type=file]' }), new File([JSON.stringify(seeds)], 'seeds.json', { type: 'application/json' }));
    expect(await within(s2()).findByTestId('upload-done-observation_seeds')).toHaveTextContent('seeds.json');
    // trajectory.json is still being checked: nothing can be created yet (fourth round)
    expect(within(s2()).getByTestId('upload-button-trajectory_json')).toHaveTextContent(/^(上传中|校验中)…$/);
    expect(screen.getByRole('button', { name: '保存为待启动' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '创建并开始' })).toBeDisabled();
    release();
    expect(await within(s2()).findByTestId('upload-done-trajectory_json')).toHaveTextContent('trajectory.json');
    expect(within(s2()).getByTestId('upload-done-observation_seeds')).toHaveTextContent('seeds.json');   // still there
    await waitFor(() => expect(screen.getByRole('button', { name: '保存为待启动' })).toBeEnabled());
    await user.click(screen.getByRole('button', { name: '保存为待启动' }));
    await waitFor(() => expect(currentLocation()).toMatch(/^\/tasks\/task-[a-z]{9}\b/));
    const body = seen.find((x) => x.method === 'POST' && x.path === '/tasks')?.body as { modules: unknown[] };
    expect(body.modules).toContainEqual({
      id: 'eef_video_consistency',
      params: { trajectory_json: expect.stringMatching(/^upload:upl-[a-z]{9}$/), observation_seeds: expect.stringMatching(/^upload:upl-[a-z]{9}$/) },
    });
  });

  it('screen 2 asks for the robot type (required) or 跳过该模块', async () => {
    const { user } = renderApp('/tasks/new?dataset=tos://pai-kit-datasets/lerobot/droid-200');
    await screen.findByText(/LeRobot v2 · 200 条 episode/);
    await fill(user, '任务名称', 'droid 200');
    await fill(user, '交付目录', 'tos://pai-kit-deliveries/droid-200-x');
    await pick(user, '交付目录访问密钥', 'prod-tos');
    // The backend and the model come from the default model (C4 1.6.0).
    await user.click(screen.getByRole('button', { name: '下一步：模块设置' }));
    await waitFor(() => expect(s2()).toBeVisible());
    const needs = within(s2()).getByTestId('needs-kinematic_limits');
    expect(requiredFieldLabels(needs)).toEqual(['机器人型号']);
    await user.click(screen.getByRole('button', { name: '保存为待启动' }));
    await waitFor(() => expect(fieldErrors(s2())).toEqual(['请选择机器人型号']));
    await user.click(within(needs).getByRole('button', { name: '跳过该模块' }));
    expect(within(s2()).getByText('已跳过')).toBeInTheDocument();
    expect(screen.getByTestId('footer-summary')).toHaveTextContent('开启 6 个模块');
  });

  it('the form starts with the default model, marked in the list, and it can still be changed (C4 1.6.0)', async () => {
    const { user } = renderApp('/tasks/new?dataset=tos://pai-kit-datasets/lerobot/droid-200');
    await screen.findByText(/LeRobot v2 · 200 条 episode/);
    await waitFor(() => expect(chosen('VLM 后端')).toContain('ark-prod'));
    expect(chosen('模型')).toContain('doubao-seed-2-0-pro-260215');
    await pick(user, '模型', /^doubao-seed-2-0-pro-260215（默认）$/);
    await pick(user, '模型', 'doubao-seed-2-0-lite-260215');
    await waitFor(() => expect(chosen('模型')).toContain('doubao-seed-2-0-lite-260215'));
  });

  it('without a default model the form asks for the backend and the model, as before (C4 1.6.0)', async () => {
    for (const b of db.backends) for (const m of b.models) m.is_default = false;
    renderApp('/tasks/new?dataset=tos://pai-kit-datasets/lerobot/droid-200');
    await screen.findByText(/LeRobot v2 · 200 条 episode/);
    expect(chosen('VLM 后端')).toContain('选择 VLM 后端');
    expect(chosen('模型')).toContain('选择模型');
  });

  it('409 source_changed at start → fingerprint dialog → 重新预检 → started (D37)', async () => {
    const seen = record();
    const { user } = renderApp('/tasks/new?dataset=tos://pai-kit-datasets/lerobot/droid-200&region=cn-beijing');
    await screen.findByText(/LeRobot v2 · 200 条 episode/);
    await fill(user, '任务名称', 'droid 200 recheck');
    await fill(user, '交付目录', 'tos://pai-kit-deliveries/droid-200-y');
    await pick(user, '交付目录访问密钥', 'prod-tos');
    await user.click(screen.getByText('快速质检'));
    await user.click(screen.getByRole('button', { name: '下一步：模块设置' }));
    await pick(user, '机器人型号', 'franka', s2());
    await user.click(screen.getByRole('button', { name: '创建并开始' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('数据集和添加时不一样了')).toBeInTheDocument();
    expect(within(dialog).getByTestId('source-change')).toHaveTextContent('meta 文件有变化');
    expect(within(dialog).getByTestId('source-change')).toHaveTextContent('文件：新增 12 个 · 删除 0 个 · 改动 1 个');
    expect(within(dialog).getByText('data/chunk-000/episode_000200.parquet')).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: '重新预检' }));
    await waitFor(() => expect(currentLocation()).toMatch(/^\/tasks\/task-[a-z]{9}\b/));
    expect(seen.some((s) => /\/repreflight$/.test(s.path))).toBe(true);
    expect(await screen.findByText('重新预检通过，任务已开始')).toBeInTheDocument();
  });

  it('an incompatible repreflight brings the form back with the fields marked', async () => {
    server.use(
      http.post('*/api/v1/tasks/:id/repreflight', async ({ params }) => {
        const t = await (await fetch(`http://localhost/api/v1/tasks/${String(params.id)}`)).json();
        return HttpResponse.json({
          compatible: false,
          incompatibilities: [
            { field: 'episodes', reason_code: 'episodes_out_of_range', reason: '前 150 条超出了范围：数据集现在只有 120 条' },
            { field: 'modules', module: 'dedup', reason_code: 'module_unsupported', reason: '精确去重现在不可用' },
          ],
          task: t,
        });
      }),
    );
    const { user } = renderApp('/tasks/new?dataset=tos://pai-kit-datasets/lerobot/droid-200');
    await screen.findByText(/LeRobot v2 · 200 条 episode/);
    await fill(user, '任务名称', 'droid 200 shrink');
    await fill(user, '交付目录', 'tos://pai-kit-deliveries/droid-200-z');
    await pick(user, '交付目录访问密钥', 'prod-tos');
    await user.click(screen.getByText('快速质检'));
    await user.click(screen.getByText('前 N 条'));
    await fill(user, '条数', '150');
    await user.click(screen.getByRole('button', { name: '下一步：模块设置' }));
    await pick(user, '机器人型号', 'franka', s2());
    await user.click(screen.getByRole('button', { name: '创建并开始' }));
    await user.click(await screen.findByRole('button', { name: '重新预检' }));
    const list = await screen.findByTestId('incompatibilities');
    expect(list).toHaveTextContent('前 150 条超出了范围');
    await waitFor(() => expect(s1()).toBeVisible());
    expect(within(s1()).getByText('前 150 条超出了范围：数据集现在只有 120 条')).toBeInTheDocument();
    expect(screen.getByTestId('module-dedup')).toHaveClass('marked');
    expect(currentLocation()).toMatch(/^\/tasks\/new\?edit=task-[a-z]{9}\b/);
  });

  it('pre-start check failures stay on the form, under the field concerned (D30)', async () => {
    const { user } = renderApp('/tasks/new');
    await screen.findByText('基本信息');
    await fill(user, '任务名称', 'bad delivery');
    await fill(user, '数据集地址', 'tos://pai-kit-datasets/lerobot/new_set');
    await pick(user, '访问密钥', 'readonly-tos');
    await fill(user, '交付目录', 'tos://pai-kit-deliveries/x');
    await screen.findByText(/LeRobot v2 · 120 条 episode/);
    await user.click(screen.getByText('快速质检'));
    await user.click(screen.getByRole('button', { name: '下一步：模块设置' }));
    await user.click(screen.getByRole('button', { name: '创建并开始' }));
    expect(await screen.findByText('任务已保存为待启动，但开始前检查没过：按标出的地方改好后再开始')).toBeInTheDocument();
    await waitFor(() => expect(s1()).toBeVisible());
    // details.checks = [{id, ok, code, reason, target, elapsed_ms}] (W8): each failure under its field.
    expect(within(formItem('交付目录')).getByText(/对访问密钥 readonly-tos 只读/)).toBeInTheDocument();
    expect(within(formItem('数据集地址')).queryByText(/读不到/)).toBeNull();
  });

  it('the delivery write probe: a failure is an error, a leftover probe object only a warning (W8)', async () => {
    const { user } = renderApp('/tasks/new');
    await screen.findByText('基本信息');
    await fill(user, '数据集地址', 'tos://pai-kit-datasets/lerobot/new_set');
    await pick(user, '访问密钥', 'readonly-tos');
    await pick(user, '交付目录访问密钥', 'prod-tos');
    await fill(user, '交付目录', 'tos://pai-kit-scratch/out');
    await user.tab();
    const out = formItem('交付目录');
    expect(await within(out).findByRole('status')).toHaveTextContent('写探针通过，但留下了探针对象：探针对象 tos://pai-kit-scratch/out/.curator-probe 已写入，但没能删掉');
    expect(fieldErrors(out)).toEqual([]);
    await fill(user, '交付目录', 'tos://pai-kit-nosuch/out');
    await user.tab();
    expect(await within(formItem('交付目录')).findByText('写不进去：存储桶 pai-kit-nosuch 不存在（NoSuchBucket）')).toBeInTheDocument();
  });

  it('交付的地域和访问密钥: the fixed first entry 「（同数据集）」, then the others; a key that failed verification cannot be picked (third round)', async () => {
    const { user } = renderApp('/tasks/new');
    await screen.findByText('基本信息');
    await fill(user, '数据集地址', 'tos://pai-kit-datasets/lerobot/new_set');
    await pick(user, '访问密钥', 'readonly-tos');
    const optionsOf = async (label: string) => {
      await user.click(screen.getAllByRole('combobox', { name: label })[0]);
      return waitFor(() => {
        // the popup opened last is the last one in the document
        const list = [...([...document.querySelectorAll('.arco-select-popup')].at(-1)?.querySelectorAll('[role="option"]') ?? [])];
        if (!list.length) throw new Error(`no options for ${label}`);
        return list.map((o) => [o.textContent?.trim(), o.getAttribute('aria-disabled') === 'true' || o.classList.contains('arco-select-option-disabled')]);
      });
    };
    const keys = await optionsOf('交付目录访问密钥');
    // Verified keys first, the dataset's own key only as the first entry; old-ci failed verification.
    expect(keys).toEqual([['（同数据集）', false], ['prod-tos', false], ['partner-upload（未验证）', false], ['old-ci（验证失败）', true]]);
    await user.keyboard('{Escape}');
    await pick(user, '地域', '华北 2（北京） cn-beijing');
    const regions = await optionsOf('交付目录地域');
    expect(regions.map((r) => r[0])).toEqual(['（同数据集）', '华东 2（上海） cn-shanghai', '华南 1（广州） cn-guangzhou']);
  });

  it('保存为待启动 creates a created task; editing it later PATCHes with If-Match (D20)', async () => {
    const seen = record();
    const { user } = renderApp('/tasks/new?edit=task_01HXR6T3');
    expect(await screen.findByLabelText('任务名称')).toHaveValue('libero-10 抽检');
    expect(screen.getByRole('radio', { name: 'HuggingFace 缓存桶' })).toBeChecked();
    await screen.findByText(/LeRobot v2 · 379 条 episode/);
    await fill(user, '任务名称', 'libero-10 抽检 v2');
    await user.click(screen.getByRole('button', { name: '下一步：模块设置' }));
    await waitFor(() => expect(s2()).toBeVisible());
    await user.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(currentLocation()).toBe('/tasks/task_01HXR6T3'));
    const patch = seen.find((s) => s.method === 'PATCH');
    expect(patch?.headers['if-match']).toMatch(/^\d+$/);
    expect((patch?.body as Record<string, unknown>).name).toBe('libero-10 抽检 v2');
  });

  it('复制为新任务 prefills from the original and creates a new task', async () => {
    const { user } = renderApp('/tasks/new?copy=task_01HXR2D8');
    expect(await screen.findByLabelText('任务名称')).toHaveValue('droid 前 50 条质检（副本）');
    expect(screen.getByLabelText('数据集地址')).toHaveValue('tos://pai-kit-datasets/lerobot/droid_100');
    await screen.findByText(/LeRobot v3 · 100 条 episode/);
    expect(screen.getByTestId('episode-count')).toHaveTextContent('已选 50 / 100 条');
    void user;
  });
});

describe('v1 deep links on /tasks/new (07 §2.1)', () => {
  it('pre-fills source, address and region; a registered dataset brings its access key', async () => {
    renderApp('/tasks/new?dataset=tos://pai-kit-datasets/lerobot/droid_100&region=cn-beijing');
    expect(await screen.findByDisplayValue('tos://pai-kit-datasets/lerobot/droid_100')).toBeInTheDocument();
    expect(screen.getByText(/已添加的数据集「droid_100」/)).toBeInTheDocument();
    expect(await screen.findByText(/LeRobot v3 · 100 条 episode/)).toBeInTheDocument();
    // The dataset bucket is read-only for its key: not lent as the delivery directory, and said so.
    expect(await screen.findByText(/数据集所在的存储桶不能当交付目录/)).toBeInTheDocument();
    // Not the task name, not a delivery name (v1 likewise).
    expect(screen.getByLabelText('任务名称')).toHaveValue('');
  });

  it('borrows a writable dataset bucket as <bucket>/deliveries/<name>-<MMDD> after a write probe', async () => {
    const { user } = renderApp('/tasks/new?dataset=tos://team-bucket/lerobot/so101_pick&region=cn-beijing');
    await screen.findByDisplayValue('tos://team-bucket/lerobot/so101_pick');
    // Several access keys and none used before: nothing is guessed; once one is picked, the
    // dataset bucket is probed and lent.
    expect(screen.getByLabelText('交付目录')).toHaveValue('');
    await pick(user, '访问密钥', 'prod-tos');
    await waitFor(() => expect((screen.getByLabelText('交付目录') as HTMLInputElement).value).toMatch(/^tos:\/\/team-bucket\/deliveries\/so101_pick-\d{4}$/));
  });

  it('every key that is present gets a persistent note (never silent)', async () => {
    renderApp('/tasks/new?dataset=gone&region=%3Cscript%3E&endpoint=%3Cimg%3E');
    expect(await screen.findByText(/链接里的数据集在本站找不到：gone/)).toBeInTheDocument();
    expect(screen.getByText('链接里的地域参数写法不对（形如 cn-beijing），已忽略')).toBeInTheDocument();
    expect(screen.getByText('链接里的端点参数看不懂，已忽略（不影响预选）')).toBeInTheDocument();
  });

  it('source=public switches to the HuggingFace cache bucket', async () => {
    renderApp('/tasks/new?source=public&dataset=libero_10');
    await screen.findByText('基本信息');
    await waitFor(() => expect(screen.getByRole('radio', { name: 'HuggingFace 缓存桶' })).toBeChecked());
    expect(await screen.findByText(/LeRobot v2 · 379 条 episode/)).toBeInTheDocument();
    expect(screen.getByDisplayValue('不需要（匿名只读）')).toBeInTheDocument();
  });

  it('several datasets → batch mode: one configuration, POST /tasks/batch (P3)', async () => {
    const seen = record();
    const { user } = renderApp('/tasks/new?dataset=tos://pai-kit-datasets/lerobot/a_set,tos://pai-kit-datasets/lerobot/b_set&region=cn-beijing');
    expect(await screen.findByText('批量新建质检任务（2 个数据集）')).toBeInTheDocument();
    await pick(user, '访问密钥', 'readonly-tos');
    const list = await screen.findByTestId('batch-list');
    await waitFor(() => expect(within(list).getAllByText(/已识别 · 120 条/)).toHaveLength(2));
    await fill(user, '任务名称', '夜间批');
    await fill(user, '交付目录', 'tos://pai-kit-deliveries/nightly');
    await pick(user, '交付目录访问密钥', 'prod-tos');
    await user.click(screen.getByText('快速质检'));
    await user.click(screen.getByRole('button', { name: '下一步：模块设置' }));
    await user.click(screen.getByRole('button', { name: '创建并开始' }));
    await waitFor(() => expect(currentLocation()).toBe('/tasks'));
    const batch = seen.find((s) => s.path === '/tasks/batch')?.body as { items: { name: string; output: { uri: string } }[]; shared: { params: { start_now: boolean } } };
    expect(batch.items.map((i) => i.name)).toEqual(['夜间批 · a_set', '夜间批 · b_set']);
    expect(batch.items[0].output.uri).toMatch(/^tos:\/\/pai-kit-deliveries\/nightly\/a_set-\d{4}$/);
    expect(batch.shared.params.start_now).toBe(true);
  });
});
