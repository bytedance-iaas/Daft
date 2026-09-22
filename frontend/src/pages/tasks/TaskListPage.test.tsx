import { screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { findTask } from '../../mocks/db';
import { server } from '../../mocks/server';
import { requiredFieldLabels } from '../../test/forms';
import { currentLocation, renderApp } from '../../test/render';

function row(name: string): HTMLElement {
  const link = screen.getByRole('link', { name });
  return link.closest('tr') as HTMLElement;
}

describe('任务列表 (07 §4.1)', () => {
  it('shows states in Chinese, 「错误」 for completed_with_errors, and the system pause hint', async () => {
    renderApp('/tasks');
    await screen.findByRole('link', { name: 'droid 前 50 条质检' });
    expect(within(row('droid 前 50 条质检')).getByText('错误')).toBeInTheDocument();
    expect(within(row('libero-10 抽检')).getByText('待启动')).toBeInTheDocument();
    expect(within(row('umi_640 全量质检')).getByText('运行中')).toBeInTheDocument();
    const widowx = row('widowx 回归');
    expect(within(widowx).getByText('已暂停')).toBeInTheDocument();
    expect(within(widowx).getByLabelText('系统将自动恢复')).toBeInTheDocument();
    expect(within(row('agibot 预检回归')).queryByLabelText('系统将自动恢复')).toBeNull();
    // Badges: pending adjudication links to the adjudication page; never exported → 「交付待导出」
    const droid = row('droid 前 50 条质检');
    expect(within(droid).getByRole('link', { name: '待裁决 10' })).toHaveAttribute('href', '/tasks/task_01HXR2D8/adjudication');
    expect(within(droid).getByText('交付待导出')).toBeInTheDocument();
    expect(within(droid).getByText('通过 41 · 拒绝 7 · 待补跑 2')).toBeInTheDocument();
    expect(screen.getByText('有未结束的任务，每 5 秒自动刷新')).toBeInTheDocument();
  });

  it('a task summary shows the episodes skipped for missing source files when there are any (D40)', async () => {
    renderApp('/tasks?q=so101');
    await screen.findByRole('link', { name: 'so101 夜间批次' });
    expect(row('so101 夜间批次')).toHaveTextContent('通过 968 · 拒绝 56 · 缺源文件 3');
  });

  it('summarises the modules column and names the preset (data driven from the registry)', async () => {
    renderApp('/tasks');
    await screen.findByRole('link', { name: 'droid 前 50 条质检' });
    const droid = within(row('droid 前 50 条质检')).getByTestId('module-summary');
    expect(droid).toHaveTextContent('7 项');
    expect(droid).toHaveTextContent('1 项错误');
    expect(droid).toHaveTextContent('完整质检');
    expect(within(row('libero-10 抽检')).getByTestId('module-summary')).toHaveTextContent('快速质检');
  });

  it('paginates by page number with a total and keeps the page in the URL', async () => {
    const { user } = renderApp('/tasks');
    expect(await screen.findByText('共 40 条')).toBeInTheDocument();
    await user.click(screen.getByText('2', { selector: '.arco-pagination-item' }));
    await waitFor(() => expect(currentLocation()).toContain('page=2'));
    expect(await screen.findByRole('link', { name: '历史批次 11' })).toBeInTheDocument();
  });

  it('filters by state, module and name through the API', async () => {
    const seen: string[] = [];
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname.endsWith('/api/v1/tasks')) seen.push(new URL(request.url).search);
    });
    renderApp('/tasks?state=completed_with_errors&module=task_success');
    expect(await screen.findByRole('link', { name: 'droid 前 50 条质检' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'libero-10 抽检' })).toBeNull();
    expect(seen.some((s) => s.includes('state=completed_with_errors') && s.includes('module=task_success'))).toBe(true);
    server.events.removeAllListeners();
  });

  it('searches by name', async () => {
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'droid 前 50 条质检' });
    await user.type(screen.getByPlaceholderText('搜索任务名称或 ID'), 'so101{Enter}');
    await waitFor(() => expect(currentLocation()).toContain('q=so101'));
    await waitFor(() => expect(screen.queryByRole('link', { name: 'droid 前 50 条质检' })).toBeNull());
    expect(screen.getByRole('link', { name: 'so101 夜间批次' })).toBeInTheDocument();
  });

  it('offers actions by state: a running task can be paused', async () => {
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'umi_640 全量质检' });
    await user.click(within(row('umi_640 全量质检')).getByRole('button', { name: /更多/ }));
    await user.click(await screen.findByRole('menuitem', { name: '暂停' }));
    expect(await screen.findByText('已请求暂停：在飞的 episode 跑完后停下')).toBeInTheDocument();
    await waitFor(() => expect(within(row('umi_640 全量质检')).getByText('已暂停')).toBeInTheDocument());
  });

  it('a system-paused task cannot be resumed by hand; unfinished tasks cannot be deleted', async () => {
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'widowx 回归' });
    expect(within(row('widowx 回归')).getByRole('button', { name: '恢复' })).toBeDisabled();
    await user.click(within(row('widowx 回归')).getByRole('button', { name: /更多/ }));
    const del = await screen.findByRole('menuitem', { name: '删除' });
    expect(del).toHaveClass('arco-dropdown-menu-disabled');
  });

  it('deleted tasks are listed under 已删除 and can be restored', async () => {
    const { user } = renderApp('/tasks?state=deleted');
    await screen.findByRole('link', { name: '误建的重复任务' });
    await user.click(within(row('误建的重复任务')).getByRole('button', { name: '恢复' }));
    expect(await screen.findByText('已恢复')).toBeInTheDocument();
  });

  it('清理交付产物 shows the exact run directory and needs the task name typed', async () => {
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'droid 前 50 条质检' });
    await user.click(within(row('droid 前 50 条质检')).getByRole('button', { name: /更多/ }));
    await user.click(await screen.findByRole('menuitem', { name: '清理交付产物' }));
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByTestId('purge-path')).toHaveTextContent('tos://pai-kit-deliveries/droid-50/20260920-130514/');
    const input = within(dialog).getByRole('textbox');
    // Required with a red asterisk, and must match the task name.
    expect(requiredFieldLabels(dialog)).toEqual(['输入任务名称「droid 前 50 条质检」确认']);
    await user.type(input, 'wrong');
    await user.click(within(dialog).getByRole('button', { name: '清理' }));
    expect(await within(dialog).findByText('和任务名称不一致')).toBeInTheDocument();
    await user.clear(input);
    await user.type(input, 'droid 前 50 条质检');
    await user.click(within(dialog).getByRole('button', { name: '清理' }));
    expect(await screen.findByText(/已开始清理 tos:\/\/pai-kit-deliveries\/droid-50\/20260920-130514\//)).toBeInTheDocument();
  });

  it('启动 that fails the pre-start checks lists every check with its reason and target (W8 details.checks)', async () => {
    findTask('task_01HXR6T3')!.output.credential = 'readonly-tos';
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'libero-10 抽检' });
    await user.click(within(row('libero-10 抽检')).getByRole('button', { name: '启动' }));
    const dialog = await screen.findByRole('dialog', { name: '开始前检查没过，任务没有开始' });
    const list = within(dialog).getByTestId('precheck-list');
    expect(list).toHaveTextContent('数据集能读：通过');
    expect(list).toHaveTextContent('交付目录能写：存储桶 pai-kit-deliveries 对访问密钥 readonly-tos 只读');
    expect(list).toHaveTextContent('tos://pai-kit-deliveries/libero_10-0920');
  });

  it('启动 shows the state the answer carries at once, without waiting for the next poll (07 §4.1)', async () => {
    // Every refetch of the list is held: what the row shows after the click can only come from
    // the answer of the action itself.
    let release = () => {};
    const held = new Promise<void>((r) => (release = r));
    let gets = 0;
    server.use(
      http.get('*/api/v1/tasks', async () => {
        gets += 1;
        if (gets > 1) await held;
      }),
    );
    const { user } = renderApp('/tasks');
    await screen.findByRole('link', { name: 'libero-10 抽检' });
    expect(within(row('libero-10 抽检')).getByText('待启动')).toBeInTheDocument();
    await user.click(within(row('libero-10 抽检')).getByRole('button', { name: '启动' }));
    await waitFor(() => expect(within(row('libero-10 抽检')).getByText('排队中')).toBeInTheDocument());
    expect(gets).toBeGreaterThan(1);
    release();
  });

  it('shows the page-level error with the Daemon message when the list cannot load', async () => {
    server.use(http.get('*/api/v1/tasks', () => HttpResponse.json({ error: { code: 'internal', message: '数据库暂时不可用，请稍后重试' } }, { status: 500 })));
    renderApp('/tasks');
    expect(await screen.findByText('数据库暂时不可用，请稍后重试')).toBeInTheDocument();
    expect(screen.getByText('页面数据没能取回')).toBeInTheDocument();
  });
});
