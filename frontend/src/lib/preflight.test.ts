import { describe, expect, it } from 'vitest';
import registry from '../../../docs/contracts/modules.json';
import type { ModuleRegistry, PreflightResult } from '../api/types';
import { asksModel, moduleParamFields, reasonText } from './preflight';

describe('reasonText', () => {
  it('says a Git LFS pointer upload in Chinese, with the files and the fix', () => {
    const problem =
      'Git LFS pointer files instead of the data: frames.lance/data/a.lance, meta/tasks.parquet, ' +
      'meta/episodes/chunk-000/file-000.parquet (and up to 2 more data files of pointer size) - the dataset was ' +
      'uploaded from a git clone made without Git LFS; fetch the files (`git lfs pull` in the clone, or `hf download`) and upload them again';
    const text = reasonText({ reason: 'x', reason_code: 'metadata_invalid', reason_args: { problem } });
    expect(text).toBe(
      '这些文件是 Git LFS 指针（一百多字节的占位），不是数据：frames.lance/data/a.lance, meta/tasks.parquet, ' +
        'meta/episodes/chunk-000/file-000.parquet 等（另有至多 2 个同样大小的数据文件）。' +
        '数据集是从没装 Git LFS 的 git clone 上传的，用 git lfs pull 或 hf download 拿到真文件后重新上传',
    );
    const one = reasonText({
      reason: 'x',
      reason_code: 'metadata_invalid',
      reason_args: { problem: 'Git LFS pointer files instead of the data: meta/tasks.parquet - the dataset was uploaded …' },
    });
    expect(one).toMatch(/^这些文件是 Git LFS 指针（一百多字节的占位），不是数据：meta\/tasks\.parquet。/);
  });

  it('keeps other metadata problems as they are', () => {
    expect(reasonText({ reason: 'x', reason_code: 'metadata_invalid', reason_args: { problem: 'meta/info.json is not valid JSON' } })).toBe(
      '数据集的元数据有问题：meta/info.json is not valid JSON',
    );
  });
});

describe('moduleParamFields', () => {
  const eef = (registry as unknown as ModuleRegistry).modules.find((m) => m.id === 'eef_video_consistency')!;
  const result = (entry: Record<string, unknown>) => ({ modules: [{ id: eef.id, ...entry }] }) as unknown as PreflightResult;
  const required = (r: PreflightResult | null) => moduleParamFields(eef, r).filter((f) => f.required).map((f) => f.key);

  it('makes trajectory.json required only where the platform cannot compute the trajectory (design doc 24)', () => {
    expect(required(null)).toEqual([]);                                  // optional in the schema (registry 4.3)
    expect(required(result({ availability: 'available' }))).toEqual([]);  // computed from the dataset
    expect(required(result({ availability: 'needs_input', reason_code: 'trajectory_missing', input_hint: { field: 'trajectory_json' } }))).toEqual(['trajectory_json']);
    // the robot type and the VLM backend have fields of their own; a choice group is asked for as a group
    expect(required(result({ availability: 'needs_input', input_hint: { field: 'vlm' } }))).toEqual([]);
    expect(required(result({ availability: 'needs_input', input_hint: { field: 'observation_seeds' } }))).toEqual([]);
  });
});

describe('the EEF module on screen 2 (design doc 25 §4.2-§4.3, registry 5.2)', () => {
  const reg = registry as unknown as ModuleRegistry;
  const eef = reg.modules.find((m) => m.id === 'eef_video_consistency')!;
  const keys = (entry: Record<string, unknown>) => moduleParamFields(eef, { modules: [{ id: eef.id, ...entry }] } as unknown as PreflightResult).map((f) => f.key);

  it('offers only the parameters that apply to the dataset', () => {
    const all = keys({ availability: 'available' });
    expect(all).toContain('observation_seeds');
    expect(all).not.toContain('record_mapping');                          // retired (5.1)
    const wrist = keys({ availability: 'available', applicable_params: all.filter((k) => !['observation_seeds', 'gripper_template'].includes(k)) });
    expect(wrist).not.toContain('observation_seeds');
    expect(wrist).toContain('use_vlm');
  });

  it('asks a model by its switch; a module that needs one always does', () => {
    expect(eef.vlm_switch).toBe('use_vlm');
    expect(asksModel(eef)).toBe(true);                                    // on by default
    expect(asksModel(eef, { use_vlm: false })).toBe(false);
    expect(asksModel(reg.modules.find((m) => m.id === 'task_success'), { use_vlm: false })).toBe(true);
    expect(asksModel(reg.modules.find((m) => m.id === 'timestamp_check'))).toBe(false);
  });
});
