import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { db } from '../../mocks/db';
import { recordRequests } from '../../test/record';
import { currentLocation, renderApp } from '../../test/render';

const rows = (kind: string) => [...screen.getByTestId(`${kind}-list`).querySelectorAll('tbody tr')].map((r) => r.querySelector('td a')?.textContent);
const listCalls = (seen: ReturnType<typeof recordRequests>) => seen.filter((r) => r.method === 'GET' && r.path === '/tasks');

describe('人工裁决 list (requester, third round; 质检报告 list dropped in the fourth)', () => {
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
