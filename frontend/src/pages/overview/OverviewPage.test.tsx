import { screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import type { Overview } from '../../api/types';
import { readPrefs } from '../../lib/prefs';
import { overviewBuckets } from '../../mocks/handlers';
import { server } from '../../mocks/server';
import { pick } from '../../test/arco';
import { recordRequests } from '../../test/record';
import { currentLocation, renderApp } from '../../test/render';

const chart = () => screen.getAllByTestId('chart')[0].getAttribute('aria-label') ?? '';
const overviewCalls = (seen: ReturnType<typeof recordRequests>) => seen.filter((r) => r.method === 'GET' && r.path === '/overview');

describe('概览 (07 §4.3, D36; F6.1)', () => {
  it('has two cards side by side: 运行情况 and the period, nothing else', async () => {
    renderApp('/overview');
    expect(await screen.findByTestId('run-running')).toHaveTextContent('1');
    expect(screen.getByTestId('run-queued')).toHaveTextContent('1');
    expect(screen.getByTestId('run-paused')).toHaveTextContent('2');
    const active = screen.getByTestId('active-tasks');
    expect(within(active).getByRole('link', { name: 'umi_640 全量质检' })).toBeInTheDocument();
    expect(active).toHaveTextContent('VLM 档 410 / 631');
    expect(document.querySelectorAll('.overview-cards > *')).toHaveLength(2);
    expect(screen.getByText('运行情况')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: '时间范围' }).closest('.arco-select')).toHaveTextContent('近 7 天');
    for (const id of ['recent-finished', 'recent-episodes', 'recent-pass', 'recent-tokens']) expect(screen.getByTestId(id)).toBeInTheDocument();
    expect(screen.getByTestId('tokens-chart-title')).toHaveTextContent('每天的 Token 消耗');
    expect(chart()).toMatch(/^\d{2}-\d{2} \d+(，\d{2}-\d{2} \d+){6}$/); // seven days
    // 待处理 and 数据集 are gone (the requester's item 8)
    expect(screen.queryByText('待处理')).toBeNull();
    expect(screen.queryByTestId('todo-errors')).toBeNull();
    expect(screen.queryByTestId('ds-total')).toBeNull();
    expect(screen.getByText('有未结束的任务，每 5 秒自动刷新')).toBeInTheDocument();
  });

  it('switching the period asks for ?days= and the browser remembers it', async () => {
    const seen = recordRequests();
    const first = renderApp('/overview');
    await screen.findByTestId('run-running');
    expect(overviewCalls(seen).map((r) => r.query.get('days'))).toEqual(['7']);
    await pick(first.user, '时间范围', '近 3 月');
    await waitFor(() => expect(screen.getByTestId('tokens-chart-title')).toHaveTextContent('每周的 Token 消耗'));
    expect(overviewCalls(seen).map((r) => r.query.get('days'))).toContain('90');
    const weeks = overviewBuckets(Date.now(), 90).spans.map((s) => s.label);
    expect(weeks).toHaveLength(13);
    expect(chart().split('，').map((x) => x.replace(/ \d+$/, ''))).toEqual(weeks);
    expect(readPrefs().overviewDays).toBe(90);

    await pick(first.user, '时间范围', '近 1 年');
    await waitFor(() => expect(screen.getByTestId('tokens-chart-title')).toHaveTextContent('每月的 Token 消耗'));
    expect(chart().split('，')).toHaveLength(12);
    first.unmount();

    seen.length = 0;
    renderApp('/overview'); // a new visit starts where the last one was
    await screen.findByTestId('run-running');
    expect(overviewCalls(seen).map((r) => r.query.get('days'))).toEqual(['365']);
    expect(screen.getByRole('combobox', { name: '时间范围' }).closest('.arco-select')).toHaveTextContent('近 1 年');
  });

  it('pages the running tasks when they do not fit next to the period card', async () => {
    const now = Date.now();
    const { spans } = overviewBuckets(now, 7);
    const active = Array.from({ length: 7 }, (_, i) => ({
      task: { id: `task-aaaaaaab${String.fromCharCode(97 + i)}`, name: `批量任务 ${i + 1}`, state: 'running' as const, created_at: now - i },
      stage: 'vlm',
      done: i,
      total: 10,
    }));
    const body: Overview = {
      todo: { error_tasks: 0, adjudication: { tasks: 0, episodes: 0 }, delivery_pending: 0, datasets_changed: 0, credentials_failed: 0, backends_failed: 0 },
      running: { running: 7, queued: 0, paused: 0, active },
      recent: { days: 7, bucket: 'day', since: spans[0].start, tasks_finished: 0, episodes_checked: 0, pass_rate: null, tokens_per_bucket: spans.map((s) => ({ ...s, tokens: 0 })) },
      datasets: { total: 0, changed: 0 },
      generated_at: now,
    };
    server.use(http.get('*/api/v1/overview', () => HttpResponse.json(body)));
    // jsdom has no layout: the list area is 200 px tall, so 3 rows of 56 px fit above the pager.
    vi.spyOn(Element.prototype, 'clientHeight', 'get').mockImplementation(function (this: Element) {
      return this.classList.contains('overview-active') ? 200 : 0;
    });
    const { user } = renderApp('/overview');
    const rows = () => screen.getAllByTestId('active-task').map((r) => within(r).getByRole('link').textContent);
    await waitFor(() => expect(rows()).toEqual(['批量任务 1', '批量任务 2', '批量任务 3']));
    const pager = document.querySelector('.overview-pager') as HTMLElement;
    expect(pager).not.toBeNull();
    await user.click(pager.querySelector('.arco-pagination-item-next') as HTMLElement);
    await waitFor(() => expect(rows()).toEqual(['批量任务 4', '批量任务 5', '批量任务 6']));
    await user.click(pager.querySelector('.arco-pagination-item-next') as HTMLElement);
    await waitFor(() => expect(rows()).toEqual(['批量任务 7']));
  });

  it('a list that fits has no pager', async () => {
    renderApp('/overview');
    await screen.findByTestId('run-running');
    expect(screen.getAllByTestId('active-task')).toHaveLength(1);
    expect(document.querySelector('.overview-pager')).toBeNull();
  });

  it('the counts lead to the filtered task list', async () => {
    const { user } = renderApp('/overview');
    await user.click(await screen.findByTestId('run-running'));
    await waitFor(() => expect(currentLocation()).toBe('/tasks?state=running'));
  });
});
