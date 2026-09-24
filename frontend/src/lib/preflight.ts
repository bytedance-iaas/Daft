// Turning a preflight result (C2 preflight.schema) into what the form shows: module availability,
// Chinese reasons from reason_code (unknown codes fall back to `reason`), and preset selections.
import type { Availability, ModuleAvailability, ModuleRegistry, ModuleSpec, PreflightResult } from '../api/types';
import { zh } from '../locales/zh';

export function reasonText(m: Pick<ModuleAvailability, 'reason' | 'reason_code' | 'reason_args'> | undefined): string {
  if (!m) return '';
  const fn = m.reason_code ? zh.reason[m.reason_code] : undefined;
  if (fn) return fn((m.reason_args ?? {}) as Record<string, unknown>);
  return m.reason ?? '';
}

export function availabilityOf(result: PreflightResult | null | undefined, id: string): ModuleAvailability | undefined {
  return result?.modules.find((m) => m.id === id);
}

/** A module the preflight did not mention is treated as available (the server decides). */
export function availability(result: PreflightResult | null | undefined, id: string): Availability | null {
  if (!result) return null;
  if (!result.format.supported) return 'unsupported';
  return availabilityOf(result, id)?.availability ?? 'available';
}

export function needsVlm(m: ModuleSpec | undefined): boolean {
  return Boolean(m && (m.needs as string[]).includes('vlm'));
}

/** Modules opted into by hand: no preset and no 全选可用 turns them on. Advisory modules (registry 1.4,
 * `affects_dataset_verdict: false`) and the EEF module, which needs an uploaded file and, since D49,
 * judges episodes (it stays opt-in). */
export function optIn(m: ModuleSpec): boolean {
  return !m.affects_dataset_verdict || (m.needs as string[]).includes('eef_input');
}

/** The selection after ticking or unticking `id`. (F5.6 ticked the modules a module re-examined along with
 * it, reading every module id in `depends_on` that way - which also tied 技能画像 to 精确去重, whose
 * `dedup` entry only orders the stages; the EEF review is gone since D49 and so is the rule.) */
export function toggleModule(selected: string[], id: string): string[] {
  return selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id];
}

/** The modules a preset turns on for this preflight (07 §3: 完整 / 快速 / 自选). */
export function presetSelection(preset: 'full' | 'quick', reg: ModuleRegistry, result: PreflightResult | null | undefined): string[] {
  return reg.modules
    .filter((m) => !optIn(m))
    .filter((m) => availability(result, m.id) !== 'unsupported' && availability(result, m.id) !== null)
    .filter((m) => preset === 'full' || !needsVlm(m))
    .map((m) => m.id);
}

/** The input a needs_input module asks for on screen 2, if it is one screen 2 handles. */
export function embodimentHint(result: PreflightResult | null | undefined, id: string): { options: string[] } | null {
  const a = availabilityOf(result, id);
  if (a?.availability !== 'needs_input' || a.input_hint?.field !== 'embodiment_id') return null;
  return { options: a.input_hint.options ?? [] };
}
