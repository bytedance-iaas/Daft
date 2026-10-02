// One row of the Episode 流水线 card (C4 PipelineEpisode). A two-block run (design doc 17 §3.3) gives every
// per-episode stage's state over both blocks and a provisional verdict - the policy on the findings so far; a
// funnel run (a task made before) gives its position in the funnel.
import type { PipelineEpisode } from '../api/types';
import { zh } from '../locales/zh';

const copy = zh.taskDetail.pipelineEpisodes;
const STAGE: Record<string, string> = copy.stage;
/** The per-episode stages of the two blocks, as the card lists them. */
const CPU_STAGES = ['integrity', 'numeric', 'frame'];
const ORDER = [...CPU_STAGES, 'vlm'];
const MARK: Record<string, string> = { done: '✓', error: '✗', running: '…', waiting: '…' };

type Row = Pick<PipelineEpisode, 'stages' | 'next_stage' | 'last_stage' | 'verdict' | 'reason' | 'provisional'>;

const open = (state: string | undefined) => state === 'waiting' || state === 'running';

/** The stages a two-block row waits for: the first unfinished one of each block. */
export function waitingStages(row: Row): string[] {
  const s = row.stages ?? {};
  const cpu = CPU_STAGES.find((id) => open(s[id]));
  return [cpu, open(s.vlm) ? 'vlm' : undefined].filter((id): id is string => Boolean(id));
}

export function rowFinished(row: Row): boolean {
  if (row.stages) return Object.values(row.stages).every((v) => v === 'done' || v === 'error');
  return row.next_stage === 'done';
}

/** The 当前结果 tag. */
export function rowState(row: Row): string {
  if (row.reason === 'missing') return copy.missing;
  if (row.stages) {
    if (!rowFinished(row)) return copy.waiting(waitingStages(row).map((id) => STAGE[id] ?? id).join('、'));
    const labels = row.provisional ? copy.provisional : (copy.verdict as Record<string, string>);
    return (row.verdict && labels[row.verdict]) || copy.finished;
  }
  if (row.next_stage === 'done') return (copy.verdict as Record<string, string>)[row.verdict ?? ''] ?? copy.finished;
  return copy.waiting(STAGE[row.next_stage ?? ''] ?? row.next_stage ?? '');
}

/** The 最近阶段 column: a funnel row's last stage; a two-block row's stages, each marked done / error / not yet. */
export function rowStages(row: Row): string {
  const s = row.stages;
  if (!s) return STAGE[row.last_stage ?? ''] ?? row.last_stage ?? '—';
  return ORDER.filter((id) => id in s).map((id) => `${STAGE[id] ?? id} ${MARK[s[id]] ?? ''}`.trim()).join(' · ');
}
