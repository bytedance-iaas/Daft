import { describe, expect, it } from 'vitest';
import { DATASET_PROFILES, preflightFor, registry } from '../../mocks/world';
import { pickDefault } from './initial';
import { defaultValues, preflightRequest, toTaskCreate, toTaskPatch, validateScreen1, validateScreen2, type FormValues } from './formModel';

const droid200 = preflightFor(DATASET_PROFILES.find((p) => p.name === 'droid-200')!, { vlmBackend: 'ark-prod' });
const ctx = { registry, preflight: droid200, batch: false };

function filled(patch: Partial<FormValues> = {}): FormValues {
  return {
    ...defaultValues(),
    name: 'x',
    datasetUri: 'tos://pai-kit-datasets/lerobot/droid-200',
    region: 'cn-beijing',
    credential: 'readonly-tos',
    outputUri: 'tos://pai-kit-deliveries/x',
    outputCredential: 'prod-tos',
    modules: ['timestamp_check', 'task_success'],
    vlmBackend: 'ark-prod',
    vlmModel: 'doubao-seed-2-0-pro-260215',
    ...patch,
  };
}

describe('validation of the two screens (every required field)', () => {
  it('screen 1', () => {
    expect(validateScreen1(filled(), ctx)).toEqual({});
    const e = validateScreen1(defaultValues(), ctx);
    expect(Object.keys(e).sort()).toEqual(['credential', 'datasetUri', 'modules', 'name', 'outputCredential', 'outputUri', 'region'].sort());
    expect(validateScreen1(filled({ datasetUri: 'oss://x/y' }), ctx).datasetUri).toBe('地址要以 tos:// 开头，形如 tos://存储桶名/前缀/数据集名');
    expect(validateScreen1(filled({ episodeMode: 'head', headN: 500 }), ctx).headN).toBe('请输入 1 到 200 之间的整数');
    expect(validateScreen1(filled({ episodeMode: 'explicit', expr: '' }), ctx).expr).toBe('请填写按编号输入');
    expect(validateScreen1(filled({ episodeMode: 'explicit', expr: '3,199-205' }), ctx).expr).toContain('超出了范围');
    expect(validateScreen1(filled({ vlmBackend: '', vlmModel: '' }), ctx)).toMatchObject({ vlmBackend: '请选择 VLM 后端', vlmModel: '请选择模型' });
    expect(validateScreen1(filled({ modules: ['timestamp_check'], vlmBackend: '' }), ctx).vlmBackend).toBeUndefined();
    expect(validateScreen1(filled({ source: 'public', publicUri: '', credential: '', region: '' }), ctx).publicUri).toBe('请选择数据集');
  });

  it('screen 2: robot type when the kinematic module is on, parameters by schema', () => {
    const v = filled({ modules: ['kinematic_limits', 'video_action_sync'] });
    expect(validateScreen2(v, ctx)).toEqual({ embodiment: '请选择机器人型号' });
    expect(validateScreen2({ ...v, embodiment: 'franka' }, ctx)).toEqual({});
    expect(validateScreen2({ ...v, skipped: ['kinematic_limits'] }, ctx)).toEqual({});
    expect(validateScreen2({ ...v, embodiment: 'franka', params: { video_action_sync: { sync_plots: 'sometimes' } } }, ctx)).toEqual({
      'params.video_action_sync.sync_plots': '同步曲线证据图的取值不在可选范围内',
    });
  });
});

describe('conversions to the contract', () => {
  it('builds a TaskCreate: skipped modules out, default params omitted, 模型默认 = null', () => {
    const v = filled({ modules: ['timestamp_check', 'kinematic_limits', 'video_action_sync', 'task_success'], skipped: ['kinematic_limits'], params: { video_action_sync: { sync_plots: 'flagged' }, task_success: { evidence_frames: 'all' } }, cpuLimit: 4 });
    const c = toTaskCreate(v, registry, droid200, 'pf_1', 'ds_droid200', false);
    expect(c).toMatchObject({
      name: 'x',
      input: { dataset_id: 'ds_droid200' },
      output: { uri: 'tos://pai-kit-deliveries/x', region: 'cn-beijing', credential: 'prod-tos' },
      preflight_id: 'pf_1',
      episodes: { mode: 'all' },
      modules: ['timestamp_check', 'video_action_sync', { id: 'task_success', params: { evidence_frames: 'all' } }],
      vlm: { backend: 'ark-prod', model: 'doubao-seed-2-0-pro-260215', reasoning_effort: null },
      params: { start_now: false, export: true, clips: false, vlm_retry: 3, vlm_hedge: true, limits: { cpu_concurrency: 4 } },
    });
    expect(c).not.toHaveProperty('embodiment_id');
    const withRobot = toTaskCreate({ ...v, skipped: [], embodiment: 'franka' }, registry, droid200, 'pf_1', null, true);
    expect(withRobot.embodiment_id).toBe('franka');
    expect(withRobot.input).toEqual({ source: 'tos', uri: 'tos://pai-kit-datasets/lerobot/droid-200', region: 'cn-beijing', credential: 'readonly-tos' });
    const patch = toTaskPatch(v, registry, droid200, 'pf_2', 'ds_droid200');
    expect(patch.params).not.toHaveProperty('start_now');
    expect(patch.embodiment_id).toBeNull();
  });

  it('asks for a preflight only when the inputs are complete, and never with the robot type', () => {
    expect(preflightRequest(defaultValues())).toBeNull();
    expect(preflightRequest(filled({ datasetUri: 'tos://bkt' }))).toBeNull();
    expect(preflightRequest(filled({ embodiment: 'franka' }))).toEqual({
      input: { source: 'tos', uri: 'tos://pai-kit-datasets/lerobot/droid-200', region: 'cn-beijing', credential: 'readonly-tos' },
      vlm_backend: 'ark-prod',
    });
    expect(preflightRequest(filled({ datasetId: 'ds_1' }))?.input).toEqual({ dataset_id: 'ds_1' });
  });

  it('auto-selection: one → it, several → the last used, none → nothing (07 §2.1)', () => {
    expect(pickDefault(['a'], undefined)).toBe('a');
    expect(pickDefault(['a', 'b'], 'b')).toBe('b');
    expect(pickDefault(['a', 'b'], 'c')).toBe('');
    expect(pickDefault([], 'a')).toBe('');
  });
});
