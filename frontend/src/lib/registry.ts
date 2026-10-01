// What the module registry (C1 2.0, design doc 17 §2) says about a module, read from its finding codes:
// whether a reject it causes may be appealed, which review lines it raises. The registry no longer
// spells these out per module; nothing here is a hard-coded module list.
import type { ModuleSpec } from '../api/types';

type WithCodes = Pick<ModuleSpec, 'codes'>;

/** A reject it causes may be appealed (D42): one of its blocking codes is appealable. */
export function isAppealable(m: WithCodes | undefined): boolean {
  return Boolean(m?.codes.some((c) => c.appealable));
}

/** The review lines its review-level codes raise (D43), in code order. */
export function reviewLinesOf(m: WithCodes | undefined): string[] {
  const out: string[] = [];
  for (const c of m?.codes ?? []) if (c.level === 'review' && c.review_line && !out.includes(c.review_line)) out.push(c.review_line);
  return out;
}

/** It raises review items or its rejects may be appealed: the adjudication page lists it. */
export function producesAdjudication(m: WithCodes | undefined): boolean {
  return reviewLinesOf(m).length > 0 || isAppealable(m);
}

/**
 * The funnel's gate of a module, which registry 2.0 dropped (hard veto, soft score, dedup removal or none).
 * The funnel keeps judging until the policy verdicts replace it, so the console keeps showing the gate it
 * judges by; this table mirrors the backend's `curation/pipeline/gates_v1.py` and goes with it (F12.3 /
 * F12.5). A module it does not know is a hard gate when one of its codes blocks by default.
 */
const FUNNEL_GATE: Record<string, string> = {
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
};
const ADVISORY = new Set(['camera_defects']);

export function funnelGate(m: Pick<ModuleSpec, 'id' | 'codes'>): string {
  return FUNNEL_GATE[m.id] ?? (m.codes.some((c) => c.level === 'blocking') ? 'hard' : 'none');
}

/** Its results never enter keep / drop / held (the funnel's advisory modules: the camera defects). */
export function isAdvisory(id: string): boolean {
  return ADVISORY.has(id);
}
