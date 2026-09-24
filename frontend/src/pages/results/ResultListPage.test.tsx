import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { db } from '../../mocks/db';
import { recordRequests } from '../../test/record';
import { currentLocation, renderApp } from '../../test/render';

const rows = (kind: string) => [...screen.getByTestId(`${kind}-list`).querySelectorAll('tbody tr')].map((r) => r.querySelector('td a')?.textContent);
const listCalls = (seen: ReturnType<typeof recordRequests>) => seen.filter((r) => r.method === 'GET' && r.path === '/tasks');

describe('质检报告 / 人工裁决 lists (requester, third round)', () => {
  it('质检报告 lists the tasks with a result (has_result), 查看报告 first and blue', async () => {
    const seen = recordRequests();
    const { user } = renderApp('/reports');
    await screen.findByRole('heading', { name: '质检报告' });
    await waitFor(() => expect(rows('reports').length).toBeGreaterThan(0));
    expect(listCalls(seen).at(-1)?.query.get('has_result')).toBe('true');
    expect(listCalls(seen).at(-1)?.query.get('pending_adjudication')).toBeNull();
    const withResult = db.tasks.filter((t) => !t.deleted_at && t.result_rev >= 1).length;
    expect(screen.getByText(`共 ${withResult} 条`)).toBeInTheDocument();
    const first = screen.getByTestId('reports-list').querySelector('tbody tr') as HTMLElement;
    const buttons = within(first).getAllByRole('button').map((b) => [b.textContent, b.classList.contains('arco-btn-primary')]);
    expect(buttons).toEqual([
      ['查看报告', true],
      ['人工裁决（10）', false],
    ]);
    await user.click(within(first).getByRole('button', { name: '查看报告' }));
    await waitFor(() => expect(currentLocation()).toBe('/tasks/task_01HXR2D8/report'));
  });

  it('人工裁决 lists the tasks with pending items; 全部有结果的 adds the others', async () => {
    const seen = recordRequests();
    const { user } = renderApp('/adjudication');
    await screen.findByRole('heading', { name: '人工裁决' });
    await waitFor(() => expect(rows('adjudication')).toEqual(['droid 前 50 条质检', 'droid-200 抽检']));
    const last = listCalls(seen).at(-1)!;
    expect([last.query.get('has_result'), last.query.get('pending_adjudication')]).toEqual(['true', 'true']);
    const first = screen.getByTestId('adjudication-list').querySelector('tbody tr') as HTMLElement;
    expect(within(first).getByRole('button', { name: '人工裁决（10）' })).toHaveClass('arco-btn-primary');
    await user.click(screen.getByText('全部有结果的'));
    await waitFor(() => expect(currentLocation()).toBe('/adjudication?show=all'));
    await waitFor(() => expect(rows('adjudication').length).toBeGreaterThan(2));
    expect(listCalls(seen).at(-1)?.query.get('pending_adjudication')).toBeNull();
  });

  it('人工裁决 without pending items says where the appeals are', async () => {
    for (const t of db.tasks) t.pending_adjudication = 0;
    renderApp('/adjudication');
    expect(await screen.findByText('没有待裁决的任务：切到「全部有结果的」可以复议被拒的条目')).toBeInTheDocument();
  });
});
