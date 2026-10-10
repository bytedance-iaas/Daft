// Turning a preflight result (C2 preflight.schema) into what the form shows: module availability,
// Chinese reasons from reason_code (unknown codes fall back to `reason`), and preset selections.
import type { Availability, ModuleAvailability, ModuleRegistry, ModuleSpec, PreflightResult, VlmBackend } from '../api/types';
import { zh } from '../locales/zh';
import { paramFields, type ParamField } from './paramSchema';

export function reasonText(m: Pick<ModuleAvailability, 'reason' | 'reason_code' | 'reason_args'> | undefined): string {
  if (!m) return '';
  const fn = m.reason_code ? zh.reason[m.reason_code] : undefined;
  if (fn) return fn((m.reason_args ?? {}) as Record<string, unknown>);
  return m.reason ?? '';
}

/**
 * A dataset's own preflight chooses no VLM backend, so its VLM modules say vlm_backend_missing.
 * The dataset page asks the backends instead (requester, third round): one verified backend with
 * a model that can see is enough for 可用; without one the module has no usable backend. `null`
 * while the backends load.
 */
export function withVlmBackends(
  row: { availability: Availability; reason: string },
  m: Pick<ModuleAvailability, 'reason_code'> | undefined,
  backends: readonly VlmBackend[] | undefined,
): { availability: Availability; reason: string } {
  if (row.availability !== 'needs_input' || m?.reason_code !== 'vlm_backend_missing') return row;
  if (!backends) return { availability: 'needs_input', reason: zh.common.loading };
  const ready = backends.some((b) => b.verify_state === 'ok' && b.models.some((x) => x.capabilities.vision !== false));
  return ready ? { availability: 'available', reason: '' } : { availability: 'needs_input', reason: zh.reason.vlm_backend_none({}) };
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

/** Modules opted into by hand: no preset and no 全选可用 turns them on - the EEF module, which needs an
 * uploaded file and, since D49, judges episodes (it stays opt-in). The advisory camera defects ride on
 * task_success and are not offered at all (`offered`). */
export function optIn(m: ModuleSpec): boolean {
  return (m.needs as string[]).includes('eef_input');
}

/** Modules the form offers: a rider (registry 1.14, `rides_on`) is answered inside its host's model
 * requests and runs whenever the host runs, so there is nothing to tick - it is left off the form and
 * appears in the report. */
export function offered(m: ModuleSpec): boolean {
  return !m.rides_on;
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
    .filter((m) => offered(m) && !optIn(m))
    .filter((m) => availability(result, m.id) !== 'unsupported' && availability(result, m.id) !== null)
    .filter((m) => preset === 'full' || !needsVlm(m))
    .map((m) => m.id);
}

/**
 * A module's parameter fields for this dataset (C1 param_schema): a parameter its preflight asks for - needs_input
 * naming it in `input_hint.field`, outside a choice group - is required here though optional in the schema. That is
 * EEF's trajectory.json where the platform cannot compute the trajectory: optional only where it can (design doc 24).
 */
export function moduleParamFields(spec: ModuleSpec | undefined, result: PreflightResult | null | undefined): ParamField[] {
  const fields = paramFields(spec?.param_schema);
  const a = spec ? availabilityOf(result, spec.id) : undefined;
  const asked = a?.availability === 'needs_input' ? a.input_hint?.field : undefined;
  return asked ? fields.map((f) => (f.key === asked && !f.choiceGroup ? { ...f, required: true } : f)) : fields;
}

/** The input a needs_input module asks for on screen 2, if it is one screen 2 handles. */
export function embodimentHint(result: PreflightResult | null | undefined, id: string): { options: string[] } | null {
  const a = availabilityOf(result, id);
  if (a?.availability !== 'needs_input' || a.input_hint?.field !== 'embodiment_id') return null;
  return { options: a.input_hint.options ?? [] };
}
