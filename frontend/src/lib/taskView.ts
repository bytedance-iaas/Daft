// Pure helpers that turn task data into what the list and detail pages show. Unit tested.
import type { ModuleRegistry, StageProgress, Subtask, TaskListItem, TaskState } from '../api/types';
import { zh } from '../locales/zh';

export type Preset = 'full' | 'quick' | 'custom';

/** Capabilities that depend on the dataset; a module needing one may have been unavailable. */
const DATASET_DEPENDENT = new Set(['action', 'state', 'embodiment_profile']);

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
export function currentStage(stages: readonly StageProgress[]): StageProgress | undefined {
  const running = stages.find((s) => s.state === 'running');
  if (running) return running;
  const started = stages.filter((s) => s.state !== 'pending');
  return started[started.length - 1] ?? stages[0];
}

export function stageLabel(id: string): string {
  return zh.stage[id] ?? id;
}

export function stagePercent(s: StageProgress | undefined): number {
  if (!s || !s.total) return 0;
  return Math.min(100, Math.round((s.done / s.total) * 100));
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
  deleted_at?: number | null;
  active_subtask?: unknown;
}

export function actionsFor(t: ActionSubject): ActionPlan {
  if (t.deleted_at) return { primary: 'restore', more: [], disabled: {} };
  const disabled: ActionPlan['disabled'] = {};
  const terminal = isTerminalState(t.state);
  if (!terminal && t.state !== 'created') disabled.delete = zh.actions.deleteDisabled;
  const hasReport = Boolean(t.summary);
  const common: TaskActionKey[] = [];
  if (t.pending_adjudication > 0 && (t.state === 'succeeded' || t.state === 'completed_with_errors')) common.push('adjudicate');
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
