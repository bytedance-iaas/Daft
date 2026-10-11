// One row of the Episode 流水线 card (C4 PipelineEpisode): every per-episode stage's state over both blocks
// (design doc 17 §3.3) and a provisional verdict - the policy on the findings so far. The card shows the stages in
// their groups (design doc 26 §2, D88): 数据完整性, CPU 检查, 模型检查.
import type { PipelineEpisode } from '../api/types';
import { zh } from '../locales/zh';
import { STAGE_GROUPS } from './taskView';

const copy = zh.taskDetail.pipelineEpisodes;
/** The groups of the episodes' stages, as the card lists them. */
export const ROW_GROUPS = STAGE_GROUPS.filter((g) => g.perEpisode);
/** The groups of each block: the CPU block's, then the model block's (a group of the other block may wait on it). */
const CPU_GROUPS = ['integrity', 'cpu'];
const MARK: Record<string, string> = { done: '✓', error: '✗', running: '…', waiting: '…' };

type Row = Pick<PipelineEpisode, 'stages' | 'verdict' | 'reason' | 'provisional'>;

const open = (state: string | undefined) => state === 'waiting' || state === 'running';

/** A group's state on a row: under way while any of its stages is; then an error if any had one; else done. */
function groupState(stages: Record<string, string>, members: readonly string[]): string | undefined {
  const states = members.filter((id) => id in stages).map((id) => stages[id]);
  if (!states.length) return undefined;
  if (states.some((s) => s === 'running')) return 'running';
  if (states.some(open)) return states.some((s) => s === 'done' || s === 'error') ? 'running' : 'waiting';
  return states.includes('error') ? 'error' : 'done';
}

/** Each group of the row with its state, in order (groups the row has no stage of left out). */
export function rowGroups(row: Row): { key: string; label: string; state: string }[] {
  const s = row.stages ?? {};
  return ROW_GROUPS.flatMap((g) => {
    const state = groupState(s, g.members);
    return state ? [{ key: g.key, label: zh.stageGroup[g.key], state }] : [];
  });
}

/** The groups a row waits for: the first unfinished one of each block. */
export function waitingStages(row: Row): string[] {
  const groups = rowGroups(row).filter((g) => open(g.state));
  const cpu = groups.find((g) => CPU_GROUPS.includes(g.key));
  const model = groups.find((g) => !CPU_GROUPS.includes(g.key));
  return [cpu, model].filter((g): g is NonNullable<typeof g> => Boolean(g)).map((g) => g.label);
}

export function rowFinished(row: Row): boolean {
  return Object.values(row.stages ?? {}).every((v) => v === 'done' || v === 'error');
}

/** The 当前结果 tag. */
export function rowState(row: Row): string {
  if (row.reason === 'missing') return copy.missing;
  if (!rowFinished(row)) return copy.waiting(waitingStages(row).join('、'));
  // provisional while anything runs; once the run is over the Daemon gives the current results' verdict (C4 5.3.2)
  const labels = row.provisional === false ? copy.final : copy.provisional;
  return (row.verdict && labels[row.verdict]) || copy.finished;
}

/** The 最近阶段 column: the row's groups, each marked done / error / not yet. */
export function rowStages(row: Row): string {
  const groups = rowGroups(row);
  return groups.length ? groups.map((g) => `${g.label} ${MARK[g.state] ?? ''}`.trim()).join(' · ') : '—';
}

/** The seconds a row's episode spent in a group: its stages' added up (none yet: null). */
export function groupSeconds(row: Pick<PipelineEpisode, 'stage_processing_s'>, members: readonly string[]): number | null {
  const took = (row.stage_processing_s ?? {}) as Record<string, number | undefined>;
  const times = members.map((id) => took[id]).filter((x): x is number => x !== null && x !== undefined);
  return times.length ? times.reduce((a, x) => a + x, 0) : null;
}
