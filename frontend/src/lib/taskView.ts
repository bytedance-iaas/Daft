// Pure helpers that turn task data into what the list and detail pages show. Unit tested.
import type { ModuleRegistry, StageProgress, Subtask, TaskListItem, TaskState } from '../api/types';
import { zh } from '../locales/zh';
import { subtaskName } from './reportView';

export type Preset = 'full' | 'quick' | 'custom';

/** Capabilities that depend on the dataset or an extra input; a module needing one may have been unavailable
 * (`eef_input`: a trajectory.json the task must bring, registry 1.4). */
const DATASET_DEPENDENT = new Set(['action', 'state', 'embodiment_profile', 'eef_input']);

/**
 * Which preset a selection looks like (07 §4.1 shows the preset name under the module count).
 * C4 does not record the preset, so it is derived from the registry: 完整 = every module except
 * ones that may have been unavailable for the dataset, 快速 = the same without any VLM module.
 * Heuristic by necessity (see README «契约缺口»); it never guesses module ids, only reads `needs`.
 */
export function presetOf(selected: readonly string[], registry: ModuleRegistry | undefined): Preset {
  if (!registry || !selected.length) return 'custom';
  const sel = new Set(selected);
  const needs = (id: string) => (registry.modules.find((m) => m.id === id)?.needs ?? []) as string[];
  const isVlm = (id: string) => needs(id).includes('vlm');
  const optional = (id: string) => needs(id).some((n) => DATASET_DEPENDENT.has(n));
  const vlmSelected = registry.modules.some((m) => sel.has(m.id) && isVlm(m.id));
  for (const m of registry.modules) {
    if (sel.has(m.id)) continue;
    if (vlmSelected || !isVlm(m.id)) {
      if (!optional(m.id)) return 'custom';
    }
  }
  if (!vlmSelected) return 'quick';
  return 'full';
}

export function presetLabel(p: Preset): string {
  return zh.preset[p];
}

/** The stage to show as «current»: the running one, else the last one that started. */
export function currentStage<T extends { state: string }>(stages: readonly T[]): T | undefined {
  const running = stages.find((s) => s.state === 'running');
  if (running) return running;
  const started = stages.filter((s) => s.state !== 'pending');
  return started[started.length - 1] ?? stages[0];
}

/** A stage's own name, as the CLI output, the log filter and the execution plan use it. */
export function stageLabel(id: string): string {
  return zh.stage[id] ?? id;
}

export function stagePercent(s: StageProgress | undefined): number {
  if (!s || !s.total) return 0;
  return Math.min(100, Math.round((s.done / s.total) * 100));
}

type StageState = StageProgress['state'];

/**
 * Stages that progress views show as one (requester item 11): 终判 + 报告 read as 「报告生成」,
 * 导出 + 交付核验 as 「交付」. The log filter and the execution plan keep the raw stages, which
 * mirror the CLI output.
 */
export const STAGE_GROUPS: readonly { key: string; members: readonly string[] }[] = [
  { key: 'report_generation', members: ['final', 'report'] },
  { key: 'delivery', members: ['export', 'verify'] },
];

function groupOf(stageId: string) {
  return STAGE_GROUPS.find((g) => g.members.includes(stageId));
}

/** The name a stage goes by where progress is shown: its group's name, else its own. */
export function progressStageLabel(stageId: string): string {
  const g = groupOf(stageId);
  return g ? zh.stageGroup[g.key] : stageLabel(stageId);
}

/** One row of a progress view: a stage, or a group of stages shown as one. */
export interface StageView {
  /** The stage id, or the group key (STAGE_GROUPS). */
  key: string;
  label: string;
  /** The raw stages it stands for, in their order. */
  members: string[];
  state: StageState;
  done: number;
  total: number;
  /** For the bar, 0–100. */
  percent: number;
  elapsed_s: number | null;
  note?: string;
}

/** How far a stage is, 0–1, for its bar: a finished stage is whole, one without a total not begun. */
function barFraction(s: Pick<StageProgress, 'state' | 'done' | 'total'>): number {
  if (s.state === 'succeeded' || s.state === 'completed_with_errors') return 1;
  if (!s.total) return 0;
  return Math.min(1, s.done / s.total);
}

/**
 * The state of stages shown as one: failed > running > completed_with_errors; then, of the members
 * not skipped, all pending → pending, all succeeded → succeeded, some done and some not yet → running
 * (the group is under way between two members); all skipped → skipped.
 */
export function mergedStageState(states: readonly StageState[]): StageState {
  if (states.includes('failed')) return 'failed';
  if (states.includes('running')) return 'running';
  if (states.includes('completed_with_errors')) return 'completed_with_errors';
  const counted = states.filter((s) => s !== 'skipped');
  if (!counted.length) return 'skipped';
  if (counted.every((s) => s === 'pending')) return 'pending';
  if (counted.every((s) => s === 'succeeded')) return 'succeeded';
  return 'running';
}

