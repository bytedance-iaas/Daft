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
