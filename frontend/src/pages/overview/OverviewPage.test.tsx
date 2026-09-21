import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { currentLocation, renderApp } from '../../test/render';

describe('概览 (07 §4.3, D36)', () => {
  it('shows what needs attention, what runs, the last 7 days and the datasets', async () => {
    renderApp('/overview');
    expect(await screen.findByTestId('todo-errors')).toHaveTextContent('1');
    expect(screen.getByTestId('todo-adjudication')).toHaveTextContent('42');
    expect(screen.getByTestId('todo-adjudication')).toHaveTextContent('条，分布在 2 个任务');
    expect(screen.getByTestId('todo-delivery')).toHaveTextContent('1');
    expect(screen.getByTestId('todo-datasets')).toHaveTextContent('1');
    expect(screen.getByTestId('todo-keys')).toHaveTextContent('1');
    expect(screen.getByTestId('todo-backends')).toHaveTextContent('1');
    expect(screen.getByTestId('run-running')).toHaveTextContent('1');
    expect(screen.getByTestId('run-queued')).toHaveTextContent('1');
    expect(screen.getByTestId('run-paused')).toHaveTextContent('2');
    const active = screen.getByTestId('active-tasks');
    expect(within(active).getByRole('link', { name: 'umi_640 全量质检' })).toBeInTheDocument();
    expect(active).toHaveTextContent('VLM 档 410 / 631');
    expect(screen.getByTestId('ds-total')).toHaveTextContent('5');
    expect(screen.getAllByTestId('chart')[0]).toHaveAttribute('aria-label', expect.stringMatching(/\d{2}-\d{2} \d+/));
    expect(screen.getByText('有未结束的任务，每 5 秒自动刷新')).toBeInTheDocument();
  });

  it('every item leads to its pre-filtered list', async () => {
    const { user } = renderApp('/overview');
    await user.click(await screen.findByTestId('todo-errors'));
    await waitFor(() => expect(currentLocation()).toBe('/tasks?state=completed_with_errors'));
  });

  it('lists the tasks with pending adjudication (no list filter for it in C4)', async () => {
    const { user } = renderApp('/overview');
    const card = await screen.findByTestId('todo-adjudication');
    await user.click(within(card).getByRole('button', { name: '看是哪些任务' }));
    expect(await screen.findByRole('link', { name: 'droid 前 50 条质检' })).toHaveAttribute('href', '/tasks/task_01HXR2D8/adjudication');
    expect(screen.getByRole('link', { name: 'droid-200 抽检' })).toHaveAttribute('href', '/tasks/task_01HXQ5R9/adjudication');
  });
});
