import { describe, expect, it } from 'vitest';
import { parseForDisplay, selectionCount, toExpr, toggleInExpr } from './episodes';
import { availability, presetSelection, reasonText } from './preflight';
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
  });
});

describe('preflight → availability and reasons', () => {
  const droid200 = DATASET_PROFILES.find((p) => p.name === 'droid-200')!;
  const mcap = DATASET_PROFILES.find((p) => p.name === 'warehouse_mcap')!;
  const umi = DATASET_PROFILES.find((p) => p.name === 'umi_640_notask')!;

  it('renders reason_code in Chinese and falls back to reason for unknown codes', () => {
    const r = preflightFor(droid200, {});
    expect(reasonText(r.modules.find((m) => m.id === 'motion_quality'))).toBe('数据集缺少 observation.state 列');
    expect(reasonText(r.modules.find((m) => m.id === 'kinematic_limits'))).toContain('未读到机器人型号');
    expect(reasonText(preflightFor(umi, {}).modules.find((m) => m.id === 'kinematic_limits'))).toContain('umi_dual_handheld_gripper 不在规格库');
    expect(reasonText({ reason: 'something new happened', reason_code: 'brand_new_code' })).toBe('something new happened');
    expect(reasonText(preflightFor(mcap, {}).modules[0])).toBe('当前版本仅支持 LeRobot v2/v3，检测到 mcap');
  });

  it('presets follow availability', () => {
    const r = preflightFor(droid200, { vlmBackend: 'ark-prod' });
    expect(availability(r, 'motion_quality')).toBe('unsupported');
    expect(presetSelection('full', registry, r)).toEqual(['timestamp_check', 'kinematic_limits', 'visual_quality', 'video_action_sync', 'task_success', 'dedup', 'skill_profile']);
    expect(presetSelection('quick', registry, r)).toEqual(['timestamp_check', 'kinematic_limits', 'visual_quality', 'video_action_sync', 'dedup']);
    expect(presetSelection('full', registry, preflightFor(mcap, {}))).toEqual([]);
  });
});
