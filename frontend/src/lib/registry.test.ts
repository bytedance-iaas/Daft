import { describe, expect, it } from 'vitest';
import modulesJson from '../../../docs/contracts/modules.json';
import type { ModuleRegistry } from '../api/types';
import { isAppealable, producesAdjudication, reviewLinesOf } from './registry';

const reg = modulesJson as unknown as ModuleRegistry;
const ids = (pick: (m: ModuleRegistry['modules'][number]) => boolean) => reg.modules.filter(pick).map((m) => m.id);

describe('registry 2.0: what the finding codes say about a module', () => {
  it('appealable modules are the ones with an appealable blocking code (D42)', () => {
    expect(ids(isAppealable)).toEqual(['eef_video_consistency', 'task_success', 'dedup']);
  });

  it('review lines come from the review-level codes (D43)', () => {
    const byId = Object.fromEntries(reg.modules.map((m) => [m.id, reviewLinesOf(m)]));
    expect(byId.task_success).toEqual(['task_verdict']);        // the label conflict asks it too
    expect(byId.data_integrity).toEqual(['integrity_check']);
    expect(byId.eef_video_consistency).toEqual(['eef_check']);
    expect(byId.motion_quality).toEqual([]);
  });

  it('the adjudication page lists the modules that ask or may be appealed - as registry 1.14 did', () => {
    expect(ids(producesAdjudication)).toEqual(['data_integrity', 'eef_video_consistency', 'task_success', 'dedup']);
  });
});
