// Behaviour of the mock world that pages rely on, beyond the shapes contract.test.ts checks.
import { describe, expect, it } from 'vitest';
import type { TaskListPage } from '../api/types';
import { MAIN_TASK } from './world';

const API = 'http://localhost/api/v1';
const JSON_HEADERS = { 'content-type': 'application/json' };

async function running(): Promise<TaskListPage> {
  return (await fetch(`${API}/tasks?state=running&page_size=100`)).json() as Promise<TaskListPage>;
}

describe('the task list filter (C4 1.10.0)', () => {
  it('state=running also lists a finished task while its subtask is queued or running (D46)', async () => {
    const before = await running();
    expect(before.items.map((t) => t.id)).not.toContain(MAIN_TASK);
    const retry = await fetch(`${API}/tasks/${MAIN_TASK}/retry`, { method: 'POST', headers: JSON_HEADERS, body: '{}' });
    expect(retry.status).toBe(202);
    const after = await running();
    expect(after.total).toBe(before.total + 1);
    const item = after.items.find((t) => t.id === MAIN_TASK);
    expect(item?.state).toBe('completed_with_errors'); // display only: the task's state stays
    expect(item?.active_subtask).toBeTruthy();
    const errors = (await (await fetch(`${API}/tasks?state=completed_with_errors`)).json()) as TaskListPage;
    expect(errors.items.map((t) => t.id)).toContain(MAIN_TASK);
  });
});
