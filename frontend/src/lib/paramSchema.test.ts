import { describe, expect, it } from 'vitest';
import { registry } from '../mocks/world';
import { changedParams, defaultParams, hasParams, paramFields, validateParam } from './paramSchema';

const schemaOf = (id: string) => registry.modules.find((m) => m.id === id)!.param_schema;

describe('param_schema → form fields (C1, D38)', () => {
  it('reads the two parameters of the frozen registry with titles and option names', () => {
    const sync = paramFields(schemaOf('video_action_sync'));
    expect(sync).toEqual([
      {
        key: 'sync_plots',
        title: '同步曲线证据图',
        description: '为哪些条目画画面运动与关节速度的对照曲线',
        required: false,
        kind: 'choice',
        options: [
          { value: 'flagged', label: '有标注或未对齐的' },
          { value: 'all', label: '全部' },
          { value: 'off', label: '不画' },
        ],
        default: 'flagged',
        min: undefined,
        max: undefined,
        exclusiveMin: undefined,
        maxLength: undefined,
        pattern: undefined,
      },
    ]);
    expect(paramFields(schemaOf('task_success'))[0]).toMatchObject({ key: 'evidence_frames', title: '证据帧', default: 'flagged' });
    expect(defaultParams(schemaOf('task_success'))).toEqual({ evidence_frames: 'flagged' });
  });

  it('modules without parameters produce no fields', () => {
    const without = registry.modules.filter((m) => !hasParams(m.param_schema)).map((m) => m.id);
    expect(without).toEqual(['timestamp_check', 'kinematic_limits', 'motion_quality', 'visual_quality', 'dedup', 'skill_profile']);
  });

  it('supports enums, booleans, bounded numbers, strings and required fields of future modules', () => {
    const schema = {
      type: 'object',
      required: ['frames', 'mode'],
      properties: {
        mode: { title: '模式', enum: ['fast', 'slow'] },
        frames: { title: '抽帧数', type: 'integer', minimum: 1, maximum: 64, default: 8 },
        threshold: { title: '阈值', type: 'number', exclusiveMinimum: 0 },
        verbose: { title: '详细日志', type: 'boolean', default: false },
        tag: { title: '标签', type: 'string', maxLength: 4 },
      },
    };
    const f = paramFields(schema);
    expect(f.map((x) => [x.key, x.kind, x.required])).toEqual([
      ['mode', 'choice', true],
      ['frames', 'integer', true],
      ['threshold', 'number', false],
      ['verbose', 'boolean', false],
      ['tag', 'string', false],
    ]);
    const byKey = Object.fromEntries(f.map((x) => [x.key, x]));
    expect(validateParam(byKey.mode, undefined)).toBe('请填写模式');
    expect(validateParam(byKey.mode, 'fast')).toBeNull();
    expect(validateParam(byKey.frames, 0)).toBe('抽帧数要不小于 1');
    expect(validateParam(byKey.frames, 65)).toBe('抽帧数不能大于 64');
    expect(validateParam(byKey.frames, 2.5)).toBe('抽帧数要填整数');
    expect(validateParam(byKey.threshold, 0)).toBe('阈值要大于 0');
    expect(validateParam(byKey.tag, 'abcde')).toBe('标签最多 4 个字符');
    expect(validateParam(byKey.verbose, undefined)).toBeNull();
  });

  it('reads a file parameter as an upload whose value is the handle (registry 1.5, F5.5)', () => {
    const [traj, seeds] = paramFields(schemaOf('eef_video_consistency'));
    expect(traj).toMatchObject({ key: 'trajectory_json', kind: 'upload', required: true, uploadKind: 'eef_trajectory', accept: ['.json'], maxMb: 64, default: '' });
    expect(seeds).toMatchObject({ key: 'observation_seeds', kind: 'upload', required: false, uploadKind: 'eef_observation_seeds', accept: ['.jsonl', '.json'] });
    expect(validateParam(traj, '')).toBe('请填写trajectory.json');
    expect(validateParam(traj, '/data/trajectory.json')).toBe('trajectory.json要先上传文件');
    expect(validateParam(traj, 'upload:upl-kqzmrtbwe')).toBeNull();
    expect(validateParam(traj, 'upload:upl_0123456789')).toBeNull(); // made before D45
    expect(validateParam(seeds, '')).toBeNull();
    expect(changedParams(schemaOf('eef_video_consistency'), { trajectory_json: 'upload:upl_0123456789', lag_search_s: 1.0 })).toEqual({ trajectory_json: 'upload:upl_0123456789' });
  });

  it('sends only the values that differ from the defaults', () => {
    expect(changedParams(schemaOf('video_action_sync'), { sync_plots: 'flagged' })).toEqual({});
    expect(changedParams(schemaOf('video_action_sync'), { sync_plots: 'all' })).toEqual({ sync_plots: 'all' });
    expect(changedParams(schemaOf('dedup'), { anything: 1 })).toEqual({});
  });
});