function single(s: StageProgress): StageView {
  return {
    key: s.id,
    label: stageLabel(s.id),
    members: [s.id],
    state: s.state,
    done: s.done,
    total: s.total,
    percent: s.state === 'skipped' ? 0 : Math.round(barFraction(s) * 100),
    elapsed_s: s.elapsed_s ?? null,
    ...(s.note ? { note: s.note } : {}),
  };
}

/**
 * Stages as progress views show them (requester item 11): the members of a STAGE_GROUPS entry
 * become one row where the first of them stands. Counts and times add up; the bar weighs every
 * member not skipped the same, so it only moves forward; notes are joined.
 */
export function groupStages(stages: readonly StageProgress[]): StageView[] {
  const out: StageView[] = [];
  const merged = new Set<string>();
  for (const s of stages) {
    const g = groupOf(s.id);
    if (!g) {
      out.push(single(s));
      continue;
    }
    if (merged.has(g.key)) continue;
    merged.add(g.key);
    const members = stages.filter((x) => g.members.includes(x.id));
    const counted = members.filter((m) => m.state !== 'skipped');
    const times = members.map((m) => m.elapsed_s).filter((x): x is number => x !== null && x !== undefined);
    const notes = members.map((m) => m.note).filter((x): x is string => Boolean(x));
    out.push({
      key: g.key,
      label: zh.stageGroup[g.key],
      members: members.map((m) => m.id),
      state: mergedStageState(members.map((m) => m.state)),
      done: members.reduce((a, m) => a + m.done, 0),
      total: members.reduce((a, m) => a + m.total, 0),
      percent: counted.length ? Math.round((counted.reduce((a, m) => a + barFraction(m), 0) / counted.length) * 100) : 0,
      elapsed_s: times.length ? times.reduce((a, x) => a + x, 0) : null,
      ...(notes.length ? { note: notes.join('；') } : {}),
    });
  }
  return out;
}

/**
 * The whole task's progress, 0–100 (requester item 20): every stage not skipped weighs the same;
 * finished ones count whole, the current one by its bar.
 */
export function overallPercent(views: readonly StageView[]): number {
  const counted = views.filter((v) => v.state !== 'skipped');
  if (!counted.length) return 0;
  const done = counted.reduce((a, v) => a + (v.state === 'succeeded' || v.state === 'completed_with_errors' || v.state === 'failed' ? 1 : v.state === 'running' ? v.percent / 100 : 0), 0);
  return Math.min(100, Math.round((done / counted.length) * 100));
}

/** 07 §4.1: red 「N 项错误」 and gray 「N 项未运行」 on the module summary. */
export function moduleProblems(counts: Record<string, number>): { errors: number; notRun: number } {
  return {
    errors: (counts.failed ?? 0) + (counts.completed_with_errors ?? 0),
    notRun: counts.skipped ?? 0,
  };
}

/**
 * Has the delivered dataset ever been exported? Decides 「导出」 vs 「重新导出」 (07 §4.2).
 * C4 has no flag for it; the main run's export stage and finished reexport subtasks tell.
 */
export function exportedBefore(stages: readonly StageProgress[], subtasks: readonly Subtask[] = []): boolean {
  if (stages.some((s) => s.id === 'export' && s.state === 'succeeded')) return true;
  return subtasks.some((s) => s.kind === 'reexport' && s.state === 'succeeded');
}

export type TaskActionKey =
  | 'start'
  | 'edit'
  | 'copy'
  | 'delete'
  | 'restore'
  | 'pause'
  | 'resume'
  | 'stop'
  | 'cancelQueue'
  | 'retry'
  | 'continue'
  | 'report'
  | 'view'
  | 'adjudicate'
  | 'export'
  | 'purge';

const TERMINAL: TaskState[] = ['stopped', 'succeeded', 'completed_with_errors', 'failed'];

export function isTerminalState(s: TaskState): boolean {
  return TERMINAL.includes(s);
}

/**
 * The subtask a task has under way: the full object on the detail page, only its id on a list
 * row. One whose own state is final is a stale cache entry and counts as none.
 */
export function activeSubtask<T extends Subtask | string>(t: { active_subtask?: T | null }): T | null {
  const a = t.active_subtask;
  if (!a) return null;
  return typeof a === 'string' || !isTerminalState(a.state) ? a : null;
}

/** 「重试 #1」 when the subtask is in the list already, else its kind alone (「重试」). */
export function subtaskLabel(s: Subtask, subtasks: readonly Subtask[] = []): string {
  return subtasks.some((x) => x.id === s.id) ? subtaskName(subtasks, s.id) : zh.taskDetail.subtaskKind[s.kind] ?? s.kind;
}

