// The new-task form as plain data: defaults, validation of each screen (07 §3: every required
// field has a red * and is checked locally before 下一步 / 保存 / 开始), and the conversions to and
// from the C4 request bodies. Pure functions, unit tested.
import type {
  EpisodeSelector,
  InputRef,
  ModuleChoice,
  ModuleRegistry,
  OutputRef,
  PreflightResult,
  Task,
  TaskCreate,
  TaskParams,
  TaskPatch,
  VlmChoice,
} from '../../api/types';
import { REGION_RE } from '../../lib/deeplink';
import { parseForDisplay } from '../../lib/episodes';
import { changedParams, paramFields, validateParam } from '../../lib/paramSchema';
import { availability, embodimentHint, needsVlm } from '../../lib/preflight';
import { presetOf } from '../../lib/taskView';
import { zh } from '../../locales/zh';

export type Source = 'tos' | 'public' | 'local';
export type TimeoutKey = 'probe' | 'endstate' | 'arbitration' | 'caption' | 'llm';

export interface FormValues {
  name: string;
  note: string;
  source: Source;
  datasetUri: string;
  publicUri: string;
  region: string;
  credential: string;
  /** Registered dataset picked (D36): its id goes into the request as the input. */
  datasetId: string | null;
  outputUri: string;
  /** '' = same as the dataset */
  outputRegion: string;
  /** '' = same as the dataset */
  outputCredential: string;
  episodeMode: 'all' | 'head' | 'explicit';
  headN: number | undefined;
  expr: string;
  preset: 'full' | 'quick' | 'custom';
  modules: string[];
  vlmBackend: string;
  vlmModel: string;
  /** '' = model default (the field is not sent) */
  effort: string;
  vlmRetry: number;
  vlmHedge: boolean;
  timeouts: Record<TimeoutKey, number>;
  cpuLimit: number | undefined;
  vlmLimit: number | undefined;
  exportDataset: boolean;
  clips: boolean;
  embodiment: string;
  /** Modules skipped on screen 2 (「跳过该模块」). */
  skipped: string[];
  params: Record<string, Record<string, unknown>>;
}

export const DEFAULT_TIMEOUTS: Record<TimeoutKey, number> = { probe: 60, endstate: 60, arbitration: 60, caption: 60, llm: 120 };

export function defaultValues(): FormValues {
  return {
    name: '',
    note: '',
    source: 'tos',
    datasetUri: '',
    publicUri: '',
    region: '',
    credential: '',
    datasetId: null,
    outputUri: '',
    outputRegion: '',
    outputCredential: '',
    episodeMode: 'all',
    headN: 50,
    expr: '',
    preset: 'full',
    modules: [],
    vlmBackend: '',
    vlmModel: '',
    effort: '',
    vlmRetry: 3,
    vlmHedge: true,
    timeouts: { ...DEFAULT_TIMEOUTS },
    cpuLimit: undefined,
    vlmLimit: undefined,
    exportDataset: true,
    clips: false,
    embodiment: '',
    skipped: [],
    params: {},
  };
}

export type Errors = Partial<Record<string, string>>;

export interface ValidationContext {
  registry: ModuleRegistry | undefined;
  preflight: PreflightResult | null;
  batch: boolean;
}

const TOS_URI = /^tos:\/\/[a-z0-9][a-z0-9-]{1,61}[a-z0-9](\/.*)?$/;

/** Modules that are selected and will actually run (screen 2 may skip some). */
export function activeModules(v: FormValues): string[] {
  return v.modules.filter((id) => !v.skipped.includes(id));
}

export function usesVlm(v: FormValues, reg: ModuleRegistry | undefined): boolean {
  return activeModules(v).some((id) => needsVlm(reg?.modules.find((m) => m.id === id)));
}

export function effectiveOutputCredential(v: FormValues): string {
  return v.outputCredential || (v.source === 'public' ? '' : v.credential);
}

export function effectiveOutputRegion(v: FormValues): string {
  return v.outputRegion || v.region;
}

