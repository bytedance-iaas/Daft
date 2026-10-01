import { describe, expect, it } from 'vitest';
import modulesJson from '../../../docs/contracts/modules.json';
import type { ModuleRegistry } from '../api/types';
import { funnelGate, isAdvisory, isAppealable, producesAdjudication, reviewLinesOf } from './registry';

const reg = modulesJson as unknown as ModuleRegistry;
const ids = (pick: (m: ModuleRegistry['modules'][number]) => boolean) => reg.modules.filter(pick).map((m) => m.id);

describe('registry 2.0: what the finding codes say about a module', () => {
  it('appealable modules are the ones with an appealable blocking code (D42)', () => {
    expect(ids(isAppealable)).toEqual(['eef_video_consistency', 'task_success', 'dedup']);
  });

  it('review lines come from the review-level codes (D43)', () => {
    const byId = Object.fromEntries(reg.modules.map((m) => [m.id, reviewLinesOf(m)]));
    expect(byId.task_success).toEqual(['task_verdict', 'label']);
    expect(byId.data_integrity).toEqual(['integrity_check']);
    expect(byId.eef_video_consistency).toEqual(['eef_check']);
    expect(byId.skill_profile).toEqual(['label']);
    expect(byId.motion_quality).toEqual([]);
  });

  it('the adjudication page lists the modules that ask or may be appealed - as registry 1.14 did', () => {
    expect(ids(producesAdjudication)).toEqual(['data_integrity', 'eef_video_consistency', 'task_success', 'dedup', 'skill_profile']);
  });

  it('the funnel gates stay what the funnel judges by until the policy verdicts replace it', () => {
    expect(Object.fromEntries(reg.modules.map((m) => [m.id, funnelGate(m)]))).toEqual({
      data_integrity: 'hard',
      timestamp_check: 'hard',
      kinematic_limits: 'hard',
      motion_quality: 'soft',
      visual_quality: 'soft',
      video_action_sync: 'hard',
      eef_video_consistency: 'hard',
      task_success: 'hard',
      camera_defects: 'none',
      dedup: 'dedup',
      skill_profile: 'none',
    });
    expect(funnelGate({ id: 'example', codes: [{ code: 'x', item: 'TASK-5', name_zh: '示例', severity: 'high', level: 'blocking', scope_kind: 'episode', appealable: false }] })).toBe('hard');
    expect(ids((m) => isAdvisory(m.id))).toEqual(['camera_defects']);
  });
});
