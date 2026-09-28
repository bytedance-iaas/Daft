import { describe, expect, it } from 'vitest';
import { DATASET_PROFILES, preflightFor } from '../mocks/world';
import { paramDraft } from './paramDraft';

const EEF = 'eef_video_consistency';
const profile = (name: string) => DATASET_PROFILES.find((p) => p.name === name)!;

describe('paramDraft (design doc 12 §8.7, D-E17)', () => {
  it('reads what a drafted record mapping reads, what it assumed and what it left out', () => {
    const d = paramDraft(preflightFor(profile('eef_ds2_lr3'), {}), EEF, 'record_mapping')!;
    expect(d.document).toMatchObject({ schema_version: 'eef-mapping/1.1' });
    expect(d.parts).toEqual([
      '位姿：observation.state.cartesian_position · xyz_rpy_xyz_extrinsic · m / rad · 参考点未声明',
      '关节角：observation.state.joint_position · franka_panda · rad',
    ]);
    expect(d.assumptions[0]).toBe('位姿：用的是实测列 observation.state.cartesian_position，没用指令列 action.cartesian_position、action.original（指令一般领先实测）');
    expect(d.assumptions).toContain('位姿：roll / pitch / yaw 按外旋 xyz 解读');
    expect(d.assumptions).toContain('关节角：robot_type「Franka」→ 内置 franka_panda 正解');
    expect(d.assumptions).toContain('关节角：数据集没写单位，按惯例取角度 rad');
    expect(d.assumptions.find((x) => x.includes('正解得到法兰'))).toBe('关节角：关节角正解得到法兰 panda_link8：上传轨迹的 eef_frame 是 panda_link8、panda_hand、panda_hand_tcp 之一时恒定差照常判，否则只报告');
    expect(d.notDrafted).toEqual([]);
  });

  it('says why nothing was drafted, and is null for a module or parameter without a draft', () => {
    const none = paramDraft(preflightFor(profile('pusht'), {}), EEF, 'record_mapping')!;
    expect(none.document).toBeNull();
    expect(none.notDrafted).toEqual([
      '位姿：没有带 x / y / z 与 roll / pitch / yaw（或四元数）分量名的实测位姿列',
      '关节角：robot_type「pusht」没有内置正解（现有 franka_panda、franka_fr3）',
    ]);
    expect(paramDraft(preflightFor(profile('warehouse_mcap'), {}), EEF, 'record_mapping')!.notDrafted).toEqual(['mcap 数据集暂不起草，请上传映射文件']);
    expect(paramDraft(preflightFor(profile('eef_ds2_lr3'), {}), EEF, 'trajectory_json')).toBeNull();
    expect(paramDraft(preflightFor(profile('eef_ds2_lr3'), {}), 'timestamp_check', 'record_mapping')).toBeNull();
    expect(paramDraft(null, EEF, 'record_mapping')).toBeNull();
  });

  it('a slice and an unknown code read plainly', async () => {
    const { paramDraft: read } = await import('./paramDraft');
    const d = read(
      {
        schema_version: '1.0',
        format: { kind: 'lerobot', version: 'v3', supported: true, detail: '' },
        validation: [],
        dataset: null,
        modules: [
          {
            id: EEF,
            availability: 'available',
            drafts: {
              record_mapping: {
                document: { schema_version: 'eef-mapping/1.1', record: { joints: { key: 'observation.state', slice: [0, 7], units: 'rad', robot: 'franka_fr3', reference_frame: '@upload' } } },
                assumptions: [{ code: 'slice_from_names', source: 'joints', args: { key: 'observation.state', slice: [0, 7] } }, { code: 'something_new' }],
                not_drafted: [],
              },
            },
          },
        ],
        meta_fingerprint: 'sha256:0',
        warnings: [],
      },
      EEF,
      'record_mapping',
    )!;
    expect(d.parts).toEqual(['关节角：observation.state[0:7] · franka_fr3 · rad']);
    expect(d.assumptions).toEqual(['关节角：取 observation.state 的一段分量 [0:7]', 'something_new']);
  });
});