/** Screen 1: basic and required inputs (07 §3). */
export function validateScreen1(v: FormValues, ctx: ValidationContext): Errors {
  const e: Errors = {};
  if (!v.name.trim()) e.name = zh.errors.required(zh.taskForm.name);
  else if (v.name.length > 128) e.name = '任务名称最多 128 个字符';
  if (v.note.length > 2000) e.note = '备注最多 2000 个字符';
  if (!ctx.batch) {
    if (v.source === 'tos') {
      if (!v.datasetUri.trim()) e.datasetUri = zh.errors.required(zh.taskForm.datasetUri);
      else if (!TOS_URI.test(v.datasetUri.trim())) e.datasetUri = zh.taskForm.datasetUriBad;
      if (!v.credential) e.credential = zh.errors.requiredSelect(zh.taskForm.credential);
    } else if (v.source === 'public') {
      if (!v.publicUri) e.publicUri = zh.errors.requiredSelect(zh.taskForm.publicDataset);
    } else if (!v.datasetUri.trim()) {
      e.datasetUri = zh.errors.required(zh.taskForm.localPath);
    }
  } else if (!v.credential) {
    e.credential = zh.errors.requiredSelect(zh.taskForm.credential);
  }
  if (v.source === 'tos') {
    if (!v.region) e.region = zh.errors.requiredSelect(zh.taskForm.region);
    else if (!REGION_RE.test(v.region)) e.region = '地域写法不对（形如 cn-beijing）';
  }
  if (!v.outputUri.trim()) e.outputUri = zh.errors.required(zh.taskForm.outputUri);
  else if (!TOS_URI.test(v.outputUri.trim())) e.outputUri = zh.taskForm.outputUriBad;
  if (v.outputRegion && !REGION_RE.test(v.outputRegion)) e.outputRegion = '地域写法不对（形如 cn-beijing）';
  if (!effectiveOutputRegion(v) && v.source !== 'tos') e.outputRegion = zh.errors.requiredSelect(zh.taskForm.outputRegion);
  if (!effectiveOutputCredential(v)) e.outputCredential = zh.errors.requiredSelect(zh.taskForm.outputCredential);
  const total = ctx.preflight?.dataset?.episode_count ?? null;
  if (v.episodeMode === 'head') {
    const n = v.headN;
    if (n === undefined || n === null || Number.isNaN(n)) e.headN = zh.errors.required(zh.taskForm.headN);
    else if (!Number.isInteger(n) || n < 1 || (total !== null && !ctx.batch && n > total)) e.headN = zh.taskForm.headRange(total ?? 999999);
  }
  if (v.episodeMode === 'explicit') {
    if (!v.expr.trim()) e.expr = zh.errors.required(zh.taskForm.expr);
    else {
      const p = parseForDisplay(v.expr, null);
      if (p.ok && total !== null && !ctx.batch && [...p.indices].some((i) => i >= total)) e.expr = `超出了范围：数据集只有 ${total} 条（ep 0–${total - 1}）`;
    }
  }
  if (!v.modules.length) e.modules = zh.taskForm.noModule;
  if (usesVlm(v, ctx.registry)) {
    if (!v.vlmBackend) e.vlmBackend = zh.errors.requiredSelect(zh.taskForm.backend);
    if (!v.vlmModel) e.vlmModel = zh.errors.requiredSelect(zh.taskForm.model);
  }
  for (const k of Object.keys(v.timeouts) as TimeoutKey[]) {
    const t = v.timeouts[k];
    if (typeof t !== 'number' || !(t > 0)) e[`timeouts.${k}`] = '超时要大于 0 秒';
  }
  if (v.cpuLimit !== undefined && (!Number.isInteger(v.cpuLimit) || v.cpuLimit < 1)) e.cpuLimit = '上限要是不小于 1 的整数，留空表示不限';
  if (v.vlmLimit !== undefined && (!Number.isInteger(v.vlmLimit) || v.vlmLimit < 1)) e.vlmLimit = '上限要是不小于 1 的整数，留空表示不限';
  return e;
}

