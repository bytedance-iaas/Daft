// Dev-only SSE simulation (doc 03 §5): running tasks advance every 2 s so `npm run dev` shows
// live progress. Events carry `id: <epoch>-<seq>` and cumulative values, like the Daemon. As the
// Daemon does, the snapshot on connect is the task's state and then its active subtask's, and a
// subtask's events carry `subtask_id` (its stages come as plain `progress` while it runs).
import { sse } from 'msw';
import type { StageProgress, Task, TaskState } from '../api/types';
import { clock, db, findTask } from './db';
import { tickSubtasks } from './subtaskSim';

const EPOCH = 1;

function currentStage(t: Task): StageProgress | undefined {
  return t.progress.stages.find((s) => s.state === 'running');
}

function runningStage(progress: unknown): StageProgress | undefined {
  const stages = (progress as { stages?: StageProgress[] } | null)?.stages ?? [];
  return stages.find((s) => s.state === 'running');
}

export const sseHandlers = [
  sse('*/events/tasks/:id', ({ client, params }) => {
    const id = String(params.id);
    let seq = 0;
    const send = (event: string, data: unknown) => client.send({ id: `${EPOCH}-${(seq += 1)}`, event, data } as never);
    const t = findTask(id);
    if (!t) {
      client.close();
      return;
    }
    send('state', { state: t.state, pause_reason: t.pause_reason ?? null, at: clock() });
    // What this stream last told about the subtask, so a change is sent once.
    let sub: { id: string; state: TaskState } | null = null;
    let taskState = t.state;
    if (t.active_subtask) {
      sub = { id: t.active_subtask.id, state: t.active_subtask.state };
      send('state', { state: sub.state, pause_reason: t.active_subtask.pause_reason ?? null, subtask_id: sub.id, at: clock() });
    }
    const timer = setInterval(() => {
      tickSubtasks();
      const task = findTask(id);
      if (!task) {
        clearInterval(timer);
        return;
      }
      const active = task.active_subtask;
      if (sub && active?.id !== sub.id) {
        // The subtask we knew ended: the task's recomputed state first, then the subtask's end.
        const ended = (db.subtasks.get(id) ?? []).find((s) => s.id === sub!.id);
        const endState = ended?.state ?? 'succeeded';
        if (task.state !== taskState) send('state', { state: task.state, pause_reason: task.pause_reason ?? null, at: clock() });
        send('state', { state: endState, subtask_id: sub.id, reason: ended?.state_reason ?? null, at: clock() });
        send('done', { state: endState, subtask_id: sub.id, failed_modules: [], reason: ended?.state_reason ?? null });
        sub = null;
        taskState = task.state;
      }
      if (active) {
        if (!sub || sub.state !== active.state) {
          sub = { id: active.id, state: active.state };
          send('state', { state: active.state, pause_reason: active.pause_reason ?? null, subtask_id: active.id, at: clock() });
        }
        const cur = runningStage(active.progress);
        if (cur) send('progress', cur);
        return;
      }
      if (task.state !== 'running') {
        clearInterval(timer);
        return;
      }
      const cur = currentStage(task);
      if (!cur) {
        clearInterval(timer);
        return;
      }
      cur.done = Math.min(cur.total, cur.done + 3);
      cur.elapsed_s = (cur.elapsed_s ?? 0) + 2;
      cur.eta_s = Math.max(0, Math.round(((cur.total - cur.done) / 3) * 2));
      task.usage = {
        ...task.usage,
        prompt_tokens: task.usage.prompt_tokens + 7800,
        completion_tokens: task.usage.completion_tokens + 260,
        reasoning_tokens: task.usage.reasoning_tokens + 150,
        cached_tokens: task.usage.cached_tokens + 3900,
        requests: task.usage.requests + 6,
      };
      send('progress', cur);
      send('usage', task.usage);
      send('log', { stage: cur.id, level: 'info', msg: `task_success episode ${cur.done}: done` });
      if (cur.done >= cur.total) {
        cur.state = 'succeeded';
        cur.eta_s = 0;
        send('progress', cur);
      }
    }, 2000);
  }),
];
