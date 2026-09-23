// The dev simulation of subtasks (D46): queued → running with its own stages → finished, the
// parent's state recomputed; every step stays inside the contract.
import { afterEach, describe, expect, it } from 'vitest';
import type { Subtask } from '../api/types';
import { formatErrors } from '../test/contract';
import { contract } from '../test/setup';
import { db, findTask, toListItem } from './db';
import { finishSubtask, SUBTASK_SIM, tickSubtasks } from './subtaskSim';
import { MAIN_TASK } from './world';

function expectValid(name: string, value: unknown) {
  const v = contract.validator(contract.schemaRef(name));
  expect(v(value), formatErrors(v.errors)).toBe(true);
}

function startRetry(now: number): Subtask {
  const t = findTask(MAIN_TASK)!;
  const s: Subtask = { id: 'sub_sim', task_id: MAIN_TASK, kind: 'retry', scope: { modules: ['task_success'], episodes: 'errors' }, state: 'queued', state_reason: null, progress: null, created_at: now, started_at: null, finished_at: null, result_rev: null };
  db.subtasks.get(MAIN_TASK)!.push(s);
  t.active_subtask = s;
  return s;
}

afterEach(() => {
  SUBTASK_SIM.enabled = false;
});

describe('subtask simulation (dev only)', () => {
  it('does nothing unless switched on (tests end subtasks by hand)', () => {
    const s = startRetry(0);
    tickSubtasks(10 ** 12);
    expect(s.state).toBe('queued');
  });

  it('queued, then running through its own stages, then finished with the task recomputed', () => {
    SUBTASK_SIM.enabled = true;
    const t0 = 1_000_000;
    const s = startRetry(t0);
    tickSubtasks(t0 + SUBTASK_SIM.queuedMs - 1);
    expect(s.state).toBe('queued');
    tickSubtasks(t0 + SUBTASK_SIM.queuedMs + SUBTASK_SIM.runMs / 8);
    expect(s.state).toBe('running');
    expect((s.progress as { stages: { id: string; state: string }[] }).stages.map((x) => `${x.id}:${x.state}`)).toEqual(['vlm:running', 'final:pending', 'report:pending', 'verify:pending']);
    const t = findTask(MAIN_TASK)!;
    expectValid('Task', t);
    expectValid('TaskListItem', toListItem(t));
    expect(t.state).toBe('completed_with_errors');
    tickSubtasks(t0 + SUBTASK_SIM.queuedMs + SUBTASK_SIM.runMs);
    expect(s).toMatchObject({ state: 'succeeded', result_rev: 3 });
    expectValid('Subtask', s);
    // The two held episodes came back: nothing waits for a retry any more.
    expect(t).toMatchObject({ state: 'succeeded', active_subtask: null, result_rev: 3, delivery_stale: true });
    expect(t.summary).toMatchObject({ passed: 43, held: 0 });
    expect(t.modules.find((m) => m.id === 'task_success')).toMatchObject({ state: 'succeeded', episodes_error: 0 });
    const tl = db.timelines.get(MAIN_TASK)!;
    expect(tl.slice(-2).map((e) => e.kind)).toEqual(['subtask_finished', 'revision']);
    tl.forEach((e) => expectValid('TimelineEntry', e));
  });

  it('a failed subtask leaves the task as it was', () => {
    startRetry(0);
    finishSubtask(MAIN_TASK, 'failed');
    const t = findTask(MAIN_TASK)!;
    expect(t).toMatchObject({ state: 'completed_with_errors', active_subtask: null, result_rev: 2 });
    expect(t.summary).toMatchObject({ held: 2 });
  });
});