/** Screen 2: inputs the enabled modules still need, and their parameters (D38). */
export function validateScreen2(v: FormValues, ctx: ValidationContext): Errors {
  const e: Errors = {};
  const active = activeModules(v);
  if (!active.length) e.modules = zh.taskForm.noModule;
  if (active.some((id) => embodimentHint(ctx.preflight, id)) && !v.embodiment) e.embodiment = zh.errors.requiredSelect(zh.taskForm.embodiment);
  for (const id of active) {
    const spec = ctx.registry?.modules.find((m) => m.id === id);
    for (const f of paramFields(spec?.param_schema)) {
      const problem = validateParam(f, v.params[id]?.[f.key] ?? f.default);
      if (problem) e[`params.${id}.${f.key}`] = problem;
    }
  }
  return e;
}

// ------------------------------------------------------------------ to the contract

export function inputRef(v: FormValues): InputRef {
  if (v.source === 'public') return { source: 'public', uri: v.publicUri };
  if (v.source === 'local') return { source: 'local', uri: v.datasetUri.trim() };
  return { source: 'tos', uri: v.datasetUri.trim().replace(/\/+$/, ''), region: v.region, credential: v.credential };
}

export function outputRef(v: FormValues, uri: string = v.outputUri.trim()): OutputRef {
  const region = effectiveOutputRegion(v);
  return { uri: uri.replace(/\/+$/, ''), ...(region ? { region } : {}), credential: effectiveOutputCredential(v) };
}

export function episodes(v: FormValues): EpisodeSelector {
  if (v.episodeMode === 'head') return { mode: 'head', n: v.headN ?? 1 };
  if (v.episodeMode === 'explicit') return { mode: 'explicit', expr: v.expr.trim() };
  return { mode: 'all' };
}

export function moduleChoices(v: FormValues, reg: ModuleRegistry | undefined): ModuleChoice[] {
  const order = reg?.modules.map((m) => m.id) ?? v.modules;
  return order
    .filter((id) => activeModules(v).includes(id))
    .map((id) => {
      const params = changedParams(reg?.modules.find((m) => m.id === id)?.param_schema, v.params[id]);
      return Object.keys(params).length ? { id, params } : id;
    });
}

export function vlmChoice(v: FormValues, reg: ModuleRegistry | undefined): VlmChoice | undefined {
  if (!usesVlm(v, reg)) return undefined;
  return { backend: v.vlmBackend, model: v.vlmModel, reasoning_effort: (v.effort || null) as VlmChoice['reasoning_effort'] };
}

export function taskParams(v: FormValues, reg: ModuleRegistry | undefined, startNow: boolean): TaskParams {
  const limits = {
    ...(v.cpuLimit ? { cpu_concurrency: v.cpuLimit } : {}),
    ...(v.vlmLimit ? { vlm_parallelism: v.vlmLimit } : {}),
  };
  return {
    start_now: startNow,
    export: v.exportDataset,
    clips: v.clips,
    ...(usesVlm(v, reg) ? { vlm_retry: v.vlmRetry, vlm_hedge: v.vlmHedge, vlm_timeouts_s: { ...v.timeouts } } : {}),
    ...(Object.keys(limits).length ? { limits } : {}),
  };
}

export function embodimentOf(v: FormValues, preflight: PreflightResult | null): string | undefined {
  const needed = activeModules(v).some((id) => embodimentHint(preflight, id));
  return needed && v.embodiment ? v.embodiment : undefined;
}

export function toTaskCreate(v: FormValues, reg: ModuleRegistry | undefined, preflight: PreflightResult | null, preflightId: string, datasetId: string | null, startNow: boolean): TaskCreate {
  const vlm = vlmChoice(v, reg);
  const embodiment = embodimentOf(v, preflight);
  return {
    name: v.name.trim(),
    ...(v.note.trim() ? { note: v.note.trim() } : {}),
    input: datasetId ? { dataset_id: datasetId } : inputRef(v),
    output: outputRef(v),
    preflight_id: preflightId,
    episodes: episodes(v),
    modules: moduleChoices(v, reg),
    ...(embodiment ? { embodiment_id: embodiment } : {}),
    ...(vlm ? { vlm } : {}),
    params: taskParams(v, reg, startNow),
  };
}

