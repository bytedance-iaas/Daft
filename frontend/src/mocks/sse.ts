// Dev-only SSE simulation (doc 03 §5): running tasks advance every 2 s so `npm run dev` shows
// live progress. Events carry `id: <epoch>-<seq>` and cumulative values, like the Daemon.
import { sse } from 'msw';
import type { StageProgress, Task } from '../api/types';
import { clock, findTask } from './db';

const EPOCH = 1;

function currentStage(t: Task): StageProgress | undefined {
  return t.progress.stages.find((s) => s.state === 'running');
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
    const timer = setInterval(() => {
      const task = findTask(id);
      if (!task || task.state !== 'running') {
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