/** A task's state as the pages show it. */
export interface DisplayState {
  state: TaskState;
  pauseReason: 'user' | 'system' | null;
  /** The subtask the state belongs to, by name where known; null for the task's own state. */
  subtask: string | null;
  /** A subtask is under way (on a list row its name is not known). */
  bySubtask: boolean;
}

/**
 * D46: while a subtask (retry, continue, apply adjudication, re-export) is under way the task
 * shows 「运行中」 with the subtask named where known; queued counts as running, a paused or
 * stopping subtask shows that. Display only: the task's own state stays what the state machine
 * says (01 §3), and once the subtask ends the recomputed state shows again.
 */
export function displayState(
  t: { state: TaskState; pause_reason?: 'user' | 'system' | null; active_subtask?: Subtask | string | null },
  subtasks: readonly Subtask[] = [],
): DisplayState {
  const a = activeSubtask(t);
  if (!a) return { state: t.state, pauseReason: t.pause_reason ?? null, subtask: null, bySubtask: false };
  if (typeof a === 'string') return { state: 'running', pauseReason: null, subtask: null, bySubtask: true };
  const state: TaskState = a.state === 'queued' || a.state === 'running' || a.state === 'created' ? 'running' : a.state;
  return { state, pauseReason: a.pause_reason ?? null, subtask: subtaskLabel(a, subtasks), bySubtask: true };
}

/** The stages of a progress document (a subtask's `progress` is a free object in C4: `{stages: [...]}`). */
export function progressStages(progress: unknown): StageProgress[] {
  const stages = progress && typeof progress === 'object' ? (progress as { stages?: unknown }).stages : undefined;
  if (!Array.isArray(stages)) return [];
  return stages.filter(
    (s): s is StageProgress =>
      Boolean(s) && typeof s === 'object' && typeof s.id === 'string' && typeof s.state === 'string' && typeof s.done === 'number' && typeof s.total === 'number',
  );
}

export interface ActionPlan {
  primary: TaskActionKey | null;
  more: TaskActionKey[];
  /** Actions shown but not clickable, with the reason as a tooltip. */
  disabled: Partial<Record<TaskActionKey, string>>;
}

/** Row and header actions by state (07 §4.1, §4.2); the server still validates every call. */
export interface ActionSubject {
  state: TaskState;
  pause_reason?: TaskListItem['pause_reason'];
  pending_adjudication: number;
  delivery_stale: boolean;
  summary?: TaskListItem['summary'];
  /** Only the detail page's Task has it; a list row has `summary`. */
  result_rev?: number;
  deleted_at?: number | null;
  active_subtask?: unknown;
}

/** Whether the task has a result (a report, a revision): what 人工裁决 and 查看报告 need. */
export function hasResult(t: Pick<ActionSubject, 'summary' | 'result_rev' | 'pending_adjudication'>): boolean {
  return Boolean(t.summary) || (t.result_rev ?? 0) > 0 || t.pending_adjudication > 0;
}

export function actionsFor(t: ActionSubject): ActionPlan {
  if (t.deleted_at) return { primary: 'restore', more: [], disabled: {} };
  const disabled: ActionPlan['disabled'] = {};
  const terminal = isTerminalState(t.state);
  if (!terminal && t.state !== 'created') disabled.delete = zh.actions.deleteDisabled;
  const hasReport = hasResult(t);
  const common: TaskActionKey[] = [];
  // D47: 人工裁决 stands as soon as there is a result, pending items or not (appeals of rejected
  // episodes are there too); the label carries the count when there are pending ones.
  if (hasReport) common.push('adjudicate');
  if (terminal && t.delivery_stale) common.push('export');
  if (terminal) common.push('copy', 'purge', 'delete');
  const busy = Boolean(t.active_subtask);
  switch (t.state) {
    case 'created':
      return { primary: 'start', more: ['edit', 'copy', 'delete'], disabled };
    case 'queued':
      return { primary: 'view', more: ['cancelQueue', 'delete'], disabled };
    case 'running':
      return { primary: 'view', more: ['pause', 'stop', 'delete'], disabled };
    case 'pausing':
    case 'stopping':
      return { primary: 'view', more: t.state === 'pausing' ? ['stop', 'delete'] : ['delete'], disabled };
    case 'paused':
      if (t.pause_reason === 'system') disabled.resume = zh.actions.systemPausedResume;
      return { primary: 'resume', more: ['view', 'stop', 'delete'], disabled };
    case 'completed_with_errors':
      if (busy) disabled.retry = zh.actions.subtaskBusy;
      return { primary: 'report', more: ['retry', ...common], disabled };
    case 'succeeded':
      return { primary: 'report', more: common, disabled };
    case 'stopped':
    case 'failed':
      if (busy) disabled.continue = zh.actions.subtaskBusy;
      return { primary: 'continue', more: ['view', ...(hasReport ? (['report'] as TaskActionKey[]) : []), ...common], disabled };
    default:
      return { primary: 'view', more: [], disabled };
  }
}
