import { describe, expect, it } from 'vitest';
import { ownIndices, parseForDisplay, pickedIndices, selectionCount, toExpr, toggleInExpr } from './episodes';
import { availability, optIn, presetSelection, reasonText, toggleModule } from './preflight';
import { DATASET_PROFILES, preflightFor, registry } from '../mocks/world';

describe('episode expressions (display only; the server validates)', () => {
  it('parses N and A-B terms and formats back to ranges', () => {
    expect([...parseForDisplay('3,10-12, 34').indices]).toEqual([3, 10, 11, 12, 34]);
    expect(toExpr([34, 3, 12, 10, 11])).toBe('3,10-12,34');
    expect(toggleInExpr('3,10-12', 11)).toBe('3,10,12');
    expect(toggleInExpr('3', 4)).toBe('3-4');
  });

  it('gives up (does not guess) on anything else', () => {
    expect(parseForDisplay('@file').ok).toBe(false);
    expect(parseForDisplay('5-2').ok).toBe(false);
    expect(toggleInExpr('@file', 1)).toBeNull();
    expect(selectionCount('explicit', undefined, '@file', 100)).toBeNull();
  });

  it('counts the selection', () => {
    expect(selectionCount('all', undefined, '', 200)).toBe(200);
    expect(selectionCount('head', 50, '', 200)).toBe(50);
    expect(selectionCount('head', 500, '', 200)).toBe(200);
    expect(selectionCount('explicit', undefined, '0-9,20', 200)).toBe(11);
    // F12.8: a dataset with its own indices (5, 9, 12, 20) counts the picks among them, not within 0..3
    const own = ownIndices('5,9,12,20');
    expect(selectionCount('explicit', undefined, '5,9', 4)).toBe(0);
    expect(selectionCount('explicit', undefined, '5,9', 4, own)).toBe(2);
    expect(selectionCount('explicit', undefined, '0,9', 4, own)).toBe(1);
    expect([...pickedIndices('9-12', 4, own).indices]).toEqual([9, 12]);
    expect(ownIndices(null)).toBeNull();
  });
});

describe('preflight → availability and reasons', () => {
  const droid200 = DATASET_PROFILES.find((p) => p.name === 'droid-200')!;
  const rrd = DATASET_PROFILES.find((p) => p.name === 'warehouse_rrd')!;
  const mcap = DATASET_PROFILES.find((p) => p.name === 'warehouse_mcap')!;
  const umi = DATASET_PROFILES.find((p) => p.name === 'umi_640_notask')!;

  it('renders reason_code in Chinese and falls back to reason for unknown codes', () => {
    const r = preflightFor(droid200, {});
    expect(reasonText(r.modules.find((m) => m.id === 'motion_quality'))).toBe('数据集缺少 observation.state 列');
    expect(reasonText(r.modules.find((m) => m.id === 'kinematic_limits'))).toContain('未读到机器人型号');
    expect(reasonText(preflightFor(umi, {}).modules.find((m) => m.id === 'kinematic_limits'))).toContain('umi_dual_handheld_gripper 不在规格库');
    expect(reasonText({ reason: 'something new happened', reason_code: 'brand_new_code' })).toBe('something new happened');
    expect(reasonText(preflightFor(rrd, {}).modules[0])).toBe('当前支持 LeRobot v2/v3、mcap 与 Lance（lerobot-lance-convert 0.3.0 起），检测到 rrd');
    // D44 / F5.13: an mcap dataset is read, the EEF module too (it asks for its trajectory.json)
    const eef = preflightFor(mcap, {}).modules.find((m) => m.id === 'eef_video_consistency');
    expect(reasonText(eef)).toBe('需要上传约定格式的 trajectory.json（勾选后在第二屏上传）');
    expect(reasonText({ reason: 'x', reason_code: 'format_unsupported_by_module', reason_args: { format: 'lance' } })).toBe('该模块只能读 LeRobot 与 mcap 数据集，不支持 lance');
    expect(reasonText({ reason: 'x', reason_code: 'format_disabled', reason_args: { format: 'lance' } })).toBe(
      '本实例关闭了 lance 格式的质检（站点配置 ingest.lance_enabled），请联系管理员',
    );
  });

  it('presets follow availability', () => {
    const r = preflightFor(droid200, { vlmBackend: 'ark-prod' });
    expect(availability(r, 'motion_quality')).toBe('unsupported');
    expect(presetSelection('full', registry, r)).toEqual(['data_integrity', 'timestamp_check', 'kinematic_limits', 'visual_quality', 'video_action_sync', 'task_success', 'dedup']);
    expect(presetSelection('quick', registry, r)).toEqual(['data_integrity', 'timestamp_check', 'kinematic_limits', 'visual_quality', 'video_action_sync', 'dedup']);
    expect(presetSelection('full', registry, preflightFor(rrd, {}))).toEqual([]);
    expect(presetSelection('quick', registry, preflightFor(mcap, {}))).toEqual(['data_integrity', 'timestamp_check', 'kinematic_limits', 'motion_quality', 'visual_quality', 'video_action_sync', 'dedup']);
  });

  it('ticking a module ticks that module only: 任务成败判定 does not drag 精确去重 along', () => {
    expect(toggleModule(['timestamp_check'], 'task_success')).toEqual(['timestamp_check', 'task_success']);
    expect(toggleModule(['dedup', 'task_success'], 'dedup')).toEqual(['task_success']);
    expect(toggleModule(['dedup'], 'dedup')).toEqual([]);
  });

  it('the EEF module is opted into by hand, never by a preset (F5.5, still so after D49)', () => {
    const r = preflightFor(droid200, { vlmBackend: 'ark-prod' });
    expect(availability(r, 'eef_video_consistency')).toBe('needs_input');
    expect(reasonText(r.modules.find((m) => m.id === 'eef_video_consistency'))).toContain('trajectory.json');
    const eef = registry.modules.find((m) => m.id === 'eef_video_consistency')!;
    expect(optIn(eef)).toBe(true);
    expect(registry.modules.filter(optIn).map((m) => m.id)).toEqual(['eef_video_consistency']);   // registry 2.0: no advisory flag; the rider is not offered
    expect(presetSelection('full', registry, r)).not.toContain('eef_video_consistency');
    // the rider (registry 1.14) is not offered at all: it runs inside task_success
    expect(presetSelection('full', registry, r)).not.toContain('camera_defects');
    expect(presetSelection('full', registry, r)).toContain('task_success');
  });
});