/** PATCH for a created task: every field may change (D20). */
export function toTaskPatch(v: FormValues, reg: ModuleRegistry | undefined, preflight: PreflightResult | null, preflightId: string, datasetId: string | null): TaskPatch {
  const c = toTaskCreate(v, reg, preflight, preflightId, datasetId, false);
  const params = { ...c.params };
  delete params.start_now;
  const patch: TaskPatch = { ...c, params, note: v.note.trim(), embodiment_id: embodimentOf(v, preflight) ?? null };
  return patch;
}

// ------------------------------------------------------------------ from a task (edit / copy)

export function fromTask(t: Task, reg: ModuleRegistry | undefined): FormValues {
  const v = defaultValues();
  const selected = t.modules.filter((m) => m.selected).map((m) => m.id);
  const p = t.params ?? {};
  return {
    ...v,
    name: t.name,
    note: t.note ?? '',
    source: t.input.source,
    datasetUri: t.input.source === 'public' ? '' : t.input.uri,
    publicUri: t.input.source === 'public' ? t.input.uri : '',
    region: t.input.region ?? '',
    credential: t.input.credential ?? '',
    datasetId: t.dataset_id ?? null,
    outputUri: t.output.uri,
    outputRegion: t.output.region && t.output.region !== t.input.region ? t.output.region : '',
    // C4 1.2: a deleted key comes back as null; the copy then asks for a key again.
    outputCredential: t.output.credential !== t.input.credential ? t.output.credential ?? '' : '',
    episodeMode: t.episodes.mode,
    headN: t.episodes.mode === 'head' ? t.episodes.n : v.headN,
    expr: t.episodes.mode === 'explicit' ? t.episodes.expr : '',
    preset: presetOf(selected, reg),
    modules: selected,
    vlmBackend: t.vlm?.backend ?? '',
    vlmModel: t.vlm?.model ?? '',
    effort: t.vlm?.reasoning_effort ?? '',
    vlmRetry: p.vlm_retry ?? 3,
    vlmHedge: p.vlm_hedge ?? true,
    timeouts: { ...DEFAULT_TIMEOUTS, ...(p.vlm_timeouts_s ?? {}) } as Record<TimeoutKey, number>,
    cpuLimit: p.limits?.cpu_concurrency,
    vlmLimit: p.limits?.vlm_parallelism,
    exportDataset: p.export ?? true,
    clips: p.clips ?? false,
    embodiment: t.embodiment_id ?? '',
  };
}

/**
 * The preflight request for the current inputs, or null while they are incomplete. It carries the
 * VLM backend (so VLM modules resolve once one is picked) but not the robot type: the kinematic
 * module must stay needs_input so screen 2 keeps asking for it (the type goes with the task).
 */
export function preflightRequest(v: FormValues): { input: InputRef | { dataset_id: string }; vlm_backend?: string } | null {
  let input: InputRef | { dataset_id: string };
  if (v.datasetId) input = { dataset_id: v.datasetId };
  else if (v.source === 'tos') {
    const uri = v.datasetUri.trim().replace(/\/+$/, '');
    if (!TOS_URI.test(uri) || !REGION_RE.test(v.region) || !v.credential || uri.split('/').length < 4) return null;
    input = { source: 'tos', uri, region: v.region, credential: v.credential };
  } else if (v.source === 'public') {
    if (!v.publicUri) return null;
    input = { source: 'public', uri: v.publicUri };
  } else {
    if (!v.datasetUri.trim()) return null;
    input = { source: 'local', uri: v.datasetUri.trim() };
  }
  return { input, ...(v.vlmBackend ? { vlm_backend: v.vlmBackend } : {}) };
}

/** The dataset's directory name (last path segment), for the delivery directory default. */
export function datasetDirName(v: FormValues): string {
  const uri = v.source === 'public' ? v.publicUri : v.datasetUri;
  return uri.trim().replace(/\/+$/, '').split('/').pop() ?? '';
}

export function isTosUri(s: string): boolean {
  return TOS_URI.test(s.trim());
}

/** Whether a module can be ticked at all for this preflight. */
export function selectable(preflight: PreflightResult | null, id: string): boolean {
  const a = availability(preflight, id);
  return a === 'available' || a === 'needs_input';
}
