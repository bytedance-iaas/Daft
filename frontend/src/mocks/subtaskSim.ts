// Subtasks of the mock world move on by themselves in `npm run dev` (queued → running → finished),
// so the console shows 「运行中 · 重试 #2」 while one runs and the recomputed state after it (D46).
// Tests leave SUBTASK_SIM off and end a subtask by hand with finishSubtask().
import type { StageProgress, Subtask, Task, TaskState } from '../api/types';
import { clock, db, decisionsOf, findTask } from './db';

/** Dev and demo builds switch it on (browser.ts); timings in ms. */
export const SUBTASK_SIM = { enabled: false, queuedMs: 3_000, runMs: 30_000 };

const KIND_NAME: Record<Subtask['kind'], string> = { retry: '重试', resume: '继续运行', apply_adjudication: '执行裁决', reexport: '重新导出' };

/** The stages a subtask of this kind runs through, with their totals (02 §5: from the first stage it redoes). */
function plan(t: Task, s: Subtask): { id: string; total: number }[] {
  switch (s.kind) {
    case 'reexport':
      return [
        { id: 'export', total: t.summary?.passed ?? 1 },
        { id: 'verify', total: 1 },
      ];
    case 'resume': {
      const left = t.progress.stages.filter((x) => x.state !== 'succeeded' && x.state !== 'skipped' && x.state !== 'completed_with_errors');
      const ids = left.length ? left.map((x) => x.id) : ['final', 'report', 'verify'];
      const total = t.modules.find((m) => m.selected)?.episodes_total || t.summary?.total || 1;
      return ids.map((id) => ({ id, total: ['final', 'report', 'verify', 'verdict'].includes(id) ? 1 : total }));
    }
    default:
      return [
        { id: 'vlm', total: Math.max(1, s.kind === 'retry' ? t.summary?.held ?? 1 : t.pending_adjudication || 1) },
        { id: 'final', total: 1 },
        { id: 'report', total: 1 },
        { id: 'verify', total: 1 },
      ];
  }
}

/** The subtask's stages `ms` after it started running: each stage takes an equal share of runMs. */
function stagesAt(t: Task, s: Subtask, ms: number): StageProgress[] {
  const steps = plan(t, s);
  const share = SUBTASK_SIM.runMs / steps.length;
  return steps.map((p, i) => {
    const into = ms - i * share;
    if (into <= 0) return { id: p.id, state: 'pending', done: 0, total: 0, elapsed_s: null, eta_s: null };
    if (into >= share) return { id: p.id, state: 'succeeded', done: p.total, total: p.total, elapsed_s: Math.round(share / 1000), eta_s: null };
    return { id: p.id, state: 'running', done: Math.floor((into / share) * p.total), total: p.total, elapsed_s: Math.round(into / 1000), eta_s: null };
  });
}

function revisionEntries(t: Task, s: Subtask, at: number): void {
  const tl = db.timelines.get(t.id);
  if (!tl) return;
  tl.push({ at, kind: 'subtask_finished', text: `${KIND_NAME[s.kind]}结束`, state: s.state, subtask_id: s.id, revision: s.result_rev ?? null });
  if (s.result_rev) tl.push({ at: at + 1, kind: 'revision', text: `结果版本 r${String(s.result_rev).padStart(4, '0')}`, state: null, subtask_id: s.id, revision: s.result_rev });
}

/** D25: the parent's terminal state recomputed from its results once a subtask is over. */
function recompute(t: Task): TaskState {
  const failed = t.modules.some((m) => m.selected && m.state === 'failed');
  const held = t.summary?.held ?? 0;
  return failed || held > 0 ? 'completed_with_errors' : 'succeeded';
}

/** What a successful subtask of each kind does to its task, roughly as the Daemon would. */
function applyOutcome(t: Task, s: Subtask, at: number): void {
  const newRevision = () => {
    t.result_rev += 1;
    s.result_rev = t.result_rev;
  };
  switch (s.kind) {
    case 'retry': {
      const scope = s.scope.modules ?? [];
      for (const m of t.modules) {
        if (!m.selected || (scope.length && !scope.includes(m.id))) continue;
        m.episodes_error = 0;
        m.error = null;
        if (m.state === 'failed' || m.state === 'completed_with_errors') m.state = 'succeeded';
      }
      if (t.summary) {
        const passed = t.summary.passed + t.summary.held;
        t.summary = { ...t.summary, passed, held: 0, pass_rate: t.summary.total ? passed / t.summary.total : null };
      }
      newRevision();
      t.delivery_stale = true;
      t.state = recompute(t);
      break;
    }
    case 'apply_adjudication':
      for (const d of decisionsOf(t.id)) d.applied = true;
      t.pending_adjudication = 0;
      if (t.summary) t.summary = { ...t.summary, review: 0 };
      newRevision();
      t.delivery_stale = true;
      t.state = recompute(t);
      break;
    case 'reexport':
      t.delivery_stale = false;
      break;
    case 'resume': {
      t.progress = { stages: t.progress.stages.map((x) => (x.state === 'skipped' ? x : { ...x, state: 'succeeded', done: x.total, eta_s: null })) };
      for (const m of t.modules) if (m.selected && (m.state === 'pending' || m.state === 'running')) m.state = 'succeeded';
      const total = t.modules.find((m) => m.selected)?.episodes_total || t.summary?.total || 0;
      if (!t.summary && total) {
        const rejected = Math.round(total * 0.05);
        t.summary = { total, passed: total - rejected, rejected, held: 0, review: 0, pass_rate: (total - rejected) / total };
      }
      t.state_reason = null;
      newRevision();
      t.state = recompute(t);
      break;
    }
    default:
      break;
  }
  t.finished_at = at;
}

/** Ends a task's active subtask the way the Daemon does (tests call it; the dev simulation too). */
export function finishSubtask(taskId: string, state: 'succeeded' | 'failed' = 'succeeded'): void {
  const t = findTask(taskId);
  const s = t?.active_subtask;
  if (!t || !s) return;
  const at = clock();
  s.started_at = s.started_at ?? at;
  s.progress = { stages: stagesAt(t, s, SUBTASK_SIM.runMs).map((x) => (state === 'failed' && x.state !== 'succeeded' ? { ...x, state: 'failed' } : x)) };
  s.state = state;
  s.finished_at = at;
  if (state === 'succeeded') applyOutcome(t, s, at);
  else s.state_reason = '模拟的子任务失败：当前版本原样保留';
  t.active_subtask = null;
  t.updated_at = Math.max(at, t.updated_at + 1);
  revisionEntries(t, s, at);
}

/**
 * Moves every active subtask on to where the clock says it is (dev only). The GET handlers and
 * the SSE simulation call it, so polling and streaming pages see the same world.
 */
export function tickSubtasks(now: number = clock()): void {
  if (!SUBTASK_SIM.enabled) return;
  for (const t of db.tasks) {
    const s = t.active_subtask;
    if (!s) continue;
    const running = now - s.created_at - SUBTASK_SIM.queuedMs;
    if (running < 0) continue;
    if (running >= SUBTASK_SIM.runMs) {
      finishSubtask(t.id);
      continue;
    }
    if (s.state === 'queued') {
      s.state = 'running';
      s.started_at = now;
    }
    s.progress = { stages: stagesAt(t, s, running) };
  }
}
